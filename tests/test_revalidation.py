import hashlib
import json
import os
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from generate_betting_plan import StrategyOutputs, _candidate_plan_row
from provisional_plan import candidate_from_plan_row, create_provisional_outputs, read_valid_provisional_state
from revalidation import _source_commit_sha, _target_dates, _validate_runtime_state, _write_state_atomic, due_stage, evaluate_candidate, run_due_revalidation
from tests.test_provisional_plan import legacy_v1_generation
from value_candidates import ValueCandidate


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 20)


def canonical_digest(payload):
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def candidate(*, candidate_id="candidate-1", state="provisional", kickoff="2026-07-20T02:00:00+08:00", rank=1, odds="2.50", stake=80):
    market = {
        "match_id": "match-1",
        "match_num": "Monday001",
        "team_a": "Home",
        "team_b": "Away",
        "kickoff_at": kickoff,
        "market_type": "had",
        "market_line": "",
        "selection": "h",
    }
    value = {
        "schema_version": 2,
        "candidate_id": candidate_id,
        "report_date": DAY.isoformat(),
        "route": "shadow",
        "strategy_version": "value-v4",
        "provisional_rank": rank,
        "normalized_market_identity": market,
        "legs": [market],
        "earliest_kickoff_at_bjt": kickoff,
        "odds": odds,
        "provisional_stake": stake,
        "confirmed_stake": 0,
        "conservative_probability": "0.50",
        "minimum_ev": "0.03",
        "minimum_acceptable_odds": "2.060000",
        "source_plan_row": {
            "match_id": "match-1", "match_num": "Monday001",
            "team_a": "Home", "team_b": "Away", "kickoff_local": kickoff,
            "market_type": "had", "market_line": "", "selection": "h",
            "odds_source": "sporttery", "sales_state": "Selling",
            "single_eligibility": {"had": True, "hhad": False, "ttg": False},
            "odds_captured_at_bjt": "2026-07-19T13:30:00+08:00",
        },
        "execution_identity": {
            "decision_snapshot_sha256": "a" * 64,
            "decision_snapshot_captured_at_bjt": "2026-07-19T13:30:00+08:00",
            "legs": [{
                "source": "sporttery", "source_record_id": "source-1",
                "match_id": "match-1", "match_num": "Monday001",
                "team_a": "Home", "team_b": "Away",
                "kickoff_at_bjt": kickoff, "market_type": "had",
                "market_line": None, "selection": "h",
                "sales_state": "Selling", "single_eligible": True,
            }],
        },
        "state": state,
        "initial_candidate_attestation_sha256": "initial",
        "t90_receipt_path": "",
        "t90_receipt_sha256": "",
    }
    value["candidate_payload_sha256"] = canonical_digest(value)
    return value


def parlay_candidate(*, kickoffs, state="provisional"):
    value = candidate(state=state, kickoff=kickoffs[0])
    legs = []
    for index, kickoff in enumerate(kickoffs, 1):
        leg = dict(value["normalized_market_identity"])
        leg.update({"match_id": f"match-{index}", "match_num": f"Monday00{index}", "kickoff_at": kickoff})
        legs.append(leg)
    value["legs"] = legs
    value["normalized_market_identity"] = {"legs": legs}
    value["earliest_kickoff_at_bjt"] = kickoffs[0]
    value["execution_identity"]["legs"] = [
        {
            **value["execution_identity"]["legs"][0],
            "source_record_id": f"source-{index}",
            "match_id": f"match-{index}",
            "match_num": f"Monday00{index}",
            "kickoff_at_bjt": kickoff,
        }
        for index, kickoff in enumerate(kickoffs, 1)
    ]
    value.pop("candidate_payload_sha256")
    value["candidate_payload_sha256"] = canonical_digest(value)
    return value


def snapshot(*, odds="2.50", kickoff="2026-07-20T02:00:00+08:00"):
    return {
        "schema_version": 1,
        "target_date": DAY.isoformat(),
        "captured_at": "2026-07-20T00:30:00+08:00",
        "source": "sporttery",
        "fetch_mode": "live",
        "source_response_sha256": "0" * 64,
        "matches": [{
            "match_id": "match-1", "source_record_id": "source-1", "match_num": "Monday001",
            "team_a": "Home", "team_b": "Away", "kickoff_at": kickoff, "sales_state": "Selling",
            "single_eligibility": {"had": True, "hhad": False, "ttg": False},
            "markets": {"had": {"h": odds}, "hhad": {}, "ttg": {}},
        }],
    }


def config():
    return {
        "mode": "shadow", "minimum_initial_minutes": 60,
        "t90_open_minutes": 105, "t90_close_minutes": 40,
        "t30_open_minutes": 40, "t30_close_minutes": 10,
        "scan_business_days": 2, "stake_unit": 2, "max_notification_days": 30,
        "reference_bankroll": 5000, "kelly_fraction": "0.25",
    }


def actual_plan_row(*, market_type="had", market_line="", selection="h", match_id="match-1", team_a="Home", team_b="Away", kickoff="2026-07-20T02:00:00+08:00", stake="80", legs=None):
    return {
        "date": DAY.isoformat(), "strategy_version": "value-v4",
        "match_id": match_id,
        "market_type": market_type, "market_line": market_line,
        "selection": selection, "play": market_type.upper(),
        "team_a": team_a, "team_b": team_b, "kickoff_local": kickoff,
        "odds": "2.50" if legs is None else "6.25", "stake": stake,
        "conservative_probability": "0.50", "minimum_ev": "0.03",
        "odds_source": "sporttery",
        "odds_captured_at_bjt": "2026-07-19T13:30:00+08:00",
        "legs_json": json.dumps(legs or [], sort_keys=True),
    }


