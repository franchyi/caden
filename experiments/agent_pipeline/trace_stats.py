"""Aggregate statistics across generated trace bundles (work/*/attempt_1/results.json): per-trace
and aggregate stage timing (LLM_WAIT vs TOOL_BURST + the LLM-wait fraction the paper reports), steps,
cost, resolved count, and resource summary. Writes trace_stats.json and prints it."""

import glob
import json
import statistics
import sys
from pathlib import Path


def _stat(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return {"min": round(min(xs), 3), "max": round(max(xs), 3),
            "mean": round(statistics.mean(xs), 3), "median": round(statistics.median(xs), 3)}


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else "work/*/attempt_1/results.json"
    rows = [json.load(open(p)) for p in sorted(glob.glob(pattern))]
    if not rows:
        print(f"no results.json matched {pattern}")
        return

    per_trace, llm_fracs = [], []
    for d in rows:
        ss = d.get("stage_seconds", {})
        llm, tb = ss.get("LLM_WAIT", 0) or 0, ss.get("TOOL_BURST", 0) or 0
        total = llm + tb
        frac = (llm / total * 100) if total > 0 else 0
        llm_fracs.append(frac)
        rsum = d.get("resource_summary", {})
        per_trace.append({
            "instance_id": d.get("instance_id"), "model": d.get("model"),
            "steps": d.get("steps"), "resolved": d.get("resolved"),
            "exit_status": d.get("exit_status"),
            "LLM_WAIT_s": llm, "TOOL_BURST_s": tb, "llm_frac_pct": round(frac, 1),
            "cost_usd": d.get("total_cost_usd"), "num_llm_calls": d.get("num_llm_calls"),
            "mem_mb_avg": rsum.get("memory_mb", {}).get("avg"),
            "cpu_pct_avg": rsum.get("cpu_percent", {}).get("avg"),
            "cpu_pct_max": rsum.get("cpu_percent", {}).get("max"),
        })

    out = {
        "n_traces": len(rows),
        "per_trace": per_trace,
        "aggregate": {
            "resolved_count": sum(1 for d in rows if d.get("resolved")),
            "steps": _stat([d.get("steps") for d in rows]),
            "LLM_WAIT_s": _stat([(d.get("stage_seconds") or {}).get("LLM_WAIT") for d in rows]),
            "TOOL_BURST_s": _stat([(d.get("stage_seconds") or {}).get("TOOL_BURST") for d in rows]),
            "llm_frac_pct": _stat(llm_fracs),
            "cost_usd": _stat([d.get("total_cost_usd") for d in rows]),
            "total_cost_usd": round(sum(d.get("total_cost_usd") or 0 for d in rows), 4),
            "mem_mb_avg": _stat([(d.get("resource_summary") or {}).get("memory_mb", {}).get("avg") for d in rows]),
            "cpu_pct_avg": _stat([(d.get("resource_summary") or {}).get("cpu_percent", {}).get("avg") for d in rows]),
        },
    }
    print(json.dumps(out, indent=2))
    Path("trace_stats.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
