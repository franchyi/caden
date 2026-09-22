"""Sample a cgroup v2's memory + CPU at a fixed interval in a background thread, and emit a
resources.json matching the SWE-bench trace corpus format ({samples, summary}). Reads
memory.current and cpu.stat (usage_usec); CPU% is the usage delta over the wall-clock delta."""

import threading
import time
from datetime import datetime


def _host_mem_bytes() -> int:
    for line in open("/proc/meminfo"):
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    return 0


class CgroupSampler:
    def __init__(self, cgroup_path: str, interval: float = 1.0):
        self.cgroup_path = cgroup_path
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mem_total = _host_mem_bytes()

    def _read_mem(self) -> int:
        with open(f"{self.cgroup_path}/memory.current") as f:
            return int(f.read().strip())

    def _read_cpu_usec(self) -> int:
        with open(f"{self.cgroup_path}/cpu.stat") as f:
            for line in f:
                k, v = line.split()
                if k == "usage_usec":
                    return int(v)
        return 0

    def _loop(self):
        prev = None  # (cpu_usec, wall)
        while not self._stop.is_set():
            now = time.time()
            try:
                mem = self._read_mem()
                cpu_usec = self._read_cpu_usec()
            except FileNotFoundError:
                break
            cpu_pct = 0.0
            if prev is not None:
                d_cpu = (cpu_usec - prev[0]) / 1e6
                d_wall = now - prev[1]
                if d_wall > 0:
                    cpu_pct = (d_cpu / d_wall) * 100.0
            prev = (cpu_usec, now)
            self.samples.append({
                "timestamp": datetime.fromtimestamp(now).isoformat(),
                "epoch": now,
                "mem_usage": f"{mem / 1e6:.2f}MB / {self._mem_total / 1e9:.1f}GB",
                "mem_percent": f"{(mem / self._mem_total * 100) if self._mem_total else 0:.2f}%",
                "cpu_percent": f"{cpu_pct:.2f}%",
            })
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def to_resources(self) -> dict:
        mems, cpus = [], []
        for s in self.samples:
            mems.append(float(s["mem_usage"].split("MB")[0]))
            cpus.append(float(s["cpu_percent"].rstrip("%")))

        def stats(xs):
            return {"min": min(xs) if xs else 0, "max": max(xs) if xs else 0,
                    "avg": sum(xs) / len(xs) if xs else 0}

        duration = (self.samples[-1]["epoch"] - self.samples[0]["epoch"]) if len(self.samples) > 1 else 0
        return {"samples": self.samples,
                "summary": {"sample_count": len(self.samples), "duration_seconds": duration,
                            "memory_mb": stats(mems), "cpu_percent": stats(cpus)}}
