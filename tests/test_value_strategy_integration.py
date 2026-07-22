import csv
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import generate_betting_plan as strategy
import betting_ledger as ledger_module
from betting_ledger import ingest_date
from official_markets import (
    THREE_WAY_SELECTIONS,
    TOTAL_GOALS_SELECTIONS,
    normalize_market,
)
from plan_lock import lock_plan, sha256_file as plan_lock_sha
from value_candidates import ValueCandidate


TARGET_DATE = date(2026, 7, 18)
BEIJING = timezone(timedelta(hours=8))
LOCKED_AT = datetime(2026, 7, 18, 13, 30, tzinfo=BEIJING)
CAPTURED_AT = "2026-07-18T13:20:00+08:00"
KICKOFF_AT = "2026-07-18T20:00:00+08:00"


def synthetic_decision_bundle(root: Path, source: str = "sporttery") -> dict:
    bundle_path = root / "output" / f"decision_bundle_{TARGET_DATE}.json"
    bundle_path.write_text("{}\n", encoding="utf-8")
    odds_path = root / "data" / f"sporttery_odds_{TARGET_DATE}.json"
    plan_path = root / "output" / f"betting_plan_{TARGET_DATE}.csv"
    plan_bytes = plan_path.read_bytes()
    return {
        "locked_at_bjt": LOCKED_AT.isoformat(),
        "decision_snapshot": {
            "source": source,
            "path": odds_path.relative_to(root).as_posix(),
            "sha256": plan_lock_sha(odds_path),
        },
        "paid_plan_evidence": {
            "plan_sha256": plan_lock_sha(plan_path),
            "bytes": len(plan_bytes),
            "rows_sha256": "e" * 64,
        },
    }


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


def settled_value_single(
    match_id: str,
    market_type: str,
    selection: str,
    *,
    line: str = "",
    report_date: str = "2026-07-17",
    home_goals: int = 2,
    away_goals: int = 0,
    stake: str = "0",
) -> dict:
    row = {
        "date": report_date,
        "report_date": report_date,
        "strategy_version": "value-v4",
        "model_version": "maturity-model",
        "match_id": match_id,
        "play": market_type.upper(),
        "market_type": market_type,
        "market_line": line,
        "selection": selection,
        "odds": "2.00",
        "locked_odds": "2.00",
        "odds_source": "sporttery",
        "odds_source_record_id": f"odds-{match_id}-{market_type}",
        "odds_captured_at_bjt": "2026-07-17T13:00:00+08:00",
        "locked_at_bjt": "2026-07-17T13:05:00+08:00",
        "kelly_fraction": "0.25",
        "stake": stake,
        "status": ledger_module.PENDING,
    }
    if Decimal(stake) > 0:
        row = ledger_module.ingest_locked_plan([], [row], {
            "report_date": report_date,
            "locked_at_bjt": row["locked_at_bjt"],
            "plan_sha256": "a" * 64,
            "odds_source": "sporttery",
        }, canonical_evidence={})[0]
    else:
        row["bet_id"] = ledger_module.stable_bet_id(row)
    result = {
        "match_id": match_id,
        "result_status": "finished",
        "result_source": "sporttery",
        "source_record_id": f"result-{match_id}",
        "captured_at_bjt": "2026-07-17T22:00:00+08:00",
        "score_scope": "regular_time_90",
        "settlement_minutes": "90",
        "home_goals": str(home_goals),
        "away_goals": str(away_goals),
    }
    return ledger_module.settle_pending(
        [row],
        {match_id: result},
        datetime(2026, 7, 17, 22, 5, tzinfo=BEIJING),
    )[0]


