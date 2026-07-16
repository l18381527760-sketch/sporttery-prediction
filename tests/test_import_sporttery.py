import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import import_sporttery
from report_status import artifact_state


TARGET_DATE = date(2026, 7, 16)


class ImportSportterySourceStatusTest(unittest.TestCase):
    def test_write_source_status_marks_an_explicit_zero_fixture_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(import_sporttery, "DATA_DIR", data_dir):
                path = import_sporttery.write_source_status(
                    "test", TARGET_DATE, fixture_count=0
                )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(0, payload["fixture_count"])
            self.assertTrue(payload["no_fixtures"])

    def test_write_source_status_marks_nonzero_fixture_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(import_sporttery, "DATA_DIR", data_dir):
                path = import_sporttery.write_source_status(
                    "test", TARGET_DATE, fixture_count=2
                )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["fixture_count"])
            self.assertFalse(payload["no_fixtures"])

    def test_write_source_status_rejects_an_unverified_fixture_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(import_sporttery, "DATA_DIR", data_dir):
                with self.assertRaises(ValueError):
                    import_sporttery.write_source_status(
                        "test", TARGET_DATE, fixture_count=-1
                    )

            self.assertFalse((data_dir / "source_status.json").exists())

    def test_main_publishes_the_count_from_a_successful_zero_fixture_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            with patch.object(import_sporttery, "DATA_DIR", data_dir), patch.object(
                sys, "argv", ["import_sporttery.py", "--date", "2026-07-16"]
            ), patch.object(import_sporttery, "fetch_selling_matches", return_value=[]), patch.object(
                import_sporttery, "fetch_zgzcw_matches", return_value=[]
            ), patch.object(
                import_sporttery, "attach_had_odds", side_effect=lambda matches, _: matches
            ), patch.object(
                import_sporttery,
                "attach_professional_market",
                side_effect=lambda matches, _: matches,
            ):
                self.assertEqual(0, import_sporttery.main())

            payload = json.loads((data_dir / "source_status.json").read_text(encoding="utf-8"))
            self.assertEqual(0, payload["fixture_count"])
            self.assertTrue(payload["no_fixtures"])

    def test_main_publishes_the_count_from_the_written_nonzero_fixture_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            match = {
                "matchId": "001",
                "matchNumStr": "001",
                "leagueName": "test",
                "homeTeam": "Home",
                "awayTeam": "Away",
            }
            with patch.object(import_sporttery, "DATA_DIR", data_dir), patch.object(
                sys, "argv", ["import_sporttery.py", "--date", "2026-07-16"]
            ), patch.object(
                import_sporttery, "fetch_selling_matches", return_value=[match]
            ), patch.object(
                import_sporttery, "fetch_zgzcw_matches", return_value=[]
            ), patch.object(
                import_sporttery, "collect_odds", return_value={}
            ), patch.object(
                import_sporttery, "attach_had_odds", side_effect=lambda matches, _: matches
            ), patch.object(
                import_sporttery,
                "attach_professional_market",
                side_effect=lambda matches, _: matches,
            ):
                self.assertEqual(0, import_sporttery.main())

            payload = json.loads((data_dir / "source_status.json").read_text(encoding="utf-8"))
            self.assertEqual(1, payload["fixture_count"])
            self.assertFalse(payload["no_fixtures"])

    def test_main_does_not_publish_zero_metadata_when_count_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            with patch.object(import_sporttery, "DATA_DIR", data_dir), patch.object(
                sys, "argv", ["import_sporttery.py", "--date", "2026-07-16"]
            ), patch.object(import_sporttery, "fetch_selling_matches", return_value=[]), patch.object(
                import_sporttery, "fetch_zgzcw_matches", return_value=[]
            ), patch.object(
                import_sporttery, "attach_had_odds", side_effect=lambda matches, _: matches
            ), patch.object(
                import_sporttery,
                "attach_professional_market",
                side_effect=lambda matches, _: matches,
            ), patch.object(
                import_sporttery,
                "count_written_fixtures",
                side_effect=ValueError("count failed"),
            ):
                with self.assertRaisesRegex(ValueError, "count failed"):
                    import_sporttery.main()

            self.assertFalse((data_dir / "source_status.json").exists())

    def test_main_fails_closed_when_all_fallback_sources_return_ambiguous_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            with patch.object(import_sporttery, "DATA_DIR", data_dir), patch.object(
                sys, "argv", ["import_sporttery.py", "--date", "2026-07-16"]
            ), patch.object(
                import_sporttery,
                "fetch_selling_matches",
                side_effect=RuntimeError("official unavailable"),
            ), patch.object(
                import_sporttery, "fetch_zgzcw_matches", return_value=[]
            ), patch.object(import_sporttery, "fetch_espn_matches", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "could not verify an empty schedule"):
                    import_sporttery.main()

            self.assertFalse((data_dir / "source_status.json").exists())
            state = artifact_state(root, TARGET_DATE)
            self.assertFalse(state["source_ready"])
            self.assertFalse(state["fixtures_ready"])


if __name__ == "__main__":
    unittest.main()
