# Mocking Evaluation

The aim is to achieve high fidelity simulation of multi-tenant Claude Code agentic workloads under a cgroup with memory and CPU limits.

```
experiments
|-- README.md
|-- analysis
|-- mock
|-- mocking_evaluation.md
|-- swebench_runner
`-- utils
```

From the project directory:

```bash
usage: run_emulation_experiments.py [-h] [--test-trace-fidelity] [--production] [--fast-forward] [--output-dir OUTPUT_DIR]
                                    [--output-prefix OUTPUT_PREFIX] [--disable-logging] [--concurrency CONCURRENCY] [--memory MEMORY] [--cpus CPUS]
                                    [--model MODEL] [--show-limits] [--cgroup-slice CGROUP_SLICE] [--memory-max-bytes MEMORY_MAX_BYTES]
                                    [--cpu-quota-percent CPU_QUOTA_PERCENT] [--cpuset-cpus CPUSET_CPUS]

Run emulation experiments.

options:
  -h, --help            show this help message and exit
  --test-trace-fidelity
                        Whether to validate the traces or not
  --production          Whether to run the mock server in production mode (disables saving logs)
  --fast-forward        Whether to run the mock server in fast forward mode
  --output-dir OUTPUT_DIR
                        Directory to output results to
  --output-prefix OUTPUT_PREFIX
                        Sub directory to output inside the output dir.
  --disable-logging     Whether to disable stdout logging from individual experiments
  --concurrency CONCURRENCY
                        Number of traces to run concurrently. Caps the total number of traces run.
  --memory MEMORY       Memory limit (default: 4g)
  --cpus CPUS           CPU limit (default: 2)
  --model MODEL         Model to use (default: haiku)
  --show-limits         Show memory and CPU limits on the resource usage plot
  --cgroup-slice CGROUP_SLICE
  --memory-max-bytes MEMORY_MAX_BYTES
                        Memory max bytes for the cgroup
  --cpu-quota-percent CPU_QUOTA_PERCENT (Currently unused)
                        Max CPU quota without the percent
  --cpuset-cpus CPUSET_CPUS
                        CPUs to use for the cgroup
```

Up to concurrency traces will be launched in podman containers that run Claude Code
under the cgroup `caden.slice` with the specified memory limit and cpuset.

Enable `fast-forward` mode for faster development speed. The flag simply makes the LLM
inference instant instead of streaming the response chunk by chunk according to the real
timestamps in the traces.