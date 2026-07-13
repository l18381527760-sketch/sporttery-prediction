import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import joblib

from draw_model_learning import (
    FEATURES,
    SMALL_SAMPLE_FEATURES,
    _atomic_dump_artifact,
    _train_artifact,
    build_training_samples,
    chronological_splits,
    league_pause_states,
    main,
    predict_draw_probability,
    promotion_decision,
    rollback_decision,
    update_draw_model,
)


class FixedProbabilityModel:
    def __init__(self, probability):
        self.probability = probability

    def predict_proba(self, rows):
        return [[1.0 - self.probability, self.probability] for _ in rows]


class DrawModelLearningTest(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp_directory.name)
        (self.temp_root / "data" / "models").mkdir(parents=True)
        (self.temp_root / "output").mkdir()

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_every_training_date_precedes_validation_date(self):
        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(30)]
        for train, validation in chronological_splits(dates, n_splits=3):
            self.assertLess(
                max(dates[index] for index in train),
                min(dates[index] for index in validation),
            )

    def test_challenger_cannot_promote_before_four_weeks(self):
        challenger = self._promotion_metrics(shadow_days=27)
        self.assertFalse(promotion_decision(challenger, {"max_drawdown": 90}))

    def test_all_gates_allow_promotion(self):
        challenger = self._promotion_metrics(shadow_days=28)
        self.assertTrue(promotion_decision(challenger, {"max_drawdown": 90}))

    def test_missing_champion_returns_existing_blended_probability(self):
        probability = predict_draw_probability(
            {"base_draw_probability": 0.31}, root=self.temp_root
        )
        self.assertEqual(0.31, probability)

    def test_prediction_uses_artifact_feature_order_and_clamps_output(self):
        self._write_champion(0.99, SMALL_SAMPLE_FEATURES)
        features = {name: 0.2 for name in FEATURES}
        features["base_draw_probability"] = 0.31
        self.assertEqual(0.70, predict_draw_probability(features, root=self.temp_root))

        self._write_champion(0.001, SMALL_SAMPLE_FEATURES)
        self.assertEqual(0.03, predict_draw_probability(features, root=self.temp_root))

    def test_rollback_when_recent_brier_or_log_loss_worsens_two_percent(self):
        self.assertTrue(
            rollback_decision(
                {"brier": 0.204, "log_loss": 0.60},
                {"brier": 0.20, "log_loss": 0.60},
            )
        )

    def test_only_underperforming_league_is_paused(self):
        rows = self._league_rows("L1", negative_roi=True, worsening=True)
        rows += self._league_rows("L2", negative_roi=False, worsening=False)
        states = league_pause_states(rows)
        self.assertTrue(states["L1"]["paused"])
        self.assertFalse(states["L2"]["paused"])

    def test_training_samples_use_exact_prematch_join_and_90_minute_result(self):
        prediction_path = self.temp_root / "output" / "predictions_2026-01-02.csv"
        self._write_csv(
            prediction_path,
            [
                self._prediction("2026-01-02", "1", "A", "B"),
                self._prediction("2026-01-02", "2", "B", "A"),
                self._prediction("2026-01-03", "3", "C", "D"),
            ],
        )
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [
                {
                    "date": "2026-01-02",
                    "team_a": "A",
                    "team_b": "B",
                    "home_goals": "1",
                    "away_goals": "1",
                    "half_home_goals": "0",
                    "half_away_goals": "1",
                    "post_match_xg": "9.9",
                },
                {
                    "date": "2026-01-03",
                    "team_a": "C",
                    "team_b": "D",
                    "home_goals": "2",
                    "away_goals": "0",
                },
            ],
        )
        (self.temp_root / "data" / "sporttery_odds_2026-01-02.json").write_text(
            json.dumps({"1": {"had": {"h": "2.00", "d": "4.00", "a": "4.00"}}}),
            encoding="utf-8",
        )

        rows = build_training_samples(self.temp_root, as_of=date(2026, 1, 2))

        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["outcome"])
        self.assertAlmostEqual(0.25, rows[0]["market_draw_probability"])
        self.assertEqual(0.32, rows[0]["base_draw_probability"])
        self.assertNotIn("half_home_goals", rows[0])
        self.assertNotIn("post_match_xg", rows[0])

    def test_small_sample_selects_two_feature_sigmoid_calibrator(self):
        artifact = _train_artifact(self._samples(40), as_of=date(2026, 2, 1))
        self.assertEqual("sigmoid_calibrator", artifact["metadata"]["model_kind"])
        self.assertEqual(SMALL_SAMPLE_FEATURES, artifact["feature_order"])

    def test_two_hundred_samples_select_full_feature_pipeline(self):
        artifact = _train_artifact(self._samples(200), as_of=date(2026, 8, 1))
        self.assertEqual("full_feature_logistic", artifact["metadata"]["model_kind"])
        self.assertEqual(FEATURES, artifact["feature_order"])
        self.assertEqual(["standardscaler", "logisticregression"], list(artifact["model"].named_steps))

    def test_atomic_model_failure_preserves_existing_champion(self):
        champion = self.temp_root / "data" / "models" / "draw_champion.joblib"
        champion.write_bytes(b"known champion")
        with patch("draw_model_learning.joblib.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                _atomic_dump_artifact({"model": "new"}, champion)
        self.assertEqual(b"known champion", champion.read_bytes())

    def test_recent_update_is_skipped_but_force_starts_a_challenger(self):
        self._write_registry({"last_training_date": "2026-07-10"})
        samples = self._samples(40)
        with patch("draw_model_learning.build_training_samples", return_value=samples), patch(
            "draw_model_learning._train_artifact", wraps=_train_artifact
        ) as trainer:
            update_draw_model(self.temp_root, as_of=date(2026, 7, 12))
            self.assertEqual(0, trainer.call_count)
            update_draw_model(self.temp_root, as_of=date(2026, 7, 12), force_train=True)
            self.assertEqual(1, trainer.call_count)

        registry = self._read_registry()
        self.assertIsNotNone(registry["challenger"])
        self.assertEqual("2026-07-12", registry["last_training_date"])

    def test_active_challenger_is_not_replaced_even_when_forced(self):
        challenger_path = self.temp_root / "data" / "models" / "draw_challenger.joblib"
        artifact = self._artifact(0.30, SMALL_SAMPLE_FEATURES, "fixed-v1")
        joblib.dump(artifact, challenger_path)
        self._write_registry(
            {
                "challenger": {
                    "version": "fixed-v1",
                    "artifact": "data/models/draw_challenger.joblib",
                    "created_on": "2026-06-20",
                    "shadow_days": 0,
                },
                "last_training_date": "2026-06-20",
            }
        )

        with patch("draw_model_learning.build_training_samples", return_value=self._samples(40)), patch(
            "draw_model_learning._train_artifact"
        ) as trainer:
            update_draw_model(self.temp_root, as_of=date(2026, 7, 12), force_train=True)

        registry = self._read_registry()
        trainer.assert_not_called()
        self.assertEqual("fixed-v1", registry["challenger"]["version"])
        self.assertEqual(22, registry["challenger"]["shadow_days"])

    def test_challenger_shadow_metrics_exclude_its_training_history(self):
        challenger_path = self.temp_root / "data" / "models" / "draw_challenger.joblib"
        artifact = self._artifact(0.30, SMALL_SAMPLE_FEATURES, "fixed-v1")
        joblib.dump(artifact, challenger_path)
        self._write_registry(
            {
                "challenger": {
                    "version": "fixed-v1",
                    "artifact": "data/models/draw_challenger.joblib",
                    "created_on": "2025-01-20",
                    "shadow_days": 0,
                },
                "last_training_date": "2025-01-20",
            }
        )

        with patch("draw_model_learning.build_training_samples", return_value=self._samples(40)):
            update_draw_model(self.temp_root, as_of=date(2025, 2, 10))

        registry = self._read_registry()
        self.assertEqual(20, registry["challenger"]["sample_count"])

    def test_training_failure_records_error_without_changing_champion(self):
        self._write_champion(0.30, SMALL_SAMPLE_FEATURES)
        champion = self.temp_root / "data" / "models" / "draw_champion.joblib"
        before = champion.read_bytes()
        with patch("draw_model_learning.build_training_samples", return_value=self._samples(40)), patch(
            "draw_model_learning._train_artifact", side_effect=RuntimeError("fit failed")
        ):
            path = update_draw_model(
                self.temp_root, as_of=date(2026, 7, 12), force_train=True
            )

        self.assertEqual(self.temp_root / "output" / "draw_model_registry.json", path)
        self.assertEqual(before, champion.read_bytes())
        self.assertIn("fit failed", self._read_registry()["last_training_error"])

    def test_promotion_retains_previous_champion_artifact_and_metadata(self):
        champion_path = self.temp_root / "data" / "models" / "draw_champion.joblib"
        challenger_path = self.temp_root / "data" / "models" / "draw_challenger.joblib"
        joblib.dump(self._artifact(0.40, SMALL_SAMPLE_FEATURES, "champion-v1"), champion_path)
        joblib.dump(self._artifact(0.30, SMALL_SAMPLE_FEATURES, "challenger-v2"), challenger_path)
        self._write_registry(
            {
                "champion": {
                    **self._model_registry_entry("champion-v1", "draw_champion.joblib"),
                    "max_drawdown": 90,
                },
                "challenger": {
                    **self._model_registry_entry("challenger-v2", "draw_challenger.joblib"),
                    "created_on": "2025-01-01",
                    "shadow_days": 0,
                },
                "last_training_date": "2025-01-01",
            }
        )

        with patch("draw_model_learning.build_training_samples", return_value=self._samples(40)), patch(
            "draw_model_learning.promotion_decision", return_value=True
        ):
            update_draw_model(self.temp_root, as_of=date(2025, 1, 29))

        registry = self._read_registry()
        self.assertEqual("challenger-v2", registry["champion"]["version"])
        self.assertEqual("champion-v1", registry["previous_champion"]["version"])
        self.assertIsNone(registry["challenger"])
        previous = joblib.load(
            self.temp_root / registry["previous_champion"]["artifact"]
        )
        self.assertEqual(0.40, previous["model"].probability)

    def test_update_rolls_back_to_previous_champion_on_latest_fifty(self):
        champion_path = self.temp_root / "data" / "models" / "draw_champion.joblib"
        previous_path = self.temp_root / "data" / "models" / "draw_previous_champion.joblib"
        joblib.dump(self._artifact(0.90, SMALL_SAMPLE_FEATURES, "bad-v2"), champion_path)
        joblib.dump(self._artifact(0.05, SMALL_SAMPLE_FEATURES, "good-v1"), previous_path)
        self._write_registry(
            {
                "champion": self._model_registry_entry("bad-v2", "draw_champion.joblib"),
                "previous_champion": self._model_registry_entry(
                    "good-v1", "draw_previous_champion.joblib"
                ),
                "challenger": {"version": "shadow", "created_on": "2026-07-01", "shadow_days": 0},
                "last_training_date": "2026-07-01",
            }
        )
        samples = self._samples(50, all_losses=True)

        with patch("draw_model_learning.build_training_samples", return_value=samples):
            update_draw_model(self.temp_root, as_of=date(2026, 7, 12))

        registry = self._read_registry()
        self.assertEqual("good-v1", registry["champion"]["version"])
        self.assertEqual("bad-v2", registry["previous_champion"]["version"])
        self.assertEqual("rollback", registry["last_model_event"]["type"])
        restored = joblib.load(champion_path)
        self.assertEqual(0.05, restored["model"].probability)

    def test_cli_returns_zero_for_recorded_training_failure_and_one_for_unrecoverable_error(self):
        with patch("draw_model_learning.update_draw_model", return_value=Path("registry.json")):
            self.assertEqual(0, main(["--train", "--date", "2026-07-12", "--force"]))
        with patch("draw_model_learning.update_draw_model", side_effect=OSError("registry unavailable")):
            self.assertEqual(1, main(["--train"]))

    def _write_champion(self, probability, feature_order):
        path = self.temp_root / "data" / "models" / "draw_champion.joblib"
        joblib.dump(self._artifact(probability, feature_order, "champion-v1"), path)
        self._write_registry(
            {"champion": self._model_registry_entry("champion-v1", "draw_champion.joblib")}
        )

    def _artifact(self, probability, feature_order, version):
        return {
            "artifact_schema_version": 1,
            "feature_order": list(feature_order),
            "metadata": {"version": version, "model_kind": "test"},
            "model": FixedProbabilityModel(probability),
        }

    @staticmethod
    def _model_registry_entry(version, filename):
        return {
            "version": version,
            "artifact": f"data/models/{filename}",
            "feature_order": list(SMALL_SAMPLE_FEATURES),
        }

    def _write_registry(self, updates):
        registry = {
            "schema_version": 1,
            "champion": None,
            "challenger": None,
            "previous_champion": None,
            "per_league": {},
            "last_training_date": None,
            "last_training_error": None,
        }
        registry.update(updates)
        (self.temp_root / "output" / "draw_model_registry.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )

    def _read_registry(self):
        return json.loads(
            (self.temp_root / "output" / "draw_model_registry.json").read_text(
                encoding="utf-8"
            )
        )

    @staticmethod
    def _promotion_metrics(shadow_days):
        return {
            "shadow_days": shadow_days,
            "sample_count": 250,
            "bet_count": 120,
            "brier_improvement": 0.03,
            "brier_skill": 0.02,
            "clv": 0.01,
            "roi": 0.02,
            "max_drawdown": 80,
        }

    @staticmethod
    def _prediction(target_date, match_id, team_a, team_b):
        return {
            "date": target_date,
            "match_id": match_id,
            "team_a": team_a,
            "team_b": team_b,
            "stage": "L1",
            "xg_a": "1.2",
            "xg_b": "1.1",
            "p_a": "0.45",
            "p_draw": "0.32",
            "p_b": "0.23",
        }

    @staticmethod
    def _write_csv(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _samples(count, all_losses=False):
        start = date(2025, 1, 1)
        rows = []
        for index in range(count):
            outcome = 0 if all_losses else index % 3 == 0
            base = 0.24 + (index % 8) * 0.01
            row = {
                "date": (start + timedelta(days=index)).isoformat(),
                "match_id": str(index),
                "team_a": f"A{index}",
                "team_b": f"B{index}",
                "stage": "L1" if index % 2 else "L2",
                "outcome": outcome,
                "base_draw_probability": base,
                "market_draw_probability": base + 0.01,
                "favorite_probability": 0.50,
                "win_probability_gap": 0.10,
                "xg_total": 2.30,
                "favorite_movement": -0.02,
                "regional_gap": 0.03,
                "source_count": 2,
                "is_knockout": 0,
                "is_balanced": 1,
            }
            rows.append(row)
        return rows

    @staticmethod
    def _league_rows(stage, negative_roi, worsening):
        rows = []
        for index in range(30):
            recent = index >= 20
            probability = 0.90 if worsening and recent else 0.10
            stake = 10
            profit = -1 if negative_roi else 1
            rows.append(
                {
                    "stage": stage,
                    "outcome": 0,
                    "model_draw_probability": probability,
                    "hypothetical_stake": stake,
                    "hypothetical_profit": profit,
                }
            )
        return rows


if __name__ == "__main__":
    unittest.main()
