"""Concrete Caden execution layer backed by the SandboxFS host API.

SandboxFS owns workspace construction, Bubblewrap, sandboxd, and the cgroup
lifecycle.  This adapter resolves the created sandbox's delegated cgroup from
its PID and applies Caden's CPU/memory residency policy through cgroup v2.

Lifecycle and CPU quiescence (freeze, confirmed thaw, prewarm, dispatch) stay
in :class:`SandboxFSExecution`. Byte movement beneath a residency decision is
delegated to an injected :class:`caden.memory_tier.MemoryTierBackend`; the
default :class:`SSDTierBackend` is the unchanged kernel reclaim path.
"""

from __future__ import annotations

import ctypes
import errno
import inspect
import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .execution import StageReport
from .service_cache import ServiceCacheController
from .memory_tier import (
    GenerationFence,
    MemoryTierBackend,
    TierAccounting,
    TierCapabilities,
    TierSandbox,
    UnsupportedTierError,
)
from .types import (
    AgentTask,
    CpuClass,
    DemotionRequest,
    DemotionResult,
    HostStat,
    ReclaimMode,
    RestoreRequest,
    RestoreResult,
    SandboxId,
    SandboxStat,
    Stage,
    StageContext,
    Tier,
)

_MADV_WILLNEED = 3
_MADV_POPULATE_READ = 22


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    argv: Sequence[str], timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@dataclass(frozen=True)
class SandboxFSConfig:
    ctl_path: str = "sandboxfsctl"
    socket_path: str = "/run/sandboxfsd.sock"
    mode: str = "t1"
    request_timeout_seconds: float = 600.0
    proc_root: Path = Path("/proc")
    cgroup_root: Path = Path("/sys/fs/cgroup")
    meminfo_path: Path = Path("/proc/meminfo")
    cpu_weights: dict[CpuClass, int] = field(
        default_factory=lambda: {
            CpuClass.IDLE: 1,
            CpuClass.NORMAL: 100,
            CpuClass.BOOST: 10_000,
        }
    )
    freeze_timeout_seconds: float = 2.0
    reclaim_settle_seconds: float = 0.05
    # Explicit delegation only. Never infer or reclaim a service parent.
    service_cache_roots: dict[str, Path] = field(default_factory=dict)
    service_cache_reserve_bytes: int = 128 << 20
    service_cache_chunk_bytes: int = 16 << 20
    service_cache_interval_seconds: float = 0.1
    service_cache_bytes_per_interval: int = 16 << 20
    # Preserve the historical probe by default. "thaw-only" is an explicit
    # ablation: confirm cgroup thaw, then let the real tool fault in its pages.
    # Prewarm profiles/argv apply only to "prewarm", never to speculative work.
    restore_mode: str = "prewarm"
    prewarm_argv: tuple[str, ...] = ("/bin/true",)
    prewarm_profiles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    speculative_prefetch_profiles: dict[str, tuple[Path, ...]] = field(
        default_factory=dict
    )
    speculative_prefetch_roots: tuple[Path, ...] = ()
    speculative_prefetch_max_bytes: int = 256 << 20
    allow_process_madvise_restore: bool = False
    speculative_madvise_profiles: tuple[str, ...] = ("default",)
    speculative_madvise_max_bytes: int = 256 << 20
    speculative_madvise_settle_seconds: float = 0.0
    speculative_madvise_passes: int = 1
    speculative_madvise_advice: str = "willneed"
    allow_zswap_compression: bool = False
    zswap_enabled_path: Path = Path("/sys/module/zswap/parameters/enabled")
    zswap_max_bytes: int = 0
    swaps_path: Path = Path("/proc/swaps")

    def __post_init__(self) -> None:
        if self.restore_mode not in {"prewarm", "thaw-only"}:
            raise ValueError("restore_mode must be prewarm or thaw-only")
        if self.speculative_prefetch_max_bytes < 0:
            raise ValueError("speculative_prefetch_max_bytes must not be negative")
        configured_paths = [
            path
            for paths in self.speculative_prefetch_profiles.values()
            for path in paths
        ]
        if configured_paths and not self.speculative_prefetch_roots:
            raise ValueError(
                "speculative prefetch files require an explicit immutable root"
            )
        for path in configured_paths:
            if not _path_within_roots(path, self.speculative_prefetch_roots):
                raise ValueError(
                    f"speculative prefetch path is outside configured roots: {path}"
                )
        if self.speculative_madvise_max_bytes < 0:
            raise ValueError("speculative_madvise_max_bytes must not be negative")
        if self.speculative_madvise_settle_seconds < 0:
            raise ValueError("speculative_madvise_settle_seconds must not be negative")
        if not 1 <= self.speculative_madvise_passes <= 16:
            raise ValueError("speculative_madvise_passes must be between 1 and 16")
        if self.speculative_madvise_advice not in {"willneed", "populate-read"}:
            raise ValueError(
                "speculative_madvise_advice must be willneed or populate-read"
            )
        if (
            self.allow_process_madvise_restore
            and self.speculative_madvise_max_bytes == 0
        ):
            raise ValueError("process_madvise restore requires a positive byte budget")
        if self.zswap_max_bytes < 0:
            raise ValueError("zswap_max_bytes must not be negative")


@dataclass
class SandboxFSRecord:
    id: SandboxId
    task: AgentTask
    state: dict[str, object]
    cgroup_path: Path
    stage: Stage = Stage.RESPONSE_WAKE
    cpu_class: CpuClass = CpuClass.NORMAL
    last_reclaimed_bytes: int = 0
    last_tier: Tier = Tier.DRAM
    zswap_writeback_original: str | None = None
    on_report: StageReport | None = None
    # Distinguishes reuse of a sandbox identifier for tier-backend fencing.
    incarnation: int = 0
    residency_lock: threading.RLock = field(default_factory=threading.RLock)
    revoking: bool = False
    destroyed: bool = False
    dispatch_ready: bool = True


