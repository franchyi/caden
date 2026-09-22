from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt

from experiments.analysis.utils import (
    ResourceSeries,
    ToolCall,
    load_tool_calls,
    load_resources,
)
from experiments.utils.parse_resources import parse_memory, parse_cpu
from experiments.swebench_runner.bench_types import (
    ResourceData,
)


TOOL_COLORS = {
    "Write": "#1f77b4",
    "WebSearch": "#ff7f0e",
    "TodoWrite": "#2ca02c",
    "Read": "#d62728",
    "Task": "#9467bd",
    "Bash": "#8c564b",
    "Glob": "#e377c2",
    "WebFetch": "#7f7f7f",
    "Edit": "#bcbd22",
    "Grep": "#17becf",
    "LLM Inference": "#cccccc",
}


def build_resource_series(resource_data: ResourceData) -> ResourceSeries:
    if not resource_data.samples:
        raise ValueError("No resource samples found")

    start_epoch = resource_data.samples[0].epoch

    times: list[float] = []
    memory: list[float] = []
    cpu: list[float] = []

    for sample in resource_data.samples:
        times.append(sample.epoch - start_epoch)

        mem_str = sample.mem_usage.split("/")[0].strip()
        mem_mb = parse_memory(mem_str)
        memory.append(mem_mb if mem_mb is not None else 0)

        cpu_val = parse_cpu(sample.cpu_percent)
        cpu.append(cpu_val if cpu_val is not None else 0.0)

    return ResourceSeries(
        times=times,
        memory=memory,
        cpu=cpu,
        start_epoch=start_epoch,
    )


def plot_resources(
    resources_path: Path,
    tool_calls_path: Path | None = None,
    output_path: Path | None = None,
    title: str | None = None,
    memory_limit: float | None = None,
    cpu_limit: float | None = None,
    show_limits: bool = False,
):
    """Generate resource usage plot."""

    resource_data = load_resources(resources_path)

    if not resource_data.samples:
        print("No resource samples found")
        return None

    series = build_resource_series(resource_data)
    summary = resource_data.summary

    # Load tool calls if provided
    tool_calls = []
    if tool_calls_path and tool_calls_path.exists():
        tool_calls = load_tool_calls(tool_calls_path, series.start_epoch)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    fig.suptitle(
        title or "Claude Code Resource Usage",
        fontsize=14,
        fontweight="bold",
    )

    # Memory plot
    ax1.fill_between(series.times, series.memory, alpha=0.3, color="blue")
    ax1.plot(
        series.times,
        series.memory,
        "b-",
        linewidth=1.5,
        label="Memory Usage",
    )

    ax1.axhline(
        y=summary.memory_mb.avg,
        color="gray",
        linestyle="--",
        alpha=0.7,
        label=f"Avg: {summary.memory_mb.avg:.1f} MB",
    )

    if show_limits and memory_limit:
        ax1.axhline(
            y=memory_limit,
            color="orange",
            linestyle=":",
            alpha=0.7,
            label=f"Limit: {memory_limit:.0f} MB",
        )

    ax1.set_ylabel("Memory (MB)", fontsize=11)
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(bottom=0)

    # CPU plot
    ax2.fill_between(series.times, series.cpu, alpha=0.3, color="green")
    ax2.plot(
        series.times,
        series.cpu,
        "g-",
        linewidth=1.5,
        label="CPU Usage",
    )

    ax2.axhline(
        y=summary.cpu_percent.avg,
        color="gray",
        linestyle="--",
        alpha=0.7,
        label=f"Avg: {summary.cpu_percent.avg:.1f}%",
    )

    if show_limits and cpu_limit:
        ax2.axhline(
            y=cpu_limit * 100,
            color="orange",
            linestyle=":",
            alpha=0.7,
            label=f"Limit ({cpu_limit:.0f} CPUs)",
        )

    ax2.set_ylabel("CPU (%)", fontsize=11)
    ax2.set_xlabel("Time (seconds)", fontsize=11)
    ax2.legend(loc="upper right", fontsize=9)
    ax2.grid(True, alpha=0.3)
    ax2.set_ylim(bottom=0)

    # Add tool call markers
    if tool_calls:
        bash_calls = [c for c in tool_calls if c.tool == "Bash"]

        for call in bash_calls:
            t = call.time
            if 0 <= t <= max(series.times):
                ax1.axvline(
                    x=t,
                    color="red",
                    linestyle=":",
                    alpha=0.5,
                    linewidth=0.8,
                )
                ax2.axvline(
                    x=t,
                    color="red",
                    linestyle=":",
                    alpha=0.5,
                    linewidth=0.8,
                )

    summary_text = [
        f"Duration: {summary.duration_seconds:.1f}s",
        f"Samples: {summary.sample_count}",
        (
            f"Memory: {summary.memory_mb.avg:.1f} MB avg, "
            f"{summary.memory_mb.max:.1f} MB max"
        ),
        (
            f"CPU: {summary.cpu_percent.avg:.1f}% avg, "
            f"{summary.cpu_percent.max:.1f}% max"
        ),
    ]

    if tool_calls:
        summary_text.append(f"Tool calls: {len(tool_calls)}")

    props = dict(boxstyle="round", facecolor="wheat", alpha=0.8)
    fig.text(
        0.02,
        0.02,
        "\n".join(summary_text),
        fontsize=9,
        verticalalignment="bottom",
        bbox=props,
        family="monospace",
    )

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.15)

    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        print(f"Plot saved to: {output_path}")
    else:
        plt.show()

    plt.close()
    return output_path


def plot_tool_usage_pie(
    resources_path: Path,
    tool_calls_path: Path,
    output_path: Path | None = None,
    title: str | None = None,
):
    resource_data = load_resources(resources_path)
    if not resource_data.samples:
        print("No resource samples found")
        return None

    summary = resource_data.summary
    total_duration = summary.duration_seconds
    start_epoch = resource_data.samples[0].epoch

    tool_calls: list[ToolCall] = []
    if tool_calls_path and tool_calls_path.exists():
        tool_calls = load_tool_calls(tool_calls_path, start_epoch)

    tool_durations: defaultdict[str, float] = defaultdict(float)
    tool_calls = list(filter(lambda call: call.tool != "Task", tool_calls))
    for call in tool_calls:
        tool_durations[call.tool] += call.duration

    total_tool_time = sum(tool_durations.values())
    llm_time = max(0.0, total_duration - total_tool_time)

    labels = ["LLM Inference"]
    sizes = [llm_time]
    colors = [TOOL_COLORS.get("LLM Inference", "#cccccc")]

    for tool, duration in sorted(
        tool_durations.items(), key=lambda x: x[1], reverse=True
    ):
        labels.append(tool)
        sizes.append(duration)
        colors.append(TOOL_COLORS.get(tool, "#333333"))

    fig, ax = plt.subplots(figsize=(10, 8))
    fig.suptitle(
        title or "Claude Code Time Breakdown",
        fontsize=14,
        fontweight="bold",
    )

    pie_output = ax.pie(
        sizes,
        colors=colors,
        autopct="%1.1f%%",
        startangle=90,
        pctdistance=0.85,
    )
    wedges = pie_output[0]

    ax.legend(
        wedges,
        labels,
        title="Categories",
        loc="center left",
        bbox_to_anchor=(1, 0, 0.5, 1),
    )

    ax.axis("equal")

    plt.tight_layout()

    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        print(f"Plot saved to: {output_path}")
    else:
        plt.show()

    plt.close()
    return output_path
