"""Publish immutable provisional betting candidates without ledger exposure."""

import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from decision_bundle import read_valid_decision_bundle
from generate_betting_plan import strategy_outputs_from_bundle
from official_markets import parse_handicap


PROVISIONAL_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 2
GENERATION_POINTER_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
MIN_INITIAL_MINUTES = 60
T90_EARLY_MINUTES = 105
BEIJING = timezone(timedelta(hours=8))
DOMESTIC_SOURCES = frozenset({"sporttery", "zgzcw"})

_CSV_FIELDS = (
    "candidate_id", "candidate_payload_sha256", "report_date", "route",
    "strategy_version", "provisional_rank", "state", "earliest_kickoff_at_bjt",
    "odds", "provisional_stake", "confirmed_stake", "conservative_probability",
    "minimum_ev", "minimum_acceptable_odds",
    "initial_candidate_attestation_sha256", "t90_receipt_path",
    "t90_receipt_sha256", "candidate_payload_json",
)
_ARTIFACT_FILENAMES = {
    "active_plan": "provisional_betting_plan_{date}.csv",
    "shadow_plan": "provisional_shadow_plan_{date}.csv",
    "state": "revalidation_state_{date}.json",
}


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


class _ProvenanceDerivedCandidate(dict):
    """A V1 payload plus identity derived only after immutable validation."""

    def __init__(self, published: dict, execution_identity: dict) -> None:
        published_copy = json.loads(_canonical_bytes(published).decode("utf-8"))
        identity_copy = json.loads(
            _canonical_bytes(execution_identity).decode("utf-8")
        )
        super().__init__(published_copy)
        self["execution_identity"] = identity_copy
        self._published = published_copy
        self._execution_identity = json.loads(
            _canonical_bytes(identity_copy).decode("utf-8")
        )


def _legacy_published_candidate(candidate: object) -> dict | None:
    if not isinstance(candidate, _ProvenanceDerivedCandidate):
        return None
    runtime = {
        key: value
        for key, value in candidate.items()
        if key not in {"execution_identity", "_runtime_state"}
    }
    if (
        runtime != candidate._published
        or candidate.get("execution_identity") != candidate._execution_identity
    ):
        return None
    return candidate._published


