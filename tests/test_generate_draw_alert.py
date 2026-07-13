import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from generate_draw_alert import FIELDS, _calibrated_probability, attach_stake, derive_structural_signals, generate_alerts, select_alerts


class GenerateDrawAlertTest(unittest.TestCase):
    def test_broken_optional_model_falls_back_to_blended_probability(self):
        with patch("generate_draw_alert.importlib.import_module", side_effect=RuntimeError("broken model")):
            self.assertEqual(0.34, _calibrated_probability({"base_draw_probability": 0.34}, 0.34))

    def test_empty_qualifying_set_writes_exact_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            (root / "data").mkdir()
            path = generate_alerts("2026-07-12", root)

            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(FIELDS, reader.fieldnames)
                self.assertEqual([], list(reader))

    def test_generation_uses_90_minute_evidence_and_main_plan_team_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            (root / "data").mkdir()
            (root / "betting_config.json").write_text(
                (Path(__file__).resolve().parents[1] / "betting_config.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "config.json").write_text(json.dumps({"knockout_stages": ["quarterfinal"]}), encoding="utf-8")
            (root / "output" / "predictions_2026-07-12.csv").write_text(
                "date,match_id,team_a,team_b,stage,xg_a,xg_b,p_a,p_draw,p_b\n"
                "2026-07-12,001,A,B,quarterfinal,1.1,1.2,0.54,0.34,0.12\n",
                encoding="utf-8",
            )
            (root / "data" / "sporttery_odds_2026-07-12.json").write_text(
                json.dumps({"001": {"had": {"h": "1.60", "d": "4.00", "a": "6.00"}}}),
                encoding="utf-8",
            )
            (root / "data" / "market_heat_2026-07-12.json").write_text(json.dumps({
                "captured_at": "2026-07-12T13:30:00+08:00",
                "matches": [{
                    "match_id": "001", "market_scope": "90m", "quality": "high",
                    "favorite_movement": -0.05, "regional_gap": 0.06,
                    "sources": {
                        "domestic_sporttery": {"market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False},
                        "professional": {"market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False},
                        "extra_time": {"market_type": "win_draw_loss", "settlement_minutes": 120, "includes_extra_time": False},
                    },
                }],
            }), encoding="utf-8")
            (root / "output" / "betting_plan_2026-07-12.csv").write_text(
                "date,team_a,team_b,selection,stake\n2026-07-12,A,B,平,100\n",
                encoding="utf-8",
            )
            (root / "output" / "draw_alert_metrics.json").write_text(
                json.dumps({"cold_draw": {"promoted": True}}), encoding="utf-8"
            )

            path = generate_alerts("2026-07-12", root)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(1, len(rows))
            self.assertEqual(("A", "B"), (rows[0]["team_a"], rows[0]["team_b"]))
            self.assertEqual("linked", rows[0]["settlement_mode"])
            self.assertEqual("0", rows[0]["additional_stake"])
            self.assertEqual("0.34", rows[0]["model_draw_probability"])
            self.assertNotIn("extra_time", json.loads(rows[0]["evidence_json"]))

    def test_structural_signals_are_deterministic_and_evidence_bound(self):
        signals = derive_structural_signals(
            "quarterfinal",
            1.10,
            1.20,
            (1.60, 4.00, 6.00),
            (0.54, 0.32, 0.14),
            0.32,
            {"knockout_stages": ["quarterfinal"]},
        )
        self.assertEqual(("knockout_caution", "low_total", "underdog_resistance"), signals)

    def test_selects_up_to_four_with_progressive_gates(self):
        candidates = [
            {"score": 0.50, "match_id": "A", "stage": "L1", "model_draw_probability": 0.34, "draw_edge": 0.08, "expected_value": 1.12},
            {"score": 0.49, "match_id": "B", "stage": "L1", "model_draw_probability": 0.33, "draw_edge": 0.07, "expected_value": 1.11},
            {"score": 0.48, "match_id": "C", "stage": "L1", "model_draw_probability": 0.32, "draw_edge": 0.07, "expected_value": 1.10},
            {"score": 0.47, "match_id": "D", "stage": "L2", "model_draw_probability": 0.33, "draw_edge": 0.07, "expected_value": 1.12},
            {"score": 0.46, "match_id": "E", "stage": "L3", "model_draw_probability": 0.34, "draw_edge": 0.08, "expected_value": 1.13},
        ]
        selected = select_alerts(candidates)
        self.assertEqual(["A", "B", "D", "E"], [row["match_id"] for row in selected])
        self.assertEqual([1, 2, 3, 4], [row["rank"] for row in selected])

    def test_fourth_alert_must_pass_fourth_gate(self):
        candidates = [{"score": 1 - index / 10, "match_id": str(index), "stage": f"L{index}", "model_draw_probability": 0.32, "draw_edge": 0.06, "expected_value": 1.10} for index in range(5)]
        self.assertEqual(3, len(select_alerts(candidates)))

    def test_overlap_reuses_main_stake_without_additional_money(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{"match_id": "001", "stake": "100", "selection": "平"}]
        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual(100, result["linked_main_stake"])
        self.assertEqual("linked", result["settlement_mode"])

    def test_overlap_without_match_id_uses_date_and_teams(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{"date": "2026-07-12", "team_a": "A", "team_b": "B", "stake": "100", "selection": "平"}]
        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual(100, result["linked_main_stake"])
        self.assertEqual("linked", result["settlement_mode"])

    def test_unpromoted_subtype_is_zero_stake_observation(self):
        result = attach_stake({"match_id": "002", "subtype": "balanced_draw"}, [], [], {"promoted": False}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual("observation", result["settlement_mode"])

    def test_alert_budget_caps_total_additional_stake_at_80(self):
        existing = [{"additional_stake": 30}, {"additional_stake": 30}]
        result = attach_stake({"match_id": "003", "subtype": "cold_draw"}, [], existing, {"promoted": True}, 500, 80, 30)
        self.assertEqual(20, result["additional_stake"])

    def test_below_minimum_remaining_capacity_is_budget_capped_observation(self):
        main = [{"stake": 495}]
        result = attach_stake({"match_id": "004", "subtype": "cold_draw"}, main, [], {"promoted": True}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual("budget_capped_observation", result["settlement_mode"])

    def test_paused_league_alert_stays_visible_as_zero_stake_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            (root / "data").mkdir()
            (root / "betting_config.json").write_text(
                (Path(__file__).resolve().parents[1] / "betting_config.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "config.json").write_text(
                json.dumps({"knockout_stages": ["quarterfinal"]}), encoding="utf-8"
            )
            (root / "output" / "predictions_2026-07-12.csv").write_text(
                "date,match_id,team_a,team_b,stage,xg_a,xg_b,p_a,p_draw,p_b\n"
                "2026-07-12,001,A,B,quarterfinal,1.1,1.2,0.54,0.34,0.12\n",
                encoding="utf-8",
            )
            (root / "data" / "sporttery_odds_2026-07-12.json").write_text(
                json.dumps({"001": {"had": {"h": "1.60", "d": "4.00", "a": "6.00"}}}),
                encoding="utf-8",
            )
            (root / "data" / "market_heat_2026-07-12.json").write_text(
                json.dumps({
                    "captured_at": "2026-07-12T13:30:00+08:00",
                    "matches": [{
                        "match_id": "001", "market_scope": "90m", "quality": "high",
                        "favorite_movement": -0.05, "regional_gap": 0.06,
                        "sources": {
                            "domestic": {"market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False},
                            "professional": {"market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False},
                        },
                    }],
                }),
                encoding="utf-8",
            )
            (root / "output" / "draw_alert_metrics.json").write_text(
                json.dumps({"cold_draw": {"promoted": True}}), encoding="utf-8"
            )
            (root / "output" / "draw_model_registry.json").write_text(
                json.dumps({"per_league": {"quarterfinal": {"paused": True}}}),
                encoding="utf-8",
            )

            path = generate_alerts("2026-07-12", root)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(1, len(rows))
            self.assertEqual("0", rows[0]["additional_stake"])
            self.assertEqual("observation", rows[0]["settlement_mode"])


if __name__ == "__main__":
    unittest.main()
