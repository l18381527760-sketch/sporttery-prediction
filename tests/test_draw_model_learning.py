import csv
import hashlib
import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from draw_model_learning import (
    FEATURES,
    SMALL_SAMPLE_FEATURES,
    _advance_challenger,
    _atomic_dump_artifact,
    _atomic_write_json,
    _promote_challenger,
    _role_is_valid,
    _rollback_if_needed,
    _shadow_metrics,
    _train_artifact,
    _validate_artifact,
    build_training_samples,
    chronological_splits,
    league_pause_states,
    main,
    predict_draw_probability,
    promotion_decision,
    rollback_decision,
    update_draw_model,
)


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

    def test_chronological_splits_keep_each_match_day_on_one_side(self):
        dates = [
            date(2026, 1, 1),
            date(2026, 1, 1),
            date(2026, 1, 2),
            date(2026, 1, 2),
            date(2026, 1, 3),
            date(2026, 1, 3),
            date(2026, 1, 4),
            date(2026, 1, 4),
        ]

        splits = list(chronological_splits(dates, n_splits=3))

        self.assertEqual(3, len(splits))
        for train, validation in splits:
            train_dates = {dates[index] for index in train}
            validation_dates = {dates[index] for index in validation}
            self.assertTrue(train_dates)
            self.assertTrue(validation_dates)
            self.assertLess(max(train_dates), min(validation_dates))
            self.assertFalse(train_dates & validation_dates)

    def test_chronological_splits_reduce_folds_for_repeated_dates(self):
        dates = [
            date(2026, 1, 1),
            date(2026, 1, 1),
            date(2026, 1, 2),
            date(2026, 1, 2),
        ]

        splits = list(chronological_splits(dates, n_splits=3))

        self.assertEqual(1, len(splits))
        train, validation = splits[0]
        self.assertLess(max(dates[index] for index in train), min(dates[index] for index in validation))

    def test_chronological_splits_return_no_folds_with_one_unique_date(self):
        dates = [date(2026, 1, 1)] * 3

        self.assertEqual([], list(chronological_splits(dates, n_splits=2)))

    def test_challenger_cannot_promote_before_four_weeks(self):
        challenger = self._promotion_metrics(shadow_days=27)
        self.assertFalse(promotion_decision(challenger, {"max_drawdown": 90}))

    def test_all_gates_allow_promotion(self):
        challenger = self._promotion_metrics(shadow_days=28)
        self.assertTrue(promotion_decision(challenger, {"max_drawdown": 90}))

    def test_log_loss_improvement_is_a_required_promotion_gate(self):
        challenger = self._promotion_metrics(shadow_days=28)
        challenger.pop("log_loss_improvement")
        self.assertFalse(promotion_decision(challenger, {"max_drawdown": 90}))

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

    def test_prediction_rejects_missing_artifact_digest_before_joblib_load(self):
        self._write_champion(0.66, SMALL_SAMPLE_FEATURES)
        registry = self._read_registry()
        registry["champion"].pop("artifact_sha256")
        self._write_registry(registry, populate_digests=False)

        with patch("draw_model_learning.joblib.load") as loader:
            self.assertEqual(
                0.31,
                predict_draw_probability(
                    self._feature_values(0.31, 0.30), root=self.temp_root
                ),
            )
        loader.assert_not_called()

    def test_prediction_rejects_mismatched_artifact_digest_before_joblib_load(self):
        self._write_champion(0.66, SMALL_SAMPLE_FEATURES)
        registry = self._read_registry()
        registry["champion"]["artifact_sha256"] = "0" * 64
        self._write_registry(registry, populate_digests=False)

        with patch("draw_model_learning.joblib.load") as loader:
            self.assertEqual(
                0.31,
                predict_draw_probability(
                    self._feature_values(0.31, 0.30), root=self.temp_root
                ),
            )
        loader.assert_not_called()

    def test_prediction_rejects_tampered_valid_artifact_before_joblib_load(self):
        self._write_champion(0.66, SMALL_SAMPLE_FEATURES)
        registry = self._read_registry()
        artifact_path = self.temp_root / registry["champion"]["artifact"]
        artifact_path.write_bytes(artifact_path.read_bytes() + b"tampered")

        self.assertEqual(
            0.31,
            predict_draw_probability(self._feature_values(0.31, 0.30), root=self.temp_root),
        )

    def test_prediction_rejects_malformed_artifact_digest(self):
        self._write_champion(0.66, SMALL_SAMPLE_FEATURES)
        registry = self._read_registry()
        registry["champion"]["artifact_sha256"] = "not-a-sha256"
        self._write_registry(registry, populate_digests=False)

        with patch("draw_model_learning.joblib.load") as loader:
            self.assertEqual(
                0.31,
                predict_draw_probability(
                    self._feature_values(0.31, 0.30), root=self.temp_root
                ),
            )
        loader.assert_not_called()

    def test_every_registry_role_checks_digest_before_joblib_load(self):
        self._write_champion(0.66, SMALL_SAMPLE_FEATURES)
        original = self._read_registry()["champion"]

        for role in ("champion", "challenger", "previous_champion"):
            for digest in (None, "0" * 64):
                with self.subTest(role=role, digest=digest):
                    entry = dict(original)
                    if digest is None:
                        entry.pop("artifact_sha256")
                    else:
                        entry["artifact_sha256"] = digest
                    with patch("draw_model_learning.joblib.load") as loader:
                        self.assertFalse(
                            _role_is_valid(
                                self.temp_root,
                                entry,
                                role,
                                date(2026, 7, 12),
                            )
                        )
                    loader.assert_not_called()

    def test_full_feature_champion_fails_closed_when_required_feature_is_missing(self):
        artifact = self._artifact(0.66, FEATURES, "full-v1")
        self._install_artifact(artifact, "full-v1.joblib", role="champion")
        features = self._feature_values()
        features.pop("is_balanced")
        self.assertEqual(
            features["base_draw_probability"],
            predict_draw_probability(features, root=self.temp_root),
        )

    def test_registry_artifact_path_rejects_absolute_and_parent_escape(self):
        outside = self.temp_root / "outside.joblib"
        joblib.dump(self._artifact(0.66, SMALL_SAMPLE_FEATURES, "outside-v1"), outside)
        for artifact_path in (str(outside), "data/models/../../outside.joblib"):
            with self.subTest(artifact_path=artifact_path):
                self._write_registry({
                    "champion": {
                        **self._model_registry_entry("outside-v1", artifact_path),
                        "artifact": artifact_path,
                    }
                })
                self.assertEqual(
                    0.31,
                    predict_draw_probability(
                        {"base_draw_probability": 0.31, "market_draw_probability": 0.30},
                        root=self.temp_root,
                    ),
                )

    def test_artifact_validation_rejects_order_kind_count_and_version_mismatch(self):
        cases = []
        wrong_order = self._artifact(0.66, list(reversed(SMALL_SAMPLE_FEATURES)), "wrong-order")
        cases.append((wrong_order, "wrong-order"))
        wrong_kind = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "wrong-kind")
        wrong_kind["metadata"]["model_kind"] = "full_feature_logistic"
        cases.append((wrong_kind, "wrong-kind"))
        wrong_count = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "wrong-count")
        wrong_count["model"].n_features_in_ = 10
        cases.append((wrong_count, "wrong-count"))
        version_mismatch = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "artifact-version")
        cases.append((version_mismatch, "registry-version"))

        for artifact, registry_version in cases:
            with self.subTest(version=registry_version):
                self._install_artifact(
                    artifact,
                    f"{registry_version}.joblib",
                    role="champion",
                    registry_version=registry_version,
                )
                self.assertEqual(
                    0.31,
                    predict_draw_probability(
                        {"base_draw_probability": 0.31, "market_draw_probability": 0.30},
                        root=self.temp_root,
                    ),
                )

    def test_artifact_validation_requires_exact_version_filename(self):
        artifact = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "exact-v1")
        self._install_artifact(artifact, "renamed-exact-v1.joblib", role="champion")

        self.assertEqual(
            0.31,
            predict_draw_probability(
                {"base_draw_probability": 0.31, "market_draw_probability": 0.30},
                root=self.temp_root,
            ),
        )

    def test_artifact_validation_rejects_wrong_concrete_estimators_and_parameters(self):
        cases = []
        wrong_small = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "wrong-small")
        wrong_small["model"].set_params(C=1.0)
        cases.append(wrong_small)
        wrong_full = self._artifact(0.66, FEATURES, "wrong-full")
        wrong_full["model"].set_params(standardscaler__with_mean=False)
        cases.append(wrong_full)
        wrong_solver = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "wrong-solver")
        wrong_solver["model"].set_params(solver="liblinear")
        cases.append(wrong_solver)
        wrong_penalty = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "wrong-penalty")
        wrong_penalty["model"].set_params(penalty="l1")
        cases.append(wrong_penalty)
        wrong_other = self._artifact(0.66, FEATURES, "wrong-other")
        wrong_other["model"].set_params(logisticregression__fit_intercept=False)
        cases.append(wrong_other)
        not_pipeline = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "not-pipeline")
        not_pipeline["feature_order"] = list(FEATURES)
        not_pipeline["metadata"]["model_kind"] = "full_feature_logistic"
        not_pipeline["model"].n_features_in_ = len(FEATURES)
        cases.append(not_pipeline)

        for artifact in cases:
            version = artifact["metadata"]["version"]
            with self.subTest(version=version):
                self._install_artifact(artifact, f"{version}.joblib", role="champion")
                self.assertEqual(
                    0.31,
                    predict_draw_probability(self._feature_values(0.31, 0.30), root=self.temp_root),
                )

    def test_artifact_validation_rejects_pipeline_shallow_parameter_changes(self):
        cases = {
            "verbose": {"verbose": True},
            "memory": {"memory": "pipeline-cache"},
            "transform_input": {"transform_input": ["X"]},
        }

        for version, parameters in cases.items():
            with self.subTest(parameter=version):
                artifact = self._artifact(0.66, FEATURES, f"pipeline-{version}")
                artifact["model"].set_params(**parameters)

                with self.assertRaises(ValueError):
                    _validate_artifact(artifact)

    def test_artifact_validation_rejects_nonfinite_estimator_capability(self):
        artifact = self._artifact(0.66, SMALL_SAMPLE_FEATURES, "nan-v1")
        artifact["model"].intercept_[:] = np.nan

        with self.assertRaises(ValueError):
            _validate_artifact(artifact)

    def test_corrupt_joblib_safely_falls_back(self):
        path = self.temp_root / "data" / "models" / "corrupt-v1.joblib"
        path.write_bytes(b"\x80")
        self._write_registry({
            "champion": self._model_registry_entry(
                "corrupt-v1", "data/models/corrupt-v1.joblib"
            )
        })
        self.assertEqual(
            0.31,
            predict_draw_probability(
                {"base_draw_probability": 0.31, "market_draw_probability": 0.30},
                root=self.temp_root,
            ),
        )

    def test_eoferror_while_loading_artifact_safely_falls_back(self):
        self._write_champion(0.66, SMALL_SAMPLE_FEATURES)
        with patch("draw_model_learning.joblib.load", side_effect=EOFError("truncated")):
            self.assertEqual(
                0.31,
                predict_draw_probability(
                    {"base_draw_probability": 0.31, "market_draw_probability": 0.30},
                    root=self.temp_root,
                ),
            )

    def test_unsupported_registry_schema_safely_falls_back(self):
        self._write_champion(0.66, SMALL_SAMPLE_FEATURES)
        registry = self._read_registry()
        registry["schema_version"] = 999
        (self.temp_root / "output" / "draw_model_registry.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
        self.assertEqual(
            0.31,
            predict_draw_probability(
                {"base_draw_probability": 0.31, "market_draw_probability": 0.30},
                root=self.temp_root,
            ),
        )

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

    def test_league_pause_windows_are_chronological_for_shuffled_input(self):
        ordered = self._league_rows("L1", negative_roi=True, worsening=True)
        shuffled = list(ordered)
        random.Random(42).shuffle(shuffled)
        self.assertEqual(league_pause_states(ordered), league_pause_states(shuffled))
        self.assertTrue(league_pause_states(shuffled)["L1"]["paused"])

    def test_training_samples_use_immutable_prematch_snapshot_and_exact_90_minute_result(self):
        entry_path = self._write_snapshot(
            "entry.json",
            date_value="2026-01-02",
            match_id="1",
            team_a="A",
            team_b="B",
            captured_at="2026-01-02T10:00:00+00:00",
            kickoff_at="2026-01-02T12:00:00+00:00",
            base_probability=0.32,
            market_probability=0.25,
        )
        self._write_snapshot(
            "closing.json",
            date_value="2026-01-02",
            match_id="1",
            team_a="A",
            team_b="B",
            captured_at="2026-01-02T11:55:00+00:00",
            kickoff_at="2026-01-02T12:00:00+00:00",
            base_probability=0.40,
            market_probability=0.28,
        )
        self._write_snapshot(
            "reverse.json",
            date_value="2026-01-02",
            match_id="2",
            team_a="B",
            team_b="A",
            captured_at="2026-01-02T10:00:00+00:00",
            kickoff_at="2026-01-02T12:00:00+00:00",
        )
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [
                {
                    **self._result(),
                    "half_home_goals": "0",
                    "half_away_goals": "1",
                    "post_match_xg": "9.9",
                },
            ],
        )
        self._write_csv(
            self.temp_root / "output" / "predictions_2026-01-02.csv",
            [{**self._prediction("2026-01-02", "1", "A", "B"), "p_draw": "0.99"}],
        )

        rows = build_training_samples(self.temp_root, as_of=date(2026, 1, 2))

        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["outcome"])
        self.assertAlmostEqual(0.25, rows[0]["market_draw_probability"])
        self.assertEqual(0.32, rows[0]["base_draw_probability"])
        self.assertEqual(0.28, rows[0]["closing_market_draw_probability"])
        self.assertEqual(entry_path.relative_to(self.temp_root).as_posix(), rows[0]["snapshot_path"])
        self.assertNotIn("half_home_goals", rows[0])
        self.assertNotIn("post_match_xg", rows[0])

    def test_training_matches_only_proven_results_by_canonical_match_id(self):
        self._write_snapshot("canonical", match_id="1")
        legacy = {
            **self._result("1"),
            "match_id": "",
            "result_status": "",
            "result_source": "",
            "source_record_id": "",
            "captured_at_bjt": "",
            "score_scope": "",
            "settlement_minutes": "",
        }
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [legacy, self._result("2")],
        )

        self.assertEqual(
            [], build_training_samples(self.temp_root, as_of=date(2026, 1, 2))
        )

    def test_training_skips_conflicting_results_for_one_match_id(self):
        self._write_snapshot("conflict", match_id="1")
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [
                self._result("1"),
                self._result("1", away_goals="0"),
            ],
        )

        self.assertEqual(
            [], build_training_samples(self.temp_root, as_of=date(2026, 1, 2))
        )

    def test_training_rejects_result_captured_after_beijing_as_of_cutoff(self):
        self._write_snapshot("future-result", match_id="1")
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [self._result(
                "1",
                captured_at_bjt="2026-01-03T00:00:00+08:00",
            )],
        )

        self.assertEqual(
            [],
            build_training_samples(self.temp_root, as_of=date(2026, 1, 2)),
        )

    def test_training_skips_snapshots_without_proven_prematch_timestamps(self):
        self._write_snapshot("missing.json", captured_at="", kickoff_at="2026-01-02T12:00:00Z")
        self._write_snapshot(
            "late.json",
            captured_at="2026-01-02T12:00:01Z",
            kickoff_at="2026-01-02T12:00:00Z",
        )
        self._write_snapshot("bad.json", captured_at="not-a-time", kickoff_at="also-bad")
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [self._result()],
        )

        self.assertEqual([], build_training_samples(self.temp_root, as_of=date(2026, 1, 2)))

    def test_training_skips_signed_snapshots_with_out_of_range_features(self):
        invalid_features = {
            "base_draw_probability": -0.3,
            "market_draw_probability": 1.2,
            "favorite_probability": 1.2,
            "win_probability_gap": -0.1,
            "xg_total": 10.1,
            "favorite_movement": -1.1,
            "regional_gap": 1.1,
            "source_count": 2.5,
            "is_knockout": 2,
            "is_balanced": -1,
        }
        for feature, value in invalid_features.items():
            with self.subTest(feature=feature, value=value):
                snapshot = self._write_snapshot(
                    f"invalid-{feature}",
                    feature_overrides={feature: value}
                )
                self._write_csv(
                    self.temp_root / "data" / "bet_results.csv",
                    [self._result()],
                )

                self.assertEqual(
                    [], build_training_samples(self.temp_root, as_of=date(2026, 1, 2))
                )
                snapshot.unlink()

    def test_snapshot_digest_rejects_tampering_and_arbitrary_names(self):
        valid_path = self._write_snapshot("valid")
        arbitrary = valid_path.with_name("arbitrary.json")
        arbitrary.write_bytes(valid_path.read_bytes())
        payload = json.loads(valid_path.read_text(encoding="utf-8"))
        payload["features"]["xg_total"] = 9.9
        valid_path.write_text(json.dumps(payload), encoding="utf-8")
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [self._result()],
        )

        self.assertEqual([], build_training_samples(self.temp_root, as_of=date(2026, 1, 2)))

    def test_snapshot_timestamp_prefix_must_match_captured_at(self):
        valid_path = self._write_snapshot("valid")
        renamed = valid_path.with_name(valid_path.name.replace("T100000Z-", "T100001Z-"))
        valid_path.replace(renamed)
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [self._result()],
        )

        self.assertEqual([], build_training_samples(self.temp_root, as_of=date(2026, 1, 2)))

    def test_equal_timestamp_distinct_snapshots_fail_closed(self):
        self._write_snapshot("first", base_probability=0.32)
        self._write_snapshot("retry", base_probability=0.36)
        self._write_csv(
            self.temp_root / "data" / "bet_results.csv",
            [self._result()],
        )

        self.assertEqual([], build_training_samples(self.temp_root, as_of=date(2026, 1, 2)))

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
        self.assertEqual(
            self._artifact_sha256(self.temp_root / registry["challenger"]["artifact"]),
            registry["challenger"]["artifact_sha256"],
        )
        self.assertEqual(self._simulation_policy(), registry["challenger"]["simulation_policy"])
        self.assertEqual("2026-07-12", registry["last_training_date"])

    def test_active_challenger_is_not_replaced_even_when_forced(self):
        challenger_path = self.temp_root / "data" / "models" / "fixed-v1.joblib"
        artifact = self._artifact(0.30, SMALL_SAMPLE_FEATURES, "fixed-v1")
        joblib.dump(artifact, challenger_path)
        self._write_registry(
            {
                "challenger": {
                    "version": "fixed-v1",
                    "artifact": "data/models/fixed-v1.joblib",
                    "feature_order": list(SMALL_SAMPLE_FEATURES),
                    "model_kind": "sigmoid_calibrator",
                    "created_on": "2026-06-20",
                    "simulation_policy": self._simulation_policy(),
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
        challenger_path = self.temp_root / "data" / "models" / "fixed-v1.joblib"
        artifact = self._artifact(0.30, SMALL_SAMPLE_FEATURES, "fixed-v1")
        joblib.dump(artifact, challenger_path)
        self._write_registry(
            {
                "challenger": {
                    "version": "fixed-v1",
                    "artifact": "data/models/fixed-v1.joblib",
                    "feature_order": list(SMALL_SAMPLE_FEATURES),
                    "model_kind": "sigmoid_calibrator",
                    "created_on": "2025-01-20",
                    "simulation_policy": self._simulation_policy(),
                    "shadow_days": 0,
                },
                "last_training_date": "2025-01-20",
            }
        )

        with patch("draw_model_learning.build_training_samples", return_value=self._samples(40)):
            update_draw_model(self.temp_root, as_of=date(2025, 2, 10))

        registry = self._read_registry()
        self.assertEqual(20, registry["challenger"]["sample_count"])

    def test_profitable_ordinary_ledger_cannot_supply_challenger_economics(self):
        self._write_csv(
            self.temp_root / "output" / "draw_alert_ledger.csv",
            [
                {
                    "date": "2025-02-01",
                    "stage": "L1",
                    "outcome": index % 2,
                    "model_draw_probability": 0.55,
                    "hypothetical_stake": 10,
                    "hypothetical_profit": 10,
                    "clv": 0.05,
                }
                for index in range(120)
            ],
        )
        self._install_artifact(
            self._artifact(0.40, FEATURES, "champion-v1"),
            "champion-v1.joblib",
            role="champion",
        )
        challenger = self._artifact(0.20, FEATURES, "challenger-v1")

        metrics = _shadow_metrics(
            challenger,
            self._samples(200),
            policy=self._simulation_policy(),
            reference_artifact=self._artifact(0.40, FEATURES, "reference-v1"),
        )

        self.assertEqual(0, metrics["bet_count"])
        self.assertEqual(0.0, metrics["roi"])
        self.assertIsNone(metrics["clv"])
        candidate = {**self._promotion_metrics(35), **metrics, "shadow_days": 35}
        self.assertFalse(promotion_decision(candidate, {"max_drawdown": 100}))

    def test_challenger_qualifying_bets_own_losses_roi_clv_and_drawdown(self):
        self._install_artifact(
            self._artifact(0.40, FEATURES, "champion-v1"),
            "champion-v1.joblib",
            role="champion",
        )
        challenger = self._artifact(0.60, FEATURES, "challenger-v1")
        samples = self._samples(30, all_losses=True)

        metrics = _shadow_metrics(
            challenger,
            samples,
            policy=self._simulation_policy(),
            reference_artifact=self._artifact(0.40, FEATURES, "reference-v1"),
        )

        self.assertEqual(30, metrics["bet_count"])
        self.assertEqual(-1.0, metrics["roi"])
        self.assertAlmostEqual(0.01, metrics["clv"])
        self.assertEqual(300.0, metrics["max_drawdown"])

    def test_shadow_economics_reuse_policy_frozen_at_challenger_creation(self):
        challenger = self._artifact(0.60, FEATURES, "challenger-v1")
        samples = self._samples(30, all_losses=True)
        frozen = self._simulation_policy(min_draw_probability=0.70)
        before = _shadow_metrics(challenger, samples, policy=frozen)
        (self.temp_root / "betting_config.json").write_text(
            json.dumps(
                {
                    "draw_alert": {
                        "min_draw_probability": 0.01,
                        "min_draw_edge": -1,
                        "min_expected_value": 0,
                        "max_xg_total": 99,
                        "hypothetical_stake": 1000,
                    }
                }
            ),
            encoding="utf-8",
        )
        after = _shadow_metrics(challenger, samples, policy=frozen)

        self.assertEqual(0, before["bet_count"])
        self.assertEqual(before, after)

    def test_day_28_under_minimum_evidence_keeps_same_challenger(self):
        challenger = self._artifact(0.20, FEATURES, "challenger-v1")
        challenger_path = self.temp_root / "data" / "models" / "challenger-v1.joblib"
        joblib.dump(challenger, challenger_path)
        registry = {
            "champion": None,
            "challenger": {
                **self._model_registry_entry(
                    "challenger-v1", "data/models/challenger-v1.joblib", FEATURES
                ),
                "created_on": "2025-01-01",
                "simulation_policy": self._simulation_policy(),
                "shadow_days": 0,
            },
            "previous_champion": None,
        }

        resolved = _advance_challenger(
            self.temp_root, registry, self._samples(50), date(2025, 1, 29)
        )

        self.assertFalse(resolved)
        self.assertEqual("challenger-v1", registry["challenger"]["version"])
        self.assertEqual(28, registry["challenger"]["shadow_days"])

    def test_same_challenger_promotes_later_after_real_evidence_clears_all_gates(self):
        champion = self._artifact(0.40, FEATURES, "champion-v1")
        challenger = self._base_driven_artifact("challenger-v1")
        champion_path = self.temp_root / "data" / "models" / "champion-v1.joblib"
        challenger_path = self.temp_root / "data" / "models" / "challenger-v1.joblib"
        joblib.dump(champion, champion_path)
        joblib.dump(challenger, challenger_path)
        registry = {
            "champion": {
                **self._model_registry_entry(
                    "champion-v1", "data/models/champion-v1.joblib", FEATURES
                ),
                "max_drawdown": 100,
            },
            "challenger": {
                **self._model_registry_entry(
                    "challenger-v1", "data/models/challenger-v1.joblib", FEATURES
                ),
                "created_on": "2025-01-01",
                "simulation_policy": self._simulation_policy(),
                "shadow_days": 0,
            },
            "previous_champion": None,
        }
        samples = self._promotion_samples(200)

        resolved = _advance_challenger(
            self.temp_root, registry, samples, date(2025, 8, 1)
        )

        self.assertTrue(resolved)
        self.assertEqual("challenger-v1", registry["champion"]["version"])
        self.assertEqual("champion-v1", registry["previous_champion"]["version"])
        self.assertIsNone(registry["challenger"])
        self.assertGreaterEqual(registry["champion"]["log_loss_improvement"], 0.02)

    def test_training_failure_records_error_without_changing_champion(self):
        self._write_champion(0.30, SMALL_SAMPLE_FEATURES)
        champion = self.temp_root / self._read_registry()["champion"]["artifact"]
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

    def test_invalid_challenger_is_quarantined_and_due_training_continues(self):
        artifact = self._artifact(0.30, SMALL_SAMPLE_FEATURES, "stuck-v1")
        self._install_artifact(artifact, "stuck-v1.joblib", role="challenger")
        registry = self._read_registry()
        registry["challenger"].update(
            created_on="not-a-date", simulation_policy=self._simulation_policy()
        )
        registry["last_training_date"] = "2025-01-01"
        self._write_registry(registry)

        with patch(
            "draw_model_learning.build_training_samples", return_value=self._samples(40)
        ):
            update_draw_model(
                self.temp_root,
                as_of=date(2026, 7, 12),
                force_train=True,
            )
            first = self._read_registry()
            update_draw_model(self.temp_root, as_of=date(2026, 7, 12))
            second = self._read_registry()

        self.assertIsNotNone(first["challenger"])
        self.assertNotEqual("stuck-v1", first["challenger"]["version"])
        events = [event for event in first["event_history"] if event["type"] == "role_recovery"]
        self.assertEqual(1, len(events))
        self.assertEqual(first["challenger"]["version"], second["challenger"]["version"])

    def test_future_challenger_creation_is_quarantined_once_and_replaced(self):
        cases = (
            (
                "created-on",
                {"created_on": "2026-07-13", "created_at": "2026-07-13T00:00:00+08:00"},
            ),
            (
                "created-at",
                {"created_on": "2026-07-11", "created_at": "2026-07-13T00:00:00+08:00"},
            ),
        )
        for label, creation in cases:
            with self.subTest(label=label):
                version = f"future-{label}"
                artifact = self._artifact(0.30, SMALL_SAMPLE_FEATURES, version)
                self._install_artifact(artifact, f"{version}.joblib", role="challenger")
                registry = self._read_registry()
                registry["challenger"].update(
                    **creation, simulation_policy=self._simulation_policy()
                )
                registry["last_training_date"] = "2025-01-01"
                self._write_registry(registry)

                with patch(
                    "draw_model_learning.build_training_samples",
                    return_value=self._samples(40),
                ):
                    update_draw_model(
                        self.temp_root,
                        as_of=date(2026, 7, 12),
                        force_train=True,
                    )
                    first = self._read_registry()
                    update_draw_model(self.temp_root, as_of=date(2026, 7, 12))
                    second = self._read_registry()

                self.assertNotEqual(version, first["challenger"]["version"])
                recovery_events = [
                    event
                    for event in first["event_history"]
                    if event.get("type") == "role_recovery"
                    and event.get("role") == "challenger"
                ]
                self.assertEqual(1, len(recovery_events))
                self.assertEqual(first["event_history"], second["event_history"])
                self.assertEqual(
                    first["challenger"]["version"], second["challenger"]["version"]
                )

    def test_invalid_previous_is_cleared_without_harming_champion_or_repeating(self):
        champion = self._artifact(0.30, SMALL_SAMPLE_FEATURES, "champion-v1")
        self._install_artifact(champion, "champion-v1.joblib", role="champion")
        registry = self._read_registry()
        registry["previous_champion"] = self._model_registry_entry(
            "missing-v1", "data/models/missing-v1.joblib"
        )
        registry["last_training_date"] = "2026-07-12"
        self._write_registry(registry)

        update_draw_model(self.temp_root, as_of=date(2026, 7, 12))
        first = self._read_registry()
        update_draw_model(self.temp_root, as_of=date(2026, 7, 12))
        second = self._read_registry()

        self.assertEqual("champion-v1", first["champion"]["version"])
        self.assertIsNone(first["previous_champion"])
        self.assertEqual(first["event_history"], second["event_history"])

    def test_invalid_champion_recovers_previous_or_clears_to_base(self):
        previous = self._artifact(0.30, SMALL_SAMPLE_FEATURES, "previous-v1")
        self._install_artifact(previous, "previous-v1.joblib", role="previous_champion")
        registry = self._read_registry()
        registry["champion"] = self._model_registry_entry(
            "missing-v1", "data/models/missing-v1.joblib"
        )
        registry["last_training_date"] = "2026-07-12"
        self._write_registry(registry)

        update_draw_model(self.temp_root, as_of=date(2026, 7, 12))
        recovered = self._read_registry()
        self.assertEqual("previous-v1", recovered["champion"]["version"])
        self.assertEqual(
            self._artifact_sha256(
                self.temp_root / recovered["champion"]["artifact"]
            ),
            recovered["champion"]["artifact_sha256"],
        )
        self.assertIsNone(recovered["previous_champion"])

        recovered["champion"] = self._model_registry_entry(
            "missing-v2", "data/models/missing-v2.joblib"
        )
        recovered["previous_champion"] = None
        self._write_registry(recovered)
        update_draw_model(self.temp_root, as_of=date(2026, 7, 12))
        cleared = self._read_registry()
        self.assertIsNone(cleared["champion"])
        self.assertEqual(
            0.31,
            predict_draw_probability({"base_draw_probability": 0.31}, root=self.temp_root),
        )

    def test_promotion_switches_registry_pointers_without_rewriting_artifacts(self):
        champion_path = self.temp_root / "data" / "models" / "champion-v1.joblib"
        challenger_path = self.temp_root / "data" / "models" / "challenger-v2.joblib"
        joblib.dump(self._artifact(0.40, SMALL_SAMPLE_FEATURES, "champion-v1"), champion_path)
        joblib.dump(self._artifact(0.30, SMALL_SAMPLE_FEATURES, "challenger-v2"), challenger_path)
        champion_bytes = champion_path.read_bytes()
        challenger_bytes = challenger_path.read_bytes()
        champion_entry = {
            **self._model_registry_entry(
                "champion-v1", "data/models/champion-v1.joblib"
            ),
            "max_drawdown": 90,
        }
        challenger_entry = {
            **self._model_registry_entry(
                "challenger-v2", "data/models/challenger-v2.joblib"
            ),
            "created_on": "2025-01-01",
            "simulation_policy": self._simulation_policy(),
            "shadow_days": 28,
        }
        registry = {
            "champion": champion_entry,
            "challenger": challenger_entry,
            "previous_champion": None,
        }

        _promote_challenger(
            self.temp_root,
            registry,
            joblib.load(challenger_path),
            challenger_entry,
            date(2025, 1, 29),
        )

        self.assertEqual("challenger-v2", registry["champion"]["version"])
        self.assertEqual("champion-v1", registry["previous_champion"]["version"])
        self.assertIsNone(registry["challenger"])
        self.assertEqual("data/models/challenger-v2.joblib", registry["champion"]["artifact"])
        self.assertEqual("data/models/champion-v1.joblib", registry["previous_champion"]["artifact"])
        self.assertEqual(challenger_entry["artifact_sha256"], registry["champion"]["artifact_sha256"])
        self.assertEqual(champion_entry["artifact_sha256"], registry["previous_champion"]["artifact_sha256"])
        self.assertEqual(champion_bytes, champion_path.read_bytes())
        self.assertEqual(challenger_bytes, challenger_path.read_bytes())
        self.assertFalse((self.temp_root / "data" / "models" / "draw_champion.joblib").exists())

    def test_update_rolls_back_to_previous_champion_on_latest_fifty(self):
        champion_path = self.temp_root / "data" / "models" / "bad-v2.joblib"
        previous_path = self.temp_root / "data" / "models" / "good-v1.joblib"
        joblib.dump(self._artifact(0.90, SMALL_SAMPLE_FEATURES, "bad-v2"), champion_path)
        joblib.dump(self._artifact(0.05, SMALL_SAMPLE_FEATURES, "good-v1"), previous_path)
        champion_bytes = champion_path.read_bytes()
        previous_bytes = previous_path.read_bytes()
        registry = {
            "champion": self._model_registry_entry(
                "bad-v2", "data/models/bad-v2.joblib"
            ),
            "previous_champion": self._model_registry_entry(
                "good-v1", "data/models/good-v1.joblib"
            ),
            "challenger": None,
        }
        samples = self._samples(50, all_losses=True)

        _rollback_if_needed(self.temp_root, registry, samples, date(2026, 7, 12))

        self.assertEqual("good-v1", registry["champion"]["version"])
        self.assertIsNone(registry["previous_champion"])
        self.assertEqual("rollback", registry["last_model_event"]["type"])
        self.assertEqual("data/models/good-v1.joblib", registry["champion"]["artifact"])
        self.assertEqual(
            registry["champion"]["artifact_sha256"],
            self._artifact_sha256(previous_path),
        )
        self.assertEqual("bad-v2", registry["last_model_event"]["displaced_champion"]["version"])
        self.assertEqual(champion_bytes, champion_path.read_bytes())
        self.assertEqual(previous_bytes, previous_path.read_bytes())

    def test_rollback_tradeoff_is_one_way_across_repeated_runs(self):
        current = self._artifact(0.03, SMALL_SAMPLE_FEATURES, "current-v2")
        previous = self._artifact(0.20, SMALL_SAMPLE_FEATURES, "previous-v1")
        current_path = self.temp_root / "data" / "models" / "current-v2.joblib"
        previous_path = self.temp_root / "data" / "models" / "previous-v1.joblib"
        joblib.dump(current, current_path)
        joblib.dump(previous, previous_path)
        registry = {
            "champion": self._model_registry_entry(
                "current-v2", "data/models/current-v2.joblib"
            ),
            "previous_champion": self._model_registry_entry(
                "previous-v1", "data/models/previous-v1.joblib"
            ),
            "challenger": None,
            "event_history": [],
        }
        samples = self._samples(50, all_losses=True)
        for index in range(5):
            samples[index]["outcome"] = 1

        _rollback_if_needed(self.temp_root, registry, samples, date(2026, 7, 12))
        event = registry["last_model_event"]
        self.assertLess(
            event["current_recent_50"]["brier"],
            event["previous_recent_50"]["brier"],
        )
        self.assertGreater(
            event["current_recent_50"]["log_loss"],
            event["previous_recent_50"]["log_loss"],
        )
        self.assertEqual("previous-v1", registry["champion"]["version"])
        self.assertIsNone(registry["previous_champion"])

        _rollback_if_needed(self.temp_root, registry, samples, date(2026, 7, 13))
        self.assertEqual("previous-v1", registry["champion"]["version"])
        self.assertIsNone(registry["previous_champion"])

    def test_promotion_registry_write_failure_leaves_old_pointer_and_artifact_bytes(self):
        champion_path = self.temp_root / "data" / "models" / "champion-v1.joblib"
        challenger_path = self.temp_root / "data" / "models" / "challenger-v2.joblib"
        joblib.dump(self._artifact(0.40, SMALL_SAMPLE_FEATURES, "champion-v1"), champion_path)
        joblib.dump(self._artifact(0.30, SMALL_SAMPLE_FEATURES, "challenger-v2"), challenger_path)
        old_registry = {
            "schema_version": 1,
            "champion": self._model_registry_entry(
                "champion-v1", "data/models/champion-v1.joblib"
            ),
            "challenger": {
                **self._model_registry_entry(
                    "challenger-v2", "data/models/challenger-v2.joblib"
                ),
                "created_on": "2025-01-01",
                "simulation_policy": self._simulation_policy(),
            },
            "previous_champion": None,
        }
        self._write_registry(old_registry)
        registry_bytes = (self.temp_root / "output" / "draw_model_registry.json").read_bytes()
        champion_bytes = champion_path.read_bytes()
        challenger_bytes = challenger_path.read_bytes()
        transition = json.loads(json.dumps(old_registry))
        _promote_challenger(
            self.temp_root,
            transition,
            joblib.load(challenger_path),
            transition["challenger"],
            date(2025, 2, 1),
        )

        with patch("draw_model_learning.Path.replace", side_effect=OSError("registry replace failed")):
            with self.assertRaises(OSError):
                _atomic_write_json(
                    self.temp_root / "output" / "draw_model_registry.json", transition
                )

        self.assertEqual(
            registry_bytes,
            (self.temp_root / "output" / "draw_model_registry.json").read_bytes(),
        )
        self.assertEqual(champion_bytes, champion_path.read_bytes())
        self.assertEqual(challenger_bytes, challenger_path.read_bytes())

    def test_rollback_registry_write_failure_leaves_old_pointer_and_artifact_bytes(self):
        champion_path = self.temp_root / "data" / "models" / "bad-v2.joblib"
        previous_path = self.temp_root / "data" / "models" / "good-v1.joblib"
        joblib.dump(self._artifact(0.90, SMALL_SAMPLE_FEATURES, "bad-v2"), champion_path)
        joblib.dump(self._artifact(0.05, SMALL_SAMPLE_FEATURES, "good-v1"), previous_path)
        old_registry = {
            "schema_version": 1,
            "champion": self._model_registry_entry(
                "bad-v2", "data/models/bad-v2.joblib"
            ),
            "previous_champion": self._model_registry_entry(
                "good-v1", "data/models/good-v1.joblib"
            ),
            "challenger": None,
        }
        self._write_registry(old_registry)
        registry_bytes = (self.temp_root / "output" / "draw_model_registry.json").read_bytes()
        champion_bytes = champion_path.read_bytes()
        previous_bytes = previous_path.read_bytes()
        transition = json.loads(json.dumps(old_registry))
        _rollback_if_needed(
            self.temp_root, transition, self._samples(50, all_losses=True), date(2025, 2, 1)
        )

        with patch("draw_model_learning.Path.replace", side_effect=OSError("registry replace failed")):
            with self.assertRaises(OSError):
                _atomic_write_json(
                    self.temp_root / "output" / "draw_model_registry.json", transition
                )

        self.assertEqual(
            registry_bytes,
            (self.temp_root / "output" / "draw_model_registry.json").read_bytes(),
        )
        self.assertEqual(champion_bytes, champion_path.read_bytes())
        self.assertEqual(previous_bytes, previous_path.read_bytes())

    def test_challenger_activation_failure_leaves_versioned_orphan_only(self):
        old_registry = {
            "schema_version": 1,
            "champion": None,
            "challenger": None,
            "previous_champion": None,
            "per_league": {},
            "last_training_date": None,
            "last_training_error": None,
        }
        self._write_registry(old_registry)
        registry_path = self.temp_root / "output" / "draw_model_registry.json"
        registry_bytes = registry_path.read_bytes()

        with patch("draw_model_learning.build_training_samples", return_value=self._samples(40)), patch(
            "draw_model_learning._atomic_write_json", side_effect=OSError("registry failed")
        ):
            with self.assertRaises(OSError):
                update_draw_model(
                    self.temp_root, as_of=date(2025, 3, 1), force_train=True
                )

        self.assertEqual(registry_bytes, registry_path.read_bytes())
        artifacts = list((self.temp_root / "data" / "models").glob("*.joblib"))
        self.assertEqual(1, len(artifacts))
        self.assertTrue(artifacts[0].name.startswith("draw-20250301-"))

    def test_cli_returns_zero_for_recorded_training_failure_and_one_for_unrecoverable_error(self):
        with patch("draw_model_learning.update_draw_model", return_value=Path("registry.json")):
            self.assertEqual(0, main(["--train", "--date", "2026-07-12", "--force"]))
        with patch("draw_model_learning.update_draw_model", side_effect=OSError("registry unavailable")):
            self.assertEqual(1, main(["--train"]))

    def _write_champion(self, probability, feature_order):
        artifact = self._artifact(probability, feature_order, "champion-v1")
        self._install_artifact(
            artifact,
            "champion-v1.joblib",
            role="champion",
        )

    def _artifact(self, probability, feature_order, version):
        model_kind = (
            "sigmoid_calibrator"
            if list(feature_order) == list(SMALL_SAMPLE_FEATURES)
            else "full_feature_logistic"
        )
        feature_count = len(feature_order)
        x_values = np.asarray(
            [
                [0.0] * feature_count,
                [1.0] * feature_count,
                [0.2] * feature_count,
                [0.8] * feature_count,
            ],
            dtype=float,
        )
        outcomes = np.asarray([0, 1, 0, 1], dtype=int)
        logistic = LogisticRegression(C=0.5, max_iter=1000, random_state=42)
        if model_kind == "full_feature_logistic":
            model = Pipeline(
                [("standardscaler", StandardScaler()), ("logisticregression", logistic)]
            )
            model.fit(x_values, outcomes)
            fitted_logistic = model.named_steps["logisticregression"]
        else:
            model = logistic.fit(x_values, outcomes)
            fitted_logistic = model
        fitted_logistic.coef_[:] = 0.0
        fitted_logistic.intercept_[:] = np.log(probability / (1.0 - probability))
        return {
            "artifact_schema_version": 1,
            "feature_order": list(feature_order),
            "metadata": {"version": version, "model_kind": model_kind},
            "model": model,
        }

    def _base_driven_artifact(self, version):
        artifact = self._artifact(0.30, FEATURES, version)
        model = artifact["model"]
        scaler = model.named_steps["standardscaler"]
        scaler.mean_[:] = 0.0
        scaler.scale_[:] = 1.0
        scaler.var_[:] = 1.0
        logistic = model.named_steps["logisticregression"]
        logistic.coef_[:] = 0.0
        logistic.coef_[0, 0] = 6.506
        logistic.intercept_[:] = -3.498
        return artifact

    @staticmethod
    def _simulation_policy(**overrides):
        policy = {
            "min_draw_probability": 0.27,
            "min_draw_edge": 0.04,
            "min_expected_value": 1.05,
            "max_xg_total": 2.5,
            "hypothetical_stake": 10.0,
        }
        policy.update(overrides)
        return policy

    def _model_registry_entry(self, version, artifact_path, feature_order=None):
        order = list(feature_order or SMALL_SAMPLE_FEATURES)
        entry = {
            "version": version,
            "artifact": artifact_path,
            "feature_order": order,
            "model_kind": (
                "sigmoid_calibrator"
                if order == list(SMALL_SAMPLE_FEATURES)
                else "full_feature_logistic"
            ),
        }
        path = self.temp_root / artifact_path
        if path.exists():
            entry["artifact_sha256"] = self._artifact_sha256(path)
        return entry

    def _install_artifact(
        self, artifact, filename, role="champion", registry_version=None
    ):
        path = self.temp_root / "data" / "models" / filename
        joblib.dump(artifact, path)
        metadata = artifact["metadata"]
        entry = {
            "version": registry_version or metadata["version"],
            "artifact": f"data/models/{filename}",
            "feature_order": list(artifact["feature_order"]),
            "model_kind": metadata["model_kind"],
        }
        self._write_registry({role: entry})
        return entry

    def _write_registry(self, updates, populate_digests=True):
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
        if populate_digests:
            for role in ("champion", "challenger", "previous_champion"):
                entry = registry.get(role)
                if not isinstance(entry, dict) or "artifact_sha256" in entry:
                    continue
                artifact_path = entry.get("artifact")
                if not isinstance(artifact_path, str):
                    continue
                path = self.temp_root / artifact_path
                if path.exists():
                    entry["artifact_sha256"] = self._artifact_sha256(path)
        (self.temp_root / "output" / "draw_model_registry.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )

    @staticmethod
    def _artifact_sha256(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _read_registry(self):
        return json.loads(
            (self.temp_root / "output" / "draw_model_registry.json").read_text(
                encoding="utf-8"
            )
        )

    @staticmethod
    def _feature_values(base_probability=0.32, market_probability=0.25):
        return {
            "base_draw_probability": base_probability,
            "market_draw_probability": market_probability,
            "favorite_probability": 0.54,
            "win_probability_gap": 0.42,
            "xg_total": 2.30,
            "favorite_movement": -0.05,
            "regional_gap": 0.06,
            "source_count": 2,
            "is_knockout": 1,
            "is_balanced": 0,
        }

    def _write_snapshot(
        self,
        label,
        date_value="2026-01-02",
        match_id="1",
        team_a="A",
        team_b="B",
        captured_at="2026-01-02T10:00:00Z",
        kickoff_at="2026-01-02T12:00:00Z",
        base_probability=0.32,
        market_probability=0.25,
        feature_overrides=None,
    ):
        directory = self.temp_root / "data" / "draw_feature_snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        features = self._feature_values(base_probability, market_probability)
        features.update(feature_overrides or {})
        payload = {
            "snapshot_schema_version": 1,
            "date": date_value,
            "match_id": match_id,
            "team_a": team_a,
            "team_b": team_b,
            "stage": "L1",
            "captured_at": captured_at,
            "kickoff_at": kickoff_at,
            "domestic_draw_odds": 4.0,
            "features": features,
        }
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        try:
            captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            timestamp = captured.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except ValueError:
            timestamp = "20260102T000000Z"
        path = directory / f"{timestamp}-{digest}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @staticmethod
    def _promotion_metrics(shadow_days):
        return {
            "shadow_days": shadow_days,
            "sample_count": 250,
            "bet_count": 120,
            "brier_improvement": 0.03,
            "log_loss_improvement": 0.03,
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
    def _result(match_id="1", **overrides):
        row = {
            "date": "2026-01-02",
            "match_id": match_id,
            "team_a": "A",
            "team_b": "B",
            "home_goals": "1",
            "away_goals": "1",
            "result_status": "finished",
            "result_source": "sporttery",
            "source_record_id": f"result-{match_id}",
            "captured_at_bjt": "2026-01-02T20:00:00+08:00",
            "score_scope": "regular_time_90",
            "settlement_minutes": "90",
        }
        row.update(overrides)
        return row

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
                "domestic_draw_odds": 3.0,
                "closing_market_draw_probability": base + 0.02,
                "captured_at": f"{(start + timedelta(days=index)).isoformat()}T10:00:00Z",
                "kickoff_at": f"{(start + timedelta(days=index)).isoformat()}T12:00:00Z",
            }
            rows.append(row)
        return rows

    @staticmethod
    def _promotion_samples(count):
        start = date(2025, 1, 2)
        rows = []
        for index in range(count):
            outcome = index % 2
            base = 0.60 if outcome else 0.20
            row = {
                "date": (start + timedelta(days=index)).isoformat(),
                "match_id": str(index),
                "team_a": f"A{index}",
                "team_b": f"B{index}",
                "stage": "L1",
                "outcome": outcome,
                "base_draw_probability": base,
                "market_draw_probability": 0.30,
                "favorite_probability": 0.55,
                "win_probability_gap": 0.20,
                "xg_total": 2.0,
                "favorite_movement": -0.05,
                "regional_gap": 0.06,
                "source_count": 2,
                "is_knockout": 1,
                "is_balanced": 0,
                "domestic_draw_odds": 3.0,
                "closing_market_draw_probability": 0.31,
                "captured_at": f"{(start + timedelta(days=index)).isoformat()}T10:00:00Z",
                "kickoff_at": f"{(start + timedelta(days=index)).isoformat()}T12:00:00Z",
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
                    "date": (date(2025, 1, 1) + timedelta(days=index)).isoformat(),
                    "captured_at": f"2025-01-{index + 1:02d}T10:00:00Z",
                    "match_id": f"{stage}-{index:03d}",
                }
            )
        return rows


if __name__ == "__main__":
    unittest.main()
