import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import live_odds
from model_metrics import snapshot_coverage, summarize


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 21)


def live_match():
    return {
        "matchId": "m1",
        "matchNumStr": "Monday001",
        "homeTeam": "Home",
        "awayTeam": "Away",
        "matchStatus": "Selling",
        "kickoff_at": "2026-07-21T18:00:00+08:00",
        "isSingleHad": True,
        "isSingleHhad": False,
        "isSingleTtg": False,
    }


def live_odds_payload():
    return {
        "had": {"h": "2.80", "d": "3.10", "a": "2.25"},
        "hhad": {},
        "ttg": {},
    }


def file_record(root: Path, path: Path) -> dict:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def write_import_contract(root: Path, fixtures: list[dict], source="sporttery") -> Path:
    data = root / "data"
    extracts = data / "import_extracts" / DAY.isoformat()
    manifests = data / "import_manifests"
    snapshots = data / "odds_snapshots"
    extracts.mkdir(parents=True)
    manifests.mkdir()
    snapshots.mkdir()
    fixture_path = extracts / "fixtures.csv"
    with fixture_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("date", "team_a", "team_b", "match_id", "kickoff_at"),
        )
        writer.writeheader()
        writer.writerows(fixtures)
    odds_path = extracts / "odds.json"
    odds_path.write_text("{}\n", encoding="utf-8")
    ratings_path = extracts / "ratings.csv"
    ratings_path.write_text("team,elo\nHome,1500\n", encoding="utf-8")
    manifest_path = manifests / f"{DAY.isoformat()}.json"
    manifest_path.write_text(
        json.dumps({
            "schema_version": 2,
            "target_date": DAY.isoformat(),
            "source": source,
            "imported_at_bjt": "2026-07-21T09:00:00+08:00",
            "fixtures": file_record(root, fixture_path),
            "odds": file_record(root, odds_path),
            "ratings": file_record(root, ratings_path),
        }),
        encoding="utf-8",
    )
    return manifest_path


