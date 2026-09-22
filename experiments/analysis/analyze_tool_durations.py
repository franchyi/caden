import argparse
from pathlib import Path
from experiments.analysis.utils import (
    load_resources,
    load_tool_calls,
    ToolCall,
)


def analyze_tool_durations(
    results_dir: Path | str = "experiments-results", prefix: str = "manual"
) -> dict:
    """
    Examines all the results in a folder and aggregates the normalized percentage
    of time spent on LLM inference vs tool calls.
    """
    results_dir = Path(results_dir)
    if not results_dir.is_absolute():
        project_root = Path(__file__).resolve().parents[2]
        results_dir = project_root / results_dir

    results_dir = results_dir / prefix

    if not results_dir.exists():
        print(f"Directory {results_dir} does not exist.")
        return {}

    llm_percentages = []
    tool_percentages = []

    for exp_dir in results_dir.iterdir():
        if not exp_dir.is_dir():
            continue

        resources_path = exp_dir / "resources.json"
        tool_calls_path = exp_dir / "tool_calls.json"

        resource_data = load_resources(resources_path)

        summary = resource_data.summary
        total_duration = summary.duration_seconds
        start_epoch = resource_data.samples[0].epoch

        tool_calls: list[ToolCall] = load_tool_calls(tool_calls_path, start_epoch)
        tool_calls = list(filter(lambda call: call.tool != "Task", tool_calls))
        total_tool_time = sum(call.duration for call in tool_calls)

        llm_time = max(0.0, total_duration - total_tool_time)

        llm_pct = llm_time / total_duration
        tool_pct = total_tool_time / total_duration

        llm_percentages.append(llm_pct)
        tool_percentages.append(tool_pct)

    if not llm_percentages:
        print("No valid results found to analyze.")
        return {}

    avg_llm_pct = sum(llm_percentages) / len(llm_percentages)
    avg_tool_pct = sum(tool_percentages) / len(tool_percentages)

    print(f"Analyzed {len(llm_percentages)} results.")
    print(f"Average LLM Inference Time: {avg_llm_pct:.2%}")
    print(f"Average Tool Call Time: {avg_tool_pct:.2%}")

    return {
        "avg_llm_percentage": avg_llm_pct,
        "avg_tool_percentage": avg_tool_pct,
        "count": len(llm_percentages),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analyze tool vs LLM inference durations across experiment results."
    )
    parser.add_argument(
        "--results-dir",
        default="experiments-results",
        help="Path to the results directory.",
    )
    parser.add_argument(
        "--prefix",
        default="manual",
        help="Sub directory to output inside the experiments-results dir",
    )
    args = parser.parse_args()

    analyze_tool_durations(args.results_dir, args.prefix)
