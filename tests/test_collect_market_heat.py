import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import collect_market_heat as collector
import capture_odds_snapshot as snapshot


def fixture() -> dict:
    return {
        "match_id": "001",
        "team_a": "Norway",
        "team_b": "England",
        "odds_a": "3.8",
        "odds_draw": "3.6",
        "odds_b": "1.95",
        "market_odds_a": "3.9",
        "market_odds_draw": "3.5",
        "market_odds_b": "1.90",
    }


class MarketHeatCollectorTest(unittest.TestCase):
    def test_build_evidence_keeps_sources_separate(self):
        evidence = collector.build_evidence(
            fixture(),
            {"open": (3.9, 3.7, 2.0), "latest": (3.8, 3.6, 1.95)},
            [],
        )

        self.assertEqual("90m", evidence["market_scope"])
        self.assertEqual(2, evidence["source_count"])
        self.assertIn("domestic_sporttery", evidence["sources"])
        self.assertIn("zgzcw_professional", evidence["sources"])
        self.assertTrue(
            all(item["market_type"] == "win_draw_loss" for item in evidence["sources"].values())
        )
        self.assertTrue(
            all(item["settlement_minutes"] == 90 for item in evidence["sources"].values())
        )
        self.assertTrue(
            all(item["includes_extra_time"] is False for item in evidence["sources"].values())
        )

    def test_same_provider_multiple_snapshots_do_not_increase_source_count(self):
        market = {
            "question": "Norway vs England",
            "outcomes": '["Norway", "Draw", "England"]',
            "outcomePrices": '["0.20", "0.25", "0.55"]',
            "volume": "1200",
        }

        evidence = collector.build_evidence(fixture(), {}, [market, market])

        self.assertEqual(3, evidence["source_count"])
        self.assertEqual(0.25, evidence["sources"]["polymarket"]["draw_probability"])
        self.assertEqual(0.20, evidence["sources"]["polymarket"]["home_probability"])
        self.assertEqual(0.55, evidence["sources"]["polymarket"]["away_probability"])

    def test_qualification_and_extra_time_markets_are_not_attached(self):
        qualification = {
            "question": "Will Norway qualify ahead of England?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.7", "0.3"]',
        }
        extra_time = {
            "question": "Norway vs England including extra time",
            "outcomes": '["Norway", "Draw", "England"]',
            "outcomePrices": '["0.2", "0.3", "0.5"]',
        }

        self.assertIsNone(collector.parse_polymarket_90m(qualification, "Norway", "England"))
        self.assertIsNone(collector.parse_polymarket_90m(extra_time, "Norway", "England"))

    def test_write_payload_records_optional_source_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "heat.json"
            collector.write_payload(path, "2026-07-12", [], ["polymarket: timeout"])

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(["polymarket: timeout"], payload["errors"])
        self.assertEqual("2026-07-12", payload["target_date"])
        self.assertIn("captured_at", payload)

    def test_offline_collection_skips_public_market_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / "data"
            snapshot_dir = data_dir / "odds_snapshots"
            snapshot_dir.mkdir(parents=True)
            (data_dir / "fixtures.csv").write_text(
                "date,team_a,team_b,odds_a,odds_draw,odds_b,market_odds_a,market_odds_draw,market_odds_b,match_id\n"
                "2026-07-12,Norway,England,3.8,3.6,1.95,3.9,3.5,1.9,001\n",
                encoding="utf-8",
            )
            (snapshot_dir / "2026-07-12-0900.json").write_text(
                json.dumps(
                    {
                        "source": "domestic_sporttery",
                        "matches": [{"match_num": "001", "market_h": "3.9", "market_d": "3.7", "market_a": "2.0"}],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(collector, "ROOT", root), patch.object(
                collector, "fetch_polymarket", side_effect=AssertionError("network must be skipped")
            ):
                output = collector.collect(date(2026, 7, 12), offline=True)

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(1, len(payload["matches"]))
        self.assertEqual([], payload["errors"])
        self.assertEqual(2, payload["matches"][0]["source_count"])

    def test_snapshot_retains_normalized_market_fields(self):
        source_match = {
            "homeTeam": "Norway",
            "awayTeam": "England",
            "matchNumStr": "001",
            "kickoff_at": "2026-07-12 20:00",
            "h": "3.8",
            "d": "3.6",
            "a": "1.95",
        }
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(snapshot, "SNAPSHOT_DIR", Path(folder)), patch.object(
                snapshot, "fetch_zgzcw_matches", return_value=[source_match]
            ):
                output = snapshot.capture(date(2026, 7, 12))

            payload = json.loads(output.read_text(encoding="utf-8"))

        match = payload["matches"][0]
        self.assertEqual("3.8", match["market_h"])
        self.assertEqual("3.6", match["market_d"])
        self.assertEqual("1.95", match["market_a"])
        self.assertEqual("win_draw_loss", match["market_type"])
        self.assertEqual(90, match["settlement_minutes"])
        self.assertIs(False, match["includes_extra_time"])


if __name__ == "__main__":
    unittest.main()
