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


class ImportSportteryResponseValidationTest(unittest.TestCase):
    def valid_match(self, **overrides):
        return {
            "matchId": "001",
            "homeTeam": "Home",
            "awayTeam": "Away",
            "matchStatus": "Selling",
            **overrides,
        }

    def assert_invalid_target_match(self, payload, field):
        with patch.object(import_sporttery, "fetch_json", return_value=payload):
            try:
                import_sporttery.fetch_selling_matches(TARGET_DATE)
            except RuntimeError as exc:
                self.assertRegex(str(exc), field)
            except Exception as exc:
                self.fail(
                    f"invalid {field} must raise RuntimeError, got {type(exc).__name__}"
                )
            else:
                self.fail(f"invalid {field} did not raise RuntimeError")

    def test_fetch_selling_matches_rejects_malformed_success_payloads(self):
        malformed_payloads = {
            "non-object payload": [],
            "missing value": {"errorCode": 0},
            "non-object value": {"errorCode": 0, "value": []},
            "missing matchInfoList": {"errorCode": 0, "value": {}},
            "non-list matchInfoList": {
                "errorCode": 0,
                "value": {"matchInfoList": {}},
            },
            "non-object day": {
                "errorCode": 0,
                "value": {"matchInfoList": [None]},
            },
            "non-list target-day matches": {
                "errorCode": 0,
                "value": {
                    "matchInfoList": [
                        {
                            "businessDate": TARGET_DATE.isoformat(),
                            "subMatchList": None,
                        }
                    ]
                },
            },
            "non-object match": {
                "errorCode": 0,
                "value": {
                    "matchInfoList": [
                        {
                            "businessDate": TARGET_DATE.isoformat(),
                            "subMatchList": [None],
                        }
                    ]
                },
            },
        }

        for label, payload in malformed_payloads.items():
            with self.subTest(label=label), patch.object(
                import_sporttery, "fetch_json", return_value=payload
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "invalid Sporttery match-list response"
                ):
                    import_sporttery.fetch_selling_matches(TARGET_DATE)

    def test_fetch_selling_matches_accepts_an_explicit_empty_schedule(self):
        payload = {"errorCode": 0, "value": {"matchInfoList": []}}

        with patch.object(import_sporttery, "fetch_json", return_value=payload):
            self.assertEqual([], import_sporttery.fetch_selling_matches(TARGET_DATE))

    def test_fetch_selling_matches_rejects_nonempty_days_without_target_date(self):
        payload = {
            "errorCode": 0,
            "value": {
                "matchInfoList": [{
                    "businessDate": "2026-07-17",
                    "subMatchList": [],
                }]
            },
        }

        with patch.object(import_sporttery, "fetch_json", return_value=payload):
            with self.assertRaisesRegex(RuntimeError, "target date"):
                import_sporttery.fetch_selling_matches(TARGET_DATE)

    def test_fetch_selling_matches_requires_an_exact_business_date_for_every_day(self):
        invalid_dates = (None, "", "2026-7-16", "2026-02-30")
        for business_date in invalid_dates:
            payload = {
                "errorCode": 0,
                "value": {
                    "matchInfoList": [{
                        "businessDate": business_date,
                        "subMatchList": [],
                    }]
                },
            }
            with self.subTest(business_date=business_date), patch.object(
                import_sporttery, "fetch_json", return_value=payload
            ):
                with self.assertRaisesRegex(RuntimeError, "businessDate"):
                    import_sporttery.fetch_selling_matches(TARGET_DATE)

    def test_fetch_selling_matches_rejects_target_day_matches_missing_identity_fields(self):
        for field in ("matchId", "homeTeam", "awayTeam", "matchStatus"):
            match = self.valid_match()
            match.pop(field)
            payload = {
                "errorCode": 0,
                "value": {
                    "matchInfoList": [{
                        "businessDate": TARGET_DATE.isoformat(),
                        "subMatchList": [match],
                    }]
                },
            }
            with self.subTest(field=field), patch.object(
                import_sporttery, "fetch_json", return_value=payload
            ):
                with self.assertRaisesRegex(RuntimeError, field):
                    import_sporttery.fetch_selling_matches(TARGET_DATE)

    def test_fetch_selling_matches_rejects_invalid_target_day_string_field_types(self):
        for field in ("homeTeam", "awayTeam", "matchStatus"):
            for value in (None, "", "  ", [], {}, True, 7):
                match = self.valid_match(**{field: value})
                payload = {
                    "errorCode": 0,
                    "value": {
                        "matchInfoList": [{
                            "businessDate": TARGET_DATE.isoformat(),
                            "subMatchList": [match],
                        }]
                    },
                }
                with self.subTest(field=field, value=value):
                    self.assert_invalid_target_match(payload, field)

    def test_fetch_selling_matches_rejects_noncanonical_target_day_statuses(self):
        for status in ("Selling ", "selling", "SELLING", "Define ", "Unknown"):
            match = self.valid_match(matchStatus=status)
            payload = {
                "errorCode": 0,
                "value": {
                    "matchInfoList": [{
                        "businessDate": TARGET_DATE.isoformat(),
                        "subMatchList": [match],
                    }]
                },
            }
            with self.subTest(status=status):
                self.assert_invalid_target_match(payload, "matchStatus")

    def test_fetch_selling_matches_selects_all_valid_target_day_statuses(self):
        matches = [
            self.valid_match(matchId="001", matchStatus="Selling"),
            self.valid_match(matchId="002", matchStatus="Define"),
        ]
        payload = {
            "errorCode": 0,
            "value": {
                "matchInfoList": [{
                    "businessDate": TARGET_DATE.isoformat(),
                    "subMatchList": matches,
                }]
            },
        }

        with patch.object(import_sporttery, "fetch_json", return_value=payload):
            self.assertEqual(matches, import_sporttery.fetch_selling_matches(TARGET_DATE))

    def test_fetch_selling_matches_rejects_invalid_target_day_match_id_types(self):
        for value in (None, "", "  ", True, False, [], {}, 1.5):
            match = self.valid_match(matchId=value)
            payload = {
                "errorCode": 0,
                "value": {
                    "matchInfoList": [{
                        "businessDate": TARGET_DATE.isoformat(),
                        "subMatchList": [match],
                    }]
                },
            }
            with self.subTest(value=value):
                self.assert_invalid_target_match(payload, "matchId")

    def test_fetch_selling_matches_accepts_an_integer_match_id(self):
        match = self.valid_match(matchId=123456)
        payload = {
            "errorCode": 0,
            "value": {
                "matchInfoList": [{
                    "businessDate": TARGET_DATE.isoformat(),
                    "subMatchList": [match],
                }]
            },
        }
        with patch.object(import_sporttery, "fetch_json", return_value=payload):
            self.assertEqual([match], import_sporttery.fetch_selling_matches(TARGET_DATE))

    def test_fetch_selling_matches_accepts_valid_and_explicit_target_day_matches(self):
        match = self.valid_match()
        payload = {
            "errorCode": 0,
            "value": {
                "matchInfoList": [
                    {
                        "businessDate": TARGET_DATE.isoformat(),
                        "subMatchList": [match],
                    },
                    {
                        "businessDate": "2026-07-17",
                        "subMatchList": [],
                    },
                ]
            },
        }
        with patch.object(import_sporttery, "fetch_json", return_value=payload):
            self.assertEqual([match], import_sporttery.fetch_selling_matches(TARGET_DATE))

    def test_fetch_selling_matches_accepts_explicit_empty_target_day_submatches(self):
        payload = {
            "errorCode": 0,
            "value": {
                "matchInfoList": [{
                    "businessDate": TARGET_DATE.isoformat(),
                    "subMatchList": [],
                }]
            },
        }
        with patch.object(import_sporttery, "fetch_json", return_value=payload):
            self.assertEqual([], import_sporttery.fetch_selling_matches(TARGET_DATE))


class ImportSportterySourceStatusTest(unittest.TestCase):
    def test_write_source_status_marks_an_explicit_zero_fixture_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(import_sporttery, "DATA_DIR", data_dir):
                path = import_sporttery.write_source_status(
                    "test", TARGET_DATE, fixture_count=0
                )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("test", payload["source"])
            self.assertEqual("专业欧赔市场", payload["analysis_source"])
            self.assertEqual(TARGET_DATE.isoformat(), payload["target_date"])
            self.assertTrue(payload["fallback"])
            self.assertEqual("", payload["message"])
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
