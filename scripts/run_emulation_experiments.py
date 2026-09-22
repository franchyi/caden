from collections import defaultdict
from dataclasses import asdict, dataclass
import argparse
import subprocess
import time
import sys
import json
import os
import re
import concurrent.futures
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from experiments.utils import prepare_output_dir
from experiments.swebench_runner.runner import SWEBenchRunner
from experiments.swebench_runner.cgroup_monitor import CgroupResourceMonitor
from experiments.mock.load_server import upload_trace
from experiments.swebench_runner.utils import (
    generate_plots,
    extract_identifier,
    extract_prompt_override,
)
from experiments.swebench_runner.trace_validator import verify_trace_fidelity
from experiments.analysis.plot_resource_usage import plot_resources
from experiments.swebench_runner.bench_types import (
    ResourceSample,
    ResourceStats,
    ResourceSummary,
    ResourceData,
)
from experiments.utils.parse_resources import (
    parse_memory,
    parse_cpu,
    parse_cpu_limit_from_cpuset,
)

SKIP_LIST = [
    "getsentry__sentry-python-2148",
    "mwouts__jupytext-372",
    "joke2k__faker-1520",
]


def __validate_memory_max_bytes(value: str) -> str:
    if not re.fullmatch(r"\d+[KMGT]", value):
        raise argparse.ArgumentTypeError(
            "memory max bytes argument must be an integer followed by K, M, G or T"
        )
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run emulation experiments.")
    parser.add_argument(
        "--test-trace-fidelity",
        action="store_true",
        help="Whether to validate the traces or not",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="Whether to run the mock server in production mode (disables saving logs)",
    )
    parser.add_argument(
        "--fast-forward",
        action="store_true",
        help="Whether to run the mock server in fast forward mode",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Directory to output results to",
    )
    parser.add_argument(
        "--output-prefix",
        type=str,
        help="Sub directory to output inside the output dir.",
        default="manual",
    )
    parser.add_argument(
        "--disable-logging",
        action="store_true",
        help="Whether to disable stdout logging from individual experiments",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of traces to run concurrently. Caps the total number of traces run.",
    )
    # Absorbed arguments from run_swebench.py
    parser.add_argument("--memory", default="4g", help="Memory limit (default: 4g)")
    parser.add_argument("--cpus", default="2", help="CPU limit (default: 2)")
    parser.add_argument(
        "--model", default="haiku", help="Model to use (default: haiku)"
    )
    parser.add_argument(
        "--show-limits",
        action="store_true",
        help="Show memory and CPU limits on the resource usage plot",
    )
    # Caden args
    parser.add_argument("--cgroup-slice", type=str, default="caden.slice")
    parser.add_argument(
        "--memory-max-bytes",
        default="32G",
        help="Memory max bytes for the cgroup",
        type=__validate_memory_max_bytes,
    )
    parser.add_argument(
        "--cpu-quota-percent", default="100", help="Max CPU quota without the percent"
    )
    parser.add_argument(
        "--cpuset-cpus", default="0-16", help="CPUs to use for the cgroup"
    )
    return parser.parse_args()


# -----------
# Mock Server
# -----------


def start_mock_server(base_dir: Path, args: argparse.Namespace) -> subprocess.Popen:
    print("Starting mock server in the background...")
    env = os.environ.copy()
    env["PRODUCTION_MODE"] = str(args.production).lower()
    env["FAST_FORWARD_MODE"] = str(args.fast_forward).lower()
    env["PYTHONPATH"] = f"{base_dir}:{env.get('PYTHONPATH', '')}".strip(":")

    process = subprocess.Popen(
        ["uv", "run", "experiments/mock/mock_server.py"], cwd=str(base_dir), env=env
    )
    time.sleep(2)

    if process.poll() is not None:
        raise RuntimeError(
            f"Mock server exited immediately with code {process.returncode}"
        )

    return process