def actual_candidate(**changes):
    row = actual_plan_row(**changes)
    initial_snapshot = actual_snapshot()
    initial_snapshot["captured_at"] = "2026-07-19T13:30:00+08:00"
    evidence = task2_bundle()
    evidence["decision_snapshot"]["captured_at_bjt"] = initial_snapshot["captured_at"]
    evidence["decision_snapshot"]["payload"] = initial_snapshot
    outputs = StrategyOutputs([], [], [row], {})
    with TemporaryDirectory() as temporary, patch(
        "provisional_plan.strategy_outputs_from_bundle", return_value=outputs
    ), patch("provisional_plan.read_valid_decision_bundle", return_value=evidence):
        return create_provisional_outputs(
            Path(temporary),
            DAY,
            datetime(2026, 7, 19, 13, 40, tzinfo=BJT),
            evidence,
        )["candidates"][0]


def actual_parlay_candidate():
    legs = [
        {
            "match_id": "match-1", "match_num": "Monday001",
            "team_a": "Home", "team_b": "Away",
            "kickoff_at": "2026-07-20T02:00:00+08:00",
            "market_type": "had", "line": "", "selection": "h",
            "odds_source": "sporttery", "sales_state": "Selling",
            "single_eligibility": {"had": True, "hhad": True, "ttg": True},
            "odds_captured_at_bjt": "2026-07-19T13:30:00+08:00",
        },
        {
            "match_id": "match-2", "match_num": "Monday002",
            "team_a": "Alpha", "team_b": "Beta",
            "kickoff_at": "2026-07-20T03:00:00+08:00",
            "market_type": "hhad", "line": "+1", "selection": "a",
            "odds_source": "sporttery", "sales_state": "Selling",
            "single_eligibility": {"had": True, "hhad": True, "ttg": True},
            "odds_captured_at_bjt": "2026-07-19T13:30:00+08:00",
        },
    ]
    return actual_candidate(
        market_type="parlay", match_id="", selection="h + a",
        legs=legs,
    )


def actual_snapshot(*, second_goal_line="+1"):
    return {
        "schema_version": 1, "target_date": DAY.isoformat(),
        "captured_at": "2026-07-20T00:30:00+08:00",
        "source": "sporttery", "fetch_mode": "live",
        "source_response_sha256": "0" * 64,
        "matches": [
            {
                "match_id": "match-1", "source_record_id": "source-1",
                "match_num": "Monday001", "team_a": "Home", "team_b": "Away",
                "kickoff_at": "2026-07-20T02:00:00+08:00", "sales_state": "Selling",
                "single_eligibility": {"had": True, "hhad": True, "ttg": True},
                "markets": {"had": {"h": "2.50"}, "hhad": {"h": "2.50", "goalLine": "+1"}, "ttg": {}},
            },
            {
                "match_id": "match-2", "source_record_id": "source-2",
                "match_num": "Monday002", "team_a": "Alpha", "team_b": "Beta",
                "kickoff_at": "2026-07-20T03:00:00+08:00", "sales_state": "Selling",
                "single_eligibility": {"had": True, "hhad": True, "ttg": True},
                "markets": {"had": {}, "hhad": {"a": "2.50", "goalLine": second_goal_line}, "ttg": {}},
            },
        ],
    }


def write_actual_snapshot(root, payload=None):
    value = payload or actual_snapshot()
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    captured = datetime.fromisoformat(value["captured_at"])
    filename = (
        f"{captured.strftime('%Y%m%dT%H%M%S%z')}-{value['source']}-"
        f"{hashlib.sha256(raw).hexdigest()[:16]}.json"
    )
    path = root / "data" / "live_odds_snapshots" / DAY.isoformat() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def runtime_entry(value, *, state=None, ledger_status="not_applicable"):
    return {
        "candidate": value, "state": state or value["state"],
        "ledger_status": ledger_status, "last_stake": value["provisional_stake"],
        "confirmed_stake": value["provisional_stake"] if state == "confirmed" else 0,
        "t90_receipt_path": "", "t90_receipt_sha256": "",
        "t30_receipt_path": "", "t30_receipt_sha256": "",
    }


def runtime_state(value, **entry_changes):
    entry = runtime_entry(value)
    entry.update(entry_changes)
    return {"schema_version": 1, "report_date": DAY.isoformat(), "candidates": [entry]}


def task2_bundle():
    initial_snapshot = actual_snapshot()
    initial_snapshot["captured_at"] = "2026-07-19T13:30:00+08:00"
    return {
        "schema_version": 3, "target_date": DAY.isoformat(),
        "locked_at_bjt": "2026-07-20T00:20:00+08:00",
        "decision_snapshot": {
            "path": "data/odds_snapshots/2026-07-20-002000-decision.json",
            "sha256": "a" * 64, "captured_at_bjt": initial_snapshot["captured_at"],
            "payload": initial_snapshot,
        },
        "configuration": {"betting": {"payload": {"value_strategy": {"min_ev": 0.03}}}},
    }


def production_value_candidate():
    return ValueCandidate(
        candidate_id="match-1:had:h",
        date=DAY.isoformat(),
        match_id="match-1",
        stage="Test League",
        team_a="Home",
        team_b="Away",
        kickoff_at="2026-07-20T02:00:00+08:00",
        market_type="had",
        play="HAD",
        selection="h",
        line=None,
        official_odds=2.5,
        official_market_probability=0.4,
        raw_model_probability=0.5,
        calibrated_model_probability=0.5,
        conservative_probability=0.5,
        probability_edge=0.1,
        expected_value=0.25,
        single_eligible=True,
        data_quality="medium",
        data_quality_multiplier=0.6,
        volatility_band="stable",
        volatility_multiplier=1.0,
        odds_source="sporttery",
        source_record_id="decision-match-1-had",
        captured_at_bjt="2026-07-20T00:00:00+08:00",
        correlation_tags=("match:match-1",),
        paid_eligible=True,
        value_gate_reasons=(),
        calibration_samples=0,
    )


