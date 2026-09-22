from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from caden.policy import DecisionEvent
from caden.types import Stage

from experiments.trajectory_replay.convert_nebius import (
    canonical_json,
    classify_operation,
    convert_row,
    wait_profile,
)
from experiments.trajectory_replay.expand_workloads import expand
from experiments.trajectory_replay.run_campaign import (
    load_workloads,
    speculation_accounting,
    workload_waves,
)
from experiments.trajectory_replay.summarize_campaigns import (
    ratio_change,
    reduction,
    validate_pair,
)


class TrajectoryConversionTest(unittest.TestCase):
    def test_classifies_supported_tools(self) -> None:
        self.assertEqual(
            classify_operation("execute_bash", {"command": "pytest -q"}), "test"
        )
        self.assertEqual(
            classify_operation("execute_bash", {"command": "grep -R value src"}),
            "search",
        )
        self.assertEqual(
            classify_operation(
                "str_replace_editor", {"command": "str_replace", "path": "x"}
            ),
            "edit",
        )
        self.assertIsNone(classify_operation("think", {"thought": "x"}))

    def test_conversion_preserves_provenance_and_caps_calls(self) -> None:
        row = {
            "trajectory_id": "trajectory-a",
            "instance_id": "repo__issue",
            "repo": "owner/repo",
            "resolved": 1,
            "exit_status": "submitted",
            "trajectory": [
                {
                    "tool_calls": [
                        {
                            "id": "one",
                            "function": {
                                "name": "execute_bash",
                                "arguments": json.dumps({"command": "pytest -q"}),
                            },
                        },
                        {
                            "id": "two",
                            "function": {
                                "name": "str_replace_editor",
                                "arguments": json.dumps(
                                    {"command": "view", "path": "/workspace"}
                                ),
                            },
                        },
                    ]
                }
            ],
        }
        workload = convert_row(row, max_tools=1, wait_ms=1000, source_revision="abc123")
        self.assertEqual(workload["tool_count"], 1)
        self.assertEqual(workload["events"][0]["type"], "wait")
        self.assertEqual(workload["events"][1]["operation"], "test")
        self.assertEqual(workload["source"]["trajectory_id"], "trajectory-a")
        self.assertFalse(workload["tool_execution"]["anonymous_memory_injection"])

    def test_speculation_accounting_charges_hold_time(self) -> None:
        events = [
            DecisionEvent(
                timestamp_ns=1_000,
                action="spec_restore",
                sandbox_id="sandbox-1",
                bytes=2_000,
            ),
            DecisionEvent(
                timestamp_ns=1_001_000,
                action="stage",
                sandbox_id="sandbox-1",
                stage=Stage.RESPONSE_WAKE,
            ),
        ]
        accounting = speculation_accounting(events)
        self.assertEqual(accounting["hold_p50_ns"], 1_000_000)
        self.assertEqual(accounting["unconsumed_events"], 0)
        self.assertEqual(accounting["prepared_byte_seconds"], 2.0)

    def test_heterogeneous_profile_has_opaque_runtime_classes(self) -> None:
        row = {
            "trajectory_id": "trajectory-profile",
            "instance_id": "repo__issue",
            "repo": "owner/repo",
            "trajectory": [
                {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "execute_bash",
                                "arguments": json.dumps({"command": "ls"}),
                            }
                        },
                        {
                            "function": {
                                "name": "execute_bash",
                                "arguments": json.dumps({"command": "git status"}),
                            }
                        },
                    ]
                }
            ],
        }
        workload = convert_row(
            row,
            max_tools=2,
            wait_ms=1000,
            wait_profile_ms=(100, 2000),
            source_revision="abc123",
        )
        waits = [event for event in workload["events"] if event["type"] == "wait"]
        self.assertEqual({event["duration_ms"] for event in waits}, {100, 2000})
        self.assertEqual(
            {event["request_class"] for event in waits},
            {"synthetic-class-0", "synthetic-class-1"},
        )
        self.assertEqual(wait_profile("100,2000"), (100, 2000))

    def test_fixed_total_workloads_are_split_into_bounded_waves(self) -> None:
        workloads = [{"id": index} for index in range(7)]
        waves = workload_waves(workloads, 3)
        self.assertEqual([len(wave) for wave in waves], [3, 3, 1])
        self.assertEqual(
            [item["id"] for wave in waves for item in wave], list(range(7))
        )
        with self.assertRaisesRegex(ValueError, "positive"):
            workload_waves(workloads, 0)

    def test_load_rejects_checksum_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "workloads").mkdir()
            workload = {"schema": "caden-tool-trajectory-v1", "events": []}
            payload = canonical_json(workload)
            path = root / "workloads" / "workload-0000.json"
            path.write_bytes(payload)
            manifest = {
                "schema": "caden-tool-trajectory-manifest-v1",
                "workloads": [
                    {
                        "path": "workloads/workload-0000.json",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
            (root / "manifest.json").write_text(json.dumps(manifest))
            loaded, _ = load_workloads(root)
            self.assertEqual(len(loaded), 1)
            path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_workloads(root)

    def test_fixed_queue_expansion_is_deterministic_and_preserves_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "workloads").mkdir(parents=True)
            events = [
                {"type": "wait", "duration_ms": 123},
                {"type": "tool", "operation": "search", "sequence": 0},
            ]
            workload = {
                "schema": "caden-tool-trajectory-v1",
                "source": {
                    "trajectory_id": "trace-a",
                    "instance_id": "instance-a",
                },
                "events": events,
                "operation_counts": {"search": 1},
                "tool_count": 1,
            }
            payload = canonical_json(workload)
            (source / "workloads" / "workload-0000.json").write_bytes(payload)
            manifest = {
                "schema": "caden-tool-trajectory-manifest-v1",
                "generated_at": "fixed",
                "source": {"dataset": "test"},
                "conversion": {},
                "workloads": [
                    {
                        "path": "workloads/workload-0000.json",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "repo": "owner/repo",
                        "tool_count": 1,
                    }
                ],
            }
            (source / "manifest.json").write_text(json.dumps(manifest))

            first = expand(source, root / "first", 3, None)
            second = expand(source, root / "second", 3, None)
            self.assertEqual(first, second)
            self.assertEqual(first["workload_count"], 3)
            self.assertEqual(first["operation_counts"], {"search": 3})
            loaded, _ = load_workloads(root / "first")
            self.assertEqual([item["events"] for item in loaded], [events] * 3)
            self.assertEqual(
                len({item["source"]["trajectory_id"] for item in loaded}), 3
            )


class TrajectorySummaryTest(unittest.TestCase):
    def test_reductions(self) -> None:
        self.assertEqual(reduction(100, 40), 0.6)
        self.assertAlmostEqual(ratio_change(100, 110), 0.1)

    def test_validates_pair_manifest(self) -> None:
        baseline = {
            "workload": {"manifest_sha256": "same"},
            "summary": {"requests": 2},
            "config": {"mode": "baseline", "policy": "static"},
            "host": {"hostname": "host"},
        }
        treatment = {
            "workload": {"manifest_sha256": "same"},
            "summary": {"requests": 2},
            "config": {"mode": "t1", "policy": "caden"},
            "host": {"hostname": "host"},
        }
        validate_pair(baseline, treatment)
        treatment["config"]["policy"] = "request-aware"
        validate_pair(baseline, treatment)
        treatment["workload"]["manifest_sha256"] = "different"
        with self.assertRaisesRegex(ValueError, "manifest"):
            validate_pair(baseline, treatment)


if __name__ == "__main__":
    unittest.main()