class SandboxFSError(RuntimeError):
    pass


class SandboxFSTierUnsupported(SandboxFSError, UnsupportedTierError):
    """Requested placement is outside the configured tier backend."""


class SSDTierBackend:
    """Adapter to the existing kernel path: cgroup reclaim to configured swap.

    This is the DRAM-SSD behavior extracted unchanged from the execution layer:
    ``memory.reclaim`` with the historical swappiness arguments, optional
    fail-closed zswap, and non-dispatching speculative preparation (immutable
    file prefetch and ``process_madvise``). It activates no swap device and
    changes no host-wide setting. A reclaim request does not prove that bytes
    reached a particular physical device: the receipt reports the cgroup charge
    delta, the swap delta and the zswap delta separately, and clean file pages
    are simply dropped rather than stored anywhere.
    """

    name = "ssd"

    def __init__(
        self,
        config: SandboxFSConfig,
        *,
        cgroup_for_pid: Callable[[int], Path] | None = None,
    ) -> None:
        self.config = config
        self._cgroup_for_pid = cgroup_for_pid or (
            lambda pid: _resolve_cgroup_for_pid(config, pid)
        )
        self._lock = threading.RLock()
        self._fence = GenerationFence()
        self._movement: dict[tuple[SandboxId, int], threading.RLock] = {}
        self._accounting: dict[tuple[SandboxId, int], TierAccounting] = {}

    def capabilities(self) -> TierCapabilities:
        try:
            zswap_state = self.config.zswap_enabled_path.read_text().strip()
        except OSError:
            zswap_state = "unavailable"
        return TierCapabilities(
            backend=self.name,
            medium="host-configured-swap",
            supported_tiers=frozenset({Tier.SSD, Tier.COMPRESSED}),
            eligible_memory=(
                "every page charged to the sandbox cgroup that kernel reclaim "
                "selects: clean/dirty file cache, anonymous and tmpfs pages",
            ),
            ineligible_memory=("pages below the requested hot floor", "mlocked pages"),
            placement_guarantee=(
                "none: the kernel chooses pages and the swap device; clean file "
                "pages are dropped, not stored"
            ),
            source_release_contract=(
                "cgroup memory.current delta after memory.reclaim while frozen"
            ),
            eager_restore=False,
            lazy_restore=True,
            speculative_preparation=True,
            required_permissions=("write access to the sandbox cgroup v2 files",),
            evidence_class="kernel-reclaim",
            codec="zswap" if self.config.allow_zswap_compression else "none",
            details={
                "configured_swaps": _read_swaps(self.config.swaps_path),
                "zswap_state": zswap_state,
                "zswap_compression_allowed": self.config.allow_zswap_compression,
                "activates_swap_device": False,
            },
        )

    def validate(self, tier: Tier, sandbox: TierSandbox | None = None) -> None:
        if tier not in {Tier.SSD, Tier.COMPRESSED}:
            raise SandboxFSTierUnsupported(
                "stock SandboxFS backend supports cgroup reclaim to configured "
                f"swap/zswap only, not {tier.value} placement"
            )
        if sandbox is not None and tier is Tier.COMPRESSED:
            self._validate_compressed_tier(sandbox)

    def attach(self, sandbox: TierSandbox) -> None:
        key = (sandbox.id, sandbox.incarnation)
        with self._lock:
            self._movement[key] = threading.RLock()
            self._accounting[key] = TierAccounting()

    def demote(self, sandbox: TierSandbox, request: DemotionRequest) -> DemotionResult:
        self.validate(request.tier)
        with self._movement_lock(sandbox):
            try:
                self._fence.admit(sandbox, request.generation)
            except Exception:
                self._account(sandbox).stale_rejections += 1
                raise
            return self._demote_locked(sandbox, request)

    def _demote_locked(
        self, record: TierSandbox, request: DemotionRequest
    ) -> DemotionResult:
        before = _read_int(record.cgroup_path / "memory.current")
        swap_before = _read_int(record.cgroup_path / "memory.swap.current", missing=0)
        zswap_before = _read_int(record.cgroup_path / "memory.zswap.current", missing=0)
        reclaimable = max(0, before - request.min_resident_bytes)
        requested = min(
            reclaimable,
            reclaimable if request.target_bytes is None else request.target_bytes,
        )
        if requested <= 0:
            return DemotionResult(
                requested_bytes=0,
                reclaimed_bytes=0,
                before_bytes=before,
                after_bytes=before,
                tier=request.tier,
                reclaim_mode=request.reclaim_mode,
                backend=self.name,
                generation=request.generation,
            )
        if request.tier is Tier.COMPRESSED:
            self._configure_compressed_tier(record)
        reclaim_value = str(requested)
        if request.reclaim_mode is ReclaimMode.FILE_ONLY:
            reclaim_value += " swappiness=0"
        elif request.reclaim_mode is ReclaimMode.ANON_ONLY:
            reclaim_value += " swappiness=200"
        partial_error: SandboxFSError | None = None
        try:
            _write(record.cgroup_path / "memory.reclaim", reclaim_value)
        except SandboxFSError as error:
            # EAGAIN means the kernel made partial progress. Report the
            # measurable delta rather than discarding useful work.
            if not _caused_by_errno(error, errno.EAGAIN):
                self._account(record).errors += 1
                raise
            partial_error = error
        if self.config.reclaim_settle_seconds > 0:
            time.sleep(self.config.reclaim_settle_seconds)
        after = _read_int(record.cgroup_path / "memory.current")
        swap_after = _read_int(record.cgroup_path / "memory.swap.current", missing=0)
        zswap_after = _read_int(record.cgroup_path / "memory.zswap.current", missing=0)
        reclaimed = max(0, before - after)
        if partial_error is not None and reclaimed == 0:
            self._account(record).errors += 1
            raise partial_error
        swap_delta = max(0, swap_after - swap_before)
        compressed_delta = max(0, zswap_after - zswap_before)
        accounting = self._account(record)
        accounting.requested_bytes += requested
        # Only the swap/zswap deltas are evidence of stored bytes; dropped
        # clean file pages release DRAM without being placed anywhere.
        accounting.stored_bytes += swap_delta + compressed_delta
        accounting.released_bytes += reclaimed
        accounting.resident_in_tier_bytes = swap_after + zswap_after
        accounting.demotions += 1
        return DemotionResult(
            requested_bytes=requested,
            reclaimed_bytes=reclaimed,
            before_bytes=before,
            after_bytes=after,
            swap_delta_bytes=swap_delta,
            compressed_delta_bytes=compressed_delta,
            tier=request.tier,
            reclaim_mode=request.reclaim_mode,
            backend=self.name,
            eligible_bytes=reclaimable,
            stored_bytes=swap_delta + compressed_delta,
            released_bytes=reclaimed,
            file_reclaimed_bytes=max(0, reclaimed - swap_delta - compressed_delta),
            partial=partial_error is not None,
            generation=request.generation,
        )

    def restore(self, sandbox: TierSandbox, request: RestoreRequest) -> RestoreResult:
        """Kernel swap-in is demand driven, so a confirmed restore moves nothing.

        Dispatch readiness is the execution layer's confirmed thaw. Speculative
        preparation may populate pages but never reports readiness.
        """
        with self._movement_lock(sandbox):
            try:
                self._fence.admit(sandbox, request.generation)
            except Exception:
                self._account(sandbox).stale_rejections += 1
                raise
            prefetched_bytes = 0
            advised_bytes = 0
            if request.speculative:
                prefetched_bytes = self._prefetch_profile(request.profile)
                advised_bytes = self._madvise_profile(sandbox, request.profile)
            accounting = self._account(sandbox)
            accounting.restores += 1
            accounting.restored_bytes += advised_bytes
            return RestoreResult(
                prefetched_bytes=prefetched_bytes,
                advised_bytes=advised_bytes,
                ready_for_dispatch=not request.speculative,
                profile=request.profile,
                backend=self.name,
                generation=request.generation,
            )

    def finish_restore(self, sandbox: TierSandbox, request: RestoreRequest) -> None:
        if not request.speculative:
            self._restore_zswap_writeback(sandbox)

    def accounting(self, sandbox: TierSandbox) -> TierAccounting:
        return self._account(sandbox)

    def capacity(self) -> tuple[int, int]:
        return 0, 0

    def detach(self, sandbox: TierSandbox, *, discard: bool = True) -> TierAccounting:
        key = (sandbox.id, sandbox.incarnation)
        with self._movement_lock(sandbox):
            with self._lock:
                accounting = self._accounting.pop(key, TierAccounting())
                self._movement.pop(key, None)
            self._fence.forget(sandbox)
        return accounting

    def close(self) -> None:
        return None

    def _movement_lock(self, sandbox: TierSandbox) -> threading.RLock:
        key = (sandbox.id, sandbox.incarnation)
        with self._lock:
            return self._movement.setdefault(key, threading.RLock())

    def _account(self, sandbox: TierSandbox) -> TierAccounting:
        key = (sandbox.id, sandbox.incarnation)
        with self._lock:
            return self._accounting.setdefault(key, TierAccounting())

    def _prefetch_profile(self, profile: str) -> int:
        paths = self.config.speculative_prefetch_profiles.get(profile, ())
        remaining = self.config.speculative_prefetch_max_bytes
        if remaining <= 0:
            return 0
        prefetched = 0
        for path in paths:
            if remaining <= 0:
                break
            if not _path_within_roots(path, self.config.speculative_prefetch_roots):
                raise SandboxFSError(
                    f"speculative prefetch path escaped configured roots: {path}"
                )
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags)
            except OSError as error:
                raise SandboxFSError(
                    f"open speculative prefetch path {path}: {error}"
                ) from error
            try:
                if hasattr(os, "posix_fadvise"):
                    os.posix_fadvise(
                        descriptor,
                        0,
                        remaining,
                        getattr(os, "POSIX_FADV_WILLNEED", _MADV_WILLNEED),
                    )
                while remaining > 0:
                    chunk = os.read(descriptor, min(1 << 20, remaining))
                    if not chunk:
                        break
                    size = len(chunk)
                    prefetched += size
                    remaining -= size
            except OSError as error:
                raise SandboxFSError(f"prefetch host path {path}: {error}") from error
            finally:
                os.close(descriptor)
        return prefetched

    def _madvise_profile(self, record: TierSandbox, profile: str) -> int:
        if (
            not self.config.allow_process_madvise_restore
            or profile not in self.config.speculative_madvise_profiles
        ):
            return 0
        procs_path = record.cgroup_path / "cgroup.procs"
        try:
            pids = sorted(
                {
                    int(line)
                    for line in procs_path.read_text().splitlines()
                    if line.strip()
                }
            )
        except (OSError, ValueError) as error:
            raise SandboxFSError(
                f"read exact cgroup PIDs from {procs_path}: {error}"
            ) from error
        limit = self.config.speculative_madvise_max_bytes
        advised = 0
        for pass_number in range(self.config.speculative_madvise_passes):
            remaining = limit
            pass_advised = 0
            for pid in pids:
                if remaining <= 0:
                    break
                try:
                    # Open first so PID reuse cannot redirect the later advice.
                    pidfd = _open_pidfd(pid)
                except SandboxFSError as error:
                    if _caused_by_errno(error, errno.ESRCH):
                        continue
                    raise
                try:
                    try:
                        actual_cgroup = self._cgroup_for_pid(pid)
                    except FileNotFoundError:
                        continue
                    except SandboxFSError as error:
                        # A control process can exit between cgroup.procs and /proc.
                        if _caused_by_errno(error, errno.ENOENT):
                            continue
                        raise
                    if actual_cgroup != record.cgroup_path.resolve():
                        raise SandboxFSError(
                            f"PID {pid} moved outside sandbox cgroup before pre-restore"
                        )
                    try:
                        ranges = _anonymous_readable_ranges(
                            self.config.proc_root / str(pid) / "maps", remaining
                        )
                    except SandboxFSError as error:
                        if _caused_by_errno(error, errno.ENOENT):
                            continue
                        raise
                    if not ranges:
                        continue
                    if self.config.speculative_madvise_advice == "populate-read":
                        moved = _process_madvise_populate_read(pid, pidfd, ranges)
                    else:
                        moved = _process_madvise_willneed(pid, pidfd, ranges)
                    pass_advised += moved
                    remaining -= min(remaining, moved)
                finally:
                    os.close(pidfd)
            advised += pass_advised
            if (
                pass_advised
                and self.config.speculative_madvise_settle_seconds > 0
                and pass_number + 1 < self.config.speculative_madvise_passes
            ):
                time.sleep(self.config.speculative_madvise_settle_seconds)
        if advised and self.config.speculative_madvise_settle_seconds > 0:
            time.sleep(self.config.speculative_madvise_settle_seconds)
        return advised

    def _validate_compressed_tier(self, record: TierSandbox) -> None:
        if not self.config.allow_zswap_compression:
            raise SandboxFSError(
                "compressed tier is fail-closed; set allow_zswap_compression "
                "only after host zswap preflight"
            )
        try:
            enabled = self.config.zswap_enabled_path.read_text().strip().lower()
        except OSError as error:
            raise SandboxFSError(f"read zswap enable state: {error}") from error
        if enabled not in {"1", "y", "yes"}:
            raise SandboxFSError("compressed tier requested but host zswap is disabled")
        for name in ("memory.zswap.current", "memory.zswap.writeback"):
            if not (record.cgroup_path / name).exists():
                raise SandboxFSError(f"compressed tier requires cgroup v2 {name}")
        if (
            self.config.zswap_max_bytes > 0
            and not (record.cgroup_path / "memory.zswap.max").exists()
        ):
            raise SandboxFSError("zswap_max_bytes requires cgroup v2 memory.zswap.max")

    def _configure_compressed_tier(self, record: TierSandbox) -> None:
        writeback = record.cgroup_path / "memory.zswap.writeback"
        with self._lock:
            if getattr(record, "zswap_writeback_original", None) is None:
                try:
                    record.zswap_writeback_original = writeback.read_text().strip()  # type: ignore[attr-defined]
                except OSError as error:
                    raise SandboxFSError(f"read {writeback}: {error}") from error
        # Prevent this cgroup's compressed pages from silently becoming an SSD
        # treatment. Global zswap enablement remains an operator precondition.
        _write(writeback, "0")
        if self.config.zswap_max_bytes > 0:
            _write(
                record.cgroup_path / "memory.zswap.max",
                str(self.config.zswap_max_bytes),
            )

    def _restore_zswap_writeback(self, record: TierSandbox) -> None:
        with self._lock:
            original = getattr(record, "zswap_writeback_original", None)
            record.zswap_writeback_original = None  # type: ignore[attr-defined]
        if original is not None:
            _write(record.cgroup_path / "memory.zswap.writeback", original)


