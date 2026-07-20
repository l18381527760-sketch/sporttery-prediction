"""Publish transactional, date-scoped revalidation reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from PIL import Image

from build_daily_image import draw_report


BEIJING = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
_HEX_DIGITS = frozenset("0123456789abcdef")
_STATUS_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "report_date",
        "revision",
        "changed_at_bjt",
        "change_digest",
        "changed_candidates",
        "published_candidate_ids",
        "next_revalidation_at_bjt",
        "all_candidates_terminal",
        "report_image_url",
        "report_image_sha256",
        "source_commit_sha",
    }
)
_STATUS_OPTIONAL_FIELDS = frozenset({"notification_sent", "notifications_sent"})


def publish_revalidation_report(
    root: Path,
    report_date: date,
    changed_candidates: list[dict],
    generated_at: datetime,
    source_commit_sha: str,
) -> dict:
    """Reconcile one business date and publish image, status, then index."""
    root = Path(root).resolve()
    if type(report_date) is not date:
        raise ValueError("report_date must be a date")
    generated = _bjt_timestamp(generated_at, "generated_at")
    source_commit = str(source_commit_sha).strip()
    if not source_commit:
        raise ValueError("source_commit_sha must not be blank")
    input_changes = _canonicalize_input(changed_candidates)

    status_path = _status_path(root, report_date)
    date_lock = status_path.parent / ".publish.lock"
    with _exclusive_file_lock(date_lock):
        previous = _read_previous_status(root, status_path)
        candidates = _state_candidates(root, report_date)
        effective = _effective_candidates(root, candidates, input_changes)
        previously_published = set(previous.get("published_candidate_ids", []))
        reportable = [
            _canonical_candidate(candidate)
            for candidate in effective
            if _is_reportable(candidate)
            and str(candidate.get("candidate_id", "")).strip()
            not in previously_published
        ]
        reportable.sort(key=lambda candidate: candidate["candidate_id"])

        if reportable:
            status = _publish_revision(
                root,
                report_date,
                status_path,
                previous,
                candidates,
                reportable,
                generated,
                source_commit,
            )
        else:
            status = _publish_scheduler_status(
                root,
                report_date,
                status_path,
                previous,
                candidates,
                generated,
                source_commit,
            )

        with _exclusive_file_lock(_index_lock_path(root)):
            _build_revalidation_index_locked(root, generated)
        return _status_result(root, status_path, status)


def build_revalidation_index(root: Path, now: datetime) -> dict:
    """Atomically rebuild the two-date index from strictly verified statuses."""
    root = Path(root).resolve()
    generated = _bjt_timestamp(now, "now")
    with _exclusive_file_lock(_index_lock_path(root)):
        return _build_revalidation_index_locked(root, generated)


def _publish_revision(
    root: Path,
    report_date: date,
    status_path: Path,
    previous: dict,
    candidates: list[dict],
    reportable: list[dict],
    generated: datetime,
    source_commit: str,
) -> dict:
    digest = _sha256(_canonical_json(reportable))
    revision = int(previous.get("revision", 0)) + 1
    directory = status_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    image_name = f"revision-{revision}-{digest[:12]}.png"
    image_path = directory / image_name

    if image_path.exists():
        _validate_retry_image(image_path, report_date, digest)
    else:
        temporary_image = _temporary_path(directory, ".png")
        try:
            draw_report(
                output_path=temporary_image,
                report_date=report_date,
                revalidation_changes=reportable,
                change_digest=digest,
            )
            rendered = temporary_image.read_bytes()
            _write_create_only(image_path, rendered)
        finally:
            temporary_image.unlink(missing_ok=True)
    image_bytes = image_path.read_bytes()
    image_sha256 = _sha256(image_bytes)

    previously_published = set(previous.get("published_candidate_ids", []))
    status = {
        "schema_version": SCHEMA_VERSION,
        "report_date": report_date.isoformat(),
        "revision": revision,
        "changed_at_bjt": generated.isoformat(),
        "change_digest": digest,
        "changed_candidates": reportable,
        "published_candidate_ids": sorted(
            previously_published
            | {candidate["candidate_id"] for candidate in reportable}
        ),
        "next_revalidation_at_bjt": _next_due(candidates),
        "all_candidates_terminal": _all_terminal(candidates, reportable),
        "report_image_url": image_path.relative_to(root).as_posix(),
        "report_image_sha256": image_sha256,
        "source_commit_sha": source_commit,
    }
    _write_json_atomic(status_path, status)
    return _verify_persisted_status(root, status_path, status)


def _publish_scheduler_status(
    root: Path,
    report_date: date,
    status_path: Path,
    previous: dict,
    candidates: list[dict],
    generated: datetime,
    source_commit: str,
) -> dict:
    if previous and candidates:
        status = {
            **previous,
            "next_revalidation_at_bjt": _next_due(candidates),
            "all_candidates_terminal": _all_terminal(candidates, []),
        }
    elif previous:
        status = previous
    else:
        status = {
            "schema_version": SCHEMA_VERSION,
            "report_date": report_date.isoformat(),
            "revision": 0,
            "changed_at_bjt": generated.isoformat(),
            "change_digest": "",
            "changed_candidates": [],
            "published_candidate_ids": [],
            "next_revalidation_at_bjt": _next_due(candidates),
            "all_candidates_terminal": _all_terminal(candidates, []),
            "report_image_url": "",
            "report_image_sha256": "",
            "source_commit_sha": source_commit,
        }
    _write_json_atomic(status_path, status)
    return _verify_persisted_status(root, status_path, status)


def _build_revalidation_index_locked(root: Path, generated: datetime) -> dict:
    base = root / "web" / "revalidation"
    records = []
    if base.exists():
        for status_path in base.glob("????-??-??/status.json"):
            status = _validated_status_record(root, status_path)
            if status is None:
                continue
            if status["all_candidates_terminal"] and _notified(status):
                continue
            records.append((status["report_date"], status_path, status))
    records.sort(key=lambda item: item[0], reverse=True)
    records = sorted(records[:2], key=lambda item: item[0])
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_bjt": generated.isoformat(),
        "dates": [
            {
                "report_date": report_day,
                "status_url": status_path.relative_to(root).as_posix(),
                "status_sha256": _sha256(status_path.read_bytes()),
                "revision": status["revision"],
                "next_revalidation_at_bjt": status["next_revalidation_at_bjt"],
            }
            for report_day, status_path, status in records
        ],
    }
    _write_json_atomic(root / "web" / "revalidation-index.json", index)
    return index


def _canonicalize_input(changed_candidates: object) -> dict[str, dict]:
    if not isinstance(changed_candidates, list) or not all(
        isinstance(candidate, dict) for candidate in changed_candidates
    ):
        raise ValueError("changed_candidates must be a list of mappings")
    canonical: dict[str, dict] = {}
    for candidate in changed_candidates:
        item = _canonical_candidate(candidate)
        candidate_id = item["candidate_id"]
        if candidate_id in canonical and canonical[candidate_id] != item:
            raise ValueError(f"conflicting duplicate terminal event: {candidate_id}")
        canonical[candidate_id] = item
    return canonical


def _effective_candidates(
    root: Path,
    candidates: list[dict],
    input_changes: dict[str, dict],
) -> list[dict]:
    if not candidates:
        return list(input_changes.values())
    effective = []
    for entry in candidates:
        candidate = entry.get("candidate")
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        if not candidate_id:
            continue
        hint = input_changes.get(candidate_id, {})
        hint_receipt = hint.get("receipt") if isinstance(hint.get("receipt"), dict) else {}
        durable_receipt = _runtime_receipt(root, entry)
        merged = {
            **candidate,
            **hint,
            **hint_receipt,
            **durable_receipt,
            **entry,
            "candidate_id": candidate_id,
        }
        if "confirmed_stake" in entry:
            merged["final_stake"] = entry["confirmed_stake"]
        effective.append(merged)
    return effective


def _runtime_receipt(root: Path, entry: dict) -> dict:
    for field in ("t30_receipt_path", "t90_receipt_path"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        path = _contained_path(root, value)
        if path is None or not path.is_file():
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(receipt, dict):
            return receipt
    return {}


def _validate_retry_image(path: Path, report_date: date, digest: str) -> None:
    try:
        with Image.open(path) as image:
            metadata = dict(image.info)
            image.verify()
    except (OSError, ValueError) as exc:
        raise ValueError("immutable report image is invalid") from exc
    if (
        metadata.get("report_date") != report_date.isoformat()
        or metadata.get("change_digest") != digest
        or metadata.get("report_stage") != "revalidation"
    ):
        raise ValueError("immutable report image conflicts with retry")


def _is_reportable(candidate: dict) -> bool:
    state = str(candidate.get("state", "")).strip()
    return state == "cancelled" or (
        state == "confirmed" and candidate.get("ledger_status") == "ingested"
    )


def _canonical_candidate(candidate: dict) -> dict:
    candidate_id = str(candidate.get("candidate_id", "")).strip()
    if not candidate_id:
        nested = candidate.get("candidate")
        if isinstance(nested, dict):
            candidate_id = str(nested.get("candidate_id", "")).strip()
    if not candidate_id:
        raise ValueError("reportable candidate_id is required")
    value = {
        key: item
        for key, item in candidate.items()
        if key not in {"_runtime_state", "candidate"}
    }
    value["candidate_id"] = candidate_id
    return _json_value(value)


def _state_candidates(root: Path, report_date: date) -> list[dict]:
    path = root / "output" / f"revalidation_state_{report_date.isoformat()}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("durable revalidation state is invalid") from exc
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not all(
        isinstance(candidate, dict) for candidate in candidates
    ):
        raise ValueError("durable revalidation candidates are invalid")
    return candidates


def _all_terminal(candidates: list[dict], reportable: list[dict]) -> bool:
    if not candidates:
        return bool(reportable) and all(
            item.get("state") in {"confirmed", "cancelled"}
            for item in reportable
        )
    return all(
        candidate.get("state") in {"confirmed", "cancelled"}
        for candidate in candidates
    )


def _next_due(candidates: list[dict]) -> str:
    due = []
    for entry in candidates:
        if entry.get("state") in {"confirmed", "cancelled"}:
            continue
        explicit = str(entry.get("next_revalidation_at_bjt", "")).strip()
        if explicit:
            try:
                due.append(_bjt_timestamp(datetime.fromisoformat(explicit), "next due").isoformat())
            except ValueError:
                continue
            continue
        candidate = (
            entry.get("candidate")
            if isinstance(entry.get("candidate"), dict)
            else entry
        )
        kickoff = _candidate_kickoff(candidate)
        if kickoff is None:
            continue
        minutes = 105 if entry.get("state") == "provisional" else 40
        due.append(
            (kickoff - timedelta(minutes=minutes))
            .astimezone(BEIJING)
            .isoformat()
        )
    return min(due) if due else ""


def _candidate_kickoff(candidate: dict) -> datetime | None:
    values = [
        candidate.get("kickoff_at"),
        candidate.get("kickoff_at_bjt"),
        candidate.get("earliest_kickoff_at_bjt"),
    ]
    execution = candidate.get("execution_identity")
    execution_legs = execution.get("legs") if isinstance(execution, dict) else None
    legs = execution_legs if isinstance(execution_legs, list) else candidate.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if isinstance(leg, dict):
                values.extend((leg.get("kickoff_at"), leg.get("kickoff_at_bjt")))
    parsed = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is not None and moment.utcoffset() is not None:
            parsed.append(moment)
    return min(parsed) if parsed else None


def _read_previous_status(root: Path, path: Path) -> dict:
    if not path.exists():
        return {}
    status = _validated_status_record(root, path)
    if status is None:
        raise ValueError("existing revalidation status is invalid")
    return status


def _validated_status_record(root: Path, status_path: Path) -> dict | None:
    try:
        raw = status_path.read_bytes()
        status = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(status, dict):
        return None
    if raw != _canonical_json(status) + b"\n":
        return None
    if not _valid_status_schema(status_path, status):
        return None
    if status["revision"] == 0:
        return status

    image_path = _contained_image_path(root, status_path, status["report_image_url"])
    if image_path is None or not image_path.is_file():
        return None
    try:
        image_bytes = image_path.read_bytes()
    except OSError:
        return None
    if _sha256(image_bytes) != status["report_image_sha256"]:
        return None
    try:
        with Image.open(image_path) as image:
            if image.format != "PNG":
                return None
            metadata = dict(image.info)
            image.verify()
    except (OSError, ValueError):
        return None
    if (
        metadata.get("report_date") != status["report_date"]
        or metadata.get("change_digest") != status["change_digest"]
        or metadata.get("report_stage") != "revalidation"
    ):
        return None
    return status


def _valid_status_schema(status_path: Path, status: dict) -> bool:
    keys = frozenset(status)
    if not _STATUS_REQUIRED_FIELDS.issubset(keys):
        return False
    if keys - _STATUS_REQUIRED_FIELDS - _STATUS_OPTIONAL_FIELDS:
        return False
    if status.get("schema_version") != SCHEMA_VERSION:
        return False
    report_day = status_path.parent.name
    try:
        date.fromisoformat(report_day)
    except ValueError:
        return False
    if status.get("report_date") != report_day:
        return False
    revision = status.get("revision")
    if type(revision) is not int or revision < 0:
        return False
    if not _valid_aware_text(status.get("changed_at_bjt")):
        return False
    next_due = status.get("next_revalidation_at_bjt")
    if not isinstance(next_due, str) or (next_due and not _valid_aware_text(next_due)):
        return False
    if type(status.get("all_candidates_terminal")) is not bool:
        return False
    if not isinstance(status.get("source_commit_sha"), str) or not status["source_commit_sha"].strip():
        return False
    if any(
        field in status and type(status[field]) is not bool
        for field in _STATUS_OPTIONAL_FIELDS
    ):
        return False

    changed = status.get("changed_candidates")
    published = status.get("published_candidate_ids")
    if not isinstance(changed, list) or not all(isinstance(item, dict) for item in changed):
        return False
    if not isinstance(published, list) or not all(
        isinstance(candidate_id, str) and candidate_id.strip()
        for candidate_id in published
    ):
        return False
    changed_ids = [str(item.get("candidate_id", "")).strip() for item in changed]
    if (
        not all(changed_ids)
        or changed_ids != sorted(set(changed_ids))
        or published != sorted(set(published))
        or not set(changed_ids).issubset(published)
        or any(not _is_reportable(item) for item in changed)
    ):
        return False

    digest = status.get("change_digest")
    image_url = status.get("report_image_url")
    image_digest = status.get("report_image_sha256")
    if revision == 0:
        return (
            changed == []
            and published == []
            and digest == ""
            and image_url == ""
            and image_digest == ""
        )
    if not changed or not _valid_digest(digest) or not _valid_digest(image_digest):
        return False
    if digest != _sha256(_canonical_json(changed)):
        return False
    expected_name = f"revision-{revision}-{digest[:12]}.png"
    return isinstance(image_url, str) and PurePosixPath(image_url).name == expected_name


def _contained_image_path(
    root: Path, status_path: Path, image_url: object
) -> Path | None:
    if not isinstance(image_url, str) or not image_url:
        return None
    pure = PurePosixPath(image_url)
    parts = pure.parts
    expected = ("web", "revalidation", status_path.parent.name)
    if pure.is_absolute() or len(parts) != 4 or tuple(parts[:3]) != expected:
        return None
    if any(part in {"", ".", ".."} for part in parts):
        return None
    path = root.joinpath(*parts)
    try:
        path.resolve().relative_to(status_path.parent.resolve())
    except ValueError:
        return None
    return path


def _contained_path(root: Path, value: str) -> Path | None:
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    path = root.joinpath(*pure.parts)
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return None
    return path


def _verify_persisted_status(root: Path, path: Path, expected: dict) -> dict:
    persisted = _validated_status_record(root, path)
    if persisted is None or persisted != expected:
        raise ValueError("revalidation report verification failed")
    return persisted


def _notified(status: dict) -> bool:
    return bool(status.get("notification_sent") or status.get("notifications_sent"))


def _status_path(root: Path, report_date: date) -> Path:
    return root / "web" / "revalidation" / report_date.isoformat() / "status.json"


def _index_lock_path(root: Path) -> Path:
    return root / "web" / ".revalidation-index.lock"


def _status_result(root: Path, path: Path, status: dict) -> dict:
    return {
        **status,
        "status_url": path.relative_to(root).as_posix(),
        "status_sha256": _sha256(path.read_bytes()),
    }


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(value) + b"\n"
    temporary = _temporary_path(path.parent, ".json")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_create_only(path: Path, payload: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError as exc:
        raise ValueError("immutable report path already exists") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _temporary_path(directory: Path, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".revalidation-", suffix=suffix, dir=directory
    )
    os.close(descriptor)
    return Path(name)


def _bjt_timestamp(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(BEIJING)


def _valid_aware_text(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value).issubset(_HEX_DIGITS)
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_now_bjt(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("now_bjt must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("now_bjt must include a timezone")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Maintain revalidation report indexes.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    rebuild = subparsers.add_parser("rebuild-index")
    rebuild.add_argument("--now-bjt", required=True, type=_parse_now_bjt)
    args = parser.parse_args()
    try:
        build_revalidation_index(Path.cwd(), args.now_bjt)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
