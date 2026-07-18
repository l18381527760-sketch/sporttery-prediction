import csv
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import generate_betting_plan as strategy
from betting_ledger import ingest_date
from official_markets import normalize_market
from plan_lock import lock_plan
from value_candidates import ValueCandidate


TARGET_DATE = date(2026, 7, 18)
BEIJING = timezone(timedelta(hours=8))
LOCKED_AT = datetime(2026, 7, 18, 13, 30, tzinfo=BEIJING)
CAPTURED_AT = "2026-07-18T13:20:00+08:00"
KICKOFF_AT = "2026-07-18T20:00:00+08:00"


def value_config(mode: str = "shadow") -> dict:
    return {
        "strategy_version": "value-v4",
        "max_daily_budget": 500,
        "value_strategy": {
            "activation_mode": mode,
            "strict_until_samples": 100,
            "strict_min_probability_edge": 0.01,
            "min_probability_edge": 0.01,
            "strict_min_ev": 0.06,
            "min_ev": 0.03,
            "strict_model_edge_weight_base": 1.0,
            "strict_model_edge_weight_max": 1.0,
            "model_edge_weight_base": 1.0,
            "model_edge_weight_max": 1.0,
            "strict_min_combo_leg_edge": 0.02,
            "min_combo_leg_edge": 0.01,
            "strict_min_combo_leg_ev": 0.02,
            "min_combo_leg_ev": 0.01,
            "strict_min_combo_ev": 0.10,
            "min_combo_ev": 0.03,
            "strict_kelly_fraction": 0.25,
            "kelly_fraction": 0.25,
            "reference_bankroll": 5000,
            "stake_unit": 2,
            "max_match_exposure": 200,
            "max_single_count": 2,
            "combo_min_legs": 2,
            "combo_max_legs": 2,
            "max_daily_combo_stake": 30,
            "min_combo_leg_probability": 0.10,
            "observation_count": 20,
            "calibration_prior": 100,
        },
        "league_calibration": {
            "min_samples": 30,
            "prior_samples": 60,
            "max_adjustment": 0.05,
            "validation_fraction": 0.25,
        },
        "simulation_account": {
            "mode": "simulation",
            "required_settled_days": 30,
            "monthly_budget_cap": 5000,
            "monthly_stop_loss": 5000,
            "real_money_automation": False,
        },
        "learning_policy": {
            "case_study_policy": "regression_only",
            "minimum_rule_samples": 30,
        },
    }


def prediction(match_id: str) -> dict:
    return {
        "date": TARGET_DATE.isoformat(),
        "match_id": match_id,
        "stage": "Test League",
        "team_a": f"Home {match_id}",
        "team_b": f"Away {match_id}",
        "kickoff_at": KICKOFF_AT,
        "p_a": "0.70",
        "p_draw": "0.20",
        "p_b": "0.10",
        "xg_a": "2.00",
        "xg_b": "0.50",
    }


def market_fixture(match_id: str, market_type: str):
    prices = {
        "had": {"h": "3.00", "d": "3.00", "a": "3.00"},
        "hhad": {"h": "3.00", "d": "3.00", "a": "3.00", "goalLine": "+1"},
        "ttg": {f"s{index}": "8.00" for index in range(8)},
    }[market_type]
    raw = {
        **prices,
        "source": "sporttery",
        "source_record_id": f"decision-{match_id}-{market_type}",
        "captured_at_bjt": CAPTURED_AT,
    }
    market = normalize_market(match_id, market_type, raw)
    assert market is not None
    snapshot = {
        "target_date": TARGET_DATE.isoformat(),
        "capture_phase": "decision",
        "captured_at": CAPTURED_AT,
        "source": "sporttery",
        "matches": [{
            **prediction(match_id),
            "markets": {market_type: prices},
            "single_eligibility": {"had": True, "hhad": True, "ttg": True},
        }],
    }
    return {match_id: {market_type: market}}, snapshot


