import json
from pathlib import Path
from experiments.analysis.utils import parse_timestamp


class TraceCollector:
    """
    Collects Claude Code traces from a project directory, merges parent and
    subagent entries into a single timeline, and extracts tool call pairs.
    """

    def __init__(self, trace_dir: Path, output_dir: Path | None = None):
        self.trace_dir = trace_dir
        self.output_dir = output_dir

    def collect(self) -> dict:
        """
        Return a dict with keys:
          - ``files``:  list of paths to saved trace files
          - ``tool_calls``: list of matched tool_use / tool_result pairs
        """
        result: dict = {"files": [], "tool_calls": []}

        latest = self._find_latest_trace()
        if latest is None:
            return result

        entries = _read_jsonl(latest)

        subagents_dir = latest.parent / latest.stem / "subagents"
        if subagents_dir.exists():
            for sub_file in subagents_dir.glob("*.jsonl"):
                entries.extend(
                    _read_jsonl(sub_file, extra_fields={"isSidechain": True})
                )

        entries.sort(key=lambda x: x.get("timestamp", ""))

        if self.output_dir:
            dest = self.output_dir / "trace.jsonl"
            _write_jsonl(dest, entries)
            result["files"].append(str(dest))

        result["tool_calls"] = parse_tool_calls(entries)
        print(f"  Found {len(result['tool_calls'])} tool calls in trace")

        if self.output_dir and result["tool_calls"]:
            with open(self.output_dir / "tool_calls.json", "w") as f:
                json.dump(result["tool_calls"], f, indent=2)

        return result

    def _find_latest_trace(self) -> Path | None:
        if not self.trace_dir.exists():
            print(f"  Warning: Trace directory not found: {self.trace_dir}")
            return None

        traces = sorted(
            self.trace_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not traces:
            print("  Warning: No trace files found")
            return None

        print(f"  Found trace: {traces[0].name}")
        return traces[0]


def parse_tool_calls(entries: list[dict]) -> list[dict]:
    """
    Walk a list of trace entries and pair each ``tool_use`` block with its
    corresponding ``tool_result``.  Returns a list of dicts, one per
    completed tool call.
    """
    tool_calls: list[dict] = []
    pending: dict[str, dict] = {}

    for entry in entries:
        ts = parse_timestamp(entry.get("timestamp"))
        content = entry.get("message", {}).get("content", [])

        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")

            if block_type == "tool_use":
                tool_id = block.get("id")
                pending[tool_id] = {
                    "timestamp": ts,
                    "tool": block.get("name"),
                    "id": tool_id,
                    "input": block.get("input", {}),
                }

            elif block_type == "tool_result":
                tool_id = block.get("tool_use_id")
                if tool_id not in pending:
                    continue
                info = pending.pop(tool_id)
                info["end_timestamp"] = ts
                result_content = block.get("content", "")
                if isinstance(result_content, str) and len(result_content) > 500:
                    result_content = result_content[:500] + "..."
                info["result_preview"] = result_content
                tool_calls.append(info)

    return tool_calls


def _read_jsonl(path: Path, extra_fields: dict | None = None) -> list[dict]:
    """Assumes that the path already exists"""
    entries: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if extra_fields:
                entry.update(extra_fields)
            entries.append(entry)
    return entries


def _write_jsonl(dest: Path, entries: list[dict]) -> None:
    with open(dest, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
