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

from plan_lock import lock_plan
from report_status import artifact_state, base_status, main, publish_status


BJT = timezone(timedelta(hours=8))
REPORT_DATE = date(2026, 7, 16)
GENERATED_AT = datetime(2026, 7, 16, 13, 35, tzinfo=BJT)


class ReportStatusTest(unittest.TestCase):
    def make_artifacts(self, root: Path, fixture_ids=("001", "002")) -> None:
        data = root / "data"
        output = root / "output"
        web = root / "web"
        data.mkdir(parents=True)
        output.mkdir()
        web.mkdir()
        (data / "source_status.json").write_text(
            json.dumps({"target_date": REPORT_DATE.isoformat(), "source": "test"}),
            encoding="utf-8",
        )
        with (data / "fixtures.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "match_id"])
            writer.writeheader()
            for match_id in fixture_ids:
                writer.writerow({"date": REPORT_DATE.isoformat(), "match_id": match_id})
            writer.writerow({"date": "2026-07-15", "match_id": "yesterday"})
        odds = {
            match_id: {"had": {"h": "2.0"}, "hhad": {}, "ttg": {}}
            for match_id in fixture_ids
        }
        (data / f"sporttery_odds_{REPORT_DATE.isoformat()}.json").write_text(
            json.dumps(odds), encoding="utf-8"
        )
        for name, fieldnames in (
            (f"predictions_{REPORT_DATE.isoformat()}.csv", ["date", "match_id"]),
            (f"betting_plan_{REPORT_DATE.isoformat()}.csv", ["date", "match_id", "stake"]),
        ):
            with (output / name).open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=fieldnames).writeheader()
        (output / f"daily_decision_{REPORT_DATE.isoformat()}.json").write_text(
            json.dumps({"date": REPORT_DATE.isoformat(), "status": "no_bet"}),
            encoding="utf-8",
        )
        (output / "betting_ledger.csv").write_text(
            "date,play,match,selection,stake,status,profit\n", encoding="utf-8"
        )
        (web / "index.html").write_text("<html></html>", encoding="utf-8")
        (web / "daily-report.png").write_bytes(b"exact png bytes")

    def make_lock(self, root: Path) -> None:
        lock_plan(
            root,
            REPORT_DATE,
            datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
            "test",
        )

    def make_decision_snapshot(self, root: Path) -> None:
        snapshots = root / "data" / "odds_snapshots"
        snapshots.mkdir()
        (snapshots / "2026-07-16-133000-decision.json").write_text(
            json.dumps({"target_date": REPORT_DATE.isoformat(), "phase": "decision"}),
            encoding="utf-8",
        )

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
                }),
                encoding="utf-8",
            )

            status = self.publish(root, "forecast")

            self.assertTrue(status["forecast_ready"])
            self.assertFalse(status["plan_ready"])
            self.assertFalse(status["settlement_ready"])

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
