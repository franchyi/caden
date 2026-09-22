from __future__ import annotations

import unittest

from caden.prediction import EmpiricalReturnEstimator


class EmpiricalReturnEstimatorTest(unittest.TestCase):
    def test_request_classes_diverge_only_after_completed_observations(self) -> None:
        estimator = EmpiricalReturnEstimator(
            minimum_class_samples=3,
            prior_mean_seconds=2,
            prior_weight=1,
        )
        prior = estimator.estimate("fast", 0.8, 0.3)
        self.assertEqual(prior.source, "prior")

        for _ in range(6):
            estimator.observe("fast", 1.0)
            estimator.observe("slow", 10.0)
        fast = estimator.estimate("fast", 0.8, 0.3)
        slow = estimator.estimate("slow", 0.8, 0.3)

        self.assertEqual(fast.source, "class")
        self.assertEqual(slow.source, "class")
        self.assertGreater(fast.probability_within_horizon, 0.8)
        self.assertLess(slow.probability_within_horizon, 0.1)
        self.assertLess(
            fast.expected_remaining_seconds,
            slow.expected_remaining_seconds,
        )

    def test_elapsed_only_uses_global_history(self) -> None:
        estimator = EmpiricalReturnEstimator(minimum_class_samples=2)
        estimator.observe("fast", 1.0)
        estimator.observe("fast", 1.0)
        estimator.observe("slow", 10.0)
        estimator.observe("slow", 10.0)

        fast = estimator.estimate("fast", 0.5, 0.6, request_aware=False)
        slow = estimator.estimate("slow", 0.5, 0.6, request_aware=False)
        self.assertEqual(fast, slow)
        self.assertEqual(fast.source, "global")

    def test_rejects_invalid_observation(self) -> None:
        estimator = EmpiricalReturnEstimator()
        with self.assertRaises(ValueError):
            estimator.observe("default", -1)


if __name__ == "__main__":
    unittest.main()
