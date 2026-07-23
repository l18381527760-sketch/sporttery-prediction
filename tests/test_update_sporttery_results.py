import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import call, patch

import update_sporttery_results as results
import import_sporttery


class ResultProvenanceTest(unittest.TestCase):
    def captured(
        self,
        source_record_id,
        score,
        captured_at,
        *,
        match_id="1001",
        result_source="zgzcw",
    ):
        return {
            "homeTeam": "甲队",
            "awayTeam": "乙队",
            "full": score,
            "half": None,
            "match_id": match_id,
            "result_status": "finished",
            "result_source": result_source,
            "source_record_id": source_record_id,
            "captured_at_bjt": captured_at,
            "score_scope": "regular_time_90",
            "settlement_minutes": "90",
        }

    def read_rows(self, path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

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
        self.assertEqual("regular_time_90", rows[0]["score_scope"])
        self.assertEqual("90", rows[0]["settlement_minutes"])

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
            with patch.object(results, "ROOT", root), patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")), patch.object(results, "fetch_zgzcw_results", return_value=fallback):
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
            self.assertEqual("regular_time_90", migrated["score_scope"])
            self.assertEqual("90", migrated["settlement_minutes"])

    def test_fallback_uses_historical_manifest_after_current_fixture_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            fixtures = data / "fixtures.csv"
            fixtures.write_text(
                "date,team_a,team_b,match_id\n2026-07-21,Team A,Team B,2040580\n",
                encoding="utf-8",
            )
            odds = data / "odds.json"
            odds.write_text("{}\n", encoding="utf-8")
            ratings = data / "ratings.csv"
            ratings.write_text("team,elo\nTeam A,1500\nTeam B,1500\n", encoding="utf-8")
            with patch.object(import_sporttery, "DATA_DIR", data):
                import_sporttery.write_import_manifest(
                    "sporttery", date(2026, 7, 21), fixtures, odds, ratings
                )
            fixtures.write_text(
                "date,team_a,team_b,match_id\n2026-07-22,New A,New B,9999\n",
                encoding="utf-8",
            )
            fallback = [{
                "homeTeam": "Team A", "awayTeam": "Team B",
                "score": "1:1", "source_record_id": "tr-88",
            }]
            with (
                patch.object(results, "ROOT", root),
                patch.object(results, "DATA_DIR", data),
                patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")),
                patch.object(results, "fetch_zgzcw_results", return_value=fallback),
            ):
                path = results.update_results(date(2026, 7, 21))

            row = self.read_rows(path)[0]
            self.assertEqual("2040580", row["match_id"])
            self.assertEqual("finished", row["result_status"])
            self.assertEqual("regular_time_90", row["score_scope"])
            self.assertEqual("tr-88", row["source_record_id"])

    def test_direct_result_does_not_require_fallback_fixture_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            direct = self.captured(
                "2040580", ("1", "1"), "2026-07-22T10:00:00+08:00",
                match_id="2040580", result_source="sporttery",
            )
            with (
                patch.object(results, "ROOT", root),
                patch.object(results, "DATA_DIR", data),
                patch.object(results, "official_result_rows", return_value=[direct]),
            ):
                path = results.update_results(date(2026, 7, 21))

            row = self.read_rows(path)[0]
            self.assertEqual("2040580", row["match_id"])
            self.assertEqual("finished", row["result_status"])
            self.assertEqual(("1", "1"), (row["home_goals"], row["away_goals"]))

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
            self.assertEqual(3, len(rows))
            self.assertEqual(["first", "second", ""], [row["legacy"] for row in rows])
            self.assertEqual(["", "", "2"], [row["home_goals"] for row in rows])
            self.assertEqual("1001", rows[2]["match_id"])

    def test_direct_mismatched_match_id_appends_without_touching_identified_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n", encoding="utf-8")
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,legacy\n"
                "2026-07-16,甲队,乙队,1,0,2002,finished,keep\n",
                encoding="utf-8",
            )
            direct = self.captured(
                "1001", ("2", "1"), "2026-07-17T11:00:00+08:00",
                result_source="sporttery",
            )
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", return_value=[direct]):
                path = results.update_results(date(2026, 7, 16))

            rows = self.read_rows(path)
            self.assertEqual(["2002", "1001"], [row["match_id"] for row in rows])
            self.assertEqual(("1", "0", "keep"), (rows[0]["home_goals"], rows[0]["away_goals"], rows[0]["legacy"]))
            self.assertEqual(("2", "1", "finished"), (rows[1]["home_goals"], rows[1]["away_goals"], rows[1]["result_status"]))

    def test_direct_exact_id_updates_first_duplicate_and_preserves_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n", encoding="utf-8")
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,legacy\n"
                "2026-07-16,甲队,乙队,,,1001,first\n"
                "2026-07-16,甲队,乙队,9,9,1001,second\n",
                encoding="utf-8",
            )
            direct = self.captured(
                "1001", ("2", "1"), "2026-07-17T11:00:00+08:00",
                result_source="sporttery",
            )
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", return_value=[direct]):
                path = results.update_results(date(2026, 7, 16))

            rows = self.read_rows(path)
            self.assertEqual(["first", "second"], [row["legacy"] for row in rows])
            self.assertEqual(("2", "1"), (rows[0]["home_goals"], rows[0]["away_goals"]))
            self.assertEqual(("9", "9"), (rows[1]["home_goals"], rows[1]["away_goals"]))

    def test_fallback_duplicate_existing_and_fixture_ids_form_one_unique_union(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n"
                "2026-07-16,甲队,乙队,1001\n"
                "2026-07-16,甲队,乙队,1001\n",
                encoding="utf-8",
            )
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,legacy\n"
                "2026-07-16,甲队,乙队,,,1001,first\n"
                "2026-07-16,甲队,乙队,,,1001,second\n",
                encoding="utf-8",
            )
            fallback = [{"homeTeam": "甲队", "awayTeam": "乙队", "score": "2:1", "source_record_id": "678"}]
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")), patch.object(results, "fetch_zgzcw_results", return_value=fallback):
                path = results.update_results(date(2026, 7, 16))

            rows = self.read_rows(path)
            self.assertEqual(2, len(rows))
            self.assertEqual(["1001", "1001"], [row["match_id"] for row in rows])
            self.assertEqual(("2", "1", "finished"), (rows[0]["home_goals"], rows[0]["away_goals"], rows[0]["result_status"]))
            self.assertEqual(("", ""), (rows[1]["home_goals"], rows[1]["away_goals"]))

    def test_ambiguous_fallback_ids_append_unavailable_without_touching_identified_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n"
                "2026-07-16,甲队,乙队,1001\n"
                "2026-07-16,甲队,乙队,3003\n",
                encoding="utf-8",
            )
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,legacy\n"
                "2026-07-16,甲队,乙队,1,0,2002,finished,keep\n",
                encoding="utf-8",
            )
            fallback = [{"homeTeam": "甲队", "awayTeam": "乙队", "score": "2:1", "source_record_id": "678"}]
            with patch.object(results, "ROOT", data.parent), patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")), patch.object(results, "fetch_zgzcw_results", return_value=fallback):
                path = results.update_results(date(2026, 7, 16))

            rows = self.read_rows(path)
            self.assertEqual(2, len(rows))
            self.assertEqual(("2002", "1", "0", "keep"), (rows[0]["match_id"], rows[0]["home_goals"], rows[0]["away_goals"], rows[0]["legacy"]))
            self.assertEqual(("", "2", "1", "unavailable"), (rows[1]["match_id"], rows[1]["home_goals"], rows[1]["away_goals"], rows[1]["result_status"]))

    def test_multiple_blank_fallback_candidates_are_ambiguous_and_remain_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n", encoding="utf-8")
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,legacy\n"
                "2026-07-16,甲队,乙队,,,,first\n"
                "2026-07-16,甲队,乙队,,,,second\n",
                encoding="utf-8",
            )
            fallback = [{"homeTeam": "甲队", "awayTeam": "乙队", "score": "2:1", "source_record_id": "678"}]
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")), patch.object(results, "fetch_zgzcw_results", return_value=fallback):
                path = results.update_results(date(2026, 7, 16))

            rows = self.read_rows(path)
            self.assertEqual(3, len(rows))
            self.assertEqual(["first", "second", ""], [row["legacy"] for row in rows])
            self.assertEqual(["", "", "2"], [row["home_goals"] for row in rows])
            self.assertEqual("unavailable", rows[2]["result_status"])

    def test_repeated_ambiguous_fallback_reuses_the_same_unavailable_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n"
                "2026-07-16,Team A,Team B,1001\n"
                "2026-07-16,Team A,Team B,3003\n",
                encoding="utf-8",
            )
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,legacy\n"
                "2026-07-16,Team A,Team B,1,0,2002,finished,identified\n"
                "2026-07-16,Team A,Team B,,,,blank-a\n"
                "2026-07-16,Team A,Team B,,,,blank-b\n",
                encoding="utf-8",
            )
            fallback = [{
                "homeTeam": "Team A",
                "awayTeam": "Team B",
                "score": "2:1",
                "source_record_id": "678",
            }]
            observations = [
                {
                    "homeTeam": "Team A", "awayTeam": "Team B", "full": ("2", "1"),
                    "half": None, "match_id": "", "result_source": "zgzcw",
                    "source_record_id": "678", "captured_at_bjt": "2026-07-17T11:00:00+08:00",
                },
                {
                    "homeTeam": "Team A", "awayTeam": "Team B", "full": ("2", "1"),
                    "half": None, "match_id": "", "result_source": "zgzcw",
                    "source_record_id": "678", "captured_at_bjt": "2026-07-17T12:00:00+08:00",
                },
            ]
            with (
                patch.object(results, "ROOT", data.parent),
                patch.object(results, "DATA_DIR", data),
                patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")),
                patch.object(results, "fetch_zgzcw_results", return_value=fallback),
                patch.object(results, "_fallback_result_row", side_effect=observations),
            ):
                path = results.update_results(date(2026, 7, 16))
                first_bytes = path.read_bytes()
                first_count = len(self.read_rows(path))
                results.update_results(date(2026, 7, 16))

            self.assertEqual(4, first_count)
            self.assertEqual(first_count, len(self.read_rows(path)))
            self.assertEqual(first_bytes, path.read_bytes())
            rows = self.read_rows(path)
            self.assertEqual(("2002", "1", "0"), (rows[0]["match_id"], rows[0]["home_goals"], rows[0]["away_goals"]))
            self.assertEqual(("", "unavailable", "678"), (rows[-1]["match_id"], rows[-1]["result_status"], rows[-1]["source_record_id"]))

    def test_fallback_without_source_record_id_is_unavailable_despite_unique_match_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n"
                "2026-07-16,Team A,Team B,1001\n",
                encoding="utf-8",
            )
            fallback = [{
                "homeTeam": "Team A",
                "awayTeam": "Team B",
                "score": "2:1",
                "source_record_id": "   ",
            }]
            observations = [
                {
                    "homeTeam": "Team A", "awayTeam": "Team B", "full": ("2", "1"),
                    "half": None, "match_id": "", "result_source": "zgzcw",
                    "source_record_id": "", "captured_at_bjt": "2026-07-17T11:00:00+08:00",
                },
                {
                    "homeTeam": "Team A", "awayTeam": "Team B", "full": ("2", "1"),
                    "half": None, "match_id": "", "result_source": "zgzcw",
                    "source_record_id": "", "captured_at_bjt": "2026-07-17T12:00:00+08:00",
                },
            ]
            with (
                patch.object(results, "ROOT", data.parent),
                patch.object(results, "DATA_DIR", data),
                patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")),
                patch.object(results, "fetch_zgzcw_results", return_value=fallback),
                patch.object(results, "_fallback_result_row", side_effect=observations),
            ):
                path = results.update_results(date(2026, 7, 16))
                first_bytes = path.read_bytes()
                first_count = len(self.read_rows(path))
                results.update_results(date(2026, 7, 16))

            row = self.read_rows(path)[0]
            self.assertEqual(1, first_count)
            self.assertEqual(first_count, len(self.read_rows(path)))
            self.assertEqual(first_bytes, path.read_bytes())
            self.assertEqual("1001", row["match_id"])
            self.assertEqual("unavailable", row["result_status"])
            self.assertEqual("", row["source_record_id"])
            self.assertEqual("2026-07-17T11:00:00+08:00", row["captured_at_bjt"])

    def test_repeated_missing_source_ambiguous_fallback_reuses_only_matching_unavailable_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n"
                "2026-07-16,Team A,Team B,1001\n"
                "2026-07-16,Team A,Team B,3003\n",
                encoding="utf-8",
            )
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,legacy\n"
                "2026-07-16,Team A,Team B,1,0,2002,finished,identified\n"
                "2026-07-16,Team A,Team B,,,,blank-a\n"
                "2026-07-16,Team A,Team B,,,,blank-b\n",
                encoding="utf-8",
            )
            fallback = [{
                "homeTeam": "Team A",
                "awayTeam": "Team B",
                "score": "2:1",
                "source_record_id": "",
            }]
            observations = [
                {
                    "homeTeam": "Team A", "awayTeam": "Team B", "full": ("2", "1"),
                    "half": None, "match_id": "", "result_source": "zgzcw",
                    "source_record_id": "", "captured_at_bjt": "2026-07-17T11:00:00+08:00",
                },
                {
                    "homeTeam": "Team A", "awayTeam": "Team B", "full": ("2", "1"),
                    "half": None, "match_id": "", "result_source": "zgzcw",
                    "source_record_id": "", "captured_at_bjt": "2026-07-17T12:00:00+08:00",
                },
                {
                    "homeTeam": "Team A", "awayTeam": "Team B", "full": ("3", "1"),
                    "half": None, "match_id": "", "result_source": "zgzcw",
                    "source_record_id": "", "captured_at_bjt": "2026-07-17T13:00:00+08:00",
                },
            ]
            with (
                patch.object(results, "ROOT", data.parent),
                patch.object(results, "DATA_DIR", data),
                patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")),
                patch.object(results, "fetch_zgzcw_results", return_value=fallback),
                patch.object(results, "_fallback_result_row", side_effect=observations),
            ):
                path = results.update_results(date(2026, 7, 16))
                first_bytes = path.read_bytes()
                first_count = len(self.read_rows(path))
                results.update_results(date(2026, 7, 16))
                self.assertEqual(first_count, len(self.read_rows(path)))
                self.assertEqual(first_bytes, path.read_bytes())
                protected = self.read_rows(path)[-1]
                fallback[0]["score"] = "3:1"
                results.update_results(date(2026, 7, 16))

            rows = self.read_rows(path)
            self.assertEqual(4, first_count)
            self.assertEqual(5, len(rows))
            self.assertEqual(("2002", "1", "0", "finished"), (rows[0]["match_id"], rows[0]["home_goals"], rows[0]["away_goals"], rows[0]["result_status"]))
            self.assertEqual(protected, rows[3])
            self.assertEqual(("", "3", "1", "unavailable"), (rows[4]["match_id"], rows[4]["home_goals"], rows[4]["away_goals"], rows[4]["result_status"]))

    def test_preexisting_score_without_status_is_protected_from_changed_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n", encoding="utf-8")
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,legacy\n"
                "2026-07-16,甲队,乙队,1,0,1001,,keep\n",
                encoding="utf-8",
            )
            direct = self.captured(
                "1001", ("2", "0"), "2026-07-17T11:00:00+08:00",
                result_source="sporttery",
            )
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", return_value=[direct]):
                path = results.update_results(date(2026, 7, 16))

            row = self.read_rows(path)[0]
            self.assertEqual(("1", "0", "keep"), (row["home_goals"], row["away_goals"], row["legacy"]))
            self.assertEqual("conflict", row["result_status"])

    def test_same_finished_observation_with_new_capture_time_is_byte_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n", encoding="utf-8")
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,result_source,source_record_id,captured_at_bjt\n"
                "2026-07-16,甲队,乙队,1,0,1001,finished,sporttery,1001,2026-07-17T10:00:00+08:00\n",
                encoding="utf-8",
            )
            first = self.captured(
                "1001", ("1", "0"), "2026-07-17T11:00:00+08:00",
                result_source="sporttery",
            )
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", return_value=[first]):
                path = results.update_results(date(2026, 7, 16))
                before = path.read_bytes()
                repeated = self.captured(
                    "1001", ("1", "0"), "2026-07-17T12:00:00+08:00",
                    result_source="sporttery",
                )
                with patch.object(results, "official_result_rows", return_value=[repeated]):
                    results.update_results(date(2026, 7, 16))

            self.assertEqual(before, path.read_bytes())

    def test_changed_score_same_record_conflicts_once_and_repeat_is_byte_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text("date,team_a,team_b,match_id\n", encoding="utf-8")
            (data / "bet_results.csv").write_text(
                "date,team_a,team_b,home_goals,away_goals,match_id,result_status,result_source,source_record_id,captured_at_bjt\n"
                "2026-07-16,甲队,乙队,1,0,1001,finished,sporttery,1001,2026-07-17T10:00:00+08:00\n",
                encoding="utf-8",
            )
            changed = self.captured(
                "1001", ("2", "0"), "2026-07-17T11:00:00+08:00",
                result_source="sporttery",
            )
            with patch.object(results, "DATA_DIR", data), patch.object(results, "official_result_rows", return_value=[changed]):
                path = results.update_results(date(2026, 7, 16))
                conflicted = path.read_bytes()
                later_repeat = self.captured(
                    "1001", ("2", "0"), "2026-07-17T13:00:00+08:00",
                    result_source="sporttery",
                )
                with patch.object(results, "official_result_rows", return_value=[later_repeat]):
                    results.update_results(date(2026, 7, 16))

            self.assertEqual(conflicted, path.read_bytes())
            row = self.read_rows(path)[0]
            self.assertEqual(("1", "0", "conflict"), (row["home_goals"], row["away_goals"], row["result_status"]))
            self.assertEqual("2026-07-17T10:00:00+08:00", row["captured_at_bjt"])


