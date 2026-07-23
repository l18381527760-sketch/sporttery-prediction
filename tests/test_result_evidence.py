import unittest

from result_evidence import (
    normalized_result,
    proven_90_minute_result,
    proven_result_provenance,
    resolve_result_batch,
)


class ResultEvidenceTest(unittest.TestCase):
    def base(self):
        return {
            "match_id": "2040580",
            "home_goals": "1",
            "away_goals": "1",
            "result_status": "finished",
            "result_source": "zgzcw",
            "source_record_id": "88",
            "captured_at_bjt": "2026-07-22T12:30:00+08:00",
            "score_scope": "regular_time_90",
            "settlement_minutes": "90",
        }

    def test_accepts_complete_regular_time_result(self):
        row = self.base()
        self.assertTrue(proven_result_provenance(row))
        self.assertTrue(proven_90_minute_result(row))
        self.assertEqual(1, normalized_result(row)["home_goals"])

    def test_rejects_every_missing_or_ambiguous_proof(self):
        for field, value in (
            ("match_id", ""),
            ("result_status", "unavailable"),
            ("result_source", ""),
            ("source_record_id", ""),
            ("captured_at_bjt", ""),
            ("score_scope", "including_extra_time"),
            ("settlement_minutes", "120"),
            ("home_goals", "x"),
        ):
            with self.subTest(field=field):
                row = self.base()
                row[field] = value
                self.assertFalse(proven_90_minute_result(row))
                self.assertIsNone(normalized_result(row))

    def test_rejects_unapproved_result_source(self):
        row = self.base()
        row["result_source"] = "unknown"
        self.assertFalse(proven_result_provenance(row))
        self.assertFalse(proven_90_minute_result(row))

    def test_requires_exact_beijing_offset(self):
        for captured_at in (
            "2026-07-22T04:30:00+00:00",
            "2026-07-22T13:30:00+09:00",
            "2026-07-22T12:30:00+07:59",
        ):
            with self.subTest(captured_at=captured_at):
                row = self.base()
                row["captured_at_bjt"] = captured_at
                self.assertFalse(proven_result_provenance(row))
                self.assertFalse(proven_90_minute_result(row))
                self.assertIsNone(normalized_result(row))

    def test_batch_collapses_exact_duplicates_and_removes_conflicting_ids(self):
        exact = self.base()
        conflict = {**self.base(), "match_id": "conflict"}
        refund = {
            "match_id": "refund",
            "result_status": "refunded",
            "result_source": "sporttery",
            "source_record_id": "refund-record",
            "captured_at_bjt": "2026-07-22T12:30:00+08:00",
        }
        invalid = {
            "match_id": "invalid",
            "result_status": "invalid",
            "result_source": "zgzcw",
            "source_record_id": "invalid-record",
            "captured_at_bjt": "2026-07-22T12:30:00+08:00",
        }

        resolved = resolve_result_batch(
            [
                exact,
                dict(exact),
                conflict,
                {**conflict, "away_goals": "0"},
                dict(conflict),
                refund,
                invalid,
            ]
        )

        self.assertEqual(
            {"2040580", "refund", "invalid"},
            set(resolved),
        )
        self.assertEqual(exact, resolved["2040580"])
        self.assertEqual("refunded", resolved["refund"]["result_status"])
        self.assertEqual("invalid", resolved["invalid"]["result_status"])

    def test_malformed_rows_and_negative_goals_fail_closed(self):
        for row in (None, [], {"result_source": "sporttery"}):
            with self.subTest(row=row):
                self.assertFalse(proven_result_provenance(row))
                self.assertFalse(proven_90_minute_result(row))
                self.assertIsNone(normalized_result(row))

        row = self.base()
        row["away_goals"] = "-1"
        self.assertFalse(proven_90_minute_result(row))
        self.assertIsNone(normalized_result(row))


if __name__ == "__main__":
    unittest.main()
