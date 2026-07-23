import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

import import_sporttery
from decision_bundle import (
    create_decision_bundle,
    read_valid_decision_bundle,
    write_prediction_metadata,
)
from generate_betting_plan import StrategyOutputs, plan_csv_bytes
from plan_lock import lock_plan
from provisional_plan import create_provisional_outputs
from report_status import (
    OFFICIAL_FIXTURE_SOURCES,
    _matching_decision_snapshot,
    artifact_state,
    base_status,
    main,
    publish_status,
)


BJT = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_DATE = date(2026, 7, 16)
GENERATED_AT = datetime(2026, 7, 16, 13, 35, tzinfo=BJT)
FIXTURE_FIELDS = [
    "date", "kickoff_local", "stage", "team_a", "team_b", "neutral", "venue",
    "odds_a", "odds_draw", "odds_b", "market_odds_a", "market_odds_draw",
    "market_odds_b", "analysis_source", "is_single_had", "match_num", "match_id",
    "pool_status", "kickoff_at",
]
PREDICTION_FIELDS = [
    "date", "kickoff", "stage", "match_num", "match_id", "team_a", "team_b",
    "xg_a", "xg_b", "p_a", "p_draw", "p_b", "adv_a", "adv_b", "pick",
    "confidence", "analysis_source", "is_single_had", "score_1", "score_1_prob",
    "score_2", "score_2_prob", "score_3", "score_3_prob", "kickoff_at",
]
PLAN_FIELDS = [
    "date", "strategy_version", "stage", "match", "team_a", "team_b", "play",
    "selection", "probability", "raw_model_probability", "league_calibrated_probability",
    "league_calibration_samples", "odds", "market_probability", "value_edge",
    "expected_value", "stake", "expected_return", "expected_profit", "reason",
    "legs_json",
]
LEDGER_FIELDS = [
    "date", "strategy_version", "stage", "match", "play", "selection", "probability",
    "raw_model_probability", "league_calibrated_probability", "league_calibration_samples",
    "odds", "market_probability", "value_edge", "expected_value", "stake", "status",
    "profit", "reason", "legs_json",
]


