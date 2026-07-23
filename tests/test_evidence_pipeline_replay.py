import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import update_sporttery_results as results
from evidence_health import build_evidence_health
from live_odds import capture_live_snapshot, read_valid_live_snapshot
from result_evidence import proven_90_minute_result


BEIJING = timezone(timedelta(hours=8))
REPLAY_DATE = date(2026, 7, 21)


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

            health = build_evidence_health(
                root,
                REPLAY_DATE,
                datetime(2026, 7, 21, 14, 0, tzinfo=BEIJING),
                zero_fixture_verified=False,
            )
            self.assertEqual(1.0, health["identity_confirmation_rate"])
            self.assertEqual(1.0, health["result_provenance_rate"])
            self.assertEqual(
                {
                    "decision": 1,
                    "pre_kickoff_30": 1,
                    "pre_kickoff_90": 1,
                },
                health["snapshot_coverage"]["requested_phases"],
            )
            self.assertEqual(1, health["snapshot_coverage"]["phases"]["decision"])
            self.assertEqual(1, health["snapshot_coverage"]["phases"]["pre_kickoff_90"])
            self.assertEqual(1, health["snapshot_coverage"]["phases"]["pre_kickoff_30"])
            self.assertEqual([], health["hard_blockers"])
