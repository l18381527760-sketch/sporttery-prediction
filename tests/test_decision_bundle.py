import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import generate_betting_plan as strategy
import predict_today
from decision_bundle import (
    BUNDLE_SCHEMA_VERSION,
    PREDICTION_METADATA_SCHEMA_VERSION,
    canonical_json_sha256,
    create_decision_bundle,
    read_valid_decision_bundle,
    write_prediction_metadata,
)


BJT = timezone(timedelta(hours=8))
TARGET_DATE = date(2026, 7, 16)
GENERATED_AT = datetime(2026, 7, 16, 13, 31, tzinfo=BJT)
LOCKED_AT = datetime(2026, 7, 16, 13, 32, tzinfo=BJT)
REPO_ROOT = Path(__file__).resolve().parents[1]


class DecisionBundleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data" / "odds_snapshots").mkdir(parents=True)
        (self.root / "output").mkdir()
        self._write_json(
            "config.json",
            {"market_blend_weight": 0.2, "confidence_thresholds": {}},
        )
        self.betting_config = json.loads(
            (REPO_ROOT / "betting_config.json").read_text(encoding="utf-8")
        )
        self.betting_config["value_strategy"]["activation_mode"] = "shadow"
        self._write_json("betting_config.json", self.betting_config)
        (self.root / "predict_today.py").write_text("MODEL = 'test'\n", encoding="utf-8")
        for name in (
            "generate_betting_plan.py",
            "value_candidates.py",
            "value_portfolio.py",
            "official_markets.py",
            "betting_ledger.py",
            "strategy_controls.py",
        ):
            (self.root / name).write_text(f"MODULE = {name!r}\n", encoding="utf-8")
        self._write_csv(
            "data/team_ratings.csv",
            [{"team": "Team A", "elo": "1500"}, {"team": "Team B", "elo": "1490"}],
        )
        self._write_csv(
            "data/fixtures.csv",
            [
                {
                    "date": "2026-07-16",
                    "team_a": "Team A",
                    "team_b": "Team B",
                    "match_id": "1001",
                    "kickoff_at": "2026-07-16T20:00:00+08:00",
                    "odds_a": "2.10",
                    "odds_draw": "3.20",
                    "odds_b": "3.40",
                },
                {
                    "date": "2026-07-17",
                    "team_a": "Later A",
                    "team_b": "Later B",
                    "match_id": "later",
                    "kickoff_at": "2026-07-17T20:00:00+08:00",
                },
            ],
        )
        self._write_json(
            "data/sporttery_odds_2026-07-16.json",
            {"1001": {"had": {"h": "2.10", "d": "3.20", "a": "3.40"}}},
        )
        extract_fixtures = (
            self.root / "data" / "import_extracts" / "2026-07-16" / "fixtures.csv"
        )
        extract_odds = extract_fixtures.with_name("odds.json")
        extract_fixtures.parent.mkdir(parents=True)
        extract_fixtures.write_bytes((self.root / "data" / "fixtures.csv").read_bytes())
        extract_odds.write_bytes(
            (self.root / "data" / "sporttery_odds_2026-07-16.json").read_bytes()
        )
        self.import_manifest_path = (
            self.root / "data" / "import_manifests" / "2026-07-16.json"
        )
        self._write_json(
            "data/import_manifests/2026-07-16.json",
            {
                "schema_version": 1,
                "target_date": "2026-07-16",
                "source": "zgzcw",
                "imported_at_bjt": "2026-07-16T13:00:00+08:00",
                "fixtures": self._file_record(extract_fixtures),
                "odds": self._file_record(extract_odds),
            },
        )
        self._write_csv(
            "output/predictions_2026-07-16.csv",
            [
                {
                    "date": "2026-07-16",
                    "match_id": "1001",
                    "team_a": "Team A",
                    "team_b": "Team B",
                    "kickoff_at": "2026-07-16T20:00:00+08:00",
                    "p_a": "0.5",
                    "p_draw": "0.3",
                    "p_b": "0.2",
                    "xg_a": "1.4",
                    "xg_b": "1.0",
                }
            ],
        )
        self._write_csv("output/betting_ledger.csv", [])
        self._write_csv("output/observation_ledger.csv", [])
        self._write_csv("data/draw_training_samples.csv", [])
        self.snapshot_path = self.root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json"
        self.snapshot_path.write_text(
            json.dumps(
                {
                    "target_date": "2026-07-16",
                    "captured_at": "2026-07-16T13:30:00+08:00",
                    "capture_phase": "decision",
                    "source": "zgzcw",
                    "import_manifest": self._file_record(self.import_manifest_path),
                    "matches": [
                        {
                            "match_id": "1001",
                            "team_a": "Team A",
                            "team_b": "Team B",
                            "match_num": "001",
                            "kickoff_at": "2026-07-16T20:00:00+08:00",
                            "markets": {
                                "had": {"h": "2.10", "d": "3.20", "a": "3.40"},
                                "hhad": {},
                                "ttg": {},
                            },
                            "single_eligibility": {"had": True, "hhad": False, "ttg": False},
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_prediction_metadata_records_real_generation_and_model_inputs(self):
        path = write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(PREDICTION_METADATA_SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(GENERATED_AT.isoformat(), payload["generated_at_bjt"])
        self.assertEqual("output/predictions_2026-07-16.csv", payload["predictions"]["path"])
        self.assertEqual(64, len(payload["predictions"]["sha256"]))
        self.assertEqual(1, payload["fixture_extract"]["match_count"])
        self.assertEqual(["1001"], [row["match_id"] for row in payload["fixture_extract"]["rows"]])
        self.assertEqual(
            {"config", "prediction_code", "ratings"},
            set(payload["model_inputs"]),
        )

    def test_bundle_binds_one_zgzcw_source_and_is_idempotent(self):
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)

        first = create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)
        first_bytes = (self.root / "output" / "decision_bundle_2026-07-16.json").read_bytes()
        second = create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)
        verified = read_valid_decision_bundle(
            self.root,
            TARGET_DATE,
            expected_locked_at=LOCKED_AT,
            verify_current_inputs=True,
        )

        self.assertEqual(first, second)
        self.assertEqual(first_bytes, (self.root / "output" / "decision_bundle_2026-07-16.json").read_bytes())
        self.assertEqual(BUNDLE_SCHEMA_VERSION, verified["schema_version"])
        self.assertEqual("zgzcw", verified["decision_snapshot"]["source"])
        self.assertEqual(
            "data/import_manifests/2026-07-16.json",
            verified["import_manifest"]["path"],
        )
        self.assertEqual(["1001"], [row["match_id"] for row in verified["decision_snapshot"]["match_identities"]])
        self.assertEqual(
            {"had": {"h": "2.10", "d": "3.20", "a": "3.40"}, "hhad": {}, "ttg": {}},
            verified["decision_snapshot"]["paid_market_values"][0]["markets"],
        )
        self.assertEqual("decision_snapshot", verified["roles"]["paid_odds"])
        self.assertIn("fixture_extract", verified["roles"]["model_reference_inputs"])
        self.assertEqual([], verified["history_inputs"]["paid_history"]["rows"])
        self.assertEqual({}, verified["history_inputs"]["account_metrics"]["payload"])
        self.assertEqual(
            verified["paid_plan_evidence"]["row_count"],
            len(verified["paid_plan_evidence"]["rows"]),
        )

    def test_bundle_rejects_snapshot_source_divergent_from_import_manifest(self):
        manifest = json.loads(self.import_manifest_path.read_text(encoding="utf-8"))
        manifest["source"] = "sporttery"
        self.import_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        snapshot["import_manifest"] = self._file_record(self.import_manifest_path)
        self.snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
        )
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)

        with self.assertRaises(ValueError):
            create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)

    def test_bundle_rejects_manifest_imported_after_snapshot_capture(self):
        manifest = json.loads(self.import_manifest_path.read_text(encoding="utf-8"))
        manifest["imported_at_bjt"] = "2026-07-16T13:30:01+08:00"
        self.import_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        snapshot["import_manifest"] = self._file_record(self.import_manifest_path)
        self.snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
        )
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)

        with self.assertRaisesRegex(ValueError, "import.*capture|snapshot"):
            create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)

    def test_existing_conflicting_bundle_fails_closed(self):
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)
        changed = json.loads(json.dumps(self.betting_config))
        changed["value_strategy"]["activation_mode"] = "active"
        self._write_json("betting_config.json", changed)

        with self.assertRaisesRegex(ValueError, "conflicting decision bundle"):
            create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)

    def test_missing_prediction_metadata_cannot_create_bundle(self):
        with self.assertRaisesRegex(ValueError, "prediction metadata"):
            create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)

    def test_tampered_bound_prediction_invalidates_bundle(self):
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)
        prediction_path = self.root / "output" / "predictions_2026-07-16.csv"
        prediction_path.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            read_valid_decision_bundle(self.root, TARGET_DATE)

    def test_tampered_embedded_contract_fields_invalidate_bundle(self):
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)
        bundle_path = self.root / "output" / "decision_bundle_2026-07-16.json"
        original = json.loads(bundle_path.read_text(encoding="utf-8"))
        variants = []

        identity = json.loads(json.dumps(original))
        identity["decision_snapshot"]["match_identities"][0]["team_a"] = "Other"
        variants.append(identity)

        fixture = json.loads(json.dumps(original))
        fixture["fixture_extract"]["rows"][0]["new_field"] = "changed"
        fixture["fixture_extract"]["sha256"] = canonical_json_sha256(
            fixture["fixture_extract"]["rows"]
        )
        variants.append(fixture)

        roles = json.loads(json.dumps(original))
        roles["roles"]["model_reference_inputs"] = []
        variants.append(roles)

        for payload in variants:
            with self.subTest(payload=payload):
                bundle_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_valid_decision_bundle(self.root, TARGET_DATE)
        bundle_path.write_text(json.dumps(original), encoding="utf-8")

    def test_prediction_cli_persists_metadata_with_its_real_generation_time(self):
        with (
            patch.object(predict_today, "OUTPUT_DIR", self.root / "output"),
            patch.object(predict_today, "read_config", return_value={}),
            patch.object(predict_today, "load_ratings", return_value={}),
            patch.object(predict_today, "load_fixtures", return_value=[]),
            patch.object(predict_today, "write_prediction_metadata") as write_metadata,
            patch("sys.argv", ["predict_today.py", "--date", "2026-07-16"]),
        ):
            self.assertEqual(0, predict_today.main())

        write_metadata.assert_called_once()
        root, target_date, generated_at = write_metadata.call_args.args
        self.assertEqual(predict_today.ROOT, root)
        self.assertEqual(TARGET_DATE, target_date)
        self.assertIsNotNone(generated_at.tzinfo)
        self.assertIsNotNone(generated_at.utcoffset())

    def test_value_generator_uses_only_inputs_selected_by_the_bundle(self):
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        bundle = create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)
        built = SimpleNamespace(plan=[{"bet_id": "paid"}], observations=[], audit={})

        with (
            patch.object(strategy, "ROOT", self.root),
            patch.object(strategy, "build_value_v4_from_inputs", return_value=built) as builder,
        ):
            plan, observations = strategy.build_value_v4_plan(
                TARGET_DATE,
                locked_at=LOCKED_AT,
                decision_bundle=bundle,
            )

        self.assertEqual([{"bet_id": "paid"}], plan)
        self.assertEqual([], observations)
        inputs = builder.call_args.kwargs
        self.assertEqual(
            bundle["configuration"]["betting"]["payload"], inputs["config"]
        )
        self.assertEqual("zgzcw", inputs["snapshot"]["source"])
        self.assertEqual(
            bundle["decision_snapshot"]["path"],
            inputs["snapshot"]["_snapshot_record_id"],
        )
        self.assertEqual(["1001"], [row["match_id"] for row in inputs["predictions"]])
        self.assertEqual(
            bundle["history_inputs"]["paid_history"]["rows"],
            inputs["paid_history"],
        )

    def _write_json(self, relative: str, payload: dict):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _write_csv(self, relative: str, rows: list[dict]):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = sorted({key for row in rows for key in row}) or ["placeholder"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def _file_record(self, path: Path) -> dict:
        payload = path.read_bytes()
        return {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }


if __name__ == "__main__":
    unittest.main()
