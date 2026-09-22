import json
from pathlib import Path

from experiments.analysis.utils import parse_timestamp
from experiments.swebench_runner.trace_collector import _read_jsonl, parse_tool_calls


def _check_message_sequence(
    gt_entries: list[dict],
    exp_entries: list[dict],
) -> tuple[list[dict], dict[str, int]]:
    """Check that GT message UUIDs appear as a subsequence in experiment trace.

    When multiple GT entries share the same message.id (text + tool_use from
    one API response), the mock server coalesces them into a single streamed
    response using the first entry's uuid. So we deduplicate GT UUIDs by
    message.id before comparing.

    Returns (mismatches_list, counts_dict).
    """
    gt_msg_entries = [e for e in gt_entries if e.get("type") == "assistant"]
    exp_msg_entries = [e for e in exp_entries if e.get("type") == "assistant"]

    # Build GT UUIDs, keeping only the first uuid per unique message.id.
    gt_uuids = []
    seen_msg_ids: set[str] = set()
    for e in gt_msg_entries:
        event_uuid = e.get("uuid")
        if not event_uuid:
            continue
        msg = e.get("message", {})
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        if msg_id and msg_id in seen_msg_ids:
            continue  # Skip subsequent entries with same message.id
        if msg_id:
            seen_msg_ids.add(msg_id)
        gt_uuids.append(event_uuid)

    exp_uuids = [
        e.get("message", {}).get("id") for e in exp_msg_entries if e.get("uuid")
    ]

    mismatches = []
    num_out_of_order = 0
    num_missing = 0
    exp_uuid_set = set(exp_uuids)

    exp_idx = 0
    for gt_uuid in gt_uuids:
        found = False
        for j in range(exp_idx, len(exp_uuids)):
            if exp_uuids[j] == gt_uuid:
                exp_idx = j + 1
                found = True
                break
        if not found:
            if gt_uuid in exp_uuid_set:
                reason = "message_out_of_order"
                detail = f"Message UUID {gt_uuid} exists in the experiment trace but is out of order (expected at or after index {exp_idx})."
                num_out_of_order += 1
            else:
                reason = "message_missing"
                detail = f"Message UUID {gt_uuid} is missing entirely from the experiment trace."
                num_missing += 1
            mismatches.append({"type": reason, "identifier": gt_uuid, "detail": detail})

    counts = {
        "message_missing": num_missing,
        "message_out_of_order": num_out_of_order,
    }
    return mismatches, counts


def _check_tool_calls(
    gt_entries: list[dict],
    exp_tool_calls: list[dict],
) -> tuple[list[dict], dict[str, int], int]:
    """Check for missing tool calls and result_preview mismatches.

    Returns (mismatches_list, counts_dict, num_gt_tool_calls).
    """
    gt_tool_calls = parse_tool_calls(gt_entries)
    gt_tc_map = {tc["id"]: tc for tc in gt_tool_calls if "id" in tc}
    exp_tc_map = {tc["id"]: tc for tc in exp_tool_calls if "id" in tc}

    mismatches = []
    num_missing = 0
    num_result_mismatch = 0

    for tc_id, gt_tc in gt_tc_map.items():
        tool_name = gt_tc.get("tool", "unknown_tool")
        if tc_id not in exp_tc_map:
            mismatches.append(
                {
                    "type": "tool_call_missing",
                    "identifier": tc_id,
                    "detail": f"Tool call {tc_id} ({tool_name}) is missing from the experiment tool calls.",
                }
            )
            num_missing += 1
        else:
            exp_tc = exp_tc_map[tc_id]
            gt_preview = gt_tc.get("result_preview", "")
            exp_preview = exp_tc.get("result_preview", "")
            if gt_preview != exp_preview:
                mismatches.append(
                    {
                        "type": "tool_call_result_mismatch",
                        "identifier": tc_id,
                        "detail": f"Tool call {tc_id} ({tool_name}) result preview mismatch.",
                        "Expected": f"{repr(gt_preview)}",
                        "Got": f"{repr(exp_preview)}",
                    }
                )
                num_result_mismatch += 1

    counts = {
        "tool_call_missing": num_missing,
        "tool_call_result_mismatch": num_result_mismatch,
    }
    return mismatches, counts, len(gt_tool_calls)


