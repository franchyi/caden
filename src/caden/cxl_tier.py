"""DRAM-CXL tier backend: native cold store plus a real page-out/page-in path.

Policy-facing semantics match :class:`caden.sandboxfs_backend.SSDTierBackend`;
the mechanism does not. ``crate_pagerd`` (``native/cxl_coldstore/pagerd.c``)
owns the store and the Device-DAX mapping, moves payload bytes natively, and
serves demand faults. This module only carries control metadata: it discovers
cooperative regions in ``/proc``, fences requests, and turns the daemon's
receipts into Caden results. No page payload enters this process.

Paging contract (full text: ``native/cxl_coldstore/PAGER.md``):

* Eligible: ``MAP_SHARED`` mappings of a memfd named ``crate-tier.*`` that a
  process *inside the sandbox cgroup* created (``coop.h``). Everything else -
  private anonymous memory of unmodified tools, tmpfs, file cache - is not
  eligible and is reported as such. It is never relabelled as CXL placement.
* Quiescence: the execution layer freezes the cgroup first; the daemon
  re-checks ``cgroup.events`` before it copies anything.
* Source release: resident pages are copied into the store, then released with
  a memfd hole punch. ``released_bytes`` is measured, not inferred.
* Restore: eager ``UFFDIO_COPY`` before thaw, or armed demand paging when the
  region's userfaultfd also resolves kernel-mode faults. A failed restore
  raises, so the execution layer never thaws or dispatches.
* Anonymous/tmpfs pages are kept out of SSD swap for the sandbox's lifetime by
  ``memory.swap.max=0`` on its own cgroup; the optional companion file-cache
  reclaim therefore cannot become an unlabelled SSD treatment. A non-zero swap
  delta is recorded as a contract violation.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .memory_tier import (
    GenerationFence,
    MemoryTierError,
    StaleGenerationError,
    TierAccounting,
    TierCapabilities,
    TierCapacityError,
    TierSandbox,
    UnsupportedTierError,
)
from .types import (
    DemotionRequest,
    DemotionResult,
    ReclaimMode,
    RestoreRequest,
    RestoreResult,
    SandboxId,
    Tier,
)

_MEMFD_PATTERN = re.compile(r"^/memfd:crate-tier\.u(-?\d+)\.k([01])(?: \(deleted\))?$")
_PAGE = 4096


class CXLTierError(MemoryTierError):
    pass


class PagerError(CXLTierError):
    def __init__(self, number: int, message: str) -> None:
        super().__init__(f"pager error {number}: {message}")
        self.errno = number


@dataclass(frozen=True)
class CoopRegion:
    pid: int
    memfd: int
    uffd: int
    address: int
    length: int
    file_offset: int
    kernel_faults: bool


@dataclass(frozen=True)
class CXLTierConfig:
    socket_path: str
    proc_root: Path = Path("/proc")
    # "eager": every cold page is resident before thaw. "lazy": arm demand
    # paging and let faults land inside the timed tool call.
    restore_mode: str = "eager"
    # A user-mode-only userfaultfd cannot resolve a fault raised inside a system
    # call. Lazy restore over such a region needs the owner's explicit promise.
    allow_user_mode_only_lazy: bool = False
    lazy_fallback_to_eager: bool = True
    # Without a userfaultfd a cold page is an ordinary memfd hole: if the store
    # owner dies, the consumer reads zeros instead of stopping. Such regions
    # are therefore ineligible unless a test environment opts in explicitly.
    allow_regions_without_userfaultfd: bool = False
    per_sandbox_max_bytes: int = 0
    # Companion kernel reclaim of clean/dirty *file* cache. It stores nothing
    # and is never counted as tier placement.
    file_cache_reclaim: bool = True
    # Keeps the sandbox's anonymous/tmpfs pages out of SSD swap entirely.
    forbid_swap: bool = True
    reclaim_settle_seconds: float = 0.05
    request_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.restore_mode not in {"eager", "lazy"}:
            raise ValueError("restore_mode must be eager or lazy")
        if self.per_sandbox_max_bytes < 0:
            raise ValueError("per_sandbox_max_bytes must not be negative")
        if self.file_cache_reclaim and not self.forbid_swap:
            raise ValueError(
                "file_cache_reclaim without forbid_swap could silently swap "
                "anonymous pages to SSD under a CXL label"
            )


class PagerClient:
    """Line protocol client over a small pool of reusable connections.

    Calls from any thread borrow an idle connection, so concurrent movement of
    different sandboxes is not serialized here, while short-lived campaign
    threads cannot exhaust the daemon's bounded connection table.
    """

    def __init__(self, socket_path: str, timeout: float) -> None:
        self._path = socket_path
        self._timeout = timeout
        self._lock = threading.Lock()
        self._idle: list[socket.socket] = []
        self._closed = False

    def call(self, *tokens: object) -> dict[str, str]:
        line = " ".join(str(token) for token in tokens)
        if "\n" in line:
            raise CXLTierError("pager command must be a single line")
        connection: socket.socket | None = None
        try:
            connection = self._borrow()
            connection.sendall(line.encode() + b"\n")
            reply = self._readline(connection)
        except OSError as error:
            if connection is not None:
                connection.close()
            raise CXLTierError(
                f"pager connection failed during {tokens[0]}: {error}"
            ) from error
        self._give_back(connection)
        fields = reply.split()
        if not fields:
            raise CXLTierError(f"empty pager reply to {tokens[0]}")
        if fields[0] == "ERR":
            number = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else errno.EIO
            raise PagerError(number, " ".join(fields[2:]))
        if fields[0] != "OK":
            raise CXLTierError(f"malformed pager reply: {reply!r}")
        return dict(item.split("=", 1) for item in fields[1:] if "=" in item)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sockets, self._idle = self._idle, []
        for connection in sockets:
            try:
                connection.close()
            except OSError:
                pass

    def _borrow(self) -> socket.socket:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout)
        try:
            connection.connect(self._path)
        except OSError:
            connection.close()
            raise
        return connection

    def _give_back(self, connection: socket.socket) -> None:
        with self._lock:
            if not self._closed and len(self._idle) < 8:
                self._idle.append(connection)
                return
        connection.close()

    @staticmethod
    def _readline(connection: socket.socket) -> str:
        # One request is outstanding per borrowed connection, so a reply never
        # carries bytes that belong to another call.
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = connection.recv(8192)
            if not chunk:
                raise OSError(errno.ECONNRESET, "pager closed the connection")
            buffer += chunk
        return buffer.decode().rstrip("\n")


def discover_regions(proc_root: Path, pids: Sequence[int]) -> list[CoopRegion]:
    """Find cooperative regions by the memfd naming convention. Metadata only."""
    regions: list[CoopRegion] = []
    for pid in pids:
        try:
            lines = (proc_root / str(pid) / "maps").read_text().splitlines()
        except OSError:
            continue  # exited between cgroup.procs and /proc
        spans: list[list[int]] = []  # [start, end, offset, inode, uffd, kernel]
        for line in lines:
            fields = line.split(maxsplit=5)
            if len(fields) < 6:
                continue
            match = _MEMFD_PATTERN.match(fields[5].strip())
            if match is None or "s" not in fields[1]:
                continue
            start_text, end_text = fields[0].split("-", 1)
            start, end = int(start_text, 16), int(end_text, 16)
            offset, inode = int(fields[2], 16), int(fields[4])
            previous = spans[-1] if spans else None
            if (
                previous is not None
                and previous[1] == start
                and previous[3] == inode
                and previous[2] + (previous[1] - previous[0]) == offset
            ):
                previous[1] = end
            else:
                spans.append(
                    [start, end, offset, inode, int(match.group(1)), int(match.group(2))]
                )
        if not spans:
            continue
        descriptors = _memfd_descriptors(proc_root, pid)
        for start, end, offset, inode, uffd, kernel in spans:
            memfd = descriptors.get(inode)
            if memfd is None or (end - start) % _PAGE or start % _PAGE:
                continue
            regions.append(
                CoopRegion(pid, memfd, uffd, start, end - start, offset, bool(kernel))
            )
    return regions


def _memfd_descriptors(proc_root: Path, pid: int) -> dict[int, int]:
    found: dict[int, int] = {}
    directory = proc_root / str(pid) / "fd"
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        path = directory / name
        try:
            if not os.readlink(path).startswith("/memfd:crate-tier."):
                continue
            found.setdefault(os.stat(path).st_ino, int(name))
        except (OSError, ValueError):
            continue
    return found


@dataclass
class _Attached:
    lock: threading.RLock = field(default_factory=threading.RLock)
    registered: set[tuple[int, int, int]] = field(default_factory=set)
    swap_max_original: str | None = None
    cold_bytes: int = 0
    movement_uncertain: bool = False
    # Wire sequence is independent of the caller's policy generation. Even
    # compatibility calls (generation=None) fence delayed, timed-out RPCs.
    wire_generation: int = 0
    lazy_refusals: int = 0
    swap_leak_bytes: int = 0
    file_reclaimed_bytes: int = 0
    ineligible_resident_bytes: int = 0
    skipped_regions_without_uffd: int = 0


class CXLTierBackend:
    """``MemoryTierBackend`` over ``crate_pagerd``."""

    name = "cxl"

    def __init__(self, config: CXLTierConfig, *, client: PagerClient | None = None) -> None:
        self.config = config
        self._client = client or PagerClient(
            config.socket_path, config.request_timeout_seconds
        )
        self._fence = GenerationFence()
        self._lock = threading.RLock()
        self._attached: dict[tuple[SandboxId, int], _Attached] = {}
        self._hello = self._client.call("HELLO")

    # -- description ---------------------------------------------------------
    def capabilities(self) -> TierCapabilities:
        hello = self._hello
        real = hello.get("medium") == "device-dax"
        return TierCapabilities(
            backend=self.name,
            medium=hello.get("medium", "unknown"),
            supported_tiers=frozenset({Tier.CXL}),
            eligible_memory=(
                "MAP_SHARED mappings of a crate-tier memfd created by a process "
                "in the sandbox cgroup (cooperative runtime, native/cxl_coldstore/coop.h)",
            ),
            ineligible_memory=(
                "private anonymous memory of unmodified tools",
                "tmpfs (/tmp, /run) and other shmem not created through coop.h",
                "file cache (optionally dropped by companion kernel reclaim; never stored)",
            ),
            placement_guarantee=(
                "stored bytes reside in the daemon's mapped store range "
                f"{hello.get('path')}@{hello.get('offset')}+{hello.get('capacity')}"
            ),
            source_release_contract=(
                "memfd hole punch after a complete store write; released bytes "
                "measured from the memfd block count while the cgroup is frozen"
            ),
            eager_restore=True,
            lazy_restore=True,
            speculative_preparation=True,
            required_permissions=(
                "root (pidfd_getfd across the sandbox user namespace)",
                "write access to the sandbox cgroup v2 files",
                "read/write access to the reserved store range",
            ),
            evidence_class=(
                "real-dax-cooperative-paging" if real else "file-emulation-cooperative-paging"
            ),
            codec=hello.get("codec", "none"),
            details={
                "pager": dict(hello),
                "restore_mode": self.config.restore_mode,
                "allow_user_mode_only_lazy": self.config.allow_user_mode_only_lazy,
                "lazy_fallback_to_eager": self.config.lazy_fallback_to_eager,
                "file_cache_reclaim": self.config.file_cache_reclaim,
                "forbid_swap": self.config.forbid_swap,
                "allow_regions_without_userfaultfd": (
                    self.config.allow_regions_without_userfaultfd
                ),
                "transparent_for_unmodified_tools": False,
            },
        )

    def validate(self, tier: Tier, sandbox: TierSandbox | None = None) -> None:
        if tier is not Tier.CXL:
            raise UnsupportedTierError(
                f"CXL tier backend places pages in the CXL store only, not {tier.value}; "
                "select the SSD backend explicitly for swap/zswap reclaim"
            )
        if sandbox is not None and self._state(sandbox, required=False) is None:
            raise CXLTierError(f"sandbox {sandbox.id!r} is not attached to the CXL tier")

    def capacity(self) -> tuple[int, int]:
        stats = self._client.call("STATS")
        return int(stats.get("logical_free", 0)), int(stats.get("logical", 0))

    def store_stats(self) -> dict[str, str]:
        return self._client.call("STATS")

    # -- lifecycle -----------------------------------------------------------
    def attach(self, sandbox: TierSandbox) -> None:
        state = _Attached()
        if self.config.forbid_swap:
            swap_max = sandbox.cgroup_path / "memory.swap.max"
            try:
                state.swap_max_original = swap_max.read_text().strip()
                swap_max.write_text("0")
            except OSError as error:
                raise CXLTierError(
                    f"cannot keep {sandbox.id!r} out of SSD swap ({swap_max}): {error}"
                ) from error
        try:
            self._client.call(
                "ATTACH",
                sandbox.id,
                sandbox.incarnation,
                sandbox.cgroup_path,
                self.config.per_sandbox_max_bytes,
            )
        except Exception:
            self._restore_swap_max(sandbox, state)
            raise
        with self._lock:
            self._attached[(sandbox.id, sandbox.incarnation)] = state

    def detach(self, sandbox: TierSandbox, *, discard: bool = True) -> TierAccounting:
        state = self._state(sandbox, required=False)
        if state is None:
            return TierAccounting()
        with state.lock:  # synchronizes with in-flight movement
            try:
                accounting = self._accounting_locked(sandbox, state)
            except PagerError as error:
                if error.errno != errno.ENOENT:
                    raise
                # A previous DETACH may have succeeded but lost its reply.
                accounting = TierAccounting(details={"already_detached": True})
            if not discard and not accounting.details.get("already_detached"):
                reply = self._call_restore(sandbox, self._next_generation(state), "eager")
                self._restore_receipt(reply, eager=True)
            try:
                reply = self._client.call("DETACH", sandbox.id, sandbox.incarnation)
            except PagerError as error:
                if error.errno != errno.ENOENT:
                    raise
                reply = {}
            accounting.details["discarded_bytes"] = int(reply.get("discarded", 0))
            self._restore_swap_max(sandbox, state)
            with self._lock:
                self._attached.pop((sandbox.id, sandbox.incarnation), None)
            self._fence.forget(sandbox)
        return accounting

    def close(self) -> None:
        with self._lock:
            leaked = sorted(identity for identity, _ in self._attached)
        if leaked:
            raise CXLTierError(
                f"refusing to close the CXL tier while sandboxes are attached: {leaked}"
            )
        self._client.close()

    # -- movement ------------------------------------------------------------
    def demote(self, sandbox: TierSandbox, request: DemotionRequest) -> DemotionResult:
        self.validate(request.tier)
        state = self._state(sandbox)
        with state.lock:
            self._admit(sandbox, request.generation)
            cgroup = sandbox.cgroup_path
            before = _read_int(cgroup / "memory.current")
            swap_before = _read_int(cgroup / "memory.swap.current", missing=0)
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
                    tier=Tier.CXL,
                    reclaim_mode=request.reclaim_mode,
                    backend=self.name,
                    generation=request.generation,
                )
            self._register_regions(sandbox, state)
            eligible = stored = released = 0
            partial = False
            if state.registered and request.reclaim_mode is not ReclaimMode.FILE_ONLY:
                state.movement_uncertain = True
                try:
                    reply = self._client.call(
                        "DEMOTE",
                        sandbox.id,
                        sandbox.incarnation,
                        self._next_generation(state, request.generation),
                        requested,
                    )
                except PagerError as error:
                    raise _translate(error) from error
                eligible = int(reply.get("eligible", 0))
                stored = int(reply.get("stored", 0))
                released = int(reply.get("released", 0))
                partial = reply.get("partial") == "1"
                state.cold_bytes = int(reply.get("cold", 0))
                state.movement_uncertain = False
            file_target = requested - stored
            if (
                self.config.file_cache_reclaim
                and file_target > 0
                and request.reclaim_mode is not ReclaimMode.ANON_ONLY
            ):
                partial = self._reclaim_file_cache(cgroup, file_target) or partial
            if self.config.reclaim_settle_seconds > 0:
                time.sleep(self.config.reclaim_settle_seconds)
            after = _read_int(cgroup / "memory.current")
            swap_after = _read_int(cgroup / "memory.swap.current", missing=0)
            swap_delta = max(0, swap_after - swap_before)
            reclaimed = max(0, before - after)
            file_reclaimed = max(0, reclaimed - released)
            state.file_reclaimed_bytes += file_reclaimed
            state.swap_leak_bytes += swap_delta
            state.ineligible_resident_bytes = max(0, after)
            return DemotionResult(
                requested_bytes=requested,
                reclaimed_bytes=reclaimed,
                before_bytes=before,
                after_bytes=after,
                swap_delta_bytes=swap_delta,
                tier=Tier.CXL,
                reclaim_mode=request.reclaim_mode,
                backend=self.name,
                eligible_bytes=eligible,
                stored_bytes=stored,
                released_bytes=released,
                file_reclaimed_bytes=file_reclaimed,
                partial=partial,
                generation=request.generation,
            )

    def restore(self, sandbox: TierSandbox, request: RestoreRequest) -> RestoreResult:
        state = self._state(sandbox)
        with state.lock:  # a wake waits here for an in-flight demotion
            self._admit(sandbox, request.generation)
            restored = pending = 0
            if state.registered:
                # Local cold_bytes is only a receipt cache, never a readiness
                # authority. A timeout can occur after the daemon released pages.
                generation = self._next_generation(state, request.generation)
                state.movement_uncertain = True
                # Preparation never relies on faults: it is eager by definition.
                lazy = self.config.restore_mode == "lazy" and not request.speculative
                mode = "eager"
                if lazy:
                    mode = "lazy-user" if self.config.allow_user_mode_only_lazy else "lazy"
                try:
                    reply = self._call_restore(sandbox, generation, mode)
                except PagerError as error:
                    if (
                        lazy
                        and error.errno == errno.EOPNOTSUPP
                        and self.config.lazy_fallback_to_eager
                    ):
                        state.lazy_refusals += 1
                        mode = "eager"
                        try:
                            reply = self._call_restore(sandbox, generation, mode)
                        except PagerError as inner:
                            raise _translate(inner) from inner
                    else:
                        raise _translate(error) from error
                restored, pending = self._restore_receipt(reply, eager=mode == "eager")
                state.cold_bytes = pending
                state.movement_uncertain = False
            return RestoreResult(
                ready_for_dispatch=not request.speculative,
                profile=request.profile,
                backend=self.name,
                tier_restored_bytes=restored,
                lazy_pending_bytes=pending,
                generation=request.generation,
            )

    def finish_restore(self, sandbox: TierSandbox, request: RestoreRequest) -> None:
        return None

    def accounting(self, sandbox: TierSandbox) -> TierAccounting:
        state = self._state(sandbox)
        with state.lock:
            return self._accounting_locked(sandbox, state)

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _next_generation(state: _Attached, requested: int | None = None) -> int:
        state.wire_generation = max(state.wire_generation + 1, requested or 0)
        return state.wire_generation

    @staticmethod
    def _restore_receipt(reply: dict[str, str], *, eager: bool) -> tuple[int, int]:
        try:
            restored, pending = int(reply["restored"]), int(reply["lazy_pending"])
        except (KeyError, ValueError) as error:
            raise CXLTierError("incomplete or malformed RESTORE receipt") from error
        if restored < 0 or pending < 0 or (eager and pending):
            raise CXLTierError("RESTORE receipt does not satisfy readiness")
        return restored, pending

    def _call_restore(self, sandbox: TierSandbox, generation: int, mode: str) -> dict[str, str]:
        return self._client.call("RESTORE", sandbox.id, sandbox.incarnation, generation, mode)

    def _admit(self, sandbox: TierSandbox, generation: int | None) -> None:
        self._fence.admit(sandbox, generation)

    def _state(self, sandbox: TierSandbox, *, required: bool = True) -> _Attached | None:
        with self._lock:
            state = self._attached.get((sandbox.id, sandbox.incarnation))
        if state is None and required:
            raise CXLTierError(f"sandbox {sandbox.id!r} is not attached to the CXL tier")
        return state

    def _register_regions(self, sandbox: TierSandbox, state: _Attached) -> None:
        try:
            pids = [
                int(line)
                for line in (sandbox.cgroup_path / "cgroup.procs").read_text().split()
            ]
        except (OSError, ValueError) as error:
            raise CXLTierError(f"read sandbox PIDs for {sandbox.id!r}: {error}") from error
        for region in discover_regions(self.config.proc_root, pids):
            key = (region.pid, region.address, region.length)
            if key in state.registered:
                continue
            if region.uffd < 0 and not self.config.allow_regions_without_userfaultfd:
                state.skipped_regions_without_uffd += 1
                continue
            try:
                self._client.call(
                    "REGION",
                    sandbox.id,
                    sandbox.incarnation,
                    region.pid,
                    region.memfd,
                    region.uffd,
                    region.address,
                    region.length,
                    region.file_offset,
                    int(region.kernel_faults),
                )
            except PagerError as error:
                if error.errno in {errno.ESRCH, errno.ENOENT}:
                    continue  # the owner exited before registration
                raise _translate(error) from error
            state.registered.add(key)

    def _reclaim_file_cache(self, cgroup: Path, target: int) -> bool:
        try:
            (cgroup / "memory.reclaim").write_text(str(target))
        except OSError as error:
            if error.errno == errno.EAGAIN:
                return True  # kernel made partial progress; the delta is measured
            raise CXLTierError(f"write {cgroup / 'memory.reclaim'}: {error}") from error
        return False

    def _restore_swap_max(self, sandbox: TierSandbox, state: _Attached) -> None:
        if state.swap_max_original is None:
            return
        try:
            (sandbox.cgroup_path / "memory.swap.max").write_text(state.swap_max_original)
        except OSError:
            pass  # the cgroup is usually gone once the sandbox is destroyed
        state.swap_max_original = None

    def _accounting_locked(self, sandbox: TierSandbox, state: _Attached) -> TierAccounting:
        stat = self._client.call("STAT", sandbox.id, sandbox.incarnation)
        number = lambda key: int(stat.get(key, 0))  # noqa: E731
        return TierAccounting(
            requested_bytes=number("requested"),
            stored_bytes=number("stored"),
            released_bytes=number("released"),
            restored_bytes=number("restored_eager") + number("restored_fault"),
            resident_in_tier_bytes=number("cold"),
            demand_faults=number("faults"),
            demotions=number("demotions"),
            restores=number("restores"),
            stale_rejections=number("stale"),
            errors=number("errors"),
            metadata_bytes=number("metadata"),
            details={
                "eligible_bytes": number("eligible"),
                "restored_eager_bytes": number("restored_eager"),
                "restored_fault_bytes": number("restored_fault"),
                "regions": number("regions"),
                "reserved_bytes": number("reserved"),
                "max_bytes": number("max_bytes"),
                "poisoned": stat.get("poisoned") == "1",
                "copy_out_ns": number("copy_out_ns"),
                "punch_ns": number("punch_ns"),
                "copy_in_ns": number("copy_in_ns"),
                "fault_ns": number("fault_ns"),
                "lazy_refusals": state.lazy_refusals,
                "movement_uncertain": state.movement_uncertain,
                "file_reclaimed_bytes": state.file_reclaimed_bytes,
                "swap_leak_bytes": state.swap_leak_bytes,
                "ineligible_resident_bytes": state.ineligible_resident_bytes,
                "skipped_regions_without_uffd": state.skipped_regions_without_uffd,
            },
        )


def _translate(error: PagerError) -> MemoryTierError:
    if error.errno == errno.ESTALE:
        return StaleGenerationError(str(error))
    if error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return TierCapacityError(str(error))
    return error


def _read_int(path: Path, *, missing: int | None = None) -> int:
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise
    except (OSError, ValueError) as error:
        raise CXLTierError(f"read integer {path}: {error}") from error


class PagerDaemon:
    """Owns one ``crate_pagerd`` process for a campaign or a test.

    The daemon is the single owner of the store; stopping it while a sandbox
    still depends on cold pages is refused by the daemon itself.
    """

    def __init__(
        self,
        binary: Path,
        socket_path: Path,
        *,
        store_path: Path,
        offset: int,
        capacity: int,
        logical_bytes: int,
        reserved_dax: bool = False,
        codec: str = "none",
        per_sandbox_max_bytes: int = 0,
        fault_around_pages: int = 16,
        lock_buffers: bool = False,
        allow_unfenced_quiescence: bool = False,
        cgroup_root: Path | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.argv = [
            str(binary),
            "--socket",
            str(socket_path),
            "--reserved-dax" if reserved_dax else "--emulate-file",
            str(store_path),
            "--offset",
            str(offset),
            "--capacity",
            str(capacity),
            "--logical-bytes",
            str(logical_bytes),
            "--codec",
            codec,
            "--fault-around-pages",
            str(fault_around_pages),
        ]
        if per_sandbox_max_bytes:
            self.argv += ["--per-sandbox-max-bytes", str(per_sandbox_max_bytes)]
        if lock_buffers:
            self.argv.append("--lock-buffers")
        if allow_unfenced_quiescence:
            self.argv.append("--allow-unfenced-quiescence")
        if cgroup_root is not None:
            self.argv += ["--cgroup-root", str(cgroup_root)]
        self._log_path = log_path
        self._log = None
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, timeout: float = 10.0) -> None:
        if self.socket_path.exists():
            raise CXLTierError(f"pager socket already exists: {self.socket_path}")
        self._log = open(self._log_path, "ab") if self._log_path else subprocess.DEVNULL
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            self.argv, stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise CXLTierError(
                    f"crate_pagerd exited with {self.process.returncode} during startup"
                )
            if self.socket_path.exists():
                return
            time.sleep(0.02)
        self.process.kill()
        raise CXLTierError("crate_pagerd did not create its control socket")

    def stop(self, *, force: bool = False, timeout: float = 30.0) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            client = PagerClient(str(self.socket_path), timeout)
            try:
                client.call("SHUTDOWN", *(("force",) if force else ()))
            finally:
                client.close()
            self.process.wait(timeout=timeout)
        if self._log not in (None, subprocess.DEVNULL):
            self._log.close()
        self.process = None
