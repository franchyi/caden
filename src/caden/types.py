"""Shared value types for Caden's lifecycle and execution interfaces.

The policy consumes only generation-fenced, content-free lifecycle metadata.
Sandbox memory movement remains separate from any model/KV-cache mechanism.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

SandboxId = str
ReqId = str


class Stage(enum.Enum):
    LLM_WAIT = "LLM_WAIT"
    RESPONSE_WAKE = "RESPONSE_WAKE"
    TOOL_BURST = "TOOL_BURST"
    RESULT_PACK = "RESULT_PACK"


class CpuClass(enum.Enum):
    IDLE = "IDLE"
    NORMAL = "NORMAL"
    BOOST = "BOOST"


class Tier(enum.Enum):
    DRAM = "DRAM"
    CXL = "CXL"
    COMPRESSED = "COMPRESSED"
    SSD = "SSD"


class ReclaimMode(enum.Enum):
    """Which page population proactive cgroup reclaim should prefer."""

    BALANCED = "BALANCED"
    FILE_ONLY = "FILE_ONLY"
    ANON_ONLY = "ANON_ONLY"


class ResidencyMode(enum.Enum):
    """Information available to the sandbox residency policy."""

    FIXED_GRACE = "FIXED_GRACE"
    ELAPSED = "ELAPSED"
    REQUEST_AWARE = "REQUEST_AWARE"


@dataclass(frozen=True)
class StageContext:
    """Content-free metadata attached to a lifecycle transition."""

    request_class: str = "default"
    restore_profile: str = "default"


@dataclass(frozen=True)
class RestoreHint:
    """A reversible, generation-fenced request to consider early restore."""

    probability: float
    horizon_seconds: float
    generation: int
    profile: str = "default"
    hint_id: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError("restore-hint probability must be in [0, 1]")
        if self.horizon_seconds < 0:
            raise ValueError("restore-hint horizon must not be negative")
        if self.generation < 0:
            raise ValueError("restore-hint generation must not be negative")


@dataclass(frozen=True)
class DemotionRequest:
    """Selective sandbox-memory demotion request.

    ``target_bytes`` is an upper target rather than a promise: Linux may
    under- or over-reclaim. ``min_resident_bytes`` bounds the request around a
    best-effort hot capsule. A COMPRESSED request requires configured zswap
    backend and is never silently treated as SSD.
    """

    tier: Tier
    target_bytes: int | None = None
    min_resident_bytes: int = 0
    reclaim_mode: ReclaimMode = ReclaimMode.BALANCED
    # Optional lifecycle generation. Tier backends fence out a request older
    # than the newest generation they already accepted for the sandbox.
    generation: int | None = None

    def __post_init__(self) -> None:
        if self.generation is not None and self.generation < 0:
            raise ValueError("demotion generation must not be negative")
        if self.tier is Tier.DRAM:
            raise ValueError("DRAM is not a demotion target")
        if self.target_bytes is not None and self.target_bytes < 0:
            raise ValueError("demotion target must not be negative")
        if self.min_resident_bytes < 0:
            raise ValueError("minimum resident bytes must not be negative")
        if (
            self.tier is Tier.COMPRESSED
            and self.reclaim_mode is not ReclaimMode.ANON_ONLY
        ):
            raise ValueError("compressed demotion requires anonymous-only reclaim")


@dataclass(frozen=True)
class DemotionResult:
    requested_bytes: int
    reclaimed_bytes: int
    before_bytes: int
    after_bytes: int
    swap_delta_bytes: int = 0
    compressed_delta_bytes: int = 0
    tier: Tier = Tier.SSD
    reclaim_mode: ReclaimMode = ReclaimMode.BALANCED
    # Movement receipt. ``reclaimed_bytes`` stays the cgroup-charge delta used
    # by policy. The fields below are backend evidence and are never inferred
    # from one another: bytes the backend found eligible, bytes written to the
    # tier, and bytes the source mapping measurably released.
    backend: str = ""
    eligible_bytes: int = 0
    stored_bytes: int = 0
    released_bytes: int = 0
    file_reclaimed_bytes: int = 0
    partial: bool = False
    generation: int | None = None


@dataclass(frozen=True)
class RestoreRequest:
    profile: str = "default"
    speculative: bool = False
    generation: int | None = None

    def __post_init__(self) -> None:
        if self.generation is not None and self.generation < 0:
            raise ValueError("restore generation must not be negative")


@dataclass(frozen=True)
class RestoreResult:
    resident_delta_bytes: int = 0
    swap_delta_bytes: int = 0
    compressed_delta_bytes: int = 0
    prefetched_bytes: int = 0
    advised_bytes: int = 0
    ready_for_dispatch: bool = True
    profile: str = "default"
    # Backend receipt: bytes copied back before dispatch was permitted, bytes
    # left for demand faults (their cost stays inside the timed tool call).
    backend: str = ""
    tier_restored_bytes: int = 0
    lazy_pending_bytes: int = 0
    generation: int | None = None


@dataclass
class AgentTask:
    cmd: list[str]
    repo: str
    klass: str = "interactive"


@dataclass
class SandboxStat:
    stage: Stage
    cpu_class: CpuClass
    mem_dram_bytes: int
    mem_demoted_bytes: int
    major_faults: int
    mem_swap_bytes: int = 0
    mem_compressed_bytes: int = 0
    cpu_usage_usec: int = 0
    page_faults: int = 0


@dataclass
class HostStat:
    dram_free_bytes: int
    dram_total_bytes: int
    cxl_free_bytes: int
    cxl_total_bytes: int
