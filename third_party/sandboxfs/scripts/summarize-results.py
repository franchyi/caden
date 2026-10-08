#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def load(path: Path):
    with path.open() as handle:
        return json.load(handle)


def ms(value):
    return value / 1_000_000


def percent_reduction(baseline, treatment):
    return 100 * (1 - treatment / baseline)


def operation_size(operation):
    value = operation.removeprefix("partial_write_").removesuffix("m")
    return int(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--t1", type=Path, required=True)
    parser.add_argument("--t0", type=Path, required=True)
    parser.add_argument("--deferred-t1", type=Path, required=True)
    parser.add_argument("--deferred-t0", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    t1 = load(args.t1)
    t0 = load(args.t0)
    deferred_t1 = load(args.deferred_t1)
    deferred_t0 = load(args.deferred_t0)
    metadata = load(args.metadata)

    t1_summaries = {
        (summary["mode"], summary["concurrency"]): summary
        for summary in t1["summaries"]
    }
    t0_summaries = {
        (summary["mode"], summary["concurrency"]): summary
        for summary in t0["summaries"]
    }

    lines = [
        "# EC2 evaluation results",
        "",
        f"Generated from benchmark commit `{metadata['benchmark_commit'][:12]}` on "
        f"`{metadata['instance_type']}` in `{metadata['availability_zone']}`.",
        "",
        "## Cold-start latency",
        "",
        "| Mode | Concurrency | Success | p50 (ms) | p95 (ms) | p99 (ms) | p50 reduction vs baseline | p95 reduction vs baseline |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for concurrency in (1, 8):
        baseline = t1_summaries[("baseline", concurrency)]
        rows = (
            ("baseline (T1 disk)", baseline),
            ("t0 (T0 disk)", t0_summaries[("t0", concurrency)]),
            ("t1 (T1 disk)", t1_summaries[("t1", concurrency)]),
        )
        for label, summary in rows:
            if summary["mode"] == "baseline":
                p50_reduction = "—"
                p95_reduction = "—"
            else:
                p50_reduction = f"{percent_reduction(baseline['ready']['p50_ns'], summary['ready']['p50_ns']):.1f}%"
                p95_reduction = f"{percent_reduction(baseline['ready']['p95_ns'], summary['ready']['p95_ns']):.1f}%"
            lines.append(
                f"| {label} | {concurrency} | "
                f"{summary['succeeded']}/{summary['succeeded'] + summary['failed']} | "
                f"{ms(summary['ready']['p50_ns']):.3f} | {ms(summary['ready']['p95_ns']):.3f} | "
                f"{ms(summary['ready']['p99_ns']):.3f} | {p50_reduction} | {p95_reduction} |"
            )

    lines.extend(
        [
            "",
            "T0 ran after reformatting the same NVMe device with `reflink=0`. Its reductions use the T1 campaign's full-copy baseline; that baseline explicitly disables reflink and copies the same corpus on the same host.",
        ]
    )

    deferred = deferred_t1["results"] + deferred_t0["results"]
    partial = [row for row in deferred if row["operation"].startswith("partial_write") and row["mode"] in ("t0", "t1")]
    lines.extend(
        [
            "",
            "## First partial write",
            "",
            "Each operation modifies 4 KiB and flushes the file before device counters are sampled.",
            "",
            "| Mode | Lower file | Command (ms) | NVMe writes |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in sorted(partial, key=lambda value: (operation_size(value["operation"]), value["mode"])):
        size = operation_size(row["operation"])
        lines.append(
            f"| {row['mode']} | {size} MiB | {ms(row['duration_ns']):.3f} | {row['device_write_bytes'] / (1024 * 1024):.3f} MiB |"
        )

    lines.extend(
        [
            "",
            "## Testbed",
            "",
            f"- OS: {metadata['ubuntu']}, kernel `{metadata['kernel']}`.",
            f"- AMI: `{metadata['ami_id']}`; instance: `{metadata['instance_id']}`.",
            f"- Benchmark commit: `{metadata['benchmark_commit']}`; metadata capture commit: `{metadata['metadata_capture_commit']}`.",
            f"- CPU: {metadata['cpu_model']}; microcode `{metadata['microcode']}`.",
            f"- NVMe: {metadata['nvme_model']}, firmware `{metadata['nvme_firmware']}`.",
            f"- {metadata['bubblewrap_version']}; {metadata['xfsprogs_version']}; {metadata['go_version']}.",
            "- Corpus: 10,004 files, 1,189,888,015 logical bytes; identical digest across T0 and T1.",
            "- Each cold-start configuration contains 100 iterations. The prepared base was already local on a warm host.",
            "",
            "T0 and T1 have similar startup latency because both use a fixed OverlayFS mount. XFS reflink changes the deferred first-write path, not the initial mount path. Temporary replacement and full rewrites allocate new data in both modes.",
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
