import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fixture_identity import fixture_match_ids
from model_metrics import snapshot_coverage
from result_evidence import proven_90_minute_result, resolve_result_batch


BEIJING = timezone(timedelta(hours=8))


def build_evidence_health(
    root: Path,
    target_date: date,
    now: datetime,
    *,
    zero_fixture_verified: bool,
) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("evidence health now must include a timezone")
    now_bjt = now.astimezone(BEIJING)
    confirmed, total, expected_bindings = _fixture_evidence(
        root,
        target_date,
    )

    results = [
        row
        for row in _result_rows(root / "data" / "bet_results.csv")
        if row.get("date") == target_date.isoformat()
    ]
    result_total = _result_identity_count(results)
    resolved_results = resolve_result_batch(results)
    proven = sum(
        proven_90_minute_result(row)
        for row in resolved_results.values()
    )
    coverage = snapshot_coverage(
        root / "data" / "odds_snapshots",
        root / "data" / "live_odds_snapshots",
        target_date,
        not_after=now_bjt,
    )

    identity_rate = (
        confirmed / total
        if total
        else (1.0 if zero_fixture_verified else 0.0)
    )
    result_rate = proven / result_total if result_total else None
    forecast_blockers = []
    decision_blockers = []
    if identity_rate < 1.0:
        forecast_blockers.append("identity_not_unique")

    decision_bindings = _decision_coverage_bindings(coverage)
    decision_captures = _decision_capture_times(coverage)
    covered_bindings = (
        expected_bindings
        & decision_bindings
        & set(decision_captures)
    )
    decision_count = len(covered_bindings)
    if total and decision_count < total:
        decision_blockers.append("decision_snapshot_incomplete")
    capture_times = [
        decision_captures[binding]
        for binding in covered_bindings
    ]
    has_future = any(captured > now_bjt for captured in capture_times)
    if has_future:
        decision_blockers.append("decision_odds_from_future")
    if total and not has_future and (
        len(capture_times) < total
        or any(
            now_bjt - captured > timedelta(minutes=30)
            for captured in capture_times
        )
    ):
        decision_blockers.append("decision_odds_stale")
    hard_blockers = list(dict.fromkeys(
        forecast_blockers + decision_blockers
    ))
    return {
        "schema_version": 1,
        "target_date": target_date.isoformat(),
        "generated_at_bjt": now_bjt.isoformat(),
        "identity_confirmed": confirmed,
        "identity_total": total,
        "identity_confirmation_rate": identity_rate,
        "result_provenance_rate": result_rate,
        "snapshot_coverage": coverage,
        "forecast_blockers": forecast_blockers,
        "decision_blockers": decision_blockers,
        "hard_blockers": hard_blockers,
    }


def _result_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return []


def _result_identity_count(rows: list[dict]) -> int:
    match_ids = set()
    malformed = 0
    for row in rows:
        match_id = row.get("match_id")
        if isinstance(match_id, str) and match_id.strip():
            match_ids.add(match_id.strip())
        else:
            malformed += 1
    return len(match_ids) + malformed


def _fixture_evidence(
    root: Path,
    target_date: date,
) -> tuple[int, int, set[tuple[str, str, str, str]]]:
    try:
        identities = fixture_match_ids(root, target_date)
    except ValueError:
        return 0, 0, set()
    confirmed = sum(len(match_ids) == 1 for match_ids in identities.values())
    bindings = {
        (*fixture_key, next(iter(match_ids)))
        for fixture_key, match_ids in identities.items()
        if len(match_ids) == 1
    }
    return confirmed, len(identities), bindings


def _decision_coverage_bindings(
    coverage: dict,
) -> set[tuple[str, str, str, str]]:
    by_phase = coverage.get("bindings_by_requested_phase")
    raw_bindings = (
        by_phase.get("decision")
        if isinstance(by_phase, dict)
        else None
    )
    if not isinstance(raw_bindings, list):
        return set()
    proven_bindings = set()
    for raw in raw_bindings:
        if (
            isinstance(raw, list)
            and len(raw) == 4
            and all(
                isinstance(value, str)
                and value
                and value == value.strip()
                for value in raw
            )
        ):
            proven_bindings.add(tuple(raw))
    return proven_bindings


def _decision_capture_times(
    coverage: dict,
) -> dict[tuple[str, str, str, str], datetime]:
    by_phase = coverage.get("latest_by_binding_by_requested_phase")
    raw_records = (
        by_phase.get("decision")
        if isinstance(by_phase, dict)
        else None
    )
    if not isinstance(raw_records, list):
        return {}
    captures = {}
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        binding = raw.get("binding")
        captured = _aware(raw.get("captured_at"))
        if (
            not isinstance(binding, list)
            or len(binding) != 4
            or not all(
                isinstance(value, str)
                and value
                and value == value.strip()
                for value in binding
            )
            or captured is None
        ):
            continue
        canonical = tuple(binding)
        captures[canonical] = max(
            captures.get(canonical, captured),
            captured,
        )
    return captures


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(BEIJING)
