from dataclasses import dataclass
from pathlib import Path
import json
from datetime import datetime

from experiments.swebench_runner.bench_types import (
    ResourceData,
    ResourceSample,
    ResourceSummary,
    ResourceStats,
)


@dataclass(slots=True)
class ResourceSeries:
    times: list[float]
    memory: list[float]
    cpu: list[float]
    start_epoch: float


@dataclass(slots=True)
class ToolCall:
    time: float
    tool: str
    duration: float = 0.0


def parse_timestamp(ts: str | float | None) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (float, int)):
        return float(ts)
    if isinstance(ts, str):
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    return None


def load_tool_calls(tool_calls_path: Path, start_epoch: float) -> list[ToolCall]:
    with open(tool_calls_path, "r") as f:
        calls = json.load(f)

    result: list[ToolCall] = []

    for call in calls:
        timestamp = parse_timestamp(call.get("timestamp"))
        tool = call.get("tool")

        if timestamp is None or not tool:
            continue

        relative_time = timestamp - start_epoch
        if relative_time >= 0:
            duration = 0.0
            end_timestamp = parse_timestamp(call.get("end_timestamp"))
            if end_timestamp is not None:
                duration = max(0.0, end_timestamp - timestamp)
            result.append(ToolCall(time=relative_time, tool=tool, duration=duration))

    return result


def load_resources(resources_path: Path) -> ResourceData:
    with open(resources_path, "r") as f:
        data = json.load(f)

    summary_data = data.get("summary")
    if summary_data is None:
        raise ValueError("No resource summary found")

    return ResourceData(
        samples=[ResourceSample(**item) for item in data["samples"]],
        summary=ResourceSummary(
            sample_count=summary_data["sample_count"],
            duration_seconds=summary_data["duration_seconds"],
            memory_mb=ResourceStats(**summary_data["memory_mb"]),
            cpu_percent=ResourceStats(**summary_data["cpu_percent"]),
        ),
    )
