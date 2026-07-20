"""Monotonic pre-kickoff revalidation over immutable provisional candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from types import MappingProxyType

from betting_ledger import ingest_revalidated_receipts
from live_odds import capture_live_snapshot, read_valid_live_snapshot
from provisional_plan import (
    PROVISIONAL_SCHEMA_VERSION,
    _copy_validated_candidate,
    _legacy_published_candidate,
    read_valid_provisional_state,
)


BEIJING = timezone(timedelta(hours=8))
STATE_SCHEMA_VERSION = 1
TERMINAL_STATES = frozenset({"confirmed", "cancelled"})
LEDGER_STATUSES = frozenset({"not_applicable", "pending", "ingested"})
ALLOWED_TRANSITIONS = {
    ("provisional", "t90", "pass"): "screened",
    ("provisional", "t90", "cancel"): "cancelled",
    ("screened", "t30", "confirm"): "confirmed",
    ("screened", "t30", "cancel"): "cancelled",
}
_REASON_CODES = frozenset({
    "passed", "confirmed", "candidate_invalid", "snapshot_invalid",
    "fixture_mismatch", "market_mismatch", "market_not_selling",
    "single_ineligible", "kickoff_invalid", "snapshot_after_kickoff",
    "odds_invalid", "odds_below_minimum", "ev_below_minimum",
    "stake_below_minimum", "t90_window_missed", "t30_window_missed",
})


@dataclass(frozen=True)
class _ValidatedRevalidationEvidence:
    candidate: Mapping[str, object]
    receipt: Mapping[str, object]
    receipt_bytes: bytes
    receipt_sha256: str
    receipt_path: str
    snapshot: Mapping[str, object]
    snapshot_bytes: bytes
    snapshot_sha256: str
    snapshot_path: str
    t90_receipt_path: str
    t90_receipt_sha256: str


def due_stage(candidate: dict, now: datetime) -> str | None:
    """Return the one monotonic transition due at *now*, in Beijing time."""
    state = candidate.get("_runtime_state", candidate.get("state")) if isinstance(candidate, dict) else None
    if state in TERMINAL_STATES:
        return None
    if state not in {"provisional", "screened"}:
        raise ValueError("candidate state is invalid")
    checked_at = _aware(now, "now")
    kickoff = _earliest_kickoff(candidate)
    minutes = Decimal(str((kickoff - checked_at).total_seconds())) / Decimal("60")
    if minutes <= 0:
        return None
    if state == "provisional":
        if minutes > Decimal("105"):
            return None
        if minutes > Decimal("40"):
            return "t90"
        return "t90_window_missed"
    if minutes > Decimal("40"):
        return None
    if minutes > Decimal("10"):
        return "t30"
    return "t30_window_missed"


def evaluate_candidate(
    candidate: dict,
    snapshot: dict,
    stage: str,
    checked_at: datetime,
    config: dict,
    remaining_caps: dict | None = None,
) -> dict:
    """Evaluate an immutable candidate without publishing state or touching a ledger."""
    checked = _aware(checked_at, "checked_at")
    validated = _validate_candidate(candidate)
    settings = _validate_config(config)
    if stage not in {"t90", "t30"}:
        raise ValueError("revalidation stage is invalid")
    state = validated.get("_runtime_state", validated["state"])
    if (state, stage) not in {("provisional", "t90"), ("screened", "t30")}:
        raise ValueError("revalidation stage is not allowed for candidate state")

    evaluation = _evaluate_legs(validated, snapshot, checked)
    probability = _decimal(validated["conservative_probability"], "conservative_probability")
    minimum_ev = _decimal(validated["minimum_ev"], "minimum_ev")
    minimum_odds = (Decimal("1") + minimum_ev) / probability
    current_odds = evaluation.get("odds", Decimal("0"))
    current_ev = probability * current_odds - Decimal("1")
    reason = evaluation.get("reason_code")
    if reason is None and current_odds <= Decimal("1"):
        reason = "odds_invalid"
    if reason is None and current_odds < minimum_odds:
        reason = "odds_below_minimum"
    if reason is None and current_ev < minimum_ev:
        reason = "ev_below_minimum"

    stake = 0
    if reason is None:
        stake = _stake(
            probability, current_odds, validated["provisional_stake"], settings, remaining_caps
        )
        if stake < settings["stake_unit"]:
            reason = "stake_below_minimum"
    decision = "cancel" if reason else ("pass" if stage == "t90" else "confirm")
    next_state = ALLOWED_TRANSITIONS[(state, stage, decision)]
    receipt = {
        "schema_version": STATE_SCHEMA_VERSION,
        "receipt_type": "pre_kickoff_revalidation",
        "candidate_id": validated["candidate_id"],
        "candidate_payload_sha256": validated["candidate_payload_sha256"],
        "stage": stage,
        "checked_at_bjt": checked.isoformat(),
        "earliest_kickoff_at_bjt": _earliest_kickoff(validated).isoformat(),
        "snapshot_source": snapshot.get("source") if isinstance(snapshot, dict) else None,
        "conservative_probability": format(probability, "f"),
        "minimum_acceptable_odds": format(minimum_odds, ".6f"),
        "current_odds": format(current_odds, "f"),
        "current_ev": format(current_ev, ".3f"),
        "provisional_stake": validated["provisional_stake"],
        "final_stake": stake,
        "decision": decision,
        "reason_code": reason or ("passed" if stage == "t90" else "confirmed"),
    }
    _bind_legacy_execution_identity(receipt, validated)
    return {"candidate_id": validated["candidate_id"], "decision": decision, "state": next_state, "stake": stake, "receipt": receipt}


def run_due_revalidation(
    root: Path,
    now: datetime,
    target_dates: list[date] | None = None,
    snapshot_provider=capture_live_snapshot,
) -> list[dict]:
    """Run only due candidates for the current and prior Beijing business dates."""
    root = Path(root).resolve()
    checked = _aware(now, "now")
    settings = _read_settings(root)
    dates = _target_dates(checked, target_dates, settings["scan_business_days"])
    source_commit_sha = _source_commit_sha(root)
    changed: list[dict] = []
    for target_date in dates:
        try:
            source_state = read_valid_provisional_state(root, target_date)
        except ValueError as exc:
            if "manifest or pointer is missing" in str(exc):
                continue
            raise
        state_path = _state_path(root, target_date)
        state = _load_or_initialize_state(root, target_date, source_state, state_path)
        source_by_id = {
            candidate["candidate_id"]: candidate
            for candidate in source_state["candidates"]
        }
        due = []
        for entry in state["candidates"]:
            effective = _copy_validated_candidate(
                source_by_id[entry["candidate"]["candidate_id"]]
            )
            effective["_runtime_state"] = entry["state"]
            stage = due_stage(effective, checked)
            if stage is not None:
                due.append((effective, entry, stage))
        due.sort(key=lambda item: (_earliest_kickoff(item[0]), item[0]["provisional_rank"], item[0]["candidate_id"]))
        next_state = json.loads(json.dumps(state))
        entries_by_id = {entry["candidate"]["candidate_id"]: entry for entry in next_state["candidates"]}
        pending_receipts: list[tuple[dict, dict]] = []
        results_by_id = {}
        needs_evaluation = []
        for effective, old_entry, stage in due:
            entry = entries_by_id[effective["candidate_id"]]
            actual_stage = "t90" if stage.startswith("t90") else "t30"
            path = _receipt_path(root, target_date, effective["candidate_id"], actual_stage)
            if path.exists():
                relative = _relative_path(root, path)
                raw = path.read_bytes()
                try:
                    receipt = _validate_receipt_file(
                        root, target_date, effective, actual_stage,
                        relative, _sha256_bytes(raw),
                    )
                    result = _replay_receipt_result(
                        root, target_date, effective, old_entry, actual_stage,
                        receipt, settings,
                    )
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        "conflicting revalidation receipt cannot represent pending transition"
                    ) from exc
                entry[f"{actual_stage}_receipt_path"] = relative
                entry[f"{actual_stage}_receipt_sha256"] = _sha256_bytes(raw)
                _apply_transition_result(entry, result)
                results_by_id[effective["candidate_id"]] = result
            else:
                needs_evaluation.append((effective, old_entry, stage))

        if needs_evaluation:
            snapshot_path = snapshot_provider(root, target_date, checked)
            snapshot = read_valid_live_snapshot(root, snapshot_path, target_date, checked)
            snapshot_rel = _relative_path(root, Path(snapshot_path))
            snapshot_sha = _sha256_bytes(_canonical_bytes(snapshot))
        for effective, old_entry, stage in needs_evaluation:
            entry = entries_by_id[effective["candidate_id"]]
            if stage.endswith("_window_missed"):
                result = _missed_window_result(effective, stage, checked)
            else:
                caps = {"previous_stake": old_entry.get("last_stake", effective["provisional_stake"])}
                result = evaluate_candidate(effective, snapshot, stage, checked, settings, caps)
            receipt = result["receipt"]
            receipt["live_odds_snapshot_path"] = snapshot_rel
            receipt["live_odds_snapshot_sha256"] = snapshot_sha
            pending_receipts.append((entry, receipt))
            _apply_transition_result(entry, result)
            results_by_id[effective["candidate_id"]] = result

        receipt_paths = []
        for entry, receipt in pending_receipts:
            path = _receipt_path(root, target_date, entry["candidate"]["candidate_id"], receipt["stage"])
            raw = _canonical_bytes(receipt)
            _write_create_only(path, raw)
            receipt_paths.append((entry, path, raw))
        for entry, path, raw in receipt_paths:
            field = f"{json.loads(raw.decode('utf-8'))['stage']}_receipt"
            entry[field + "_path"] = _relative_path(root, path)
            entry[field + "_sha256"] = _sha256_bytes(raw)
        if due:
            _validate_runtime_state(root, target_date, next_state, source_state)
            _write_state_atomic(state_path, next_state)

        published = _load_or_initialize_state(
            root, target_date, source_state, state_path
        )
        pending = [
            entry for entry in published["candidates"]
            if entry["state"] == "confirmed"
            and entry["ledger_status"] == "pending"
        ]
        if pending:
            receipt_paths = [
                root / entry["t30_receipt_path"] for entry in pending
            ]
            ingest_revalidated_receipts(root, target_date, receipt_paths)
            ingested = json.loads(json.dumps(published))
            pending_ids = {
                entry["candidate"]["candidate_id"] for entry in pending
            }
            for entry in ingested["candidates"]:
                if entry["candidate"]["candidate_id"] in pending_ids:
                    entry["ledger_status"] = "ingested"
            _validate_runtime_state(root, target_date, ingested, source_state)
            _write_state_atomic(state_path, ingested)
            entries_by_id = {
                entry["candidate"]["candidate_id"]: entry
                for entry in ingested["candidates"]
            }
        durable = _load_or_initialize_state(
            root, target_date, source_state, state_path
        )
        entries_by_id = {
            entry["candidate"]["candidate_id"]: entry
            for entry in durable["candidates"]
        }
        date_changes = []
        for effective, _old_entry, _stage in due:
            entry = entries_by_id[effective["candidate_id"]]
            result = results_by_id[effective["candidate_id"]]
            date_changes.append({
                "candidate_id": effective["candidate_id"], "state": entry["state"],
                "stake": entry["confirmed_stake"], "receipt": result["receipt"],
            })
        if date_changes:
            from revalidation_reporting import publish_revalidation_report

            publish_revalidation_report(
                root,
                target_date,
                date_changes,
                checked,
                source_commit_sha,
            )
            changed.extend(date_changes)
    return changed


def read_valid_revalidation_receipt(
    root: Path,
    path: Path,
    target_date: date,
    expected_stage: str = "t30",
    *,
    _capture_evidence: bool = False,
) -> dict | _ValidatedRevalidationEvidence:
    """Read one final receipt only after validating its complete evidence graph."""
    root = Path(root).resolve()
    supplied = Path(path).resolve()
    if expected_stage not in {"t90", "t30"}:
        raise ValueError("expected revalidation stage is invalid")
    relative = _relative_path(root, supplied)
    try:
        raw = supplied.read_bytes()
        preliminary = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("revalidation receipt is missing or invalid") from exc
    if raw != _canonical_bytes(preliminary):
        raise ValueError("revalidation receipt serialization is invalid")

    source = read_valid_provisional_state(root, target_date)
    state_path = _state_path(root, target_date)
    state = _load_or_initialize_state(root, target_date, source, state_path)
    candidate_id = preliminary.get("candidate_id")
    entries = [
        entry for entry in state["candidates"]
        if entry["candidate"]["candidate_id"] == candidate_id
    ]
    if len(entries) != 1:
        raise ValueError("revalidation receipt candidate is invalid")
    entry = entries[0]
    source_candidates = [
        item for item in source["candidates"]
        if item["candidate_id"] == candidate_id
    ]
    if len(source_candidates) != 1:
        raise ValueError("revalidation receipt candidate is invalid")
    candidate = source_candidates[0]
    if (
        entry["state"] != "confirmed"
        or entry["ledger_status"] not in {"pending", "ingested"}
        or entry.get(f"{expected_stage}_receipt_path") != relative
        or entry.get(f"{expected_stage}_receipt_sha256") != _sha256_bytes(raw)
        or supplied
        != _receipt_path(root, target_date, candidate_id, expected_stage).resolve()
    ):
        raise ValueError("revalidation receipt is not a final confirmation")

    receipt, snapshot, snapshot_bytes = _validate_receipt_file(
        root,
        target_date,
        candidate,
        expected_stage,
        relative,
        _sha256_bytes(raw),
        captured_bytes=raw,
        capture_snapshot=True,
    )
    if expected_stage != "t30" or receipt.get("decision") != "confirm":
        raise ValueError("only a final T-30 confirmation can enter a ledger")

    source_candidate = candidate
    previous = {
        "state": source_candidate["state"],
        "last_stake": source_candidate["provisional_stake"],
    }
    t90_checked = None
    if source_candidate["state"] == "screened":
        initial = _validate_initial_t90_receipt(
            root, target_date, source, source_candidate
        )
        t90_relative = source_candidate["t90_receipt_path"]
        t90_sha = source_candidate["t90_receipt_sha256"]
        t90_checked = _aware(
            initial.get("generated_at_bjt"), "initial T-90 checked_at_bjt"
        )
    else:
        t90_relative = entry.get("t90_receipt_path")
        t90_sha = entry.get("t90_receipt_sha256")
        if not t90_relative or not t90_sha:
            raise ValueError("final confirmation lacks T-90 receipt evidence")
        t90 = _validate_receipt_file(
            root, target_date, candidate, "t90", t90_relative, t90_sha
        )
        effective = _copy_validated_candidate(source_candidate)
        effective["_runtime_state"] = previous["state"]
        t90_result = _replay_receipt_result(
            root,
            target_date,
            effective,
            previous,
            "t90",
            t90,
            _read_settings(root),
        )
        previous = {
            "state": t90_result["state"],
            "last_stake": t90_result["stake"],
        }
        t90_checked = _aware(t90["checked_at_bjt"], "T-90 checked_at_bjt")

    effective = _copy_validated_candidate(source_candidate)
    effective["_runtime_state"] = previous["state"]
    final_result = _replay_receipt_result(
        root,
        target_date,
        effective,
        previous,
        "t30",
        receipt,
        _read_settings(root),
        validated_snapshot=snapshot,
    )
    checked = _aware(receipt["checked_at_bjt"], "T-30 checked_at_bjt")
    if (
        final_result["state"] != "confirmed"
        or final_result["stake"] > source_candidate["provisional_stake"]
        or t90_checked is None
        or checked <= t90_checked
        or checked >= _earliest_kickoff(source_candidate)
    ):
        raise ValueError("revalidation receipt time or amount graph is invalid")
    evidence = _ValidatedRevalidationEvidence(
        candidate=_freeze_json(source_candidate),
        receipt=_freeze_json(receipt),
        receipt_bytes=raw,
        receipt_sha256=_sha256_bytes(raw),
        receipt_path=relative,
        snapshot=_freeze_json(snapshot),
        snapshot_bytes=snapshot_bytes,
        snapshot_sha256=_sha256_bytes(snapshot_bytes),
        snapshot_path=receipt["live_odds_snapshot_path"],
        t90_receipt_path=t90_relative,
        t90_receipt_sha256=t90_sha,
    )
    if _capture_evidence:
        return evidence
    return _thaw_json(evidence.receipt)


def _apply_transition_result(entry: dict, result: dict) -> None:
    entry["state"] = result["state"]
    entry["last_stake"] = result["stake"]
    entry["confirmed_stake"] = result["stake"] if result["state"] == "confirmed" else 0
    entry["ledger_status"] = "pending" if result["state"] == "confirmed" else "not_applicable"


def _replay_receipt_result(
    root: Path,
    target_date: date,
    candidate: dict,
    previous_entry: dict,
    stage: str,
    receipt: dict,
    settings: dict,
    *,
    validated_snapshot: dict | None = None,
) -> dict:
    checked_at = _aware(receipt["checked_at_bjt"], "receipt checked_at_bjt")
    reason = receipt["reason_code"]
    expected_due = reason if reason in {"t90_window_missed", "t30_window_missed"} else stage
    if due_stage(candidate, checked_at) != expected_due:
        raise ValueError("revalidation receipt was not due at its recorded time")
    if validated_snapshot is None:
        snapshot_path = (root / receipt["live_odds_snapshot_path"]).resolve()
        snapshot = read_valid_live_snapshot(
            root, snapshot_path, target_date, checked_at
        )
    else:
        snapshot = validated_snapshot
    if expected_due.endswith("_window_missed"):
        result = _missed_window_result(candidate, expected_due, checked_at)
    else:
        caps = {"previous_stake": previous_entry.get("last_stake", candidate["provisional_stake"])}
        result = evaluate_candidate(candidate, snapshot, stage, checked_at, settings, caps)
    expected_receipt = result["receipt"]
    expected_receipt["live_odds_snapshot_path"] = receipt["live_odds_snapshot_path"]
    expected_receipt["live_odds_snapshot_sha256"] = receipt["live_odds_snapshot_sha256"]
    if expected_receipt != receipt:
        raise ValueError("revalidation receipt cannot be reproduced from its snapshot")
    state, stake = _receipt_transition(previous_entry["state"], stage, receipt)
    if state != result["state"] or stake != result["stake"]:
        raise ValueError("revalidation receipt transition is inconsistent")
    return result


def _missed_window_result(candidate: dict, stage: str, checked: datetime) -> dict:
    if stage not in {"t90_window_missed", "t30_window_missed"}:
        raise ValueError("missed revalidation window is invalid")
    actual_stage = "t90" if stage.startswith("t90") else "t30"
    decision = "cancel"
    receipt = {
        "schema_version": STATE_SCHEMA_VERSION, "receipt_type": "pre_kickoff_revalidation",
        "candidate_id": candidate["candidate_id"], "candidate_payload_sha256": candidate["candidate_payload_sha256"],
        "stage": actual_stage, "checked_at_bjt": checked.isoformat(),
        "earliest_kickoff_at_bjt": _earliest_kickoff(candidate).isoformat(),
        "snapshot_source": None, "conservative_probability": candidate["conservative_probability"],
        "minimum_acceptable_odds": candidate["minimum_acceptable_odds"], "current_odds": "0",
        "current_ev": "-1.000", "provisional_stake": candidate["provisional_stake"],
        "final_stake": 0, "decision": decision, "reason_code": stage,
    }
    _bind_legacy_execution_identity(receipt, candidate)
    current_state = candidate.get("_runtime_state", candidate["state"])
    return {
        "candidate_id": candidate["candidate_id"], "decision": decision,
        "state": ALLOWED_TRANSITIONS[(current_state, actual_stage, decision)],
        "stake": 0, "receipt": receipt,
    }


def _evaluate_legs(candidate: dict, snapshot: dict, checked: datetime) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("source") not in {"sporttery", "zgzcw"} or snapshot.get("fetch_mode") != "live":
        return {"reason_code": "snapshot_invalid"}
    try:
        captured = _aware(snapshot["captured_at"], "snapshot captured_at")
    except (KeyError, ValueError):
        return {"reason_code": "snapshot_invalid"}
    if captured > checked:
        return {"reason_code": "snapshot_invalid"}
    execution_identity = candidate.get("execution_identity")
    initial_capture = (
        execution_identity.get("decision_snapshot_captured_at_bjt")
        if isinstance(execution_identity, dict)
        else None
    )
    if initial_capture:
        try:
            if captured <= _aware(initial_capture, "initial odds capture"):
                return {"reason_code": "snapshot_invalid"}
        except ValueError:
            return {"reason_code": "snapshot_invalid"}
    matches = snapshot.get("matches")
    if not isinstance(matches, list):
        return {"reason_code": "snapshot_invalid"}
    by_id = {row.get("match_id"): row for row in matches if isinstance(row, dict)}
    if len(by_id) != len(matches):
        return {"reason_code": "snapshot_invalid"}
    try:
        legs = _bound_legs(candidate)
    except ValueError:
        return {"reason_code": "candidate_invalid"}
    odds = Decimal("1")
    for leg in legs:
        if snapshot.get("source") != leg["source"]:
            return {"reason_code": "fixture_mismatch"}
        row = by_id.get(leg["match_id"])
        if row is None or not _fixture_matches(leg, row):
            return {"reason_code": "fixture_mismatch"}
        try:
            kickoff = _aware(row["kickoff_at"], "snapshot kickoff")
        except (KeyError, ValueError):
            return {"reason_code": "kickoff_invalid"}
        if kickoff <= checked or captured >= kickoff:
            return {"reason_code": "snapshot_after_kickoff"}
        if row.get("sales_state") != leg["sales_state"] or row.get("sales_state") != "Selling":
            return {"reason_code": "market_not_selling"}
        market = row.get("markets", {}).get(leg["market_type"])
        if not isinstance(market, dict) or leg["selection"] not in market:
            return {"reason_code": "market_mismatch"}
        if leg["market_type"] == "hhad":
            try:
                if _handicap(market.get("goalLine")) != _handicap(leg["market_line"]):
                    return {"reason_code": "market_mismatch"}
            except ValueError:
                return {"reason_code": "market_mismatch"}
        elif leg["market_line"]:
            return {"reason_code": "market_mismatch"}
        eligibility = row.get("single_eligibility", {})
        if (
            not isinstance(eligibility, dict)
            or eligibility.get(leg["market_type"]) is not leg["single_eligible"]
            or eligibility.get(leg["market_type"]) is not True
        ):
            return {"reason_code": "single_ineligible"}
        try:
            odd = _decimal(market[leg["selection"]], "current odds")
        except ValueError:
            return {"reason_code": "odds_invalid"}
        if odd <= 1:
            return {"reason_code": "odds_invalid"}
        odds *= odd
    return {"odds": odds}


def _fixture_matches(leg: dict, row: dict) -> bool:
    for key in (
        "source_record_id", "match_id", "match_num", "team_a", "team_b"
    ):
        if row.get(key) != leg[key]:
            return False
    if row.get("kickoff_at") != leg["kickoff_at_bjt"]:
        return False
    return True


def _normalized_legs(candidate: dict) -> list[dict]:
    supplied = candidate.get("legs")
    if isinstance(supplied, list) and supplied:
        raw = supplied
    else:
        identity = candidate.get("normalized_market_identity")
        raw = identity.get("legs") if isinstance(identity, dict) and identity.get("market_type") == "parlay" else [identity]
    legs = []
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError("candidate leg is invalid")
        leg = dict(value)
        for key in ("match_id", "market_type", "selection"):
            if not isinstance(leg.get(key), str) or not leg[key]:
                raise ValueError("candidate leg identity is invalid")
        legs.append(leg)
    return legs


def _bound_legs(candidate: dict) -> list[dict]:
    normalized = _normalized_legs(candidate)
    execution_identity = candidate.get("execution_identity")
    if not isinstance(execution_identity, dict):
        raise ValueError("candidate execution identity is invalid")
    snapshot_digest = execution_identity.get("decision_snapshot_sha256")
    if (
        not isinstance(snapshot_digest, str)
        or len(snapshot_digest) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_digest)
    ):
        raise ValueError("candidate decision snapshot digest is invalid")
    _aware(
        execution_identity.get("decision_snapshot_captured_at_bjt"),
        "candidate decision snapshot capture",
    )
    bound = execution_identity.get("legs")
    if not isinstance(bound, list) or len(bound) != len(normalized):
        raise ValueError("candidate execution legs differ from normalized identity")
    for identity, leg in zip(normalized, bound):
        if not isinstance(leg, dict):
            raise ValueError("candidate execution leg is invalid")
        for key in (
            "source", "source_record_id", "match_id", "match_num", "team_a",
            "team_b", "kickoff_at_bjt", "market_type", "selection", "sales_state",
        ):
            _required_identity_text(leg.get(key), key)
        if leg["source"] not in {"sporttery", "zgzcw"}:
            raise ValueError("candidate source is not domestic")
        if leg["sales_state"] != "Selling":
            raise ValueError("candidate sales state is invalid")
        if leg.get("single_eligible") is not True:
            raise ValueError("candidate single eligibility is invalid")
        _aware(leg["kickoff_at_bjt"], "candidate execution kickoff")
        market_type = leg["market_type"]
        if market_type == "hhad":
            line = str(_handicap(leg.get("market_line")))
        elif leg.get("market_line") is None:
            line = ""
        else:
            raise ValueError("candidate market line is invalid")
        if not _source_leg_matches(
            identity,
            {
                "match_id": leg["match_id"],
                "market_type": market_type,
                "selection": leg["selection"],
                "line": line,
                "kickoff_at": leg["kickoff_at_bjt"],
            },
        ):
            raise ValueError("candidate execution leg differs from normalized identity")
    return bound


def _source_leg_matches(identity: dict, evidence: dict) -> bool:
    evidence_line = str(evidence.get("market_line", evidence.get("line", "")) or "").strip()
    identity_line = str(identity.get("market_line", "") or "").strip()
    if identity.get("market_type") == "hhad":
        try:
            line_matches = _handicap(evidence_line) == _handicap(identity_line)
        except ValueError:
            return False
    else:
        line_matches = evidence_line == identity_line
    kickoff = evidence.get("kickoff_at") or evidence.get("kickoff_local")
    identity_kickoff = identity.get("kickoff_at_bjt", identity.get("kickoff_at"))
    kickoff_matches = identity_kickoff in {None, ""} or (
        kickoff not in {None, ""}
        and _aware(kickoff, "source leg kickoff") == _aware(identity_kickoff, "normalized leg kickoff")
    )
    return (
        str(evidence.get("match_id") or "").strip() == identity.get("match_id")
        and str(evidence.get("market_type") or "").strip().lower() == identity.get("market_type")
        and str(evidence.get("selection") or "").strip() == identity.get("selection")
        and line_matches
        and kickoff_matches
    )


def _required_identity_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"candidate {name} is invalid")
    return value


def _handicap(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("handicap is invalid")
    text = str(value).strip()
    try:
        parsed = Decimal(text)
    except Exception as exc:
        raise ValueError("handicap is invalid") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ValueError("handicap is invalid")
    return int(parsed)


def _stake(probability: Decimal, odds: Decimal, provisional_stake: int, settings: dict, caps: dict | None) -> int:
    full_kelly = (probability * odds - Decimal("1")) / (odds - Decimal("1"))
    fraction = _decimal(settings.get("kelly_fraction", "0.25"), "kelly_fraction")
    bankroll = _decimal(settings.get("reference_bankroll", 5000), "reference_bankroll")
    raw = max(Decimal("0"), full_kelly * fraction * bankroll)
    limits = [Decimal(provisional_stake)]
    if caps is not None:
        if not isinstance(caps, dict):
            raise ValueError("remaining caps are invalid")
        for value in caps.values():
            if value is not None:
                limit = _integral(value, "remaining cap")
                if limit < 0:
                    raise ValueError("remaining cap must be nonnegative")
                limits.append(Decimal(limit))
    capped = min([raw, *limits])
    unit = Decimal(settings["stake_unit"])
    return int((capped / unit).to_integral_value(rounding=ROUND_DOWN) * unit)


def _validate_candidate(
    candidate: object, validated_source: dict | None = None
) -> dict:
    if not isinstance(candidate, dict):
        raise ValueError("candidate is invalid")
    published = _legacy_published_candidate(candidate)
    if published is None and validated_source is not None:
        source_published = _legacy_published_candidate(validated_source)
        if source_published is not None and candidate == validated_source:
            published = source_published
    if published is None:
        if candidate.get("schema_version") != PROVISIONAL_SCHEMA_VERSION:
            raise ValueError("candidate schema or legacy provenance is invalid")
        payload = {
            key: value for key, value in candidate.items()
            if key not in {"candidate_payload_sha256", "_runtime_state"}
        }
    else:
        if "schema_version" in published or "execution_identity" in published:
            raise ValueError("legacy candidate provenance is invalid")
        payload = {
            key: value for key, value in published.items()
            if key != "candidate_payload_sha256"
        }
    if candidate.get("candidate_payload_sha256") != _sha256(payload):
        raise ValueError("candidate digest is invalid")
    if candidate.get("state") not in {"provisional", "screened"}:
        raise ValueError("candidate state is invalid")
    runtime_state = candidate.get("_runtime_state", candidate["state"])
    if runtime_state not in {"provisional", "screened"}:
        raise ValueError("candidate runtime state is invalid")
    _bound_legs(candidate)
    _earliest_kickoff(candidate)
    if _integral(candidate.get("provisional_stake"), "provisional stake") < 0:
        raise ValueError("provisional stake must be nonnegative")
    probability = _decimal(candidate.get("conservative_probability"), "conservative_probability")
    if not Decimal("0") < probability < Decimal("1"):
        raise ValueError("candidate conservative probability is invalid")
    _decimal(candidate.get("minimum_ev"), "minimum_ev")
    return candidate


def _validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise ValueError("pre-kickoff revalidation config is invalid")
    required = ("minimum_initial_minutes", "t90_open_minutes", "t90_close_minutes", "t30_open_minutes", "t30_close_minutes", "scan_business_days", "stake_unit", "max_notification_days")
    values = {}
    for key in required:
        if key not in config:
            raise ValueError("pre-kickoff revalidation config is missing")
        values[key] = _integral(config[key], key)
        if values[key] <= 0:
            raise ValueError("pre-kickoff revalidation config must be positive")
    if config.get("mode") not in {"shadow", "active"}:
        raise ValueError("pre-kickoff revalidation mode is invalid")
    if values["stake_unit"] != 2:
        raise ValueError("stake_unit must be exactly 2")
    if values["scan_business_days"] != 2:
        raise ValueError("scan_business_days must be exactly 2")
    if not values["minimum_initial_minutes"] <= values["t90_open_minutes"] or not values["t90_open_minutes"] > values["t90_close_minutes"] or values["t90_close_minutes"] != values["t30_open_minutes"] or not values["t30_open_minutes"] > values["t30_close_minutes"]:
        raise ValueError("pre-kickoff revalidation windows overlap or are inverted")
    values["mode"] = config["mode"]
    values["reference_bankroll"] = config.get("reference_bankroll", 5000)
    values["kelly_fraction"] = config.get("kelly_fraction", "0.25")
    _decimal(values["reference_bankroll"], "reference_bankroll")
    fraction = _decimal(values["kelly_fraction"], "kelly_fraction")
    if not Decimal("0") < fraction <= Decimal("1"):
        raise ValueError("kelly_fraction is invalid")
    return values


def _read_settings(root: Path) -> dict:
    try:
        payload = json.loads((root / "betting_config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("betting config is missing or invalid") from exc
    settings = _validate_config(payload.get("pre_kickoff_revalidation"))
    value = payload.get("value_strategy", {})
    if isinstance(value, dict):
        settings["reference_bankroll"] = value.get("reference_bankroll", settings["reference_bankroll"])
        settings["kelly_fraction"] = value.get("kelly_fraction", settings["kelly_fraction"])
    return _validate_config(settings)


def _source_commit_sha(root: Path) -> str:
    github_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    if len(github_sha) in {40, 64} and set(github_sha).issubset(
        frozenset("0123456789abcdef")
    ):
        return github_sha
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        local_sha = completed.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        local_sha = ""
    if len(local_sha) in {40, 64} and set(local_sha).issubset(
        frozenset("0123456789abcdef")
    ):
        return local_sha
    return "local-" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _target_dates(now: datetime, supplied: list[date] | None, scan_days: int) -> list[date]:
    checked = _aware(now, "now")
    allowed = [checked.date() - timedelta(days=offset) for offset in range(scan_days)]
    if supplied is None:
        return allowed
    if (
        not isinstance(supplied, list)
        or len(supplied) > scan_days
        or any(type(item) is not date for item in supplied)
        or len(set(supplied)) != len(supplied)
        or not set(supplied).issubset(allowed)
    ):
        raise ValueError("target dates are invalid")
    return sorted(supplied, reverse=True)


def _load_or_initialize_state(root: Path, target_date: date, source: dict, path: Path) -> dict:
    if not path.exists():
        candidates = []
        for source_candidate in source.get("candidates", []):
            validated = _validate_candidate(source_candidate)
            initial_state = validated["state"]
            candidates.append({
                "candidate": validated, "state": initial_state, "ledger_status": "not_applicable",
                "last_stake": validated["provisional_stake"], "confirmed_stake": 0,
                "t90_receipt_path": "", "t90_receipt_sha256": "",
                "t30_receipt_path": "", "t30_receipt_sha256": "",
            })
        state = {"schema_version": STATE_SCHEMA_VERSION, "report_date": target_date.isoformat(), "candidates": candidates}
        _validate_runtime_state(root, target_date, state, source)
        return state
    try:
        raw = path.read_bytes()
        state = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("revalidation state is missing or invalid") from exc
    if raw != _canonical_bytes(state):
        raise ValueError("revalidation state serialization is invalid")
    _validate_runtime_state(root, target_date, state, source)
    return state


def _validate_runtime_state(root: Path, target_date: date, state: object, source: dict) -> None:
    if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA_VERSION or state.get("report_date") != target_date.isoformat() or not isinstance(state.get("candidates"), list):
        raise ValueError("revalidation state is invalid")
    source_candidates = source.get("candidates")
    if not isinstance(source_candidates, list):
        raise ValueError("provisional source candidates are invalid")
    source_by_id = {}
    for item in source_candidates:
        validated_source = _validate_candidate(item)
        source_by_id[validated_source["candidate_id"]] = validated_source
    if len(source_by_id) != len(source_candidates):
        raise ValueError("provisional source candidates are duplicated")
    actual = {}
    for entry in state["candidates"]:
        if not isinstance(entry, dict):
            raise ValueError("revalidation state candidate is invalid")
        raw_candidate = entry.get("candidate")
        if not isinstance(raw_candidate, dict):
            raise ValueError("revalidation state candidate is invalid")
        candidate_id = raw_candidate.get("candidate_id")
        base_candidate = source_by_id.get(candidate_id)
        candidate = _validate_candidate(raw_candidate, base_candidate)
        actual[candidate_id] = candidate["candidate_payload_sha256"]
        if base_candidate != candidate:
            raise ValueError("revalidation state candidate differs from immutable provisional generation")
        current = entry.get("state")
        ledger_status = entry.get("ledger_status")
        if current not in {"provisional", "screened", "confirmed", "cancelled"} or ledger_status not in LEDGER_STATUSES:
            raise ValueError("revalidation state transition is invalid")
        if current == "confirmed" and ledger_status not in {"pending", "ingested"}:
            raise ValueError("confirmed candidate ledger status is invalid")
        if current == "cancelled" and ledger_status != "not_applicable":
            raise ValueError("cancelled candidate ledger status is invalid")
        if current in {"provisional", "screened"} and ledger_status != "not_applicable":
            raise ValueError("nonterminal candidate ledger status is invalid")
        last_stake = _integral(entry.get("last_stake"), "last stake")
        confirmed_stake = _integral(entry.get("confirmed_stake"), "confirmed stake")
        receipts = {}
        for stage in ("t90", "t30"):
            receipt_path = entry.get(f"{stage}_receipt_path", "")
            receipt_sha = entry.get(f"{stage}_receipt_sha256", "")
            if bool(receipt_path) != bool(receipt_sha):
                raise ValueError("revalidation receipt state is invalid")
            if receipt_path:
                receipts[stage] = _validate_receipt_file(
                    root, target_date, base_candidate, stage,
                    receipt_path, receipt_sha
                )
        expected_state = base_candidate["state"]
        expected_stake = base_candidate["provisional_stake"]
        if expected_state == "screened":
            _validate_initial_t90_receipt(root, target_date, source, base_candidate)
            if "t90" in receipts:
                raise ValueError("revalidation receipt transition duplicates initial T-90")
        elif "t90" in receipts:
            expected_state, expected_stake = _receipt_transition(
                expected_state, "t90", receipts["t90"]
            )
        if "t30" in receipts:
            expected_state, expected_stake = _receipt_transition(
                expected_state, "t30", receipts["t30"]
            )
        if current != expected_state:
            raise ValueError("revalidation receipt transition does not match runtime state")
        if last_stake != expected_stake:
            raise ValueError("revalidation receipt stake does not match runtime state")
        expected_confirmed = expected_stake if current == "confirmed" else 0
        if confirmed_stake != expected_confirmed:
            raise ValueError("revalidation confirmed stake does not match receipt transition")
    expected = {
        candidate_id: item["candidate_payload_sha256"]
        for candidate_id, item in source_by_id.items()
    }
    if actual != expected or len(actual) != len(state["candidates"]):
        raise ValueError("revalidation state candidates differ from immutable provisional generation")


def _validate_receipt_file(
    root: Path,
    target_date: date,
    candidate: dict,
    stage: str,
    relative: str,
    expected_sha: str,
    *,
    captured_bytes: bytes | None = None,
    capture_snapshot: bool = False,
) -> dict | tuple[dict, dict, bytes]:
    path = (root / relative).resolve()
    if _relative_path(root, path) != relative:
        raise ValueError("revalidation receipt path is invalid")
    expected_path = _receipt_path(root, target_date, candidate["candidate_id"], stage)
    if path != expected_path.resolve():
        raise ValueError("revalidation receipt path is invalid")
    if captured_bytes is None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError("revalidation receipt is missing") from exc
    else:
        raw = captured_bytes
    if _sha256_bytes(raw) != expected_sha:
        raise ValueError("revalidation receipt digest is invalid")
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("revalidation receipt is invalid") from exc
    if (
        raw != _canonical_bytes(receipt)
        or receipt.get("schema_version") != STATE_SCHEMA_VERSION
        or receipt.get("receipt_type") != "pre_kickoff_revalidation"
        or receipt.get("candidate_id") != candidate["candidate_id"]
        or receipt.get("candidate_payload_sha256") != candidate["candidate_payload_sha256"]
        or receipt.get("stage") != stage
        or receipt.get("reason_code") not in _REASON_CODES
    ):
        raise ValueError("revalidation receipt is invalid")
    legacy_published = _legacy_published_candidate(candidate)
    if legacy_published is not None:
        if receipt.get("execution_identity_sha256") != _sha256(
            candidate["execution_identity"]
        ):
            raise ValueError("legacy execution identity receipt binding is invalid")
    elif "execution_identity_sha256" in receipt:
        raise ValueError("revalidation receipt execution identity binding is invalid")
    try:
        checked_at = _aware(receipt.get("checked_at_bjt"), "receipt checked_at_bjt")
        receipt_kickoff = _aware(receipt.get("earliest_kickoff_at_bjt"), "receipt kickoff")
        final_stake = _integral(receipt.get("final_stake"), "receipt final stake")
        _decimal(receipt.get("current_odds"), "receipt current odds")
        _decimal(receipt.get("current_ev"), "receipt current EV")
    except ValueError as exc:
        raise ValueError("revalidation receipt is invalid") from exc
    if (
        receipt_kickoff != _earliest_kickoff(candidate)
        or receipt.get("conservative_probability") != candidate["conservative_probability"]
        or receipt.get("minimum_acceptable_odds") != candidate["minimum_acceptable_odds"]
        or receipt.get("provisional_stake") != candidate["provisional_stake"]
        or final_stake < 0
        or final_stake % 2
    ):
        raise ValueError("revalidation receipt candidate or stake binding is invalid")
    snapshot_path = receipt.get("live_odds_snapshot_path")
    snapshot_sha = receipt.get("live_odds_snapshot_sha256")
    if not isinstance(snapshot_path, str) or not snapshot_path:
        raise ValueError("revalidation receipt snapshot path is invalid")
    snapshot_absolute = (root / snapshot_path).resolve()
    if _relative_path(root, snapshot_absolute) != snapshot_path:
        raise ValueError("revalidation receipt snapshot path is invalid")
    if not _valid_sha256(snapshot_sha):
        raise ValueError("revalidation receipt snapshot digest is invalid")
    try:
        snapshot = read_valid_live_snapshot(root, snapshot_absolute, target_date, checked_at)
    except ValueError as exc:
        raise ValueError("revalidation receipt snapshot is missing or invalid") from exc
    snapshot_bytes = _canonical_bytes(snapshot)
    if _sha256_bytes(snapshot_bytes) != snapshot_sha:
        raise ValueError("revalidation receipt snapshot digest is invalid")
    if receipt["reason_code"] in {"t90_window_missed", "t30_window_missed"}:
        if receipt.get("snapshot_source") is not None:
            raise ValueError("missed-window receipt snapshot state is invalid")
    elif (
        receipt.get("snapshot_source") not in {"sporttery", "zgzcw"}
        or receipt.get("snapshot_source") != snapshot.get("source")
    ):
        raise ValueError("revalidation receipt snapshot source is invalid")
    if capture_snapshot:
        return receipt, snapshot, snapshot_bytes
    return receipt


def _receipt_transition(previous_state: str, stage: str, receipt: dict) -> tuple[str, int]:
    decision = receipt.get("decision")
    reason = receipt.get("reason_code")
    transition = ALLOWED_TRANSITIONS.get((previous_state, stage, decision))
    if transition is None:
        raise ValueError("revalidation receipt encodes a skipped or rollback transition")
    if decision == "pass" and reason != "passed":
        raise ValueError("T-90 pass receipt reason is invalid")
    if decision == "confirm" and reason != "confirmed":
        raise ValueError("T-30 confirmation receipt reason is invalid")
    if decision == "cancel" and reason in {"passed", "confirmed"}:
        raise ValueError("cancellation receipt reason is invalid")
    stake = _integral(receipt.get("final_stake"), "receipt final stake")
    if decision == "cancel" and stake != 0:
        raise ValueError("cancelled receipt stake is invalid")
    return transition, stake


def _validate_initial_t90_receipt(
    root: Path, target_date: date, source: dict, candidate: dict
) -> dict:
    generation_id = source.get("generation_id")
    relative = candidate.get("t90_receipt_path")
    expected_sha = candidate.get("t90_receipt_sha256")
    if not _valid_sha256(generation_id) or not isinstance(relative, str) or not _valid_sha256(expected_sha):
        raise ValueError("initial T-90 receipt provenance is invalid")
    expected_path = (
        root / "output" / "provisional_generations" / target_date.isoformat()
        / generation_id / "receipts" / f"{candidate['candidate_id']}-t90.json"
    ).resolve()
    path = (root / relative).resolve()
    if path != expected_path or _relative_path(root, path) != relative:
        raise ValueError("initial T-90 receipt path is invalid")
    try:
        raw = path.read_bytes()
        receipt = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("initial T-90 receipt is invalid") from exc
    if raw != _canonical_bytes(receipt) or _sha256_bytes(raw) != expected_sha:
        raise ValueError("initial T-90 receipt digest or serialization is invalid")
    decision_snapshot = receipt.get("decision_snapshot")
    decision_sha = receipt.get("decision_snapshot_sha256")
    published = _legacy_published_candidate(candidate)
    attested_candidate = published if published is not None else candidate
    initial_candidate = {
        key: value for key, value in attested_candidate.items()
        if key not in {
            "candidate_payload_sha256", "initial_candidate_attestation_sha256",
            "t90_receipt_path", "t90_receipt_sha256",
        }
    }
    expected_attestation = _sha256({
        "candidate_id": candidate["candidate_id"],
        "initial_candidate": initial_candidate,
        "decision_snapshot_sha256": decision_sha,
    })
    if (
        receipt.get("schema_version")
        != (1 if published is not None else PROVISIONAL_SCHEMA_VERSION)
        or receipt.get("receipt_type") != "t90_initial_snapshot"
        or receipt.get("candidate_id") != candidate["candidate_id"]
        or not isinstance(decision_snapshot, dict)
        or decision_sha != _sha256(decision_snapshot)
        or candidate.get("initial_candidate_attestation_sha256") != expected_attestation
        or receipt.get("initial_candidate_attestation_sha256") != expected_attestation
    ):
        raise ValueError("initial T-90 receipt candidate binding is invalid")
    _aware(receipt.get("generated_at_bjt"), "initial T-90 generated_at_bjt")
    return receipt


def _bind_legacy_execution_identity(receipt: dict, candidate: dict) -> None:
    if _legacy_published_candidate(candidate) is not None:
        receipt["execution_identity_sha256"] = _sha256(
            candidate["execution_identity"]
        )


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("conflicting revalidation receipt already exists")


def _write_state_atomic(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = _canonical_bytes(state)
    lock_path = path.with_name(path.name + ".lock")
    with _exclusive_file_lock(lock_path):
        for stale in path.parent.glob(f".{path.name}.*.tmp"):
            stale.unlink(missing_ok=True)
        if path.exists():
            try:
                current_bytes = path.read_bytes()
                current = json.loads(current_bytes.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("existing revalidation state is invalid") from exc
            if current_bytes != _canonical_bytes(current):
                raise ValueError("existing revalidation state is not canonical")
            if current_bytes == serialized:
                return
            _require_monotonic_state_write(current, state)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def _require_monotonic_state_write(current: dict, proposed: dict) -> None:
    if (
        current.get("schema_version") != proposed.get("schema_version")
        or current.get("report_date") != proposed.get("report_date")
        or not isinstance(current.get("candidates"), list)
        or not isinstance(proposed.get("candidates"), list)
    ):
        raise ValueError("revalidation state write is not monotonic")
    current_by_id = {
        entry.get("candidate", {}).get("candidate_id"): entry
        for entry in current["candidates"]
        if isinstance(entry, dict) and isinstance(entry.get("candidate"), dict)
    }
    proposed_by_id = {
        entry.get("candidate", {}).get("candidate_id"): entry
        for entry in proposed["candidates"]
        if isinstance(entry, dict) and isinstance(entry.get("candidate"), dict)
    }
    if (
        None in current_by_id
        or None in proposed_by_id
        or len(current_by_id) != len(current["candidates"])
        or len(proposed_by_id) != len(proposed["candidates"])
        or current_by_id.keys() != proposed_by_id.keys()
    ):
        raise ValueError("revalidation state write is not monotonic")

    allowed_state_progress = {
        ("provisional", "screened"),
        ("provisional", "cancelled"),
        ("screened", "confirmed"),
        ("screened", "cancelled"),
    }
    ledger_rank = {"not_applicable": 0, "pending": 1, "ingested": 2}
    for candidate_id, old_entry in current_by_id.items():
        new_entry = proposed_by_id[candidate_id]
        if old_entry["candidate"] != new_entry["candidate"]:
            raise ValueError("revalidation state candidate changed during write")
        old_state = old_entry.get("state")
        new_state = new_entry.get("state")
        if old_state != new_state and (old_state, new_state) not in allowed_state_progress:
            raise ValueError("revalidation state rollback is not permitted")
        old_ledger = old_entry.get("ledger_status")
        new_ledger = new_entry.get("ledger_status")
        if old_ledger not in ledger_rank or new_ledger not in ledger_rank:
            raise ValueError("revalidation ledger status is invalid")
        if old_state == new_state and ledger_rank[new_ledger] < ledger_rank[old_ledger]:
            raise ValueError("revalidation ledger status rollback is not permitted")
        for field in (
            "t90_receipt_path",
            "t90_receipt_sha256",
            "t30_receipt_path",
            "t30_receipt_sha256",
        ):
            old_value = old_entry.get(field) or ""
            new_value = new_entry.get(field) or ""
            if old_value and old_value != new_value:
                raise ValueError("revalidation receipt evidence rollback is not permitted")


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


def _state_path(root: Path, target_date: date) -> Path:
    return root / "output" / f"revalidation_state_{target_date.isoformat()}.json"


def _receipt_path(root: Path, target_date: date, candidate_id: str, stage: str) -> Path:
    return root / "output" / "revalidation_receipts" / target_date.isoformat() / f"{candidate_id}-{stage}.json"


def _earliest_kickoff(candidate: dict) -> datetime:
    execution_identity = candidate.get("execution_identity") if isinstance(candidate, dict) else None
    execution_legs = execution_identity.get("legs") if isinstance(execution_identity, dict) else None
    if isinstance(execution_legs, list) and execution_legs:
        return min(
            _aware(leg.get("kickoff_at_bjt"), "execution leg kickoff")
            for leg in execution_legs
            if isinstance(leg, dict)
        )
    legs = candidate.get("legs") if isinstance(candidate, dict) else None
    if isinstance(legs, list) and legs:
        return min(_aware(leg.get("kickoff_at_bjt", leg.get("kickoff_at")), "leg kickoff") for leg in legs if isinstance(leg, dict))
    return _aware(candidate.get("earliest_kickoff_at_bjt"), "earliest kickoff")


def _aware(value: object, name: str) -> datetime:
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
    return parsed.astimezone(BEIJING)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} is invalid")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} is invalid")
    return parsed


def _integral(value: object, name: str) -> int:
    parsed = _decimal(value, name)
    if parsed != parsed.to_integral_value():
        raise ValueError(f"{name} must be integral")
    return int(parsed)


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _sha256(value: object) -> str:
    return _sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("path escapes repository root") from exc


def _parse_cli_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target date must be YYYY-MM-DD") from exc
    if value != parsed.isoformat():
        raise argparse.ArgumentTypeError("target date must be YYYY-MM-DD")
    return parsed


def _parse_cli_now(value: str) -> datetime:
    try:
        return _aware(value, "now_bjt")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Run due pre-kickoff revalidations.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_due = subparsers.add_parser("run-due")
    run_due.add_argument("--target-date", type=_parse_cli_date)
    run_due.add_argument("--now-bjt", type=_parse_cli_now, required=True)
    args = parser.parse_args()
    try:
        changes = run_due_revalidation(
            Path.cwd(),
            args.now_bjt,
            [args.target_date] if args.target_date else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    changed_dates = sorted(
        {
            change["receipt"]["target_date"]
            for change in changes
            if isinstance(change.get("receipt"), dict)
            and isinstance(change["receipt"].get("target_date"), str)
        }
    )
    print(json.dumps({"changed_dates": changed_dates}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
