import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import update_sporttery_results as results


class ResultProvenanceTest(unittest.TestCase):
    def captured(self, source_record_id, score, captured_at):
        return {
            "homeTeam": "甲队",
            "awayTeam": "乙队",
            "full": score,
            "half": None,
            "match_id": "1001",
            "result_status": "finished",
            "result_source": "zgzcw",
            "source_record_id": source_record_id,
            "captured_at_bjt": captured_at,
        }

    def test_direct_sporttery_rows_keep_match_id_and_finished_provenance(self):
        with patch.object(results, "fetch_matches", return_value=[{
            "matchId": "1001", "matchResultStatus": "2", "homeTeam": "甲队", "awayTeam": "乙队",
            "sectionsNo999": "2:1", "sectionsNo1": "1:0",
        }]):
            rows = results.official_result_rows(date(2026, 7, 16))

        self.assertEqual("1001", rows[0]["match_id"])
        self.assertEqual("finished", rows[0]["result_status"])
        self.assertEqual("sporttery", rows[0]["result_source"])
        self.assertTrue(rows[0]["source_record_id"])
        self.assertIn("+08:00", rows[0]["captured_at_bjt"])

    def test_zgzcw_parser_retains_the_source_row_id(self):
        parser = results.ZgzcwResultParser()
        parser.feed('<table><tr id="tr_678" class="endBet"><td class="wh-4"><a href="/soccer/team/a">甲队</a></td><td class="wh-5 bf">1:0</td><td class="wh-6"><a href="/soccer/team/b">乙队</a></td></tr></table>')

        self.assertEqual("678", parser.results[0]["source_record_id"])

    def test_fallback_resolves_only_proven_fixture_match_ids_and_preserves_legacy_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n2026-07-16,甲队,乙队,1001\n", encoding="utf-8")
            (data / "bet_results.csv").write_text("date,team_a,team_b,legacy\n2026-07-15,旧队,对手,keep\n", encoding="utf-8")
            fallback = [{"homeTeam": "甲队", "awayTeam": "乙队", "score": "3:2", "source_record_id": "678"}]
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")), patch.object(results, "fetch_zgzcw_results", return_value=fallback):
                path = results.update_results(date(2026, 7, 16))

            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            legacy, migrated = rows
            self.assertEqual("keep", legacy["legacy"])
            self.assertEqual("1001", migrated["match_id"])
            self.assertEqual("finished", migrated["result_status"])
            self.assertEqual("zgzcw", migrated["result_source"])
            self.assertEqual("678", migrated["source_record_id"])
            self.assertIn("+08:00", migrated["captured_at_bjt"])

    def test_unproven_fallback_id_is_unavailable_and_conflicting_score_never_overwrites_finished_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n", encoding="utf-8")
            (data / "bet_results.csv").write_text("date,team_a,team_b,home_goals,away_goals,match_id,result_status,result_source,source_record_id,captured_at_bjt\n2026-07-16,甲队,乙队,1,0,1001,finished,sporttery,old,2026-07-17T10:00:00+08:00\n", encoding="utf-8")
            fallback = [{"homeTeam": "甲队", "awayTeam": "乙队", "score": "2:0", "source_record_id": "678"}]
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")), patch.object(results, "fetch_zgzcw_results", return_value=fallback):
                path = results.update_results(date(2026, 7, 16))

            with path.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(("1", "0"), (row["home_goals"], row["away_goals"]))
            self.assertEqual("conflict", row["result_status"])
            self.assertIn("old", row["source_record_id"])
            self.assertIn("678", row["source_record_id"])

    def test_conflict_survives_repeated_and_later_captures_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n2026-07-16,甲队,乙队,1001\n",
                encoding="utf-8",
            )
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,result_source,source_record_id,captured_at_bjt\n"
                "2026-07-16,甲队,乙队,1,0,1001,finished,sporttery,old,2026-07-17T10:00:00+08:00\n",
                encoding="utf-8",
            )
            first_conflict = self.captured("678", ("2", "0"), "2026-07-17T11:00:00+08:00")
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", return_value=[first_conflict]):
                path = results.update_results(date(2026, 7, 16))
                first_bytes = path.read_bytes()
                results.update_results(date(2026, 7, 16))
                self.assertEqual(first_bytes, path.read_bytes())

                later_conflict = self.captured("789", ("3", "0"), "2026-07-17T12:00:00+08:00")
                with patch.object(results, "official_result_rows", return_value=[later_conflict]):
                    results.update_results(date(2026, 7, 16))

            with path.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(("1", "0"), (row["home_goals"], row["away_goals"]))
            self.assertEqual("conflict", row["result_status"])
            self.assertEqual("sporttery|zgzcw", row["result_source"])
            self.assertEqual("678|789|old", row["source_record_id"])
            self.assertEqual(
                "2026-07-17T10:00:00+08:00|2026-07-17T11:00:00+08:00|2026-07-17T12:00:00+08:00",
                row["captured_at_bjt"],
            )

    def test_duplicate_legacy_rows_and_unknown_columns_survive_migration_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n2026-07-16,甲队,乙队,1001\n",
                encoding="utf-8",
            )
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,legacy\n"
                "2026-07-16,甲队,乙队,,,first\n"
                "2026-07-16,甲队,乙队,,,second\n",
                encoding="utf-8",
            )
            captured = self.captured("678", ("2", "1"), "2026-07-17T11:00:00+08:00")
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", return_value=[captured]):
                path = results.update_results(date(2026, 7, 16))

            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(2, len(rows))
            self.assertEqual(["first", "second"], [row["legacy"] for row in rows])
            self.assertEqual(("2", "1"), (rows[0]["home_goals"], rows[0]["away_goals"]))
            self.assertEqual(("", ""), (rows[1]["home_goals"], rows[1]["away_goals"]))


if __name__ == "__main__":
    unittest.main()