def production_value_v4_row():
    value = production_value_candidate()
    return _candidate_plan_row(
        value,
        80,
        locked_at=datetime(2026, 7, 20, 0, 5, tzinfo=BJT),
        portfolio_rank=1,
    )


def production_value_v4_parlay_row():
    first = production_value_candidate()
    second = replace(
        first,
        candidate_id="match-2:hhad:a",
        match_id="match-2",
        team_a="Alpha",
        team_b="Beta",
        kickoff_at="2026-07-20T03:00:00+08:00",
        market_type="hhad",
        play="HHAD",
        selection="a",
        line=1,
        source_record_id="decision-match-2-hhad",
    )
    legs = [
        {
            "match_id": leg.match_id,
            "market_type": leg.market_type,
            "selection": leg.selection,
            "line": "" if leg.line is None else str(leg.line),
            "odds": "2.50",
            "locked_odds": "2.50",
            "odds_source": leg.odds_source,
            "odds_source_record_id": leg.source_record_id,
            "odds_captured_at_bjt": leg.captured_at_bjt,
            "expected_value": 0.25,
            "net_ev": 0.25,
        }
        for leg in (first, second)
    ]
    return _candidate_plan_row(
        first,
        80,
        locked_at=datetime(2026, 7, 20, 0, 5, tzinfo=BJT),
        portfolio_rank=1,
        market_type="parlay",
        play="PARLAY",
        selection="h + a",
        odds=Decimal("6.25"),
        probability=0.25,
        legs=legs,
    )


def production_task2_bundle():
    value = task2_bundle()
    value["locked_at_bjt"] = "2026-07-20T00:05:00+08:00"
    value["decision_snapshot"]["captured_at_bjt"] = "2026-07-20T00:00:00+08:00"
    value["decision_snapshot"]["payload"] = {
        "schema_version": 1,
        "target_date": DAY.isoformat(),
        "captured_at": "2026-07-20T00:00:00+08:00",
        "source": "sporttery",
        "fetch_mode": "live",
        "source_response_sha256": "0" * 64,
        "matches": [
            {
                "match_id": "match-1",
                "source_record_id": "source-1",
                "match_num": "Monday001",
                "team_a": "Home",
                "team_b": "Away",
                "kickoff_at": "2026-07-20T02:00:00+08:00",
                "sales_state": "Selling",
                "single_eligibility": {"had": True, "hhad": True, "ttg": True},
                "markets": {
                    "had": {"h": "2.50"},
                    "hhad": {"h": "2.50", "goalLine": "+1"},
                    "ttg": {},
                },
            }
        ],
    }
    return value


