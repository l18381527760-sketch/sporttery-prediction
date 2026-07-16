import csv
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from plan_lock import lock_plan, main, read_valid_lock


BJT = timezone(timedelta(hours=8))
TARGET_DATE = date(2026, 7, 16)


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
            "sporttery",
        )
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    else:
        result_queue.put(("ok", payload))


class PlanLockTest(unittest.TestCase):
    def make_artifacts(self, root: Path) -> None:
        (root / "output").mkdir()
        (root / "data").mkdir()
        with (root / "output" / "betting_plan_2026-07-16.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "match", "stake"])
            writer.writeheader()
            writer.writerow({"date": "2026-07-16", "match": "A vs B", "stake": 20})
        (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
            json.dumps({"001": {"had": {"h": "2.00"}}}), encoding="utf-8"
        )

    def make_lock(self, root: Path) -> dict:
        return lock_plan(
            root,
            TARGET_DATE,
            datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
            "sporttery",
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
                "sporttery",
            )
            self.assertIsNotNone(read_valid_lock(root, date(2026, 7, 16)))
            (root / "output" / "betting_plan_2026-07-16.csv").write_text(
                "changed", encoding="utf-8"
            )
            self.assertIsNone(read_valid_lock(root, date(2026, 7, 16)))

    def test_relocking_an_unchanged_plan_preserves_the_first_lock_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            first = lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
                "sporttery",
            )
            second = lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 14, 5, tzinfo=BJT),
                "sporttery",
            )
            self.assertEqual(first, second)
            self.assertEqual("2026-07-16T13:31:00+08:00", second["locked_at_bjt"])

    def test_read_valid_lock_rejects_unsupported_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            payload = self.make_lock(root)
            payload["schema_version"] = 2
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
            Path("data/sporttery_odds_2026-07-16.json"),
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
                    "sporttery",
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
                    "2026-07-16T14:05:00+08:00",
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
                "sporttery",
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
                "sporttery",
            )
            (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
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
                "--source",
                "sporttery",
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
                "--source",
                "sporttery",
            ]), patch.object(os, "getcwd", return_value=str(root)):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertNotEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
