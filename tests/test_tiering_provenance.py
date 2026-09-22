import unittest

from experiments.cxl_tiering.provenance import configuration_order


class TieringProvenanceTest(unittest.TestCase):
    def test_sorted_json_keys_do_not_determine_run_order(self):
        status = {"configurations": {
            "baseline": {"started_unix": 1, "finished_unix": 2},
            "crate-cxl": {"started_unix": 3, "finished_unix": 4},
            "crate-ssd": {"started_unix": 2, "finished_unix": 3},
        }}
        self.assertEqual(configuration_order(status), ["baseline", "crate-ssd", "crate-cxl"])

    def test_ambiguous_intervals_are_rejected(self):
        for configurations in (
            {"a": {}},
            {"a": {"started_unix": float("nan"), "finished_unix": 2}},
            {"a": {"started_unix": 2, "finished_unix": 1}},
            {"a": {"started_unix": 1, "finished_unix": 3},
             "b": {"started_unix": 2, "finished_unix": 4}},
        ):
            with self.subTest(configurations=configurations), self.assertRaises(ValueError):
                configuration_order({"configurations": configurations})