def settled_value_parlay(report_date: str = "2026-07-17") -> dict:
    legs = [
        {
            "match_id": "maturity-parlay-a",
            "market_type": "had",
            "selection": THREE_WAY_SELECTIONS["h"],
            "line": "",
            "odds": "2.00",
            "odds_source": "sporttery",
            "odds_source_record_id": "odds-maturity-parlay-a",
            "odds_captured_at_bjt": "2026-07-17T13:00:00+08:00",
        },
        {
            "match_id": "maturity-parlay-b",
            "market_type": "ttg",
            "selection": TOTAL_GOALS_SELECTIONS["s2"],
            "line": "",
            "odds": "3.00",
            "odds_source": "sporttery",
            "odds_source_record_id": "odds-maturity-parlay-b",
            "odds_captured_at_bjt": "2026-07-17T13:00:00+08:00",
        },
    ]
    row = {
        "date": report_date,
        "report_date": report_date,
        "strategy_version": "value-v4",
        "model_version": "maturity-model",
        "match_id": "",
        "play": "PARLAY",
        "market_type": "parlay",
        "market_line": "",
        "selection": "two legs",
        "legs_json": json.dumps(legs, ensure_ascii=False),
        "odds": "6.00",
        "locked_odds": "6.00",
        "odds_source": "sporttery",
        "odds_source_record_id": "odds-maturity-parlay",
        "odds_captured_at_bjt": "2026-07-17T13:00:00+08:00",
        "locked_at_bjt": "2026-07-17T13:05:00+08:00",
        "kelly_fraction": "0.25",
        "stake": "10",
        "status": ledger_module.PENDING,
    }
    row = ledger_module.ingest_locked_plan([], [row], {
        "report_date": report_date,
        "locked_at_bjt": row["locked_at_bjt"],
        "plan_sha256": "b" * 64,
        "odds_source": "sporttery",
    }, canonical_evidence={})[0]
    results = {
        "maturity-parlay-a": {
            "match_id": "maturity-parlay-a",
            "result_status": "finished",
            "result_source": "sporttery",
            "source_record_id": "result-maturity-parlay-a",
            "captured_at_bjt": "2026-07-17T22:00:00+08:00",
            "score_scope": "regular_time_90",
            "settlement_minutes": "90",
            "home_goals": "2",
            "away_goals": "0",
        },
        "maturity-parlay-b": {
            "match_id": "maturity-parlay-b",
            "result_status": "finished",
            "result_source": "sporttery",
            "source_record_id": "result-maturity-parlay-b",
            "captured_at_bjt": "2026-07-17T22:00:00+08:00",
            "score_scope": "regular_time_90",
            "settlement_minutes": "90",
            "home_goals": "2",
            "away_goals": "0",
        },
    }
    return ledger_module.settle_pending(
        [row], results, datetime(2026, 7, 17, 22, 5, tzinfo=BEIJING)
    )[0]


