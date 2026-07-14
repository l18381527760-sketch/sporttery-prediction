import json
import tempfile
import unittest
from pathlib import Path

from model_metrics import snapshot_coverage, summarize


class ModelMetricsTest(unittest.TestCase):
    def test_risk_and_calibration_metrics_are_reported(self):
        rows = [
            {"date": "2026-07-01", "play": "平局单场", "stage": "联赛A", "probability": "0.60", "odds": "2", "stake": "10", "status": "命中", "profit": "10"},
            {"date": "2026-07-02", "play": "平局单场", "stage": "联赛A", "probability": "0.60", "odds": "2", "stake": "10", "status": "未中", "profit": "-10"},
            {"date": "2026-07-03", "play": "胜平负2串1", "stage": "联赛B", "probability": "0.40", "odds": "3", "stake": "10", "status": "未中", "profit": "-10"},
            {"date": "2026-07-04", "play": "胜平负2串1", "stage": "联赛B", "probability": "0.40", "odds": "3", "stake": "10", "status": "命中", "profit": "20"},
        ]

        metrics = summarize(rows)
        overall = metrics["overall"]

        self.assertEqual(20.0, overall["max_drawdown"])
        self.assertEqual(2, overall["max_losing_streak"])
        self.assertEqual(0, overall["current_losing_streak"])
        self.assertIsNotNone(overall["calibration_error"])
        self.assertEqual(2, metrics["by_play"]["平局单场"]["count"])
        self.assertEqual(2, metrics["by_play"]["胜平负串关"]["count"])
        self.assertIn("profit", metrics["by_play"]["胜平负串关"])
        self.assertEqual(metrics["by_play"], metrics["by_play_all"])

    def test_snapshot_coverage_separates_opening_decision_and_closing_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, phase in enumerate(("opening", "decision", "pre_kickoff")):
                (root / f"snapshot-{index}.json").write_text(
                    json.dumps(
                        {
                            "captured_at": f"2026-07-12T1{index}:00:00+08:00",
                            "capture_phase": "monitoring",
                            "matches": [{"capture_phase": phase}],
                        }
                    ),
                    encoding="utf-8",
                )

            coverage = snapshot_coverage(root)

        self.assertEqual(3, coverage["files"])
        self.assertEqual(1, coverage["phases"]["opening"])
        self.assertEqual(1, coverage["phases"]["decision"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff"])


if __name__ == "__main__":
    unittest.main()
