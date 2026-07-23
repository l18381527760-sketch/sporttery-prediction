import unittest

from result_evidence import (
    normalized_result,
    proven_90_minute_result,
    proven_result_provenance,
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
