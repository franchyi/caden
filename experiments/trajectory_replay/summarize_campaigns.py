#!/usr/bin/env python3
"""Compare full-copy/static and a named T1 trajectory-replay treatment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--treatment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = load(args.baseline)
    treatment = load(args.treatment)
    validate_pair(baseline, treatment)
    before = baseline["summary"]
    after = treatment["summary"]
    memory_reduction = reduction(
        before["attributable_dram_active_mean_bytes"],
        after["attributable_dram_active_mean_bytes"],
    )
    p50_reduction = reduction(
        before["request_ready_p50_ns"], after["request_ready_p50_ns"]
    )
    p95_reduction = reduction(
        before["request_ready_p95_ns"], after["request_ready_p95_ns"]
    )
    turn_p95_change = ratio_change(before["turn_p95_ns"], after["turn_p95_ns"])
    verdict = {
        "memory_reduction_fraction": memory_reduction,
        "request_ready_p50_reduction_fraction": p50_reduction,
        "request_ready_p95_reduction_fraction": p95_reduction,
        "turn_p95_change_fraction": turn_p95_change,
        "memory_target_pass": memory_reduction > 0.50,
        "cold_start_target_pass": p50_reduction > 0.50 and p95_reduction > 0.50,
        "tool_latency_10pct_guardrail_pass": turn_p95_change <= 0.10,
        "trace_replay_claim_pass": (
            before["success"]
            and after["success"]
            and memory_reduction > 0.50
            and p50_reduction > 0.50
            and p95_reduction > 0.50
            and turn_p95_change <= 0.10
        ),
        "claim_scope": "trace-derived deterministic sandbox tool replay",
    }
    report = {
        "schema": "caden-trajectory-replay-comparison-v1",
        "baseline": str(args.baseline),
        "treatment": str(args.treatment),
        "baseline_summary": before,
        "treatment_summary": after,
        "verdict": verdict,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(verdict, indent=2))
    return 0


def load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("schema") not in {"caden-trajectory-replay-v1", "orca-trajectory-replay-v1"}
    ):
        raise ValueError(f"invalid trajectory replay report: {path}")
    return value


def validate_pair(baseline: dict, treatment: dict) -> None:
    if (
        baseline["workload"]["manifest_sha256"]
        != treatment["workload"]["manifest_sha256"]
    ):
        raise ValueError("workload manifest mismatch")
    if baseline["summary"]["requests"] != treatment["summary"]["requests"]:
        raise ValueError("request count mismatch")
    if (
        baseline["config"]["mode"] != "baseline"
        or baseline["config"]["policy"] != "static"
    ):
        raise ValueError("baseline must be F0-S0")
    if treatment["config"]["mode"] != "t1" or treatment["config"]["policy"] not in {
        "caden",
        "orca",  # Historical result packages.
        "fixed",
        "elapsed",
        "request-aware",
    }:
        raise ValueError("treatment must be a T1 residency policy")
    if baseline["host"]["hostname"] != treatment["host"]["hostname"]:
        raise ValueError("campaign host mismatch")


def reduction(original: int, optimized: int) -> float:
    if original <= 0:
        raise ValueError("baseline metric must be positive")
    return 1 - optimized / original


def ratio_change(original: int, optimized: int) -> float:
    if original <= 0:
        raise ValueError("baseline metric must be positive")
    return optimized / original - 1


if __name__ == "__main__":
    raise SystemExit(main())
