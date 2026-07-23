"""Capture and validate immutable live domestic odds snapshots."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from import_sporttery import (
    fetch_odds,
    fetch_selling_matches,
    fetch_zgzcw_matches,
    fetch_zgzcw_odds,
    read_valid_import_manifest,
    single_eligibility,
)


BEIJING = timezone(timedelta(hours=8))
LIVE_SCHEMA_V1 = 1
LIVE_SCHEMA_VERSION = 2
DOMESTIC_SOURCES = frozenset({"sporttery", "zgzcw"})
LIVE_PHASES = frozenset({
    "opening", "decision", "monitoring", "pre_kickoff_90", "pre_kickoff_30"
})
_SUPPORTED_MARKETS = ("had", "hhad", "ttg")


def capture_live_snapshot(
    root: Path,
    target_date: date,
    captured_at: datetime,
    preferred_source: str | None = None,
    sporttery_fetcher=None,
    sporttery_odds_fetcher=None,
    zgzcw_match_fetcher=None,
    zgzcw_odds_fetcher=None,
    *,
    phase: str = "monitoring",
) -> Path:
    root = Path(root).resolve()
    captured = _aware_datetime(captured_at, "captured_at").astimezone(BEIJING)
    phase = _capture_phase(phase)
    source = _preferred_source(preferred_source)
    sporttery_fetcher = sporttery_fetcher or fetch_selling_matches
    sporttery_odds_fetcher = sporttery_odds_fetcher or fetch_odds
    zgzcw_match_fetcher = zgzcw_match_fetcher or fetch_zgzcw_matches
    zgzcw_odds_fetcher = zgzcw_odds_fetcher or fetch_zgzcw_odds

    if source in (None, "sporttery"):
        try:
            matches = sporttery_fetcher(target_date)
        except Exception:
            if source == "sporttery":
                raise
        else:
            try:
                normalized, odds_by_source_id = _normalize_sporttery(
                    matches, sporttery_odds_fetcher, captured, phase
                )
            except ValueError:
                raise
            except Exception:
                if source == "sporttery":
                    raise
            else:
                return _publish(
                    root,
                    target_date,
                    captured,
                    "sporttery",
                    {"matches": matches, "odds": odds_by_source_id},
                    normalized,
                    phase,
                )

    matches = zgzcw_match_fetcher(target_date)
    odds_by_source_id = zgzcw_odds_fetcher(target_date)
    normalized = _normalize_zgzcw(
        root, target_date, matches, odds_by_source_id, captured, phase
    )
    return _publish(
        root, target_date, captured, "zgzcw", (matches, odds_by_source_id), normalized, phase
    )


def read_valid_live_snapshot(
    root: Path,
    path: Path,
    target_date: date,
    not_after: datetime | None = None,
) -> dict:
    root = Path(root).resolve()
    snapshot_path = Path(path)
    if not snapshot_path.is_absolute():
        snapshot_path = root / snapshot_path
    snapshot_path = snapshot_path.resolve()
    _require_within_root(root, snapshot_path)
    try:
        raw = snapshot_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("live snapshot is missing or invalid") from exc
    if _canonical_json_bytes(payload) != raw:
        raise ValueError("live snapshot is not canonical")
    captured = _validate_payload(payload, target_date, not_after)
    expected_dir = root / "data" / "live_odds_snapshots" / target_date.isoformat()
    expected_name = _filename(captured, payload["source"], raw)
    if snapshot_path.parent != expected_dir or snapshot_path.name != expected_name:
        raise ValueError("live snapshot path is invalid")
    return payload


def _preferred_source(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.lower() not in DOMESTIC_SOURCES:
        raise ValueError("preferred source is invalid")
    return value.lower()


def _capture_phase(value: object) -> str:
    if not isinstance(value, str) or value not in LIVE_PHASES:
        raise ValueError("live capture phase is invalid")
    return value


def _normalize_sporttery(
    matches: object, odds_fetcher, captured: datetime, phase: str
) -> tuple[list[dict], dict[str, dict]]:
    if not isinstance(matches, list):
        raise ValueError("Sporttery matches are invalid")
    normalized = []
    odds_by_source_id = {}
    for item in matches:
        identity = _source_identity(item, captured)
        if identity is None:
            continue
        source_record_id, match_num, team_a, team_b, kickoff, sales_state, eligibility = identity
        odds = odds_fetcher(source_record_id)
        markets = _normalize_markets(odds)
        odds_by_source_id[source_record_id] = odds
        normalized.append(_match_row(
            source_record_id,
            source_record_id,
            match_num,
            team_a,
            team_b,
            kickoff,
            sales_state,
            eligibility,
            markets,
            captured,
            phase,
        ))
    _require_unique_ids(normalized)
    return normalized, odds_by_source_id


def _normalize_zgzcw(
    root: Path,
    target_date: date,
    matches: object,
    odds_by_source_id: object,
    captured: datetime,
    phase: str,
) -> list[dict]:
    if not isinstance(matches, list) or not isinstance(odds_by_source_id, dict):
        raise ValueError("ZGZCW source response is invalid")
    fixtures = _manifest_fixture_identities(root, target_date)
    normalized = []
    for item in matches:
        identity = _source_identity(item, captured, assume_naive_beijing=True)
        if identity is None:
            continue
        source_record_id, match_num, team_a, team_b, kickoff, sales_state, eligibility = identity
        fixture_id = fixtures.get((match_num, team_a, team_b, kickoff.isoformat()))
        if fixture_id is None:
            raise ValueError("ZGZCW fallback fixture identity mismatch")
        if source_record_id not in odds_by_source_id:
            raise ValueError("ZGZCW fallback market mapping is missing")
        markets = _normalize_markets(odds_by_source_id[source_record_id])
        normalized.append(_match_row(
            fixture_id,
            source_record_id,
            match_num,
            team_a,
            team_b,
            kickoff,
            sales_state,
            eligibility,
            markets,
            captured,
            phase,
        ))
    _require_unique_ids(normalized)
    return normalized


def _source_identity(
    item: object, captured: datetime, *, assume_naive_beijing: bool = False
):
    if not isinstance(item, dict):
        raise ValueError("live source match is invalid")
    source_record_id = _required_text(item.get("matchId"), "source record ID")
    match_num = _required_text(item.get("matchNumStr"), "match number")
    team_a = _required_text(item.get("homeTeam"), "home team")
    team_b = _required_text(item.get("awayTeam"), "away team")
    sales_state = _required_text(item.get("matchStatus"), "market sales state")
    kickoff = (
        _beijing_datetime(item.get("kickoff_at"), "kickoff_at")
        if assume_naive_beijing
        else _aware_datetime(item.get("kickoff_at"), "kickoff_at").astimezone(BEIJING)
    )
    if kickoff <= captured:
        return None
    return (
        source_record_id,
        match_num,
        team_a,
        team_b,
        kickoff,
        sales_state,
        single_eligibility(item),
    )


def _normalize_markets(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("live source odds are invalid")
    markets = {}
    for name in _SUPPORTED_MARKETS:
        raw_market = value.get(name, {})
        if not isinstance(raw_market, dict):
            raise ValueError("live source market is invalid")
        market = {}
        for key, odd in raw_market.items():
            market[_required_text(key, "market selection")] = _required_text(odd, "market odd")
        markets[name] = market
    if not any(markets.values()):
        raise ValueError("live source response has no supported market")
    return markets


def _manifest_fixture_identities(root: Path, target_date: date) -> dict[tuple[str, str, str, str], str]:
    manifest = read_valid_import_manifest(root, target_date)
    try:
        fixture_path = root / manifest["fixtures"]["path"]
        with fixture_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (KeyError, OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("fallback fixture identities are invalid") from exc
    identities = {}
    for row in rows:
        if row.get("date") != target_date.isoformat():
            continue
        match_id = _required_text(row.get("match_id"), "fixture match_id")
        kickoff = _beijing_datetime(row.get("kickoff_at"), "fixture kickoff_at")
        key = (
            _required_text(row.get("match_num"), "fixture match number"),
            _required_text(row.get("team_a"), "fixture team_a"),
            _required_text(row.get("team_b"), "fixture team_b"),
            kickoff.isoformat(),
        )
        if key in identities or match_id in identities.values():
            raise ValueError("fallback fixture identity is ambiguous")
        identities[key] = match_id
    return identities


def _match_row(
    match_id: str,
    source_record_id: str,
    match_num: str,
    team_a: str,
    team_b: str,
    kickoff: datetime,
    sales_state: str,
    eligibility: dict,
    markets: dict,
    captured: datetime,
    requested_phase: str,
) -> dict:
    minutes_to_kickoff = _minutes_to_kickoff(kickoff, captured)
    return {
        "match_id": match_id,
        "source_record_id": source_record_id,
        "match_num": match_num,
        "team_a": team_a,
        "team_b": team_b,
        "kickoff_at": kickoff.isoformat(),
        "sales_state": sales_state,
        "single_eligibility": eligibility,
        "markets": markets,
        "capture_phase": _match_phase(requested_phase, minutes_to_kickoff),
        "minutes_to_kickoff": minutes_to_kickoff,
    }


def _minutes_to_kickoff(kickoff: datetime, captured: datetime) -> int:
    minutes = int(
        (kickoff.astimezone(BEIJING) - captured.astimezone(BEIJING)).total_seconds() // 60
    )
    if minutes < 0:
        raise ValueError("live snapshot kickoff is not future")
    return minutes


def _match_phase(requested: str, minutes: int) -> str:
    if minutes <= 45:
        return "pre_kickoff_30"
    if minutes <= 105:
        return "pre_kickoff_90"
    if requested in {"pre_kickoff_90", "pre_kickoff_30"}:
        return "monitoring"
    return requested


def _require_unique_ids(matches: list[dict]) -> None:
    match_ids = [row["match_id"] for row in matches]
    source_ids = [row["source_record_id"] for row in matches]
    if len(match_ids) != len(set(match_ids)) or len(source_ids) != len(set(source_ids)):
        raise ValueError("live match identities are duplicated")


def _publish(
    root: Path,
    target_date: date,
    captured: datetime,
    source: str,
    source_response: object,
    matches: list[dict],
    phase: str,
) -> Path:
    _require_requested_phase_evidence(phase, matches)
    payload = {
        "schema_version": LIVE_SCHEMA_VERSION,
        "target_date": target_date.isoformat(),
        "captured_at": captured.isoformat(),
        "source": source,
        "fetch_mode": "live",
        "capture_phase": phase,
        "source_response_sha256": _canonical_sha256(source_response),
        "matches": matches,
    }
    raw = _canonical_json_bytes(payload)
    path = root / "data" / "live_odds_snapshots" / target_date.isoformat() / _filename(captured, source, raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("conflicting live snapshot already exists")
    return path


def _require_requested_phase_evidence(
    phase: str,
    matches: list[dict],
) -> None:
    if (
        phase in {"pre_kickoff_90", "pre_kickoff_30"}
        and not any(row.get("capture_phase") == phase for row in matches)
    ):
        raise ValueError(
            "requested pre-kickoff phase is outside its timing window"
        )


def _filename(captured: datetime, source: str, raw: bytes) -> str:
    prefix = hashlib.sha256(raw).hexdigest()[:16]
    return f"{captured.astimezone(BEIJING).strftime('%Y%m%dT%H%M%S%z')}-{source}-{prefix}.json"


def _validate_payload(payload: object, target_date: date, not_after: datetime | None) -> datetime:
    if not isinstance(payload, dict):
        raise ValueError("live snapshot schema is invalid")
    if payload.get("schema_version") == LIVE_SCHEMA_V1:
        return _validate_v1_payload(payload, target_date, not_after)
    if payload.get("schema_version") == LIVE_SCHEMA_VERSION:
        return _validate_v2_payload(payload, target_date, not_after)
    raise ValueError("live snapshot schema is invalid")


def _validate_v1_payload(payload: dict, target_date: date, not_after: datetime | None) -> datetime:
    return _validate_common_payload(payload, target_date, not_after, phase=None)


def _validate_v2_payload(payload: dict, target_date: date, not_after: datetime | None) -> datetime:
    return _validate_common_payload(
        payload, target_date, not_after, phase=_capture_phase(payload.get("capture_phase"))
    )


def _validate_common_payload(
    payload: dict, target_date: date, not_after: datetime | None, phase: str | None
) -> datetime:
    if payload.get("target_date") != target_date.isoformat() or payload.get("fetch_mode") != "live":
        raise ValueError("live snapshot metadata is invalid")
    source = payload.get("source")
    if not isinstance(source, str) or source not in DOMESTIC_SOURCES:
        raise ValueError("live snapshot source is invalid")
    captured = _aware_datetime(payload.get("captured_at"), "live snapshot captured_at").astimezone(BEIJING)
    if not_after is not None and captured > _aware_datetime(not_after, "not_after").astimezone(BEIJING):
        raise ValueError("live snapshot was captured after the allowed time")
    digest = payload.get("source_response_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("live snapshot response digest is invalid")
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise ValueError("live snapshot matches are invalid")
    seen_match_ids = set()
    seen_source_ids = set()
    for row in matches:
        if not isinstance(row, dict):
            raise ValueError("live snapshot match is invalid")
        match_id = _required_text(row.get("match_id"), "live match_id")
        source_id = _required_text(row.get("source_record_id"), "live source record ID")
        if match_id in seen_match_ids or source_id in seen_source_ids:
            raise ValueError("live snapshot match identities are duplicated")
        seen_match_ids.add(match_id)
        seen_source_ids.add(source_id)
        _required_text(row.get("match_num"), "live match number")
        _required_text(row.get("team_a"), "live team_a")
        _required_text(row.get("team_b"), "live team_b")
        kickoff = _aware_datetime(row.get("kickoff_at"), "live kickoff_at").astimezone(BEIJING)
        if kickoff <= captured:
            raise ValueError("live snapshot kickoff is not future")
        if phase is not None:
            minutes_to_kickoff = row.get("minutes_to_kickoff")
            if (
                not isinstance(minutes_to_kickoff, int)
                or isinstance(minutes_to_kickoff, bool)
                or minutes_to_kickoff < 0
            ):
                raise ValueError("live snapshot minutes to kickoff is invalid")
            if minutes_to_kickoff != _minutes_to_kickoff(kickoff, captured):
                raise ValueError("live snapshot minutes to kickoff is invalid")
            if _capture_phase(row.get("capture_phase")) != _match_phase(phase, minutes_to_kickoff):
                raise ValueError("live snapshot match capture phase is invalid")
        _required_text(row.get("sales_state"), "live market sales state")
        markets = row.get("markets")
        if not isinstance(markets, dict) or set(markets) != set(_SUPPORTED_MARKETS):
            raise ValueError("live snapshot markets are invalid")
        if any(not isinstance(markets[name], dict) for name in _SUPPORTED_MARKETS) or not any(markets.values()):
            raise ValueError("live snapshot supported markets are invalid")
        for market in markets.values():
            for key, odd in market.items():
                _required_text(key, "live market selection")
                _required_text(odd, "live market odd")
        eligibility = row.get("single_eligibility")
        if not isinstance(eligibility, dict) or set(eligibility) != set(_SUPPORTED_MARKETS):
            raise ValueError("live snapshot eligibility is invalid")
        if any(not isinstance(eligibility[name], bool) for name in _SUPPORTED_MARKETS):
            raise ValueError("live snapshot eligibility is invalid")
    if phase is not None:
        _require_requested_phase_evidence(phase, matches)
    return captured


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty canonical text")
    return value


def _aware_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    else:
        raise ValueError(f"{name} must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _beijing_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    else:
        raise ValueError(f"{name} must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def _require_within_root(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("live snapshot path escapes repository root") from exc
