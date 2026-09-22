from pathlib import Path
import requests


def upload_trace(trace_file: Path, mock_server_url: str = "http://localhost:8000"):
    """POST trace.jsonl to the mock server to register the replay session."""
    url = f"{mock_server_url}/v1/replays"
    with open(trace_file, "rb") as f:
        files = {"trace": (trace_file.name, f, "application/jsonl")}
        response = requests.post(url, files=files)
        if response.status_code != 200:
            print(f"Failed to load trace file: {trace_file}")


def load_traces():
    traces_root_dir = Path(__file__).resolve().parents[2] / "traces"
    traces_dir = traces_root_dir / "all_images_haiku"
    for task_dir in traces_dir.iterdir():
        if not task_dir.is_dir():
            continue

        attempt_dir = task_dir / "attempt_1"
        if not attempt_dir.is_dir():
            continue

        trace_file = attempt_dir / "trace.jsonl"
        if not trace_file.exists():
            continue

        upload_trace(trace_file)


if __name__ == "__main__":
    load_traces()
