import csv
import hashlib
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import betting_ledger as ledger_module
from betting_ledger import ingest_locked_plan, write_ledger_atomic
from decision_bundle import create_decision_bundle, write_prediction_metadata
from generate_betting_plan import plan_csv_bytes
from official_markets import THREE_WAY_SELECTIONS, TOTAL_GOALS_SELECTIONS
from plan_lock import lock_plan, main, read_valid_lock, sha256_file as plan_lock_sha


BJT = timezone(timedelta(hours=8))
TARGET_DATE = date(2026, 7, 16)
SETTLED_AT = datetime(2026, 7, 17, 12, 0, tzinfo=BJT)
REPO_ROOT = Path(__file__).resolve().parents[1]


def canonical_paid_row(*, parlay: bool) -> dict:
    row = {
        "date": TARGET_DATE.isoformat(),
        "strategy_version": "value-v4",
        "model_version": "model-3",
        "match_id": "1001",
        "team_a": "A",
        "team_b": "B",
        "kickoff_local": "2026-07-16T20:00:00+08:00",
        "play": "HAD",
        "market_type": "had",
        "market_line": "",
        "selection": THREE_WAY_SELECTIONS["h"],
        "odds": "2.00",
        "locked_odds": "2.00",
        "odds_source": "sporttery",
        "odds_source_record_id": "odds-1001",
        "odds_captured_at_bjt": "2026-07-16T13:30:00+08:00",
        "raw_probability": "0.54",
        "calibrated_probability": "0.53",
        "official_market_probability": "0.50",
        "conservative_probability": "0.51",
        "edge": "0.01",
        "net_ev": "0.02",
        "full_kelly": "0.02",
        "kelly_fraction": "0.25",
        "data_quality_multiplier": "1.0",
        "volatility_multiplier": "1.0",
        "performance_multiplier": "1.0",
        "portfolio_rank": "1",
        "binding_limits": "daily",
        "stake": "20",
        "data_quality": "high",
        "volatility_band": "low",
        "legs_json": "[]",
    }
    if not parlay:
        return row
    legs = [
        {
            "match_id": "parlay-1",
            "market_type": "had",
            "selection": THREE_WAY_SELECTIONS["h"],
            "line": "",
            "odds": "2.00",
            "odds_source": "sporttery",
            "odds_source_record_id": "odds-parlay-1",
            "odds_captured_at_bjt": "2026-07-16T13:30:00+08:00",
        },
        {
            "match_id": "parlay-2",
            "market_type": "ttg",
            "selection": TOTAL_GOALS_SELECTIONS["s2"],
            "line": "",
            "odds": "3.00",
            "odds_source": "sporttery",
            "odds_source_record_id": "odds-parlay-2",
            "odds_captured_at_bjt": "2026-07-16T13:30:00+08:00",
        },
    ]
    return {
        **row,
        "match_id": "",
        "play": "PARLAY",
        "market_type": "parlay",
        "selection": "combo",
        "odds": "6.00",
        "locked_odds": "6.00",
        "stake": "10",
        "legs_json": json.dumps(legs, ensure_ascii=False),
    }


def _concurrent_lock_worker(
    root_text: str,
    locked_at_text: str,
    barrier,
    result_queue,
) -> None:
    import plan_lock

    original_sha256_file = plan_lock.sha256_file
    hash_count = 0

    def synchronized_sha256_file(path: Path) -> str:
        nonlocal hash_count
        digest = original_sha256_file(path)
        hash_count += 1
        if hash_count == 2:
            barrier.wait(timeout=10)
        return digest

    plan_lock.sha256_file = synchronized_sha256_file
    try:
        payload = plan_lock.lock_plan(
            Path(root_text),
            TARGET_DATE,
            datetime.fromisoformat(locked_at_text),
        )
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        result_queue.put(("ok", payload))


def _abandon_lock_worker(root_text: str) -> None:
    import plan_lock

    def terminate_before_publication(path: Path, target: Path) -> None:
        os._exit(23)

    Path.replace = terminate_before_publication
    plan_lock.lock_plan(
        Path(root_text),
        TARGET_DATE,
        datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
    )