class ValueV4PlanIntegrationTest(unittest.TestCase):
    def test_explicit_input_builder_returns_candidates_observations_and_diagnostics(self):
        _markets, snapshot = market_fixture("explicit", "had")
        snapshot["source_record_id"] = "snapshot-explicit"

        result = strategy.build_value_v4_from_inputs(
            TARGET_DATE,
            locked_at=LOCKED_AT,
            config=value_config(),
            predictions=[prediction("explicit")],
            snapshot=snapshot,
            paid_history=[],
            observation_history=[],
            training_samples=[],
        )

        self.assertGreater(len(result.candidates), 0)
        self.assertEqual(len(result.candidates), len(result.observations))
        self.assertEqual([], result.diagnostics)
        self.assertEqual(len(result.candidates), result.audit["candidate_count"])

    def test_empty_optional_snapshot_markets_are_absent_not_invalid(self):
        _markets, snapshot = market_fixture("optional-empty", "had")
        snapshot["source_record_id"] = "snapshot-optional-empty"
        snapshot["matches"][0]["markets"].update(hhad={}, ttg={})
        diagnostics = []

        markets = strategy.load_official_decision_markets(
            TARGET_DATE, snapshot=snapshot, diagnostics=diagnostics
        )

        self.assertEqual({"had"}, set(markets["optional-empty"]))
        self.assertEqual([], diagnostics)

    def test_ledger_history_is_cut_off_at_lock_and_later_settlement_is_pending(self):
        settled = settled_value_single(
            "known-settlement", "had", THREE_WAY_SELECTIONS["h"], stake="20"
        )
        later_settlement = deepcopy(settled)
        later_settlement["bet_id"] = "later-settlement"
        later_settlement["settled_at_bjt"] = "2026-07-18T13:30:00.000001+08:00"
        later_settlement["profit"] = "20"
        later_settlement["return"] = "40"
        future_lock = deepcopy(settled)
        future_lock["bet_id"] = "future-lock"
        future_lock["locked_at_bjt"] = "2026-07-18T13:30:00.000001+08:00"
        future_date = deepcopy(settled)
        future_date["bet_id"] = "future-date"
        future_date["date"] = "2026-07-19"
        future_date["report_date"] = "2026-07-19"
        legacy = {
            "date": "2026-07-11",
            "strategy_version": "",
            "locked_at_bjt": "",
            "status": ledger_module.WON,
            "stake": "100",
            "profit": "100",
            "return": "200",
        }

        rows = strategy.ledger_history_as_of(
            [settled, later_settlement, future_lock, future_date, legacy],
            TARGET_DATE,
            LOCKED_AT,
        )

        self.assertEqual(3, len(rows))
        by_id = {row.get("bet_id", "legacy"): row for row in rows}
        self.assertIn(settled["bet_id"], by_id)
        pending = by_id["later-settlement"]
        self.assertEqual(ledger_module.PENDING, pending["status"])
        self.assertEqual("0", pending["profit"])
        self.assertEqual("0", pending["return"])
        self.assertEqual(ledger_module.PENDING, by_id["legacy"]["status"])
        self.assertEqual("0", by_id["legacy"]["profit"])

        with self.assertRaisesRegex(ValueError, "locked_at_bjt"):
            strategy.ledger_history_as_of(
                [{**settled, "locked_at_bjt": "not-a-time"}],
                TARGET_DATE,
                LOCKED_AT,
            )

    def test_training_samples_are_strictly_point_in_time_or_fail_invalid(self):
        past = {
            "date": "2026-07-17",
            "match_id": "past",
            "stage": "Test League",
            "captured_at": "2026-07-17T12:00:00+08:00",
            "kickoff_at": "2026-07-17T18:00:00+08:00",
            "outcome": "1",
            "base_draw_probability": "0.30",
        }
        late_capture = {
            **past,
            "match_id": "late",
            "captured_at": "2026-07-18T13:30:00.000001+08:00",
            "kickoff_at": "2026-07-18T18:00:00+08:00",
        }
        outcome_not_yet_mature = {
            **past,
            "match_id": "ongoing",
            "kickoff_at": "2026-07-18T11:30:00+08:00",
        }
        target_day = {**past, "date": "2026-07-18", "match_id": "target"}

        rows = strategy.training_samples_as_of(
            [past, late_capture, outcome_not_yet_mature, target_day],
            TARGET_DATE,
            LOCKED_AT,
        )

        self.assertEqual(["past"], [row["match_id"] for row in rows])
        with self.assertRaisesRegex(ValueError, "captured_at"):
            strategy.training_samples_as_of(
                [{**past, "captured_at": "naive-time"}],
                TARGET_DATE,
                LOCKED_AT,
            )

    def test_generator_exposes_no_public_paid_ledger_rebuild_writer(self):
        self.assertFalse(hasattr(strategy, "write_ledger"))

    def test_canonical_settled_observations_drive_candidates_and_limits_together(self):
        configured = value_config()
        configured["value_strategy"].update(
            strict_max_single_stake=50, max_single_stake=200
        )
        captured = {}
        settled = [
            settled_value_single(
                f"match-{index}", "had", THREE_WAY_SELECTIONS["h"]
            )
            for index in range(100)
        ]
        future = settled_value_single(
            "future-match",
            "had",
            THREE_WAY_SELECTIONS["h"],
            report_date=TARGET_DATE.isoformat(),
        )
        observations = [
            *settled,
            dict(settled[0]),
            {**settled[0], "bet_id": "pending", "status": "未结算"},
            {**settled[0], "bet_id": "legacy", "strategy_version": "legacy-v3"},
            future,
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

        def capture_candidates(
            predictions, markets, snapshot, config, calibrations, *, diagnostics=None
        ):
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

    def test_settled_maturity_counts_unique_market_units_not_selections(self):
        settled_date = (TARGET_DATE - timedelta(days=1)).isoformat()

        def settled(match_id, market_type, line, selection, suffix):
            return settled_value_single(
                match_id,
                market_type,
                selection,
                line=line,
                report_date=settled_date,
            )

        had_vector = [
            settled("vector-match", "had", "", selection, code)
            for code, selection in THREE_WAY_SELECTIONS.items()
        ]
        ttg_vector = [
            settled("vector-match", "ttg", "", selection, code)
            for code, selection in TOTAL_GOALS_SELECTIONS.items()
        ]
        paid = [had_vector[0], ttg_vector[0]]
        observations = [*had_vector, *ttg_vector, deepcopy(had_vector[0])]

        self.assertEqual(
            2,
            strategy._settled_sample_count(paid, observations, TARGET_DATE),
        )

        different_report_date = settled_value_single(
            "vector-match",
            "had",
            THREE_WAY_SELECTIONS["h"],
            report_date=(TARGET_DATE - timedelta(days=2)).isoformat(),
        )
        self.assertEqual(
            3,
            strategy._settled_sample_count(
                paid, [*observations, different_report_date], TARGET_DATE
            ),
        )

        another_match = settled(
            "other-match", "had", "", THREE_WAY_SELECTIONS["h"], "other"
        )
        hhad_plus_one = [
            settled(
                "vector-match", "hhad", line, selection, f"plus-{code}"
            )
            for line, (code, selection) in zip(
                ("+1", "1"), tuple(THREE_WAY_SELECTIONS.items())[:2]
            )
        ]
        hhad_minus_one = settled(
            "vector-match", "hhad", "-1", THREE_WAY_SELECTIONS["a"], "minus"
        )
        self.assertEqual(
            5,
            strategy._settled_sample_count(
                [*paid, another_match],
                [*observations, *hhad_plus_one, hhad_minus_one],
                TARGET_DATE,
            ),
        )

    def test_settled_maturity_rejects_noncanonical_or_unproven_singles(self):
        valid = settled_value_single(
            "maturity-single", "had", THREE_WAY_SELECTIONS["h"]
        )
        self.assertEqual(
            1, strategy._settled_sample_count([], [valid], TARGET_DATE)
        )

        unsupported_selection = {**valid, "selection": "unsupported"}
        unsupported_selection["bet_id"] = ledger_module.stable_bet_id(
            unsupported_selection
        )
        cases = (
            ("spoofed identity", {**valid, "bet_id": "spoofed"}),
            ("unsupported selection", unsupported_selection),
            ("missing result status", {**valid, "result_status": ""}),
            ("missing result source", {**valid, "result_source": ""}),
            ("missing result record", {**valid, "source_record_id": ""}),
            ("naive result capture", {
                **valid, "captured_at_bjt": "2026-07-17T22:00:00"
            }),
            ("missing settlement time", {**valid, "settled_at_bjt": ""}),
            ("inconsistent refund", {
                **valid, "result_status": "refunded", "status": ledger_module.WON
            }),
            ("pending", {**valid, "status": ledger_module.PENDING}),
            ("future", settled_value_single(
                "future-single",
                "had",
                THREE_WAY_SELECTIONS["h"],
                report_date=TARGET_DATE.isoformat(),
            )),
            ("malformed date", {
                **valid, "date": "not-a-date", "report_date": "not-a-date"
            }),
        )
        for name, row in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    0, strategy._settled_sample_count([], [row], TARGET_DATE)
                )

    def test_settled_maturity_requires_canonical_parlay_leg_evidence(self):
        valid = settled_value_parlay()
        self.assertIn("|", valid["captured_at_bjt"])
        self.assertEqual(
            2, strategy._settled_sample_count([], [valid], TARGET_DATE)
        )

        malformed_result_json = {**valid, "result_legs_json": "not-json"}
        missing_result_leg = deepcopy(valid)
        result_legs = json.loads(missing_result_leg["result_legs_json"])
        missing_result_leg["result_legs_json"] = json.dumps(
            result_legs[:1], ensure_ascii=False
        )
        missing_result_record = deepcopy(valid)
        result_legs = json.loads(missing_result_record["result_legs_json"])
        result_legs[0]["source_record_id"] = ""
        missing_result_record["result_legs_json"] = json.dumps(
            result_legs, ensure_ascii=False
        )
        naive_result_capture = deepcopy(valid)
        result_legs = json.loads(naive_result_capture["result_legs_json"])
        result_legs[0]["captured_at_bjt"] = "2026-07-17T22:00:00"
        naive_result_capture["result_legs_json"] = json.dumps(
            result_legs, ensure_ascii=False
        )
        mismatched_result_identity = deepcopy(valid)
        result_legs = json.loads(mismatched_result_identity["result_legs_json"])
        result_legs[0]["match_id"] = "different-match"
        mismatched_result_identity["result_legs_json"] = json.dumps(
            result_legs, ensure_ascii=False
        )
        unsupported_leg = deepcopy(valid)
        plan_legs = json.loads(unsupported_leg["legs_json"])
        plan_legs[0]["selection"] = "unsupported"
        unsupported_leg["legs_json"] = json.dumps(plan_legs, ensure_ascii=False)
        unsupported_leg["bet_id"] = ledger_module.stable_bet_id(unsupported_leg)
        same_match = deepcopy(valid)
        plan_legs = json.loads(same_match["legs_json"])
        plan_legs[1]["match_id"] = plan_legs[0]["match_id"]
        same_match["legs_json"] = json.dumps(plan_legs, ensure_ascii=False)
        same_match["bet_id"] = ledger_module.stable_bet_id(same_match)

        cases = (
            ("missing result legs", {**valid, "result_legs_json": ""}),
            ("malformed result JSON", malformed_result_json),
            ("missing result leg", missing_result_leg),
            ("missing result record", missing_result_record),
            ("naive result capture", naive_result_capture),
            ("mismatched result identity", mismatched_result_identity),
            ("unsupported plan leg", unsupported_leg),
            ("same-match plan legs", same_match),
        )
        for name, row in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    0, strategy._settled_sample_count([], [row], TARGET_DATE)
                )

    def test_settled_maturity_requires_valid_paid_or_observation_economics(self):
        observation = settled_value_single(
            "economic-observation", "had", THREE_WAY_SELECTIONS["h"]
        )
        paid_single = settled_value_single(
            "economic-paid", "had", THREE_WAY_SELECTIONS["h"], stake="20"
        )
        paid_parlay = settled_value_parlay()
        self.assertEqual(
            1, strategy._settled_sample_count([], [observation], TARGET_DATE)
        )
        self.assertEqual(
            1, strategy._settled_sample_count([paid_single], [], TARGET_DATE)
        )
        self.assertEqual(
            2, strategy._settled_sample_count([paid_parlay], [], TARGET_DATE)
        )

        def changed_leg(row, **changes):
            changed = deepcopy(row)
            legs = json.loads(changed["legs_json"])
            legs[0].update(changes)
            changed["legs_json"] = json.dumps(legs, ensure_ascii=False)
            return changed

        cases = (
            ("nonnumeric stake", {**paid_single, "stake": "not-money"}),
            ("negative stake", {**paid_single, "stake": "-2"}),
            ("odd stake", {**paid_single, "stake": "3"}),
            ("single cap", {**paid_single, "stake": "202"}),
            ("foreign paid source", {**paid_single, "odds_source": "external"}),
            ("missing paid record", {**paid_single, "odds_source_record_id": ""}),
            ("missing paid lock", {**paid_single, "locked_at_bjt": ""}),
            ("post-lock paid capture", {
                **paid_single,
                "odds_captured_at_bjt": "2026-07-17T13:05:00.000001+08:00",
            }),
            ("missing paid locked odds", {**paid_single, "locked_odds": ""}),
            ("paid odds mismatch", {**paid_single, "odds": "2.10"}),
            ("invalid paid Kelly", {**paid_single, "kelly_fraction": "0"}),
            ("foreign observation source", {
                **observation, "odds_source": "external"
            }),
            ("missing observation record", {
                **observation, "odds_source_record_id": ""
            }),
            ("missing observation lock", {
                **observation, "locked_at_bjt": ""
            }),
            ("post-lock observation capture", {
                **observation,
                "odds_captured_at_bjt": "2026-07-17T13:05:00.000001+08:00",
            }),
            ("observation odds mismatch", {**observation, "odds": "2.10"}),
            ("zero-stake parlay", {**paid_parlay, "stake": "0"}),
            ("parlay stake cap", {**paid_parlay, "stake": "32"}),
            ("missing parlay leg record", changed_leg(
                paid_parlay, odds_source_record_id=""
            )),
            ("foreign parlay leg source", changed_leg(
                paid_parlay, odds_source="external"
            )),
            ("post-lock parlay leg capture", changed_leg(
                paid_parlay,
                odds_captured_at_bjt="2026-07-17T13:05:00.000001+08:00",
            )),
            ("parlay product mismatch", {
                **paid_parlay, "odds": "6.01", "locked_odds": "6.01"
            }),
        )
        for name, row in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    0, strategy._settled_sample_count([], [row], TARGET_DATE)
                )

    def test_active_mode_audit_has_no_selected_shadow_rows(self):
        with self.strategy_context(value_config("active")):
            with (
                patch.object(strategy, "build_legacy_value_plan", return_value=([], [])),
                patch.object(strategy, "build_candidates", return_value=[candidate("active")]),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual([], outputs.shadow_plan)
        self.assertEqual([], outputs.audit["selected_shadow"])

    def test_active_mode_does_not_construct_or_finalize_legacy(self):
        with self.strategy_context(value_config("active")):
            with (
                patch.object(
                    strategy,
                    "build_legacy_value_plan",
                    side_effect=AssertionError("legacy construction ran"),
                ) as legacy_build,
                patch.object(strategy, "build_candidates", return_value=[candidate("active-only")]),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        legacy_build.assert_not_called()
        self.assertEqual(["value-v4"], [row["strategy_version"] for row in outputs.active_plan])
        self.assertEqual([], outputs.shadow_plan)
        self.assertFalse(any(
            row.get("strategy_version", "").startswith("legacy")
            for row in [*outputs.active_plan, *outputs.shadow_plan]
        ))

    def test_zero_candidate_audit_exposes_early_discard_diagnostics(self):
        markets, snapshot = market_fixture("audit", "had")
        snapshot["matches"][0]["team_a"] = "Mismatched Home"
        with self.strategy_context(value_config("active")):
            with (
                patch.object(strategy, "load_predictions", return_value=[prediction("audit")]),
                patch.object(strategy, "load_value_snapshot", return_value=snapshot),
                patch.object(strategy, "load_official_decision_markets", return_value=markets),
                patch.object(strategy, "build_legacy_value_plan", return_value=([], [])),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual([], outputs.active_plan)
        self.assertIn({
            "code": "prediction_identity_mismatch",
            "context": {"match_id": "audit", "prediction_index": 0},
        }, outputs.audit["rejection_reasons"])

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
                    patch.object(
                        strategy,
                        "read_valid_decision_bundle",
                        return_value={"synthetic": True},
                    ),
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
            observation_path = root / "output" / "observation_ledger.csv"
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", root / "output"),
                patch.object(strategy, "DATA_DIR", root / "data"),
                patch.object(strategy, "settle_ledger", return_value=ledger_path) as settle_mock,
                patch.object(
                    strategy,
                    "write_observation_ledger",
                    return_value=observation_path,
                ) as observation_mock,
                patch.object(strategy, "build_strategy_outputs", side_effect=AssertionError("generated")),
                patch.object(sys, "argv", [
                    "generate_betting_plan.py", "--date", str(TARGET_DATE), "--settle-only"
                ]),
            ):
                result = strategy.main()

        self.assertEqual(0, result)
        self.assertEqual(root, settle_mock.call_args.args[0])
        self.assertEqual({}, settle_mock.call_args.args[1])
        self.assertEqual({}, observation_mock.call_args.args[0])

    def test_settle_only_updates_canonical_observations_without_touching_paid_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            data = root / "data"
            output.mkdir()
            data.mkdir()
            observations = [
                strategy._candidate_plan_row(
                    candidate("obs-had", market_type="had"),
                    0,
                    locked_at=LOCKED_AT,
                    portfolio_rank=0,
                ),
                strategy._candidate_plan_row(
                    candidate("obs-hhad", market_type="hhad"),
                    0,
                    locked_at=LOCKED_AT,
                    portfolio_rank=0,
                ),
                strategy._candidate_plan_row(
                    candidate("obs-ttg", market_type="ttg"),
                    0,
                    locked_at=LOCKED_AT,
                    portfolio_rank=0,
                ),
            ]
            result_rows = [
                {
                    "match_id": match_id,
                    "result_status": "finished",
                    "home_goals": home,
                    "away_goals": away,
                    "result_source": "sporttery",
                    "source_record_id": f"result-{match_id}",
                    "captured_at_bjt": "2026-07-19T09:00:00+08:00",
                    "score_scope": "regular_time_90",
                    "settlement_minutes": "90",
                }
                for match_id, home, away in (
                    ("obs-had", "2", "1"),
                    ("obs-hhad", "1", "1"),
                    ("obs-ttg", "2", "0"),
                )
            ]
            with (data / "bet_results.csv").open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=list(result_rows[0]))
                writer.writeheader()
                writer.writerows(result_rows)
            ledger_module.write_ledger_atomic(output / "betting_ledger.csv", [])

            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", output),
                patch.object(strategy, "DATA_DIR", data),
            ):
                strategy.write_observation_plan(observations, TARGET_DATE)

                def settle_once():
                    with (
                        patch.object(
                            strategy,
                            "build_strategy_outputs",
                            side_effect=AssertionError("generation ran"),
                        ),
                        patch.object(sys, "argv", [
                            "generate_betting_plan.py",
                            "--date",
                            str(TARGET_DATE),
                            "--settle-only",
                        ]),
                    ):
                        self.assertEqual(0, strategy.main())

                settle_once()
                observation_path = output / "observation_ledger.csv"
                paid_path = output / "betting_ledger.csv"
                first_observation_bytes = ledger_module.resolve_ledger_path(
                    observation_path
                ).read_bytes()
                first_paid_bytes = ledger_module.resolve_ledger_path(
                    paid_path
                ).read_bytes()
                with ledger_module.resolve_ledger_path(observation_path).open(
                    encoding="utf-8-sig", newline=""
                ) as handle:
                    rows = list(csv.DictReader(handle))

                settle_once()

            self.assertEqual(
                first_observation_bytes,
                ledger_module.resolve_ledger_path(observation_path).read_bytes(),
            )
            self.assertEqual(
                first_paid_bytes,
                ledger_module.resolve_ledger_path(paid_path).read_bytes(),
            )
            self.assertEqual(3, len(rows))
            self.assertEqual({ledger_module.WON}, {row["status"] for row in rows})
            for field in (
                "bet_id", "match_id", "market_type", "market_line",
                "odds_source", "odds_source_record_id", "odds_captured_at_bjt",
                "locked_odds", "model_version", "raw_probability",
                "calibrated_probability", "official_market_probability",
            ):
                self.assertTrue(all(row.get(field, "") != "" or field == "market_line" for row in rows))
            self.assertEqual(
                3,
                strategy._settled_sample_count(
                    [], rows, TARGET_DATE + timedelta(days=1)
                ),
            )
            with ledger_module.resolve_ledger_path(paid_path).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                self.assertEqual([], list(csv.DictReader(handle)))

    def test_observation_plan_loader_ignores_pre_value_v4_history(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            output.mkdir()
            rows_by_date = {
                "2026-07-17": {
                    "strategy_version": "value-v3",
                    "match_id": "legacy-observation",
                },
                "2026-07-18": {
                    "strategy_version": "value-v4",
                    "match_id": "canonical-observation",
                },
            }
            for date_text, row in rows_by_date.items():
                with (output / f"observation_plan_{date_text}.csv").open(
                    "w", encoding="utf-8-sig", newline=""
                ) as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerow(row)

            with patch.object(strategy, "OUTPUT_DIR", output):
                rows = strategy.load_all_observation_plans()

        self.assertEqual(
            ["canonical-observation"],
            [row["match_id"] for row in rows],
        )

    def test_settle_only_without_observation_plans_writes_valid_empty_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            data = root / "data"
            output.mkdir()
            data.mkdir()
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", output),
                patch.object(strategy, "DATA_DIR", data),
                patch.object(sys, "argv", [
                    "generate_betting_plan.py", "--date", str(TARGET_DATE), "--settle-only"
                ]),
            ):
                self.assertEqual(0, strategy.main())

            with ledger_module.resolve_ledger_path(
                output / "observation_ledger.csv"
            ).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                reader = csv.DictReader(handle)
                self.assertIn("bet_id", reader.fieldnames)
                self.assertIn("market_type", reader.fieldnames)
                self.assertEqual([], list(reader))

    def test_empty_observation_plan_uses_full_canonical_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            output.mkdir()
            with patch.object(strategy, "OUTPUT_DIR", output):
                path = strategy.write_observation_plan([], TARGET_DATE)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(list(strategy.PLAN_FIELD_ORDER), reader.fieldnames)
                self.assertEqual([], list(reader))

    def test_legacy_parlay_finalization_serializes_exact_decimal_product(self):
        markets = {}
        for match_id, home_odds in (("decimal-a", "2.10"), ("decimal-b", "2.20")):
            market = normalize_market(match_id, "had", {
                "h": home_odds,
                "d": "3.20",
                "a": "3.80",
                "source": "sporttery",
                "source_record_id": f"record-{match_id}",
                "captured_at_bjt": CAPTURED_AT,
            })
            self.assertIsNotNone(market)
            markets[match_id] = {"had": market}
        legs = [
            {
                "match_id": match_id,
                "selection": THREE_WAY_SELECTIONS["h"],
                "odds": odds,
            }
            for match_id, odds in (("decimal-a", 2.1), ("decimal-b", 2.2))
        ]
        raw = {
            "date": TARGET_DATE.isoformat(),
            "strategy_version": "legacy-v3",
            "play": "legacy combo display",
            "selection": "two legs",
            "legs_json": json.dumps(legs, ensure_ascii=False),
            "odds": 2.1 * 2.2,
            "probability": 0.40,
            "stake": 10,
        }

        finalized = strategy._finalize_legacy_plan([raw], markets, LOCKED_AT)[0]
        exact_product = Decimal("2.10") * Decimal("2.20")

        self.assertEqual(exact_product, Decimal(str(finalized["odds"])))
        self.assertEqual(exact_product, Decimal(str(finalized["locked_odds"])))
        self.assertEqual(
            exact_product,
            Decimal(json.loads(finalized["legs_json"])[0]["odds"])
            * Decimal(json.loads(finalized["legs_json"])[1]["odds"]),
        )

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
            bundle = synthetic_decision_bundle(root)
            with patch("plan_lock.read_valid_decision_bundle", return_value=bundle):
                lock_plan(root, TARGET_DATE, LOCKED_AT)
                ledger_path = ingest_date(root, TARGET_DATE)
            with ledger_module.resolve_ledger_path(ledger_path).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
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

    def test_normalization_diagnostics_preserve_invalid_siblings_in_final_audit(self):
        snapshot = {
            "target_date": TARGET_DATE.isoformat(),
            "capture_phase": "decision",
            "captured_at": CAPTURED_AT,
            "source": "sporttery",
            "source_record_id": "mixed-snapshot",
            "matches": [
                {
                    "match_id": "mixed",
                    "markets": {
                        "had": {"h": "3.00", "d": "3.00", "a": "3.00"},
                        "hhad": "malformed",
                        "ttg": {"s0": "8.00"},
                        "score": {"1-0": "8.00"},
                    },
                },
                {"match_id": "", "markets": {}},
                {"markets": {}},
                {"match_id": "bad-markets", "markets": []},
            ],
        }
        diagnostics = []
        markets = strategy.load_official_decision_markets(
            TARGET_DATE, snapshot=snapshot, diagnostics=diagnostics
        )

        self.assertIn("had", markets["mixed"])
        self.assertEqual(
            {
                "market_payload_invalid",
                "market_normalization_rejected",
                "snapshot_match_id_invalid",
                "snapshot_markets_invalid",
                "unsupported_market_key",
            },
            {item["code"] for item in diagnostics},
        )
        self.assertIn(
            {
                "code": "market_payload_invalid",
                "context": {"match_id": "mixed", "market_type": "hhad"},
            },
            diagnostics,
        )
        self.assertIn(
            {
                "code": "snapshot_match_id_invalid",
                "context": {"match_id": "", "match_index": 1},
            },
            diagnostics,
        )
        self.assertIn(
            {
                "code": "market_normalization_rejected",
                "context": {"match_id": "mixed", "market_type": "ttg"},
            },
            diagnostics,
        )

        real_loader = strategy.load_official_decision_markets

        def reject_candidates(
            predictions, markets, snapshot, config, calibrations, *, diagnostics=None
        ):
            diagnostics.append({
                "code": "candidate_test_rejection",
                "context": {"match_id": "mixed"},
            })
            return []

        with self.strategy_context(value_config("active")):
            with (
                patch.object(strategy, "load_value_snapshot", return_value=snapshot),
                patch.object(
                    strategy,
                    "load_official_decision_markets",
                    wraps=real_loader,
                ),
                patch.object(strategy, "build_candidates", side_effect=reject_candidates),
            ):
                outputs = strategy.build_strategy_outputs(
                    TARGET_DATE, locked_at=LOCKED_AT
                )

        audit_codes = {
            item["code"]
            for item in outputs.audit["rejection_reasons"]
            if isinstance(item, dict)
        }
        self.assertIn("market_payload_invalid", audit_codes)
        self.assertIn("market_normalization_rejected", audit_codes)
        self.assertIn("candidate_test_rejection", audit_codes)

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
            bundle = synthetic_decision_bundle(root)
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
                patch("plan_lock.read_valid_decision_bundle", return_value=bundle),
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", output),
                patch.object(strategy, "DATA_DIR", data),
                patch.object(strategy, "build_strategy_outputs", side_effect=AssertionError("regenerated")),
                patch.object(sys, "argv", ["generate_betting_plan.py", "--date", str(TARGET_DATE), "--locked-at", LOCKED_AT.isoformat()]),
            ):
                lock_plan(root, TARGET_DATE, LOCKED_AT)
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

    def test_generated_parlay_legs_persist_official_evidence(self):
        candidates = []
        for index in range(6):
            official_odds = (Decimal("2.123456789") if index % 2 else Decimal("3.987654321"))
            candidates.append(replace(
                candidate(f"evidence-{index}"),
                official_odds=float(official_odds),
                expected_value=0.60 * float(official_odds) - 1.0,
            ))
        with self.strategy_context(value_config()):
            with (
                patch.object(strategy, "build_legacy_value_plan", return_value=([], [])),
                patch.object(strategy, "build_candidates", return_value=candidates),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        parlay = next(row for row in outputs.shadow_plan if row["market_type"] == "parlay")
        legs = json.loads(parlay["legs_json"])
        self.assertEqual(2, len(legs))
        exact_product = Decimal("1")
        for leg in legs:
            self.assertEqual("sporttery", leg["odds_source"])
            self.assertEqual(f"decision-{leg['match_id']}", leg["odds_source_record_id"])
            self.assertEqual(CAPTURED_AT, leg["odds_captured_at_bjt"])
            self.assertGreater(float(leg["odds"]), 1.0)
            self.assertEqual(leg["odds"], leg["locked_odds"])
            self.assertGreater(float(leg["expected_value"]), 0.0)
            self.assertEqual(leg["expected_value"], leg["net_ev"])
            exact_product *= Decimal(leg["odds"])
        self.assertEqual(exact_product, Decimal(str(parlay["odds"])))
        self.assertEqual(exact_product, Decimal(str(parlay["locked_odds"])))

    def test_hhad_first_parlay_keeps_line_only_on_the_leg(self):
        first = candidate("line-first", market_type="hhad")
        second = candidate("line-second", market_type="had")
        legs = [
            {
                "match_id": leg.match_id,
                "market_type": leg.market_type,
                "selection": leg.selection,
                "line": "" if leg.line is None else str(leg.line),
            }
            for leg in (first, second)
        ]

        row = strategy._candidate_plan_row(
            first,
            10,
            locked_at=LOCKED_AT,
            portfolio_rank=1,
            market_type="parlay",
            play="PARLAY",
            selection="two legs",
            odds=Decimal("9.00"),
            probability=0.36,
            legs=legs,
        )

        self.assertEqual("", row["market_line"])
        self.assertEqual("1", json.loads(row["legs_json"])[0]["line"])

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
        self.assertEqual({
            "bankroll": 5000.0,
            "reference_bankroll": 5000.0,
            "kelly_fraction": 0.25,
            "stake_unit": 2,
            "max_match_exposure": 200,
            "max_single_stake": 200,
            "single_budget_cap": 200,
            "max_single_count": 2,
            "max_parlay_count": 1,
            "max_parlay_stake": 30,
            "max_daily_combo_stake": 30,
            "max_daily_stake": 500,
            "monthly_budget_cap": 5000,
            "monthly_stop_loss": 5000,
            "settled_samples": 0,
            "strict_until_samples": 100,
            "strict_mode": True,
        }, outputs.audit["risk_caps"])
        checks = {check["name"]: check for check in outputs.audit["risk_checks"]}
        self.assertEqual(len(checks), len(outputs.audit["risk_checks"]))
        self.assertTrue(checks["kelly_fraction_cap"]["passed"])
        self.assertLessEqual(checks["kelly_fraction_cap"]["value"], 0.25)
        self.assertTrue(checks["stake_unit"]["passed"])
        self.assertTrue(checks["max_single_count"]["passed"])
        self.assertTrue(checks["max_parlay_count"]["passed"])
        self.assertLessEqual(checks["max_parlay_count"]["value"], 1)

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
            patch.object(
                strategy,
                "assert_activation_ready",
                return_value={"passed": True},
                create=True,
            ),
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