def write_legacy_snapshot(
    root: Path,
    match: dict,
    captured: datetime,
    requested_phase: str,
) -> Path:
    manifest_path = root / "data" / "import_manifests" / f"{DAY.isoformat()}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    kickoff = datetime.fromisoformat(match["kickoff_at"])
    minutes = int((kickoff - captured).total_seconds() // 60)
    match_phase = requested_phase
    if requested_phase == "monitoring" and minutes <= 60:
        match_phase = "pre_kickoff"
    payload = {
        "target_date": DAY.isoformat(),
        "captured_at": captured.isoformat(),
        "capture_phase": requested_phase,
        "source": manifest["source"],
        "import_manifest": file_record(root, manifest_path),
        "matches": [{
            "match_id": match["match_id"],
            "team_a": match["team_a"],
            "team_b": match["team_b"],
            "kickoff_at": match["kickoff_at"],
            "capture_phase": match_phase,
            "minutes_to_kickoff": minutes,
        }],
    }
    path = (
        root
        / "data"
        / "odds_snapshots"
        / (
            f"{DAY.isoformat()}-{captured.strftime('%H%M%S')}-"
            f"{requested_phase}.json"
        )
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ModelMetricsTest(unittest.TestCase):
    def test_risk_and_calibration_metrics_are_reported(self):
        rows = [
            {"date": "2026-07-01", "play": "平局单场", "stage": "联赛A", "probability": "0.60", "odds": "2", "stake": "10", "status": "命中", "profit": "10"},
            {"date": "2026-07-02", "play": "平局单场", "stage": "联赛A", "probability": "0.60", "odds": "2", "stake": "10", "status": "未中", "profit": "-10"},
            {"date": "2026-07-03", "play": "胜平负2串1", "stage": "联赛B", "probability": "0.40", "odds": "3", "stake": "10", "status": "未中", "profit": "-10"},
            {"date": "2026-07-04", "play": "胜平负2串1", "stage": "联赛B", "probability": "0.40", "odds": "3", "stake": "10", "status": "命中", "profit": "20"},
        ]

        metrics = summarize(rows)
        overall = metrics["overall"]

        self.assertEqual(20.0, overall["max_drawdown"])
        self.assertEqual(2, overall["max_losing_streak"])
        self.assertEqual(0, overall["current_losing_streak"])
        self.assertIsNotNone(overall["calibration_error"])
        self.assertEqual(2, metrics["by_play"]["平局单场"]["count"])
        self.assertEqual(2, metrics["by_play"]["胜平负串关"]["count"])
        self.assertIn("profit", metrics["by_play"]["胜平负串关"])
        self.assertEqual(metrics["by_play"], metrics["by_play_all"])

    def test_snapshot_coverage_separates_opening_decision_and_closing_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = [
                {
                    "date": DAY.isoformat(),
                    "team_a": f"Home {index}",
                    "team_b": f"Away {index}",
                    "match_id": f"match-{index}",
                    "kickoff_at": "2026-07-21T18:00:00+08:00",
                }
                for index in range(3)
            ]
            write_import_contract(root, fixtures)
            write_legacy_snapshot(
                root,
                fixtures[0],
                datetime(2026, 7, 21, 10, 0, tzinfo=BJT),
                "opening",
            )
            write_legacy_snapshot(
                root,
                fixtures[1],
                datetime(2026, 7, 21, 11, 0, tzinfo=BJT),
                "decision",
            )
            write_legacy_snapshot(
                root,
                fixtures[2],
                datetime(2026, 7, 21, 17, 15, tzinfo=BJT),
                "monitoring",
            )

            coverage = snapshot_coverage(root / "data" / "odds_snapshots")

        self.assertEqual(3, coverage["files"])
        self.assertEqual(1, coverage["phases"]["opening"])
        self.assertEqual(1, coverage["phases"]["decision"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff"])

    def test_counts_nested_live_pre_kickoff_phases_once_per_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            for captured_at in (
                datetime(2026, 7, 21, 16, 45, tzinfo=BJT),
                datetime(2026, 7, 21, 17, 15, tzinfo=BJT),
            ):
                live_odds.capture_live_snapshot(
                    root,
                    DAY,
                    captured_at,
                    phase="decision",
                    sporttery_fetcher=lambda target_date: [live_match()],
                    sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
                )

            coverage = snapshot_coverage(
                legacy,
                root / "data" / "live_odds_snapshots",
                DAY,
            )

        self.assertEqual(2, coverage["files"])
        self.assertEqual(2, coverage["matches"])
        self.assertEqual(1, coverage["unique_fixture_bindings"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff_90"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff_30"])
        self.assertEqual(1, coverage["requested_phases"]["decision"])
        self.assertEqual(
            [[DAY.isoformat(), "Home", "Away", "m1"]],
            coverage["bindings_by_requested_phase"]["decision"],
        )
        self.assertEqual(2, coverage["coverage_schema_version"])
        self.assertEqual(
            "validated_match_observations",
            coverage["counting_units"]["matches"],
        )

    def test_snapshot_coverage_excludes_files_captured_after_as_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for captured_at in (
                datetime(2026, 7, 21, 13, 45, tzinfo=BJT),
                datetime(2026, 7, 21, 14, 5, tzinfo=BJT),
            ):
                live_odds.capture_live_snapshot(
                    root,
                    DAY,
                    captured_at,
                    phase="decision",
                    sporttery_fetcher=lambda target_date: [live_match()],
                    sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
                )

            coverage = snapshot_coverage(
                root / "legacy",
                root / "data" / "live_odds_snapshots",
                DAY,
                not_after=datetime(2026, 7, 21, 14, 0, tzinfo=BJT),
            )

        self.assertEqual(1, coverage["files"])
        self.assertEqual(1, coverage["matches"])
        self.assertEqual(
            "2026-07-21T13:45:00+08:00",
            coverage["latest_by_requested_phase"]["decision"],
        )
        self.assertEqual(
            [{
                "binding": [DAY.isoformat(), "Home", "Away", "m1"],
                "captured_at": "2026-07-21T13:45:00+08:00",
            }],
            coverage["latest_by_binding_by_requested_phase"]["decision"],
        )

    def test_legacy_matches_and_phases_remain_observation_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = {
                "date": DAY.isoformat(),
                "team_a": "Home",
                "team_b": "Away",
                "match_id": "m1",
                "kickoff_at": "2026-07-21T18:00:00+08:00",
            }
            write_import_contract(root, [fixture])
            write_legacy_snapshot(
                root,
                fixture,
                datetime(2026, 7, 21, 13, 30, tzinfo=BJT),
                "decision",
            )
            write_legacy_snapshot(
                root,
                fixture,
                datetime(2026, 7, 21, 13, 45, tzinfo=BJT),
                "decision",
            )

            coverage = snapshot_coverage(root / "data" / "odds_snapshots")

        self.assertEqual(2, coverage["matches"])
        self.assertEqual(2, coverage["phases"]["decision"])
        self.assertEqual(1, coverage["unique_fixture_bindings"])
        self.assertEqual(1, coverage["unique_phases"]["decision"])

    def test_valid_v1_live_snapshot_does_not_synthesize_phase_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = live_odds.capture_live_snapshot(
                root,
                DAY,
                datetime(2026, 7, 21, 16, 45, tzinfo=BJT),
                phase="decision",
                sporttery_fetcher=lambda target_date: [live_match()],
                sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
            )
            payload = json.loads(original.read_text(encoding="utf-8"))
            payload["schema_version"] = 1
            payload.pop("capture_phase")
            payload["matches"][0].pop("capture_phase")
            payload["matches"][0].pop("minutes_to_kickoff")
            raw = live_odds._canonical_json_bytes(payload)
            captured = datetime.fromisoformat(payload["captured_at"])
            historical = original.with_name(
                live_odds._filename(captured, payload["source"], raw)
            )
            historical.write_bytes(raw)
            original.unlink()

            coverage = snapshot_coverage(
                root / "legacy",
                root / "data" / "live_odds_snapshots",
                DAY,
            )

        self.assertEqual(1, coverage["files"])
        self.assertEqual(1, coverage["matches"])
        self.assertEqual({}, coverage["requested_phases"])
        self.assertTrue(all(count == 0 for count in coverage["phases"].values()))
        self.assertEqual({}, coverage["bindings_by_requested_phase"])

    def test_forged_live_phase_evidence_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = live_odds.capture_live_snapshot(
                root,
                DAY,
                datetime(2026, 7, 21, 16, 45, tzinfo=BJT),
                phase="decision",
                sporttery_fetcher=lambda target_date: [live_match()],
                sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
            )
            payload = json.loads(original.read_text(encoding="utf-8"))
            payload["matches"][0]["capture_phase"] = "decision"
            raw = live_odds._canonical_json_bytes(payload)
            captured = datetime.fromisoformat(payload["captured_at"])
            forged = original.with_name(
                live_odds._filename(captured, payload["source"], raw)
            )
            forged.write_bytes(raw)
            original.unlink()

            coverage = snapshot_coverage(
                root / "legacy",
                root / "data" / "live_odds_snapshots",
                DAY,
            )

        self.assertEqual(0, coverage["files"])
        self.assertEqual(0, coverage["matches"])
        self.assertEqual({}, coverage["requested_phases"])
        self.assertEqual({}, coverage["bindings_by_requested_phase"])

    def test_legacy_coverage_rejects_unproven_or_forged_contracts(self):
        cases = (
            "missing source",
            "injected source",
            "missing team",
            "bad filename",
            "bad embedded manifest hash",
            "missing manifest proof",
            "hash-mismatched manifest input",
            "invalid match phase",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = {
                    "date": DAY.isoformat(),
                    "team_a": "Home",
                    "team_b": "Away",
                    "match_id": "m1",
                    "kickoff_at": "2026-07-21T18:00:00+08:00",
                }
                write_import_contract(root, [fixture])
                path = write_legacy_snapshot(
                    root,
                    fixture,
                    datetime(2026, 7, 21, 13, 30, tzinfo=BJT),
                    "decision",
                )
                payload = json.loads(path.read_text(encoding="utf-8"))
                if case == "missing source":
                    payload.pop("source")
                elif case == "injected source":
                    payload["source"] = "injected"
                elif case == "missing team":
                    payload["matches"][0].pop("team_a")
                elif case == "bad filename":
                    renamed = path.with_name(
                        f"{DAY.isoformat()}-133001-decision.json"
                    )
                    path.rename(renamed)
                    path = renamed
                elif case == "bad embedded manifest hash":
                    payload["import_manifest"]["sha256"] = "0" * 64
                elif case == "missing manifest proof":
                    payload.pop("import_manifest")
                elif case == "hash-mismatched manifest input":
                    (
                        root
                        / "data"
                        / "import_extracts"
                        / DAY.isoformat()
                        / "odds.json"
                    ).write_text('{"changed": true}\n', encoding="utf-8")
                else:
                    payload["matches"][0]["capture_phase"] = "monitoring"
                if case not in {
                    "bad filename",
                    "hash-mismatched manifest input",
                }:
                    path.write_text(json.dumps(payload), encoding="utf-8")

                coverage = snapshot_coverage(
                    root / "data" / "odds_snapshots",
                    root / "data" / "live_odds_snapshots",
                    DAY,
                )

            self.assertEqual(0, coverage["files"])
            self.assertEqual(0, coverage["matches"])

    def test_latest_snapshot_is_ordered_chronologically_across_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = live_odds.capture_live_snapshot(
                root,
                DAY,
                datetime(2026, 7, 21, 13, 50, tzinfo=BJT),
                phase="decision",
                sporttery_fetcher=lambda target_date: [live_match()],
                sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
            )
            newer = live_odds.capture_live_snapshot(
                root,
                DAY,
                datetime(2026, 7, 21, 14, 0, tzinfo=BJT),
                phase="decision",
                sporttery_fetcher=lambda target_date: [live_match()],
                sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
            )
            payload = json.loads(newer.read_text(encoding="utf-8"))
            payload["captured_at"] = "2026-07-21T06:00:00+00:00"
            raw = live_odds._canonical_json_bytes(payload)
            captured = datetime.fromisoformat(payload["captured_at"])
            offset_path = newer.with_name(
                live_odds._filename(captured, payload["source"], raw)
            )
            offset_path.write_bytes(raw)
            newer.unlink()

            coverage = snapshot_coverage(
                root / "data" / "odds_snapshots",
                root / "data" / "live_odds_snapshots",
                DAY,
            )

        self.assertTrue(older.name)
        self.assertEqual(
            "2026-07-21T14:00:00+08:00",
            coverage["latest_by_requested_phase"]["decision"],
        )


if __name__ == "__main__":
    unittest.main()