class ReportStatusTest(unittest.TestCase):
    def write_report_image(
        self, root: Path, report_date: date, report_stage: str, build_id: str
    ) -> Path:
        path = root / "web" / "daily-report.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = PngInfo()
        metadata.add_text("report_date", report_date.isoformat())
        metadata.add_text("report_stage", report_stage)
        metadata.add_text("build_id", build_id)
        Image.new("RGB", (1, 1)).save(path, pnginfo=metadata)
        return path

    def make_artifacts(self, root: Path, fixture_ids=("001", "002")) -> None:
        data = root / "data"
        output = root / "output"
        web = root / "web"
        data.mkdir(parents=True)
        output.mkdir()
        web.mkdir()
        source_status = {"target_date": REPORT_DATE.isoformat(), "source": "竞彩网"}
        if not fixture_ids:
            source_status["fixture_count"] = 0
            source_status["no_fixtures"] = True
        (data / "source_status.json").write_text(
            json.dumps(source_status),
            encoding="utf-8",
        )
        with (data / "fixtures.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIXTURE_FIELDS)
            writer.writeheader()
            for match_id in fixture_ids:
                writer.writerow({
                    "date": REPORT_DATE.isoformat(),
                    "kickoff_local": "20:00",
                    "stage": "league",
                    "team_a": f"A-{match_id}",
                    "team_b": f"B-{match_id}",
                    "neutral": "false",
                    "venue": "test",
                    "match_id": match_id,
                    "kickoff_at": "2026-07-16T20:00:00+08:00",
                })
            if fixture_ids:
                writer.writerow({
                    "date": "2026-07-15",
                    "kickoff_local": "20:00",
                    "stage": "league",
                    "team_a": "Yesterday A",
                    "team_b": "Yesterday B",
                    "neutral": "false",
                    "venue": "test",
                    "match_id": "yesterday",
                    "kickoff_at": "2026-07-15T20:00:00+08:00",
                })
        odds = {
            match_id: {"had": {"h": "2.0"}, "hhad": {}, "ttg": {}}
            for match_id in fixture_ids
        }
        (data / f"sporttery_odds_{REPORT_DATE.isoformat()}.json").write_text(
            json.dumps(odds), encoding="utf-8"
        )
        with (output / f"predictions_{REPORT_DATE.isoformat()}.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=PREDICTION_FIELDS)
            writer.writeheader()
            for match_id in fixture_ids:
                writer.writerow({
                    "date": REPORT_DATE.isoformat(),
                    "match_id": match_id,
                    "team_a": f"A-{match_id}",
                    "team_b": f"B-{match_id}",
                    "kickoff_at": "2026-07-16T20:00:00+08:00",
                })
        with (output / f"betting_plan_{REPORT_DATE.isoformat()}.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            csv.DictWriter(handle, fieldnames=PLAN_FIELDS).writeheader()
        (root / "config.json").write_text("{}\n", encoding="utf-8")
        betting_config = json.loads(
            (REPO_ROOT / "betting_config.json").read_text(encoding="utf-8")
        )
        betting_config["value_strategy"]["activation_mode"] = "shadow"
        (root / "betting_config.json").write_text(
            json.dumps(betting_config), encoding="utf-8"
        )
        for name in (
            "predict_today.py",
            "generate_betting_plan.py",
            "value_candidates.py",
            "value_portfolio.py",
            "official_markets.py",
            "betting_ledger.py",
            "strategy_controls.py",
        ):
            (root / name).write_text(f"MODULE = {name!r}\n", encoding="utf-8")
        (data / "team_ratings.csv").write_text(
            "team,elo\nA,1500\nB,1490\n", encoding="utf-8"
        )
        def manifest_record(path: Path) -> dict:
            payload = path.read_bytes()
            return {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        extracts = data / "import_extracts" / REPORT_DATE.isoformat()
        extracts.mkdir(parents=True)
        extract_fixtures = extracts / "fixtures.csv"
        extract_odds = extracts / "odds.json"
        extract_ratings = extracts / "ratings.csv"
        extract_fixtures.write_bytes((data / "fixtures.csv").read_bytes())
        extract_odds.write_bytes(
            (data / f"sporttery_odds_{REPORT_DATE.isoformat()}.json").read_bytes()
        )
        extract_ratings.write_bytes((data / "team_ratings.csv").read_bytes())
        manifests = data / "import_manifests"
        manifests.mkdir()
        (manifests / f"{REPORT_DATE.isoformat()}.json").write_text(
            json.dumps({
                "schema_version": 2,
                "target_date": REPORT_DATE.isoformat(),
                "source": "sporttery",
                "imported_at_bjt": "2026-07-16T13:29:00+08:00",
                "fixtures": manifest_record(extract_fixtures),
                "odds": manifest_record(extract_odds),
                "ratings": manifest_record(extract_ratings),
            }),
            encoding="utf-8",
        )
        (output / "observation_ledger.csv").write_text(
            "placeholder\n", encoding="utf-8"
        )
        (data / "draw_training_samples.csv").write_text(
            "placeholder\n", encoding="utf-8"
        )
        (output / f"daily_decision_{REPORT_DATE.isoformat()}.json").write_text(
            json.dumps({"date": REPORT_DATE.isoformat(), "status": "no_bet"}),
            encoding="utf-8",
        )
        with (output / "betting_ledger.csv").open("w", encoding="utf-8", newline="") as handle:
            csv.DictWriter(handle, fieldnames=LEDGER_FIELDS).writeheader()
        (web / "index.html").write_text("<html></html>", encoding="utf-8")
        (web / "daily-report.png").write_bytes(b"exact png bytes")

    def make_lock(
        self,
        root: Path,
        locked_at: datetime = datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
    ) -> None:
        snapshots = root / "data" / "odds_snapshots"
        snapshots.mkdir(exist_ok=True)
        def record(path: Path) -> dict:
            content = path.read_bytes()
            return {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        manifest_path = (
            root / "data" / "import_manifests" / f"{REPORT_DATE.isoformat()}.json"
        )
        extracts = root / "data" / "import_extracts" / REPORT_DATE.isoformat()
        extracts.mkdir(parents=True, exist_ok=True)
        extract_fixtures = extracts / "fixtures.csv"
        extract_odds = extracts / "odds.json"
        extract_ratings = extracts / "ratings.csv"
        extract_fixtures.write_bytes((root / "data" / "fixtures.csv").read_bytes())
        extract_odds.write_bytes(
            (
                root
                / "data"
                / f"sporttery_odds_{REPORT_DATE.isoformat()}.json"
            ).read_bytes()
        )
        extract_ratings.write_bytes(
            (root / "data" / "team_ratings.csv").read_bytes()
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({
                "schema_version": 2,
                "target_date": REPORT_DATE.isoformat(),
                "source": "sporttery",
                "imported_at_bjt": "2026-07-16T13:29:00+08:00",
                "fixtures": record(extract_fixtures),
                "odds": record(extract_odds),
                "ratings": record(extract_ratings),
            }),
            encoding="utf-8",
        )
        with (root / "data" / "fixtures.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            fixtures = [
                row
                for row in csv.DictReader(handle)
                if row.get("date") == REPORT_DATE.isoformat()
            ]
        (snapshots / "2026-07-16-133000-decision.json").write_text(
            json.dumps({
                "target_date": REPORT_DATE.isoformat(),
                "captured_at": "2026-07-16T13:30:00+08:00",
                "capture_phase": "decision",
                "source": "sporttery",
                "import_manifest": record(manifest_path),
                "source_record_id": "report-status-snapshot",
                "matches": [{
                    "match_id": row["match_id"],
                    "source_record_id": f"report-status-{row['match_id']}",
                    "team_a": row["team_a"],
                    "team_b": row["team_b"],
                    "match_num": row["match_id"],
                    "kickoff_at": row["kickoff_at"],
                    "sales_state": "Selling",
                    "markets": {
                        "had": {"h": "2.00", "d": "3.20", "a": "3.50"},
                        "hhad": {},
                        "ttg": {},
                    },
                    "single_eligibility": {
                        "had": True,
                        "hhad": False,
                        "ttg": False,
                    },
                } for row in fixtures],
            }),
            encoding="utf-8",
        )
        write_prediction_metadata(
            root,
            REPORT_DATE,
            datetime(2026, 7, 16, 13, 30, 30, tzinfo=BJT),
        )
        bundle = create_decision_bundle(root, REPORT_DATE, locked_at)
        (root / "output" / f"betting_plan_{REPORT_DATE.isoformat()}.csv").write_bytes(
            plan_csv_bytes(bundle["paid_plan_evidence"]["rows"])
        )
        lock_plan(
            root,
            REPORT_DATE,
            locked_at,
        )

    def make_decision_snapshot(self, root: Path) -> None:
        snapshots = root / "data" / "odds_snapshots"
        snapshots.mkdir(exist_ok=True)
        path = snapshots / "2026-07-16-133000-decision.json"
        if path.exists():
            return
        path.write_text(
            json.dumps({
                "target_date": REPORT_DATE.isoformat(),
                "captured_at": "2026-07-16T13:30:00+08:00",
                "phase": "decision",
                "matches": [{"match_id": "001"}, {"match_id": "002"}],
            }),
            encoding="utf-8",
        )

    def write_producer_decision_snapshot(self, root: Path) -> None:
        snapshots = root / "data" / "odds_snapshots"
        snapshots.mkdir()
        (snapshots / "2026-07-16-133000-decision.json").write_text(
            json.dumps({
                "target_date": REPORT_DATE.isoformat(),
                "captured_at": "2026-07-16T13:30:00+08:00",
                "capture_phase": "decision",
                "source": "zgzcw",
                "matches": [{"match_id": "001"}, {"match_id": "002"}],
            }),
            encoding="utf-8",
        )

    def write_decision_snapshot(self, root: Path, timestamp: str) -> Path:
        snapshots = root / "data" / "odds_snapshots"
        snapshots.mkdir(exist_ok=True)
        path = snapshots / f"2026-07-16-{timestamp}-decision.json"
        if path.exists():
            return path
        path.write_text(
            json.dumps({
                "target_date": REPORT_DATE.isoformat(),
                "captured_at": (
                    f"2026-07-16T{timestamp[:2]}:{timestamp[2:4]}:"
                    f"{timestamp[4:]}+08:00"
                ),
                "phase": "decision",
                "matches": [{"match_id": "001"}, {"match_id": "002"}],
            }),
            encoding="utf-8",
        )
        return path

    def publish(self, root: Path, phase: str, **kwargs) -> dict:
        self.write_report_image(
            root, REPORT_DATE, phase, "123456-1-" + phase
        )
        return publish_status(
            root,
            REPORT_DATE,
            phase,
            "123456-1-" + phase,
            "abc123",
            GENERATED_AT,
            **kwargs,
        )

    def test_base_status_contains_the_machine_readable_defaults(self):
        self.assertEqual(
            {
                "schema_version": 2,
                "report_date": "2026-07-16",
                "forecast_ready": False,
                "decision_snapshot_ready": False,
                "settlement_ready": False,
                "plan_ready": False,
                "settled_through": "",
                "decision_odds_at_bjt": "",
                "plan_locked_at_bjt": "",
                "report_stage": "forecast",
                "initial_report_ready": False,
                "provisional_plan_sha256": "",
                "provisional_candidate_count": 0,
                "confirmed_stake": 0,
                "provisional_stake": 0,
                "revalidation_ready": False,
            },
            base_status(REPORT_DATE),
        )

    def test_provisional_status_uses_bundle_and_provisional_artifacts_without_plan_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            (root / "output" / f"plan_lock_{REPORT_DATE.isoformat()}.json").unlink()
            decision_bundle = read_valid_decision_bundle(root, REPORT_DATE)
            active_row = {
                "date": REPORT_DATE.isoformat(),
                "strategy_version": "value-v4",
                "match_id": "001",
                "market_type": "had",
                "market_line": "",
                "selection": "h",
                "kickoff_local": "2026-07-16T16:00:00+08:00",
                "odds": "2.10",
                "stake": "140",
                "conservative_probability": "0.55",
                "minimum_ev": "0.05",
                "legs_json": "[]",
            }
            shadow_row = {**active_row, "match_id": "002", "stake": "20"}
            outputs = StrategyOutputs([active_row], [], [shadow_row], {})
            with patch(
                "provisional_plan.strategy_outputs_from_bundle", return_value=outputs
            ):
                create_provisional_outputs(
                    root, REPORT_DATE, GENERATED_AT, decision_bundle
                )

            status = self.publish(root, "provisional")

            self.assertEqual(2, status["schema_version"])
            self.assertEqual("provisional", status["report_stage"])
            self.assertTrue(status["initial_report_ready"])
            self.assertRegex(status["provisional_plan_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(0, status["confirmed_stake"])
            self.assertEqual(140, status["provisional_stake"])
            self.assertFalse(status["data_quality"]["plan_lock_ready"])
            self.assertTrue(status["revalidation_ready"])
            revalidation_status = json.loads(
                (
                    root
                    / f"web/revalidation/{REPORT_DATE.isoformat()}/status.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(0, revalidation_status["revision"])
            self.assertTrue((root / "web/revalidation-index.json").is_file())

    def test_provisional_status_rejects_state_only_fractional_stake_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            decision_bundle = read_valid_decision_bundle(root, REPORT_DATE)
            row = {
                "date": REPORT_DATE.isoformat(),
                "strategy_version": "value-v4",
                "match_id": "001",
                "market_type": "had",
                "market_line": "",
                "selection": "h",
                "kickoff_local": "2026-07-16T16:00:00+08:00",
                "odds": "2.10",
                "stake": "140",
                "conservative_probability": "0.55",
                "minimum_ev": "0.05",
                "legs_json": "[]",
            }
            with patch(
                "provisional_plan.strategy_outputs_from_bundle",
                return_value=StrategyOutputs([row], [], [], {}),
            ):
                create_provisional_outputs(root, REPORT_DATE, GENERATED_AT, decision_bundle)

            pointer_path = root / "output" / f"provisional_generation_{REPORT_DATE.isoformat()}.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            state_path = root / pointer["artifacts"]["state"]["path"]
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["candidates"][0]["provisional_stake"] = 140.5
            candidate = state["candidates"][0]
            candidate["candidate_payload_sha256"] = hashlib.sha256(
                json.dumps(
                    {
                        key: value
                        for key, value in candidate.items()
                        if key != "candidate_payload_sha256"
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            state["active_provisional_stake"] = 140.5
            state_bytes = (
                json.dumps(
                    state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            state_path.write_bytes(state_bytes)
            pointer["artifacts"]["state"]["sha256"] = hashlib.sha256(state_bytes).hexdigest()
            pointer["artifacts"]["state"]["bytes"] = len(state_bytes)
            pointer_path.write_text(
                json.dumps(
                    pointer,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            status = self.publish(root, "provisional")

            self.assertFalse(status["initial_report_ready"])
            self.assertEqual(0, status["provisional_stake"])

    def test_artifact_state_uses_only_the_validated_csv_state_join_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            validated = {
                "provisional_plan_sha256": "a" * 64,
                "active_candidate_count": 1,
                "shadow_candidate_count": 0,
                "active_provisional_stake": 140,
                "candidates": [
                    {"route": "active", "provisional_stake": "999.9"}
                ],
            }
            with patch(
                "report_status.read_valid_decision_bundle", return_value={}
            ), patch(
                "report_status.read_valid_provisional_state",
                return_value=validated,
            ):
                state = artifact_state(root, REPORT_DATE)

            self.assertEqual(140, state["provisional_stake"])

    def test_schema_one_status_is_readable_when_publishing_a_provisional_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "web" / "report-status.json").write_text(
                json.dumps({**base_status(REPORT_DATE), "schema_version": 1, "forecast_ready": True}),
                encoding="utf-8",
            )
            status = self.publish(root, "forecast")
            self.assertEqual(2, status["schema_version"])
            self.assertTrue(status["forecast_ready"])

    def test_new_business_date_discards_yesterdays_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "web" / "report-status.json").write_text(
                json.dumps({
                    **base_status(date(2026, 7, 15)),
                    "forecast_ready": True,
                    "plan_ready": True,
                    "settlement_ready": True,
                    "decision_odds_at_bjt": "2026-07-15T14:00:00+08:00",
                    "plan_locked_at_bjt": "2026-07-15T14:01:00+08:00",
                }),
                encoding="utf-8",
            )

            status = self.publish(root, "forecast")

            self.assertTrue(status["forecast_ready"])
            self.assertFalse(status["plan_ready"])
            self.assertFalse(status["settlement_ready"])
            self.assertEqual("", status["decision_odds_at_bjt"])
            self.assertEqual("", status["plan_locked_at_bjt"])

    def test_three_same_day_phases_merge_without_losing_prior_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            self.make_decision_snapshot(root)

            self.publish(root, "forecast")
            self.publish(root, "decision")
            status = self.publish(root, "settlement", settled_through=date(2026, 7, 15))

            self.assertEqual(2, status["schema_version"])
            self.assertEqual("2026-07-16", status["report_date"])
            self.assertTrue(status["forecast_ready"])
            self.assertTrue(status["decision_snapshot_ready"])
            self.assertTrue(status["settlement_ready"])
            self.assertTrue(status["plan_ready"])
            self.assertEqual("2026-07-15", status["settled_through"])
            self.assertRegex(status["image_sha256"], r"^[0-9a-f]{64}$")

    def test_decision_requires_a_valid_lock_and_matching_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            status = self.publish(root, "decision")
            self.assertFalse(status["decision_snapshot_ready"])
            self.assertFalse(status["plan_ready"])

            self.make_lock(root)
            self.make_decision_snapshot(root)
            status = self.publish(root, "decision")
            self.assertTrue(status["decision_snapshot_ready"])
            self.assertTrue(status["plan_ready"])
            self.assertEqual("2026-07-16T13:30:00+08:00", status["decision_odds_at_bjt"])
            self.assertEqual("2026-07-16T13:31:00+08:00", status["plan_locked_at_bjt"])

    def test_decision_snapshot_accepts_the_payload_date_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            snapshots = root / "data" / "odds_snapshots"
            snapshots.mkdir()
            (snapshots / "2026-07-16-133000-decision.json").write_text(
                json.dumps({
                    "date": REPORT_DATE.isoformat(),
                    "captured_at": "2026-07-16T13:30:00+08:00",
                    "phase": "decision",
                    "matches": [{"match_id": "001"}, {"match_id": "002"}],
                }),
                encoding="utf-8",
            )

            status = self.publish(root, "decision")

            self.assertTrue(status["decision_snapshot_ready"])

    def test_matching_decision_snapshot_requires_a_nonempty_matches_list(self):
        payloads = (
            {"matches": None},
            {"matches": {"001": {}}},
            {"matches": []},
        )
        for matches_payload in payloads:
            with self.subTest(matches=matches_payload["matches"]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                snapshots = root / "data" / "odds_snapshots"
                snapshots.mkdir(parents=True)
                (snapshots / "2026-07-16-133000-decision.json").write_text(
                    json.dumps({
                        "target_date": REPORT_DATE.isoformat(),
                        "phase": "decision",
                        **matches_payload,
                    }),
                    encoding="utf-8",
                )

                self.assertEqual(
                    (False, ""),
                    _matching_decision_snapshot(root, REPORT_DATE),
                )

    def test_decision_accepts_capture_odds_snapshot_producer_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.write_producer_decision_snapshot(root)

            status = self.publish(root, "decision")

            self.assertTrue(status["decision_snapshot_ready"])
            self.assertEqual("2026-07-16T13:30:00+08:00", status["decision_odds_at_bjt"])

    def test_verified_zero_fixture_day_can_complete_without_match_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            snapshots = root / "data" / "odds_snapshots"
            snapshots.mkdir()
            (snapshots / "2026-07-16-133000-decision.json").write_text(
                json.dumps({
                    "target_date": REPORT_DATE.isoformat(),
                    "phase": "decision",
                    "matches": [],
                }),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)
            status = self.publish(root, "decision")

            self.assertEqual(0, state["fixture_count"])
            self.assertEqual(1.0, state["odds_coverage"])
            self.assertTrue(status["decision_snapshot_ready"])
            self.assertTrue(status["data_quality"]["decision_snapshot_ready"])
            self.assertEqual("", status["decision_odds_at_bjt"])
            self.assertFalse(status["plan_ready"])

    def test_every_official_source_can_verify_a_zero_fixture_day(self):
        for source in sorted(OFFICIAL_FIXTURE_SOURCES):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_artifacts(root, fixture_ids=())
                with patch.object(import_sporttery, "DATA_DIR", root / "data"):
                    import_sporttery.write_source_status(
                        source, REPORT_DATE, fixture_count=0
                    )

                state = artifact_state(root, REPORT_DATE)

                self.assertTrue(state["fixtures_ready"])
                self.assertTrue(state["zero_fixture_verified"])
                self.assertEqual(0, state["fixture_count"])

    def test_nonofficial_sources_cannot_verify_a_zero_fixture_day(self):
        for source in ("ESPN", "test", [], {}):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_artifacts(root, fixture_ids=())
                source_path = root / "data" / "source_status.json"
                source_status = json.loads(source_path.read_text(encoding="utf-8"))
                source_status["source"] = source
                source_path.write_text(json.dumps(source_status), encoding="utf-8")

                try:
                    state = artifact_state(root, REPORT_DATE)
                except TypeError as exc:
                    self.fail(
                        f"non-string source must fail closed, not crash: {exc}"
                    )
                status = self.publish(root, "decision")

                self.assertTrue(state["fixtures_ready"])
                self.assertEqual(0, state["fixture_count"])
                self.assertFalse(state["zero_fixture_verified"])
                self.assertFalse(status["decision_snapshot_ready"])
                self.assertFalse(status["data_quality"]["decision_snapshot_ready"])
                self.assertEqual(0, status["official_fixture_count"])
                self.assertEqual(0, status["official_odds_count"])
                self.assertEqual(0.0, status["official_odds_coverage_ratio"])

    def test_header_only_fixtures_require_explicit_zero_fixture_source_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            (root / "data" / "source_status.json").write_text(
                json.dumps({"target_date": REPORT_DATE.isoformat(), "source": "test"}),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)
            status = self.publish(root, "decision")

            self.assertFalse(state["fixtures_ready"])
            self.assertIsNone(state["fixture_count"])
            self.assertFalse(status["decision_snapshot_ready"])

    def test_zero_fixture_proof_rejects_a_zero_count_alias_without_an_explicit_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            (root / "data" / "source_status.json").write_text(
                json.dumps({
                    "target_date": REPORT_DATE.isoformat(),
                    "source": "test",
                    "match_count": 0,
                }),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)

            self.assertFalse(state["fixtures_ready"])
            self.assertFalse(state["zero_fixture_verified"])
            self.assertIsNone(state["fixture_count"])

    def test_zero_fixture_proof_rejects_alias_even_with_an_explicit_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            (root / "data" / "source_status.json").write_text(
                json.dumps({
                    "target_date": REPORT_DATE.isoformat(),
                    "source": "竞彩网",
                    "match_count": 0,
                    "no_fixtures": True,
                }),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)

            self.assertFalse(state["fixtures_ready"])
            self.assertFalse(state["zero_fixture_verified"])
            self.assertIsNone(state["fixture_count"])

    def test_non_object_source_status_does_not_crash_official_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "source_status.json").write_text(
                json.dumps(["invalid"]), encoding="utf-8"
            )

            try:
                state = artifact_state(root, REPORT_DATE)
            except AttributeError as exc:
                self.fail(f"malformed source metadata must fail closed, not crash: {exc}")

            self.assertTrue(state["fixtures_ready"])
            self.assertEqual(0, state["official_fixture_count"])

    def test_zero_fixture_proof_rejects_canonical_zero_count_without_explicit_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            (root / "data" / "source_status.json").write_text(
                json.dumps({
                    "target_date": REPORT_DATE.isoformat(),
                    "source": "竞彩网",
                    "fixture_count": 0,
                }),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)
            status = self.publish(root, "decision")

            self.assertFalse(state["fixtures_ready"])
            self.assertFalse(state["zero_fixture_verified"])
            self.assertIsNone(state["fixture_count"])
            self.assertFalse(status["decision_snapshot_ready"])
            self.assertFalse(status["data_quality"]["decision_snapshot_ready"])

    def test_zero_fixture_proof_rejects_a_count_that_contradicts_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            (root / "data" / "source_status.json").write_text(
                json.dumps({
                    "target_date": REPORT_DATE.isoformat(),
                    "source": "test",
                    "fixture_count": 0,
                    "no_fixtures": False,
                }),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)

            self.assertFalse(state["fixtures_ready"])
            self.assertFalse(state["zero_fixture_verified"])

    def test_fixture_proof_rejects_a_declared_count_that_differs_from_csv_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "source_status.json").write_text(
                json.dumps({
                    "target_date": REPORT_DATE.isoformat(),
                    "source": "test",
                    "fixture_count": 0,
                    "no_fixtures": True,
                }),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)

            self.assertFalse(state["fixtures_ready"])
            self.assertIsNone(state["fixture_count"])

    def test_nonzero_fixtures_expose_partial_odds_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
                json.dumps({"001": {"had": {"h": "2.0"}}, "002": {"had": {}}}),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)

            self.assertEqual(2, state["fixture_count"])
            self.assertEqual(1, state["odds_covered_fixture_count"])
            self.assertEqual(0.5, state["odds_coverage"])

    def test_status_publishes_documented_official_counts_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
                json.dumps({"001": {"had": {"h": "2.0"}}, "002": {"had": {}}}),
                encoding="utf-8",
            )

            status = self.publish(root, "forecast")

            self.assertEqual(2, status.get("official_fixture_count"))
            self.assertEqual(1, status.get("official_odds_count"))
            self.assertEqual(0.5, status.get("official_odds_coverage_ratio"))
            self.assertEqual(status["fixture_count"], status["official_fixture_count"])
            self.assertEqual(
                status["odds_covered_fixture_count"], status["official_odds_count"]
            )
            self.assertEqual(
                status["odds_coverage"], status["official_odds_coverage_ratio"]
            )

    def test_espn_only_fixtures_are_not_reported_as_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            source_path = root / "data" / "source_status.json"
            source_status = json.loads(source_path.read_text(encoding="utf-8"))
            source_status["source"] = "ESPN"
            source_path.write_text(json.dumps(source_status), encoding="utf-8")

            status = self.publish(root, "forecast")

            self.assertEqual(2, status["fixture_count"])
            self.assertEqual(2, status["odds_covered_fixture_count"])
            self.assertEqual(0, status["official_fixture_count"])
            self.assertEqual(0, status["official_odds_count"])
            self.assertEqual(0.0, status["official_odds_coverage_ratio"])

    def test_wrong_date_official_source_metadata_cannot_mark_rows_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            source_path = root / "data" / "source_status.json"
            source_status = json.loads(source_path.read_text(encoding="utf-8"))
            source_status["target_date"] = "2026-07-15"
            source_path.write_text(json.dumps(source_status), encoding="utf-8")

            status = self.publish(root, "forecast")

            self.assertEqual(2, status["fixture_count"])
            self.assertEqual(0, status["official_fixture_count"])
            self.assertEqual(0, status["official_odds_count"])
            self.assertEqual(0.0, status["official_odds_coverage_ratio"])

    def test_null_and_blank_odds_do_not_count_as_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
                json.dumps({
                    "001": {"had": {"h": None}, "hhad": {}, "ttg": {}},
                    "002": {"had": {"h": "  "}, "hhad": {}, "ttg": {}},
                }),
                encoding="utf-8",
            )

            state = artifact_state(root, REPORT_DATE)

            self.assertEqual(0, state["odds_covered_fixture_count"])
            self.assertEqual(0.0, state["odds_coverage"])

    def test_status_hash_matches_exact_png_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)

            status = self.publish(root, "forecast")

            self.assertEqual(
                hashlib.sha256(
                    (root / "web" / "daily-report.png").read_bytes()
                ).hexdigest(),
                status["image_sha256"],
            )

    def test_image_readiness_requires_exact_date_stage_and_build_metadata(self):
        variants = (
            (date(2026, 7, 18), "provisional", "build-1"),
            (REPORT_DATE, "forecast", "build-1"),
            (REPORT_DATE, "provisional", "build-2"),
        )
        for image_date, image_stage, image_build_id in variants:
            with self.subTest(
                image_date=image_date,
                image_stage=image_stage,
                image_build_id=image_build_id,
            ), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "data").mkdir()
                (root / "output").mkdir()
                self.write_report_image(
                    root, image_date, image_stage, image_build_id
                )

                state = artifact_state(
                    root,
                    REPORT_DATE,
                    expected_report_stage="provisional",
                    expected_build_id="build-1",
                )

                self.assertFalse(state["image_ready"])
                self.assertEqual("", state["image_sha256"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "output").mkdir()
            self.write_report_image(root, REPORT_DATE, "provisional", "build-1")

            state = artifact_state(
                root,
                REPORT_DATE,
                expected_report_stage="provisional",
                expected_build_id="build-1",
            )

            self.assertTrue(state["image_ready"])
            self.assertTrue(state["image_sha256"])

    def test_settlement_cannot_claim_a_date_before_yesterday(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)

            status = self.publish(root, "settlement", settled_through=date(2026, 7, 14))

            self.assertFalse(status["settlement_ready"])
            self.assertEqual("2026-07-14", status["settled_through"])

    def test_missing_fixtures_file_is_not_a_zero_fixture_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            (root / "data" / "fixtures.csv").unlink()

            state = artifact_state(root, REPORT_DATE)
            status = self.publish(root, "decision")

            self.assertFalse(state["fixtures_ready"])
            self.assertIsNone(state["fixture_count"])
            self.assertFalse(status["decision_snapshot_ready"])

    def test_rejects_malformed_artifact_headers(self):
        cases = (
            ("fixtures.csv", ["date", "match_id"], "fixtures_ready"),
            (f"predictions_{REPORT_DATE.isoformat()}.csv", ["date", "match_id"], "predictions_ready"),
            (f"betting_plan_{REPORT_DATE.isoformat()}.csv", ["date", "stake"], "plan_csv_ready"),
            ("betting_ledger.csv", ["date", "stake"], "ledger_ready"),
        )
        for filename, fields, ready_field in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_artifacts(root)
                parent = root / ("data" if filename == "fixtures.csv" else "output")
                with (parent / filename).open("w", encoding="utf-8", newline="") as handle:
                    csv.DictWriter(handle, fieldnames=fields).writeheader()

                self.assertFalse(artifact_state(root, REPORT_DATE)[ready_field])

    def test_daily_decision_requires_date_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "output" / "daily_decision_2026-07-16.json").write_text(
                json.dumps({"date": REPORT_DATE.isoformat()}), encoding="utf-8"
            )

            self.assertFalse(artifact_state(root, REPORT_DATE)["decision_ready"])

    def test_decision_requires_a_schema_valid_plan_even_with_a_valid_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            with (root / "output" / "betting_plan_2026-07-16.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                csv.DictWriter(handle, fieldnames=["date", "stake"]).writeheader()

            status = self.publish(root, "decision")

            self.assertFalse(status["plan_ready"])

    def test_forecast_readiness_uses_prediction_phase_artifacts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "output" / "betting_plan_2026-07-16.csv").unlink()
            (root / "output" / "daily_decision_2026-07-16.json").unlink()

            status = self.publish(root, "forecast")

            self.assertTrue(status["forecast_ready"])
            self.assertFalse(status["data_quality"]["plan_csv_ready"])
            self.assertFalse(status["data_quality"]["decision_ready"])

    def test_forecast_readiness_requires_domestic_odds_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "sporttery_odds_2026-07-16.json").unlink()

            status = self.publish(root, "forecast")

            self.assertFalse(status["forecast_ready"])
            self.assertFalse(status["data_quality"]["odds_ready"])

    def test_forecast_readiness_requires_an_immutable_import_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "import_manifests" / "2026-07-16.json").unlink()

            status = self.publish(root, "forecast")

            self.assertFalse(status["forecast_ready"])
            self.assertFalse(status["data_quality"]["import_manifest_ready"])

    def test_forecast_readiness_rejects_empty_domestic_odds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
                "{}\n", encoding="utf-8"
            )

            status = self.publish(root, "forecast")

            self.assertFalse(status["forecast_ready"])
            self.assertEqual(0, status["official_odds_count"])

    def test_forecast_readiness_requires_full_official_odds_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            odds_path = root / "data" / "sporttery_odds_2026-07-16.json"
            odds = json.loads(odds_path.read_text(encoding="utf-8"))
            odds.pop("002")
            odds_path.write_text(json.dumps(odds), encoding="utf-8")

            status = self.publish(root, "forecast")

            self.assertFalse(status["forecast_ready"])
            self.assertEqual(0.5, status["official_odds_coverage_ratio"])

    def test_identity_health_blocker_forces_forecast_not_ready(self):
        health = {
            "forecast_blockers": ["identity_not_unique"],
            "decision_blockers": [],
            "hard_blockers": ["identity_not_unique"],
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "report_status.build_evidence_health",
            return_value=health,
        ):
            root = Path(tmp)
            self.make_artifacts(root)

            status = self.publish(root, "forecast")

        self.assertEqual(2, status["schema_version"])
        self.assertFalse(status["forecast_ready"])
        self.assertEqual(health, status["evidence_health"])

    def test_decision_blocker_does_not_retroactively_invalidate_forecast(self):
        healthy = {
            "forecast_blockers": [],
            "decision_blockers": [],
            "hard_blockers": [],
        }
        blocked = {
            "forecast_blockers": [],
            "decision_blockers": ["decision_snapshot_incomplete"],
            "hard_blockers": ["decision_snapshot_incomplete"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_decision_snapshot(root)
            with patch(
                "report_status.build_evidence_health",
                return_value=healthy,
            ):
                forecast = self.publish(root, "forecast")
            with patch(
                "report_status.build_evidence_health",
                return_value=blocked,
            ):
                decision = self.publish(root, "decision")

        self.assertTrue(forecast["forecast_ready"])
        self.assertTrue(decision["forecast_ready"])
        self.assertFalse(decision["decision_snapshot_ready"])
        self.assertEqual(blocked, decision["evidence_health"])

    def test_empty_health_blockers_preserve_existing_decision_readiness(self):
        health = {
            "forecast_blockers": [],
            "decision_blockers": [],
            "hard_blockers": [],
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "report_status.build_evidence_health",
            return_value=health,
        ):
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_decision_snapshot(root)

            status = self.publish(root, "decision")

        self.assertTrue(status["decision_snapshot_ready"])
        self.assertEqual(health, status["evidence_health"])

    def test_invalid_snapshot_json_surfaces_decision_evidence_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            snapshots = root / "data" / "odds_snapshots"
            snapshots.mkdir()
            (snapshots / "2026-07-16-133000-decision.json").write_text(
                "{",
                encoding="utf-8",
            )

            status = self.publish(root, "decision")

        self.assertFalse(status["decision_snapshot_ready"])
        self.assertIn(
            "decision_snapshot_incomplete",
            status["evidence_health"]["decision_blockers"],
        )
        self.assertIn(
            "decision_odds_stale",
            status["evidence_health"]["decision_blockers"],
        )

    def test_decision_health_blocker_gates_provisional_readiness(self):
        health = {
            "forecast_blockers": [],
            "decision_blockers": ["decision_odds_stale"],
            "hard_blockers": ["decision_odds_stale"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.write_report_image(
                root,
                REPORT_DATE,
                "provisional",
                "123456-1-provisional",
            )
            state = artifact_state(
                root,
                REPORT_DATE,
                expected_report_stage="provisional",
                expected_build_id="123456-1-provisional",
            )
            state.update({
                "decision_bundle_ready": True,
                "provisional_plan_ready": True,
                "provisional_shadow_ready": True,
                "provisional_state_ready": True,
                "provisional_plan_count": 1,
                "provisional_shadow_count": 0,
                "provisional_plan_sha256": "a" * 64,
                "provisional_stake": 10,
            })
            with (
                patch("report_status.artifact_state", return_value=state),
                patch(
                    "report_status.build_evidence_health",
                    return_value=health,
                ),
            ):
                status = self.publish(root, "provisional")

        self.assertFalse(status["initial_report_ready"])
        self.assertFalse(status["revalidation_ready"])

    def test_settlement_rebinds_the_current_validated_provisional_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.write_report_image(
                root, REPORT_DATE, "settlement", "123456-1-settlement"
            )
            state = artifact_state(
                root,
                REPORT_DATE,
                expected_report_stage="settlement",
                expected_build_id="123456-1-settlement",
            )
            state.update({
                "decision_bundle_ready": True,
                "provisional_plan_ready": True,
                "provisional_shadow_ready": True,
                "provisional_state_ready": True,
                "provisional_plan_count": 1,
                "provisional_shadow_count": 1,
                "provisional_plan_sha256": "b" * 64,
                "provisional_stake": 55,
            })
            (root / "web" / "report-status.json").write_text(
                json.dumps({
                    **base_status(REPORT_DATE),
                    "forecast_ready": True,
                    "initial_report_ready": True,
                    "revalidation_ready": True,
                    "provisional_plan_sha256": "a" * 64,
                    "provisional_candidate_count": 1,
                    "provisional_stake": 30,
                }),
                encoding="utf-8",
            )

            with (
                patch("report_status.artifact_state", return_value=state),
                patch(
                    "report_status.build_evidence_health",
                    return_value={
                        "forecast_blockers": [],
                        "decision_blockers": [],
                        "hard_blockers": [],
                    },
                ),
            ):
                status = self.publish(
                    root, "settlement", settled_through=date(2026, 7, 15)
                )

            self.assertEqual("b" * 64, status["provisional_plan_sha256"])
            self.assertEqual(2, status["provisional_candidate_count"])
            self.assertEqual(55, status["provisional_stake"])

    def test_same_phase_rerun_invalidates_forecast_readiness_when_predictions_disappear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.assertTrue(self.publish(root, "forecast")["forecast_ready"])
            (root / "output" / "predictions_2026-07-16.csv").unlink()

            status = self.publish(root, "forecast")

            self.assertFalse(status["forecast_ready"])
            self.assertFalse(status["data_quality"]["predictions_ready"])

    def test_cross_phase_rerun_invalidates_forecast_readiness_when_predictions_disappear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.assertTrue(self.publish(root, "forecast")["forecast_ready"])
            (root / "output" / "predictions_2026-07-16.csv").unlink()

            status = self.publish(root, "decision")

            self.assertFalse(status["forecast_ready"])
            self.assertFalse(status["data_quality"]["predictions_ready"])

    def test_formerly_ready_forecast_is_invalidated_by_malformed_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.assertTrue(self.publish(root, "forecast")["forecast_ready"])
            with (root / "output" / "predictions_2026-07-16.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                csv.DictWriter(handle, fieldnames=["date", "match_id"]).writeheader()

            status = self.publish(root, "forecast")

            self.assertFalse(status["forecast_ready"])
            self.assertFalse(status["data_quality"]["predictions_ready"])

    def test_same_phase_decision_rerun_invalidates_missing_proofs_but_preserves_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            self.make_decision_snapshot(root)
            initial = self.publish(root, "decision")
            (root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json").unlink()
            (root / "output" / "plan_lock_2026-07-16.json").unlink()

            status = self.publish(root, "decision")

            self.assertFalse(status["decision_snapshot_ready"])
            self.assertFalse(status["plan_ready"])
            self.assertEqual(initial["decision_odds_at_bjt"], status["decision_odds_at_bjt"])
            self.assertEqual(initial["plan_locked_at_bjt"], status["plan_locked_at_bjt"])

    def test_formerly_ready_decision_is_invalidated_by_malformed_proofs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            self.make_decision_snapshot(root)
            initial = self.publish(root, "decision")
            self.assertTrue(initial["decision_snapshot_ready"])
            self.assertTrue(initial["plan_ready"])
            (root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json").write_text(
                "{", encoding="utf-8"
            )
            (root / "output" / "plan_lock_2026-07-16.json").write_text(
                "{", encoding="utf-8"
            )
            with (root / "output" / "betting_plan_2026-07-16.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                csv.DictWriter(handle, fieldnames=["date", "stake"]).writeheader()

            status = self.publish(root, "decision")

            self.assertFalse(status["decision_snapshot_ready"])
            self.assertFalse(status["plan_ready"])
            self.assertFalse(status["data_quality"]["decision_snapshot_ready"])
            self.assertFalse(status["data_quality"]["plan_lock_ready"])
            self.assertFalse(status["data_quality"]["plan_csv_ready"])

    def test_decision_rerun_keeps_latest_timestamps_when_only_older_artifacts_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root, datetime(2026, 7, 16, 14, 1, tzinfo=BJT))
            older_snapshot = self.write_decision_snapshot(root, "133000")
            newer_snapshot = self.write_decision_snapshot(root, "140000")
            initial = self.publish(root, "decision")
            self.assertEqual("2026-07-16T14:00:00+08:00", initial["decision_odds_at_bjt"])
            self.assertEqual("2026-07-16T14:01:00+08:00", initial["plan_locked_at_bjt"])

            newer_snapshot.unlink()
            lock_path = root / "output" / "plan_lock_2026-07-16.json"
            lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
            lock_payload["locked_at_bjt"] = "2026-07-16T13:31:00+08:00"
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            self.assertTrue(older_snapshot.exists())

            status = self.publish(root, "decision")

            self.assertEqual("2026-07-16T14:00:00+08:00", status["decision_odds_at_bjt"])
            self.assertEqual("2026-07-16T14:01:00+08:00", status["plan_locked_at_bjt"])

    def test_same_phase_settlement_rerun_invalidates_readiness_when_ledger_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            initial = self.publish(root, "settlement", settled_through=date(2026, 7, 15))
            self.assertTrue(initial["settlement_ready"])
            (root / "output" / "betting_ledger.csv").unlink()

            status = self.publish(root, "settlement", settled_through=date(2026, 7, 15))

            self.assertFalse(status["settlement_ready"])
            self.assertFalse(status["data_quality"]["ledger_ready"])

    def test_cross_phase_rerun_invalidates_settlement_readiness_when_ledger_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            initial = self.publish(root, "settlement", settled_through=date(2026, 7, 15))
            self.assertTrue(initial["settlement_ready"])
            (root / "output" / "betting_ledger.csv").unlink()

            status = self.publish(root, "forecast")

            self.assertFalse(status["settlement_ready"])
            self.assertEqual("2026-07-15", status["settled_through"])
            self.assertFalse(status["data_quality"]["ledger_ready"])

    def test_formerly_ready_settlement_is_invalidated_by_a_malformed_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            initial = self.publish(root, "settlement", settled_through=date(2026, 7, 15))
            self.assertTrue(initial["settlement_ready"])
            with (root / "output" / "betting_ledger.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                csv.DictWriter(handle, fieldnames=["date", "stake"]).writeheader()

            status = self.publish(root, "settlement", settled_through=date(2026, 7, 15))

            self.assertFalse(status["settlement_ready"])
            self.assertFalse(status["data_quality"]["ledger_ready"])

    def test_settlement_rerun_does_not_regress_the_settlement_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.publish(root, "settlement", settled_through=date(2026, 7, 15))

            status = self.publish(root, "settlement", settled_through=date(2026, 7, 14))

            self.assertEqual("2026-07-15", status["settled_through"])

    def test_cross_phase_rerun_invalidates_missing_decision_proofs_but_preserves_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            self.make_decision_snapshot(root)
            initial = self.publish(root, "decision")
            (root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json").unlink()
            (root / "output" / "plan_lock_2026-07-16.json").unlink()

            status = self.publish(root, "forecast")

            self.assertFalse(status["decision_snapshot_ready"])
            self.assertFalse(status["plan_ready"])
            self.assertEqual(initial["decision_odds_at_bjt"], status["decision_odds_at_bjt"])
            self.assertEqual(initial["plan_locked_at_bjt"], status["plan_locked_at_bjt"])

    def test_publication_replaces_the_status_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)

            self.publish(root, "forecast")

            status_path = root / "web" / "report-status.json"
            self.assertEqual("2026-07-16", json.loads(status_path.read_text(encoding="utf-8"))["report_date"])
            self.assertFalse((root / "web" / "report-status.json.tmp").exists())

    def test_cli_rejects_invalid_required_values(self):
        invalid_arguments = (
            ["--date", "2026-07-16", "--phase", "decision", "--build-id", "", "--source-commit", "abc", "--generated-at", "2026-07-16T13:35:00+08:00"],
            ["--date", "2026-07-16", "--phase", "decision", "--build-id", "build", "--source-commit", "", "--generated-at", "2026-07-16T13:35:00+08:00"],
            ["--date", "2026-07-16", "--phase", "decision", "--build-id", "build", "--source-commit", "abc", "--generated-at", "2026-07-16T13:35:00"],
            ["--date", "2026-07-16", "--phase", "other", "--build-id", "build", "--source-commit", "abc", "--generated-at", "2026-07-16T13:35:00+08:00"],
            ["--date", "2026-07-16", "--phase", "settlement", "--build-id", "build", "--source-commit", "abc", "--generated-at", "2026-07-16T13:35:00+08:00"],
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), patch.object(
                sys, "argv", ["report_status.py", *arguments]
            ), patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertNotEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
