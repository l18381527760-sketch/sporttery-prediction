import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import generate_betting_plan as strategy
from import_sporttery import ZgzcwMatchParser


def config() -> dict:
    return {
        "strategy_version": "value-v2",
        "max_daily_budget": 500,
        "value_strategy": {
            "strict_until_samples": 100,
            "strict_min_probability_edge": 0.01,
            "strict_min_expected_return": 1.01,
            "strict_model_edge_weight_base": 1.0,
            "strict_model_edge_weight_max": 1.0,
            "strict_kelly_fraction": 0.125,
            "strict_max_single_stake": 50,
            "strict_single_budget_cap": 100,
            "reference_bankroll": 5000,
            "min_single_stake": 10,
            "max_single_count": 2,
            "min_combo_leg_probability": 0.40,
            "strict_min_combo_leg_edge": 0.01,
            "strict_min_combo_leg_expected_return": 1.0,
            "strict_min_combo_expected_return": 1.01,
            "combo_min_legs": 2,
            "combo_max_legs": 3,
            "three_leg_value_premium": 1.05,
            "strict_combo_stake": 20,
            "observation_count": 5,
            "calibration_prior": 100,
            "min_history_samples": 30,
        },
        "league_calibration": {
            "min_samples": 30,
            "prior_samples": 30,
            "max_adjustment": 0.05,
            "validation_fraction": 0.25,
        },
        "simulation_account": {
            "required_settled_days": 30,
            "monthly_budget_cap": 3000,
            "monthly_stop_loss": 500,
            "real_money_automation": False,
        },
    }


def prediction(match_id: str, single: bool, p_home: float = 0.60) -> dict:
    return {
        "date": "2026-07-12",
        "match_id": match_id,
        "stage": "测试联赛",
        "team_a": f"主队{match_id}",
        "team_b": f"客队{match_id}",
        "p_a": str(p_home),
        "p_draw": "0.25",
        "p_b": str(0.75 - p_home),
        "is_single_had": "true" if single else "false",
        "analysis_source": "test",
    }


def odds(*match_ids: str) -> dict:
    return {match_id: {"had": {"h": 2.0, "d": 3.2, "a": 4.0}} for match_id in match_ids}


class ValueStrategyTest(unittest.TestCase):
    def run_strategy(self, predictions: list[dict], market: dict, training_samples=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty = Path(temp_dir)
            with (
                patch.object(strategy, "OUTPUT_DIR", empty),
                patch.object(strategy, "DATA_DIR", empty),
                patch.object(strategy, "read_json", return_value=config()),
                patch.object(strategy, "load_predictions", return_value=predictions),
                patch.object(strategy, "load_odds", return_value=market),
                patch.object(strategy, "load_draw_training_samples", return_value=training_samples or []),
            ):
                return strategy.build_value_plan(date(2026, 7, 12))

    def test_real_singles_require_official_single_eligibility(self):
        predictions = [prediction("001", True), prediction("002", False)]
        plan, observations = self.run_strategy(predictions, odds("001", "002"))

        singles = [item for item in plan if not json.loads(item["legs_json"])]
        self.assertEqual(["主队001 vs 客队001"], [item["match"] for item in singles])
        self.assertEqual(2, len(observations))
        self.assertTrue(all(item["stake"] == 0 for item in observations))

    def test_combo_prefers_two_legs_without_five_percent_ev_premium(self):
        predictions = [
            prediction("001", False, 0.60),
            prediction("002", False, 0.60),
            prediction("003", False, 0.51),
        ]
        plan, _ = self.run_strategy(predictions, odds("001", "002", "003"))

        combos = [item for item in plan if json.loads(item["legs_json"])]
        self.assertEqual(1, len(combos))
        self.assertEqual(2, len(json.loads(combos[0]["legs_json"])))

    def test_zgzcw_dg_attribute_marks_single_eligibility(self):
        parser = ZgzcwMatchParser(date(2026, 7, 12))
        parser.feed(
            '<table><tr id="tr_001" t="2026-07-12 18:00" dg="1">'
            '<td class="wh-4"><a href="/soccer/team/1">主队</a></td>'
            '<td class="wh-6"><a href="/soccer/team/2">客队</a></td>'
            '</tr></table>'
        )

        self.assertEqual(1, len(parser.matches))
        self.assertTrue(parser.matches[0]["isSingleHad"])

    def test_draw_probability_is_league_calibrated_before_value_gating(self):
        start = date(2026, 1, 1)
        samples = [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "match_id": str(index),
                "stage": "测试联赛",
                "base_draw_probability": "0.20",
                "outcome": "1" if index % 5 in {0, 1} else "0",
            }
            for index in range(40)
        ]
        row = prediction("001", True, 0.30)
        row.update(p_draw="0.40", p_b="0.30")
        market = {"001": {"had": {"h": 4.0, "d": 3.0, "a": 3.5}}}

        plan, _ = self.run_strategy([row], market, samples)

        self.assertEqual(1, len(plan))
        self.assertEqual("平", plan[0]["selection"])
        self.assertAlmostEqual(0.45, plan[0]["league_calibrated_probability"])
        self.assertEqual(40, plan[0]["league_calibration_samples"])
        self.assertAlmostEqual(0.45, plan[0]["probability"])

    def test_monthly_budget_limit_blocks_money_but_keeps_observations(self):
        configured = config()
        configured["simulation_account"]["monthly_budget_cap"] = 100
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            (output / "betting_ledger.csv").write_text(
                "date,play,probability,odds,stake,status,profit\n"
                "2026-07-01,平局单场,0.4,3.0,100,未中,-100\n",
                encoding="utf-8",
            )
            with (
                patch.object(strategy, "OUTPUT_DIR", output),
                patch.object(strategy, "DATA_DIR", output),
                patch.object(strategy, "read_json", return_value=configured),
                patch.object(strategy, "load_predictions", return_value=[prediction("001", True)]),
                patch.object(strategy, "load_odds", return_value=odds("001")),
                patch.object(strategy, "load_draw_training_samples", return_value=[]),
            ):
                plan, observations = strategy.build_value_plan(date(2026, 7, 12))

        self.assertEqual([], plan)
        self.assertEqual(1, len(observations))
        self.assertEqual(0, observations[0]["stake"])


if __name__ == "__main__":
    unittest.main()
