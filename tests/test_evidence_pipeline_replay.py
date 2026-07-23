import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import generate_betting_plan as betting_plan
import update_sporttery_results as results
from draw_model_learning import build_training_samples
from evidence_health import build_evidence_health
from live_odds import capture_live_snapshot, read_valid_live_snapshot
from result_evidence import proven_90_minute_result
import tests.test_report_status as report_status_fixture


BEIJING = timezone(timedelta(hours=8))
REPLAY_DATE = date(2026, 7, 21)
DRAW_FEATURES = {
    "base_draw_probability": 0.32,
    "market_draw_probability": 0.25,
    "favorite_probability": 0.54,
    "win_probability_gap": 0.42,
    "xg_total": 2.30,
    "favorite_movement": -0.05,
    "regional_gap": 0.06,
    "source_count": 2,
    "is_knockout": 0,
    "is_balanced": 1,
}


def write_import_manifest_fixture(
    root: Path,
    *,
    target_date: date,
    match_id: str,
    match_num: str,
    home: str,
    away: str,
    kickoff: str,
) -> None:
    extracts = root / "data" / "import_extracts" / target_date.isoformat()
    manifests = root / "data" / "import_manifests"
    extracts.mkdir(parents=True)
    manifests.mkdir(parents=True, exist_ok=True)
    fixture = extracts / "fixtures.csv"
    fixture.write_text(
        "date,team_a,team_b,match_id,match_num,kickoff_at\n"
        f"{target_date.isoformat()},{home},{away},{match_id},{match_num},{kickoff}\n",
        encoding="utf-8",
    )
    odds = extracts / "odds.json"
    odds.write_text("{}", encoding="utf-8")
    ratings = extracts / "ratings.csv"
    ratings.write_text(
        f"team,elo\n{home},1500\n{away},1500\n",
        encoding="utf-8",
    )
    records = {}
    for name, path in (("fixtures", fixture), ("odds", odds), ("ratings", ratings)):
        payload = path.read_bytes()
        records[name] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    manifest = {
        "schema_version": 2,
        "target_date": target_date.isoformat(),
        "source": "sporttery",
        "imported_at_bjt": f"{target_date.isoformat()}T12:05:00+08:00",
        **records,
    }
    (manifests / f"{target_date.isoformat()}.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


def capture_replay_snapshot(
    root: Path,
    *,
    phase: str,
    captured_at: datetime,
) -> Path:
    source_match = {
        "matchId": "2040580",
        "matchNumStr": "Tuesday001",
        "homeTeam": "Team A",
        "awayTeam": "Team B",
        "kickoff_at": "2026-07-21T18:00:00+08:00",
        "matchStatus": "Selling",
        "isSingleHad": True,
        "isSingleHhad": False,
        "isSingleTtg": False,
    }
    source_odds = {
        "had": {"h": "2.10", "d": "3.20", "a": "3.30"},
        "hhad": {},
        "ttg": {},
    }
    return capture_live_snapshot(
        root,
        REPLAY_DATE,
        captured_at,
        phase=phase,
        preferred_source="sporttery",
        sporttery_fetcher=lambda _target: [source_match],
        sporttery_odds_fetcher=lambda _source_id: source_odds,
    )


def write_draw_feature_snapshot(root: Path, row: dict) -> Path:
    target_date = date.fromisoformat(row["date"])
    captured_at = datetime.combine(
        target_date,
        datetime.min.time(),
        tzinfo=BEIJING,
    ).replace(hour=13)
    kickoff_at = datetime.fromisoformat(
        f"{target_date.isoformat()}T18:00:00+08:00"
    )
    payload = {
        "snapshot_schema_version": 1,
        "date": target_date.isoformat(),
        "match_id": row["match_id"],
        "team_a": row["team_a"],
        "team_b": row["team_b"],
        "stage": "replay",
        "captured_at": captured_at.isoformat(),
        "kickoff_at": kickoff_at.isoformat(),
        "domestic_draw_odds": 3.2,
        "features": DRAW_FEATURES,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    timestamp = captured_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / "data" / "draw_feature_snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{timestamp}-{digest}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def append_result_rows(path: Path, rows: list[dict]) -> None:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        existing = list(reader)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([*existing, *rows])


def make_ambiguous_report_identity(root: Path) -> None:
    helper = report_status_fixture.ReportStatusTest()
    helper.make_artifacts(root)
    fixture_paths = (
        root / "data" / "fixtures.csv",
        root
        / "data"
        / "import_extracts"
        / report_status_fixture.REPORT_DATE.isoformat()
        / "fixtures.csv",
    )
    for path in fixture_paths:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames
            rows = list(reader)
        for row in rows:
            if (
                row["date"] == report_status_fixture.REPORT_DATE.isoformat()
                and row["match_id"] == "002"
            ):
                row["team_a"] = "A-001"
                row["team_b"] = "B-001"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    predictions = (
        root
        / "output"
        / f"predictions_{report_status_fixture.REPORT_DATE.isoformat()}.csv"
    )
    with predictions.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)
    for row in rows:
        if row["match_id"] == "002":
            row["team_a"] = "A-001"
            row["team_b"] = "B-001"
    with predictions.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    manifest_path = (
        root
        / "data"
        / "import_manifests"
        / f"{report_status_fixture.REPORT_DATE.isoformat()}.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extract = fixture_paths[1]
    payload = extract.read_bytes()
    manifest["fixtures"].update(
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


class EvidencePipelineReplayTest(unittest.TestCase):
    def test_network_free_replay_is_strict_idempotent_and_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback_by_date = {}
            for offset in range(7):
                target = REPLAY_DATE - timedelta(days=offset)
                suffix = target.strftime("%d")
                match_id = "2040580" if target == REPLAY_DATE else f"20405{suffix}"
                home = "Team A" if target == REPLAY_DATE else f"Home {suffix}"
                away = "Team B" if target == REPLAY_DATE else f"Away {suffix}"
                kickoff = f"{target.isoformat()}T18:00:00+08:00"
                write_import_manifest_fixture(
                    root,
                    target_date=target,
                    match_id=match_id,
                    match_num=f"Match{suffix}",
                    home=home,
                    away=away,
                    kickoff=kickoff,
                )
                fallback_by_date[target] = [{
                    "homeTeam": home,
                    "awayTeam": away,
                    "score": "1:1",
                    "source_record_id": "" if offset == 6 else f"source-{suffix}",
                    "captured_at_bjt": (
                        f"{target.isoformat()}T20:00:00+08:00"
                    ),
                }]

            data = root / "data"
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id,kickoff_at\n"
                "2026-07-22,New A,New B,9999,2026-07-22T18:00:00+08:00\n",
                encoding="utf-8",
            )

            with (
                patch.object(results, "ROOT", root),
                patch.object(results, "DATA_DIR", data),
                patch.object(
                    results,
                    "official_result_rows",
                    side_effect=RuntimeError("offline"),
                ),
                patch.object(
                    results,
                    "fetch_zgzcw_results",
                    side_effect=lambda target: fallback_by_date[target],
                ),
                patch.object(
                    results,
                    "_fallback_result_row",
                    side_effect=lambda item: {
                        **item,
                        "full": results.parse_score(item["score"]),
                        "half": None,
                        "match_id": "",
                        "result_source": "zgzcw",
                        "source_record_id": item["source_record_id"],
                        "captured_at_bjt": item["captured_at_bjt"],
                        "score_scope": "regular_time_90",
                        "settlement_minutes": "90",
                    },
                ),
            ):
                self.assertEqual(
                    0,
                    results.main([
                        "--date",
                        REPLAY_DATE.isoformat(),
                        "--reconcile-days",
                        "7",
                    ]),
                )
                result_path = data / "bet_results.csv"
                first_bytes = result_path.read_bytes()
                self.assertEqual(
                    0,
                    results.main([
                        "--date",
                        REPLAY_DATE.isoformat(),
                        "--reconcile-days",
                        "7",
                    ]),
                )
                self.assertEqual(first_bytes, result_path.read_bytes())

            with result_path.open(encoding="utf-8-sig", newline="") as handle:
                result_rows = {
                    row["date"]: row
                    for row in csv.DictReader(handle)
                }
            replay_row = result_rows[REPLAY_DATE.isoformat()]
            self.assertEqual("2040580", replay_row["match_id"])
            self.assertTrue(proven_90_minute_result(replay_row))
            anonymous_date = (REPLAY_DATE - timedelta(days=6)).isoformat()
            self.assertEqual("unavailable", result_rows[anonymous_date]["result_status"])
            self.assertFalse(proven_90_minute_result(result_rows[anonymous_date]))

            conflict_row = result_rows[
                (REPLAY_DATE - timedelta(days=1)).isoformat()
            ]
            ambiguous_row = result_rows[
                (REPLAY_DATE - timedelta(days=2)).isoformat()
            ]
            anonymous_row = result_rows[anonymous_date]
            for row in (conflict_row, ambiguous_row, anonymous_row):
                write_draw_feature_snapshot(root, row)
            append_result_rows(
                result_path,
                [
                    {**conflict_row, "home_goals": "2"},
                    {
                        **ambiguous_row,
                        "source_record_id": (
                            ambiguous_row["source_record_id"] + "-other"
                        ),
                    },
                ],
            )

            with patch.object(betting_plan, "DATA_DIR", data):
                settlement_ingress = betting_plan.load_results()
            self.assertNotIn(conflict_row["match_id"], settlement_ingress)
            self.assertNotIn(ambiguous_row["match_id"], settlement_ingress)
            self.assertEqual(
                [],
                build_training_samples(root, as_of=REPLAY_DATE),
            )

            write_draw_feature_snapshot(root, replay_row)
            samples = build_training_samples(root, as_of=REPLAY_DATE)
            self.assertEqual(1, len(samples))
            self.assertEqual(replay_row["match_id"], samples[0]["match_id"])
            self.assertNotIn(
                conflict_row["match_id"],
                {sample["match_id"] for sample in samples},
            )
            self.assertNotIn(
                ambiguous_row["match_id"],
                {sample["match_id"] for sample in samples},
            )
            self.assertNotIn(
                anonymous_row["match_id"],
                {sample["match_id"] for sample in samples},
            )

            snapshots = (
                capture_replay_snapshot(
                    root,
                    phase="decision",
                    captured_at=datetime(2026, 7, 21, 13, 45, tzinfo=BEIJING),
                ),
                capture_replay_snapshot(
                    root,
                    phase="pre_kickoff_90",
                    captured_at=datetime(2026, 7, 21, 16, 30, tzinfo=BEIJING),
                ),
                capture_replay_snapshot(
                    root,
                    phase="pre_kickoff_30",
                    captured_at=datetime(2026, 7, 21, 17, 30, tzinfo=BEIJING),
                ),
            )
            for path in snapshots:
                payload = read_valid_live_snapshot(root, path, REPLAY_DATE)
                self.assertEqual(2, payload["schema_version"])
                self.assertEqual("live", payload["fetch_mode"])

            decision_health = build_evidence_health(
                root,
                REPLAY_DATE,
                datetime(2026, 7, 21, 14, 0, tzinfo=BEIJING),
                zero_fixture_verified=False,
            )
            self.assertEqual(1.0, decision_health["identity_confirmation_rate"])
            self.assertEqual(1.0, decision_health["result_provenance_rate"])
            self.assertEqual(
                {"decision": 1},
                decision_health["snapshot_coverage"]["requested_phases"],
            )
            self.assertEqual(
                1,
                decision_health["snapshot_coverage"]["phases"]["decision"],
            )
            self.assertEqual(
                0,
                decision_health["snapshot_coverage"]["phases"]["pre_kickoff_90"],
            )
            self.assertEqual(
                0,
                decision_health["snapshot_coverage"]["phases"]["pre_kickoff_30"],
            )
            self.assertEqual([], decision_health["hard_blockers"])

            post_capture_health = build_evidence_health(
                root,
                REPLAY_DATE,
                datetime(2026, 7, 21, 17, 31, tzinfo=BEIJING),
                zero_fixture_verified=False,
            )
            self.assertEqual(
                {
                    "decision": 1,
                    "pre_kickoff_30": 1,
                    "pre_kickoff_90": 1,
                },
                post_capture_health["snapshot_coverage"]["requested_phases"],
            )

    def test_fallback_corroborated_by_sporttery_remains_settleable_and_trainable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            write_import_manifest_fixture(
                root,
                target_date=REPLAY_DATE,
                match_id="2040580",
                match_num="Tuesday001",
                home="Team A",
                away="Team B",
                kickoff="2026-07-21T18:00:00+08:00",
            )
            fallback_source = [{
                "homeTeam": "Team A",
                "awayTeam": "Team B",
                "score": "1:1",
                "source_record_id": "zg-88",
            }]
            fallback_observation = {
                "homeTeam": "Team A",
                "awayTeam": "Team B",
                "full": ("1", "1"),
                "half": None,
                "match_id": "",
                "result_status": "finished",
                "result_source": "zgzcw",
                "source_record_id": "zg-88",
                "captured_at_bjt": "2026-07-21T20:00:00+08:00",
                "score_scope": "regular_time_90",
                "settlement_minutes": "90",
            }
            official_observation = {
                **fallback_observation,
                "match_id": "2040580",
                "result_source": "sporttery",
                "source_record_id": "2040580",
                "captured_at_bjt": "2026-07-21T20:05:00+08:00",
            }
            with (
                patch.object(results, "ROOT", root),
                patch.object(results, "DATA_DIR", data),
                patch.object(
                    results,
                    "official_result_rows",
                    side_effect=RuntimeError("official pending"),
                ),
                patch.object(
                    results,
                    "fetch_zgzcw_results",
                    return_value=fallback_source,
                ),
                patch.object(
                    results,
                    "_fallback_result_row",
                    return_value=fallback_observation,
                ),
            ):
                result_path = results.update_results(REPLAY_DATE)
            with (
                patch.object(results, "ROOT", root),
                patch.object(results, "DATA_DIR", data),
                patch.object(
                    results,
                    "official_result_rows",
                    return_value=[official_observation],
                ),
            ):
                results.update_results(REPLAY_DATE)

            with result_path.open(
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                row = next(csv.DictReader(handle))
            observations = json.loads(row["result_observations_json"])
            self.assertEqual("sporttery", row["result_source"])
            self.assertEqual("2040580", row["source_record_id"])
            self.assertNotIn("|", row["captured_at_bjt"])
            self.assertEqual(
                {"sporttery", "zgzcw"},
                {
                    item["result_source"]
                    for item in observations["observations"]
                },
            )
            self.assertTrue(proven_90_minute_result(row))

            with patch.object(betting_plan, "DATA_DIR", data):
                settlement_ingress = betting_plan.load_results()
            self.assertIn("2040580", settlement_ingress)

            write_draw_feature_snapshot(root, row)
            samples = build_training_samples(root, as_of=REPLAY_DATE)
            self.assertEqual(["2040580"], [
                sample["match_id"] for sample in samples
            ])

    def test_real_hard_blocker_forces_actual_report_readiness_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_ambiguous_report_identity(root)
            helper = report_status_fixture.ReportStatusTest()

            status = helper.publish(root, "forecast")

        self.assertIn(
            "identity_not_unique",
            status["evidence_health"]["hard_blockers"],
        )
        for key in (
            "source_ready",
            "fixtures_ready",
            "import_manifest_ready",
            "odds_ready",
            "official_odds_complete",
            "predictions_ready",
            "site_ready",
            "image_ready",
        ):
            self.assertTrue(status["data_quality"][key], key)
        self.assertFalse(status["forecast_ready"])
