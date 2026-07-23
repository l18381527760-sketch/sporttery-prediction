import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_historical_features


class BuildHistoricalFeaturesTest(unittest.TestCase):
    def test_load_results_keeps_only_proven_90_minute_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder)
            rows = [
                self.result("proven"),
                {
                    **self.result("legacy"),
                    "result_status": "",
                    "result_source": "",
                    "source_record_id": "",
                    "captured_at_bjt": "",
                    "score_scope": "",
                    "settlement_minutes": "",
                },
                {**self.result("ambiguous"), "match_id": ""},
            ]
            with (data_dir / "bet_results.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            with patch.object(build_historical_features, "DATA_DIR", data_dir):
                loaded = build_historical_features.load_results()

        self.assertEqual(["proven"], [row["match_id"] for row in loaded])

    @staticmethod
    def result(match_id):
        return {
            "date": "2026-07-22",
            "team_a": "A",
            "team_b": "B",
            "match_id": match_id,
            "home_goals": "1",
            "away_goals": "1",
            "result_status": "finished",
            "result_source": "sporttery",
            "source_record_id": f"record-{match_id}",
            "captured_at_bjt": "2026-07-22T12:30:00+08:00",
            "score_scope": "regular_time_90",
            "settlement_minutes": "90",
        }


if __name__ == "__main__":
    unittest.main()