class SandboxFSExecution:
    """Caden's lifecycle, CPU, memory, and observation paths over SandboxFS.

    ``run`` creates a ready SandboxFS sandbox but does not interpret the agent
    command or invent stage transitions.  The agent runtime executes through
    :meth:`exec` and reports its real stages to Caden.
    """

    def __init__(
        self,
        config: SandboxFSConfig | None = None,
        *,
        runner: CommandRunner = _default_runner,
        id_factory: Callable[[], str] | None = None,
        memory_tier: MemoryTierBackend | None = None,
    ) -> None:
        self.config = config or SandboxFSConfig()
        self._runner = runner
        self._id_factory = id_factory or (lambda: f"caden-{uuid.uuid4().hex[:16]}")
        self._records: dict[SandboxId, SandboxFSRecord] = {}
        self._lock = threading.RLock()
        self._incarnations = 0
        self._residency_fence = GenerationFence()
        # DRAM-SSD stays the default; any other backend is explicit injection.
        self._tier: MemoryTierBackend = memory_tier or SSDTierBackend(
            self.config, cgroup_for_pid=self._cgroup_for_pid
        )
        self._tier_is_cxl = Tier.CXL in self._tier.capabilities().supported_tiers
        self.service_cache = (ServiceCacheController(
            self.config.service_cache_roots,
            reserve_bytes=self.config.service_cache_reserve_bytes,
            chunk_bytes=self.config.service_cache_chunk_bytes,
            interval_seconds=self.config.service_cache_interval_seconds,
            max_bytes_per_interval=self.config.service_cache_bytes_per_interval,
            idle=self._service_idle,
        ) if self.config.service_cache_roots else None)

    def _service_idle(self, base: str) -> bool:
        with self._lock:
            return all(record.stage is Stage.LLM_WAIT for record in self._records.values()
                       if record.task.repo == base)

    def _service_foreground(self, base: str):
        return self.service_cache.foreground(base) if self.service_cache else nullcontext()

    def run(self, task: AgentTask, on_report: StageReport) -> SandboxId:
        sandbox_id = self._id_factory()
        with self._service_foreground(task.repo):
            state = self._ctl_json(
                "create", "--id", sandbox_id, "--base", task.repo,
                "--mode", self.config.mode,
            )
        pid = _required_int(state, "pid")
        try:
            cgroup_path = self._cgroup_for_pid(pid)
            if self.service_cache:
                delegated = self.config.service_cache_roots[task.repo]
                if (cgroup_path.parent.name != "sandboxfs"
                        or cgroup_path.parent.parent != delegated.parent):
                    raise SandboxFSError("created sandbox does not belong to its delegated cache service")
        except Exception:  # preserve the original attach failure
            # Creation succeeded on the host, so failure to attach the Caden
            # control path must not leak the sandbox.
            try:
                self._ctl_json("destroy", sandbox_id)
            except Exception:  # noqa: BLE001, S110 - best-effort rollback
                pass  # rollback failure must not hide the original attach failure
            raise
        with self._lock:
            self._incarnations += 1
            incarnation = self._incarnations
        record = SandboxFSRecord(
            id=sandbox_id,
            task=task,
            state=state,
            cgroup_path=cgroup_path,
            on_report=on_report,
            incarnation=incarnation,
        )
        try:
            self._tier.attach(record)
        except Exception:  # preserve the original attach failure
            try:
                self._ctl_json("destroy", sandbox_id)
            except Exception:  # noqa: BLE001, S110 - best-effort rollback
                pass  # rollback failure must not hide the tier attach failure
            raise
        with self._lock:
            self._records[sandbox_id] = record
        return sandbox_id

    def revoke(self, sandbox: SandboxId) -> None:
        with self._lock:
            record = self._records.get(sandbox)
            if record is None:
                return
        with record.residency_lock:
            with self._lock:
                if self._records.get(sandbox) is not record:
                    return
                record.revoking = True
            self._revoke_locked(record)

    def _revoke_locked(self, record: SandboxFSRecord) -> None:
        # A failed restore must never thaw consumers. SandboxFS's public
        # destroy API can kill a frozen cgroup; require proof of exit before
        # discarding pages. Keep this record on any failure for cleanup retry.
        teardown = RestoreRequest()
        if not record.destroyed:
            try:
                prepared = self._tier.restore(record, teardown)
                if not prepared.ready_for_dispatch:
                    raise SandboxFSError("teardown restore did not reach readiness")
            except Exception as error:
                record.state["teardown_restore_error"] = str(error)
                # Do not thaw, even if destroy's graceful shutdown times out.
            else:
                self._set_frozen(record.cgroup_path, False)
            finally:
                self._tier.finish_restore(record, teardown)
            self._ctl_json("destroy", record.id)
            record.destroyed = True
        self._require_consumers_exited(record)
        self._tier.detach(record, discard=True)
        with self._lock:
            self._records.pop(record.id, None)
        self._residency_fence.forget(record)

    @staticmethod
    def _require_consumers_exited(record: SandboxFSRecord) -> None:
        events = record.cgroup_path / "cgroup.events"
        if events.exists():
            if _read_key_values(events).get("populated") != 0:
                raise SandboxFSError(f"destroyed sandbox {record.id} still has consumers")
        elif record.cgroup_path.exists():
            # Compatibility/test cgroups without cgroup.events still need an
            # explicit empty process list; a missing proof is not success.
            if (record.cgroup_path / "cgroup.procs").read_text().strip():
                raise SandboxFSError(f"destroyed sandbox {record.id} still has consumers")

    @contextmanager
    def _residency_operation(self, sandbox: SandboxId, generation: int | None = None):
        record = self._record(sandbox)
        with record.residency_lock:
            with self._lock:
                if self._records.get(sandbox) is not record or record.revoking:
                    raise SandboxFSError(f"sandbox {sandbox} is being revoked")
            self._residency_fence.admit(record, generation)
            yield record

    def exec(self, sandbox: SandboxId, argv: Sequence[str]) -> dict[str, object]:
        with self._residency_operation(sandbox) as record:
            if not record.dispatch_ready:
                raise SandboxFSError(f"sandbox {sandbox} has not completed confirmed restore")
            return self._exec_locked(sandbox, argv)

    def _exec_locked(self, sandbox: SandboxId, argv: Sequence[str]) -> dict[str, object]:
        if not argv:
            raise ValueError("argv must not be empty")
        command = self._ctl_command("exec-json", sandbox, "--", *argv)
        # Includes any in-flight cache-reclaim chunk in the measured API turn.
        with self._service_foreground(self._record(sandbox).task.repo):
            completed = self._runner(command, self.config.request_timeout_seconds)
        try:
            response = _decode_json(completed.stdout, command)
        except SandboxFSError as error:
            if completed.returncode != 0:
                raise SandboxFSError(_command_error(command, completed)) from error
            raise
        return response

    def report(
        self,
        sandbox: SandboxId,
        stage: Stage,
        context: StageContext | None = None,
    ) -> None:
        record = self._record(sandbox)
        with self._lock:
            record.stage = stage
            callback = record.on_report
        if callback is not None:
            _invoke_stage_report(callback, sandbox, stage, context)

    def assign(
        self, sandbox: SandboxId, task: AgentTask, on_report: StageReport
    ) -> None:
        """Assign a previously prepared, never-leased sandbox to one request."""
        record = self._record(sandbox)
        with self._lock:
            record.task = task
            record.on_report = on_report
            record.stage = Stage.RESPONSE_WAKE

    def set_cpu(self, sandbox: SandboxId, cls: CpuClass) -> None:
        record = self._record(sandbox)
        weight = self.config.cpu_weights[cls]
        _write(record.cgroup_path / "cpu.weight", str(weight))
        with self._lock:
            record.cpu_class = cls

    def demote(self, sandbox: SandboxId, tier: Tier) -> int:
        mode = (
            ReclaimMode.ANON_ONLY if tier is Tier.COMPRESSED else ReclaimMode.BALANCED
        )
        result = self.demote_selective(
            sandbox,
            DemotionRequest(tier=tier, reclaim_mode=mode),
        )
        return result.reclaimed_bytes

    def demote_selective(
        self, sandbox: SandboxId, request: DemotionRequest
    ) -> DemotionResult:
        # Unsupported placement fails before the sandbox is looked up, frozen
        # or touched; a backend is never silently substituted for another.
        self._tier.validate(request.tier)
        with self._residency_operation(sandbox, request.generation) as record:
            return self._demote_locked(record, request)

    def _demote_locked(self, record: SandboxFSRecord, request: DemotionRequest) -> DemotionResult:
        self._tier.validate(request.tier, record)
        # Quiescence is a lifecycle obligation: every backend may rely on the
        # cgroup being frozen for the whole demotion.
        record.dispatch_ready = False
        self._set_frozen(record.cgroup_path, True)
        # An exception can mean partial progress or an uncertain RPC outcome.
        # Remember the target before movement, so teardown must restore it.
        record.last_tier = request.tier
        result = self._tier.demote(record, request)
        if result.requested_bytes > 0:
            with self._lock:
                record.last_reclaimed_bytes = result.reclaimed_bytes
                record.last_tier = request.tier
        return result

    def restore(self, sandbox: SandboxId) -> None:
        self.restore_selective(sandbox, RestoreRequest())

    def restore_selective(
        self, sandbox: SandboxId, request: RestoreRequest
    ) -> RestoreResult:
        """Prepare or commit a wake without moving work past its caller's boundary.

        In opt-in ``thaw-only`` mode a confirmed wake omits the legacy prewarm
        command, but still synchronously waits for ``cgroup.events`` to report
        thawed. ``ready_for_dispatch`` means dispatch is permitted, not that all
        pages are resident or that a command has succeeded. The caller must time
        restore plus the actual :meth:`exec`; demand faults and execution errors
        remain in that normal API call. Speculative preparation never thaws.

        The tier backend's confirmed restore runs *before* thaw. If it raises,
        the sandbox stays frozen and nothing is dispatched.
        """
        with self._residency_operation(sandbox, request.generation) as record:
            return self._restore_locked(record, request)

    def _restore_locked(self, record: SandboxFSRecord, request: RestoreRequest) -> RestoreResult:
        sandbox = record.id
        before = _read_int(record.cgroup_path / "memory.current")
        swap_before = _read_int(record.cgroup_path / "memory.swap.current", missing=0)
        zswap_before = _read_int(record.cgroup_path / "memory.zswap.current", missing=0)
        if not request.speculative:
            record.dispatch_ready = False
        try:
            # Speculative preparation is deliberately non-dispatching and
            # leaves the cgroup frozen; only a confirmed lifecycle transition
            # may thaw or execute work.
            prepared = self._tier.restore(record, request)
            if not request.speculative:
                if not prepared.ready_for_dispatch:
                    raise SandboxFSError(
                        f"tier backend {self._tier.name!r} did not reach its "
                        f"readiness contract for {sandbox}"
                    )
                self._set_frozen(
                    record.cgroup_path,
                    False,
                    require_confirmation=self.config.restore_mode == "thaw-only",
                )
                if self.config.restore_mode == "prewarm":
                    argv = self.config.prewarm_profiles.get(
                        request.profile, self.config.prewarm_argv
                    )
                    if argv:
                        response = self._exec_locked(sandbox, argv)
                        if response.get("exit_code") != 0:
                            raise SandboxFSError(
                                f"prewarm failed in {sandbox}: "
                                f"{response.get('error', response)!r}"
                            )
        finally:
            if not request.speculative:
                self._tier.finish_restore(record, request)
        after = _read_int(record.cgroup_path / "memory.current")
        swap_after = _read_int(record.cgroup_path / "memory.swap.current", missing=0)
        zswap_after = _read_int(record.cgroup_path / "memory.zswap.current", missing=0)
        if not request.speculative:
            with self._lock:
                record.last_tier = Tier.DRAM
                record.dispatch_ready = True
        return RestoreResult(
            resident_delta_bytes=max(0, after - before),
            swap_delta_bytes=max(0, swap_before - swap_after),
            compressed_delta_bytes=max(0, zswap_before - zswap_after),
            prefetched_bytes=prepared.prefetched_bytes,
            advised_bytes=prepared.advised_bytes,
            ready_for_dispatch=not request.speculative,
            profile=request.profile,
            backend=self._tier.name,
            tier_restored_bytes=prepared.tier_restored_bytes,
            lazy_pending_bytes=prepared.lazy_pending_bytes,
            generation=request.generation,
        )

    def stat(self, sandbox: SandboxId) -> SandboxStat:
        record = self._record(sandbox)
        memory = _read_int(record.cgroup_path / "memory.current")
        swap = _read_int(record.cgroup_path / "memory.swap.current", missing=0)
        compressed = _read_int(record.cgroup_path / "memory.zswap.current", missing=0)
        memory_stat = _read_key_values(record.cgroup_path / "memory.stat")
        cpu_stat = _read_key_values(record.cgroup_path / "cpu.stat")
        return SandboxStat(
            stage=record.stage,
            cpu_class=record.cpu_class,
            mem_dram_bytes=memory,
            mem_demoted_bytes=swap,
            mem_swap_bytes=swap,
            mem_compressed_bytes=compressed,
            major_faults=memory_stat.get("pgmajfault", 0),
            page_faults=memory_stat.get("pgfault", 0),
            cpu_usage_usec=cpu_stat.get("usage_usec", 0),
        )

    def host_stat(self) -> HostStat:
        values = _read_meminfo(self.config.meminfo_path)
        cxl_free, cxl_total = 0, 0
        if self._tier_is_cxl:
            cxl_free, cxl_total = self._tier.capacity()
        return HostStat(
            dram_free_bytes=values.get("MemAvailable", values.get("MemFree", 0)),
            dram_total_bytes=values.get("MemTotal", 0),
            cxl_free_bytes=cxl_free,
            cxl_total_bytes=cxl_total,
        )

    def state(self, sandbox: SandboxId) -> dict[str, object]:
        return dict(self._record(sandbox).state)

    def sandbox_ids(self) -> list[SandboxId]:
        with self._lock:
            return sorted(self._records)

    def memory_capabilities(self) -> dict[str, object]:
        try:
            zswap_state = self.config.zswap_enabled_path.read_text().strip()
        except OSError:
            zswap_state = "unavailable"
        return {
            "adapter_selective_reclaim": True,
            "adapter_anonymous_only_reclaim": True,
            "adapter_file_only_reclaim": True,
            "sandbox_cgroup_controls_checked": False,
            "zswap_state": zswap_state,
            "zswap_compression_allowed": self.config.allow_zswap_compression,
            "libc_process_madvise_available": _process_madvise_available(),
            "process_madvise_allowed": self.config.allow_process_madvise_restore,
            "process_madvise_advice": self.config.speculative_madvise_advice,
            "process_madvise_passes": self.config.speculative_madvise_passes,
            "process_madvise_permission_checked": False,
            "speculative_dispatch_allowed": False,
            "confirmed_restore_mode": self.config.restore_mode,
            "memory_tier_backend": self._tier.capabilities().as_json(),
        }

    @property
    def memory_tier(self) -> MemoryTierBackend:
        return self._tier

    def tier_accounting(self, sandbox: SandboxId) -> TierAccounting:
        return self._tier.accounting(self._record(sandbox))

    def _record(self, sandbox: SandboxId) -> SandboxFSRecord:
        with self._lock:
            try:
                return self._records[sandbox]
            except KeyError as error:
                raise SandboxFSError(f"unknown sandbox {sandbox!r}") from error

    def _ctl_command(self, *args: str) -> list[str]:
        return [
            self.config.ctl_path,
            "--socket",
            self.config.socket_path,
            "--timeout",
            f"{self.config.request_timeout_seconds}s",
            *args,
        ]

    def _ctl_json(self, *args: str) -> dict[str, object]:
        command = self._ctl_command(*args)
        completed = self._runner(command, self.config.request_timeout_seconds)
        if completed.returncode != 0:
            raise SandboxFSError(_command_error(command, completed))
        return _decode_json(completed.stdout, command)

    def _cgroup_for_pid(self, pid: int) -> Path:
        return _resolve_cgroup_for_pid(self.config, pid)

    def _set_frozen(
        self, cgroup: Path, frozen: bool, *, require_confirmation: bool = False
    ) -> None:
        desired = "1" if frozen else "0"
        events_path = cgroup / "cgroup.events"
        if require_confirmation and not events_path.exists():
            raise SandboxFSError(f"confirmed thaw requires cgroup events: {events_path}")
        _write(cgroup / "cgroup.freeze", desired)
        if not events_path.exists():
            if require_confirmation:
                raise SandboxFSError(f"cgroup events disappeared during thaw: {events_path}")
            return
        deadline = time.monotonic() + self.config.freeze_timeout_seconds
        while time.monotonic() < deadline:
            events = _read_key_values(events_path)
            if events.get("frozen", int(not frozen)) == int(frozen):
                return
            time.sleep(0.005)
        raise SandboxFSError(
            f"cgroup {cgroup} did not become {'frozen' if frozen else 'thawed'}"
        )


