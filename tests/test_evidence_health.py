import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from evidence_health import build_evidence_health


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 21)
NOW = datetime(2026, 7, 21, 14, 0, tzinfo=BJT)


def coverage(
    *,
    decision_ids=("1", "2"),
    decision_count=2,
    decision_at="2026-07-21T13:45:00+08:00",
):
    return {
        "files": 1,
        "matches": len(decision_ids),
        "phases": {
            "decision": decision_count,
            "pre_kickoff_90": 0,
            "pre_kickoff_30": 0,
        },
        "requested_phases": {"decision": decision_count},
        "latest": decision_at,
        "latest_by_phase": {"decision": decision_at},
        "latest_by_requested_phase": {"decision": decision_at},
        "match_ids_by_requested_phase": {
            "decision": list(decision_ids),
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
                patch("evidence_health.fixture_identity_rate", return_value=(1, 2)),
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
                patch("evidence_health.fixture_identity_rate", return_value=(2, 2)),
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
            decision_ids=(),
            decision_count=0,
            decision_at=None,
        )
        empty_coverage["requested_phases"] = {}
        empty_coverage["latest_by_requested_phase"] = {}
        empty_coverage["latest_by_phase"] = {}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("evidence_health.fixture_identity_rate", return_value=(0, 0)),
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
