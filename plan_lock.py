import argparse
import errno
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from decision_bundle import read_valid_decision_bundle

if os.name == "nt":
    import msvcrt
else:
    import fcntl


BEIJING = timezone(timedelta(hours=8))
SCHEMA_VERSION = 2
CLAIM_WAIT_SECONDS = 5.0
CLAIM_POLL_SECONDS = 0.01


class PlanLockError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_paths(root: Path, target_date: date) -> tuple[Path, Path]:
    date_text = target_date.isoformat()
    return (
        root / "output" / f"betting_plan_{date_text}.csv",
        root / "output" / f"decision_bundle_{date_text}.json",
    )


def _lock_path(root: Path, target_date: date) -> Path:
    return root / "output" / f"plan_lock_{target_date.isoformat()}.json"


def _try_process_lock(handle) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        busy_errors = {errno.EACCES, errno.EAGAIN}
        if exc.errno in busy_errors:
            return False
        raise
    return True


def _release_process_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _process_lock(lock_path: Path):
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()

    acquired = False
    deadline = time.monotonic() + CLAIM_WAIT_SECONDS
    try:
        while not _try_process_lock(handle):
            if time.monotonic() >= deadline:
                raise PlanLockError(
                    f"timed out waiting for plan lock owner: {lock_path}"
                )
            time.sleep(CLAIM_POLL_SECONDS)
        acquired = True
        yield
    finally:
        if acquired:
            _release_process_lock(handle)
        handle.close()


def _existing_lock(root: Path, target_date: date, lock_path: Path) -> dict | None:
    if not lock_path.exists():
        return None
    payload = read_valid_lock(root, target_date)
    if payload is None:
        raise PlanLockError(
            f"existing plan lock is invalid; refusing to overwrite: {lock_path}"
        )
    return payload


def read_valid_lock(root: Path, target_date: date) -> dict | None:
    lock_path = _lock_path(root, target_date)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if payload.get("report_date") != target_date.isoformat():
        return None

    plan_path, bundle_path = _artifact_paths(root, target_date)
    expected_plan_path = f"output/betting_plan_{target_date.isoformat()}.csv"
    expected_bundle_path = f"output/decision_bundle_{target_date.isoformat()}.json"
    if payload.get("plan_path") != expected_plan_path:
        return None
    if payload.get("decision_bundle_path") != expected_bundle_path:
        return None

    plan_hash = payload.get("plan_sha256")
    bundle_hash = payload.get("decision_bundle_sha256")
    if not isinstance(plan_hash, str) or not plan_hash:
        return None
    if not isinstance(bundle_hash, str) or not bundle_hash:
        return None

    try:
        if (
            sha256_file(plan_path) != plan_hash
            or sha256_file(bundle_path) != bundle_hash
        ):
            return None
        bundle = read_valid_decision_bundle(
            root,
            target_date,
            expected_locked_at=_parse_aware_datetime(payload["locked_at_bjt"]),
        )
        plan_evidence = bundle.get("paid_plan_evidence")
        if (
            not isinstance(plan_evidence, dict)
            or payload.get("paid_plan_evidence_sha256")
            != plan_evidence.get("rows_sha256")
            or plan_hash != plan_evidence.get("plan_sha256")
            or plan_path.stat().st_size != plan_evidence.get("bytes")
        ):
            return None
        snapshot = bundle["decision_snapshot"]
        if (
            payload.get("odds_path") != snapshot.get("path")
            or payload.get("odds_sha256") != snapshot.get("sha256")
            or payload.get("odds_source") != snapshot.get("source")
        ):
            return None
        return payload
    except (KeyError, OSError, ValueError):
        return None


def lock_plan(root: Path, target_date: date, locked_at: datetime) -> dict:
    if locked_at.tzinfo is None or locked_at.utcoffset() is None:
        raise ValueError("locked_at must include a timezone")

    lock_path = _lock_path(root, target_date)
    existing = _existing_lock(root, target_date, lock_path)
    if existing is not None:
        return existing

    bundle = read_valid_decision_bundle(
        root,
        target_date,
        expected_locked_at=locked_at,
        verify_current_inputs=True,
    )
    snapshot = bundle["decision_snapshot"]
    source = snapshot["source"]
    plan_path, bundle_path = _artifact_paths(root, target_date)
    plan_evidence = bundle.get("paid_plan_evidence")
    if (
        not isinstance(plan_evidence, dict)
        or sha256_file(plan_path) != plan_evidence.get("plan_sha256")
        or plan_path.stat().st_size != plan_evidence.get("bytes")
    ):
        raise ValueError("paid plan differs from immutable decision evidence")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report_date": target_date.isoformat(),
        "locked_at_bjt": locked_at.astimezone(BEIJING).isoformat(),
        "plan_path": f"output/betting_plan_{target_date.isoformat()}.csv",
        "plan_sha256": sha256_file(plan_path),
        "decision_bundle_path": f"output/decision_bundle_{target_date.isoformat()}.json",
        "decision_bundle_sha256": sha256_file(bundle_path),
        "paid_plan_evidence_sha256": plan_evidence["rows_sha256"],
        "odds_path": snapshot["path"],
        "odds_sha256": snapshot["sha256"],
        "odds_source": source,
    }

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = lock_path.with_name(lock_path.name + ".tmp")
    process_lock_path = lock_path.with_name(lock_path.name + ".lock")
    with _process_lock(process_lock_path):
        existing = _existing_lock(root, target_date, lock_path)
        if existing is not None:
            return existing

        handle = temp_path.open("w", encoding="utf-8")
        published = False
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()

            existing = _existing_lock(root, target_date, lock_path)
            if existing is not None:
                return existing
            temp_path.replace(lock_path)
            published = True
            return payload
        finally:
            if not handle.closed:
                handle.close()
            if not published:
                temp_path.unlink(missing_ok=True)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("locked-at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("locked-at must include a timezone")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    is_locked = subparsers.add_parser("is-locked")
    is_locked.add_argument("--date", required=True, type=_parse_date)

    lock = subparsers.add_parser("lock")
    lock.add_argument("--date", required=True, type=_parse_date)
    lock.add_argument("--locked-at", required=True, type=_parse_aware_datetime)

    args = parser.parse_args()
    root = Path.cwd()
    if args.command == "is-locked":
        return 0 if read_valid_lock(root, args.date) is not None else 1

    try:
        lock_plan(root, args.date, args.locked_at)
    except (OSError, ValueError, PlanLockError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