def candidate(match_id: str, *, market_type: str = "had", play: str | None = None) -> ValueCandidate:
    line = 1 if market_type == "hhad" else None
    selection = "2球" if market_type == "ttg" else "胜"
    return ValueCandidate(
        candidate_id=f"{match_id}:{market_type}:{selection}",
        date=TARGET_DATE.isoformat(),
        match_id=match_id,
        stage="Test League",
        team_a=f"Home {match_id}",
        team_b=f"Away {match_id}",
        kickoff_at=KICKOFF_AT,
        market_type=market_type,
        play=play or market_type.upper(),
        selection=selection,
        line=line,
        official_odds=3.0,
        official_market_probability=1 / 3,
        raw_model_probability=0.60,
        calibrated_model_probability=0.60,
        conservative_probability=0.60,
        probability_edge=0.60 - 1 / 3,
        expected_value=0.80,
        single_eligible=True,
        data_quality="medium",
        data_quality_multiplier=0.60,
        volatility_band="stable",
        volatility_multiplier=1.0,
        odds_source="sporttery",
        source_record_id=f"decision-{match_id}",
        captured_at_bjt=CAPTURED_AT,
        correlation_tags=(f"match:{match_id}",),
        paid_eligible=True,
        value_gate_reasons=(),
        calibration_samples=0,
    )