def _copy_validated_candidate(candidate: dict) -> dict:
    published = _legacy_published_candidate(candidate)
    if published is None:
        return dict(candidate)
    return _ProvenanceDerivedCandidate(
        published, candidate["execution_identity"]
    )


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_create_only(
    path: Path,
    content: bytes,
    *,
    conflict_message: str = "conflicting provisional artifact",
) -> None:
    """Atomically create a file, or prove the existing bytes are identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(conflict_message)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except OSError:
            if not path.exists():
                raise
            if path.read_bytes() != content:
                raise ValueError(conflict_message)
    finally:
        if temporary.exists():
            temporary.unlink()


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _integral_stake(value: object, name: str = "provisional stake") -> int:
    amount = _decimal(value, name)
    if amount < 0 or amount != amount.to_integral_value():
        raise ValueError(f"{name} must be a nonnegative integral amount")
    return int(amount)


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an aware ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{name} must be an aware ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be an aware ISO-8601 datetime")
    return parsed


def _bjt(value: object, name: str) -> str:
    return _aware(value, name).astimezone(BEIJING).isoformat()


def _normalized_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _normalized_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalized_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _normalize_bjt_fields(value: object) -> object:
    if isinstance(value, dict):
        normalized = {}
        for key in sorted(value):
            item = value[key]
            if str(key).endswith("_bjt") and item not in {None, ""}:
                normalized[str(key)] = _bjt(item, str(key))
            else:
                normalized[str(key)] = _normalize_bjt_fields(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_bjt_fields(item) for item in value]
    return _normalized_value(value)


def _validate_bjt_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_bjt") and item not in {None, ""}:
                if not isinstance(item, str) or _bjt(item, key) != item:
                    raise ValueError(f"{key} is not normalized to Asia/Shanghai")
            else:
                _validate_bjt_fields(item)
    elif isinstance(value, list):
        for item in value:
            _validate_bjt_fields(item)


def _parse_legs(row: dict) -> list[dict]:
    raw = row.get("legs_json", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as exc:
            raise ValueError("legs_json must be valid JSON") from exc
    if not isinstance(raw, list):
        raise ValueError("legs_json must be a list")
    legs = []
    for raw_leg in raw:
        if not isinstance(raw_leg, dict):
            raise ValueError("parlay leg must be a mapping")
        kickoff = raw_leg.get("kickoff_at") or raw_leg.get("kickoff_local")
        leg = {
            "match_id": str(raw_leg.get("match_id") or "").strip(),
            "market_type": str(raw_leg.get("market_type") or "").strip().lower(),
            "market_line": str(
                raw_leg.get("market_line", raw_leg.get("line", "")) or ""
            ).strip(),
            "selection": str(raw_leg.get("selection") or "").strip(),
        }
        if kickoff not in {None, ""}:
            leg["kickoff_at_bjt"] = _bjt(kickoff, "parlay leg kickoff")
        legs.append(leg)
    if any(
        not leg["match_id"] or not leg["market_type"] or not leg["selection"]
        for leg in legs
    ):
        raise ValueError("parlay leg identity is incomplete")
    return sorted(legs, key=_canonical_bytes)


def _market_identity(row: dict) -> tuple[dict, list[dict], datetime]:
    market_type = str(row.get("market_type") or "").strip().lower()
    if not market_type:
        raise ValueError("market_type is required")
    if market_type == "parlay":
        legs = _parse_legs(row)
        if len(legs) < 2:
            raise ValueError("parlay requires at least two legs")
        leg_kickoffs = [
            _aware(leg["kickoff_at_bjt"], "parlay leg kickoff")
            for leg in legs
            if leg.get("kickoff_at_bjt")
        ]
        earliest = min(leg_kickoffs) if leg_kickoffs else _aware(
            row.get("kickoff_local") or row.get("kickoff_at"), "kickoff"
        )
        return {"market_type": "parlay", "legs": legs}, legs, earliest
    kickoff = _aware(row.get("kickoff_local") or row.get("kickoff_at"), "kickoff")
    identity = {
        "match_id": str(row.get("match_id") or "").strip(),
        "market_type": market_type,
        "market_line": str(row.get("market_line") or "").strip(),
        "selection": str(row.get("selection") or "").strip(),
    }
    if not identity["match_id"] or not identity["selection"]:
        raise ValueError("market identity is incomplete")
    return identity, [], kickoff


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty canonical text")
    return value


def _canonical_market_line(market_type: str, value: object, name: str) -> str | None:
    if market_type == "hhad":
        try:
            return str(parse_handicap(value))
        except ValueError as exc:
            raise ValueError(f"{name} is invalid") from exc
    if value not in {None, ""}:
        raise ValueError(f"{name} must be null outside HHAD")
    return None


def _candidate_legs(candidate: dict) -> list[dict]:
    identity = candidate.get("normalized_market_identity")
    if not isinstance(identity, dict):
        raise ValueError("candidate normalized market identity is invalid")
    if identity.get("market_type") == "parlay":
        legs = identity.get("legs")
    else:
        legs = [identity]
    if not isinstance(legs, list) or not legs:
        raise ValueError("candidate normalized legs are invalid")
    return legs


def _derive_execution_identity(candidate: dict, decision_snapshot: dict) -> dict:
    if not isinstance(decision_snapshot, dict):
        raise ValueError("decision snapshot record is invalid")
    snapshot = decision_snapshot.get("payload")
    if not isinstance(snapshot, dict):
        raise ValueError("decision snapshot payload is invalid")
    source = _required_text(snapshot.get("source"), "decision snapshot source").lower()
    if source not in DOMESTIC_SOURCES:
        raise ValueError("decision snapshot source is not domestic")
    captured_at_bjt = _bjt(snapshot.get("captured_at"), "decision snapshot captured_at")
    matches = snapshot.get("matches")
    if not isinstance(matches, list) or any(not isinstance(row, dict) for row in matches):
        raise ValueError("decision snapshot matches are invalid")
    by_id = {row.get("match_id"): row for row in matches}
    if len(by_id) != len(matches):
        raise ValueError("decision snapshot match identities are duplicated")

    bound = []
    for identity in _candidate_legs(candidate):
        if not isinstance(identity, dict):
            raise ValueError("candidate normalized leg is invalid")
        match_id = _required_text(identity.get("match_id"), "candidate match_id")
        market_type = _required_text(
            identity.get("market_type"), "candidate market_type"
        ).lower()
        selection = _required_text(identity.get("selection"), "candidate selection")
        if market_type not in {"had", "hhad", "ttg"}:
            raise ValueError("candidate market type is invalid")
        match = by_id.get(match_id)
        if not isinstance(match, dict):
            raise ValueError("candidate match is absent from decision snapshot")
        markets = match.get("markets")
        market = markets.get(market_type) if isinstance(markets, dict) else None
        if not isinstance(market, dict) or selection not in market:
            raise ValueError("candidate market is absent from decision snapshot")
        snapshot_line = _canonical_market_line(
            market_type, market.get("goalLine"), "decision snapshot market line"
        )
        candidate_line = _canonical_market_line(
            market_type, identity.get("market_line"), "candidate market line"
        )
        if snapshot_line != candidate_line:
            raise ValueError("candidate market line differs from decision snapshot")
        eligibility = match.get("single_eligibility")
        single_eligible = (
            eligibility.get(market_type) if isinstance(eligibility, dict) else None
        )
        if single_eligible is not True:
            raise ValueError("candidate is not single eligible in decision snapshot")
        sales_state = _required_text(
            match.get("sales_state"), "decision snapshot sales state"
        )
        if sales_state != "Selling":
            raise ValueError("candidate market is not selling in decision snapshot")
        bound.append({
            "source": source,
            "source_record_id": _required_text(
                match.get("source_record_id"), "decision snapshot source record ID"
            ),
            "match_id": match_id,
            "match_num": _required_text(
                match.get("match_num"), "decision snapshot match_num"
            ),
            "team_a": _required_text(match.get("team_a"), "decision snapshot team_a"),
            "team_b": _required_text(match.get("team_b"), "decision snapshot team_b"),
            "kickoff_at_bjt": _bjt(
                match.get("kickoff_at"), "decision snapshot kickoff_at"
            ),
            "market_type": market_type,
            "market_line": snapshot_line,
            "selection": selection,
            "sales_state": sales_state,
            "single_eligible": single_eligible,
        })
    return {
        "decision_snapshot_sha256": _sha256(decision_snapshot),
        "decision_snapshot_captured_at_bjt": captured_at_bjt,
        "legs": bound,
    }


def _bind_execution_identity(candidate: dict, decision_snapshot: dict) -> None:
    execution_identity = _derive_execution_identity(candidate, decision_snapshot)
    candidate["execution_identity"] = execution_identity
    candidate["earliest_kickoff_at_bjt"] = min(
        _aware(leg["kickoff_at_bjt"], "execution identity kickoff")
        for leg in execution_identity["legs"]
    ).isoformat()
    _attest(candidate)


def _attest(candidate: dict) -> dict:
    payload = {
        key: value
        for key, value in candidate.items()
        if key != "candidate_payload_sha256"
    }
    candidate["candidate_payload_sha256"] = _sha256(payload)
    return candidate


def candidate_from_plan_row(
    row: dict, route: str, report_date: date, provisional_rank: int
) -> dict:
    """Normalize one deterministic plan row into a route-specific candidate."""
    if not isinstance(row, dict):
        raise ValueError("plan row must be a mapping")
    if route not in {"active", "shadow"}:
        raise ValueError("route must be active or shadow")
    if not isinstance(report_date, date):
        raise ValueError("report_date must be a date")
    identity, legs, earliest = _market_identity(row)
    strategy_version = str(row.get("strategy_version") or "").strip()
    if not strategy_version:
        raise ValueError("strategy_version is required")
    odds = _decimal(row.get("odds"), "odds")
    stake = _integral_stake(row.get("stake"))
    probability = _decimal(
        row.get("conservative_probability"), "conservative_probability"
    )
    minimum_ev = _decimal(
        row.get("minimum_ev", row.get("min_ev", 0)), "minimum_ev"
    )
    if odds <= 1 or not Decimal("0") < probability < Decimal("1"):
        raise ValueError("candidate odds or probability is invalid")
    stable_identity = {
        "report_date": report_date.isoformat(),
        "route": route,
        "strategy_version": strategy_version,
        "market": identity,
    }
    candidate = {
        "schema_version": PROVISIONAL_SCHEMA_VERSION,
        "candidate_id": "candidate-" + _sha256(stable_identity),
        "report_date": report_date.isoformat(),
        "route": route,
        "strategy_version": strategy_version,
        "provisional_rank": int(provisional_rank),
        "normalized_market_identity": identity,
        "legs": legs,
        "earliest_kickoff_at_bjt": _bjt(earliest, "earliest kickoff"),
        "odds": format(odds, "f"),
        "provisional_stake": stake,
        "confirmed_stake": 0,
        "conservative_probability": format(probability, "f"),
        "minimum_ev": format(minimum_ev, "f"),
        "minimum_acceptable_odds": format(
            (Decimal("1") + minimum_ev) / probability, ".6f"
        ),
        "source_plan_row": _normalize_bjt_fields(row),
        "state": "provisional",
        "initial_candidate_attestation_sha256": "",
        "t90_receipt_path": "",
        "t90_receipt_sha256": "",
    }
    return _attest(candidate)


def _minimum_ev(bundle: dict, outputs: object) -> Decimal:
    value = bundle["configuration"]["betting"]["payload"].get("value_strategy", {})
    strict = bool(getattr(outputs, "audit", {}).get("risk_caps", {}).get("strict_mode"))
    return _decimal(
        value.get("strict_min_ev" if strict else "min_ev", 0), "minimum_ev"
    )


def _initial_candidate_binding(candidate: dict, decision_snapshot: dict) -> dict:
    initial_candidate = {
        key: value
        for key, value in candidate.items()
        if key not in {
            "candidate_payload_sha256",
            "initial_candidate_attestation_sha256",
            "t90_receipt_path",
            "t90_receipt_sha256",
        }
    }
    return {
        "candidate_id": candidate["candidate_id"],
        "initial_candidate": initial_candidate,
        "decision_snapshot_sha256": _sha256(decision_snapshot),
    }


def _assign_initial_state(
    candidate: dict,
    generated_at: datetime,
    decision_snapshot: dict,
) -> None:
    minutes = (
        _aware(candidate["earliest_kickoff_at_bjt"], "kickoff") - generated_at
    ).total_seconds() / 60
    if minutes < MIN_INITIAL_MINUTES:
        raise ValueError("candidate is less than 60 minutes from earliest kickoff")
    if minutes <= T90_EARLY_MINUTES:
        candidate["state"] = "screened"
    candidate["initial_candidate_attestation_sha256"] = _sha256(
        _initial_candidate_binding(candidate, decision_snapshot)
    )


def _receipt_bytes(
    candidate: dict,
    generated_at_bjt: str,
    decision_snapshot: dict,
) -> bytes:
    receipt = {
        "schema_version": PROVISIONAL_SCHEMA_VERSION,
        "receipt_type": "t90_initial_snapshot",
        "candidate_id": candidate["candidate_id"],
        "initial_candidate_attestation_sha256": candidate[
            "initial_candidate_attestation_sha256"
        ],
        "generated_at_bjt": generated_at_bjt,
        "decision_snapshot_sha256": _sha256(decision_snapshot),
        "decision_snapshot": decision_snapshot,
    }
    return _canonical_bytes(receipt) + b"\n"


def _candidate_csv_row(candidate: dict) -> dict:
    row = {}
    for key in _CSV_FIELDS:
        if key == "candidate_payload_json":
            row[key] = _canonical_bytes(candidate).decode("utf-8")
        else:
            value = candidate.get(key, "")
            row[key] = "" if value is None else str(value)
    return row


def _csv_bytes(candidates: list[dict]) -> bytes:
    with io.StringIO(newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(_candidate_csv_row(candidate))
        return b"\xef\xbb\xbf" + handle.getvalue().encode("utf-8")


def _artifact_record(root: Path, path: Path, content: bytes) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _bytes_sha256(content),
        "bytes": len(content),
    }


def _pointer_path(root: Path, target_date: date) -> Path:
    return root / "output" / f"provisional_generation_{target_date.isoformat()}.json"


def _build_generation(
    root: Path,
    target_date: date,
    generated_at: datetime,
    decision_bundle: dict,
    outputs: object,
) -> tuple[dict, bytes, dict[str, tuple[Path, bytes]]]:
    generated_at_bjt = _bjt(generated_at, "generated_at")
    generated = _aware(generated_at_bjt, "generated_at")
    decision_snapshot = decision_bundle["decision_snapshot"]
    minimum_ev = _minimum_ev(decision_bundle, outputs)
    candidates_by_route = {}
    identities = set()
    for route, rows in (("active", outputs.active_plan), ("shadow", outputs.shadow_plan)):
        candidates = []
        for rank, original in enumerate(rows, start=1):
            row = dict(original)
            row.setdefault("minimum_ev", format(minimum_ev, "f"))
            candidate = candidate_from_plan_row(row, route, target_date, rank)
            _bind_execution_identity(candidate, decision_snapshot)
            if candidate["candidate_id"] in identities:
                raise ValueError("duplicate normalized candidate identity")
            identities.add(candidate["candidate_id"])
            _assign_initial_state(candidate, generated, decision_snapshot)
            candidates.append(candidate)
        candidates_by_route[route] = candidates

    bundle_sha256 = _sha256(decision_bundle)
    generation_id = _sha256({
        "report_date": target_date.isoformat(),
        "generated_at_bjt": generated_at_bjt,
        "decision_bundle_sha256": bundle_sha256,
        "initial_candidate_attestations": [
            candidate["initial_candidate_attestation_sha256"]
            for route in ("active", "shadow")
            for candidate in candidates_by_route[route]
        ],
    })
    generation_dir = (
        root / "output" / "provisional_generations" / target_date.isoformat()
        / generation_id
    )
    for route in ("active", "shadow"):
        for candidate in candidates_by_route[route]:
            if candidate["state"] == "screened":
                receipt_path = (
                    generation_dir / "receipts"
                    / f"{candidate['candidate_id']}-t90.json"
                )
                serialized = _receipt_bytes(
                    candidate, generated_at_bjt, decision_snapshot
                )
                _write_create_only(receipt_path, serialized)
                candidate["t90_receipt_path"] = (
                    receipt_path.relative_to(root).as_posix()
                )
                candidate["t90_receipt_sha256"] = _bytes_sha256(serialized)
            _attest(candidate)

    active_bytes = _csv_bytes(candidates_by_route["active"])
    shadow_bytes = _csv_bytes(candidates_by_route["shadow"])
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "report_date": target_date.isoformat(),
        "generation_id": generation_id,
        "generated_at_bjt": generated_at_bjt,
        "decision_bundle_path": (
            f"output/decision_bundle_{target_date.isoformat()}.json"
        ),
        "decision_bundle_sha256": bundle_sha256,
        "provisional_plan_sha256": _bytes_sha256(active_bytes),
        "provisional_shadow_plan_sha256": _bytes_sha256(shadow_bytes),
        "active_candidate_count": len(candidates_by_route["active"]),
        "shadow_candidate_count": len(candidates_by_route["shadow"]),
        "active_provisional_stake": sum(
            candidate["provisional_stake"]
            for candidate in candidates_by_route["active"]
        ),
        "candidates": [
            *candidates_by_route["active"], *candidates_by_route["shadow"]
        ],
    }
    state_bytes = _canonical_bytes(state) + b"\n"
    artifacts = {
        "active_plan": (
            generation_dir
            / _ARTIFACT_FILENAMES["active_plan"].format(date=target_date),
            active_bytes,
        ),
        "shadow_plan": (
            generation_dir
            / _ARTIFACT_FILENAMES["shadow_plan"].format(date=target_date),
            shadow_bytes,
        ),
        "state": (
            generation_dir / _ARTIFACT_FILENAMES["state"].format(date=target_date),
            state_bytes,
        ),
    }
    pointer = {
        "schema_version": GENERATION_POINTER_SCHEMA_VERSION,
        "report_date": target_date.isoformat(),
        "generation_id": generation_id,
        "decision_bundle_path": state["decision_bundle_path"],
        "decision_bundle_sha256": bundle_sha256,
        "artifacts": {
            key: _artifact_record(root, path, content)
            for key, (path, content) in artifacts.items()
        },
    }
    return pointer, _canonical_bytes(pointer) + b"\n", artifacts


def _read_json_bytes(content: bytes, name: str) -> dict:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be a mapping")
    return payload


def _expected_artifact_path(
    root: Path, target_date: date, generation_id: str, key: str
) -> Path:
    return (
        root / "output" / "provisional_generations" / target_date.isoformat()
        / generation_id / _ARTIFACT_FILENAMES[key].format(date=target_date)
    )


def _validate_artifact_record(
    root: Path,
    target_date: date,
    generation_id: str,
    key: str,
    record: object,
) -> Path:
    if not isinstance(record, dict):
        raise ValueError("provisional generation artifact record is invalid")
    expected = _expected_artifact_path(root, target_date, generation_id, key)
    if record.get("path") != expected.relative_to(root).as_posix():
        raise ValueError("provisional generation artifact path is invalid")
    if not expected.is_file():
        raise ValueError("provisional generation artifact is missing")
    content = expected.read_bytes()
    if (
        record.get("sha256") != _bytes_sha256(content)
        or record.get("bytes") != len(content)
    ):
        raise ValueError("provisional generation artifact digest is invalid")
    return expected


def _validate_candidate(
    candidate: object,
    target_date: date,
    decision_snapshot: dict,
    schema_version: int,
) -> dict:
    if not isinstance(candidate, dict):
        raise ValueError("candidate payload must be a mapping")
    expected_digest = _sha256({
        key: value
        for key, value in candidate.items()
        if key != "candidate_payload_sha256"
    })
    if candidate.get("candidate_payload_sha256") != expected_digest:
        raise ValueError("candidate payload digest is invalid")
    route = candidate.get("route")
    if route not in {"active", "shadow"}:
        raise ValueError("candidate route is invalid")
    if candidate.get("report_date") != target_date.isoformat():
        raise ValueError("candidate report date is invalid")
    identity = candidate.get("normalized_market_identity")
    strategy_version = candidate.get("strategy_version")
    if not isinstance(identity, dict) or not isinstance(strategy_version, str) or not strategy_version:
        raise ValueError("candidate identity is invalid")
    stable_identity = {
        "report_date": target_date.isoformat(),
        "route": route,
        "strategy_version": strategy_version,
        "market": identity,
    }
    if candidate.get("candidate_id") != "candidate-" + _sha256(stable_identity):
        raise ValueError("candidate identity digest is invalid")
    if (
        not isinstance(candidate.get("provisional_rank"), int)
        or isinstance(candidate.get("provisional_rank"), bool)
        or candidate["provisional_rank"] < 1
    ):
        raise ValueError("candidate provisional rank is invalid")
    if (
        not isinstance(candidate.get("provisional_stake"), int)
        or isinstance(candidate.get("provisional_stake"), bool)
        or candidate["provisional_stake"] < 0
    ):
        raise ValueError("provisional stake must be a nonnegative integral amount")
    if candidate.get("confirmed_stake") != 0 or isinstance(
        candidate.get("confirmed_stake"), bool
    ):
        raise ValueError("provisional candidate has confirmed stake")
    probability = _decimal(
        candidate.get("conservative_probability"), "conservative_probability"
    )
    minimum_ev = _decimal(candidate.get("minimum_ev"), "minimum_ev")
    if not Decimal("0") < probability < Decimal("1"):
        raise ValueError("candidate conservative probability is invalid")
    expected_minimum_odds = format(
        (Decimal("1") + minimum_ev) / probability, ".6f"
    )
    if candidate.get("minimum_acceptable_odds") != expected_minimum_odds:
        raise ValueError("candidate minimum acceptable odds is invalid")
    if _decimal(candidate.get("odds"), "odds") <= 1:
        raise ValueError("candidate odds are invalid")
    if candidate.get("state") not in {"provisional", "screened"}:
        raise ValueError("candidate state is invalid")
    if schema_version == LEGACY_SCHEMA_VERSION:
        if "schema_version" in candidate or "execution_identity" in candidate:
            raise ValueError("legacy candidate contains a post-V1 field")
    else:
        if candidate.get("schema_version") != PROVISIONAL_SCHEMA_VERSION:
            raise ValueError("candidate schema is invalid")
        expected_execution_identity = _derive_execution_identity(
            candidate, decision_snapshot
        )
        if candidate.get("execution_identity") != expected_execution_identity:
            raise ValueError(
                "candidate execution identity differs from decision bundle provenance"
            )
        expected_kickoff = min(
            _aware(leg["kickoff_at_bjt"], "execution identity kickoff")
            for leg in expected_execution_identity["legs"]
        ).isoformat()
        if candidate.get("earliest_kickoff_at_bjt") != expected_kickoff:
            raise ValueError(
                "candidate earliest kickoff differs from execution identity"
            )
    _validate_bjt_fields(candidate)
    return candidate


def _read_candidate_csv(
    path: Path,
    route: str,
    target_date: date,
    decision_snapshot: dict,
    schema_version: int,
) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != _CSV_FIELDS:
                raise ValueError("provisional CSV header is invalid")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("provisional CSV is missing or invalid") from exc
    candidates = []
    for row in rows:
        try:
            candidate = json.loads(row["candidate_payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("candidate payload JSON is invalid") from exc
        candidate = _validate_candidate(
            candidate, target_date, decision_snapshot, schema_version
        )
        if candidate["route"] != route:
            raise ValueError("candidate route does not match provisional CSV")
        if row != _candidate_csv_row(candidate):
            raise ValueError("candidate CSV columns differ from candidate payload JSON")
        candidates.append(candidate)
    return candidates


def _validate_receipt(
    root: Path,
    target_date: date,
    generation_id: str,
    candidate: dict,
    generated_at_bjt: str,
    decision_snapshot: dict,
    schema_version: int,
) -> None:
    expected_attestation = _sha256(
        _initial_candidate_binding(candidate, decision_snapshot)
    )
    if candidate.get("initial_candidate_attestation_sha256") != expected_attestation:
        raise ValueError("initial candidate attestation is invalid")
    if candidate["state"] == "provisional":
        if candidate.get("t90_receipt_path") or candidate.get("t90_receipt_sha256"):
            raise ValueError("provisional candidate has an unexpected T-90 receipt")
        return
    expected_path = (
        root / "output" / "provisional_generations" / target_date.isoformat()
        / generation_id / "receipts" / f"{candidate['candidate_id']}-t90.json"
    )
    if candidate.get("t90_receipt_path") != expected_path.relative_to(root).as_posix():
        raise ValueError("T-90 receipt path is invalid")
    try:
        content = expected_path.read_bytes()
    except OSError as exc:
        raise ValueError("T-90 receipt is missing") from exc
    if candidate.get("t90_receipt_sha256") != _bytes_sha256(content):
        raise ValueError("T-90 receipt digest is invalid")
    receipt = _read_json_bytes(content, "T-90 receipt")
    if content != _canonical_bytes(receipt) + b"\n":
        raise ValueError("T-90 receipt serialization is invalid")
    expected = {
        "schema_version": schema_version,
        "receipt_type": "t90_initial_snapshot",
        "candidate_id": candidate["candidate_id"],
        "initial_candidate_attestation_sha256": expected_attestation,
        "generated_at_bjt": generated_at_bjt,
        "decision_snapshot_sha256": _sha256(decision_snapshot),
        "decision_snapshot": decision_snapshot,
    }
    if receipt != expected:
        raise ValueError("T-90 receipt candidate or initial snapshot binding is invalid")
    _validate_bjt_fields(receipt)


def _validate_generation(
    root: Path,
    target_date: date,
    pointer: dict,
    decision_bundle: dict,
) -> dict:
    schema_version = pointer.get("schema_version")
    if schema_version not in {
        LEGACY_SCHEMA_VERSION, GENERATION_POINTER_SCHEMA_VERSION
    }:
        raise ValueError("provisional generation manifest schema is invalid")
    if pointer.get("report_date") != target_date.isoformat():
        raise ValueError("provisional generation manifest date is invalid")
    generation_id = pointer.get("generation_id")
    if (
        not isinstance(generation_id, str)
        or len(generation_id) != 64
        or any(character not in "0123456789abcdef" for character in generation_id)
    ):
        raise ValueError("provisional generation id is invalid")
    bundle_sha256 = _sha256(decision_bundle)
    expected_bundle_path = f"output/decision_bundle_{target_date.isoformat()}.json"
    if (
        pointer.get("decision_bundle_path") != expected_bundle_path
        or pointer.get("decision_bundle_sha256") != bundle_sha256
    ):
        raise ValueError("provisional generation decision bundle provenance is invalid")
    records = pointer.get("artifacts")
    if not isinstance(records, dict) or set(records) != set(_ARTIFACT_FILENAMES):
        raise ValueError("provisional generation manifest artifacts are invalid")
    paths = {
        key: _validate_artifact_record(
            root, target_date, generation_id, key, records[key]
        )
        for key in _ARTIFACT_FILENAMES
    }
    state = _read_json_bytes(paths["state"].read_bytes(), "provisional state")
    if state.get("schema_version") != schema_version:
        raise ValueError("provisional state schema is invalid")
    if (
        state.get("report_date") != target_date.isoformat()
        or state.get("generation_id") != generation_id
        or not isinstance(state.get("candidates"), list)
    ):
        raise ValueError("provisional state date, generation, or candidates are invalid")
    if (
        state.get("decision_bundle_path") != expected_bundle_path
        or state.get("decision_bundle_sha256") != bundle_sha256
    ):
        raise ValueError("provisional state decision bundle provenance is invalid")
    generated_at_bjt = state.get("generated_at_bjt")
    if not isinstance(generated_at_bjt, str) or _bjt(
        generated_at_bjt, "generated_at_bjt"
    ) != generated_at_bjt:
        raise ValueError("provisional generated_at_bjt is invalid")
    if (
        state.get("provisional_plan_sha256") != records["active_plan"]["sha256"]
        or state.get("provisional_shadow_plan_sha256")
        != records["shadow_plan"]["sha256"]
    ):
        raise ValueError("provisional plan digest is invalid")

    decision_snapshot = decision_bundle["decision_snapshot"]
    active = _read_candidate_csv(
        paths["active_plan"], "active", target_date, decision_snapshot,
        schema_version,
    )
    shadow = _read_candidate_csv(
        paths["shadow_plan"], "shadow", target_date, decision_snapshot,
        schema_version,
    )
    csv_candidates = [*active, *shadow]
    csv_ids = [candidate["candidate_id"] for candidate in csv_candidates]
    if len(csv_ids) != len(set(csv_ids)):
        raise ValueError("duplicate candidate id in provisional CSVs")
    state_candidates = [
        _validate_candidate(
            candidate, target_date, decision_snapshot, schema_version
        )
        for candidate in state["candidates"]
    ]
    state_ids = [candidate["candidate_id"] for candidate in state_candidates]
    if len(state_ids) != len(set(state_ids)):
        raise ValueError("duplicate candidate id in provisional state")
    csv_by_id = {candidate["candidate_id"]: candidate for candidate in csv_candidates}
    state_by_id = {
        candidate["candidate_id"]: candidate for candidate in state_candidates
    }
    if csv_by_id != state_by_id:
        raise ValueError("provisional state and CSV candidate join is invalid")
    if state_candidates != csv_candidates:
        raise ValueError("provisional state and CSV candidate order differs")
    active_stake = sum(candidate["provisional_stake"] for candidate in active)
    if (
        state.get("active_candidate_count") != len(active)
        or state.get("shadow_candidate_count") != len(shadow)
        or state.get("active_provisional_stake") != active_stake
        or not isinstance(state.get("active_provisional_stake"), int)
        or isinstance(state.get("active_provisional_stake"), bool)
    ):
        raise ValueError("provisional state validated route totals are invalid")
    for candidate in csv_candidates:
        _validate_receipt(
            root,
            target_date,
            generation_id,
            candidate,
            generated_at_bjt,
            decision_bundle["decision_snapshot"],
            schema_version,
        )
    _validate_bjt_fields(state)
    if schema_version == LEGACY_SCHEMA_VERSION:
        adapted = dict(state)
        adapted["candidates"] = [
            _ProvenanceDerivedCandidate(
                candidate,
                _derive_execution_identity(candidate, decision_snapshot),
            )
            for candidate in state_candidates
        ]
        return adapted
    return state


def create_provisional_outputs(
    root: Path,
    target_date: date,
    generated_at: datetime,
    decision_bundle: dict,
) -> dict:
    """Create one immutable provisional generation and publish its pointer."""
    root = Path(root)
    generated = _aware(generated_at, "generated_at").astimezone(BEIJING)
    validated_bundle = read_valid_decision_bundle(root, target_date)
    if decision_bundle != validated_bundle:
        raise ValueError("supplied decision bundle differs from validated decision bundle")

    pointer_path = _pointer_path(root, target_date)
    existing_state = None
    if pointer_path.exists():
        existing_state = read_valid_provisional_state(root, target_date)
        if existing_state.get("schema_version") == LEGACY_SCHEMA_VERSION:
            return existing_state
        generated = _aware(
            existing_state["generated_at_bjt"], "existing generated_at_bjt"
        )
    outputs = strategy_outputs_from_bundle(
        target_date, validated_bundle, generated
    )
    pointer, pointer_bytes, artifacts = _build_generation(
        root, target_date, generated, validated_bundle, outputs
    )

    if existing_state is not None:
        if pointer_path.read_bytes() != pointer_bytes:
            raise ValueError("conflicting provisional publication")
        return existing_state

    for path, content in artifacts.values():
        _write_create_only(path, content)
    _validate_generation(root, target_date, pointer, validated_bundle)
    _write_create_only(
        pointer_path,
        pointer_bytes,
        conflict_message="conflicting provisional publication",
    )
    return read_valid_provisional_state(root, target_date)


def read_valid_provisional_state(root: Path, target_date: date) -> dict:
    """Read only the fully validated generation selected by the immutable pointer."""
    root = Path(root)
    pointer_path = _pointer_path(root, target_date)
    try:
        pointer_bytes = pointer_path.read_bytes()
    except OSError as exc:
        raise ValueError("provisional generation manifest or pointer is missing") from exc
    pointer = _read_json_bytes(pointer_bytes, "provisional generation manifest")
    if pointer_bytes != _canonical_bytes(pointer) + b"\n":
        raise ValueError("provisional generation manifest serialization is invalid")
    decision_bundle = read_valid_decision_bundle(root, target_date)
    return _validate_generation(root, target_date, pointer, decision_bundle)


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc
    if value != parsed.isoformat():
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    return parsed


def _parse_generated_at(value: str) -> datetime:
    try:
        return _aware(value, "generated_at")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create one immutable provisional plan generation."
    )
    parser.add_argument("--date", required=True, type=_parse_date)
    parser.add_argument("--generated-at", required=True, type=_parse_generated_at)
    args = parser.parse_args()
    try:
        bundle = read_valid_decision_bundle(Path.cwd(), args.date)
        create_provisional_outputs(Path.cwd(), args.date, args.generated_at, bundle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