def is_valid_trace_dir(trace_dir: Path) -> bool:
    for skip in SKIP_LIST:
        if skip in str(trace_dir):
            print(f"Skipping: {skip}")
            return False

    trace_file = trace_dir / "trace.jsonl"
    return trace_file.exists()


def discover_traces(traces_dir: Path) -> list[Path]:
    trace_dirs = sorted(traces_dir.glob("*/attempt_1"))
    result = list(filter(is_valid_trace_dir, trace_dirs))
    print(f"Found {len(result)} trace directories. Starting emulation experiments...")
    return result


# ------
# cgroup
# ------


def init_cgroup(
    name: str | Path,
    memory_max_bytes: str | None = None,
    cpu_quota_percent: str | None = None,
    cpuset_cpus: str | None = None,
):
    """
    memory_max_bytes: Hard memory ceiling for the entire slice
    cpu_quota_percent: Total CPU budget as a percentage of one core.
    cpuset_cpus: What cores to pin for the containers
    """
    slice_name = name.name if isinstance(name, Path) else name
    # slice_name = f"/sys/fs/cgroup/user.slice/{slice_name}"
    cmd = [
        "systemd-run",
        "--user",
        "--slice",
        slice_name,
        "--scope",
        "--description=caden",
        # systemd-run requires a command to execute, so we pass in true as the
        # placeholder command to actually create the slice
        "--",
        "true",
    ]
    subprocess.run(cmd, check=True)
    props = []
    if memory_max_bytes:
        props.append(f"MemoryMax={memory_max_bytes}")
    if cpu_quota_percent:
        props.append(f"CPUQuota={cpu_quota_percent}%")
    if cpuset_cpus:
        props.append(f"AllowedCPUs={cpuset_cpus}")
    if props:
        cmd = ["systemctl", "--user", "set-property", "--runtime", slice_name]
        for p in props:
            cmd.append(p)
        subprocess.run(cmd, check=True)


def remove_cgroup(name: str):
    subprocess.run(["systemctl", "--user", "stop", name], check=False)


# ---------------
# Phases pipeline
# ---------------
@dataclass
class EmulationState:
    trace_dir: Path
    runner: SWEBenchRunner
    prompt: str
    image_name: str
    args: argparse.Namespace


def prepare(
    trace_dir: Path, slice_name: str, args: argparse.Namespace
) -> EmulationState:
    results_json = trace_dir / "results.json"
    with open(results_json, "r") as f:
        results_data = json.load(f)
    image_name = results_data["image"]

    trace_file = trace_dir / "trace.jsonl"
    identifier = extract_identifier(trace_file)
    if not identifier:
        raise ValueError(f"Could not find identifier in {trace_file}")

    upload_trace(trace_file)
    prompt = extract_prompt_override(trace_file)

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else prepare_output_dir(image_name, args.output_prefix)
    )

    runner = SWEBenchRunner(
        image_name=image_name,
        cgroup_parent=slice_name,
        memory_limit=args.memory,
        cpu_limit=args.cpus,
        output_dir=output_dir,
        replay_session=identifier,
    )

    runner.prepare(model=args.model)
    return EmulationState(trace_dir, runner, prompt, image_name, args)


def run(state: EmulationState) -> EmulationState:
    state.runner.run_container(prompt=state.prompt)
    return state