def _calculate_llm_time(entries: list[dict]) -> float:
    if not entries:
        return 0.0

    first_ts: float | None = None
    for entry in entries:
        ts = entry.get("timestamp")
        if ts:
            first_ts = parse_timestamp(ts)
            break
    
    last_ts: float | None = None
    for entry in entries[::-1]:
        ts = entry.get("timestamp")
        if ts:
            last_ts = parse_timestamp(ts)
            break

    if first_ts is None or last_ts is None:
        return 0.0

    total_duration = last_ts - first_ts

    tool_calls = parse_tool_calls(entries)
    tool_time = 0.0
    for tc in tool_calls:
        t1 = parse_timestamp(tc.get("timestamp"))
        t2 = parse_timestamp(tc.get("end_timestamp"))
        if t1 is not None and t2 is not None and t2 > t1:
            tool_time += t2 - t1

    return max(0.0, total_duration - tool_time)


def _get_llm_timing_metrics(gt_entries: list[dict], exp_entries: list[dict]) -> dict:
    gt_llm_time = _calculate_llm_time(gt_entries)
    exp_llm_time = _calculate_llm_time(exp_entries)

    deviation_seconds = exp_llm_time - gt_llm_time
    deviation_percentage = 0.0
    if gt_llm_time > 0:
        deviation_percentage = (deviation_seconds / gt_llm_time) * 100

    return {
        "gt_llm_time_seconds": gt_llm_time,
        "exp_llm_time_seconds": exp_llm_time,
        "deviation_seconds": deviation_seconds,
        "deviation_percentage": deviation_percentage,
    }


def verify_trace_fidelity(
    gt_trace_dir: Path | str,
    output_dir: Path | str,
    exp_tool_calls: list[dict],
) -> None:
    """Verify trace fidelity against ground truth and generate a fidelity_report.json file."""
    gt_trace_dir = Path(gt_trace_dir)
    gt_trace_file = gt_trace_dir / "trace.jsonl"
    if not gt_trace_file.exists():
        print(
            f"Warning: Ground truth trace file not found at {gt_trace_file}. Skipping fidelity check."
        )
        return

    output_dir = Path(output_dir)
    exp_trace_file = output_dir / "trace.jsonl"
    if not exp_trace_file.exists():
        print(
            f"Warning: Experiment trace file not found at {exp_trace_file}. Skipping fidelity check."
        )
        return

    try:
        gt_entries = _read_jsonl(gt_trace_file)
        exp_entries = _read_jsonl(exp_trace_file)

        trace_mismatches, trace_counts = _check_message_sequence(
            gt_entries, exp_entries
        )
        tool_mismatches, tool_counts, num_gt_tool_calls = _check_tool_calls(
            gt_entries, exp_tool_calls
        )

        counts = {**trace_counts, **tool_counts}
        num_mismatches = len(trace_mismatches) + len(tool_mismatches)

        timing_metrics = _get_llm_timing_metrics(gt_entries, exp_entries)

        report = {
            "summary": {
                "status": "SUCCESS" if num_mismatches == 0 else "MISMATCH",
                "total_mismatches": num_mismatches,
                "counts": counts,
                "len_gt_entries": len(gt_entries),
                "len_exp_entries": len(exp_entries),
                "len_gt_tool_calls": num_gt_tool_calls,
                "len_exp_tool_calls": len(exp_tool_calls),
                "timing_metrics": timing_metrics,
            },
            "trace_mismatches": trace_mismatches,
            "tool_call_mismatches": tool_mismatches,
        }

        report_file = output_dir / "fidelity_report.json"
        with open(report_file, "w") as f:
            json.dump(report, f, indent=2)

        print("\n" + "=" * 60)
        print("Trace Fidelity Verification Summary")
        print("=" * 60)
        print(f"Status: {report['summary']['status']}")
        print(f"Total Mismatches: {report['summary']['total_mismatches']}")
        print("Breakdown by type:")
        for m_type, count in counts.items():
            print(f"  - {m_type}: {count}")

        print("\nTiming Metrics:")
        print(f"  GT LLM Time:  {timing_metrics['gt_llm_time_seconds']:.2f}s")
        print(f"  EXP LLM Time: {timing_metrics['exp_llm_time_seconds']:.2f}s")
        print(
            f"  Deviation:    {timing_metrics['deviation_seconds']:+.2f}s ({timing_metrics['deviation_percentage']:+.1f}%)"
        )

        if num_mismatches != 0:
            print("\nDetailed Mismatches:")
            for idx, m in enumerate(trace_mismatches, 1):
                print(f"{idx}. [{m['type']}] ID: {m['identifier']}")
                print(f"   Detail: {m['detail']}")
            for idx, m in enumerate(tool_mismatches, len(trace_mismatches) + 1):
                print(f"{idx}. [{m['type']}] ID: {m['identifier']}")
                print(f"   Detail: {m['detail']}")
        print(f"\nReport saved to: {report_file}")
        print("=" * 60)

    except Exception as e:
        print(f"Warning: Failed to verify trace fidelity: {e}")
