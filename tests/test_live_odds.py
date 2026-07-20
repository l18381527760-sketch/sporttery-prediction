import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import live_odds


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 19)
NOW = datetime(2026, 7, 19, 20, 0, tzinfo=BJT)


def sporttery_match(**overrides):
    match = {
        "matchId": "m1",
        "matchNumStr": "Sunday001",
        "homeTeam": "Home",
        "awayTeam": "Away",
        "matchStatus": "Selling",
        "kickoff_at": "2026-07-19T22:00:00+08:00",
        "isSingleHad": True,
        "isSingleHhad": True,
        "isSingleTtg": True,
    }
    match.update(overrides)
    return match


def had_odds(**overrides):
    odds = {"had": {"h": "2.80", "d": "3.10", "a": "2.25"}, "hhad": {}, "ttg": {}}
    odds.update(overrides)
    return odds


class LiveOddsTest(TestCase):
    def test_capture_calls_live_sporttery_endpoints_and_never_manifest_odds(self):
        matches = [sporttery_match()]
        odds = had_odds()
        with TemporaryDirectory() as tmp, patch(
            "live_odds.read_valid_import_manifest", side_effect=AssertionError("manifest odds read")
        ):
            path = live_odds.capture_live_snapshot(
                Path(tmp), DAY, NOW,
                sporttery_fetcher=lambda day: matches,
                sporttery_odds_fetcher=lambda match_id: odds,
            )
            payload = live_odds.read_valid_live_snapshot(Path(tmp), path, DAY, NOW)
        self.assertEqual("live", payload["fetch_mode"])
        self.assertEqual("sporttery", payload["source"])
        self.assertEqual("2.80", payload["matches"][0]["markets"]["had"]["h"])

    def test_zgzcw_fallback_maps_exact_fixture_identity_and_market(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_manifest_fixture(root)
            source_match = sporttery_match(
                matchId="zg-9",
                matchStatus="ZGZCW",
            )
            manifest = {"fixtures": {"path": "data/import_extracts/2026-07-19/fixtures.csv"}}
            with patch("live_odds.read_valid_import_manifest", return_value=manifest):
                path = live_odds.capture_live_snapshot(
                    root,
                    DAY,
                    NOW,
                    sporttery_fetcher=lambda day: (_ for _ in ()).throw(RuntimeError("offline")),
                    zgzcw_match_fetcher=lambda day: [source_match],
                    zgzcw_odds_fetcher=lambda day: {"zg-9": had_odds()},
                )
            payload = live_odds.read_valid_live_snapshot(root, path, DAY, NOW)

        match = payload["matches"][0]
        self.assertEqual("fixture-1", match["match_id"])
        self.assertEqual("zg-9", match["source_record_id"])
        self.assertEqual("Sunday001", match["match_num"])
        self.assertEqual(("Home", "Away"), (match["team_a"], match["team_b"]))
        self.assertEqual("2026-07-19T22:00:00+08:00", match["kickoff_at"])
        self.assertEqual(had_odds()["had"], match["markets"]["had"])

    def test_zgzcw_fallback_rejects_any_fixture_identity_mismatch(self):
        variants = (
            {"matchNumStr": "Sunday002"},
            {"homeTeam": "Other Home"},
            {"awayTeam": "Other Away"},
            {"kickoff_at": "2026-07-19T22:01:00+08:00"},
        )
        for overrides in variants:
            with self.subTest(overrides=overrides), TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._write_manifest_fixture(root)
                manifest = {"fixtures": {"path": "data/import_extracts/2026-07-19/fixtures.csv"}}
                with patch("live_odds.read_valid_import_manifest", return_value=manifest):
                    with self.assertRaisesRegex(ValueError, "fallback fixture identity"):
                        live_odds.capture_live_snapshot(
                            root,
                            DAY,
                            NOW,
                            preferred_source="zgzcw",
                            zgzcw_match_fetcher=lambda day, item=overrides: [sporttery_match(matchId="zg-9", matchStatus="ZGZCW", **item)],
                            zgzcw_odds_fetcher=lambda day: {"zg-9": had_odds()},
                        )

    def test_capture_filters_already_started_matches(self):
        with TemporaryDirectory() as tmp:
            path = live_odds.capture_live_snapshot(
                Path(tmp), DAY, NOW,
                sporttery_fetcher=lambda day: [sporttery_match(kickoff_at="2026-07-19T20:00:00+08:00")],
                sporttery_odds_fetcher=lambda match_id: had_odds(),
            )
            payload = live_odds.read_valid_live_snapshot(Path(tmp), path, DAY, NOW)
        self.assertEqual([], payload["matches"])

    def test_capture_rejects_missing_or_naive_kickoff(self):
        for kickoff in ("", "2026-07-19T22:00:00"):
            with self.subTest(kickoff=kickoff), TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "kickoff"):
                    live_odds.capture_live_snapshot(
                        Path(tmp), DAY, NOW,
                        sporttery_fetcher=lambda day, value=kickoff: [sporttery_match(kickoff_at=value)],
                        sporttery_odds_fetcher=lambda match_id: had_odds(),
                    )

    def test_capture_rejects_source_response_without_supported_market(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "supported market"):
                live_odds.capture_live_snapshot(
                    Path(tmp), DAY, NOW,
                    sporttery_fetcher=lambda day: [sporttery_match()],
                    sporttery_odds_fetcher=lambda match_id: had_odds(had={}, hhad={}, ttg={}),
                )

    def test_conflicting_existing_append_only_snapshot_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = live_odds.capture_live_snapshot(
                root, DAY, NOW,
                sporttery_fetcher=lambda day: [sporttery_match()],
                sporttery_odds_fetcher=lambda match_id: had_odds(),
            )
            path.write_bytes(b"conflicting payload")
            with self.assertRaisesRegex(ValueError, "conflicting live snapshot"):
                live_odds.capture_live_snapshot(
                    root, DAY, NOW,
                    sporttery_fetcher=lambda day: [sporttery_match()],
                    sporttery_odds_fetcher=lambda match_id: had_odds(),
                )

    def _write_manifest_fixture(self, root: Path) -> None:
        path = root / "data" / "import_extracts" / DAY.isoformat() / "fixtures.csv"
        path.parent.mkdir(parents=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("date", "match_id", "match_num", "team_a", "team_b", "kickoff_at"))
            writer.writeheader()
            writer.writerow({
                "date": DAY.isoformat(),
                "match_id": "fixture-1",
                "match_num": "Sunday001",
                "team_a": "Home",
                "team_b": "Away",
                "kickoff_at": "2026-07-19T22:00:00+08:00",
            })
