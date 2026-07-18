import csv
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import generate_betting_plan as strategy
from audit_shadow_portfolio import (
    audit_generated_portfolios,
    run_audit,
    validate_audit_payload,
)
from betting_ledger import stable_bet_id
from decision_bundle import create_decision_bundle, write_prediction_metadata
from official_markets import THREE_WAY_SELECTIONS


BEIJING = timezone(timedelta(hours=8))


def config(mode="shadow"):
    return {
        "max_daily_budget": 500,
        "value_strategy": {
            "activation_mode": mode,
            "stake_unit": 2,
            "max_match_exposure": 200,
            "max_daily_combo_stake": 30,
        },
        "simulation_account": {
            "mode": "simulation",
            "monthly_budget_cap": 5000,
            "real_money_automation": False,
        },
    }


def single_row(
    report_date="2026-07-11",
    match_id="m1",
    *,
    market_type="had",
    play="HAD",
    selection=None,
    line="",
    stake=20,
    source="sporttery",
    expected_value=0.10,
):
    row = {
        "date": report_date,
        "report_date": report_date,
        "strategy_version": "value-v4",
        "model_version": "value-v4",
        "match_id": match_id,
        "play": play,
        "market_type": market_type,
        "market_line": line,
        "selection": selection or THREE_WAY_SELECTIONS["h"],
        "legs_json": "[]",
        "odds_source": source,
        "odds_source_record_id": f"snapshot#{match_id}:{market_type}",
        "odds_captured_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_odds": "2.00",
        "odds": "2.00",
        "conservative_probability": 0.55,
        "expected_value": expected_value,
        "net_ev": expected_value,
        "stake": stake,
        "profit": -float(stake),
        "return": 0,
    }
    row["bet_id"] = stable_bet_id(row)
    return row


def parlay_row(
    report_date="2026-07-11",
    *,
    match_ids=("p1", "p2"),
    stake=20,
    source="sporttery",
    expected_value=0.10,
):
    legs = [
        {
            "match_id": match_id,
            "market_type": "had",
            "selection": THREE_WAY_SELECTIONS["h"],
            "line": "",
            "odds": "2.00",
            "locked_odds": "2.00",
            "odds_source": source,
            "odds_source_record_id": f"snapshot#{match_id}:had",
            "odds_captured_at_bjt": f"{report_date}T12:00:00+08:00",
            "expected_value": "0.10",
            "net_ev": "0.10",
        }
        for match_id in match_ids
    ]
    row = {
        "date": report_date,
        "report_date": report_date,
        "strategy_version": "value-v4",
        "model_version": "value-v4",
        "match_id": "",
        "play": "PARLAY",
        "market_type": "parlay",
        "market_line": "",
        "selection": " + ".join(leg["selection"] for leg in legs),
        "legs_json": json.dumps(legs, ensure_ascii=False, sort_keys=True),
        "odds_source": source,
        "odds_source_record_id": json.dumps(
            sorted(leg["odds_source_record_id"] for leg in legs)
        ),
        "odds_captured_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_odds": str(2 ** len(legs)),
        "odds": str(2 ** len(legs)),
        "conservative_probability": 0.30,
        "expected_value": expected_value,
        "net_ev": expected_value,
        "stake": stake,
        "profit": -float(stake),
        "return": 0,
    }
    try:
        row["bet_id"] = stable_bet_id(row)
    except ValueError:
        row["bet_id"] = f"invalid-parlay-{len(legs)}"
    return row


def violation_codes(payload):
    return {item["code"] for item in payload["violations"]}


def build_result(
    report_date="2026-07-11",
    *,
    plan=None,
    candidate_count=1,
    observation_count=1,
    diagnostics=None,
):
    plan = (
        [single_row(report_date, match_id=f"match-{report_date}")]
        if plan is None
        else plan
    )
    diagnostics = list(diagnostics or [])
    return strategy.ValueV4BuildResult(
        plan=plan,
        observations=[{} for _ in range(observation_count)],
        candidates=[object() for _ in range(candidate_count)],
        diagnostics=diagnostics,
        audit={
            "candidate_count": candidate_count,
            "observation_count": observation_count,
            "diagnostic_count": len(diagnostics),
        },
    )