class RevalidationTest(TestCase):
    def test_source_commit_prefers_valid_github_sha_and_has_deterministic_local_fallback(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}):
                self.assertEqual("a" * 40, _source_commit_sha(root))
            with patch.dict(os.environ, {"GITHUB_SHA": "untrusted"}):
                first = _source_commit_sha(root)
                second = _source_commit_sha(root)

        self.assertEqual(first, second)
        self.assertRegex(first, r"^local-[0-9a-f]{64}$")

    def test_due_stage_uses_earliest_parlay_leg(self):
        value = parlay_candidate(kickoffs=["2026-07-20T01:30:00+08:00", "2026-07-20T03:00:00+08:00"], state="screened")
        self.assertEqual("t30", due_stage(value, datetime.fromisoformat("2026-07-20T00:55:00+08:00")))

    def test_due_stage_opens_t90_at_105_minutes_and_not_before(self):
        value = candidate()
        self.assertIsNone(due_stage(value, datetime(2026, 7, 20, 0, 14, tzinfo=BJT)))
        self.assertEqual("t90", due_stage(value, datetime(2026, 7, 20, 0, 15, tzinfo=BJT)))

    def test_due_stage_cancels_missed_windows_and_never_transitions_after_kickoff(self):
        self.assertEqual("t90_window_missed", due_stage(candidate(), datetime(2026, 7, 20, 1, 20, tzinfo=BJT)))
        self.assertEqual("t30_window_missed", due_stage(candidate(state="screened"), datetime(2026, 7, 20, 1, 50, tzinfo=BJT)))
        self.assertIsNone(due_stage(candidate(), datetime(2026, 7, 20, 2, 0, tzinfo=BJT)))
        self.assertIsNone(due_stage(candidate(state="confirmed"), datetime(2026, 7, 20, 0, 15, tzinfo=BJT)))

    def test_evaluation_preserves_probability_and_can_only_reduce_stake(self):
        result = evaluate_candidate(candidate(), snapshot(), "t90", datetime(2026, 7, 20, 0, 30, tzinfo=BJT), config(), {"daily": 30})
        self.assertEqual("pass", result["decision"])
        self.assertEqual("screened", result["state"])
        self.assertEqual("0.50", result["receipt"]["conservative_probability"])
        self.assertEqual(30, result["stake"])
        self.assertEqual("0.250", result["receipt"]["current_ev"])

    def test_evaluation_cancels_on_any_strict_domestic_snapshot_mismatch(self):
        changed = snapshot(odds="2.50")
        changed["matches"][0]["team_b"] = "Different"
        result = evaluate_candidate(candidate(), changed, "t90", datetime(2026, 7, 20, 0, 30, tzinfo=BJT), config())
        self.assertEqual("cancel", result["decision"])
        self.assertEqual("fixture_mismatch", result["receipt"]["reason_code"])

    def test_actual_single_binds_complete_source_fixture_and_market_identity(self):
        value = actual_candidate()
        checked = datetime(2026, 7, 20, 0, 30, tzinfo=BJT)
        self.assertEqual(
            "pass",
            evaluate_candidate(value, actual_snapshot(), "t90", checked, config())["decision"],
        )
        mutations = {
            "source": lambda fresh: fresh.update(source="zgzcw"),
            "source_record": lambda fresh: fresh["matches"][0].update(source_record_id="changed"),
            "match_id": lambda fresh: fresh["matches"][0].update(match_id="match-9"),
            "match_num": lambda fresh: fresh["matches"][0].update(match_num="Monday999"),
            "team": lambda fresh: fresh["matches"][0].update(team_b="Changed"),
            "kickoff": lambda fresh: fresh["matches"][0].update(kickoff_at="2026-07-20T02:05:00+08:00"),
            "sales": lambda fresh: fresh["matches"][0].update(sales_state="Stopped"),
            "eligibility": lambda fresh: fresh["matches"][0]["single_eligibility"].update(had=False),
            "selection": lambda fresh: fresh["matches"][0]["markets"].update(had={"d": "2.50"}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                fresh = actual_snapshot()
                mutate(fresh)
                self.assertEqual(
                    "cancel",
                    evaluate_candidate(value, fresh, "t90", checked, config())["decision"],
                )

    def test_production_value_v4_candidate_passes_matching_fresh_evidence_end_to_end(self):
        row = production_value_v4_row()
        self.assertNotIn("match_num", row)
        self.assertNotIn("sales_state", row)
        self.assertNotIn("single_eligibility", row)
        bundle = production_task2_bundle()
        outputs = StrategyOutputs([row], [], [], {})
        with TemporaryDirectory() as temporary, patch(
            "provisional_plan.strategy_outputs_from_bundle", return_value=outputs
        ), patch("provisional_plan.read_valid_decision_bundle", return_value=bundle):
            root = Path(temporary)
            state = create_provisional_outputs(
                root,
                DAY,
                datetime(2026, 7, 20, 0, 10, tzinfo=BJT),
                bundle,
            )
            published = state["candidates"][0]

        result = evaluate_candidate(
            published,
            actual_snapshot(),
            "t90",
            datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
            config(),
        )
        self.assertEqual("pass", result["decision"])
        self.assertEqual("passed", result["receipt"]["reason_code"])

    def test_production_value_v4_parlay_binds_every_leg_end_to_end(self):
        row = production_value_v4_parlay_row()
        source_legs = json.loads(row["legs_json"])
        self.assertTrue(all("match_num" not in leg for leg in source_legs))
        self.assertTrue(all("kickoff_at" not in leg for leg in source_legs))
        evidence = task2_bundle()
        outputs = StrategyOutputs([row], [], [], {})
        with TemporaryDirectory() as temporary, patch(
            "provisional_plan.strategy_outputs_from_bundle", return_value=outputs
        ), patch("provisional_plan.read_valid_decision_bundle", return_value=evidence):
            published = create_provisional_outputs(
                Path(temporary),
                DAY,
                datetime(2026, 7, 19, 13, 40, tzinfo=BJT),
                evidence,
            )["candidates"][0]

        self.assertEqual(2, len(published["execution_identity"]["legs"]))
        self.assertEqual(
            [None, "1"],
            [leg["market_line"] for leg in published["execution_identity"]["legs"]],
        )
        self.assertEqual(
            "pass",
            evaluate_candidate(
                published,
                actual_snapshot(),
                "t90",
                datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                config(),
            )["decision"],
        )

    def test_run_due_revalidation_processes_v1_and_v2_generations(self):
        row = actual_plan_row()
        evidence = task2_bundle()
        generated_at = datetime(2026, 7, 19, 23, 30, tzinfo=BJT)
        checked_at = datetime(2026, 7, 20, 0, 30, tzinfo=BJT)
        for version in (1, 2):
            with self.subTest(version=version), TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "betting_config.json").write_text(
                    json.dumps({"pre_kickoff_revalidation": config()}),
                    encoding="utf-8",
                )
                with patch(
                    "provisional_plan.read_valid_decision_bundle",
                    return_value=evidence,
                ), patch(
                    "provisional_plan.strategy_outputs_from_bundle",
                    return_value=StrategyOutputs([row], [], [], {}),
                ):
                    if version == 1:
                        legacy_v1_generation(
                            root, DAY, row, evidence, generated_at
                        )
                    else:
                        create_provisional_outputs(
                            root, DAY, generated_at, evidence
                        )
                    generation_files = {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in (root / "output" / "provisional_generations").rglob("*")
                        if path.is_file()
                    }
                    changed = run_due_revalidation(
                        root,
                        checked_at,
                        target_dates=[DAY],
                        snapshot_provider=lambda *_args: write_actual_snapshot(root),
                    )
                    t30_snapshot = actual_snapshot()
                    t30_snapshot["captured_at"] = "2026-07-20T01:35:00+08:00"
                    confirmed = run_due_revalidation(
                        root,
                        datetime(2026, 7, 20, 1, 35, tzinfo=BJT),
                        target_dates=[DAY],
                        snapshot_provider=lambda *_args: write_actual_snapshot(
                            root, t30_snapshot
                        ),
                    )

                self.assertEqual("screened", changed[0]["state"])
                self.assertEqual("confirmed", confirmed[0]["state"])
                report_status_path = (
                    root / f"web/revalidation/{DAY.isoformat()}/status.json"
                )
                revalidation_status = json.loads(
                    report_status_path.read_text(encoding="utf-8")
                )
                report_image = root / revalidation_status["report_image_url"]
                revalidation_index = json.loads(
                    (root / "web/revalidation-index.json").read_text(encoding="utf-8")
                )
                self.assertEqual(1, revalidation_status["revision"])
                self.assertEqual(
                    [confirmed[0]["candidate_id"]],
                    revalidation_status["published_candidate_ids"],
                )
                self.assertEqual("ingested", revalidation_status["changed_candidates"][0]["ledger_status"])
                self.assertTrue(report_image.is_file())
                self.assertEqual(
                    hashlib.sha256(report_image.read_bytes()).hexdigest(),
                    revalidation_status["report_image_sha256"],
                )
                self.assertEqual(
                    [DAY.isoformat()],
                    [entry["report_date"] for entry in revalidation_index["dates"]],
                )
                runtime = json.loads(
                    (root / f"output/revalidation_state_{DAY.isoformat()}.json").read_text(
                        encoding="utf-8"
                    )
                )
                runtime_candidate = runtime["candidates"][0]["candidate"]
                self.assertIn("execution_identity", runtime_candidate)
                if version == 1:
                    self.assertEqual(
                        canonical_digest(runtime_candidate["execution_identity"]),
                        changed[0]["receipt"]["execution_identity_sha256"],
                    )
                    self.assertEqual(
                        canonical_digest(runtime_candidate["execution_identity"]),
                        confirmed[0]["receipt"]["execution_identity_sha256"],
                    )
                    self.assertEqual(
                        generation_files,
                        {
                            path.relative_to(root).as_posix(): path.read_bytes()
                            for path in (root / "output" / "provisional_generations").rglob("*")
                            if path.is_file()
                        },
                    )

    def test_scheduler_rejects_caller_forged_legacy_execution_identity_overlay(self):
        row = actual_plan_row()
        evidence = task2_bundle()
        with TemporaryDirectory() as temporary, patch(
            "provisional_plan.read_valid_decision_bundle", return_value=evidence
        ):
            root = Path(temporary)
            legacy_v1_generation(
                root,
                DAY,
                row,
                evidence,
                datetime(2026, 7, 19, 23, 30, tzinfo=BJT),
            )
            validated = read_valid_provisional_state(root, DAY)["candidates"][0]
        validated["execution_identity"]["legs"][0]["match_num"] = "Forged999"
        source = {
            "schema_version": 1,
            "report_date": DAY.isoformat(),
            "candidates": [validated],
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": config()}),
                encoding="utf-8",
            )
            with patch("revalidation.read_valid_provisional_state", return_value=source):
                with self.assertRaisesRegex(ValueError, "legacy|provenance|schema"):
                    run_due_revalidation(
                        root,
                        datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                        target_dates=[DAY],
                        snapshot_provider=lambda *_args: write_actual_snapshot(root),
                    )

    def test_actual_single_hhad_requires_exact_live_goal_line(self):
        value = actual_candidate(market_type="hhad", market_line="+1")
        fresh = actual_snapshot()
        fresh["matches"][0]["markets"]["hhad"] = {"h": "2.50", "goalLine": "-1"}
        result = evaluate_candidate(
            value, fresh, "t90", datetime(2026, 7, 20, 0, 30, tzinfo=BJT), config()
        )
        self.assertEqual("cancel", result["decision"])
        self.assertEqual("market_mismatch", result["receipt"]["reason_code"])

    def test_actual_parlay_binds_each_leg_and_hhad_goal_line(self):
        value = actual_parlay_candidate()
        checked = datetime(2026, 7, 20, 0, 30, tzinfo=BJT)
        self.assertEqual(
            "pass",
            evaluate_candidate(value, actual_snapshot(), "t90", checked, config())["decision"],
        )
        for field, changed in (("team_a", "Changed"), ("match_num", "Monday999")):
            with self.subTest(field=field):
                fresh = actual_snapshot()
                fresh["matches"][1][field] = changed
                self.assertEqual(
                    "cancel",
                    evaluate_candidate(value, fresh, "t90", checked, config())["decision"],
                )
        result = evaluate_candidate(
            value, actual_snapshot(second_goal_line="-1"), "t90", checked, config()
        )
        self.assertEqual("cancel", result["decision"])
        self.assertEqual("market_mismatch", result["receipt"]["reason_code"])

    def test_evaluation_rejects_snapshot_that_is_not_fresh_after_initial_capture(self):
        value = candidate()
        value["execution_identity"][
            "decision_snapshot_captured_at_bjt"
        ] = "2026-07-20T00:30:00+08:00"
        value["candidate_payload_sha256"] = canonical_digest({key: item for key, item in value.items() if key != "candidate_payload_sha256"})
        result = evaluate_candidate(value, snapshot(), "t90", datetime(2026, 7, 20, 0, 30, tzinfo=BJT), config())
        self.assertEqual("cancel", result["decision"])
        self.assertEqual("snapshot_invalid", result["receipt"]["reason_code"])

    def test_config_requires_the_fixed_two_yuan_stake_unit(self):
        invalid = config()
        invalid["stake_unit"] = 3
        with self.assertRaisesRegex(ValueError, "stake_unit"):
            evaluate_candidate(candidate(), snapshot(), "t90", datetime(2026, 7, 20, 0, 30, tzinfo=BJT), invalid)

    def test_all_caps_are_applied_before_the_final_two_yuan_floor(self):
        checked = datetime(2026, 7, 20, 0, 30, tzinfo=BJT)
        odd_remaining = evaluate_candidate(
            actual_candidate(), actual_snapshot(), "t90", checked, config(), {"daily": 3}
        )
        odd_provisional = evaluate_candidate(
            actual_candidate(stake="3"), actual_snapshot(), "t90", checked, config()
        )
        below_unit = evaluate_candidate(
            actual_candidate(), actual_snapshot(), "t90", checked, config(), {"daily": 1}
        )
        self.assertEqual(2, odd_remaining["stake"])
        self.assertEqual(2, odd_provisional["stake"])
        self.assertEqual("cancel", below_unit["decision"])
        self.assertEqual(0, below_unit["stake"])
        self.assertTrue(all(result["stake"] % 2 == 0 for result in (odd_remaining, odd_provisional, below_unit)))

    def test_runtime_state_rejects_terminal_states_without_transition_receipts(self):
        value = actual_candidate()
        source = {"report_date": DAY.isoformat(), "candidates": [value]}
        cases = (
            ("confirmed", "pending", value["provisional_stake"]),
            ("cancelled", "not_applicable", 0),
        )
        with TemporaryDirectory() as temporary:
            for state, ledger_status, confirmed_stake in cases:
                with self.subTest(state=state):
                    runtime = runtime_state(
                        value, state=state, ledger_status=ledger_status,
                        confirmed_stake=confirmed_stake,
                    )
                    with self.assertRaisesRegex(ValueError, "receipt|transition"):
                        _validate_runtime_state(Path(temporary), DAY, runtime, source)

    def test_runtime_state_rejects_skips_rollbacks_and_receipt_decision_mismatches(self):
        value = actual_candidate()
        source = {"report_date": DAY.isoformat(), "candidates": [value]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": config()}), encoding="utf-8"
            )
            with patch("revalidation.read_valid_provisional_state", return_value=source):
                provider = lambda *_args: write_actual_snapshot(root)
                run_due_revalidation(
                    root, datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                    target_dates=[DAY], snapshot_provider=provider,
                )
                run_due_revalidation(
                    root, datetime(2026, 7, 20, 1, 25, tzinfo=BJT),
                    target_dates=[DAY], snapshot_provider=provider,
                )
            valid = json.loads((root / f"output/revalidation_state_{DAY.isoformat()}.json").read_text(encoding="utf-8"))

            skipped = json.loads(json.dumps(valid))
            skipped_entry = skipped["candidates"][0]
            skipped_entry["t90_receipt_path"] = ""
            skipped_entry["t90_receipt_sha256"] = ""
            with self.assertRaisesRegex(ValueError, "receipt|transition"):
                _validate_runtime_state(root, DAY, skipped, source)

            rolled_back = json.loads(json.dumps(valid))
            rolled_entry = rolled_back["candidates"][0]
            rolled_entry.update(state="screened", ledger_status="not_applicable", confirmed_stake=0)
            with self.assertRaisesRegex(ValueError, "receipt|transition"):
                _validate_runtime_state(root, DAY, rolled_back, source)

            mismatched = json.loads(json.dumps(valid))
            mismatch_entry = mismatched["candidates"][0]
            mismatch_entry.update(state="cancelled", ledger_status="not_applicable", confirmed_stake=0)
            with self.assertRaisesRegex(ValueError, "receipt|transition"):
                _validate_runtime_state(root, DAY, mismatched, source)

    def test_runtime_state_accepts_only_validated_task2_initial_t90_attestation(self):
        outputs = StrategyOutputs([actual_plan_row()], [], [], {})
        bundle = task2_bundle()
        with TemporaryDirectory() as temporary, patch(
            "provisional_plan.strategy_outputs_from_bundle", return_value=outputs
        ), patch("provisional_plan.read_valid_decision_bundle", return_value=bundle):
            root = Path(temporary)
            create_provisional_outputs(
                root, DAY, datetime(2026, 7, 20, 0, 30, tzinfo=BJT), bundle
            )
            source = read_valid_provisional_state(root, DAY)
            screened = source["candidates"][0]
            self.assertEqual("screened", screened["state"])
            _validate_runtime_state(root, DAY, runtime_state(screened), source)

            forged = actual_candidate()
            forged["state"] = "screened"
            forged["candidate_payload_sha256"] = canonical_digest(
                {key: item for key, item in forged.items() if key != "candidate_payload_sha256"}
            )
            forged_source = {
                "generation_id": source["generation_id"],
                "report_date": DAY.isoformat(), "candidates": [forged],
            }
            with self.assertRaisesRegex(ValueError, "initial|receipt|transition"):
                _validate_runtime_state(root, DAY, runtime_state(forged), forged_source)

    def test_retry_replays_create_only_receipt_after_state_write_failure(self):
        value = actual_candidate()
        source = {"report_date": DAY.isoformat(), "candidates": [value]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": config()}), encoding="utf-8"
            )
            captures = []

            def provider(_root, target_date, checked):
                captures.append((target_date, checked))
                return write_actual_snapshot(root)

            with patch("revalidation.read_valid_provisional_state", return_value=source):
                with patch("revalidation._write_state_atomic", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        run_due_revalidation(
                            root, datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                            target_dates=[DAY], snapshot_provider=provider,
                        )
                changed = run_due_revalidation(
                    root, datetime(2026, 7, 20, 0, 31, tzinfo=BJT),
                    target_dates=[DAY], snapshot_provider=provider,
                )

            self.assertEqual(1, len(captures))
            self.assertEqual("screened", changed[0]["state"])
            self.assertEqual(
                "2026-07-20T00:30:00+08:00",
                changed[0]["receipt"]["checked_at_bjt"],
            )
            published = json.loads(
                (root / f"output/revalidation_state_{DAY.isoformat()}.json").read_text(encoding="utf-8")
            )
            self.assertEqual("screened", published["candidates"][0]["state"])
            self.assertTrue(published["candidates"][0]["t90_receipt_path"])

    def test_state_writer_ignores_stale_fixed_temp_and_cleans_interrupted_temp(self):
        value = candidate()
        state = runtime_state(value)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "output" / f"revalidation_state_{DAY}.json"
            state_path.parent.mkdir(parents=True)
            stale = state_path.with_name(state_path.name + ".tmp")
            stale.write_bytes(b"stale crash residue")
            unique_stale = state_path.with_name(
                f".{state_path.name}.interrupted.tmp"
            )
            unique_stale.write_bytes(b"interrupted unique temp")

            _write_state_atomic(state_path, state)

            self.assertEqual(
                state,
                json.loads(state_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(b"stale crash residue", stale.read_bytes())
            self.assertFalse(unique_stale.exists())

            failed_path = root / "output" / "revalidation_state_failed.json"
            with patch(
                "revalidation.os.replace", side_effect=OSError("replace interrupted")
            ), self.assertRaisesRegex(OSError, "replace interrupted"):
                _write_state_atomic(failed_path, state)
            residues = [
                path
                for path in failed_path.parent.iterdir()
                if path.name.startswith(f".{failed_path.name}.")
                and path.name.endswith(".tmp")
            ]
            self.assertEqual([], residues)

    def test_state_writer_serializes_concurrent_status_progress(self):
        value = candidate()
        pending = runtime_state(
            value,
            state="confirmed",
            ledger_status="pending",
            confirmed_stake=value["provisional_stake"],
        )
        ingested = json.loads(json.dumps(pending))
        ingested["candidates"][0]["ledger_status"] = "ingested"
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "output" / f"revalidation_state_{DAY}.json"
            barrier = threading.Barrier(2)
            errors = []

            def publish(state):
                barrier.wait()
                try:
                    _write_state_atomic(path, state)
                except ValueError as exc:
                    errors.append(str(exc))

            threads = [
                threading.Thread(target=publish, args=(state,))
                for state in (pending, ingested)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            published = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                "ingested", published["candidates"][0]["ledger_status"]
            )
            self.assertLessEqual(len(errors), 1)
            self.assertTrue(
                all("rollback" in message or "monotonic" in message for message in errors)
            )

    def test_state_writer_rejects_delayed_ledger_status_rollback(self):
        value = candidate()
        pending = runtime_state(
            value,
            state="confirmed",
            ledger_status="pending",
            confirmed_stake=value["provisional_stake"],
        )
        ingested = json.loads(json.dumps(pending))
        ingested["candidates"][0]["ledger_status"] = "ingested"
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "output" / f"revalidation_state_{DAY}.json"
            _write_state_atomic(path, ingested)
            committed = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "rollback|monotonic|stale"):
                _write_state_atomic(path, pending)

            self.assertEqual(committed, path.read_bytes())

    def test_ledger_failure_keeps_pending_and_retry_ingests_exactly_once(self):
        value = actual_candidate()
        source = {"report_date": DAY.isoformat(), "candidates": [value]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = config()
            settings["mode"] = "active"
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": settings}),
                encoding="utf-8",
            )
            with patch("revalidation.read_valid_provisional_state", return_value=source):
                run_due_revalidation(
                    root,
                    datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                    target_dates=[DAY],
                    snapshot_provider=lambda *_args: write_actual_snapshot(root),
                )
                final_snapshot = actual_snapshot()
                final_snapshot["captured_at"] = "2026-07-20T01:35:00+08:00"
                with patch(
                    "betting_ledger._commit_ledger_generation_locked",
                    side_effect=OSError("ledger unavailable"),
                ), self.assertRaisesRegex(OSError, "ledger unavailable"):
                    run_due_revalidation(
                        root,
                        datetime(2026, 7, 20, 1, 35, tzinfo=BJT),
                        target_dates=[DAY],
                        snapshot_provider=lambda *_args: write_actual_snapshot(
                            root, final_snapshot
                        ),
                    )
                pending = json.loads(
                    (root / f"output/revalidation_state_{DAY}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual("pending", pending["candidates"][0]["ledger_status"])
                self.assertNotIn("change_digest", pending)
                self.assertFalse((root / "output" / "observation_ledger.csv").exists())
                run_due_revalidation(
                    root,
                    datetime(2026, 7, 20, 1, 36, tzinfo=BJT),
                    target_dates=[DAY],
                    snapshot_provider=lambda *_args: self.fail(
                        "pending retry must not capture a new snapshot"
                    ),
                )
            ingested = json.loads(
                (root / f"output/revalidation_state_{DAY}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("ingested", ingested["candidates"][0]["ledger_status"])
            import betting_ledger as ledger_module

            with ledger_module.resolve_ledger_path(
                root / "output" / "observation_ledger.csv"
            ).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                self.assertEqual(1, len(list(__import__("csv").DictReader(handle))))

    def test_retry_rejects_orphan_receipt_without_its_bound_snapshot(self):
        value = actual_candidate()
        source = {"report_date": DAY.isoformat(), "candidates": [value]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": config()}), encoding="utf-8"
            )
            snapshot_path = None

            def provider(_root, target_date, checked):
                nonlocal snapshot_path
                snapshot_path = write_actual_snapshot(root)
                return snapshot_path

            with patch("revalidation.read_valid_provisional_state", return_value=source):
                with patch("revalidation._write_state_atomic", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        run_due_revalidation(
                            root, datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                            target_dates=[DAY], snapshot_provider=provider,
                        )
                snapshot_path.unlink()
                with self.assertRaisesRegex(ValueError, "conflicting revalidation receipt"):
                    run_due_revalidation(
                        root, datetime(2026, 7, 20, 0, 31, tzinfo=BJT),
                        target_dates=[DAY],
                        snapshot_provider=lambda *_args: self.fail("replay must not refetch"),
                    )

    def test_retry_rejects_orphan_receipt_not_reproducible_from_bound_snapshot(self):
        value = actual_candidate()
        source = {"report_date": DAY.isoformat(), "candidates": [value]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": config()}), encoding="utf-8"
            )
            with patch("revalidation.read_valid_provisional_state", return_value=source):
                with patch("revalidation._write_state_atomic", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        run_due_revalidation(
                            root, datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                            target_dates=[DAY],
                            snapshot_provider=lambda *_args: write_actual_snapshot(root),
                        )
                receipt_path = next((root / "output" / "revalidation_receipts").rglob("*.json"))
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt["current_odds"] = "9.99"
                receipt_path.write_bytes(
                    (json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                )
                with self.assertRaisesRegex(ValueError, "conflicting revalidation receipt"):
                    run_due_revalidation(
                        root, datetime(2026, 7, 20, 0, 31, tzinfo=BJT),
                        target_dates=[DAY],
                        snapshot_provider=lambda *_args: self.fail("replay must not refetch"),
                    )

    def test_scheduler_cancels_screened_candidate_that_misses_t30_window(self):
        value = actual_candidate()
        source = {"report_date": DAY.isoformat(), "candidates": [value]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": config()}), encoding="utf-8"
            )
            with patch("revalidation.read_valid_provisional_state", return_value=source):
                provider = lambda *_args: write_actual_snapshot(root)
                run_due_revalidation(
                    root, datetime(2026, 7, 20, 0, 30, tzinfo=BJT),
                    target_dates=[DAY], snapshot_provider=provider,
                )
                changed = run_due_revalidation(
                    root, datetime(2026, 7, 20, 1, 50, tzinfo=BJT),
                    target_dates=[DAY], snapshot_provider=provider,
                )
            self.assertEqual("cancelled", changed[0]["state"])
            self.assertEqual("t30_window_missed", changed[0]["receipt"]["reason_code"])

    def test_run_scans_today_and_yesterday_in_bjt_and_orders_due_candidates(self):
        yesterday = DAY - timedelta(days=1)
        first = candidate(candidate_id="candidate-b", rank=2, kickoff="2026-07-20T02:00:00+08:00")
        second = candidate(candidate_id="candidate-a", rank=1, kickoff="2026-07-20T02:00:00+08:00")
        old = candidate(candidate_id="candidate-old", kickoff="2026-07-19T02:00:00+08:00")
        states = {DAY: {"report_date": DAY.isoformat(), "candidates": [first, second]}, yesterday: {"report_date": yesterday.isoformat(), "candidates": [old]}}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(json.dumps({"pre_kickoff_revalidation": config()}), encoding="utf-8")
            captured_dates = []
            def provider(_root, target_date, now):
                captured_dates.append(target_date)
                return _root / "snapshots" / f"{target_date}.json"
            def reader(_root, path, target_date, not_after):
                value = snapshot()
                value["target_date"] = target_date.isoformat()
                value["captured_at"] = "2026-07-20T00:15:00+08:00"
                return value
            with patch("revalidation.read_valid_provisional_state", side_effect=lambda _root, target_date: states[target_date]), patch("revalidation.read_valid_live_snapshot", side_effect=reader):
                changed = run_due_revalidation(root, datetime(2026, 7, 20, 0, 15, tzinfo=BJT), snapshot_provider=provider)
        self.assertEqual([DAY], captured_dates)
        self.assertEqual(["candidate-a", "candidate-b"], [item["candidate_id"] for item in changed])
        self.assertTrue(all(item["state"] == "screened" for item in changed))

    def test_explicit_target_dates_are_limited_to_today_and_yesterday_in_beijing(self):
        pacific = timezone(timedelta(hours=-7))
        now = datetime(2026, 7, 19, 10, 15, tzinfo=pacific)
        yesterday = DAY - timedelta(days=1)

        self.assertEqual([DAY, yesterday], _target_dates(now, None, 2))
        self.assertEqual([DAY, yesterday], _target_dates(now, [yesterday, DAY], 2))
        for supplied in ([DAY - timedelta(days=2)], [DAY + timedelta(days=1)]):
            with self.subTest(supplied=supplied):
                with self.assertRaisesRegex(ValueError, "target dates are invalid"):
                    _target_dates(now, supplied, 2)

    def test_explicit_target_dates_reject_duplicates_and_malformed_values(self):
        now = datetime(2026, 7, 20, 0, 15, tzinfo=BJT)
        for supplied in ([DAY, DAY], [DAY.isoformat()], [datetime(2026, 7, 20, 0, 0, tzinfo=BJT)]):
            with self.subTest(supplied=supplied):
                with self.assertRaisesRegex(ValueError, "target dates are invalid"):
                    _target_dates(now, supplied, 2)

    def test_run_skips_a_date_without_a_provisional_pointer_and_does_not_fetch(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "betting_config.json").write_text(json.dumps({"pre_kickoff_revalidation": config()}), encoding="utf-8")
            with patch("revalidation.read_valid_provisional_state", side_effect=ValueError("provisional generation manifest or pointer is missing")):
                provider = lambda *_args: self.fail("snapshot provider must not run")
                self.assertEqual([], run_due_revalidation(root, datetime(2026, 7, 20, 0, 15, tzinfo=BJT), target_dates=[DAY], snapshot_provider=provider))


if __name__ == "__main__":
    import unittest
    unittest.main()
