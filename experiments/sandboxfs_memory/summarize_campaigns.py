#!/usr/bin/env python3
"""Compare F0-S0 and T1-S2 campaign reports and emit a claim verdict."""

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

    baseline_summary = baseline["summary"]
    treatment_summary = treatment["summary"]
    memory_reduction = reduction(
        baseline_summary["attributable_dram_active_mean_bytes"],
        treatment_summary["attributable_dram_active_mean_bytes"],
    )
    ready_p50_reduction = reduction(
        baseline_summary["request_ready_p50_ns"],
        treatment_summary["request_ready_p50_ns"],
    )
    ready_p95_reduction = reduction(
        baseline_summary["request_ready_p95_ns"],
        treatment_summary["request_ready_p95_ns"],
    )
    verdict = {
        "memory_reduction_fraction": memory_reduction,
        "request_ready_p50_reduction_fraction": ready_p50_reduction,
        "request_ready_p95_reduction_fraction": ready_p95_reduction,
        "memory_target_pass": memory_reduction > 0.50,
        "cold_start_target_pass": ready_p50_reduction > 0.50
        and ready_p95_reduction > 0.50,
        "mechanism_claim_pass": baseline_summary["success"]
        and treatment_summary["success"]
        and memory_reduction > 0.50
        and ready_p50_reduction > 0.50
        and ready_p95_reduction > 0.50,
        "business_claim_pass": False,
        "business_claim_reason": (
            "Synthetic mechanism reports cannot establish the internal Multi-Agent "
            "business claim; it requires at least three pre-registered internal "
            "workload runs and the interactive turn-latency SLO."
        ),
    }
    report = {
        "schema": "caden-sandboxfs-comparison-v1",
        "baseline": str(args.baseline),
        "treatment": str(args.treatment),
        "baseline_summary": baseline_summary,
        "treatment_summary": treatment_summary,
        "verdict": verdict,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(verdict, indent=2))
    return 0


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema") not in {"caden-sandboxfs-memory-v1", "orca-sandboxfs-memory-v1"}:
        raise ValueError(f"invalid campaign report: {path}")
    return value


def validate_pair(baseline: dict[str, object], treatment: dict[str, object]) -> None:
    baseline_config = baseline["config"]
    treatment_config = treatment["config"]
    for key in (
        "base",
        "sandboxes",
        "wss_mib",
        "wss_pattern",
        "wake_stride_kib",
        "llm_wait_seconds",
        "max_admissions",
        "max_wakes",
    ):
        if baseline_config[key] != treatment_config[key]:
            raise ValueError(f"configuration mismatch for {key}")
    if baseline_config["mode"] != "baseline" or baseline_config["policy"] != "static":
        raise ValueError("baseline must be full-copy static F0-S0")
    if treatment_config["mode"] != "t1" or treatment_config["policy"] not in {"caden", "orca"}:
        raise ValueError("treatment must be T1-S2")
    if baseline["host"]["hostname"] != treatment["host"]["hostname"]:
        raise ValueError("campaigns were not run on the same host")


def reduction(original: int, optimized: int) -> float:
    if original <= 0:
        raise ValueError("baseline metric must be positive")
    return 1 - optimized / original


if __name__ == "__main__":
    raise SystemExit(main())
