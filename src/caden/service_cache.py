"""Opt-in, bounded reclamation of explicitly delegated daemon cache cgroups.

This is Caden residency policy, not a filesystem/lifecycle implementation. It
never walks the host cgroup tree or reclaims a service parent/sandbox child.
An operator supplies exact SandboxFS daemon leaves. Foreground API operations
have priority over another chunk; one already-running kernel write is charged
to the foreground operation that waits for it. Kernel reclaim is best effort:
the configured reserve is a target, not a hard resident-memory guarantee.
"""
from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class _Domain:
    path: Path
    foreground_waiters: int = 0
    foreground_active: int = 0
    background_active: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)
    fd: int = -1
    swap_max: str | None = None
    file_only: bool | None = None


class ServiceCacheController:
    def __init__(self, roots: dict[str, Path], *, reserve_bytes: int = 128 << 20,
                 chunk_bytes: int = 16 << 20, interval_seconds: float = 0.1,
                 max_bytes_per_interval: int | None = None,
                 idle: Callable[[str], bool] = lambda base: False):
        if not roots or reserve_bytes <= 0 or chunk_bytes <= 0 or interval_seconds <= 0:
            raise ValueError("explicit cache roots and positive bounds are required")
        paths = list(roots.values())
        if len(set(paths)) != len(paths):
            raise ValueError("each cache domain must have one unique daemon leaf")
        for path in paths:
            if (not path.is_absolute() or path.name != "daemon"
                    or not path.parent.name.endswith(".service")
                    or path.resolve() != path or ".." in path.parts):
                raise ValueError(f"not an exact nonsymlink daemon leaf: {path}")
        self.domains = {base: _Domain(path) for base, path in roots.items()}
        self.reserve_bytes, self.chunk_bytes = reserve_bytes, chunk_bytes
        self.interval_seconds, self.idle = interval_seconds, idle
        self.max_bytes_per_interval = max_bytes_per_interval or chunk_bytes
        if self.max_bytes_per_interval < chunk_bytes:
            raise ValueError("interval byte budget must cover one bounded chunk")
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _read(domain: _Domain, name: str) -> str:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=domain.fd)
        with os.fdopen(fd) as handle:
            return handle.read().strip()

    @staticmethod
    def _write(domain: _Domain, name: str, value: str) -> None:
        fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=domain.fd)
        with os.fdopen(fd, "w") as handle:
            handle.write(value)

    def _observe(self, domain: _Domain) -> dict:
        stat = dict((k, int(v)) for k, v in
                    (line.split() for line in self._read(domain, "memory.stat").splitlines()))
        return {"current": int(self._read(domain, "memory.current")),
                "swap": int(self._read(domain, "memory.swap.current")), **stat}

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("cache controller is one-shot")
        try:
            for domain in self.domains.values():
                domain.fd = os.open(domain.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                # Disable swap for only these explicitly delegated daemon leaves.
                # This lets old kernels use bounded ordinary reclaim without
                # silently offloading the daemon's anonymous state to SSD.
                domain.swap_max = self._read(domain, "memory.swap.max")
                self._write(domain, "memory.swap.max", "0")
                try:
                    self._write(domain, "memory.reclaim", "0 swappiness=0")
                    domain.file_only = True
                except OSError as error:
                    if error.errno == errno.EAGAIN:
                        domain.file_only = True
                    elif error.errno == errno.EINVAL:
                        domain.file_only = False
                    else:
                        raise
            self._thread = threading.Thread(target=self._run, name="caden-service-cache", daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def foreground(self, base: str):
        domain = self.domains.get(base)
        if domain is None:
            # Unknown bases cannot silently bypass protection in an enabled run.
            raise ValueError(f"base has no delegated cache domain: {base}")
        with domain.condition:
            domain.foreground_waiters += 1
            try:
                while domain.background_active:
                    domain.condition.wait()
                domain.foreground_active += 1
            finally:
                domain.foreground_waiters -= 1
        try:
            # Readers run concurrently; only a background reclaim is exclusive.
            yield
        finally:
            with domain.condition:
                domain.foreground_active -= 1
                domain.condition.notify_all()

    def step(self, base: str) -> int:
        domain = self.domains[base]
        with domain.condition:
            if (domain.foreground_waiters or domain.foreground_active
                    or domain.background_active or self._stop.is_set()):
                return 0
            domain.background_active = True
        try:
            if not self.idle(base):
                return 0
            before = self._observe(domain)
            # Never select dirty/writeback or mapped bytes deliberately. These
            # aggregate counters are a conservative budget, not page selection.
            cold = max(0, before.get("inactive_file", 0) - before.get("file_dirty", 0)
                       - before.get("file_writeback", 0) - before.get("file_mapped", 0))
            amount = min(self.chunk_bytes, max(0, before["current"] - self.reserve_bytes), cold)
            if amount < 1 << 20:
                return 0
            started = time.monotonic_ns()
            partial = False
            try:
                self._write(domain, "memory.reclaim", str(amount) +
                            (" swappiness=0" if domain.file_only else ""))
            except OSError as error:
                if error.errno != errno.EAGAIN:
                    raise
                partial = True
            after = self._observe(domain)
            self.events.append({"base": base, "cgroup": str(domain.path),
                "started_ns": started, "duration_ns": time.monotonic_ns() - started,
                "requested_bytes": amount, "charge_delta_bytes": before["current"] - after["current"],
                "file_delta_bytes": before.get("file", 0) - after.get("file", 0),
                "swap_delta_bytes": after["swap"] - before["swap"],
                "file_only_supported": domain.file_only, "partial": partial,
                "before": before, "after": after})
            if after["swap"] > before["swap"]:
                raise RuntimeError("daemon swap increased during cache reclamation")
            return amount
        finally:
            with domain.condition:
                domain.background_active = False
                domain.condition.notify_all()

    def _run(self) -> None:
        # Rate limit actual work, not empty visits. Fair round-robin domains;
        # one worker and a global byte budget keep reclaim bounded.
        spent = 0
        window = time.monotonic()
        while not self._stop.is_set():
            work = 0
            for base in self.domains:
                if self._stop.is_set():
                    return
                try:
                    amount = self.step(base)
                    spent += amount
                    work += amount
                except Exception as error:
                    self.events.append({"base": base, "error": f"{type(error).__name__}: {error}"})
                    self._stop.set()  # Disable the feature, preserve evidence.
                    return
                if spent + self.chunk_bytes > self.max_bytes_per_interval:
                    if self._stop.wait(max(0, self.interval_seconds - (time.monotonic() - window))):
                        return
                    spent, window = 0, time.monotonic()
            if not work:
                if self._stop.wait(self.interval_seconds):
                    return
                spent, window = 0, time.monotonic()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()  # Do not restore limits while a write is in flight.
        failures = []
        for domain in self.domains.values():
            if domain.fd < 0:
                continue
            try:
                if domain.swap_max is not None:
                    self._write(domain, "memory.swap.max", domain.swap_max)
            except OSError as error:
                failures.append(f"{domain.path}: {error}")
            finally:
                os.close(domain.fd)
                domain.fd = -1
        if failures:
            raise RuntimeError("cannot restore daemon limits: " + "; ".join(failures))
