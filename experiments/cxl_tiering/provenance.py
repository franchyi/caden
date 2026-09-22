"""Validation helpers for tiering result provenance (no artifact writes)."""
import math


def configuration_order(status: dict) -> list[str]:
    """Recover chronological order, never JSON/dictionary key order.

    Missing, tied, or overlapping intervals cannot support a sequential-run
    claim. Fail rather than silently inventing an order for incomplete runs.
    """
    records = status.get("configurations", {})
    intervals = []
    for label, record in records.items():
        start, end = record.get("started_unix"), record.get("finished_unix")
        if any(isinstance(value, bool) or not isinstance(value, (float, int))
               or not math.isfinite(value) for value in (start, end)):
            raise ValueError(f"missing/invalid run interval for {label}")
        if end <= start:
            raise ValueError(f"non-positive run interval for {label}")
        intervals.append((start, end, label))
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            raise ValueError(f"overlapping run intervals: {previous[2]}, {current[2]}")
    return [label for _, _, label in intervals]
