import unittest

from draw_alert_core import (
    DrawInputs,
    MarketEvidence,
    classify_candidate,
    fair_probabilities,
    rank_candidates,
)


CFG = {
    "min_draw_probability": 0.27,
    "min_draw_edge": 0.04,
    "min_expected_value": 1.05,
    "max_xg_total": 2.50,
    "cold_favorite_probability": 0.55,
    "balanced_max_win_gap": 0.10,
    "balanced_max_xg_total": 2.35,
}


def sample(**changes):
    values = dict(
        match_id="001", team_a="A", team_b="B", stage="quarter-final",
        domestic_odds=(1.60, 4.00, 6.00), model_probabilities=(0.54, 0.32, 0.14),
        calibrated_draw_probability=0.32, xg_total=2.10, source_count=3,
        market_sources=(
            MarketEvidence("sporttery", "win_draw_loss", 90, False),
            MarketEvidence("zgzcw", "win_draw_loss", 90, False),
        ),
        market_scope="90m", favorite_movement=-0.06, regional_gap=0.07,
        underdog_win_probability=0.14, underdog_not_lose_probability=0.46,
        structural_signals=("knockout_caution", "underdog_defense"), data_quality="high",
    )
    values.update(changes)
    return DrawInputs(**values)


class DrawAlertCoreTest(unittest.TestCase):
    def test_fair_probabilities_remove_overround(self):
        fair = fair_probabilities(1.90, 3.60, 4.00)
        self.assertAlmostEqual(1.0, sum(fair), places=9)

    def test_norway_england_shape_is_cold_draw(self):
        candidate = classify_candidate(sample(), CFG)
        self.assertEqual("cold_draw", candidate.subtype)

    def test_balanced_low_goal_match_is_balanced_draw(self):
        candidate = classify_candidate(sample(
            stage="K联赛", domestic_odds=(2.70, 3.10, 2.60),
            model_probabilities=(0.33, 0.34, 0.33), calibrated_draw_probability=0.34,
            xg_total=2.05, favorite_movement=-0.01, regional_gap=0.01,
            structural_signals=("low_total", "similar_strength"),
        ), CFG)
        self.assertEqual("balanced_draw", candidate.subtype)

    def test_named_balanced_regressions_use_balanced_path(self):
        for match_id in ("jeju-daejeon", "seoul-gangwon"):
            candidate = classify_candidate(sample(
                match_id=match_id, stage="K联赛", domestic_odds=(2.70, 3.10, 2.60),
                model_probabilities=(0.33, 0.34, 0.33), calibrated_draw_probability=0.34,
                xg_total=2.05, favorite_movement=-0.01, regional_gap=0.01,
                structural_signals=("low_total", "similar_strength"),
            ), CFG)
            self.assertEqual("balanced_draw", candidate.subtype)

    def test_named_knockout_regressions_use_cold_path(self):
        for match_id in ("norway-england", "argentina-switzerland", "argentina-cape-verde", "germany-paraguay"):
            candidate = classify_candidate(sample(match_id=match_id), CFG)
            self.assertEqual("cold_draw", candidate.subtype)

    def test_favorite_risk_does_not_force_a_draw(self):
        self.assertIsNone(classify_candidate(sample(calibrated_draw_probability=0.25), CFG))

    def test_non_favorite_uneven_match_is_not_cold_draw(self):
        self.assertIsNone(classify_candidate(
            sample(domestic_odds=(1.90, 3.60, 4.00)), CFG
        ))

    def test_duplicate_market_sources_are_rejected(self):
        self.assertIsNone(classify_candidate(sample(
            source_count=99,
            market_sources=(
                MarketEvidence("sporttery", "win_draw_loss", 90, False),
                MarketEvidence("sporttery", "win_draw_loss", 90, False),
            ),
        ), CFG))

    def test_invalid_market_sources_do_not_count_toward_two_sources(self):
        self.assertIsNone(classify_candidate(sample(
            source_count=99,
            market_sources=(
                MarketEvidence("sporttery", "win_draw_loss", 90, False),
                MarketEvidence("qualification", "qualification", 90, False),
                MarketEvidence("extra-time", "win_draw_loss", 120, False),
                MarketEvidence("with-extra-time", "win_draw_loss", 90, True),
            ),
        ), CFG))

    def test_valid_market_sources_override_source_count_summary(self):
        candidate = classify_candidate(sample(source_count=0), CFG)
        self.assertEqual("cold_draw", candidate.subtype)

    def test_unknown_data_quality_is_rejected(self):
        self.assertIsNone(classify_candidate(sample(data_quality="unknown"), CFG))

    def test_non_90m_market_is_rejected(self):
        self.assertIsNone(classify_candidate(sample(market_scope="qualification"), CFG))

    def test_ranking_prefers_value_then_data_quality(self):
        low = classify_candidate(sample(match_id="low", calibrated_draw_probability=0.31), CFG)
        high = classify_candidate(sample(match_id="high", calibrated_draw_probability=0.34), CFG)
        self.assertEqual("high", rank_candidates([low, high])[0].inputs.match_id)


if __name__ == "__main__":
    unittest.main()
