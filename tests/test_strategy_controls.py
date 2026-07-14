import unittest
from datetime import date, timedelta

from strategy_controls import (
    apply_league_draw_calibration,
    build_daily_decision,
    combo_leg_limit,
    fit_league_draw_calibrations,
    simulation_account_state,
)


def calibration_rows(stage: str, count: int, draw_indexes: set[int]) -> list[dict]:
    start = date(2026, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "match_id": f"{stage}-{index:03d}",
            "stage": stage,
            "base_draw_probability": "0.20",
            "outcome": "1" if index in draw_indexes else "0",
        }
        for index in range(count)
    ]


class LeagueDrawCalibrationTest(unittest.TestCase):
    def test_fewer_than_thirty_samples_stays_global(self):
        rows = calibration_rows("测试联赛", 29, set(range(0, 29, 3)))

        table = fit_league_draw_calibrations(
            rows, min_samples=30, prior_samples=30, max_adjustment=0.05
        )
        probability, state = apply_league_draw_calibration(0.28, "测试联赛", table)

        self.assertEqual(0.28, probability)
        self.assertFalse(state["enabled"])
        self.assertEqual(29, state["sample_count"])

    def test_validated_league_bias_is_shrunk_and_capped(self):
        draws = {index for index in range(40) if index % 5 in {0, 1}}
        rows = calibration_rows("测试联赛", 40, draws)

        table = fit_league_draw_calibrations(
            rows, min_samples=30, prior_samples=30, max_adjustment=0.05
        )
        probability, state = apply_league_draw_calibration(0.28, "测试联赛", table)

        self.assertTrue(state["enabled"])
        self.assertAlmostEqual(0.05, state["adjustment"])
        self.assertAlmostEqual(0.33, probability)
        self.assertLess(state["validation_brier_after"], state["validation_brier_before"])

    def test_validation_regression_disables_league_adjustment(self):
        draws = set(range(12))  # Training is draw-heavy; the ten-row holdout has no draws.
        rows = calibration_rows("测试联赛", 40, draws)

        table = fit_league_draw_calibrations(
            rows, min_samples=30, prior_samples=30, max_adjustment=0.05
        )
        probability, state = apply_league_draw_calibration(0.28, "测试联赛", table)

        self.assertFalse(state["enabled"])
        self.assertEqual(0.28, probability)
        self.assertGreater(state["validation_brier_after"], state["validation_brier_before"])

    def test_one_league_never_adjusts_another(self):
        rows = calibration_rows("甲级", 40, {index for index in range(40) if index % 5 in {0, 1}})
        table = fit_league_draw_calibrations(rows, min_samples=30)

        probability, state = apply_league_draw_calibration(0.28, "乙级", table)

        self.assertEqual(0.28, probability)
        self.assertEqual(0, state["sample_count"])


class SimulationAccountControlTest(unittest.TestCase):
    POLICY = {
        "mode": "simulation",
        "required_settled_days": 30,
        "monthly_budget_cap": 100,
        "monthly_stop_loss": 60,
        "real_money_automation": False,
    }

    def test_monthly_budget_is_a_hard_ceiling(self):
        rows = [
            {"date": "2026-07-01", "stake": "50", "profit": "10", "status": "命中"},
            {"date": "2026-07-02", "stake": "50", "profit": "-50", "status": "未中"},
            {"date": "2026-07-14", "stake": "500", "profit": "0", "status": "未结算"},
        ]

        state = simulation_account_state(rows, [], date(2026, 7, 14), self.POLICY)

        self.assertTrue(state["paused"])
        self.assertIn("monthly_budget_cap", state["pause_reasons"])
        self.assertEqual(0, state["remaining_monthly_budget"])

    def test_monthly_stop_loss_blocks_new_stakes(self):
        rows = [
            {"date": "2026-07-01", "stake": "50", "profit": "-30", "status": "未中"},
            {"date": "2026-07-02", "stake": "40", "profit": "-40", "status": "未中"},
        ]

        state = simulation_account_state(rows, [], date(2026, 7, 14), self.POLICY)

        self.assertTrue(state["paused"])
        self.assertIn("monthly_stop_loss", state["pause_reasons"])

    def test_thirty_settled_days_only_unlock_manual_review(self):
        observations = [
            {
                "date": (date(2026, 5, 1) + timedelta(days=index)).isoformat(),
                "status": "命中" if index % 3 == 0 else "未中",
            }
            for index in range(30)
        ]
        metrics = {
            "active_strategy": {"roi": None},
            "active_betting_strategy": {"roi": 0.08},
            "clv": {"average_clv": 0.02},
        }

        state = simulation_account_state(
            [], observations, date(2026, 7, 14), self.POLICY, metrics
        )

        self.assertEqual(30, state["completed_days"])
        self.assertTrue(state["review_ready"])
        self.assertFalse(state["real_money_automation"])
        self.assertEqual("simulation", state["mode"])

    def test_no_bet_decision_is_written_as_a_real_decision(self):
        state = simulation_account_state([], [], date(2026, 7, 14), self.POLICY)

        decision = build_daily_decision(
            [], [{"stake": 0}], date(2026, 7, 14), 12, state
        )

        self.assertEqual("no_bet", decision["status"])
        self.assertEqual(0, decision["simulated_stake"])
        self.assertEqual(12, decision["matches_reviewed"])
        self.assertIn("概率优势", decision["reason"])
        self.assertEqual("regression_only", decision["case_study_policy"])

    def test_parlay_is_two_legs_until_thirty_days_and_never_exceeds_three(self):
        policy = {"combo_max_legs": 4, "three_leg_min_settled_days": 30}

        self.assertEqual(2, combo_leg_limit(policy, 29))
        self.assertEqual(3, combo_leg_limit(policy, 30))


if __name__ == "__main__":
    unittest.main()
