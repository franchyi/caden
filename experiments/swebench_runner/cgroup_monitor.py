import threading
import time
import os
from datetime import datetime
from collections import defaultdict
from pathlib import Path


from experiments.swebench_runner.bench_types import (
    ResourceStats,
    ResourceSummary,
    ResourceSample,
    ResourceData,
)
from experiments.utils.parse_resources import parse_memory, parse_cpu

CGROUP_ROOT = Path("/sys/fs/cgroup")
USEC = 1_000_000.0


class CgroupResourceMonitor:
    def __init__(self, slice_name: str, interval: float = 1.0):
        self.slice_name = slice_name
        self.interval = interval
        self.container_stats = defaultdict(list)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        uid = os.getuid()
        self.cgroup_path = (
            f"user.slice/user-{uid}.slice/user@{uid}.service/{slice_name}"
        )
        # For calculating cpu usage percentage
        self._cpu_baseline: dict[str, tuple[int, float]] = {}

    # -- context manager --
    def __enter__(self):
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        return False

    # -- public API --
    def get_resource_data(self, container_id: str) -> ResourceData | None:
        samples = self.container_stats.get(container_id, [])
        if not samples:
            return None

        mem_values = []
        cpu_values = []

        for s in samples:
            try:
                mem_str = s.mem_usage.strip()
                mem_mb = parse_memory(mem_str)
                if mem_mb is not None:
                    mem_values.append(mem_mb)

                cpu_val = parse_cpu(s.cpu_percent)
                if cpu_val is not None:
                    cpu_values.append(cpu_val)
            except Exception:
                pass

        summary = ResourceSummary(
            sample_count=len(samples),
            duration_seconds=samples[-1].epoch - samples[0].epoch
            if len(samples) > 1
            else 0,
            memory_mb=ResourceStats(
                min=min(mem_values) if mem_values else 0,
                max=max(mem_values) if mem_values else 0,
                avg=sum(mem_values) / len(mem_values) if mem_values else 0,
            ),
            cpu_percent=ResourceStats(
                min=min(cpu_values) if cpu_values else 0,
                max=max(cpu_values) if cpu_values else 0,
                avg=sum(cpu_values) / len(cpu_values) if cpu_values else 0,
            ),
        )
        return ResourceData(samples=samples, summary=summary)

    # -- private APIs --
    def _monitor_loop(self):
        while not self._stop_event.is_set():
            self._poll_stats()
            time.sleep(self.interval)

    @staticmethod
    def _read_cgroup_stats(cgroup_dir: Path, container_id: str) -> tuple[int, int]:
        cgroup_dir = cgroup_dir / f"libpod-{container_id}.scope"

        memory_bytes = int((cgroup_dir / "memory.current").read_text().strip())

        cpu_usage_usec: int = 0
        with open(cgroup_dir / "cpu.stat") as f:
            for line in f:
                k, v = line.split()
                if k == "usage_usec":
                    cpu_usage_usec = int(v)
                    break

        return memory_bytes, cpu_usage_usec

    def _poll_stats(self):
        timestamp = datetime.now().isoformat()
        now = time.time()

        cgroup_dir = CGROUP_ROOT / self.cgroup_path

        # NOTE: This might need to change if we use a different container
        for scope in cgroup_dir.glob("libpod-*.scope"):
            container_id = scope.name.removeprefix("libpod-").removesuffix(".scope")

            try:
                mem_bytes, cpu_usage_usec = self._read_cgroup_stats(
                    cgroup_dir, container_id
                )
            except Exception as e:
                print(f"Error reading stats for {container_id}: {e}")
                continue

            mem_bytes = mem_bytes / (1024**2)
            cpu_pct = 0.0

            if cpu_usage_usec is not None:
                prev = self._cpu_baseline.get(container_id)
                if prev is not None:
                    prev_usage, prev_time = prev

                    delta_cpu_sec = (cpu_usage_usec - prev_usage) / USEC
                    delta_wall_sec = now - prev_time

                    cpu_pct = (delta_cpu_sec / delta_wall_sec) * 100.0

                self._cpu_baseline[container_id] = (cpu_usage_usec, now)

            sample = ResourceSample(
                timestamp=timestamp,
                epoch=now,
                mem_usage=f"{mem_bytes:.2f}MiB",
                cpu_percent=f"{cpu_pct:.2f}%",
            )
            self.container_stats[container_id].append(sample)