class PlanLockTest(unittest.TestCase):
    def make_artifacts(self, root: Path) -> None:
        (root / "output").mkdir()
        (root / "data" / "odds_snapshots").mkdir(parents=True)
        (root / "config.json").write_text("{}\n", encoding="utf-8")
        (root / "betting_config.json").write_bytes(
            (REPO_ROOT / "betting_config.json").read_bytes()
        )
        for name in (
            "predict_today.py",
            "generate_betting_plan.py",
            "value_candidates.py",
            "value_portfolio.py",
            "official_markets.py",
            "betting_ledger.py",
            "strategy_controls.py",
        ):
            (root / name).write_text(f"MODULE = {name!r}\n", encoding="utf-8")
        self.write_csv(
            root / "data" / "team_ratings.csv",
            [{"team": "A", "elo": "1500"}, {"team": "B", "elo": "1490"}],
        )
        self.write_csv(
            root / "data" / "fixtures.csv",
            [{
                "date": "2026-07-16",
                "team_a": "A",
                "team_b": "B",
                "match_id": "1001",
                "kickoff_at": "2026-07-16T20:00:00+08:00",
            }],
        )
        odds_path = root / "data" / "sporttery_odds_2026-07-16.json"
        odds_path.write_text(
            json.dumps({"1001": {"had": {"h": "2.00", "d": "3.20", "a": "3.50"}}}),
            encoding="utf-8",
        )
        extract_fixtures = (
            root / "data" / "import_extracts" / "2026-07-16" / "fixtures.csv"
        )
        extract_odds = extract_fixtures.with_name("odds.json")
        extract_fixtures.parent.mkdir(parents=True)
        extract_fixtures.write_bytes((root / "data" / "fixtures.csv").read_bytes())
        extract_odds.write_bytes(odds_path.read_bytes())
        def record(path: Path) -> dict:
            content = path.read_bytes()
            return {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        manifest_path = root / "data" / "import_manifests" / "2026-07-16.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(
            json.dumps({
                "schema_version": 1,
                "target_date": "2026-07-16",
                "source": "sporttery",
                "imported_at_bjt": "2026-07-16T13:29:00+08:00",
                "fixtures": record(extract_fixtures),
                "odds": record(extract_odds),
            }),
            encoding="utf-8",
        )
        self.write_csv(
            root / "output" / "predictions_2026-07-16.csv",
            [{
                "date": "2026-07-16",
                "team_a": "A",
                "team_b": "B",
                "match_id": "1001",
                "kickoff_at": "2026-07-16T20:00:00+08:00",
            }],
        )
        self.write_csv(root / "output" / "betting_ledger.csv", [])
        self.write_csv(root / "output" / "observation_ledger.csv", [])
        self.write_csv(root / "data" / "draw_training_samples.csv", [])
        (root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json").write_text(
            json.dumps({
                "target_date": "2026-07-16",
                "captured_at": "2026-07-16T13:30:00+08:00",
                "capture_phase": "decision",
                "source": "sporttery",
                "import_manifest": record(manifest_path),
                "matches": [{
                    "match_id": "1001",
                    "team_a": "A",
                    "team_b": "B",
                    "match_num": "001",
                    "kickoff_at": "2026-07-16T20:00:00+08:00",
                    "markets": {
                        "had": {"h": "2.00", "d": "3.20", "a": "3.50"},
                        "hhad": {},
                        "ttg": {},
                    },
                    "single_eligibility": {"had": True, "hhad": False, "ttg": False},
                }],
            }),
            encoding="utf-8",
        )
        write_prediction_metadata(
            root,
            TARGET_DATE,
            datetime(2026, 7, 16, 13, 30, 30, tzinfo=BJT),
        )
        bundle = create_decision_bundle(
            root,
            TARGET_DATE,
            datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
        )
        (root / "output" / "betting_plan_2026-07-16.csv").write_bytes(
            plan_csv_bytes(bundle["paid_plan_evidence"]["rows"])
        )

    @staticmethod
    def write_csv(path: Path, rows: list[dict]) -> None:
        fields = sorted({key for row in rows for key in row}) or ["placeholder"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def make_lock(self, root: Path) -> dict:
        return lock_plan(
            root,
            TARGET_DATE,
            datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
        )

    def lock_path(self, root: Path) -> Path:
        return root / "output" / "plan_lock_2026-07-16.json"

    def write_lock_payload(self, root: Path, payload: dict) -> None:
        self.lock_path(root).write_text(json.dumps(payload), encoding="utf-8")

    def test_lock_is_valid_only_while_plan_and_odds_hashes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
            )
            self.assertIsNotNone(read_valid_lock(root, date(2026, 7, 16)))
            (root / "output" / "betting_plan_2026-07-16.csv").write_text(
                "changed", encoding="utf-8"
            )
            self.assertIsNone(read_valid_lock(root, date(2026, 7, 16)))

    def test_real_lock_rejects_joint_plan_and_lock_tamper_for_single_and_parlay(self):
        for parlay in (False, True):
            with self.subTest(parlay=parlay), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_artifacts(root)
                lock = self.make_lock(root)
                plan_row = canonical_paid_row(parlay=parlay)
                plan_path = root / lock["plan_path"]
                self.write_csv(plan_path, [plan_row])
                lock["plan_sha256"] = plan_lock_sha(plan_path)
                self.write_lock_payload(root, lock)

                canonical = ingest_locked_plan(
                    [], [plan_row], lock, canonical_evidence={}
                )[0]
                canonical["row_payload_sha256"] = ledger_module._row_payload_digest(
                    canonical
                )
                write_ledger_atomic(
                    root / "output" / "betting_ledger.csv", [canonical]
                )

                self.assertIsNone(read_valid_lock(root, TARGET_DATE))
                with self.assertRaisesRegex(ValueError, "lock|evidence|anchor"):
                    ledger_module.settle_ledger(root, {}, SETTLED_AT)

    def test_next_day_shared_fixture_update_preserves_prior_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)

            self.write_csv(
                root / "data" / "fixtures.csv",
                [{
                    "date": "2026-07-17",
                    "team_a": "Next A",
                    "team_b": "Next B",
                    "match_id": "next-1",
                    "kickoff_at": "2026-07-17T20:00:00+08:00",
                }],
            )

            self.assertIsNotNone(read_valid_lock(root, TARGET_DATE))

    def test_lock_derives_zgzcw_source_from_the_validated_decision_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            bundle_path = root / "output" / "decision_bundle_2026-07-16.json"
            bundle_path.write_text("{}\n", encoding="utf-8")
            plan_path = root / "output" / "betting_plan_2026-07-16.csv"
            bundle = {
                "locked_at_bjt": "2026-07-16T13:31:00+08:00",
                "paid_plan_evidence": {
                    "plan_sha256": plan_lock_sha(plan_path),
                    "bytes": plan_path.stat().st_size,
                    "rows_sha256": "c" * 64,
                },
                "decision_snapshot": {
                    "source": "zgzcw",
                    "path": "data/odds_snapshots/2026-07-16-133000-decision.json",
                    "sha256": plan_lock_sha(root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json"),
                },
            }

            with patch(
                "plan_lock.read_valid_decision_bundle",
                return_value=bundle,
                create=True,
            ):
                payload = lock_plan(
                    root,
                    TARGET_DATE,
                    datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
                )

            self.assertEqual("zgzcw", payload["odds_source"])
            self.assertEqual(
                "output/decision_bundle_2026-07-16.json",
                payload["decision_bundle_path"],
            )
            self.assertEqual(64, len(payload["decision_bundle_sha256"]))

    def test_lock_cli_no_longer_accepts_a_separate_source_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            bundle_path = root / "output" / "decision_bundle_2026-07-16.json"
            bundle_path.write_text("{}\n", encoding="utf-8")
            plan_path = root / "output" / "betting_plan_2026-07-16.csv"
            bundle = {
                "locked_at_bjt": "2026-07-16T13:31:00+08:00",
                "paid_plan_evidence": {
                    "plan_sha256": plan_lock_sha(plan_path),
                    "bytes": plan_path.stat().st_size,
                    "rows_sha256": "c" * 64,
                },
                "decision_snapshot": {
                    "source": "zgzcw",
                    "path": "data/odds_snapshots/2026-07-16-133000-decision.json",
                    "sha256": plan_lock_sha(root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json"),
                },
            }
            with (
                patch("plan_lock.read_valid_decision_bundle", return_value=bundle, create=True),
                patch.object(
                    sys,
                    "argv",
                    [
                        "plan_lock.py",
                        "lock",
                        "--date",
                        "2026-07-16",
                        "--locked-at",
                        "2026-07-16T13:31:00+08:00",
                    ],
                ),
                patch.object(os, "getcwd", return_value=str(root)),
            ):
                self.assertEqual(0, main())

    def test_relocking_an_unchanged_plan_preserves_the_first_lock_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            first = lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
            )
            second = lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 14, 5, tzinfo=BJT),
            )
            self.assertEqual(first, second)
            self.assertEqual("2026-07-16T13:31:00+08:00", second["locked_at_bjt"])

    def test_read_valid_lock_rejects_unsupported_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            payload = self.make_lock(root)
            payload["schema_version"] = 999
            self.write_lock_payload(root, payload)

            self.assertIsNone(read_valid_lock(root, TARGET_DATE))

    def test_read_valid_lock_rejects_wrong_report_date_and_paths(self):
        cases = {
            "report date": ("report_date", "2026-07-15"),
            "plan path": ("plan_path", "output/betting_plan_2026-07-15.csv"),
            "odds path": ("odds_path", "data/sporttery_odds_2026-07-15.json"),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_artifacts(root)
                payload = self.make_lock(root)
                payload[field] = value
                self.write_lock_payload(root, payload)

                self.assertIsNone(read_valid_lock(root, TARGET_DATE))

    def test_read_valid_lock_rejects_empty_hashes(self):
        for field in ("plan_sha256", "odds_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_artifacts(root)
                payload = self.make_lock(root)
                payload[field] = ""
                self.write_lock_payload(root, payload)

                self.assertIsNone(read_valid_lock(root, TARGET_DATE))

    def test_read_valid_lock_rejects_missing_artifacts(self):
        artifacts = (
            Path("output/betting_plan_2026-07-16.csv"),
            Path("output/decision_bundle_2026-07-16.json"),
            Path("data/odds_snapshots/2026-07-16-133000-decision.json"),
        )
        for artifact in artifacts:
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_artifacts(root)
                self.make_lock(root)
                (root / artifact).unlink()

                self.assertIsNone(read_valid_lock(root, TARGET_DATE))

    def test_lock_plan_preserves_malformed_existing_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_path = self.lock_path(root)
            original_bytes = b'{"schema_version":'
            lock_path.write_bytes(original_bytes)

            with self.assertRaisesRegex(RuntimeError, "existing plan lock is invalid"):
                self.make_lock(root)

            self.assertEqual(original_bytes, lock_path.read_bytes())

    def test_lock_plan_preserves_wrong_existing_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            payload = self.make_lock(root)
            payload["report_date"] = "2026-07-15"
            lock_path = self.lock_path(root)
            original_bytes = json.dumps(payload).encode("utf-8")
            lock_path.write_bytes(original_bytes)

            with self.assertRaisesRegex(RuntimeError, "existing plan lock is invalid"):
                self.make_lock(root)

            self.assertEqual(original_bytes, lock_path.read_bytes())

    def test_lock_plan_preserves_existing_lock_after_artifacts_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            self.make_lock(root)
            lock_path = self.lock_path(root)
            original_bytes = lock_path.read_bytes()
            (root / "output" / "betting_plan_2026-07-16.csv").write_text(
                "changed", encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "existing plan lock is invalid"):
                lock_plan(
                    root,
                    TARGET_DATE,
                    datetime(2026, 7, 16, 14, 5, tzinfo=BJT),
                )

            self.assertEqual(original_bytes, lock_path.read_bytes())

    def test_concurrent_same_date_locking_returns_the_first_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_lock_worker,
                    args=(
                        str(root),
                        locked_at,
                        barrier,
                        result_queue,
                    ),
                )
                for locked_at in (
                    "2026-07-16T13:31:00+08:00",
                    "2026-07-16T13:31:00+08:00",
                )
            ]

            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)

            self.assertTrue(all(not process.is_alive() for process in processes))
            results = [result_queue.get(timeout=5) for _ in processes]
            self.assertEqual(
                ["ok", "ok"],
                [status for status, _ in results],
                results,
            )
            payloads = [payload for _, payload in results]
            self.assertEqual(payloads[0], payloads[1])
            self.assertEqual(
                payloads[0],
                json.loads(self.lock_path(root).read_text(encoding="utf-8")),
            )
            self.assertFalse(
                (root / "output" / "plan_lock_2026-07-16.json.tmp").exists()
            )

    def test_rerun_recovers_after_owner_death_leaves_stale_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_abandon_lock_worker, args=(str(root),))
            process.start()
            process.join(timeout=15)

            self.assertFalse(process.is_alive())
            self.assertEqual(23, process.exitcode)
            temp_path = root / "output" / "plan_lock_2026-07-16.json.tmp"
            process_lock_path = root / "output" / "plan_lock_2026-07-16.json.lock"
            self.assertTrue(temp_path.exists())
            self.assertTrue(process_lock_path.exists())
            self.assertFalse(self.lock_path(root).exists())

            try:
                with patch("plan_lock.CLAIM_WAIT_SECONDS", 0.05):
                    payload = self.make_lock(root)
            except RuntimeError as exc:
                self.fail(f"dead owner must not block rerun: {exc}")

            self.assertEqual(payload, read_valid_lock(root, TARGET_DATE))
            self.assertFalse(temp_path.exists())
            self.assertTrue(process_lock_path.exists())

    def test_publisher_does_not_remove_a_successor_temp_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            temp_path = root / "output" / "plan_lock_2026-07-16.json.tmp"
            original_replace = Path.replace

            def replace_then_create_successor(path: Path, target: Path) -> Path:
                result = original_replace(path, target)
                path.write_bytes(b"successor claim")
                return result

            with patch.object(Path, "replace", new=replace_then_create_successor):
                self.make_lock(root)

            self.assertTrue(temp_path.exists(), "successor claim must still exist")
            self.assertEqual(b"successor claim", temp_path.read_bytes())

    def test_is_locked_cli_returns_zero_for_a_valid_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
            )
            with patch.object(sys, "argv", [
                "plan_lock.py", "is-locked", "--date", "2026-07-16"
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertEqual(0, main())

    def test_is_locked_cli_returns_one_for_a_missing_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(sys, "argv", [
                "plan_lock.py", "is-locked", "--date", "2026-07-16"
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertEqual(1, main())

    def test_is_locked_cli_returns_one_for_an_invalid_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
            )
            (root / "data" / "odds_snapshots" / "2026-07-16-133000-decision.json").write_text(
                "changed", encoding="utf-8"
            )
            with patch.object(sys, "argv", [
                "plan_lock.py", "is-locked", "--date", "2026-07-16"
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertEqual(1, main())

    def test_lock_cli_returns_nonzero_when_an_artifact_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "output").mkdir()
            (root / "data").mkdir()
            with patch.object(sys, "argv", [
                "plan_lock.py",
                "lock",
                "--date",
                "2026-07-16",
                "--locked-at",
                "2026-07-16T13:31:00+08:00",
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertNotEqual(0, main())

    def test_lock_cli_rejects_a_naive_locked_at_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            with patch.object(sys, "argv", [
                "plan_lock.py",
                "lock",
                "--date",
                "2026-07-16",
                "--locked-at",
                "2026-07-16T13:31:00",
            ]), patch.object(os, "getcwd", return_value=str(root)):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertNotEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
