import json
import threading
from pathlib import Path
from experiments.analysis.plot_resource_usage import plot_resources, plot_tool_usage_pie
from experiments.utils.parse_resources import parse_memory

_plot_lock = threading.Lock()



def generate_plots(
    output_dir: Path | str,
    image_name: str,
    memory_limit: str | None = None,
    cpu_limit: str | None = None,
    show_limits: bool = False,
) -> None:
    output_dir = Path(output_dir)
    resources_file = output_dir / "resources.json"
    if not resources_file.exists():
        return

    try:
        tool_calls_file = output_dir / "tool_calls.json"
        plot_output_path = output_dir / "resource_usage.png"
        pie_output_path = output_dir / "tool_usage_pie.png"

        mem_limit_mb = parse_memory(memory_limit) if memory_limit else None

        cpu_limit_val = None
        if cpu_limit:
            try:
                cpu_limit_val = float(cpu_limit)
            except ValueError:
                pass

        with _plot_lock:
            print(f"Generating resource usage plot: {plot_output_path}")
            plot_resources(
                resources_path=resources_file,
                tool_calls_path=tool_calls_file if tool_calls_file.exists() else None,
                output_path=plot_output_path,
                title=f"Claude Code Resource Usage ({image_name})",
                memory_limit=mem_limit_mb,
                cpu_limit=cpu_limit_val,
                show_limits=show_limits,
            )

            print(f"Generating tool usage pie chart: {pie_output_path}")
            plot_tool_usage_pie(
                resources_path=resources_file,
                tool_calls_path=tool_calls_file,
                output_path=pie_output_path,
                title=f"Claude Code Time Breakdown ({image_name})",
            )
    except Exception as e:
        print(f"Warning: Failed to generate plots: {e}")


def extract_prompt_override(trace_file: Path) -> str:
    """Extract user prompt. It may not be the second line as dask_dask-2205 shows"""
    prompt: str = ""
    with open(trace_file, "r") as f:
        for line in f.readlines():
            data = json.loads(line)
            if data.get("type", "") != "user":
                continue
            if data.get("message", None) is None:
                continue
            prompt = data["message"]["content"]
            break
    return prompt


def extract_identifier(trace_file: Path) -> str | None:
    with open(trace_file, "r") as f:
        first_line = f.readline()
        first_data = json.loads(first_line)
        if "leafUuid" in first_data:
            return first_data["leafUuid"]
        elif "sessionId" in first_data:
            return first_data["sessionId"]
        else:
            return None
