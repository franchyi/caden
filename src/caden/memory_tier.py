"""Shared memory-tier backend contract for sandbox residency.

Caden policy decides *when* a waiting sandbox is demoted or woken. The execution
layer owns lifecycle and CPU quiescence (freeze/thaw, prewarm, dispatch). A
``MemoryTierBackend`` owns only the byte movement underneath that decision and
the evidence for it. Policy must not contain device paths, mmap calls or
backend-specific paging procedures; backends must not thaw or dispatch work.

Two implementations share this contract:

* ``SSDTierBackend`` (``caden.sandboxfs_backend``): adapter to the existing
  kernel path - cgroup v2 ``memory.reclaim`` against the already configured
  host swap/zswap. A reclaim request is not proof that bytes reached a
  particular physical device.
* ``CXLTierBackend`` (``caden.cxl_tier``): native pager daemon that copies
  registered cooperative mappings into the mmap cold store, releases their
  source pages, and serves eager or demand restores.

The contract unifies policy-facing semantics, not the kernel mechanism. The two
backends cover different page populations; see each backend's capabilities.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .types import (
    DemotionRequest,
    DemotionResult,
    RestoreRequest,
    RestoreResult,
    SandboxId,
    Tier,
)


class MemoryTierError(RuntimeError):
    """Base class for tier-backend failures."""


class UnsupportedTierError(MemoryTierError):
    """The selected backend cannot honor this placement; never substituted."""


class StaleGenerationError(MemoryTierError):
    """A request was fenced out by a newer generation or sandbox incarnation."""


class TierCapacityError(MemoryTierError):
    """The backend store or per-sandbox allocation is exhausted."""


@dataclass(frozen=True)
class TierCapabilities:
    """Static, run-artifact-facing description of one configured backend."""

    backend: str
    medium: str
    supported_tiers: frozenset[Tier]
    eligible_memory: tuple[str, ...]
    ineligible_memory: tuple[str, ...]
    placement_guarantee: str
    source_release_contract: str
    eager_restore: bool
    lazy_restore: bool
    speculative_preparation: bool
    required_permissions: tuple[str, ...]
    evidence_class: str
    codec: str = "none"
    details: dict[str, object] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "medium": self.medium,
            "supported_tiers": sorted(tier.value for tier in self.supported_tiers),
            "eligible_memory": list(self.eligible_memory),
            "ineligible_memory": list(self.ineligible_memory),
            "placement_guarantee": self.placement_guarantee,
            "source_release_contract": self.source_release_contract,
            "eager_restore": self.eager_restore,
            "lazy_restore": self.lazy_restore,
            "speculative_preparation": self.speculative_preparation,
            "required_permissions": list(self.required_permissions),
            "evidence_class": self.evidence_class,
            "codec": self.codec,
            "details": dict(self.details),
        }


@dataclass
class TierAccounting:
    """Per-sandbox movement accounting; every quantity is reported separately.

    ``requested`` is what policy asked for, ``stored`` what reached the tier,
    ``released`` what the source mapping measurably gave back, ``restored`` what
    returned to DRAM. None of them is inferred from another.
    """

    requested_bytes: int = 0
    stored_bytes: int = 0
    released_bytes: int = 0
    restored_bytes: int = 0
    resident_in_tier_bytes: int = 0
    demand_faults: int = 0
    demotions: int = 0
    restores: int = 0
    stale_rejections: int = 0
    errors: int = 0
    metadata_bytes: int = 0
    details: dict[str, object] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "requested_bytes": self.requested_bytes,
            "stored_bytes": self.stored_bytes,
            "released_bytes": self.released_bytes,
            "restored_bytes": self.restored_bytes,
            "resident_in_tier_bytes": self.resident_in_tier_bytes,
            "demand_faults": self.demand_faults,
            "demotions": self.demotions,
            "restores": self.restores,
            "stale_rejections": self.stale_rejections,
            "errors": self.errors,
            "metadata_bytes": self.metadata_bytes,
            "details": dict(self.details),
        }


@runtime_checkable
class TierSandbox(Protocol):
    """Identity a backend may rely on; supplied by the execution layer.

    ``incarnation`` distinguishes reuse of a sandbox identifier. The execution
    layer guarantees the cgroup is frozen (quiescent) before ``demote`` and
    stays frozen until a confirmed ``restore`` returns successfully.
    """

    id: SandboxId
    cgroup_path: Path
    incarnation: int


class GenerationFence:
    """Monotonic per-sandbox fence shared by every backend.

    Requests may carry the policy's lifecycle generation. A request older than
    the newest generation already accepted for that sandbox incarnation is
    rejected, so a late demotion cannot run after the wake that superseded it.
    Requests without a generation (compatibility callers) are not fenced here;
    they remain serialized by the per-sandbox movement lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[tuple[SandboxId, int], int] = {}

    def admit(self, sandbox: TierSandbox, generation: int | None) -> None:
        if generation is None:
            return
        key = (sandbox.id, sandbox.incarnation)
        with self._lock:
            latest = self._latest.get(key)
            if latest is not None and generation < latest:
                raise StaleGenerationError(
                    f"generation {generation} for {sandbox.id!r} is older than "
                    f"accepted generation {latest}"
                )
            self._latest[key] = generation

    def forget(self, sandbox: TierSandbox) -> None:
        with self._lock:
            self._latest.pop((sandbox.id, sandbox.incarnation), None)


@runtime_checkable
class MemoryTierBackend(Protocol):
    """Byte movement beneath Caden's residency decisions.

    Ordering contract (enforced by the execution layer, relied on by backends):

    1. ``attach`` once per sandbox incarnation, before any movement.
    2. ``demote`` only while the sandbox cgroup is confirmed frozen. Partial
       progress is reported, not discarded; a failed demotion leaves every
       unreleased page resident and usable.
    3. ``restore(speculative=True)`` is preparation-only: it must not thaw,
       dispatch, or report ``ready_for_dispatch``.
    4. ``restore(speculative=False)`` runs *before* thaw. It returns only when
       the backend's declared readiness contract holds (all pages resident for
       eager restore; fault service armed for lazy restore). If it raises, the
       execution layer must not thaw or dispatch.
    5. ``finish_restore`` runs after a confirmed restore attempt, success or
       failure, for backend-side cleanup that must not be skipped.
    6. Before ``detach(discard=True)`` the execution layer proves consumers
       have exited; restore failure never authorizes thaw. Cleanup errors
       retain a non-dispatchable record for retry.
       ``detach`` synchronizes with in-flight movement, then discards the
       sandbox's tier state. After it returns no store page belongs to the
       sandbox. ``close`` refuses while attached sandboxes still hold pages.
    """

    name: str

    def capabilities(self) -> TierCapabilities: ...

    def validate(self, tier: Tier) -> None: ...

    def attach(self, sandbox: TierSandbox) -> None: ...

    def demote(
        self, sandbox: TierSandbox, request: DemotionRequest
    ) -> DemotionResult: ...

    def restore(
        self, sandbox: TierSandbox, request: RestoreRequest
    ) -> RestoreResult: ...

    def finish_restore(self, sandbox: TierSandbox, request: RestoreRequest) -> None: ...

    def accounting(self, sandbox: TierSandbox) -> TierAccounting: ...

    def capacity(self) -> tuple[int, int]:
        """``(free_bytes, total_bytes)`` of the tier medium; zeros if unknown."""
        ...

    def detach(self, sandbox: TierSandbox, *, discard: bool = True) -> TierAccounting: ...

    def close(self) -> None: ...