def get_results(state: EmulationState, monitor: CgroupResourceMonitor) -> dict:
    results = state.runner.get_results()
    ret = {}
    if (
        results.output_dir
        and hasattr(state.runner, "container_id")
        and state.runner.container_id
    ):
        resource_data = monitor.get_resource_data(state.runner.container_id)
        with open(Path(results.output_dir) / "resources.json", "w") as f:
            json.dump(asdict(resource_data), f, indent=2)
        results.resource_samples = resource_data

        summary = resource_data.summary
        print(
            f"[{state.image_name}] Collected {len(resource_data.samples)} resource samples"
        )
        print(
            f"[{state.image_name}] Memory: avg={summary.memory_mb.avg:.1f}MB, "
            f"max={summary.memory_mb.max:.1f}MB"
        )
        print(
            f"[{state.image_name}] CPU: avg={summary.cpu_percent.avg:.1f}%, "
            f"max={summary.cpu_percent.max:.1f}%"
        )

        generate_plots(
            output_dir=results.output_dir,
            image_name=state.image_name,
            memory_limit=state.args.memory,
            cpu_limit=state.args.cpus,
            show_limits=state.args.show_limits,
        )

        if state.args.test_trace_fidelity:
            verify_trace_fidelity(
                gt_trace_dir=str(state.trace_dir),
                output_dir=results.output_dir,
                exp_tool_calls=results.traces.get("tool_calls", [])
                if results.traces
                else [],
            )

        ret = {
            "trace_dir": str(state.trace_dir),
            "success": results.error is None,
            "output_dir": str(results.output_dir),
            "resource_summary": asdict(summary),
            "error": results.error,
        }

    return ret


