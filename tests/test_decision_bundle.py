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
import live_odds
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
            [
                {
                    "team": "Team A",
                    "elo": "1500",
                    "attack": "0.10",
                    "defense": "0.05",
                    "form": "0.02",
                    "injury": "0",
                    "rest_days": "4",
                    "home_adv": "0.08",
                },
                {
                    "team": "Team B",
                    "elo": "1490",
                    "attack": "0.04",
                    "defense": "0.03",
                    "form": "0.01",
                    "injury": "0",
                    "rest_days": "4",
                    "home_adv": "0",
                },
            ],
        )
        self._write_csv(
            "data/fixtures.csv",
            [
                {
                    "date": "2026-07-16",
                    "kickoff_local": "20:00",
                    "team_a": "Team A",
                    "team_b": "Team B",
                    "match_id": "1001",
                    "kickoff_at": "2026-07-16T20:00:00+08:00",
                    "stage": "group",
                    "neutral": "false",
                    "venue": "Test Venue",
                    "odds_a": "2.10",
                    "odds_draw": "3.20",
                    "odds_b": "3.40",
                },
                {
                    "date": "2026-07-17",
                    "kickoff_local": "20:00",
                    "team_a": "Later A",
                    "team_b": "Later B",
                    "match_id": "later",
                    "kickoff_at": "2026-07-17T20:00:00+08:00",
                    "stage": "group",
                    "neutral": "false",
                    "venue": "Later Venue",
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
        extract_ratings = extract_fixtures.with_name("ratings.csv")
        extract_fixtures.parent.mkdir(parents=True)
        extract_fixtures.write_bytes((self.root / "data" / "fixtures.csv").read_bytes())
        extract_odds.write_bytes(
            (self.root / "data" / "sporttery_odds_2026-07-16.json").read_bytes()
        )
        extract_ratings.write_bytes(
            (self.root / "data" / "team_ratings.csv").read_bytes()
        )
        self.import_manifest_path = (
            self.root / "data" / "import_manifests" / "2026-07-16.json"
        )
        self._write_json(
            "data/import_manifests/2026-07-16.json",
            {
                "schema_version": 2,
                "target_date": "2026-07-16",
                "source": "zgzcw",
                "imported_at_bjt": "2026-07-16T13:00:00+08:00",
                "fixtures": self._file_record(extract_fixtures),
                "odds": self._file_record(extract_odds),
                "ratings": self._file_record(extract_ratings),
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
            {"config", "fixtures", "prediction_code", "ratings"},
            set(payload["model_inputs"]),
        )

    def test_prediction_cli_consumes_manifest_inputs_without_mutable_history(self):
        self._write_json(
            "config.json",
            json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8")),
        )
        self._write_csv(
            "data/team_ratings.csv",
            [
                {
                    "team": team,
                    "elo": elo,
                    "attack": "4.0",
                    "defense": "4.0",
                    "form": "4.0",
                    "injury": "0",
                    "rest_days": "4",
                    "home_adv": "0",
                }
                for team, elo in (
                    ("Team A", "2500"),
                    ("Team B", "500"),
                    ("Shared A", "1800"),
                    ("Shared B", "1700"),
                )
            ],
        )
        self._write_csv(
            "data/fixtures.csv",
            [
                {
                    "date": TARGET_DATE.isoformat(),
                    "kickoff_local": "21:00",
                    "kickoff_at": "2026-07-16T21:00:00+08:00",
                    "stage": "group",
                    "team_a": "Shared A",
                    "team_b": "Shared B",
                    "neutral": "false",
                    "venue": "Mutable Venue",
                    "match_id": "mutable-1",
                }
            ],
        )
        self._write_csv(
            "data/team_history_features.csv",
            [
                {
                    "team": "Team A",
                    "matches": "6",
                    "attack": "9.0",
                    "defense": "9.0",
                    "form": "9.0",
                    "rest_days": "1",
                }
            ],
        )

        with (
            patch.object(predict_today, "ROOT", self.root),
            patch.object(predict_today, "DATA_DIR", self.root / "data"),
            patch.object(predict_today, "OUTPUT_DIR", self.root / "output"),
            patch.object(
                predict_today,
                "predict_fixture",
                wraps=predict_today.predict_fixture,
            ) as predict_fixture,
            patch("sys.argv", ["predict_today.py", "--date", "2026-07-16"]),
        ):
            self.assertEqual(0, predict_today.main())

        fixture, ratings, _config = predict_fixture.call_args.args
        self.assertEqual(("Team A", "Team B", "1001"), (
            fixture.team_a,
            fixture.team_b,
            fixture.match_id,
        ))
        self.assertEqual(1500.0, ratings["Team A"].elo)
        self.assertEqual(0.10, ratings["Team A"].attack)
        with (
            self.root / "output" / "predictions_2026-07-16.csv"
        ).open(encoding="utf-8-sig", newline="") as handle:
            prediction = next(csv.DictReader(handle))
        self.assertEqual("1001", prediction["match_id"])

        metadata = json.loads(
            (
                self.root
                / "output"
                / "predictions_2026-07-16.meta.json"
            ).read_text(encoding="utf-8")
        )
        manifest = json.loads(
            self.import_manifest_path.read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["fixtures"], metadata["model_inputs"]["fixtures"])
        self.assertEqual(manifest["ratings"], metadata["model_inputs"]["ratings"])
        self.assertNotIn("history", metadata["model_inputs"])
        self.assertEqual(
            ["1001"],
            [row["match_id"] for row in metadata["fixture_extract"]["rows"]],
        )

    def test_prediction_cli_requires_schema_two_manifest_even_without_files(self):
        self._write_json(
            "config.json",
            json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8")),
        )
        manifest_bytes = self.import_manifest_path.read_bytes()
        for state in ("missing", "old_schema"):
            with self.subTest(state=state):
                self.import_manifest_path.write_bytes(manifest_bytes)
                if state == "missing":
                    self.import_manifest_path.unlink()
                else:
                    manifest = json.loads(manifest_bytes)
                    manifest["schema_version"] = 1
                    self.import_manifest_path.write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )
                with (
                    patch.object(predict_today, "ROOT", self.root),
                    patch.object(predict_today, "DATA_DIR", self.root / "data"),
                    patch.object(predict_today, "OUTPUT_DIR", self.root / "output"),
                    patch(
                        "sys.argv",
                        [
                            "predict_today.py",
                            "--date",
                            "2026-07-16",
                            "--no-files",
                        ],
                    ),
                    self.assertRaisesRegex(ValueError, "manifest|schema"),
                ):
                    predict_today.main()
        self.import_manifest_path.write_bytes(manifest_bytes)

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

    def test_bundle_binds_exact_live_snapshot_without_matching_manifest_source(self):
        self._prepare_live_identity_fields()
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        live_path = live_odds.capture_live_snapshot(
            self.root,
            TARGET_DATE,
            datetime(2026, 7, 16, 13, 30, tzinfo=BJT),
            sporttery_fetcher=lambda day: [{
                "matchId": "1001",
                "matchNumStr": "001",
                "homeTeam": "Team A",
                "awayTeam": "Team B",
                "matchStatus": "Selling",
                "kickoff_at": "2026-07-16T20:00:00+08:00",
                "isSingleHad": True,
                "isSingleHhad": False,
                "isSingleTtg": False,
            }],
            sporttery_odds_fetcher=lambda match_id: {
                "had": {"h": "2.10", "d": "3.20", "a": "3.40"}, "hhad": {}, "ttg": {},
            },
        )

        bundle = create_decision_bundle(
            self.root, TARGET_DATE, LOCKED_AT, decision_snapshot_path=live_path
        )

        self.assertEqual(
            live_path.relative_to(self.root).as_posix(),
            bundle["decision_snapshot"]["path"],
        )
        self.assertEqual("sporttery", bundle["decision_snapshot"]["source"])
        self.assertEqual("live", bundle["decision_snapshot"]["payload"]["fetch_mode"])

    def test_live_bundle_reread_rejects_embedded_payload_reclassified_as_legacy(self):
        self._prepare_live_identity_fields()
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        bundle = create_decision_bundle(
            self.root,
            TARGET_DATE,
            LOCKED_AT,
            decision_snapshot_path=self._capture_live_snapshot(),
        )
        bundle_path = self.root / "output" / "decision_bundle_2026-07-16.json"
        snapshot = bundle["decision_snapshot"]["payload"]
        snapshot.pop("fetch_mode")
        snapshot["capture_phase"] = "decision"
        snapshot["source"] = "zgzcw"
        snapshot["import_manifest"] = self._file_record(self.import_manifest_path)
        bundle["decision_snapshot"]["source"] = "zgzcw"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "live snapshot|inconsistent"):
            read_valid_decision_bundle(self.root, TARGET_DATE)

    def test_legacy_bundle_reread_rejects_altered_embedded_snapshot_payload(self):
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        bundle = create_decision_bundle(self.root, TARGET_DATE, LOCKED_AT)
        bundle_path = self.root / "output" / "decision_bundle_2026-07-16.json"
        snapshot = bundle["decision_snapshot"]["payload"]
        snapshot["matches"][0]["markets"]["had"]["h"] = "9.99"
        bundle["decision_snapshot"]["paid_market_values"][0]["markets"] = snapshot[
            "matches"
        ][0]["markets"]
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "snapshot.*inconsistent"):
            read_valid_decision_bundle(self.root, TARGET_DATE)

    def test_live_bundle_creation_rejects_lock_at_or_after_fixture_kickoff(self):
        self._prepare_live_identity_fields()
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        live_path = self._capture_live_snapshot()

        with self.assertRaisesRegex(ValueError, "lock.*pre-kickoff"):
            create_decision_bundle(
                self.root,
                TARGET_DATE,
                datetime(2026, 7, 16, 20, 0, tzinfo=BJT),
                decision_snapshot_path=live_path,
            )

    def test_live_bundle_reread_rejects_lock_at_or_after_fixture_kickoff(self):
        self._prepare_live_identity_fields()
        write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
        bundle = create_decision_bundle(
            self.root,
            TARGET_DATE,
            LOCKED_AT,
            decision_snapshot_path=self._capture_live_snapshot(),
        )
        bundle_path = self.root / "output" / "decision_bundle_2026-07-16.json"
        bundle["locked_at_bjt"] = "2026-07-16T20:00:00+08:00"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "lock.*pre-kickoff"):
            read_valid_decision_bundle(self.root, TARGET_DATE)

    def test_live_bundle_requires_matching_canonical_match_number(self):
        variants = (
            ("live", "002"),
            ("prediction", "002"),
            ("fixture", "002"),
            ("prediction", ""),
            ("fixture", " "),
        )
        for artifact, match_num in variants:
            with self.subTest(artifact=artifact, match_num=match_num):
                bundle_path = self.root / "output" / "decision_bundle_2026-07-16.json"
                if bundle_path.exists():
                    bundle_path.unlink()
                self._prepare_live_identity_fields()
                if artifact == "prediction":
                    self._rewrite_prediction_match_number(match_num)
                elif artifact == "fixture":
                    self._rewrite_fixture_match_number(match_num)
                write_prediction_metadata(self.root, TARGET_DATE, GENERATED_AT)
                live_path = self._capture_live_snapshot(
                    match_num=match_num if artifact == "live" else "001"
                )

                with self.assertRaisesRegex(ValueError, "match_num|identities"):
                    create_decision_bundle(
                        self.root,
                        TARGET_DATE,
                        LOCKED_AT,
                        decision_snapshot_path=live_path,
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
        consumed_inputs = {
            "fixtures": {"path": "fixtures", "sha256": "a" * 64, "bytes": 1},
            "ratings": {"path": "ratings", "sha256": "b" * 64, "bytes": 1},
        }
        with (
            patch.object(predict_today, "OUTPUT_DIR", self.root / "output"),
            patch.object(predict_today, "read_config", return_value={}),
            patch.object(
                predict_today,
                "load_manifest_prediction_inputs",
                return_value=({}, [], consumed_inputs),
            ),
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
        self.assertEqual(
            consumed_inputs,
            write_metadata.call_args.kwargs["consumed_manifest_inputs"],
        )

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

    def _capture_live_snapshot(self, match_num: str = "001") -> Path:
        return live_odds.capture_live_snapshot(
            self.root,
            TARGET_DATE,
            datetime(2026, 7, 16, 13, 30, tzinfo=BJT),
            sporttery_fetcher=lambda day: [{
                "matchId": "1001",
                "matchNumStr": match_num,
                "homeTeam": "Team A",
                "awayTeam": "Team B",
                "matchStatus": "Selling",
                "kickoff_at": "2026-07-16T20:00:00+08:00",
                "isSingleHad": True,
                "isSingleHhad": False,
                "isSingleTtg": False,
            }],
            sporttery_odds_fetcher=lambda match_id: {
                "had": {"h": "2.10", "d": "3.20", "a": "3.40"},
                "hhad": {},
                "ttg": {},
            },
        )

    def _prepare_live_identity_fields(self) -> None:
        self._rewrite_prediction_match_number("001")
        self._rewrite_fixture_match_number("001")

    def _rewrite_prediction_match_number(self, match_num: str) -> None:
        self._write_csv(
            "output/predictions_2026-07-16.csv",
            [{
                "date": "2026-07-16",
                "match_id": "1001",
                "match_num": match_num,
                "team_a": "Team A",
                "team_b": "Team B",
                "kickoff_at": "2026-07-16T20:00:00+08:00",
                "p_a": "0.5",
                "p_draw": "0.3",
                "p_b": "0.2",
                "xg_a": "1.4",
                "xg_b": "1.0",
            }],
        )

    def _rewrite_fixture_match_number(self, match_num: str) -> None:
        extract_path = self.root / "data" / "import_extracts" / "2026-07-16" / "fixtures.csv"
        self._write_csv(
            "data/import_extracts/2026-07-16/fixtures.csv",
            [{
                "date": "2026-07-16",
                "match_id": "1001",
                "match_num": match_num,
                "team_a": "Team A",
                "team_b": "Team B",
                "kickoff_at": "2026-07-16T20:00:00+08:00",
            }],
        )
        manifest = json.loads(self.import_manifest_path.read_text(encoding="utf-8"))
        manifest["fixtures"] = self._file_record(extract_path)
        self._write_json("data/import_manifests/2026-07-16.json", manifest)

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