class ResultCliTest(unittest.TestCase):
    def test_reconcile_days_defaults_to_one_update_of_the_requested_date(self):
        with patch.object(results, "update_results", return_value=Path("results.csv")) as update:
            exit_code = results.main(["--date", "2026-07-21"])

        self.assertEqual(0, exit_code)
        self.assertEqual([call(date(2026, 7, 21))], update.call_args_list)

    def test_reconcile_days_accepted_endpoints_run_oldest_first(self):
        expected_starts = {
            1: date(2026, 7, 21),
            30: date(2026, 6, 22),
        }
        for reconcile_days, start in expected_starts.items():
            with self.subTest(reconcile_days=reconcile_days), patch.object(
                results, "update_results", return_value=Path("results.csv")
            ) as update:
                exit_code = results.main([
                    "--date", "2026-07-21", "--reconcile-days", str(reconcile_days),
                ])

            self.assertEqual(0, exit_code)
            self.assertEqual(
                [start + timedelta(days=offset) for offset in range(reconcile_days)],
                [call.args[0] for call in update.call_args_list],
            )

    def test_reconcile_days_runs_oldest_first_for_a_middle_value(self):
        with patch.object(results, "update_results", return_value=Path("results.csv")) as update:
            exit_code = results.main([
                "--date", "2026-07-21", "--reconcile-days", "3",
            ])

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [date(2026, 7, 19), date(2026, 7, 20), date(2026, 7, 21)],
            [call.args[0] for call in update.call_args_list],
        )

    def test_reconcile_days_rejects_values_outside_one_through_thirty_or_non_integers(self):
        for value in ("0", "31", "three"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as raised:
                results.main(["--date", "2026-07-21", "--reconcile-days", value])
            self.assertEqual(2, raised.exception.code)

    def test_date_rejects_non_padded_malformed_and_invalid_calendar_values(self):
        for value in ("2026-7-21", "20260721", "not-a-date", "2026-02-30"):
            with self.subTest(value=value):
                with patch.object(results, "update_results") as update:
                    with self.assertRaises(SystemExit) as raised:
                        results.main(["--date", value])
                self.assertEqual(2, raised.exception.code)
                update.assert_not_called()

    def test_reconcile_failure_stops_before_later_dates_and_propagates(self):
        failure = RuntimeError("result source unavailable")
        with patch.object(
            results,
            "update_results",
            side_effect=[Path("first.csv"), failure, Path("third.csv")],
        ) as update:
            with self.assertRaisesRegex(RuntimeError, "result source unavailable"):
                results.main([
                    "--date", "2026-07-21", "--reconcile-days", "3",
                ])

        self.assertEqual(
            [date(2026, 7, 19), date(2026, 7, 20)],
            [call.args[0] for call in update.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
