import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fixture_identity import fixture_identity_rate, fixture_match_ids


DAY = date(2026, 7, 21)


class FixtureIdentityTest(unittest.TestCase):
    def write_manifest(self, root: Path, fixture_rows: str) -> None:
        data = root / "data"
        extracts = data / "import_extracts" / DAY.isoformat()
        manifests = data / "import_manifests"
        extracts.mkdir(parents=True)
        manifests.mkdir(parents=True)
        fixture = extracts / "fixtures.csv"
        fixture.write_text(
            "date,team_a,team_b,match_id,kickoff_at\n" + fixture_rows,
            encoding="utf-8",
        )
        odds = extracts / "odds.json"
        odds.write_text("{}", encoding="utf-8")
        ratings = extracts / "ratings.csv"
        ratings.write_text(
            "team,elo\n\u7532\u961f,1500\n\u4e59\u961f,1500\n",
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
            "target_date": DAY.isoformat(),
            "source": "sporttery",
            "imported_at_bjt": "2026-07-21T12:05:00+08:00",
            **records,
        }
        (manifests / "2026-07-21.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

    def test_reads_target_day_from_immutable_manifest_when_current_csv_is_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            self.write_manifest(
                root,
            "2026-07-21,\u7532\u961f,\u4e59\u961f,2040580,2026-07-21T18:00:00+08:00\n",
            )
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n2026-07-22,\u4e19\u961f,\u4e01\u961f,999\n",
                encoding="utf-8",
            )

            identities = fixture_match_ids(root, DAY)
            self.assertEqual(
                frozenset({"2040580"}),
                identities[("2026-07-21", "\u7532\u961f", "\u4e59\u961f")],
            )
            self.assertEqual((1, 1), fixture_identity_rate(root, DAY))

    def test_falls_back_to_current_fixture_csv_when_target_manifest_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id,kickoff_at\n"
                "2026-07-21,\u7532\u961f,\u4e59\u961f,2040580,2026-07-21T18:00:00+08:00\n"
                "2026-07-21,\u4e19\u961f,\u4e01\u961f,2040581,2026-07-21T20:00:00+08:00\n"
                "2026-07-22,\u7532\u961f,\u4e59\u961f,999,2026-07-22T18:00:00+08:00\n",
                encoding="utf-8",
            )

            identities = fixture_match_ids(root, DAY)
            self.assertEqual(
                {
                    ("2026-07-21", "\u7532\u961f", "\u4e59\u961f"): frozenset({"2040580"}),
                    ("2026-07-21", "\u4e19\u961f", "\u4e01\u961f"): frozenset({"2040581"}),
                },
                identities,
            )
            self.assertEqual((2, 2), fixture_identity_rate(root, DAY))

    def test_rejects_duplicate_provider_ids_for_different_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_manifest(
                root,
                "2026-07-21,\u7532\u961f,\u4e59\u961f,2040580,2026-07-21T18:00:00+08:00\n"
                "2026-07-21,\u4e19\u961f,\u4e01\u961f,2040580,2026-07-21T20:00:00+08:00\n",
            )
            with self.assertRaisesRegex(ValueError, "fixture match_id is duplicated"):
                fixture_match_ids(root, DAY)
