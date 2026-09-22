# Caden Implementation

Implementation code will live here.

The interfaces (see `doc/scheduling-system-design.md`) live in `caden/`: `types.py`
(shared types), `sandbox.py` (`Sandbox` abstraction + `BubblewrapSandbox` backend),
`execution.py` (`CpuPath` / `MemoryPath` / `Execution` — I2), `scheduler.py` (`Caden` — I1).

The planned lower-bound implementation is a cooperative stage shim, cgroup v2
freezer and `memory.reclaim` controller, and a `sched_ext` CPU policy for
response wakeups and local tool bursts.
