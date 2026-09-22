from __future__ import annotations

import unittest

from experiments.sandboxfs_memory.summarize_campaigns import (
    reduction,
    validate_pair,
)


def campaign(mode: str, policy: str) -> dict[str, object]:
    return {
        "config": {
            "base": "bench-medium",
            "sandboxes": 8,
            "wss_mib": 512,
            "wss_pattern": "mixed",
            "wake_stride_kib": 64,
            "llm_wait_seconds": 15.0,
            "max_admissions": 4,
            "max_wakes": 2,
            "mode": mode,
            "policy": policy,
        },
        "host": {"hostname": "host-a"},
    }


class CampaignSummaryTest(unittest.TestCase):
    def test_validates_required_baseline_and_treatment(self) -> None:
        validate_pair(campaign("baseline", "static"), campaign("t1", "caden"))

    def test_rejects_working_set_mismatch(self) -> None:
        baseline = campaign("baseline", "static")
        treatment = campaign("t1", "caden")
        treatment["config"]["wss_pattern"] = "random"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "wss_pattern"):
            validate_pair(baseline, treatment)

    def test_reduction(self) -> None:
        self.assertEqual(reduction(100, 40), 0.6)
        with self.assertRaises(ValueError):
            reduction(0, 0)


if __name__ == "__main__":
    unittest.main()
