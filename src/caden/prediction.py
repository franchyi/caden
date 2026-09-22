"""Online, past-only return-time estimation for sandbox residency policy."""

from __future__ import annotations

import collections
import math
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ReturnEstimate:
    probability_within_horizon: float
    expected_remaining_seconds: float
    sample_count: int
    survivor_count: int
    source: str


class EmpiricalReturnEstimator:
    """Small empirical conditional-CDF estimator with a smooth prior.

    Samples become visible only through :meth:`observe`, which Caden calls after
    the corresponding model response has committed. Sparse request classes fall
    back to the global history. The exponential prior avoids brittle all-zero
    or all-one probabilities before enough calls complete.
    """

    def __init__(
        self,
        *,
        minimum_class_samples: int = 3,
        maximum_samples: int = 256,
        prior_mean_seconds: float = 1.0,
        prior_weight: float = 2.0,
    ) -> None:
        if minimum_class_samples <= 0:
            raise ValueError("minimum_class_samples must be positive")
        if maximum_samples <= 0:
            raise ValueError("maximum_samples must be positive")
        if prior_mean_seconds <= 0:
            raise ValueError("prior_mean_seconds must be positive")
        if prior_weight <= 0:
            raise ValueError("prior_weight must be positive")
        self.minimum_class_samples = minimum_class_samples
        self.maximum_samples = maximum_samples
        self.prior_mean_seconds = prior_mean_seconds
        self.prior_weight = prior_weight
        self._global: collections.deque[float] = collections.deque(
            maxlen=maximum_samples
        )
        self._by_class: dict[str, collections.deque[float]] = {}
        self._lock = threading.RLock()

    def observe(self, request_class: str, duration_seconds: float) -> None:
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("observed duration must be finite and nonnegative")
        key = request_class or "default"
        with self._lock:
            self._global.append(duration_seconds)
            samples = self._by_class.setdefault(
                key, collections.deque(maxlen=self.maximum_samples)
            )
            samples.append(duration_seconds)

    def estimate(
        self,
        request_class: str,
        elapsed_seconds: float,
        horizon_seconds: float,
        *,
        request_aware: bool = True,
    ) -> ReturnEstimate:
        if elapsed_seconds < 0 or horizon_seconds < 0:
            raise ValueError("elapsed time and horizon must not be negative")
        key = request_class or "default"
        with self._lock:
            class_samples = list(self._by_class.get(key, ()))
            global_samples = list(self._global)
        if request_aware and len(class_samples) >= self.minimum_class_samples:
            samples = class_samples
            source = "class"
        elif global_samples:
            samples = global_samples
            source = (
                "global"
                if len(global_samples) >= self.minimum_class_samples
                else "warmup"
            )
        else:
            samples = []
            source = "prior"

        residuals = [
            duration - elapsed_seconds
            for duration in samples
            if duration > elapsed_seconds
        ]
        successes = sum(residual <= horizon_seconds for residual in residuals)
        prior_probability = 1.0 - math.exp(-horizon_seconds / self.prior_mean_seconds)
        denominator = len(residuals) + self.prior_weight
        probability = (successes + self.prior_weight * prior_probability) / denominator
        expected_remaining = (
            sum(residuals) + self.prior_weight * self.prior_mean_seconds
        ) / denominator
        return ReturnEstimate(
            probability_within_horizon=max(0.0, min(1.0, probability)),
            expected_remaining_seconds=max(0.0, expected_remaining),
            sample_count=len(samples),
            survivor_count=len(residuals),
            source=source,
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "global_samples": list(self._global),
                "class_samples": {
                    key: list(values) for key, values in self._by_class.items()
                },
            }
