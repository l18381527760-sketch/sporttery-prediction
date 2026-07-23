import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fixture_identity import fixture_identity_rate, fixture_match_ids
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
    try:
        confirmed, total = fixture_identity_rate(root, target_date)
    except ValueError:
        confirmed, total = 0, 0
    expected_match_ids = _unique_fixture_match_ids(root, target_date)

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

    decision_count = _decision_coverage_count(
        coverage,
        expected_match_ids,
    )
    if total and decision_count < total:
        decision_blockers.append("decision_snapshot_incomplete")
    decision_at = _aware(
        coverage.get("latest_by_requested_phase", {}).get("decision")
    )
    if total and decision_at is not None and decision_at > now_bjt:
        decision_blockers.append("decision_odds_from_future")
    if total and (
        decision_at is None
        or now_bjt - decision_at > timedelta(minutes=30)
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


def _unique_fixture_match_ids(
    root: Path,
    target_date: date,
) -> set[str] | None:
    try:
        identities = fixture_match_ids(root, target_date)
    except ValueError:
        return None
    return {
        next(iter(match_ids))
        for match_ids in identities.values()
        if len(match_ids) == 1
    }


def _decision_coverage_count(
    coverage: dict,
    expected_match_ids: set[str] | None,
) -> int:
    by_phase = coverage.get("match_ids_by_requested_phase")
    decision_ids = by_phase.get("decision") if isinstance(by_phase, dict) else None
    if isinstance(decision_ids, list) and all(
        isinstance(match_id, str) for match_id in decision_ids
    ):
        proven_ids = set(decision_ids)
        if expected_match_ids is not None:
            proven_ids &= expected_match_ids
        return len(proven_ids)
    requested = coverage.get("requested_phases")
    count = requested.get("decision", 0) if isinstance(requested, dict) else 0
    return count if type(count) is int and count >= 0 else 0


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
