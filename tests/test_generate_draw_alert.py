import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import joblib

from draw_model_learning import FEATURES, _train_artifact, predict_draw_probability
from generate_draw_alert import FIELDS, _calibrated_probability, _candidate_from_rows, _capture_feature_snapshot, _qualifying_source_records, attach_stake, derive_structural_signals, generate_alerts, select_alerts


def source_record(**overrides):
    record = {
        "market_type": "win_draw_loss",
        "settlement_minutes": 90,
        "includes_extra_time": False,
        "home_probability": 0.20,
        "draw_probability": 0.25,
        "away_probability": 0.55,
    }
    record.update(overrides)
    return record


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

    def test_alert_csv_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            (root / "data").mkdir()
            path = root / "output" / "draw_alert_2026-07-12.csv"
            path.write_text("previous", encoding="utf-8")

            with patch("generate_draw_alert.Path.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    generate_alerts("2026-07-12", root)

            self.assertEqual("previous", path.read_text(encoding="utf-8"))

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
                        "domestic_sporttery": source_record(),
                        "professional": source_record(),
                        "extra_time": source_record(settlement_minutes=120),
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

    def test_rank_gate_rejects_nonfinite_or_out_of_range_candidate_numbers(self):
        base = {"score": 0.50, "match_id": "A", "stage": "L1", "model_draw_probability": 0.34, "draw_edge": 0.08, "expected_value": 1.12}
        for field, value in (
            ("score", float("nan")),
            ("model_draw_probability", float("inf")),
            ("draw_edge", float("nan")),
            ("expected_value", float("inf")),
            ("model_draw_probability", 1.1),
            ("draw_edge", 1.1),
        ):
            with self.subTest(field=field, value=value):
                self.assertEqual([], select_alerts([{**base, field: value}]))

    def test_alert_level_uses_approved_high_and_medium_thresholds(self):
        evidence = {
            "market_scope": "90m",
            "quality": "high",
            "favorite_movement": -0.05,
            "regional_gap": 0.06,
            "sources": {
                "domestic": source_record(),
                "professional": source_record(),
            },
        }
        domestic = {"001": {"had": {"h": "1.60", "d": "4.00", "a": "6.00"}}}
        common = {
            "date": "2026-07-12", "match_id": "001", "team_a": "A", "team_b": "B",
            "stage": "quarterfinal", "xg_a": "1.0", "xg_b": "1.0", "p_a": "0.54", "p_b": "0.14",
        }
        draw_config = {
            "min_draw_probability": 0.27, "min_draw_edge": 0.04, "min_expected_value": 1.05,
            "max_xg_total": 2.5, "cold_favorite_probability": 0.55,
            "balanced_max_win_gap": 0.1, "balanced_max_xg_total": 2.35,
        }
        app_config = {"knockout_stages": ["quarterfinal"]}
        high = _candidate_from_rows(
            {**common, "p_draw": "0.32"}, evidence, domestic, datetime(2026, 7, 12, tzinfo=timezone.utc),
            draw_config, app_config,
        )
        medium = _candidate_from_rows(
            {**common, "p_draw": "0.31"}, evidence, domestic, datetime(2026, 7, 12, tzinfo=timezone.utc),
            draw_config, app_config,
        )

        self.assertEqual("高级", high["alert_level"])
        self.assertEqual("中级", medium["alert_level"])

    def test_candidate_rejects_an_unreasonable_individual_xg_value(self):
        evidence = {
            "market_scope": "90m", "quality": "high", "favorite_movement": -0.05, "regional_gap": 0.06,
            "sources": {
                "domestic": source_record(),
                "professional": source_record(),
            },
        }
        candidate = _candidate_from_rows(
            {
                "date": "2026-07-12", "match_id": "001", "team_a": "A", "team_b": "B", "stage": "quarterfinal",
                "xg_a": "-1.0", "xg_b": "3.0", "p_a": "0.54", "p_draw": "0.32", "p_b": "0.14",
            },
            evidence, {"001": {"had": {"h": "1.60", "d": "4.00", "a": "6.00"}}},
            datetime(2026, 7, 12, tzinfo=timezone.utc),
            {
                "min_draw_probability": 0.27, "min_draw_edge": 0.04, "min_expected_value": 1.05,
                "max_xg_total": 2.5, "cold_favorite_probability": 0.55,
                "balanced_max_win_gap": 0.1, "balanced_max_xg_total": 2.35,
            },
            {"knockout_stages": ["quarterfinal"]},
        )

        self.assertIsNone(candidate)

    def test_out_of_range_probabilities_are_rejected_before_snapshot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = {
                "kickoff_at": "2026-07-12T12:00:00+00:00",
                "market_scope": "90m",
                "quality": "high",
                "favorite_movement": -0.05,
                "regional_gap": 0.06,
                "sources": {
                    "domestic": source_record(),
                    "professional": source_record(),
                },
            }

            candidate = _candidate_from_rows(
                {
                    "date": "2026-07-12",
                    "match_id": "001",
                    "team_a": "A",
                    "team_b": "B",
                    "stage": "quarterfinal",
                    "xg_a": "1.0",
                    "xg_b": "1.0",
                    "p_a": "1.2",
                    "p_draw": "-0.3",
                    "p_b": "0.1",
                },
                evidence,
                {"001": {"had": {"h": "1.60", "d": "4.00", "a": "6.00"}}},
                datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc),
                {
                    "min_draw_probability": 0.27,
                    "min_draw_edge": 0.04,
                    "min_expected_value": 1.05,
                    "max_xg_total": 2.5,
                    "cold_favorite_probability": 0.55,
                    "balanced_max_win_gap": 0.1,
                    "balanced_max_xg_total": 2.35,
                },
                {"knockout_stages": ["quarterfinal"]},
                root=root,
            )

            self.assertIsNone(candidate)
            self.assertFalse((root / "data" / "draw_feature_snapshots").exists())

    def test_nonfinite_market_probability_cannot_be_a_qualifying_source(self):
        records = _qualifying_source_records({
            "sources": {
                "bad": {
                    "market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False,
                    "home_probability": float("nan"), "draw_probability": 0.25, "away_probability": 0.55,
                },
                "good": {
                    "market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False,
                    "home_probability": 0.20, "draw_probability": 0.25, "away_probability": 0.55,
                },
            },
        })

        self.assertEqual({"good"}, set(records))

    def test_sources_require_probabilities_and_finite_optional_volume(self):
        records = _qualifying_source_records({
            "sources": {
                "metadata_only": {
                    "market_type": "win_draw_loss",
                    "settlement_minutes": 90,
                    "includes_extra_time": False,
                },
                "nan_volume": source_record(volume=float("nan")),
                "infinite_nested_number": source_record(
                    metadata={"risk": float("inf")}
                ),
                "good": source_record(volume=1_250_000),
            }
        })

        self.assertEqual({"good"}, set(records))

    def test_overlap_reuses_main_stake_without_additional_money(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{"match_id": "001", "stake": "100", "selection": "平"}]
        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual(100, result["linked_main_stake"])
        self.assertEqual("linked", result["settlement_mode"])

    def test_same_id_with_conflicting_date_and_teams_does_not_link(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{
            "match_id": "001",
            "date": "2026-07-13",
            "team_a": "X",
            "team_b": "Y",
            "stake": "100",
            "selection": "平",
        }]

        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)

        self.assertEqual("standalone", result["settlement_mode"])
        self.assertEqual(30, result["additional_stake"])

    def test_overlap_without_match_id_uses_date_and_teams(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{"date": "2026-07-12", "team_a": "A", "team_b": "B", "stake": "100", "selection": "平"}]
        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual(100, result["linked_main_stake"])
        self.assertEqual("linked", result["settlement_mode"])

    def test_combo_draw_leg_reuses_the_combo_stake(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{
            "selection": "串关",
            "stake": "60",
            "legs_json": json.dumps([
                {"date": "2026-07-12", "team_a": "A", "team_b": "B", "selection": "平"},
            ]),
        }]

        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)

        self.assertEqual(0, result["additional_stake"])
        self.assertEqual(60, result["linked_main_stake"])
        self.assertEqual("linked", result["settlement_mode"])

    def test_combo_same_id_with_conflicting_date_and_teams_does_not_link(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{
            "selection": "串关",
            "stake": "60",
            "legs_json": json.dumps([
                {"match_id": "001", "date": "2026-07-13", "team_a": "X", "team_b": "Y", "selection": "平"},
            ]),
        }]

        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)

        self.assertEqual("standalone", result["settlement_mode"])
        self.assertEqual(30, result["additional_stake"])
        self.assertEqual(0, result["linked_main_stake"])

    def test_combo_conflicting_nonempty_id_does_not_link(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{
            "selection": "串关",
            "stake": "60",
            "legs_json": json.dumps([
                {"match_id": "999", "date": "2026-07-12", "team_a": "A", "team_b": "B", "selection": "平"},
            ]),
        }]

        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)

        self.assertEqual("standalone", result["settlement_mode"])
        self.assertEqual(30, result["additional_stake"])
        self.assertEqual(0, result["linked_main_stake"])

    def test_invalid_linked_main_stakes_fail_closed_without_crashing(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        for raw_stake in ("NaN", "Infinity", "-60", "501", "10.5"):
            with self.subTest(stake=raw_stake):
                main = [{"match_id": "001", "stake": raw_stake, "selection": "平"}]

                result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)

                self.assertEqual("observation", result["settlement_mode"])
                self.assertEqual(0, result["additional_stake"])
                self.assertEqual(0, result["linked_main_stake"])

    def test_any_invalid_budget_amount_closes_new_alert_staking(self):
        cases = (
            ([{"match_id": "001", "stake": "499.5", "selection": "胜"}], []),
            ([{"match_id": "001", "stake": "NaN", "selection": "胜"}], []),
            ([], [{"additional_stake": "Infinity"}]),
            ([], [{"additional_stake": "-20"}]),
        )
        for main_plan, existing_alerts in cases:
            with self.subTest(main_plan=main_plan, existing_alerts=existing_alerts):
                result = attach_stake(
                    {"match_id": "002", "subtype": "cold_draw"},
                    main_plan,
                    existing_alerts,
                    {"promoted": True},
                    500,
                    80,
                    30,
                )

                self.assertEqual("budget_capped_observation", result["settlement_mode"])
                self.assertEqual(0, result["additional_stake"])

    def test_combo_draw_leg_uses_date_and_forward_team_order_as_fallback(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{
            "selection": "串关",
            "stake": "60",
            "legs_json": json.dumps([
                {"date": "2026-07-12", "team_a": "B", "team_b": "A", "selection": "平"},
                {"date": "2026-07-12", "team_a": "A", "team_b": "B", "selection": "平"},
            ]),
        }]

        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)

        self.assertEqual("linked", result["settlement_mode"])
        self.assertEqual(60, result["linked_main_stake"])

    def test_unparseable_combo_legs_fail_closed_without_matching_other_selections(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{
            "selection": "串关",
            "stake": "60",
            "legs_json": "not-json",
        }]

        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)

        self.assertEqual("standalone", result["settlement_mode"])
        self.assertEqual(30, result["additional_stake"])
        self.assertEqual(0, result["linked_main_stake"])

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
                            "domestic": source_record(),
                            "professional": source_record(),
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

    def test_generator_captures_all_features_and_serves_real_full_feature_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            (root / "data" / "models").mkdir(parents=True)
            (root / "betting_config.json").write_text(
                (Path(__file__).resolve().parents[1] / "betting_config.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "config.json").write_text(
                json.dumps({"knockout_stages": ["quarterfinal"]}), encoding="utf-8"
            )
            artifact = _train_artifact(self._full_feature_samples(), as_of=date(2026, 7, 11))
            artifact_path = root / "data" / "models" / f"{artifact['metadata']['version']}.joblib"
            joblib.dump(artifact, artifact_path)
            (root / "output" / "draw_model_registry.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "champion": {
                        "version": artifact["metadata"]["version"],
                        "artifact": artifact_path.relative_to(root).as_posix(),
                        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                        "feature_order": artifact["feature_order"],
                        "model_kind": artifact["metadata"]["model_kind"],
                    },
                    "challenger": None,
                    "previous_champion": None,
                    "per_league": {},
                }),
                encoding="utf-8",
            )
            (root / "output" / "predictions_2026-07-12.csv").write_text(
                "date,match_id,team_a,team_b,stage,xg_a,xg_b,p_a,p_draw,p_b\n"
                "2026-07-12,001,A,B,quarterfinal,1.0,1.0,0.20,0.60,0.20\n",
                encoding="utf-8",
            )
            (root / "data" / "sporttery_odds_2026-07-12.json").write_text(
                json.dumps({"001": {"had": {"h": "3.00", "d": "4.00", "a": "3.00"}}}),
                encoding="utf-8",
            )
            (root / "data" / "market_heat_2026-07-12.json").write_text(
                json.dumps({
                    "captured_at": "2026-07-01T10:00:00+00:00",
                    "matches": [{
                        "match_id": "001",
                        "kickoff_at": "2026-07-12T12:00:00+00:00",
                        "market_scope": "90m",
                        "quality": "high",
                        "favorite_movement": -0.05,
                        "regional_gap": 0.06,
                        "sources": {
                            "domestic": source_record(),
                            "professional": source_record(),
                        },
                    }],
                }),
                encoding="utf-8",
            )
            (root / "output" / "draw_alert_metrics.json").write_text(
                json.dumps({"balanced_draw": {"promoted": False}}), encoding="utf-8"
            )

            path = generate_alerts(
                "2026-07-12",
                root,
                snapshot_time=datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc),
            )
            snapshots = list((root / "data" / "draw_feature_snapshots").glob("*.json"))
            self.assertEqual(1, len(snapshots))
            snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
            self.assertEqual("2026-07-12T19:00:00+08:00", snapshot["captured_at"])
            self.assertEqual(FEATURES, list(snapshot["features"]))
            self.assertEqual(0.20, snapshot["features"]["favorite_probability"])
            self.assertEqual(0.0, snapshot["features"]["win_probability_gap"])
            self.assertEqual(1, snapshot["features"]["is_knockout"])
            self.assertEqual(1, snapshot["features"]["is_balanced"])
            expected = predict_draw_probability(snapshot["features"], root=root)
            self.assertNotEqual(snapshot["features"]["base_draw_probability"], expected)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertAlmostEqual(expected, float(rows[0]["model_draw_probability"]))

            generate_alerts(
                "2026-07-12",
                root,
                snapshot_time=datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(
                1, len(list((root / "data" / "draw_feature_snapshots").glob("*.json")))
            )

    def test_snapshot_capture_rejects_post_kickoff_write_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _capture_feature_snapshot(
                root,
                {
                    "date": "2026-07-12",
                    "match_id": "001",
                    "team_a": "A",
                    "team_b": "B",
                    "stage": "quarterfinal",
                },
                {"kickoff_at": "2026-07-12 20:00:00"},
                datetime(2026, 7, 12, 12, 0, 1, tzinfo=timezone.utc),
                4.0,
                {
                    "base_draw_probability": 0.32,
                    "market_draw_probability": 0.25,
                    "favorite_probability": 0.54,
                    "win_probability_gap": 0.42,
                    "xg_total": 2.30,
                    "favorite_movement": -0.05,
                    "regional_gap": 0.06,
                    "source_count": 2,
                    "is_knockout": 1,
                    "is_balanced": 0,
                },
            )

        self.assertIsNone(path)

    @staticmethod
    def _full_feature_samples():
        start = date(2025, 1, 1)
        rows = []
        for index in range(200):
            outcome = index % 2
            base = 0.60 if outcome else 0.20
            rows.append({
                "date": (start + timedelta(days=index)).isoformat(),
                "match_id": str(index),
                "team_a": f"A{index}",
                "team_b": f"B{index}",
                "stage": "quarterfinal",
                "outcome": outcome,
                "base_draw_probability": base,
                "market_draw_probability": 0.25,
                "favorite_probability": 0.20,
                "win_probability_gap": 0.0,
                "xg_total": 2.0,
                "favorite_movement": -0.05,
                "regional_gap": 0.06,
                "source_count": 2,
                "is_knockout": 1,
                "is_balanced": 1,
            })
        return rows


if __name__ == "__main__":
    unittest.main()