class MechanicalPortfolioAuditTest(unittest.TestCase):
    def test_mechanical_gate_does_not_require_positive_historical_roi(self):
        payload = audit_generated_portfolios(
            {"2026-07-11": [single_row()]}, config()
        )

        self.assertTrue(payload["passed"])
        self.assertEqual([], payload["violations"])
        self.assertNotIn("roi", json.dumps(payload).lower())

    def test_zero_checked_dates_fails_closed(self):
        payload = audit_generated_portfolios({}, config())

        self.assertFalse(payload["passed"])
        self.assertIn("zero_checked_dates", violation_codes(payload))

    def test_zero_stake_paid_row_fails_but_an_empty_paid_list_is_valid(self):
        zero = audit_generated_portfolios(
            {"2026-07-11": [single_row(stake=0)]}, config()
        )
        empty = audit_generated_portfolios({"2026-07-11": []}, config())

        self.assertIn("zero_stake_paid_row", violation_codes(zero))
        self.assertFalse(zero["passed"])
        self.assertTrue(empty["passed"])

    def test_nonnumeric_negative_and_odd_stakes_fail_closed(self):
        for stake, expected in (
            ("not-money", "stake_invalid"),
            (-2, "stake_invalid"),
            (3, "stake_unit"),
        ):
            with self.subTest(stake=stake):
                row = single_row()
                row["stake"] = stake
                payload = audit_generated_portfolios(
                    {"2026-07-11": [row]}, config()
                )
                self.assertIn(expected, violation_codes(payload))
                if stake == 3:
                    self.assertEqual(3, payload["maxima"]["daily_stake"])

    def test_single_display_odds_must_equal_locked_odds(self):
        row = single_row()
        row["odds"] = "2.01"

        payload = audit_generated_portfolios({"2026-07-11": [row]}, config())

        self.assertIn("invalid_locked_price_evidence", violation_codes(payload))

    def test_each_required_row_violation_fails_the_gate(self):
        cases = {}

        forbidden = single_row()
        forbidden.update(play="SCORE", market_type="score", selection="1-0")
        forbidden["bet_id"] = stable_bet_id(forbidden)
        cases["forbidden_play"] = [forbidden]

        cases["parlay_leg_count"] = [parlay_row(match_ids=("p1", "p2", "p3"))]

        non_domestic = single_row(source="professional")
        cases["non_domestic_odds"] = [non_domestic]

        cases["nonpositive_configured_ev"] = [single_row(expected_value=0)]
        cases["stake_unit"] = [single_row(stake=3)]

        exposure_a = single_row(match_id="same", stake=102)
        exposure_b = single_row(
            match_id="same", market_type="ttg", play="TTG", selection="2球", stake=102
        )
        cases["match_exposure"] = [exposure_a, exposure_b]

        cases["parlay_stake"] = [parlay_row(stake=32)]

        daily_rows = [single_row(match_id=f"daily-{index}", stake=100) for index in range(6)]
        cases["daily_stake"] = daily_rows

        duplicate = single_row()
        cases["duplicate_bet_id"] = [duplicate, deepcopy(duplicate)]

        for expected_code, rows in cases.items():
            with self.subTest(expected_code=expected_code):
                payload = audit_generated_portfolios({"2026-07-11": rows}, config())
                self.assertFalse(payload["passed"])
                self.assertIn(expected_code, violation_codes(payload))

    def test_non_exact_parlays_and_non_domestic_legs_fail(self):
        one_leg = parlay_row(match_ids=("only",))
        bad_leg_source = parlay_row()
        legs = json.loads(bad_leg_source["legs_json"])
        legs[1]["odds_source"] = "professional"
        bad_leg_source["legs_json"] = json.dumps(legs, ensure_ascii=False, sort_keys=True)

        one_leg_result = audit_generated_portfolios(
            {"2026-07-11": [one_leg]}, config()
        )
        source_result = audit_generated_portfolios(
            {"2026-07-11": [bad_leg_source]}, config()
        )

        self.assertIn("parlay_leg_count", violation_codes(one_leg_result))
        self.assertIn("non_domestic_odds", violation_codes(source_result))

    def test_invalid_paid_market_identity_is_detected_before_maxima(self):
        row = single_row(selection="not-a-had-selection", stake=600)
        row["bet_id"] = stable_bet_id(row)

        payload = audit_generated_portfolios({"2026-07-11": [row]}, config())

        self.assertIn("invalid_market_identity", violation_codes(payload))
        self.assertEqual(600, payload["maxima"]["match_exposure"])
        self.assertEqual(600, payload["maxima"]["daily_stake"])

    def test_every_positive_numeric_stake_counts_in_maxima_despite_other_errors(self):
        bad_parlay = parlay_row(stake=40)
        legs = json.loads(bad_parlay["legs_json"])
        legs[0]["selection"] = "unsupported"
        bad_parlay["legs_json"] = json.dumps(legs, ensure_ascii=False, sort_keys=True)
        bad_parlay["bet_id"] = "invalid-but-present"

        payload = audit_generated_portfolios(
            {"2026-07-11": [bad_parlay]}, config()
        )

        self.assertEqual(40, payload["maxima"]["parlay_stake"])
        self.assertEqual(40, payload["maxima"]["daily_stake"])
        self.assertEqual(40, payload["maxima"]["match_exposure"])

    def test_parlay_legs_require_matching_source_odds_and_positive_equal_ev(self):
        cases = {}

        source_mismatch = parlay_row()
        legs = json.loads(source_mismatch["legs_json"])
        legs[0]["odds_source"] = "zgzcw"
        source_mismatch["legs_json"] = json.dumps(legs, ensure_ascii=False, sort_keys=True)
        cases["parlay_leg_source_mismatch"] = source_mismatch

        odds_mismatch = parlay_row()
        legs = json.loads(odds_mismatch["legs_json"])
        legs[0]["locked_odds"] = "2.01"
        odds_mismatch["legs_json"] = json.dumps(legs, ensure_ascii=False, sort_keys=True)
        cases["invalid_locked_price_evidence"] = odds_mismatch

        nonpositive_ev = parlay_row()
        legs = json.loads(nonpositive_ev["legs_json"])
        legs[0]["expected_value"] = "0"
        legs[0]["net_ev"] = "0"
        nonpositive_ev["legs_json"] = json.dumps(legs, ensure_ascii=False, sort_keys=True)
        cases["nonpositive_configured_ev"] = nonpositive_ev

        unequal_ev = parlay_row()
        legs = json.loads(unequal_ev["legs_json"])
        legs[0]["net_ev"] = "0.11"
        unequal_ev["legs_json"] = json.dumps(legs, ensure_ascii=False, sort_keys=True)
        cases["inconsistent_configured_ev"] = unequal_ev

        for expected, row in cases.items():
            with self.subTest(expected=expected):
                payload = audit_generated_portfolios(
                    {"2026-07-11": [row]}, config()
                )
                self.assertIn(expected, violation_codes(payload))

    def test_monthly_stake_over_cap_fails(self):
        portfolios = {}
        for offset in range(11):
            report_date = (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
            portfolios[report_date] = [
                single_row(report_date, match_id=f"m-{offset}-{index}", stake=100)
                for index in range(5)
            ]

        payload = audit_generated_portfolios(portfolios, config())

        self.assertIn("monthly_stake", violation_codes(payload))
        self.assertEqual(5500, payload["maxima"]["monthly_stake"])


class RepositoryAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data" / "odds_snapshots").mkdir(parents=True)
        (self.root / "output").mkdir()
        (self.root / "betting_config.json").write_text(
            json.dumps(config()), encoding="utf-8"
        )
        (self.root / "config.json").write_text("{}\n", encoding="utf-8")
        for name in (
            "predict_today.py",
            "generate_betting_plan.py",
            "value_candidates.py",
            "value_portfolio.py",
            "official_markets.py",
            "betting_ledger.py",
            "strategy_controls.py",
        ):
            (self.root / name).write_text(f"MODULE = {name!r}\n", encoding="utf-8")
        (self.root / "data" / "team_ratings.csv").write_text(
            "team,elo\nHome,1500\nAway,1490\n", encoding="utf-8"
        )
        (self.root / "output" / "betting_ledger.csv").write_text(
            "placeholder\n", encoding="utf-8"
        )
        (self.root / "output" / "observation_ledger.csv").write_text(
            "placeholder\n", encoding="utf-8"
        )
        (self.root / "data" / "draw_training_samples.csv").write_text(
            "placeholder\n", encoding="utf-8"
        )
        with (self.root / "data" / "fixtures.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "date", "match_id", "team_a", "team_b", "kickoff_at"
                ],
            )
            writer.writeheader()
            for report_date in ("2026-07-11", "2026-07-12", "2026-07-13"):
                writer.writerow({
                    "date": report_date,
                    "match_id": f"match-{report_date}",
                    "team_a": "Home",
                    "team_b": "Away",
                    "kickoff_at": f"{report_date}T18:00:00+08:00",
                })

    def tearDown(self):
        self.temp.cleanup()

    def _write_common_evidence(self, report_date):
        match_id = f"match-{report_date}"
        (self.root / "output" / f"predictions_{report_date}.csv").write_text(
            (
                "date,match_id,team_a,team_b,kickoff_at,p_a,p_draw,p_b,xg_a,xg_b\n"
                f"{report_date},{match_id},Home,Away,"
                f"{report_date}T18:00:00+08:00,0.70,0.20,0.10,2.0,0.5\n"
            ),
            encoding="utf-8",
        )
        (self.root / "data" / f"sporttery_odds_{report_date}.json").write_text(
            json.dumps({match_id: {"had": {"h": "2.00", "d": "3.20", "a": "3.50"}}}),
            encoding="utf-8",
        )

    def _write_snapshot(self, report_date, *, source="sporttery", valid=True):
        match_id = f"match-{report_date}"
        def record(path):
            content = path.read_bytes()
            return {
                "path": path.relative_to(self.root).as_posix(),
                "sha256": sha256(content).hexdigest(),
                "bytes": len(content),
            }
        manifest_path = (
            self.root / "data" / "import_manifests" / f"{report_date}.json"
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({
                "schema_version": 1,
                "target_date": report_date,
                "source": source,
                "imported_at_bjt": f"{report_date}T11:58:00+08:00",
                "fixtures": record(self.root / "data" / "fixtures.csv"),
                "odds": record(
                    self.root / "data" / f"sporttery_odds_{report_date}.json"
                ),
            }),
            encoding="utf-8",
        )
        payload = {
            "target_date": report_date,
            "captured_at": f"{report_date}T12:00:00+08:00",
            "capture_phase": "decision",
            "source": source,
            "import_manifest": record(manifest_path),
            "source_record_id": f"snapshot-{report_date}",
            "matches": [
                {
                    "match_id": match_id,
                    "team_a": "Home",
                    "team_b": "Away",
                    "kickoff_at": f"{report_date}T18:00:00+08:00",
                    "markets": {
                        "had": {"h": "2.00", "d": "3.20", "a": "3.50"},
                        "hhad": {},
                        "ttg": {},
                    },
                    "single_eligibility": {"had": True, "hhad": False, "ttg": False},
                }
            ],
        }
        path = (
            self.root
            / "data"
            / "odds_snapshots"
            / f"{report_date}-120000-decision.json"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
        target = date.fromisoformat(report_date)
        generated_at = datetime.fromisoformat(
            f"{report_date}T11:59:00+08:00"
        )
        locked_at = datetime.fromisoformat(f"{report_date}T12:00:00+08:00")
        write_prediction_metadata(self.root, target, generated_at)
        create_decision_bundle(self.root, target, locked_at)
        if not valid:
            payload["matches"][0].pop("match_id")
            path.write_text(json.dumps(payload), encoding="utf-8")

    def test_repository_audit_checks_and_classifies_dates_deterministically(self):
        for report_date in ("2026-07-11", "2026-07-12", "2026-07-13"):
            self._write_common_evidence(report_date)
        self._write_snapshot("2026-07-11")
        self._write_snapshot("2026-07-13", valid=False)
        calls = []

        def builder(target_date, **inputs):
            calls.append((target_date, inputs))
            return build_result(target_date.isoformat())

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 13),
            plan_builder=builder,
        )

        self.assertTrue(payload["passed"])
        self.assertEqual(["2026-07-11"], payload["checked_dates"])
        self.assertEqual(["2026-07-12"], payload["excluded_missing"])
        self.assertEqual(["2026-07-13"], payload["excluded_invalid"])
        self.assertEqual(1, len(calls))
        called_date, inputs = calls[0]
        self.assertEqual(date(2026, 7, 11), called_date)
        self.assertEqual(datetime(2026, 7, 11, 12, tzinfo=BEIJING), inputs["locked_at"])
        self.assertEqual("2026-07-11T12:00:00+08:00", inputs["snapshot"]["captured_at"])
        self.assertEqual(
            "data/odds_snapshots/2026-07-11-120000-decision.json",
            inputs["snapshot"]["_snapshot_record_id"],
        )
        self.assertEqual("2026-07-11T18:00:00+08:00", inputs["predictions"][0]["kickoff_at"])
        self.assertEqual([], inputs["paid_history"])
        self.assertEqual([], inputs["observation_history"])
        self.assertEqual([], inputs["training_samples"])
        coverage = {item["date"]: item for item in payload["source_coverage"]}
        self.assertEqual("checked", coverage["2026-07-11"]["status"])
        self.assertTrue(coverage["2026-07-11"]["sporttery"])
        self.assertEqual("excluded_missing", coverage["2026-07-12"]["status"])
        self.assertEqual("excluded_invalid", coverage["2026-07-13"]["status"])
        validate_audit_payload(payload)
        persisted = json.loads(
            (self.root / "output" / "shadow_portfolio_activation_audit.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(payload, persisted)

    def test_verified_domestic_fallback_is_reported_separately(self):
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11", source="zgzcw")

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, **inputs: build_result(
                plan=[single_row(source="zgzcw")]
            ),
        )

        coverage = payload["source_coverage"][0]
        self.assertFalse(coverage["sporttery"])
        self.assertTrue(coverage["verified_domestic_fallback"])

    def test_audit_in_active_mode_preserves_historical_plan_lock_and_ledger_bytes(self):
        (self.root / "betting_config.json").write_text(
            json.dumps(config(mode="active")), encoding="utf-8"
        )
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11")
        protected = {
            self.root / "output" / "betting_plan_2026-07-10.csv": b"old-plan\n",
            self.root / "output" / "plan_lock_2026-07-10.json": b'{"locked":true}\n',
            self.root / "output" / "betting_ledger.csv": (
                b"strategy_version,locked_odds,stake,bet_id\n"
                b"legacy-v3,2.10,20,old-id\n"
            ),
        }
        for path, content in protected.items():
            path.write_bytes(content)

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, **inputs: build_result(),
        )

        self.assertTrue(payload["historical_artifacts_unchanged"])
        for path, content in protected.items():
            self.assertEqual(content, path.read_bytes())

    def test_valid_empty_plan_requires_nonzero_reconstruction_and_no_fatal_diagnostics(self):
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11")

        valid_empty = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, **inputs: build_result(plan=[]),
        )
        self.assertTrue(valid_empty["passed"])
        self.assertEqual(0, valid_empty["counts"]["paid_rows"])
        self.assertEqual(1, valid_empty["counts"]["candidates"])
        self.assertEqual(1, valid_empty["counts"]["observations"])

        unproven = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, **inputs: build_result(
                plan=[], candidate_count=0, observation_count=0
            ),
        )
        self.assertFalse(unproven["passed"])
        self.assertEqual([], unproven["checked_dates"])
        reasons = unproven["excluded_dates"][0]["reasons"]
        self.assertIn("portfolio_reconstruction_unproven", reasons)

        fatal = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, **inputs: build_result(
                plan=[],
                diagnostics=[{
                    "code": "prediction_identity_mismatch",
                    "context": {"match_id": "match-2026-07-11"},
                }],
            ),
        )
        self.assertFalse(fatal["passed"])
        self.assertIn(
            "fatal_reconstruction_diagnostics",
            fatal["excluded_dates"][0]["reasons"],
        )

    def test_prediction_kickoff_must_exactly_match_snapshot_identity(self):
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11")
        prediction_path = self.root / "output" / "predictions_2026-07-11.csv"
        prediction_path.write_text(
            prediction_path.read_text(encoding="utf-8").replace(
                "2026-07-11T18:00:00+08:00",
                "2026-07-11T18:00:01+08:00",
            ),
            encoding="utf-8",
        )
        builder = unittest.mock.Mock(side_effect=AssertionError("builder ran"))

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=builder,
        )

        self.assertFalse(payload["passed"])
        self.assertEqual(["2026-07-11"], payload["excluded_invalid"])
        self.assertEqual(
            ["activation_evidence_invalid:ValueError"],
            payload["excluded_dates"][0]["reasons"],
        )
        builder.assert_not_called()

    def test_audit_injects_root_histories_and_hashes_every_rebuild_input(self):
        self._write_common_evidence("2026-07-11")
        files = {
            self.root / "output" / "betting_ledger.csv": (
                "date,locked_at_bjt,stake\n"
                "2026-07-10,2026-07-10T12:00:00+08:00,20\n"
            ),
            self.root / "output" / "observation_ledger.csv": (
                "date,locked_at_bjt,stake\n"
                "2026-07-10,2026-07-10T12:00:00+08:00,0\n"
            ),
            self.root / "data" / "draw_training_samples.csv": (
                "date,match_id,captured_at,kickoff_at,outcome,base_draw_probability\n"
                "2026-07-10,training,2026-07-10T12:00:00+08:00,"
                "2026-07-10T18:00:00+08:00,1,0.3\n"
            ),
        }
        for path, content in files.items():
            path.write_text(content, encoding="utf-8")
        self._write_snapshot("2026-07-11")
        captured = {}

        def builder(target_date, **inputs):
            captured.update(inputs)
            return build_result()

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=builder,
        )

        self.assertEqual("20", captured["paid_history"][0]["stake"])
        self.assertEqual("0", captured["observation_history"][0]["stake"])
        self.assertEqual("training", captured["training_samples"][0]["match_id"])
        rebuild_inputs = payload["evidence"][0]["rebuild_inputs"]
        for name in ("paid_history", "observation_history", "training_samples"):
            path = self.root / rebuild_inputs[name]["path"]
            self.assertEqual(path.stat().st_size, rebuild_inputs[name]["bytes"])
            self.assertEqual(
                sha256(path.read_bytes()).hexdigest(),
                rebuild_inputs[name]["sha256"],
            )

    def test_active_routing_rehashes_persisted_audit_evidence_and_fails_stale(self):
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11")
        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, **inputs: build_result(plan=[]),
        )
        self.assertTrue(payload["passed"])
        (self.root / "betting_config.json").write_text(
            json.dumps(config(mode="active")), encoding="utf-8"
        )

        with (
            patch.object(strategy, "ROOT", self.root),
            patch.object(strategy, "OUTPUT_DIR", self.root / "output"),
            patch.object(strategy, "DATA_DIR", self.root / "data"),
            patch.object(
                strategy, "build_value_v4_plan", return_value=([], [])
            ) as build,
        ):
            outputs = strategy.build_strategy_outputs(
                date(2026, 7, 11),
                locked_at=datetime(2026, 7, 11, 12, tzinfo=BEIJING),
            )
            self.assertEqual([], outputs.active_plan)
            build.assert_called_once()

            changed_config = config(mode="active")
            changed_config["max_daily_budget"] = 498
            (self.root / "betting_config.json").write_text(
                json.dumps(changed_config), encoding="utf-8"
            )
            build.reset_mock()
            with self.assertRaisesRegex(ValueError, "configuration"):
                strategy.build_strategy_outputs(
                    date(2026, 7, 11),
                    locked_at=datetime(2026, 7, 11, 12, tzinfo=BEIJING),
                )
            build.assert_not_called()
            (self.root / "betting_config.json").write_text(
                json.dumps(config(mode="active")), encoding="utf-8"
            )

            audit_path = (
                self.root / "output" / "shadow_portfolio_activation_audit.json"
            )
            saved_audit = audit_path.read_text(encoding="utf-8")
            malformed_audit = json.loads(saved_audit)
            malformed_audit["source_coverage"] = []
            audit_path.write_text(json.dumps(malformed_audit), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source coverage"):
                strategy.build_strategy_outputs(
                    date(2026, 7, 11),
                    locked_at=datetime(2026, 7, 11, 12, tzinfo=BEIJING),
                )
            build.assert_not_called()
            audit_path.write_text(saved_audit, encoding="utf-8")

    def test_readiness_uses_immutable_as_of_extracts_after_shared_files_advance(self):
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11", source="zgzcw")
        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, **inputs: build_result(plan=[]),
        )
        self.assertTrue(payload["passed"])
        files = payload["evidence"][0]["activation_evidence"]["files"]
        self.assertTrue(files)
        for record in files.values():
            self.assertTrue(
                record["path"].startswith("output/activation_evidence/2026-07-11/")
            )

        (self.root / "betting_config.json").write_text(
            json.dumps(config(mode="active")), encoding="utf-8"
        )
        strategy.assert_activation_ready(self.root)

        with (self.root / "data" / "fixtures.csv").open(
            "a", encoding="utf-8", newline=""
        ) as handle:
            handle.write(
                "2026-07-14,next-day,Next Home,Next Away,"
                "2026-07-14T18:00:00+08:00\n"
            )
        (self.root / "output" / "betting_ledger.csv").write_text(
            "date,stake,status,profit\n2026-07-11,20,命中,20.00\n",
            encoding="utf-8",
        )
        (self.root / "output" / "observation_ledger.csv").write_text(
            "date,stake,status\n2026-07-11,0,命中\n",
            encoding="utf-8",
        )
        (self.root / "data" / "draw_training_samples.csv").write_text(
            "date,match_id,outcome\n2026-07-11,settled,1\n",
            encoding="utf-8",
        )

        strategy.assert_activation_ready(self.root)

        audit_path = self.root / "output" / "shadow_portfolio_activation_audit.json"
        saved_audit = audit_path.read_text(encoding="utf-8")
        incomplete_audit = json.loads(saved_audit)
        incomplete_audit["evidence"][0]["activation_evidence"]["files"].pop(
            "decision_bundle"
        )
        audit_path.write_text(json.dumps(incomplete_audit), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manifest"):
            strategy.assert_activation_ready(self.root)
        audit_path.write_text(saved_audit, encoding="utf-8")

        immutable_path = self.root / files["fixtures"]["path"]
        immutable_path.write_bytes(immutable_path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            strategy.assert_activation_ready(self.root)

    def test_active_routing_rejects_missing_or_failed_audit_before_generation(self):
        (self.root / "betting_config.json").write_text(
            json.dumps(config(mode="active")), encoding="utf-8"
        )
        with (
            patch.object(strategy, "ROOT", self.root),
            patch.object(strategy, "OUTPUT_DIR", self.root / "output"),
            patch.object(strategy, "DATA_DIR", self.root / "data"),
            patch.object(
                strategy,
                "build_value_v4_plan",
                side_effect=AssertionError("generation ran"),
            ) as build,
        ):
            with self.assertRaisesRegex(ValueError, "audit"):
                strategy.build_strategy_outputs(
                    date(2026, 7, 11),
                    locked_at=datetime(2026, 7, 11, 12, tzinfo=BEIJING),
                )
            build.assert_not_called()

            (self.root / "output" / "shadow_portfolio_activation_audit.json").write_text(
                json.dumps({
                    "schema_version": "shadow-portfolio-activation-audit-v1",
                    "passed": False,
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "audit"):
                strategy.build_strategy_outputs(
                    date(2026, 7, 11),
                    locked_at=datetime(2026, 7, 11, 12, tzinfo=BEIJING),
                )
            build.assert_not_called()

    def test_schema_rejects_passed_payload_without_checked_dates(self):
        payload = audit_generated_portfolios({}, config())
        payload["passed"] = True

        with self.assertRaises(ValueError):
            validate_audit_payload(payload)

    def test_repository_configuration_is_simulation_only_and_shadow_until_prospective_audit(self):
        root = Path(__file__).resolve().parents[1]
        repository_config = json.loads(
            (root / "betting_config.json").read_text(encoding="utf-8")
        )

        mode = repository_config["value_strategy"]["activation_mode"]
        self.assertEqual("shadow", mode)
        self.assertEqual("simulation", repository_config["simulation_account"]["mode"])
        self.assertIs(
            False,
            repository_config["simulation_account"]["real_money_automation"],
        )
        with self.assertRaisesRegex(ValueError, "has not passed"):
            strategy.assert_activation_ready(root)


if __name__ == "__main__":
    unittest.main()
