import csv
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import capture_odds_snapshot
import report_status
from report_status import FIXTURE_REQUIRED_FIELDS, OFFICIAL_FIXTURE_SOURCES


TARGET_DATE = "2026-07-16"
TARGET_DATE_VALUE = date.fromisoformat(TARGET_DATE)


class CaptureOddsSnapshotCliTest(unittest.TestCase):
    def run_main(self, root: Path, phase: str, capture_result):
        with patch.object(capture_odds_snapshot, "ROOT", root), patch.object(
            capture_odds_snapshot, "capture", return_value=capture_result
        ), patch.object(
            sys,
            "argv",
            ["capture_odds_snapshot.py", "--date", TARGET_DATE, "--phase", phase],
        ):
            return capture_odds_snapshot.main()

    def write_source_status(self, root: Path, payload: dict) -> None:
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "source_status.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def write_fixtures(
        self,
        root: Path,
        rows=(),
        fieldnames=tuple(sorted(FIXTURE_REQUIRED_FIELDS)),
    ) -> None:
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        with (data / "fixtures.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_decision_returns_nonzero_when_capture_is_empty_and_zero_day_is_unproven(self):
        invalid_proofs = (
            None,
            {"target_date": TARGET_DATE, "fixture_count": 1, "no_fixtures": False},
            {"target_date": TARGET_DATE, "fixture_count": 0},
            {"target_date": "2026-07-15", "fixture_count": 0, "no_fixtures": True},
        )
        for source_status in invalid_proofs:
            with self.subTest(source_status=source_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if source_status is not None:
                    self.write_source_status(root, source_status)
                self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_returns_zero_for_a_nonempty_written_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot.json"
            snapshot.write_text(
                json.dumps({"matches": [{"match_id": "001"}]}),
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_main(root, "decision", snapshot))

    def test_decision_rejects_an_unproven_empty_written_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps({"matches": []}), encoding="utf-8")

            self.assertNotEqual(0, self.run_main(root, "decision", snapshot))

    def test_decision_accepts_a_proven_empty_written_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = next(iter(OFFICIAL_FIXTURE_SOURCES))
            self.write_source_status(root, {
                "source": source,
                "target_date": TARGET_DATE,
                "fixture_count": 0,
                "no_fixtures": True,
            })
            self.write_fixtures(root)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps({"matches": []}), encoding="utf-8")

            self.assertEqual(0, self.run_main(root, "decision", snapshot))

    def test_decision_returns_zero_for_each_official_zero_fixture_proof(self):
        for source in sorted(OFFICIAL_FIXTURE_SOURCES):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": source,
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                })
                self.write_fixtures(root)
                self.assertEqual(0, self.run_main(root, "decision", None))

    def test_decision_rejects_nonofficial_zero_fixture_sources(self):
        for source in ("ESPN", "test", [], {}):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": source,
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                })
                self.write_fixtures(root)
                try:
                    result = self.run_main(root, "decision", None)
                except TypeError as exc:
                    self.fail(
                        f"non-string source must return nonzero, not crash: {exc}"
                    )
                self.assertNotEqual(0, result)

    def test_decision_zero_fixture_proof_requires_a_readable_date_column(self):
        fixture_setups = (
            ("missing", None),
            ("bad header", ((), ("match_id",))),
        )
        for label, fixture_setup in fixture_setups:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": "竞彩网",
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                })
                if fixture_setup is not None:
                    rows, fieldnames = fixture_setup
                    self.write_fixtures(root, rows=rows, fieldnames=fieldnames)
                self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_zero_fixture_proof_rejects_a_target_date_csv_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_source_status(root, {
                "source": "竞彩网",
                "target_date": TARGET_DATE,
                "fixture_count": 0,
                "no_fixtures": True,
            })
            self.write_fixtures(root, rows=({"date": TARGET_DATE},))
            self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_zero_fixture_proof_rejects_conflicting_count_aliases(self):
        conflicts = (
            {"match_count": 1},
            {"fixtures_count": 1},
            {"fixtures_count": 0, "match_count": 1},
        )
        for aliases in conflicts:
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": "竞彩网",
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                    **aliases,
                })
                self.write_fixtures(root)

                self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_empty_capture_matches_report_zero_fixture_authority(self):
        self.assertTrue(
            hasattr(report_status, "verified_zero_fixture_day"),
            "report_status must expose the shared zero-fixture authority",
        )
        cases = (
            ({}, True),
            ({"match_count": 1}, False),
        )
        for aliases, expected in cases:
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": "中国足彩网",
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                    **aliases,
                })
                self.write_fixtures(root)

                report_verified = report_status.verified_zero_fixture_day(
                    root, TARGET_DATE_VALUE
                )
                capture_verified = self.run_main(root, "decision", None) == 0

                self.assertEqual(expected, report_verified)
                self.assertEqual(report_verified, capture_verified)

    def test_decision_zero_fixture_proof_allows_rows_for_other_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_source_status(root, {
                "source": "中国足彩网",
                "target_date": TARGET_DATE,
                "fixture_count": 0,
                "no_fixtures": True,
            })
            self.write_fixtures(root, rows=({"date": "2026-07-15"},))
            self.assertEqual(0, self.run_main(root, "decision", None))

    def test_optional_phases_keep_empty_capture_success_semantics(self):
        for phase in ("opening", "monitoring"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(0, self.run_main(Path(tmp), phase, None))

    def test_live_flag_delegates_to_immutable_live_capture_and_prints_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "data" / "live_odds_snapshots" / TARGET_DATE / "live.json"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(json.dumps({"matches": [{"match_id": "live-1"}]}), encoding="utf-8")
            with (
                patch.object(capture_odds_snapshot, "ROOT", root),
                patch.object(capture_odds_snapshot, "capture_live_snapshot", return_value=snapshot) as capture_live,
                patch.object(sys, "argv", [
                    "capture_odds_snapshot.py", "--date", TARGET_DATE, "--phase", "decision", "--live", "--print-path",
                ]),
                patch("builtins.print") as output,
            ):
                self.assertEqual(0, capture_odds_snapshot.main())

        capture_live.assert_called_once()
        self.assertEqual((root, TARGET_DATE_VALUE), capture_live.call_args.args[:2])
        self.assertEqual("decision", capture_live.call_args.kwargs["phase"])
        output.assert_any_call(str(snapshot))


class CaptureOddsSnapshotProductionTest(unittest.TestCase):
    def write_import_contract(self, root: Path, source: str) -> Path:
        data = root / "data"
        manifests = data / "import_manifests"
        snapshots = data / "odds_snapshots"
        manifests.mkdir(parents=True)
        snapshots.mkdir(parents=True)
        fixtures = data / "fixtures.csv"
        with fixtures.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "date", "kickoff_at", "stage", "team_a", "team_b",
                    "is_single_had", "is_single_hhad", "is_single_ttg",
                    "match_num", "match_id",
                ),
            )
            writer.writeheader()
            writer.writerow({
                "date": TARGET_DATE,
                "kickoff_at": "2026-07-16T20:00:00+08:00",
                "stage": "Test",
                "team_a": "Imported Home",
                "team_b": "Imported Away",
                "is_single_had": "true",
                "is_single_hhad": "false",
                "is_single_ttg": "false",
                "match_num": "001",
                "match_id": "import-1",
            })
        odds = data / f"sporttery_odds_{TARGET_DATE}.json"
        odds.write_text(
            json.dumps({"import-1": {"had": {"h": "2.00", "d": "3.00", "a": "4.00"}}}),
            encoding="utf-8",
        )
        extracts = data / "import_extracts" / TARGET_DATE
        extracts.mkdir(parents=True)
        extract_fixtures = extracts / "fixtures.csv"
        extract_odds = extracts / "odds.json"
        extract_ratings = extracts / "ratings.csv"
        extract_fixtures.write_bytes(fixtures.read_bytes())
        extract_odds.write_bytes(odds.read_bytes())
        extract_ratings.write_text(
            "team,elo\nImported Home,1500\nImported Away,1490\n",
            encoding="utf-8-sig",
        )

        def record(path: Path) -> dict:
            payload = path.read_bytes()
            return {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }

        manifest = manifests / f"{TARGET_DATE}.json"
        manifest.write_text(
            json.dumps({
                "schema_version": 2,
                "target_date": TARGET_DATE,
                "source": source,
                "imported_at_bjt": "2026-07-16T13:00:00+08:00",
                "fixtures": record(extract_fixtures),
                "odds": record(extract_odds),
                "ratings": record(extract_ratings),
            }),
            encoding="utf-8",
        )
        return manifest

    def test_capture_uses_sporttery_import_when_later_availability_flips_to_zgzcw(self):
        captured_at = datetime(2026, 7, 16, 13, 30, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.write_import_contract(root, "sporttery")
            with patch.object(capture_odds_snapshot, "ROOT", root), patch.object(
                capture_odds_snapshot, "SNAPSHOT_DIR", root / "data" / "odds_snapshots"
            ), patch.object(
                capture_odds_snapshot,
                "fetch_selling_matches",
                side_effect=RuntimeError("Sporttery now unavailable"),
            ) as fetch_direct, patch.object(
                capture_odds_snapshot,
                "fetch_zgzcw_matches",
                return_value=[{"matchId": "network-z", "homeTeam": "Z", "awayTeam": "G"}],
            ) as fetch_fallback:
                output = capture_odds_snapshot.capture(
                    TARGET_DATE_VALUE, captured_at=captured_at
                )
            payload = json.loads(output.read_text(encoding="utf-8"))
            manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()

        self.assertEqual("sporttery", payload["source"])
        self.assertEqual(["import-1"], [row["match_id"] for row in payload["matches"]])
        self.assertEqual(
            "data/import_manifests/2026-07-16.json",
            payload["import_manifest"]["path"],
        )
        self.assertEqual(manifest_hash, payload["import_manifest"]["sha256"])
        fetch_direct.assert_not_called()
        fetch_fallback.assert_not_called()

    def test_capture_uses_zgzcw_import_when_later_availability_flips_to_sporttery(self):
        captured_at = datetime(2026, 7, 16, 13, 30, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_import_contract(root, "zgzcw")
            with patch.object(capture_odds_snapshot, "ROOT", root), patch.object(
                capture_odds_snapshot, "SNAPSHOT_DIR", root / "data" / "odds_snapshots"
            ), patch.object(
                capture_odds_snapshot,
                "fetch_selling_matches",
                return_value=[{"matchId": "network-s", "homeTeam": "S", "awayTeam": "P"}],
            ) as fetch_direct, patch.object(
                capture_odds_snapshot, "fetch_zgzcw_matches"
            ) as fetch_fallback:
                output = capture_odds_snapshot.capture(
                    TARGET_DATE_VALUE, captured_at=captured_at
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("zgzcw", payload["source"])
        self.assertEqual(["import-1"], [row["match_id"] for row in payload["matches"]])
        fetch_direct.assert_not_called()
        fetch_fallback.assert_not_called()

if __name__ == "__main__":
    unittest.main()
