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

import import_sporttery
from plan_lock import lock_plan
from report_status import artifact_state, base_status, main, publish_status


BJT = timezone(timedelta(hours=8))
REPORT_DATE = date(2026, 7, 16)
GENERATED_AT = datetime(2026, 7, 16, 13, 35, tzinfo=BJT)
FIXTURE_FIELDS = [
    "date", "kickoff_local", "stage", "team_a", "team_b", "neutral", "venue",
    "odds_a", "odds_draw", "odds_b", "market_odds_a", "market_odds_draw",
    "market_odds_b", "analysis_source", "is_single_had", "match_num", "match_id",
    "pool_status",
]
PREDICTION_FIELDS = [
    "date", "kickoff", "stage", "match_num", "match_id", "team_a", "team_b",
    "xg_a", "xg_b", "p_a", "p_draw", "p_b", "adv_a", "adv_b", "pick",
    "confidence", "analysis_source", "is_single_had", "score_1", "score_1_prob",
    "score_2", "score_2_prob", "score_3", "score_3_prob",
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
    def make_artifacts(self, root: Path, fixture_ids=("001", "002")) -> None:
        data = root / "data"
        output = root / "output"
        web = root / "web"
        data.mkdir(parents=True)
        output.mkdir()
        web.mkdir()
        source_status = {"target_date": REPORT_DATE.isoformat(), "source": "test"}
        if not fixture_ids:
            source_status["fixture_count"] = 0
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
                    "team_a": "A",
                    "team_b": "B",
                    "neutral": "false",
                    "venue": "test",
                    "match_id": match_id,
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
                })
        odds = {
            match_id: {"had": {"h": "2.0"}, "hhad": {}, "ttg": {}}
            for match_id in fixture_ids
        }
        (data / f"sporttery_odds_{REPORT_DATE.isoformat()}.json").write_text(
            json.dumps(odds), encoding="utf-8"
        )
        for name, fieldnames in (
            (f"predictions_{REPORT_DATE.isoformat()}.csv", PREDICTION_FIELDS),
            (f"betting_plan_{REPORT_DATE.isoformat()}.csv", PLAN_FIELDS),
        ):
            with (output / name).open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=fieldnames).writeheader()
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
        lock_plan(
            root,
            REPORT_DATE,
            locked_at,
            "test",
        )

    def make_decision_snapshot(self, root: Path) -> None:
        snapshots = root / "data" / "odds_snapshots"
        snapshots.mkdir()
        (snapshots / "2026-07-16-133000-decision.json").write_text(
            json.dumps({"target_date": REPORT_DATE.isoformat(), "phase": "decision"}),
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
                "matches": [{"match_num": "001"}],
            }),
            encoding="utf-8",
        )

    def write_decision_snapshot(self, root: Path, timestamp: str) -> Path:
        snapshots = root / "data" / "odds_snapshots"
        snapshots.mkdir(exist_ok=True)
        path = snapshots / f"2026-07-16-{timestamp}-decision.json"
        path.write_text(
            json.dumps({"target_date": REPORT_DATE.isoformat(), "phase": "decision"}),
            encoding="utf-8",
        )
        return path

    def publish(self, root: Path, phase: str, **kwargs) -> dict:
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
                "schema_version": 1,
                "report_date": "2026-07-16",
                "forecast_ready": False,
                "decision_snapshot_ready": False,
                "settlement_ready": False,
                "plan_ready": False,
                "settled_through": "",
                "decision_odds_at_bjt": "",
                "plan_locked_at_bjt": "",
            },
            base_status(REPORT_DATE),
        )

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

            self.assertEqual(1, status["schema_version"])
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
                json.dumps({"date": REPORT_DATE.isoformat(), "phase": "decision"}),
                encoding="utf-8",
            )

            status = self.publish(root, "decision")

            self.assertTrue(status["decision_snapshot_ready"])

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

            state = artifact_state(root, REPORT_DATE)
            status = self.publish(root, "decision")

            self.assertEqual(0, state["fixture_count"])
            self.assertEqual(1.0, state["odds_coverage"])
            self.assertTrue(status["decision_snapshot_ready"])
            self.assertFalse(status["plan_ready"])

    def test_import_producer_zero_metadata_verifies_a_zero_fixture_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root, fixture_ids=())
            with patch.object(import_sporttery, "DATA_DIR", root / "data"):
                import_sporttery.write_source_status(
                    "test", REPORT_DATE, fixture_count=0
                )

            state = artifact_state(root, REPORT_DATE)

            self.assertTrue(state["fixtures_ready"])
            self.assertTrue(state["zero_fixture_verified"])
            self.assertEqual(0, state["fixture_count"])

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
                hashlib.sha256(b"exact png bytes").hexdigest(),
                status["image_sha256"],
            )

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
            with (root / "output" / "betting_plan_2026-07-16.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                csv.DictWriter(handle, fieldnames=["date", "stake"]).writeheader()
            self.make_lock(root)

            status = self.publish(root, "decision")

            self.assertFalse(status["plan_ready"])

    def test_same_phase_rerun_preserves_prior_forecast_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.assertTrue(self.publish(root, "forecast")["forecast_ready"])
            (root / "output" / "predictions_2026-07-16.csv").unlink()

            status = self.publish(root, "forecast")

            self.assertTrue(status["forecast_ready"])
            self.assertFalse(status["data_quality"]["predictions_ready"])

    def test_same_phase_decision_rerun_preserves_prior_readiness_and_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            self.make_decision_snapshot(root)
            initial = self.publish(root, "decision")
            (root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json").unlink()
            (root / "output" / "plan_lock_2026-07-16.json").unlink()

            status = self.publish(root, "decision")

            self.assertTrue(status["decision_snapshot_ready"])
            self.assertTrue(status["plan_ready"])
            self.assertEqual(initial["decision_odds_at_bjt"], status["decision_odds_at_bjt"])
            self.assertEqual(initial["plan_locked_at_bjt"], status["plan_locked_at_bjt"])

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

    def test_same_phase_settlement_rerun_preserves_prior_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            initial = self.publish(root, "settlement", settled_through=date(2026, 7, 15))
            self.assertTrue(initial["settlement_ready"])
            (root / "output" / "betting_ledger.csv").unlink()

            status = self.publish(root, "settlement", settled_through=date(2026, 7, 15))

            self.assertTrue(status["settlement_ready"])
            self.assertFalse(status["data_quality"]["ledger_ready"])

    def test_settlement_rerun_does_not_regress_the_settlement_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.publish(root, "settlement", settled_through=date(2026, 7, 15))

            status = self.publish(root, "settlement", settled_through=date(2026, 7, 14))

            self.assertEqual("2026-07-15", status["settled_through"])

    def test_cross_phase_rerun_preserves_prior_decision_readiness_and_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            self.make_decision_snapshot(root)
            initial = self.publish(root, "decision")
            (root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json").unlink()
            (root / "output" / "plan_lock_2026-07-16.json").unlink()

            status = self.publish(root, "forecast")

            self.assertTrue(status["decision_snapshot_ready"])
            self.assertTrue(status["plan_ready"])
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
