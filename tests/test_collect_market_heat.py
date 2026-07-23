import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import collect_market_heat as collector
import capture_odds_snapshot as snapshot
from draw_model_learning import build_training_samples
from generate_draw_alert import generate_alerts


def fixture() -> dict:
    return {
        "match_id": "001",
        "kickoff_at": "2026-07-12T20:00:00+08:00",
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
        self.assertEqual("2026-07-12T20:00:00+08:00", evidence["kickoff_at"])
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

    def test_invalid_odds_cannot_create_a_probability_source(self):
        self.assertIsNone(collector.probability_record((float("nan"), 3.60, 4.00), None))

    def test_same_provider_multiple_snapshots_do_not_increase_source_count(self):
        market = {
            "question": "Norway vs England",
            "sportsMarketType": "moneyline",
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

    def test_polymarket_rejects_non_full_time_market_scopes(self):
        scoped_titles = (
            "Norway vs England - First Half Result",
            "Norway vs England - First-Half Result",
            "Norway vs England - 1H Result",
            "Norway vs England - Halftime Result",
            "Norway vs England - \u4e0a\u534a\u573a\u8d5b\u679c",
            "Norway vs England - First Quarter Result",
            "Norway vs England - First Period Result",
            "Norway vs England - Qualification",
            "Norway vs England - To Qualify",
            "Norway vs England - Advance",
            "Norway vs England - Including Extra Time",
            "Norway vs England - Penalties Winner",
            "Norway vs England - Total Corners",
            "Norway vs England - Total Goals",
            "Norway vs England - Match Result",
            "Who will win: Norway vs England",
        )
        for title in scoped_titles:
            with self.subTest(title=title):
                market = {
                    "question": title,
                    "outcomes": '["Norway", "Draw", "England"]',
                    "outcomePrices": '["0.20", "0.25", "0.55"]',
                }
                self.assertIsNone(collector.parse_polymarket_90m(market, "Norway", "England"))

    def test_polymarket_rejects_ambiguous_title_even_when_question_is_bare_matchup(self):
        market = {
            "title": "Norway vs England - First Half Result",
            "question": "Norway vs England",
            "outcomes": '["Norway", "Draw", "England"]',
            "outcomePrices": '["0.20", "0.25", "0.55"]',
        }

        self.assertIsNone(collector.parse_polymarket_90m(market, "Norway", "England"))

    def test_polymarket_bare_matchup_requires_moneyline_market_type(self):
        base = {
            "question": "Norway vs England",
            "outcomes": '["Norway", "Draw", "England"]',
            "outcomePrices": '["0.20", "0.25", "0.55"]',
        }
        self.assertIsNone(collector.parse_polymarket_90m(base, "Norway", "England"))
        for market_type in (
            "first_half_moneyline",
            "soccer_halftime_result",
            "soccer_extra_time",
            "soccer_team_to_advance",
            "unknown_scope",
        ):
            with self.subTest(market_type=market_type):
                market = {**base, "sportsMarketType": market_type}
                self.assertIsNone(
                    collector.parse_polymarket_90m(market, "Norway", "England")
                )

        accepted = {**base, "sportsMarketType": "moneyline"}
        self.assertIsNotNone(
            collector.parse_polymarket_90m(accepted, "Norway", "England")
        )

        conflicting = {
            **base,
            "question": "Norway vs England - Full Time Result",
            "sportsMarketType": "first_half_moneyline",
        }
        self.assertIsNone(
            collector.parse_polymarket_90m(conflicting, "Norway", "England")
        )

    def test_polymarket_accepts_only_whitelisted_full_time_match_titles(self):
        full_time_titles = (
            "Norway vs England",
            "Norway v England",
            "Norway vs England - Full Time Result",
            "Norway v England: 90 Minute Result",
        )
        for title in full_time_titles:
            with self.subTest(title=title):
                market = {
                    "question": title,
                    "sportsMarketType": "moneyline",
                    "outcomes": '["Norway", "Tie", "England"]',
                    "outcomePrices": '["0.20", "0.25", "0.55"]',
                }
                parsed = collector.parse_polymarket_90m(market, "Norway", "England")
                self.assertIsNotNone(parsed)
                self.assertEqual(90, parsed["settlement_minutes"])

        title_only_market = {
            "title": "Full Time Result: Norway vs England",
            "outcomes": '["Norway", "Draw", "England"]',
            "outcomePrices": '["0.20", "0.25", "0.55"]',
        }
        self.assertIsNotNone(
            collector.parse_polymarket_90m(title_only_market, "Norway", "England")
        )

    def test_polymarket_requires_exact_home_draw_away_structure(self):
        malformed_structures = (
            (
                '["Norway", "Draw", "England", "Other"]',
                '["0.20", "0.25", "0.55", "0.00"]',
            ),
            (
                '["Norway", "Draw", "England"]',
                '["0.20", "0.25", "0.55", "0.00"]',
            ),
            (
                '["England", "Draw", "Norway"]',
                '["0.55", "0.25", "0.20"]',
            ),
            (
                '["Norway", "England", "Draw"]',
                '["0.20", "0.55", "0.25"]',
            ),
        )
        for outcomes, prices in malformed_structures:
            with self.subTest(outcomes=outcomes, prices=prices):
                market = {
                    "question": "Norway vs England",
                    "sportsMarketType": "moneyline",
                    "outcomes": outcomes,
                    "outcomePrices": prices,
                }
                self.assertIsNone(collector.parse_polymarket_90m(market, "Norway", "England"))

    def test_ambiguous_polymarket_market_is_not_labeled_as_90_minutes(self):
        market = {
            "question": "Norway vs England - First Half Result",
            "outcomes": '["Norway", "Draw", "England"]',
            "outcomePrices": '["0.20", "0.25", "0.55"]',
        }

        evidence = collector.build_evidence(fixture(), {}, [market])

        self.assertNotIn("polymarket", evidence["sources"])
        self.assertEqual(2, evidence["source_count"])

    def test_polymarket_rejects_nonfinite_and_out_of_range_numbers(self):
        base = {
            "question": "Norway vs England",
            "sportsMarketType": "moneyline",
            "outcomes": '["Norway", "Draw", "England"]',
            "outcomePrices": '["0.20", "0.25", "0.55"]',
            "volume": "1200",
        }
        for field, value in (
            ("outcomePrices", '["NaN", "0.25", "0.55"]'),
            ("outcomePrices", '["0.20", "Infinity", "0.55"]'),
            ("outcomePrices", '["0.20", "1.01", "0.55"]'),
            ("volume", "NaN"),
            ("volume", "Infinity"),
            ("volume", "-1"),
        ):
            with self.subTest(field=field, value=value):
                market = {**base, field: value}
                self.assertIsNone(collector.parse_polymarket_90m(market, "Norway", "England"))

    def test_public_market_response_has_a_hard_read_limit(self):
        class OversizedResponse:
            def __init__(self):
                self.read_limits = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                self.read_limits.append(size)
                return b" " * (2 * 1024 * 1024 + 1)

        response = OversizedResponse()
        with patch.object(collector, "urlopen", return_value=response):
            with self.assertRaisesRegex(collector.PublicMarketError, "response too large"):
                collector.fetch_polymarket("Norway", "England")

        self.assertEqual([2 * 1024 * 1024 + 1], response.read_limits)

    def test_write_payload_records_optional_source_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "heat.json"
            collector.write_payload(path, "2026-07-12", [], ["polymarket: timeout"])

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(["polymarket: timeout"], payload["errors"])
        self.assertEqual("2026-07-12", payload["target_date"])
        self.assertIn("captured_at", payload)

    def test_write_payload_failure_preserves_previous_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "heat.json"
            path.write_text('{"previous": true}', encoding="utf-8")
            with patch("collect_market_heat.Path.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    collector.write_payload(path, "2026-07-12", [], [])

            self.assertEqual('{"previous": true}', path.read_text(encoding="utf-8"))

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

    def test_successful_empty_public_market_response_records_matching_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "fixtures.csv").write_text(
                "date,team_a,team_b,odds_a,odds_draw,odds_b,market_odds_a,market_odds_draw,market_odds_b,match_id\n"
                "2026-07-12,Norway,England,3.8,3.6,1.95,3.9,3.5,1.9,001\n",
                encoding="utf-8",
            )
            with patch.object(collector, "ROOT", root), patch.object(
                collector, "fetch_polymarket", return_value=[]
            ):
                output = collector.collect(date(2026, 7, 12))

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(["polymarket 001: no matching 90m market"], payload["errors"])

    def test_public_market_request_error_is_not_recorded_as_matching_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "fixtures.csv").write_text(
                "date,team_a,team_b,odds_a,odds_draw,odds_b,market_odds_a,market_odds_draw,market_odds_b,match_id\n"
                "2026-07-12,Norway,England,3.8,3.6,1.95,3.9,3.5,1.9,001\n",
                encoding="utf-8",
            )
            with patch.object(collector, "ROOT", root), patch.object(
                collector, "fetch_polymarket", side_effect=collector.PublicMarketError("timeout")
            ):
                output = collector.collect(date(2026, 7, 12))

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(["polymarket 001: timeout"], payload["errors"])

    def test_snapshot_retains_normalized_market_fields(self):
        source_match = {
            "homeTeam": "Norway",
            "awayTeam": "England",
            "matchNumStr": "001",
            "kickoff_at": "2026-07-12 20:00",
            "h": "3.8",
            "d": "3.6",
            "a": "1.95",
            "market_h": "2.4",
            "market_d": "3.2",
            "market_a": "3.1",
        }
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(snapshot, "SNAPSHOT_DIR", Path(folder)):
                output = snapshot.capture(
                    date(2026, 7, 12),
                    phase="decision",
                    captured_at=datetime(2026, 7, 12, 13, 30, tzinfo=timezone(timedelta(hours=8))),
                    matches=[source_match],
                    odds_by_match={},
                )

            payload = json.loads(output.read_text(encoding="utf-8"))

        match = payload["matches"][0]
        self.assertEqual("injected", payload["source"])
        self.assertEqual("3.8", match["h"])
        self.assertEqual("3.6", match["d"])
        self.assertEqual("1.95", match["a"])
        self.assertEqual("2.4", match["market_h"])
        self.assertEqual("3.2", match["market_d"])
        self.assertEqual("3.1", match["market_a"])
        self.assertEqual("win_draw_loss", match["market_type"])
        self.assertEqual(90, match["settlement_minutes"])
        self.assertIs(False, match["includes_extra_time"])
        self.assertEqual("decision", payload["capture_phase"])
        self.assertEqual("decision", match["capture_phase"])
        self.assertEqual(390, match["minutes_to_kickoff"])

    def test_snapshot_drops_matches_that_have_already_kicked_off(self):
        source_match = {
            "homeTeam": "A",
            "awayTeam": "B",
            "kickoff_at": "2026-07-12 13:00",
            "h": "2.0",
            "d": "3.2",
            "a": "4.0",
        }
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(snapshot, "SNAPSHOT_DIR", Path(folder)):
                output = snapshot.capture(
                    date(2026, 7, 12),
                    captured_at=datetime(2026, 7, 12, 13, 1, tzinfo=timezone(timedelta(hours=8))),
                    matches=[source_match],
                    odds_by_match={},
                )

        self.assertIsNone(output)

    def test_collector_payload_flows_to_verified_training_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_dir = root / "data"
            output_dir = root / "output"
            data_dir.mkdir()
            output_dir.mkdir()
            (data_dir / "fixtures.csv").write_text(
                "date,kickoff_local,stage,team_a,team_b,neutral,venue,odds_a,"
                "odds_draw,odds_b,market_odds_a,market_odds_draw,market_odds_b,"
                "analysis_source,is_single_had,match_num,match_id,pool_status\n"
                "2026-07-12,\u5468\u4e00201,quarterfinal,Norway,England,false,Test,"
                "3.8,3.6,1.95,3.9,3.5,1.9,market,true,\u5468\u4e00201,001,\n",
                encoding="utf-8",
            )
            snapshot_dir = data_dir / "odds_snapshots"
            snapshot_dir.mkdir()
            (snapshot_dir / "2026-07-12-0900.json").write_text(
                json.dumps(
                    {
                        "source": "zgzcw",
                        "matches": [
                            {
                                "team_a": "Norway",
                                "team_b": "England",
                                "match_num": "\u5468\u4e00201",
                                "kickoff_at": "2026-07-12T20:00:00+08:00",
                                "h": "3.8",
                                "d": "3.6",
                                "a": "1.95",
                                "market_h": "3.9",
                                "market_d": "3.5",
                                "market_a": "1.9",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(collector, "ROOT", root):
                market_heat_path = collector.collect(date(2026, 7, 12), offline=True)
            market_heat = json.loads(market_heat_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "2026-07-12T20:00:00+08:00",
                market_heat["matches"][0]["kickoff_at"],
            )
            (root / "betting_config.json").write_text(
                (Path(__file__).resolve().parents[1] / "betting_config.json").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            (root / "config.json").write_text(
                json.dumps({"knockout_stages": ["quarterfinal"]}), encoding="utf-8"
            )
            (output_dir / "predictions_2026-07-12.csv").write_text(
                "date,match_id,team_a,team_b,stage,xg_a,xg_b,p_a,p_draw,p_b\n"
                "2026-07-12,001,Norway,England,quarterfinal,1.0,1.0,0.20,0.60,0.20\n",
                encoding="utf-8",
            )
            (data_dir / "sporttery_odds_2026-07-12.json").write_text(
                json.dumps({"001": {"had": {"h": "3.8", "d": "3.6", "a": "1.95"}}}),
                encoding="utf-8",
            )

            generate_alerts(
                "2026-07-12",
                root,
                snapshot_time=datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc),
            )
            with (data_dir / "bet_results.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "date",
                        "match_id",
                        "team_a",
                        "team_b",
                        "home_goals",
                        "away_goals",
                        "result_status",
                        "result_source",
                        "source_record_id",
                        "captured_at_bjt",
                        "score_scope",
                        "settlement_minutes",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "date": "2026-07-12",
                        "match_id": "001",
                        "team_a": "Norway",
                        "team_b": "England",
                        "home_goals": "1",
                        "away_goals": "1",
                        "result_status": "finished",
                        "result_source": "sporttery",
                        "source_record_id": "result-001",
                        "captured_at_bjt": "2026-07-12T22:00:00+08:00",
                        "score_scope": "regular_time_90",
                        "settlement_minutes": "90",
                    }
                )

            samples = build_training_samples(root, as_of=date(2026, 7, 12))

        self.assertEqual(1, len(samples))
        self.assertEqual("2026-07-12T20:00:00+08:00", samples[0]["kickoff_at"])
        self.assertEqual("2026-07-12T19:00:00+08:00", samples[0]["captured_at"])
        self.assertEqual(1, samples[0]["outcome"])


if __name__ == "__main__":
    unittest.main()