class ValueV4PlanIntegrationTest(unittest.TestCase):
    def test_generator_exposes_no_public_paid_ledger_rebuild_writer(self):
        self.assertFalse(hasattr(strategy, "write_ledger"))

    def test_canonical_settled_observations_drive_candidates_and_limits_together(self):
        configured = value_config()
        configured["value_strategy"].update(
            strict_max_single_stake=50, max_single_stake=200
        )
        captured = {}
        settled = [
            {
                "date": "2026-07-17",
                "strategy_version": "value-v4",
                "bet_id": f"settled-{index}",
                "match_id": f"match-{index}",
                "market_type": "had",
                "status": "命中",
                "stake": "0",
            }
            for index in range(100)
        ]
        observations = [
            *settled,
            dict(settled[0]),
            {**settled[0], "bet_id": "pending", "status": "未结算"},
            {**settled[0], "bet_id": "legacy", "strategy_version": "legacy-v3"},
            {**settled[0], "bet_id": "future", "date": TARGET_DATE.isoformat()},
            {
                **settled[0],
                "bet_id": "malformed-parlay",
                "market_type": "parlay",
                "match_id": "",
                "canonical_legs_json": json.dumps([
                    {"match_id": "same", "market_type": "had", "selection": "胜", "line": ""},
                    {"match_id": "same", "market_type": "ttg", "selection": "2球", "line": ""},
                ], ensure_ascii=False),
            },
        ]
        real_allocate = strategy.allocate_portfolio

        def load_history(path: Path):
            return observations if path.name == "observation_ledger.csv" else []

        def capture_candidates(predictions, markets, snapshot, config, calibrations):
            captured["candidate_samples"] = config["value_strategy"]["settled_samples"]
            return []

        def capture_limits(candidates, limits, account):
            captured["limit_samples"] = limits.settled_samples
            captured["max_single_stake"] = limits.max_single_stake
            return real_allocate(candidates, limits, account)

        with self.strategy_context(configured):
            with (
                patch.object(strategy, "load_csv", side_effect=load_history),
                patch.object(strategy, "build_candidates", side_effect=capture_candidates),
                patch.object(strategy, "allocate_portfolio", side_effect=capture_limits),
            ):
                strategy.build_value_v4_plan(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual(100, captured["candidate_samples"])
        self.assertEqual(100, captured["limit_samples"])
        self.assertEqual(200, captured["max_single_stake"])

    def test_active_mode_audit_has_no_selected_shadow_rows(self):
        with self.strategy_context(value_config("active")):
            with (
                patch.object(strategy, "build_legacy_value_plan", return_value=([], [])),
                patch.object(strategy, "build_candidates", return_value=[candidate("active")]),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual([], outputs.shadow_plan)
        self.assertEqual([], outputs.audit["selected_shadow"])

    def test_empty_shadow_generation_writes_full_schema_and_never_paid_ledger(self):
        for generate_only in (False, True):
            with self.subTest(generate_only=generate_only), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                output = root / "output"
                output.mkdir()
                ledger_path = output / "betting_ledger.csv"
                ledger_path.write_bytes(b"locked-ledger-bytes")
                argv = [
                    "generate_betting_plan.py", "--date", str(TARGET_DATE),
                    "--locked-at", LOCKED_AT.isoformat(),
                ]
                if generate_only:
                    argv.append("--generate-only")
                empty_outputs = strategy.StrategyOutputs(
                    [], [], [], {"activation_mode": "shadow", "selected_shadow": []}
                )
                with (
                    patch.object(strategy, "ROOT", root),
                    patch.object(strategy, "OUTPUT_DIR", output),
                    patch.object(strategy, "DATA_DIR", root / "data"),
                    patch.object(strategy, "build_strategy_outputs", return_value=empty_outputs),
                    patch.object(strategy, "write_daily_decision", return_value=output / "decision.json"),
                    patch.object(strategy, "settle_ledger") as settle_mock,
                    patch.object(sys, "argv", argv),
                ):
                    result = strategy.main()
                shadow_path = output / f"shadow_betting_plan_{TARGET_DATE}.csv"
                with shadow_path.open(encoding="utf-8-sig", newline="") as handle:
                    fields = csv.DictReader(handle).fieldnames

                self.assertEqual(0, result)
                self.assertIn("bet_id", fields)
                self.assertIn("market_type", fields)
                self.assertIn("locked_odds", fields)
                self.assertEqual(b"locked-ledger-bytes", ledger_path.read_bytes())
                settle_mock.assert_not_called()

    def test_generate_only_and_settle_only_are_mutually_exclusive(self):
        with (
            patch.object(sys, "argv", [
                "generate_betting_plan.py", "--generate-only", "--settle-only"
            ]),
            patch.object(strategy, "settle_ledger") as settle_mock,
            self.assertRaises(SystemExit),
        ):
            strategy.main()
        settle_mock.assert_not_called()

    def test_settle_only_delegates_to_ledger_without_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            ledger_path = root / "output" / "betting_ledger.csv"
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", root / "output"),
                patch.object(strategy, "DATA_DIR", root / "data"),
                patch.object(strategy, "settle_ledger", return_value=ledger_path) as settle_mock,
                patch.object(strategy, "build_strategy_outputs", side_effect=AssertionError("generated")),
                patch.object(sys, "argv", [
                    "generate_betting_plan.py", "--date", str(TARGET_DATE), "--settle-only"
                ]),
            ):
                result = strategy.main()

        self.assertEqual(0, result)
        self.assertEqual(root, settle_mock.call_args.args[0])
        self.assertEqual({}, settle_mock.call_args.args[1])

    def write_real_generation_fixture(self, root: Path) -> None:
        output = root / "output"
        data = root / "data"
        snapshots = data / "odds_snapshots"
        output.mkdir(parents=True)
        snapshots.mkdir(parents=True)
        (root / "betting_config.json").write_text(
            json.dumps(value_config(), ensure_ascii=False), encoding="utf-8"
        )
        predictions = []
        for index in range(1, 4):
            row = prediction(f"legacy-{index}")
            row["is_single_had"] = "true" if index == 1 else "false"
            predictions.append(row)
        with (output / f"predictions_{TARGET_DATE.isoformat()}.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(predictions[0]))
            writer.writeheader()
            writer.writerows(predictions)
        odds = {
            row["match_id"]: {"had": {"h": "3.00", "d": "3.00", "a": "3.00"}}
            for row in predictions
        }
        (data / f"sporttery_odds_{TARGET_DATE.isoformat()}.json").write_text(
            json.dumps(odds), encoding="utf-8"
        )
        snapshot = {
            "target_date": TARGET_DATE.isoformat(),
            "capture_phase": "decision",
            "captured_at": CAPTURED_AT,
            "source": "sporttery",
            "source_record_id": "decision-snapshot-before-lock",
            "matches": [
                {
                    **row,
                    "markets": odds[row["match_id"]],
                    "single_eligibility": {"had": row["is_single_had"] == "true"},
                }
                for row in predictions
            ],
        }
        (snapshots / f"{TARGET_DATE.isoformat()}-132000-decision.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )

    def test_real_shadow_plan_writes_locks_and_ingests_canonical_legacy_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write_real_generation_fixture(root)
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", root / "output"),
                patch.object(strategy, "DATA_DIR", root / "data"),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)
                plan_path = strategy.write_plan(outputs.active_plan, TARGET_DATE)
            lock_plan(root, TARGET_DATE, LOCKED_AT, "sporttery")
            ledger_path = ingest_date(root, TARGET_DATE)
            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            locked_plan_bytes = plan_path.read_bytes()

        self.assertEqual({"had", "parlay"}, {row["market_type"] for row in rows})
        self.assertTrue(all(row["match_id"] or row["market_type"] == "parlay" for row in rows))
        self.assertTrue(all(row["bet_id"] for row in rows))
        parlay = next(row for row in rows if row["market_type"] == "parlay")
        legs = json.loads(parlay["canonical_legs_json"])
        self.assertEqual(2, len(legs))
        self.assertEqual(2, len({leg["match_id"] for leg in legs}))
        self.assertTrue(locked_plan_bytes.startswith(b"\xef\xbb\xbf"))

    def test_snapshot_and_market_normalization_use_latest_capture_not_after_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshots = root / "data" / "odds_snapshots"
            snapshots.mkdir(parents=True)
            before = market_fixture("cutoff", "had")[1]
            before["source_record_id"] = "before-lock"
            after = deepcopy(before)
            after["captured_at"] = "2026-07-18T13:40:00+08:00"
            after["source_record_id"] = "after-lock"
            after["matches"][0]["markets"]["had"]["h"] = "9.00"
            (snapshots / f"{TARGET_DATE}-132000-decision.json").write_text(
                json.dumps(before), encoding="utf-8"
            )
            (snapshots / f"{TARGET_DATE}-134000-decision.json").write_text(
                json.dumps(after), encoding="utf-8"
            )
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", root / "output"),
                patch.object(strategy, "DATA_DIR", root / "data"),
            ):
                selected = strategy.load_value_snapshot(TARGET_DATE, locked_at=LOCKED_AT)
                markets = strategy.load_official_decision_markets(
                    TARGET_DATE, snapshot=selected
                )

        self.assertEqual(CAPTURED_AT, selected["captured_at"])
        self.assertEqual(3.0, markets["cutoff"]["had"].prices["胜"])
        self.assertIn("before-lock", markets["cutoff"]["had"].source_record_id)

    def test_valid_existing_lock_bypasses_generation_and_preserves_plan_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            data = root / "data"
            output.mkdir()
            data.mkdir()
            plan_path = output / f"betting_plan_{TARGET_DATE}.csv"
            plan_path.write_bytes(b"date,stake\n2026-07-18,10\n")
            (data / f"sporttery_odds_{TARGET_DATE}.json").write_text("{}", encoding="utf-8")
            lock_plan(root, TARGET_DATE, LOCKED_AT, "sporttery")
            snapshots = data / "odds_snapshots"
            snapshots.mkdir()
            later_snapshot = market_fixture("later", "had")[1]
            later_snapshot["captured_at"] = "2026-07-18T13:40:00+08:00"
            later_snapshot["matches"][0]["markets"]["had"]["h"] = "9.00"
            (snapshots / f"{TARGET_DATE}-134000-decision.json").write_text(
                json.dumps(later_snapshot), encoding="utf-8"
            )
            original = plan_path.read_bytes()
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", output),
                patch.object(strategy, "DATA_DIR", data),
                patch.object(strategy, "build_strategy_outputs", side_effect=AssertionError("regenerated")),
                patch.object(sys, "argv", ["generate_betting_plan.py", "--date", str(TARGET_DATE), "--locked-at", LOCKED_AT.isoformat()]),
            ):
                result = strategy.main()
                final = plan_path.read_bytes()

        self.assertEqual(0, result)
        self.assertEqual(original, final)

    def test_existing_invalid_lock_fails_before_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            (output / f"plan_lock_{TARGET_DATE}.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", output),
                patch.object(strategy, "DATA_DIR", root / "data"),
                patch.object(strategy, "build_strategy_outputs", side_effect=AssertionError("regenerated")),
                patch.object(sys, "argv", ["generate_betting_plan.py", "--date", str(TARGET_DATE), "--locked-at", LOCKED_AT.isoformat()]),
            ):
                result = strategy.main()

        self.assertEqual(1, result)

    def run_v4(self, market_type: str):
        markets, snapshot = market_fixture(f"match-{market_type}", market_type)
        row = prediction(f"match-{market_type}")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", root / "output"),
                patch.object(strategy, "DATA_DIR", root / "data"),
                patch.object(strategy, "read_json", return_value=value_config()),
                patch.object(strategy, "load_predictions", return_value=[row]),
                patch.object(strategy, "load_value_snapshot", return_value=snapshot, create=True),
                patch.object(strategy, "load_official_decision_markets", return_value=markets, create=True),
                patch.object(strategy, "load_draw_training_samples", return_value=[]),
            ):
                return strategy.build_value_v4_plan(TARGET_DATE, locked_at=LOCKED_AT)

    def test_had_hhad_and_ttg_can_each_independently_qualify(self):
        for market_type in ("had", "hhad", "ttg"):
            with self.subTest(market_type=market_type):
                plan, observations = self.run_v4(market_type)
                self.assertEqual([market_type], [row["market_type"] for row in plan])
                self.assertTrue(observations)
                self.assertTrue(all(float(row["stake"]) == 0 for row in observations))

    def test_unsupported_play_never_enters_plan_and_is_audited(self):
        invalid = replace(candidate("bad"), play="SCORE")
        with self.strategy_context(value_config()):
            with patch.object(strategy, "build_candidates", return_value=[invalid], create=True):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual([], outputs.shadow_plan)
        self.assertTrue(any("unsupported_play" in reason for reason in outputs.audit["rejection_reasons"]))

    def test_shadow_and_active_modes_route_only_the_selected_strategy(self):
        for mode, active_version, shadow_count in (("shadow", "legacy-v3", 1), ("active", "value-v4", 0)):
            with self.subTest(mode=mode), self.strategy_context(value_config(mode)):
                with (
                    patch.object(strategy, "build_legacy_value_plan", return_value=([{"strategy_version": "legacy-v3", "stake": 10}], []), create=True),
                    patch.object(strategy, "_finalize_legacy_plan", side_effect=lambda rows, markets, locked_at: rows),
                    patch.object(strategy, "build_value_v4_plan", return_value=([{"strategy_version": "value-v4", "bet_id": "v4", "market_type": "had", "stake": 20}], [{"strategy_version": "value-v4", "stake": 0}]), create=True),
                ):
                    outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

            self.assertEqual(active_version, outputs.active_plan[0]["strategy_version"])
            self.assertEqual(shadow_count, len(outputs.shadow_plan))
            self.assertTrue(all(row["strategy_version"] == "value-v4" for row in outputs.observations))

    def test_locked_rerun_preserves_v4_odds_and_bet_ids(self):
        row = candidate("locked")
        with self.strategy_context(value_config()):
            with patch.object(strategy, "build_candidates", return_value=[row], create=True):
                first, _ = strategy.build_value_v4_plan(TARGET_DATE, locked_at=LOCKED_AT)
                second, _ = strategy.build_value_v4_plan(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual(
            [(item["bet_id"], item["locked_odds"]) for item in first],
            [(item["bet_id"], item["locked_odds"]) for item in second],
        )

    def test_invalid_activation_mode_fails_closed(self):
        with self.strategy_context(value_config("paper")):
            with self.assertRaises(ValueError):
                strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

    def test_zero_candidates_make_valid_no_bet_outputs_and_zero_paid_stake(self):
        with self.strategy_context(value_config()):
            with (
                patch.object(strategy, "build_legacy_value_plan", return_value=([], []), create=True),
                patch.object(strategy, "build_candidates", return_value=[], create=True),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual([], outputs.active_plan)
        self.assertEqual([], outputs.shadow_plan)
        self.assertEqual(0, outputs.audit["comparison"]["active_paid_stake"])
        self.assertEqual(0, outputs.audit["comparison"]["shadow_paid_stake"])

    def test_allocator_limits_survive_daily_integration(self):
        candidates = [candidate(f"m{index}") for index in range(6)]
        with self.strategy_context(value_config()):
            with (
                patch.object(strategy, "build_legacy_value_plan", return_value=([], []), create=True),
                patch.object(strategy, "build_candidates", return_value=candidates, create=True),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        selected = outputs.shadow_plan
        self.assertLessEqual(sum(int(row["stake"]) for row in selected), 500)
        self.assertLessEqual(len([row for row in selected if row["market_type"] != "parlay"]), 2)
        self.assertLessEqual(sum(int(row["stake"]) for row in selected if row["market_type"] == "parlay"), 30)
        self.assertTrue(all(check["passed"] for check in outputs.audit["risk_checks"]))
        self.assertEqual(200, outputs.audit["risk_caps"]["max_match_exposure"])
        self.assertEqual(5000, outputs.audit["risk_caps"]["monthly_budget_cap"])

    def strategy_context(self, config):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        snapshot = {
            "target_date": TARGET_DATE.isoformat(),
            "capture_phase": "decision",
            "captured_at": CAPTURED_AT,
            "source": "sporttery",
            "matches": [],
        }
        return _Patches(
            patch.object(strategy, "ROOT", root),
            patch.object(strategy, "OUTPUT_DIR", root / "output"),
            patch.object(strategy, "DATA_DIR", root / "data"),
            patch.object(strategy, "read_json", return_value=deepcopy(config)),
            patch.object(strategy, "load_predictions", return_value=[]),
            patch.object(strategy, "load_value_snapshot", return_value=snapshot, create=True),
            patch.object(strategy, "load_official_decision_markets", return_value={}, create=True),
            patch.object(strategy, "load_draw_training_samples", return_value=[]),
        )


class _Patches:
    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        for item in reversed(self.patches):
            item.stop()


if __name__ == "__main__":
    unittest.main()