def post_experiment_plots(
    report_dir: Path,
    monitor: CgroupResourceMonitor,
    timestamp_suffix: str,
    mem_limit: float,
    cpu_limit: float,
):
    overall_samples: list[ResourceSample] = []
    epoch_to_samples = defaultdict(list)

    for cid, samples in monitor.container_stats.items():
        for s in samples:
            epoch_to_samples[s.epoch].append(s)

    if not epoch_to_samples:
        return

    mem_values = []
    cpu_values = []

    for epoch in sorted(epoch_to_samples.keys()):
        samples = epoch_to_samples[epoch]
        timestamp = samples[0].timestamp

        total_mem_mb = 0.0
        total_cpu_pct = 0.0

        for s in samples:
            mem_mb = parse_memory(s.mem_usage.strip())
            if mem_mb is not None:
                total_mem_mb += mem_mb
            cpu_val = parse_cpu(s.cpu_percent)
            if cpu_val is not None:
                total_cpu_pct += cpu_val

        mem_values.append(total_mem_mb)
        cpu_values.append(total_cpu_pct)

        overall_samples.append(
            ResourceSample(
                timestamp=timestamp,
                epoch=epoch,
                mem_usage=f"{total_mem_mb:.2f}MiB",
                cpu_percent=f"{total_cpu_pct:.2f}%",
            )
        )

    summary = ResourceSummary(
        sample_count=len(overall_samples),
        duration_seconds=overall_samples[-1].epoch - overall_samples[0].epoch
        if len(overall_samples) > 1
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

    resource_data = ResourceData(samples=overall_samples, summary=summary)
    resources_path = report_dir / f"overall_resources_{timestamp_suffix}.json"
    with open(resources_path, "w") as f:
        json.dump(asdict(resource_data), f, indent=2)

    plot_resources(
        resources_path=resources_path,
        output_path=report_dir / f"overall_resources_{timestamp_suffix}.png",
        title="Overall Cgroup Resource Usage",
        show_limits=True,
        memory_limit=mem_limit,
        cpu_limit=cpu_limit,
    )


def main():
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    base_dir = script_dir.parent
    traces_dir = base_dir / "traces" / "all_images_haiku"

    mock_server_process = start_mock_server(base_dir, args)

    start_time = datetime.now()
    report_dir = base_dir / "experiments-results" / args.output_prefix
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"report_{start_time.strftime('%Y%m%d_%H%M%S')}.json"

    report = {
        "start_time": start_time.isoformat(),
        "metadata": {
            "concurrency": args.concurrency,
            "cgroup_limits": {
                "memory_max_bytes": args.memory_max_bytes,
                "cpu_quota_percent": args.cpu_quota_percent,
                "cpuset_cpus": args.cpuset_cpus,
            },
        },
        "experiments": [],
    }

    monitor = None

    try:
        init_cgroup(
            args.cgroup_slice,
            args.memory_max_bytes,
            args.cpu_quota_percent,
            args.cpuset_cpus,
        )

        traces = discover_traces(traces_dir)[: args.concurrency]
        workers = len(traces) if traces else 1

        print("\n--- Preparing images ---")
        states = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(prepare, trace_dir, args.cgroup_slice, args)
                for trace_dir in traces
            ]
            for future in concurrent.futures.as_completed(futures):
                states.append(future.result())

        print("\n--- Running ---")
        finished_states = []
        with CgroupResourceMonitor(args.cgroup_slice, interval=1.0) as monitor:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(run, state) for state in states]
                for future in concurrent.futures.as_completed(futures):
                    finished_states.append(future.result())

        print("\n--- Results ---")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(get_results, state, monitor)
                for state in finished_states
            ]
            for future in concurrent.futures.as_completed(futures):
                report["experiments"].append(future.result())

    except KeyboardInterrupt:
        print("\nInterrupted by user. Shutting down...")
    except Exception as e:
        print(f"\nError during execution: {e}")
    finally:
        print(f"\nRemoving cgroup {args.cgroup_slice}")
        remove_cgroup(args.cgroup_slice)

        print(f"\nSaving final report to {report_path}")
        end_time = datetime.now()
        report["end_time"] = end_time.isoformat()

        total_time_seconds = (end_time - start_time).total_seconds()

        experiments = report["experiments"]
        total_workloads = len(experiments)
        successful_workloads = sum(1 for exp in experiments if exp.get("success"))

        # TODO: Make this better
        def is_infra_fail(err):
            if not err:
                return False
            return (
                "container storage" in err.lower() or "failed to start" in err.lower()
            )

        infrastructure_failures = sum(
            1 for exp in experiments if is_infra_fail(exp.get("error"))
        )

        report["execution_stats"] = {
            "total_time_seconds": total_time_seconds,
            "total_workloads": total_workloads,
            "successful_workloads": successful_workloads,
            "infrastructure_failures": infrastructure_failures,
            "throughput_workloads_per_second": total_workloads / total_time_seconds
            if total_time_seconds > 0
            else 0,
        }

        min_cpus: list[float] = []
        avg_cpus: list[float] = []
        peak_cpus: list[float] = []
        min_mems: list[float] = []
        avg_mems: list[float] = []
        peak_mems: list[float] = []
        for exp in experiments:
            res = exp.get("resource_summary")
            if res:
                min_cpus.append(res["cpu_percent"]["min"])
                avg_cpus.append(res["cpu_percent"]["avg"])
                peak_cpus.append(res["cpu_percent"]["max"])
                min_mems.append(res["memory_mb"]["min"])
                avg_mems.append(res["memory_mb"]["avg"])
                peak_mems.append(res["memory_mb"]["max"])

        if avg_cpus:
            report["resource_utilization"] = {
                "min_cpu_percent": min(min_cpus),
                "avg_cpu_percent": sum(avg_cpus) / len(avg_cpus),
                "peak_cpu_percent": max(peak_cpus),
                "min_memory_mb": min(min_mems),
                "avg_memory_mb": sum(avg_mems) / len(avg_mems),
                "peak_memory_mb": max(peak_mems),
            }

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        if monitor is not None:
            print("\nGenerating overall resource plots...")
            timestamp_suffix = start_time.strftime("%Y%m%d_%H%M%S")
            mem_limit = parse_memory(args.memory_max_bytes)
            cpu_limit = parse_cpu_limit_from_cpuset(args.cpuset_cpus)
            post_experiment_plots(
                report_dir, monitor, timestamp_suffix, mem_limit, cpu_limit
            )

        print("Shutting down mock server...")
        mock_server_process.terminate()
        mock_server_process.wait()
        print("Mock server shutdown complete.")


if __name__ == "__main__":
    main()
