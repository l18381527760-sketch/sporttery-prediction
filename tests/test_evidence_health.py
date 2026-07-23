import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import live_odds
from evidence_health import build_evidence_health


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 21)
NOW = datetime(2026, 7, 21, 14, 0, tzinfo=BJT)


def coverage(
    *,
    decision_bindings=None,
    decision_count=2,
    decision_at="2026-07-21T13:45:00+08:00",
):
    if decision_bindings is None:
        decision_bindings = (
            [DAY.isoformat(), "Home 1", "Away 1", "1"],
            [DAY.isoformat(), "Home 2", "Away 2", "2"],
        )
    return {
        "files": 1,
        "matches": len(decision_bindings),
        "phases": {
            "decision": decision_count,
            "pre_kickoff_90": 0,
            "pre_kickoff_30": 0,
        },
        "requested_phases": {"decision": decision_count},
        "latest": decision_at,
        "latest_by_phase": {"decision": decision_at},
        "latest_by_requested_phase": {"decision": decision_at},
        "bindings_by_requested_phase": {
            "decision": list(decision_bindings),
        },
    }


class EvidenceHealthTest(unittest.TestCase):
    def test_health_blocks_non_unique_identity_and_reports_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "bet_results.csv").write_text(
                "date,match_id,home_goals,away_goals,result_status,result_source,"
                "source_record_id,captured_at_bjt,score_scope,settlement_minutes\n"
                "2026-07-21,1,1,1,finished,sporttery,1,"
                "2026-07-22T12:00:00+08:00,regular_time_90,90\n",
                encoding="utf-8",
            )
            with (
                patch("evidence_health.fixture_match_ids", return_value={
                    (DAY.isoformat(), "Home 1", "Away 1"): frozenset({"1"}),
                    (DAY.isoformat(), "Home 2", "Away 2"): frozenset({"2", "3"}),
                }),
                patch("evidence_health.snapshot_coverage", return_value=coverage()),
            ):
                health = build_evidence_health(
                    root,
                    DAY,
                    NOW,
                    zero_fixture_verified=False,
                )

        self.assertEqual(0.5, health["identity_confirmation_rate"])
        self.assertIn("identity_not_unique", health["hard_blockers"])
        self.assertEqual(1.0, health["result_provenance_rate"])

    def test_conflicting_result_rows_do_not_count_as_proven(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "bet_results.csv").write_text(
                "date,match_id,home_goals,away_goals,result_status,result_source,"
                "source_record_id,captured_at_bjt,score_scope,settlement_minutes\n"
                "2026-07-21,1,1,1,finished,sporttery,1,"
                "2026-07-22T12:00:00+08:00,regular_time_90,90\n"
                "2026-07-21,1,2,1,finished,sporttery,1,"
                "2026-07-22T12:00:00+08:00,regular_time_90,90\n"
                "2026-07-21,2,0,0,finished,zgzcw,2,"
                "2026-07-22T12:05:00+08:00,regular_time_90,90\n",
                encoding="utf-8",
            )
            with (
                patch("evidence_health.fixture_match_ids", return_value={
                    (DAY.isoformat(), "Home 1", "Away 1"): frozenset({"1"}),
                    (DAY.isoformat(), "Home 2", "Away 2"): frozenset({"2"}),
                }),
                patch("evidence_health.snapshot_coverage", return_value=coverage()),
            ):
                health = build_evidence_health(
                    root,
                    DAY,
                    NOW,
                    zero_fixture_verified=False,
                )

        self.assertEqual(0.5, health["result_provenance_rate"])

    def test_zero_fixture_day_requires_verified_zero_fixture_evidence(self):
        empty_coverage = coverage(
            decision_bindings=(),
            decision_count=0,
            decision_at=None,
        )
        empty_coverage["requested_phases"] = {}
        empty_coverage["latest_by_requested_phase"] = {}
        empty_coverage["latest_by_phase"] = {}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("evidence_health.fixture_match_ids", return_value={}),
            patch(
                "evidence_health.snapshot_coverage",
                return_value=empty_coverage,
            ),
        ):
            root = Path(tmp)
            unverified = build_evidence_health(
                root,
                DAY,
                NOW,
                zero_fixture_verified=False,
            )
            verified = build_evidence_health(
                root,
                DAY,
                NOW,
                zero_fixture_verified=True,
            )

        self.assertEqual(0.0, unverified["identity_confirmation_rate"])
        self.assertEqual(["identity_not_unique"], unverified["forecast_blockers"])
        self.assertEqual(1.0, verified["identity_confirmation_rate"])
        self.assertEqual([], verified["hard_blockers"])

    def test_relabelled_fixture_ids_do_not_satisfy_decision_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n"
                "2026-07-21,Home A,Away A,A\n"
                "2026-07-21,Home B,Away B,B\n",
                encoding="utf-8",
            )
            live_odds.capture_live_snapshot(
                root,
                DAY,
                datetime(2026, 7, 21, 13, 45, tzinfo=BJT),
                phase="decision",
                sporttery_fetcher=lambda target_date: [
                    {
                        "matchId": "B",
                        "matchNumStr": "Monday001",
                        "homeTeam": "Home A",
                        "awayTeam": "Away A",
                        "matchStatus": "Selling",
                        "kickoff_at": "2026-07-21T18:00:00+08:00",
                        "isSingleHad": True,
                        "isSingleHhad": False,
                        "isSingleTtg": False,
                    },
                    {
                        "matchId": "A",
                        "matchNumStr": "Monday002",
                        "homeTeam": "Home B",
                        "awayTeam": "Away B",
                        "matchStatus": "Selling",
                        "kickoff_at": "2026-07-21T18:00:00+08:00",
                        "isSingleHad": True,
                        "isSingleHhad": False,
                        "isSingleTtg": False,
                    },
                ],
                sporttery_odds_fetcher=lambda match_id: {
                    "had": {"h": "2.80", "d": "3.10", "a": "2.25"},
                    "hhad": {},
                    "ttg": {},
                },
            )
            health = build_evidence_health(
                root,
                DAY,
                NOW,
                zero_fixture_verified=False,
            )

        self.assertEqual(1.0, health["identity_confirmation_rate"])
        self.assertEqual(
            [
                [DAY.isoformat(), "Home A", "Away A", "B"],
                [DAY.isoformat(), "Home B", "Away B", "A"],
            ],
            health["snapshot_coverage"][
                "bindings_by_requested_phase"
            ]["decision"],
        )
        self.assertIn(
            "decision_snapshot_incomplete",
            health["decision_blockers"],
        )

    def test_future_decision_evidence_is_blocked_without_being_stale(self):
        future = coverage(
            decision_bindings=(
                [DAY.isoformat(), "Home 1", "Away 1", "1"],
            ),
            decision_count=1,
            decision_at="2026-07-21T14:00:01+08:00",
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("evidence_health.fixture_match_ids", return_value={
                (DAY.isoformat(), "Home 1", "Away 1"): frozenset({"1"}),
            }),
            patch("evidence_health.snapshot_coverage", return_value=future),
        ):
            health = build_evidence_health(
                Path(tmp),
                DAY,
                NOW,
                zero_fixture_verified=False,
            )

        self.assertIn("decision_odds_from_future", health["decision_blockers"])
        self.assertNotIn("decision_odds_stale", health["decision_blockers"])

    def test_decision_evidence_exactly_thirty_minutes_old_is_accepted(self):
        boundary = coverage(
            decision_bindings=(
                [DAY.isoformat(), "Home 1", "Away 1", "1"],
            ),
            decision_count=1,
            decision_at="2026-07-21T13:30:00+08:00",
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("evidence_health.fixture_match_ids", return_value={
                (DAY.isoformat(), "Home 1", "Away 1"): frozenset({"1"}),
            }),
            patch("evidence_health.snapshot_coverage", return_value=boundary),
        ):
            health = build_evidence_health(
                Path(tmp),
                DAY,
                NOW,
                zero_fixture_verified=False,
            )

        self.assertEqual([], health["decision_blockers"])

    def test_decision_evidence_older_than_thirty_minutes_is_stale(self):
        stale = coverage(
            decision_bindings=(
                [DAY.isoformat(), "Home 1", "Away 1", "1"],
            ),
            decision_count=1,
            decision_at="2026-07-21T13:29:59+08:00",
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("evidence_health.fixture_match_ids", return_value={
                (DAY.isoformat(), "Home 1", "Away 1"): frozenset({"1"}),
            }),
            patch("evidence_health.snapshot_coverage", return_value=stale),
        ):
            health = build_evidence_health(
                Path(tmp),
                DAY,
                NOW,
                zero_fixture_verified=False,
            )

        self.assertEqual(
            ["decision_odds_stale"],
            health["decision_blockers"],
        )

    def test_now_must_include_a_timezone(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            build_evidence_health(
                Path("."),
                DAY,
                datetime(2026, 7, 21, 14, 0),
                zero_fixture_verified=False,
            )


if __name__ == "__main__":
    unittest.main()
