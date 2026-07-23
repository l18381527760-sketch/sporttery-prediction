from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fixture_identity import fixture_match_ids
from import_sporttery import (
    APPROVED_IMPORT_SOURCES,
    import_manifest_path,
    read_valid_import_manifest,
)


BEIJING = timezone(timedelta(hours=8))
LEGACY_REQUESTED_PHASES = frozenset({"opening", "decision", "monitoring"})
LEGACY_MATCH_PHASES = LEGACY_REQUESTED_PHASES | {"pre_kickoff"}
_FILENAME = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?P<time>\d{6})-"
    r"(?P<phase>opening|decision|monitoring)\.json"
)


def read_valid_legacy_snapshot(
    root: Path,
    path: Path,
    target_date: date | None = None,
) -> tuple[dict, datetime]:
    root = Path(root).resolve()
    snapshot_path = Path(path).resolve()
    expected_dir = root / "data" / "odds_snapshots"
    if snapshot_path.parent != expected_dir:
        raise ValueError("legacy snapshot path is invalid")
    filename = _FILENAME.fullmatch(snapshot_path.name)
    if filename is None:
        raise ValueError("legacy snapshot filename is invalid")
    try:
        filename_date = date.fromisoformat(filename.group("date"))
    except ValueError as exc:
        raise ValueError("legacy snapshot filename date is invalid") from exc
    if filename_date.isoformat() != filename.group("date"):
        raise ValueError("legacy snapshot filename date is invalid")
    if target_date is not None and filename_date != target_date:
        raise ValueError("legacy snapshot date is invalid")

    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy snapshot is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("legacy snapshot schema is invalid")
    requested_phase = payload.get("capture_phase")
    if (
        payload.get("target_date") != filename_date.isoformat()
        or requested_phase != filename.group("phase")
        or requested_phase not in LEGACY_REQUESTED_PHASES
    ):
        raise ValueError("legacy snapshot metadata is invalid")
    captured = _aware_beijing(
        payload.get("captured_at"),
        "legacy snapshot captured_at",
    )
    if (
        captured.date() != filename_date
        or captured.strftime("%H%M%S") != filename.group("time")
    ):
        raise ValueError("legacy snapshot captured_at differs from filename")

    source = payload.get("source")
    if (
        not isinstance(source, str)
        or source not in APPROVED_IMPORT_SOURCES
    ):
        raise ValueError("legacy snapshot source is invalid")
    manifest = read_valid_import_manifest(root, filename_date)
    manifest_path = import_manifest_path(root, filename_date)
    if payload.get("import_manifest") != _file_record(root, manifest_path):
        raise ValueError("legacy snapshot import manifest proof is invalid")
    if manifest.get("source") != source:
        raise ValueError("legacy snapshot source differs from import manifest")
    imported_at = _aware_beijing(
        manifest.get("imported_at_bjt"),
        "import manifest imported_at_bjt",
    )
    if imported_at > captured:
        raise ValueError("import timestamp follows legacy snapshot capture")

    fixtures = fixture_match_ids(root, filename_date)
    canonical_bindings = {
        (*fixture_key, match_id)
        for fixture_key, match_ids in fixtures.items()
        for match_id in match_ids
    }
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        raise ValueError("legacy snapshot matches are invalid")
    seen_bindings = set()
    seen_match_ids = set()
    for row in matches:
        if not isinstance(row, dict):
            raise ValueError("legacy snapshot match is invalid")
        match_id = _canonical_text(row.get("match_id"), "legacy match_id")
        team_a = _canonical_text(row.get("team_a"), "legacy team_a")
        team_b = _canonical_text(row.get("team_b"), "legacy team_b")
        binding = (filename_date.isoformat(), team_a, team_b, match_id)
        if (
            binding not in canonical_bindings
            or binding in seen_bindings
            or match_id in seen_match_ids
        ):
            raise ValueError("legacy snapshot fixture binding is invalid")
        seen_bindings.add(binding)
        seen_match_ids.add(match_id)
        _validate_match_phase(row, requested_phase, captured)
    return payload, captured


def _validate_match_phase(
    row: dict,
    requested_phase: str,
    captured: datetime,
) -> None:
    phase = row.get("capture_phase")
    if phase not in LEGACY_MATCH_PHASES:
        raise ValueError("legacy snapshot match phase is invalid")
    if "minutes_to_kickoff" not in row:
        raise ValueError("legacy snapshot minutes to kickoff are missing")
    kickoff = _match_datetime(row.get("kickoff_at"))
    minutes = row.get("minutes_to_kickoff")
    if kickoff is None:
        if minutes is not None or phase != requested_phase:
            raise ValueError("legacy snapshot match phase shape is invalid")
        return
    if captured >= kickoff:
        raise ValueError("legacy snapshot match is not pre-kickoff")
    expected_minutes = int((kickoff - captured).total_seconds() // 60)
    if (
        type(minutes) is not int
        or minutes != expected_minutes
    ):
        raise ValueError("legacy snapshot minutes to kickoff are invalid")
    expected_phase = requested_phase
    if requested_phase == "monitoring" and expected_minutes <= 60:
        expected_phase = "pre_kickoff"
    if phase != expected_phase:
        raise ValueError("legacy snapshot match phase shape is invalid")


def _match_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def _aware_beijing(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(hours=8)
    ):
        raise ValueError(f"{name} must use the Beijing offset")
    return parsed.astimezone(BEIJING)


def _file_record(root: Path, path: Path) -> dict:
    payload = path.read_bytes()
    return {
        "path": path.resolve().relative_to(root).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _canonical_text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ValueError(f"{name} must be nonblank canonical text")
    return value