def _resolve_cgroup_for_pid(config: SandboxFSConfig, pid: int) -> Path:
    cgroup_file = config.proc_root / str(pid) / "cgroup"
    try:
        lines = cgroup_file.read_text().splitlines()
    except OSError as error:
        raise SandboxFSError(f"read sandbox cgroup for PID {pid}: {error}") from error
    for line in lines:
        if line.startswith("0::"):
            relative = line.removeprefix("0::").lstrip("/")
            path = (config.cgroup_root / relative).resolve()
            root = config.cgroup_root.resolve()
            if path != root and root in path.parents:
                return path
            break
    raise SandboxFSError(f"PID {pid} has no safe unified cgroup v2 path")


def _read_swaps(path: Path) -> list[dict[str, object]]:
    """Configured swap backing as evidence; never modified by this adapter."""
    try:
        lines = path.read_text().splitlines()[1:]
    except OSError:
        return []
    swaps: list[dict[str, object]] = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 5:
            swaps.append(
                {
                    "name": fields[0],
                    "type": fields[1],
                    "size_kib": fields[2],
                    "priority": fields[4],
                }
            )
    return swaps


def _required_int(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise SandboxFSError(f"SandboxFS response lacks integer {key!r}: {value!r}")
    return item


def _decode_json(payload: str, command: Sequence[str]) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise SandboxFSError(
            f"invalid JSON from {command[0]}: {error}: {payload!r}"
        ) from error
    if not isinstance(value, dict):
        raise SandboxFSError(f"unexpected JSON from {command[0]}: {value!r}")
    return value


def _command_error(
    command: Sequence[str], completed: subprocess.CompletedProcess[str]
) -> str:
    detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
    return f"command failed ({completed.returncode}): {command!r}: {detail}"


def _write(path: Path, value: str) -> None:
    try:
        path.write_text(value)
    except OSError as error:
        raise SandboxFSError(f"write {path}: {error}") from error


def _path_within_roots(path: Path, roots: Sequence[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        resolved_root = root.resolve()
        if resolved == resolved_root or resolved_root in resolved.parents:
            return True
    return False


class _IOVec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


def _anonymous_readable_ranges(
    maps_path: Path, maximum_bytes: int
) -> list[tuple[int, int]]:
    try:
        lines = maps_path.read_text().splitlines()
    except OSError as error:
        raise SandboxFSError(f"read process mappings {maps_path}: {error}") from error
    remaining = maximum_bytes
    ranges: list[tuple[int, int]] = []
    for line in lines:
        if remaining <= 0:
            break
        fields = line.split(maxsplit=5)
        if len(fields) < 5 or "r" not in fields[1]:
            continue
        mapping = fields[5] if len(fields) == 6 else ""
        if mapping and not mapping.startswith(("[heap]", "[stack", "[anon")):
            continue
        try:
            start_text, end_text = fields[0].split("-", 1)
            start = int(start_text, 16)
            end = int(end_text, 16)
        except ValueError as error:
            raise SandboxFSError(f"invalid mapping in {maps_path}: {line!r}") from error
        length = min(max(0, end - start), remaining)
        if length:
            ranges.append((start, length))
            remaining -= length
    return ranges


def _process_madvise_available() -> bool:
    if not hasattr(os, "pidfd_open"):
        return False
    try:
        return hasattr(ctypes.CDLL(None), "process_madvise")
    except OSError:
        return False


def _open_pidfd(pid: int) -> int:
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None:
        raise SandboxFSError("host Python lacks pidfd_open")
    try:
        return pidfd_open(pid, 0)
    except OSError as error:
        raise SandboxFSError(f"pidfd_open({pid}) failed: {error}") from error


def _process_madvise_willneed(
    pid: int, pidfd: int, ranges: Sequence[tuple[int, int]]
) -> int:
    return _process_madvise_ranges(pid, pidfd, ranges, _MADV_WILLNEED, "MADV_WILLNEED")


def _process_madvise_populate_read(
    pid: int, pidfd: int, ranges: Sequence[tuple[int, int]]
) -> int:
    return _process_madvise_ranges(
        pid, pidfd, ranges, _MADV_POPULATE_READ, "MADV_POPULATE_READ"
    )


def _process_madvise_ranges(
    pid: int,
    pidfd: int,
    ranges: Sequence[tuple[int, int]],
    advice: int,
    advice_name: str,
) -> int:
    if not _process_madvise_available():
        raise SandboxFSError("host libc/kernel lacks process_madvise or pidfd_open")
    libc = ctypes.CDLL(None, use_errno=True)
    process_madvise = libc.process_madvise
    process_madvise.argtypes = (
        ctypes.c_int,
        ctypes.POINTER(_IOVec),
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_uint,
    )
    process_madvise.restype = ctypes.c_ssize_t
    advised = 0
    for offset in range(0, len(ranges), 64):
        batch = ranges[offset : offset + 64]
        vectors = (_IOVec * len(batch))(
            *(_IOVec(ctypes.c_void_p(start), length) for start, length in batch)
        )
        result = process_madvise(pidfd, vectors, len(batch), advice, 0)
        if result < 0:
            number = ctypes.get_errno()
            error = OSError(number, os.strerror(number))
            raise SandboxFSError(
                f"process_madvise({advice_name}) for PID {pid}: {error}"
            ) from error
        advised += int(result)
        if result < sum(length for _, length in batch):
            break
    return advised


def _invoke_stage_report(
    callback: StageReport,
    sandbox: SandboxId,
    stage: Stage,
    context: StageContext | None,
) -> None:
    """Invoke a context-aware callback while retaining plugin compatibility."""

    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        callback(sandbox, stage, context)
        return
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
    ]
    if (
        any(parameter.kind is parameter.VAR_POSITIONAL for parameter in parameters)
        or len(positional) >= 3
    ):
        callback(sandbox, stage, context)
    else:
        callback(sandbox, stage)  # type: ignore[call-arg]


def _caused_by_errno(error: BaseException, number: int) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, OSError) and current.errno == number:
            return True
        current = current.__cause__
    return False


def _read_int(path: Path, *, missing: int | None = None) -> int:
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise
    except (OSError, ValueError) as error:
        raise SandboxFSError(f"read integer {path}: {error}") from error


def _read_key_values(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text().splitlines()
    except OSError as error:
        raise SandboxFSError(f"read {path}: {error}") from error
    values: dict[str, int] = {}
    for line in lines:
        fields = line.split()
        if len(fields) == 2:
            try:
                values[fields[0]] = int(fields[1])
            except ValueError:
                continue
    return values


def _read_meminfo(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text().splitlines()
    except OSError as error:
        raise SandboxFSError(f"read {path}: {error}") from error
    values: dict[str, int] = {}
    for line in lines:
        key, separator, remainder = line.partition(":")
        if not separator:
            continue
        fields = remainder.split()
        if not fields:
            continue
        multiplier = 1024 if len(fields) > 1 and fields[1] == "kB" else 1
        try:
            values[key] = int(fields[0]) * multiplier
        except ValueError:
            continue
    return values
