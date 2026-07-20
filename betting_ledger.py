import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from official_markets import THREE_WAY_SELECTIONS, TOTAL_GOALS_SELECTIONS, parse_handicap
from plan_lock import read_valid_lock


PENDING = "未结算"
WON = "命中"
LOST = "未中"
REFUNDED = "退款"
ABNORMAL = "异常"

DOMESTIC_ODDS_SOURCES = frozenset({"sporttery", "zgzcw"})
VALUE_V4_SINGLE_MARKETS = frozenset({"had", "hhad", "ttg"})
VALUE_V4_PLAY_BY_MARKET = {"had": "HAD", "hhad": "HHAD", "ttg": "TTG"}
VALUE_V4_THREE_WAY_SELECTIONS = frozenset(THREE_WAY_SELECTIONS.values())
VALUE_V4_TOTAL_GOALS_SELECTIONS = frozenset(TOTAL_GOALS_SELECTIONS.values())
TERMINAL_STATUSES = frozenset({WON, LOST, REFUNDED})
MONEY_ZERO = Decimal("0.00")
MONEY_QUANTUM = Decimal("0.01")
PAID_STAKE_UNIT = Decimal("2")
HARD_DAILY_STAKE = Decimal("500")
HARD_MONTHLY_STAKE = Decimal("5000")
HARD_MONTHLY_STOP_LOSS = Decimal("5000")
HARD_MATCH_EXPOSURE = Decimal("200")
HARD_PARLAY_STAKE = Decimal("30")
HARD_SINGLE_STAKE = Decimal("200")
HARD_SINGLE_BUDGET = Decimal("200")
HARD_SINGLE_COUNT = 2
HARD_PARLAY_COUNT = 1
MAX_VALUE_V4_KELLY = Decimal("0.25")
NEW_PAID_STRATEGY_VERSIONS = frozenset({"legacy-v3", "value-v4"})
CANONICAL_PAID_CUTOVER_DATE = date(2026, 7, 18)

LEDGER_FIELD_ORDER = (
    "bet_id", "date", "report_date", "strategy_version", "model_version",
    "locked_at_bjt", "plan_sha256", "row_payload_sha256", "match_id", "team_a", "team_b", "kickoff_local",
    "play", "market_type", "market_line", "selection", "legs_json",
    "canonical_legs_json", "odds_source", "odds_source_record_id",
    "odds_captured_at_bjt", "locked_odds", "odds", "raw_probability",
    "calibrated_probability", "official_market_probability",
    "conservative_probability", "edge", "net_ev", "full_kelly",
    "kelly_fraction", "data_quality_multiplier", "volatility_multiplier",
    "performance_multiplier", "portfolio_rank", "binding_limits", "stake", "data_quality",
    "volatility_band", "status", "result_status", "result_source",
    "source_record_id", "captured_at_bjt", "score_scope", "settlement_minutes",
    "home_goals", "away_goals",
    "settled_at_bjt", "return", "profit", "result_legs_json", "clv",
    "evidence_type", "candidate_id", "candidate_payload_sha256",
    "t90_receipt_path", "t90_receipt_sha256",
    "t30_receipt_path", "t30_receipt_sha256",
    "live_odds_snapshot_path", "live_odds_snapshot_sha256",
    "final_confirmed_at_bjt",
)
REQUIRED_FIELD_ORDER = LEDGER_FIELD_ORDER

LEDGER_TRANSACTION_SCHEMA_VERSION = 1
LEDGER_TRANSACTION_NAMES = (
    "betting_ledger.csv",
    "observation_ledger.csv",
)
LEDGER_TRANSACTION_MANIFEST = "ledger_transaction_manifest.json"
LEDGER_GENERATION_DIRECTORY = "ledger_generations"
LEDGER_FIXED_TRANSACTION_NAMES = ("revalidation_rehearsal_ledger.csv",)
_UNSPECIFIED_GENERATION = object()

ROW_PAYLOAD_SCHEMA_VERSION = 2
LEGACY_ROW_PAYLOAD_SCHEMA_VERSION = 1
IMMUTABLE_ROW_PAYLOAD_FIELDS = (
    "bet_id", "date", "report_date", "strategy_version", "model_version",
    "locked_at_bjt", "plan_sha256", "stage", "match", "match_id", "team_a",
    "team_b", "kickoff_local", "play", "market_type", "market_line",
    "selection", "legs_json", "canonical_legs_json", "odds_source",
    "odds_source_record_id", "odds_captured_at_bjt", "locked_odds", "odds",
    "probability", "raw_probability", "raw_model_probability",
    "calibrated_probability", "league_calibrated_probability",
    "league_calibration_samples", "official_market_probability",
    "market_probability", "conservative_probability", "edge", "value_edge",
    "net_ev", "expected_value", "stake", "expected_return", "expected_profit",
    "full_kelly", "kelly_fraction", "data_quality_multiplier",
    "volatility_multiplier", "performance_multiplier", "portfolio_rank",
    "binding_limits", "data_quality", "volatility_band", "reason",
    "evidence_type", "candidate_id", "candidate_payload_sha256",
    "t90_receipt_path", "t90_receipt_sha256",
    "t30_receipt_path", "t30_receipt_sha256",
    "live_odds_snapshot_path", "live_odds_snapshot_sha256",
    "final_confirmed_at_bjt",
)


def stable_bet_id(plan_row: dict) -> str:
    """Return the deterministic identity for one valid plan row."""
    if not isinstance(plan_row, dict):
        raise ValueError("plan row must be a mapping")
    return _hash_identity(_identity_payload(plan_row))


def ingest_locked_plan(
    existing_rows: list[dict],
    plan_rows: list[dict],
    lock: dict,
    *,
    canonical_evidence: dict[tuple[str, str], str],
) -> list[dict]:
    """Migrate legacy rows and append only previously unseen locked plan identities."""
    lock_source = _validate_lock(lock)
    locked_at = _aware_datetime(lock["locked_at_bjt"], "lock locked_at_bjt")
    if not isinstance(existing_rows, list) or not isinstance(plan_rows, list):
        raise ValueError("ledger and plan rows must be lists")

    ingested, known_keys = _normalize_existing_rows(
        existing_rows, canonical_evidence=canonical_evidence
    )

    new_rows: list[tuple[dict, str]] = []
    plan_ids: set[str] = set()
    for source_row in plan_rows:
        if not isinstance(source_row, dict):
            raise ValueError("plan row must be a mapping")
        if source_row.get("date") != lock["report_date"]:
            raise ValueError("plan date must match lock report_date")
        bet_id = stable_bet_id(source_row)
        if bet_id in plan_ids:
            raise ValueError("duplicate canonical identity in locked plan")
        plan_ids.add(bet_id)
        if ("canonical", bet_id) in known_keys:
            continue
        new_rows.append((source_row, bet_id))

    if not new_rows:
        return ingested

    _validate_new_paid_rows(
        ingested,
        [row for row, _bet_id in new_rows],
        lock_source,
        lock["report_date"],
        locked_at,
    )
    for source_row, bet_id in new_rows:
        ingested.append(_new_locked_row(source_row, lock, lock_source, bet_id))
    return ingested


def _validate_new_paid_rows(
    existing_rows: list[dict],
    new_rows: list[dict],
    lock_source: str,
    report_date: str,
    locked_at: datetime,
) -> None:
    new_stakes: dict[int, Decimal] = {}
    for row in new_rows:
        strategy_version = _required_text(
            row.get("strategy_version"), "strategy_version"
        )
        if strategy_version not in NEW_PAID_STRATEGY_VERSIONS:
            raise ValueError("strategy_version is not permitted for new paid rows")
        stake = _paid_stake(row.get("stake"))
        new_stakes[id(row)] = stake
        source = _required_text(row.get("odds_source"), "odds_source").lower()
        if source not in DOMESTIC_ODDS_SOURCES or source != lock_source:
            raise ValueError("paid row odds source must match the domestic lock source")
        _required_text(row.get("odds_source_record_id"), "odds_source_record_id")
        _capture_not_after_lock(
            row.get("odds_captured_at_bjt"), "odds_captured_at_bjt", locked_at
        )
        display_odds = _decimal_odds(row.get("odds"), "odds")
        locked_odds = _decimal_odds(row.get("locked_odds"), "locked_odds")
        if not _is_parlay(row) and display_odds != locked_odds:
            raise ValueError("single odds and locked_odds must be exactly equal")
        if strategy_version == "value-v4":
            _value_v4_kelly(row.get("kelly_fraction"))

    _validate_paid_portfolio(new_rows, lock_source, locked_at)
    _validate_paid_account_caps(
        existing_rows, new_rows, new_stakes, report_date
    )


def _validate_paid_account_caps(
    existing_rows: list[dict],
    new_rows: list[dict],
    new_stakes: dict[int, Decimal],
    report_date: str,
) -> None:
    target = date.fromisoformat(report_date)
    monthly_existing = [
        row for row in existing_rows if _row_month(row) == (target.year, target.month)
    ]
    daily_existing = [
        row for row in monthly_existing if _row_date(row) == target
    ]
    new_total = sum(new_stakes.values(), MONEY_ZERO)
    daily_total = sum((_account_stake(row) for row in daily_existing), MONEY_ZERO) + new_total
    if daily_total > HARD_DAILY_STAKE:
        raise ValueError("daily paid stake exceeds 500")

    monthly_total = sum(
        (_account_stake(row) for row in monthly_existing), MONEY_ZERO
    ) + new_total
    if monthly_total > HARD_MONTHLY_STAKE:
        raise ValueError("monthly paid stake exceeds 5000")

    realized_profit = sum(
        (
            _account_decimal(row.get("profit"))
            for row in monthly_existing
            if row.get("status") in TERMINAL_STATUSES
        ),
        MONEY_ZERO,
    )
    if realized_profit <= -HARD_MONTHLY_STOP_LOSS:
        raise ValueError("monthly stop loss blocks new paid stake")

    daily_rows = [(row, _account_stake(row)) for row in daily_existing]
    daily_rows.extend((row, new_stakes[id(row)]) for row in new_rows)
    parlays = [(row, stake) for row, stake in daily_rows if _is_parlay(row)]
    singles = [(row, stake) for row, stake in daily_rows if not _is_parlay(row)]
    if any(stake > HARD_SINGLE_STAKE for _row, stake in singles):
        raise ValueError("single stake exceeds 200")

    match_exposure: dict[str, Decimal] = {}
    for row, stake in daily_rows:
        for match_id in _account_match_ids(row):
            match_exposure[match_id] = match_exposure.get(match_id, MONEY_ZERO) + stake
    if any(exposure > HARD_MATCH_EXPOSURE for exposure in match_exposure.values()):
        raise ValueError("match exposure exceeds 200")

    if sum((stake for _row, stake in parlays), MONEY_ZERO) > HARD_PARLAY_STAKE:
        raise ValueError("parlay stake exceeds 30")
    if len(parlays) > HARD_PARLAY_COUNT:
        raise ValueError("parlay count exceeds 1")
    if len(singles) > HARD_SINGLE_COUNT:
        raise ValueError("single count exceeds 2")
    if sum((stake for _row, stake in singles), MONEY_ZERO) > HARD_SINGLE_BUDGET:
        raise ValueError("single budget exceeds 200")


def _paid_stake(value: object) -> Decimal:
    stake = _required_decimal(value, "stake")
    if stake <= MONEY_ZERO:
        raise ValueError("stake must be positive")
    if stake % PAID_STAKE_UNIT != MONEY_ZERO:
        raise ValueError("stake must use exact 2-yuan units")
    return stake


def _value_v4_kelly(value: object) -> Decimal:
    kelly = _required_decimal(value, "Kelly fraction")
    if kelly <= MONEY_ZERO or kelly > MAX_VALUE_V4_KELLY:
        raise ValueError("value-v4 Kelly fraction must be positive and at most 0.25")
    return kelly


def _required_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite decimal")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite decimal")
    return result


def _row_date(row: dict) -> date | None:
    value = row.get("report_date") or row.get("date")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _row_month(row: dict) -> tuple[int, int] | None:
    parsed = _row_date(row)
    return None if parsed is None else (parsed.year, parsed.month)


def _account_decimal(value: object) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return MONEY_ZERO
    return amount if amount.is_finite() else MONEY_ZERO


def _account_stake(row: dict) -> Decimal:
    stake = _account_decimal(row.get("stake"))
    return stake if stake > MONEY_ZERO else MONEY_ZERO


def _account_match_ids(row: dict) -> list[str]:
    try:
        if _is_parlay(row):
            try:
                legs = _canonical_legs(row, allow_legacy_match=True)
            except ValueError:
                canonical_row = dict(row)
                canonical_row.pop("legs", None)
                canonical_row["legs_json"] = row.get("canonical_legs_json")
                legs = _canonical_legs(canonical_row, allow_legacy_match=True)
            return sorted({leg["match_id"] for leg in legs})
        return [_canonical_match_id(row.get("match_id"), allow_legacy_match=True)]
    except ValueError:
        return []


def _validate_paid_portfolio(
    plan_rows: list[dict], lock_source: str, locked_at: datetime
) -> None:
    parlays = 0
    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        strategy_version = _required_text(
            row.get("strategy_version"), "strategy_version"
        )
        if strategy_version not in NEW_PAID_STRATEGY_VERSIONS:
            continue
        market_type = _required_text(row.get("market_type"), "market_type").lower()
        _required_text(row.get("odds_source_record_id"), "odds_source_record_id")
        _aware_iso(row.get("odds_captured_at_bjt"), "odds_captured_at_bjt")
        if market_type == "parlay":
            parlays += 1
            legs = _canonical_legs(row)
            raw_legs = _raw_legs(row)
            if len({leg["match_id"] for leg in legs}) != 2:
                raise ValueError(
                    f"{strategy_version} parlay legs must use distinct matches"
                )
            for leg in legs:
                if leg["market_type"] not in VALUE_V4_SINGLE_MARKETS:
                    raise ValueError(
                        f"{strategy_version} parlay leg market is unsupported"
                    )
                _validate_paid_market(
                    leg["market_type"], leg["selection"], leg["line"],
                    "parlay leg", strategy_version,
                )
            combined_odds = Decimal("1")
            for leg in raw_legs:
                source = _required_text(
                    leg.get("odds_source"), "parlay leg odds_source"
                ).lower()
                if source not in DOMESTIC_ODDS_SOURCES or source != lock_source:
                    raise ValueError(
                        f"{strategy_version} parlay leg source must match the lock"
                    )
                _required_text(
                    leg.get("odds_source_record_id"),
                    "parlay leg odds_source_record_id",
                )
                _capture_not_after_lock(
                    leg.get("odds_captured_at_bjt"),
                    "parlay leg odds_captured_at_bjt",
                    locked_at,
                )
                combined_odds *= _decimal_odds(leg.get("odds"), "parlay leg odds")
            locked_odds = _decimal_odds(row.get("locked_odds"), "locked_odds")
            display_odds = _decimal_odds(row.get("odds"), "odds")
            if combined_odds != locked_odds or locked_odds != display_odds:
                raise ValueError(
                    f"{strategy_version} parlay odds must equal the exact leg product"
                )
            continue
        if market_type not in VALUE_V4_SINGLE_MARKETS:
            raise ValueError(f"{strategy_version} single market is unsupported")
        if (
            strategy_version == "value-v4"
            and _required_text(row.get("play"), "play")
            != VALUE_V4_PLAY_BY_MARKET[market_type]
        ):
            raise ValueError("value-v4 play must match market_type")
        _canonical_match_id(row.get("match_id"))
        selection = _required_text(row.get("selection"), "selection")
        line = _line_value(row.get("market_line", row.get("line", "")))
        _validate_paid_market(
            market_type, selection, line, "single", strategy_version
        )
    if parlays > 1:
        raise ValueError("paid parlay count exceeds 1")


def _validate_paid_market(
    market_type: str,
    selection: str,
    line: str,
    context: str,
    strategy_version: str,
) -> None:
    if market_type in {"had", "hhad"}:
        if selection not in VALUE_V4_THREE_WAY_SELECTIONS:
            raise ValueError(
                f"{strategy_version} {context} selection is unsupported"
            )
    elif selection not in VALUE_V4_TOTAL_GOALS_SELECTIONS:
        raise ValueError(
            f"{strategy_version} {context} selection is unsupported"
        )

    if market_type == "hhad":
        try:
            parse_handicap(line)
        except ValueError as exc:
            raise ValueError(
                f"{strategy_version} HHAD {context} requires an integer handicap"
            ) from exc
    elif line:
        raise ValueError(
            f"{strategy_version} HAD/TTG {context} cannot have a line"
        )


def _decimal_odds(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite decimal odds greater than one")
    try:
        odds = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be finite decimal odds greater than one") from exc
    if not odds.is_finite() or odds <= Decimal("1"):
        raise ValueError(f"{name} must be finite decimal odds greater than one")
    return odds


def update_observation_ledger(
    existing_rows: list[dict],
    plan_rows: list[dict],
    results: dict,
    settled_at: datetime,
) -> list[dict]:
    """Append canonical zero-stake observations and settle them canonically."""
    if not isinstance(existing_rows, list) or not isinstance(plan_rows, list):
        raise ValueError("observation ledger and plan rows must be lists")
    if not isinstance(results, dict):
        raise ValueError("observation results must be a mapping")

    rows: list[dict] = []
    known_ids: set[str] = set()
    for source_row in existing_rows:
        if not isinstance(source_row, dict):
            raise ValueError("existing observation row must be a mapping")
        row = dict(source_row)
        bet_id = row.get("bet_id")
        if isinstance(bet_id, str) and bet_id:
            known_ids.add(bet_id)
        rows.append(row)

    for source_row in plan_rows:
        bet_id = _validate_new_observation(source_row)
        if bet_id in known_ids:
            continue
        known_ids.add(bet_id)
        rows.append(_new_observation_row(source_row, bet_id))
    return settle_pending(rows, results, settled_at)


def _validate_new_observation(row: dict) -> str:
    if not isinstance(row, dict):
        raise ValueError("observation plan row must be a mapping")
    if _required_text(row.get("strategy_version"), "strategy_version") != "value-v4":
        raise ValueError("observation strategy_version must be value-v4")
    if _required_decimal(row.get("stake"), "observation stake") != MONEY_ZERO:
        raise ValueError("observation row must have zero stake")

    market_type = _required_text(row.get("market_type"), "market_type").lower()
    if market_type not in VALUE_V4_SINGLE_MARKETS:
        raise ValueError("observation market is unsupported")
    if _required_text(row.get("play"), "play") != VALUE_V4_PLAY_BY_MARKET[market_type]:
        raise ValueError("observation play must match market_type")
    _canonical_match_id(row.get("match_id"))
    selection = _required_text(row.get("selection"), "selection")
    line = _line_value(row.get("market_line", row.get("line", "")))
    _validate_paid_market(
        market_type, selection, line, "observation", "value-v4"
    )

    source = _required_text(row.get("odds_source"), "odds_source").lower()
    if source not in DOMESTIC_ODDS_SOURCES:
        raise ValueError("observation odds source must be domestic")
    _required_text(row.get("odds_source_record_id"), "odds_source_record_id")
    _aware_iso(row.get("odds_captured_at_bjt"), "odds_captured_at_bjt")
    display_odds = _decimal_odds(row.get("odds"), "odds")
    locked_odds = _decimal_odds(row.get("locked_odds"), "locked_odds")
    if display_odds != locked_odds:
        raise ValueError("observation odds and locked_odds must be exactly equal")

    bet_id = stable_bet_id(row)
    provided_id = row.get("bet_id")
    if provided_id not in (None, "") and provided_id != bet_id:
        raise ValueError("observation bet_id must equal its canonical stable bet_id")
    return bet_id


def _new_observation_row(source_row: dict, bet_id: str) -> dict:
    row = dict(source_row)
    row["bet_id"] = bet_id
    row["status"] = PENDING
    for field in (
        "result_status", "result_source", "source_record_id", "captured_at_bjt",
        "score_scope", "settlement_minutes", "home_goals", "away_goals",
        "settled_at_bjt", "result_legs_json", "clv",
    ):
        row[field] = ""
    row["return"] = "0.00"
    row["profit"] = "0.00"
    return row


def settle_pending(
    rows: list[dict],
    results: dict,
    settled_at: datetime,
    *,
    allow_correction: bool = False,
) -> list[dict]:
    """Settle only proven pending rows; terminal rows are intentionally immutable."""
    if not isinstance(rows, list) or not isinstance(results, dict):
        raise ValueError("rows and results must be mappings in lists/dicts")
    settled_time = _aware_iso(settled_at, "settled_at")
    updated: list[dict] = []
    for source_row in rows:
        row = dict(source_row)
        if row.get("strategy_version") in NEW_PAID_STRATEGY_VERSIONS:
            stake = _required_decimal(row.get("stake"), "canonical stake")
            if stake > MONEY_ZERO:
                _verify_row_payload_digest(row)
        status = row.get("status", PENDING)
        if allow_correction:
            if status == ABNORMAL:
                correction_match_id = _abnormal_match_id(row)
                correction = _result_for(correction_match_id, results)
                if _is_proven_result(correction):
                    new_record_id = correction["source_record_id"]
                    if new_record_id != row.get("source_record_id"):
                        row["status"] = PENDING
            updated.append(row)
            continue
        if status in TERMINAL_STATUSES:
            updated.append(row)
            continue
        if status == ABNORMAL:
            updated.append(row)
            continue
        if status != PENDING:
            updated.append(row)
            continue

        if _is_parlay(row):
            updated.append(_settle_parlay(row, results, settled_time))
        else:
            updated.append(_settle_single(row, results, settled_time))
    return updated


def settled_market_identities(row: dict) -> list[dict]:
    """Return canonical market identities only for proven terminal value-v4 rows."""
    if (
        not isinstance(row, dict)
        or row.get("strategy_version") != "value-v4"
        or row.get("status") not in TERMINAL_STATUSES
    ):
        return []
    try:
        if _required_text(row.get("bet_id"), "bet_id") != stable_bet_id(row):
            return []
        _validate_maturity_economics(row)
        _aware_iso(row.get("settled_at_bjt"), "settled_at_bjt")
        market_type = _required_text(row.get("market_type"), "market_type").lower()
        if market_type == "parlay":
            return _settled_parlay_identities(row)
        if market_type not in VALUE_V4_SINGLE_MARKETS:
            return []
        if _required_text(row.get("play"), "play") != VALUE_V4_PLAY_BY_MARKET[market_type]:
            return []
        identity = {
            "match_id": _canonical_match_id(row.get("match_id")),
            "market_type": market_type,
            "selection": _required_text(row.get("selection"), "selection"),
            "line": _line_value(row.get("market_line", row.get("line", ""))),
        }
        _validate_paid_market(
            identity["market_type"], identity["selection"], identity["line"],
            "settled single", "value-v4",
        )
        if not _is_proven_result(row):
            return []
        expected_status = _single_terminal_status(identity, row)
        return [identity] if row.get("status") == expected_status else []
    except (TypeError, ValueError):
        return []


def _validate_maturity_economics(row: dict) -> None:
    stake = _required_decimal(row.get("stake"), "stake")
    locked_at = _aware_datetime(row.get("locked_at_bjt"), "locked_at_bjt")
    if stake == MONEY_ZERO:
        _validate_new_observation(row)
        _capture_not_after_lock(
            row.get("odds_captured_at_bjt"), "odds_captured_at_bjt", locked_at
        )
        return
    lock_source = _required_text(row.get("odds_source"), "odds_source").lower()
    _validate_new_paid_rows(
        [], [row], lock_source, _identity_date(row), locked_at
    )


def _settled_parlay_identities(row: dict) -> list[dict]:
    legs = _canonical_legs(row)
    if len({leg["match_id"] for leg in legs}) != 2:
        return []
    for leg in legs:
        if leg["market_type"] not in VALUE_V4_SINGLE_MARKETS:
            return []
        _validate_paid_market(
            leg["market_type"], leg["selection"], leg["line"],
            "settled parlay leg", "value-v4",
        )

    raw_result_legs = row.get("result_legs_json")
    if not isinstance(raw_result_legs, str) or not raw_result_legs:
        return []
    try:
        result_legs = json.loads(raw_result_legs)
    except json.JSONDecodeError:
        return []
    if not isinstance(result_legs, list) or len(result_legs) != 2:
        return []
    if _canonical_legs({"legs": result_legs}) != legs:
        return []

    all_refunded = True
    any_loss = False
    for leg, result in zip(legs, _sorted_result_legs(result_legs)):
        if not _is_proven_result(result):
            return []
        if result.get("result_status") == "refunded":
            continue
        all_refunded = False
        outcome = _outcome(
            leg["market_type"], leg["selection"], leg["line"], result
        )
        if outcome is None:
            return []
        any_loss = any_loss or not outcome

    aggregate = _parlay_provenance(result_legs)
    if any(
        row.get(field) != aggregate[field]
        for field in (
            "result_status", "result_source", "source_record_id", "captured_at_bjt",
            "score_scope", "settlement_minutes",
        )
    ):
        return []
    expected_status = REFUNDED if all_refunded else LOST if any_loss else WON
    return legs if row.get("status") == expected_status else []


def _single_terminal_status(identity: dict, result: dict) -> str | None:
    if result.get("result_status") == "refunded":
        return REFUNDED
    outcome = _outcome(
        identity["market_type"], identity["selection"], identity["line"], result
    )
    if outcome is None:
        return None
    return WON if outcome else LOST


def _sorted_result_legs(result_legs: list[dict]) -> list[dict]:
    return sorted(
        result_legs,
        key=lambda result: json.dumps(
            {
                "match_id": _canonical_match_id(result.get("match_id")),
                "market_type": _required_text(
                    result.get("market_type"), "leg market_type"
                ).lower(),
                "selection": _required_text(result.get("selection"), "leg selection"),
                "line": _canonical_market_line(
                    _required_text(
                        result.get("market_type"), "leg market_type"
                    ).lower(),
                    result.get("line", result.get("market_line", "")),
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def write_ledger_atomic(path: Path, rows: list[dict]) -> Path:
    """Persist a deterministic UTF-8-SIG ledger without exposing partial files."""
    path = Path(path)
    if _is_physical_generation_path(path):
        raise ValueError("public writes to physical ledger generations are forbidden")
    payload = _ledger_csv_bytes(rows)
    if path.name in LEDGER_TRANSACTION_NAMES:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _ledger_commit_lock(path.parent):
            if (path.parent / LEDGER_TRANSACTION_MANIFEST).exists():
                raise ValueError(
                    "manifest-backed ledger writes require a generation transaction"
                )
            _write_bytes_atomic(path, payload)
        return path
    _write_bytes_atomic(path, payload)
    return path


def update_observation_ledger_atomic(
    path: Path,
    plan_rows: list[dict],
    results: dict,
    settled_at: datetime,
) -> Path:
    """Append and settle observations against one locked ledger generation."""
    logical = Path(path)
    if logical.name != "observation_ledger.csv":
        raise ValueError("observation ledger path is invalid")

    def mutate(_generation_id: str | None, current: dict[str, list[dict]]):
        return {
            logical.name: update_observation_ledger(
                current[logical.name], plan_rows, results, settled_at
            )
        }

    return _mutate_ledger_rows(logical.parent, (logical.name,), mutate)[logical.name]


def _mutate_ledger_rows(output: Path, names: tuple[str, ...], mutate) -> dict[str, Path]:
    """Read, validate, mutate, and commit ledger rows under one generation lock."""
    output = Path(output)
    destinations = tuple(dict.fromkeys(names))
    allowed = set(LEDGER_TRANSACTION_NAMES) | set(LEDGER_FIXED_TRANSACTION_NAMES)
    if not destinations or any(name not in allowed for name in destinations):
        raise ValueError("ledger transaction destinations are invalid")

    with _ledger_commit_lock(output):
        manifest = _read_ledger_manifest(output)
        generation_id = None if manifest is None else manifest["generation_id"]
        current = {
            name: _read_ledger_rows_locked(output, manifest, name)
            for name in destinations
        }
        updates = mutate(generation_id, current)
        if not isinstance(updates, dict) or not updates:
            raise ValueError("ledger transaction produced no updates")
        if any(name not in destinations for name in updates):
            raise ValueError("ledger transaction updated an unread destination")

        generation_updates = {
            name: _ledger_csv_bytes(rows)
            for name, rows in updates.items()
            if name in LEDGER_TRANSACTION_NAMES
        }
        fixed_updates = {
            name: _ledger_csv_bytes(rows)
            for name, rows in updates.items()
            if name in LEDGER_FIXED_TRANSACTION_NAMES
        }
        if generation_updates and fixed_updates:
            raise ValueError("fixed and generated ledgers cannot share a transaction")
        if generation_updates:
            _commit_ledger_generation_locked(
                output,
                generation_updates,
                expected_generation_id=generation_id,
            )
        else:
            for name, payload in fixed_updates.items():
                _write_bytes_atomic(output / name, payload)
    return {name: output / name for name in updates}


def _read_ledger_rows_locked(
    output: Path, manifest: dict | None, name: str
) -> list[dict]:
    if name in LEDGER_TRANSACTION_NAMES and manifest is not None:
        path = _manifest_destination_path(output, manifest, name)
    else:
        path = output / name
    return _read_csv_file(path)


def _is_physical_generation_path(path: Path) -> bool:
    marker = LEDGER_GENERATION_DIRECTORY.casefold()
    supplied_parts = {part.casefold() for part in Path(path).parts}
    resolved_parts = {
        part.casefold() for part in Path(path).resolve(strict=False).parts
    }
    return marker in supplied_parts or marker in resolved_parts


def resolve_ledger_path(path: Path) -> Path:
    """Resolve a logical ledger path through the one committed generation."""
    logical = Path(path)
    if logical.name not in LEDGER_TRANSACTION_NAMES:
        return logical
    manifest = _read_ledger_manifest(logical.parent)
    if manifest is None:
        return logical
    return _manifest_destination_path(logical.parent, manifest, logical.name)


def _ledger_csv_bytes(rows: list[dict]) -> bytes:
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    unknown_fields = sorted({key for row in rows if isinstance(row, dict) for key in row} - set(REQUIRED_FIELD_ORDER))
    fieldnames = [*REQUIRED_FIELD_ORDER, *unknown_fields]
    with io.StringIO(newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for source_row in rows:
            if not isinstance(source_row, dict):
                raise ValueError("ledger row must be a mapping")
            writer.writerow(
                {
                    field: _csv_value(source_row.get(field, ""))
                    for field in fieldnames
                }
            )
        return b"\xef\xbb\xbf" + handle.getvalue().encode("utf-8")


def _commit_ledger_generation(
    output: Path, rows_by_name: dict[str, list[dict]]
) -> dict[str, Path]:
    payloads = {
        name: _ledger_csv_bytes(rows) for name, rows in rows_by_name.items()
    }
    with _ledger_commit_lock(output):
        return _commit_ledger_generation_locked(output, payloads)


def _commit_ledger_generation_locked(
    output: Path,
    updates: dict[str, bytes],
    *,
    expected_generation_id: str | None | object = _UNSPECIFIED_GENERATION,
) -> dict[str, Path]:
    output = Path(output)
    if not updates or any(name not in LEDGER_TRANSACTION_NAMES for name in updates):
        raise ValueError("ledger transaction destinations are invalid")
    current_manifest = _read_ledger_manifest(output)
    current_generation_id = (
        None if current_manifest is None else current_manifest["generation_id"]
    )
    if (
        expected_generation_id is not _UNSPECIFIED_GENERATION
        and expected_generation_id != current_generation_id
    ):
        raise ValueError("ledger generation changed before commit")
    payloads: dict[str, bytes] = {}
    for name in LEDGER_TRANSACTION_NAMES:
        if name in updates:
            payloads[name] = updates[name]
            continue
        if current_manifest is not None:
            payloads[name] = _manifest_destination_path(
                output, current_manifest, name
            ).read_bytes()
            continue
        historical = output / name
        payloads[name] = (
            historical.read_bytes()
            if historical.exists()
            else _ledger_csv_bytes([])
        )

    digests = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in payloads.items()
    }
    generation_id = _ledger_generation_id(digests)
    generation_dir = output / LEDGER_GENERATION_DIRECTORY / generation_id
    generation_dir.mkdir(parents=True, exist_ok=True)
    committed: dict[str, Path] = {}
    destinations = {}
    for name in LEDGER_TRANSACTION_NAMES:
        destination = generation_dir / name
        if destination.exists():
            if destination.read_bytes() != payloads[name]:
                raise ValueError("prepared ledger generation conflicts with its digest")
        else:
            _write_bytes_atomic(destination, payloads[name])
        committed[name] = destination
        destinations[name] = {
            "path": destination.relative_to(output).as_posix(),
            "sha256": digests[name],
        }
    _fsync_directory(generation_dir)

    manifest = {
        "schema_version": LEDGER_TRANSACTION_SCHEMA_VERSION,
        "generation_id": generation_id,
        "destinations": destinations,
    }
    manifest_path = output / LEDGER_TRANSACTION_MANIFEST
    serialized = _canonical_json_bytes(manifest)
    if not manifest_path.exists() or manifest_path.read_bytes() != serialized:
        _write_bytes_atomic(manifest_path, serialized)
    return committed


def _read_ledger_manifest(output: Path) -> dict | None:
    output = Path(output)
    path = output / LEDGER_TRANSACTION_MANIFEST
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("ledger transaction manifest is invalid") from exc
    destinations = manifest.get("destinations")
    if (
        raw != _canonical_json_bytes(manifest)
        or manifest.get("schema_version") != LEDGER_TRANSACTION_SCHEMA_VERSION
        or not _is_sha256(manifest.get("generation_id"))
        or not isinstance(destinations, dict)
        or set(destinations) != set(LEDGER_TRANSACTION_NAMES)
    ):
        raise ValueError("ledger transaction manifest is invalid")
    digests = {}
    for name in LEDGER_TRANSACTION_NAMES:
        record = destinations.get(name)
        if not isinstance(record, dict) or not _is_sha256(record.get("sha256")):
            raise ValueError("ledger transaction manifest is invalid")
        destination = _manifest_destination_path(output, manifest, name)
        try:
            payload = destination.read_bytes()
        except OSError as exc:
            raise ValueError("committed ledger generation is missing") from exc
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise ValueError("committed ledger generation digest is invalid")
        digests[name] = record["sha256"]
    if manifest["generation_id"] != _ledger_generation_id(digests):
        raise ValueError("ledger transaction generation identity is invalid")
    return manifest


def _manifest_destination_path(output: Path, manifest: dict, name: str) -> Path:
    if name not in LEDGER_TRANSACTION_NAMES:
        raise ValueError("ledger destination is invalid")
    record = manifest.get("destinations", {}).get(name)
    relative = record.get("path") if isinstance(record, dict) else None
    expected = (
        Path(LEDGER_GENERATION_DIRECTORY)
        / str(manifest.get("generation_id") or "")
        / name
    ).as_posix()
    if relative != expected:
        raise ValueError("ledger transaction destination path is invalid")
    root = Path(output).resolve()
    destination = (root / relative).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ValueError("ledger transaction destination escapes output") from exc
    return destination


def _ledger_generation_id(digests: dict[str, str]) -> str:
    payload = {
        "schema_version": LEDGER_TRANSACTION_SCHEMA_VERSION,
        "destinations": {
            name: digests[name] for name in LEDGER_TRANSACTION_NAMES
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@contextmanager
def _ledger_commit_lock(output: Path):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / ".ledger_transaction.lock"
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


def ingest_date(root: Path, target_date: date) -> Path:
    """Ingest exactly one valid locked paid plan, never a shadow artifact."""
    root = Path(root)
    lock = read_valid_lock(root, target_date)
    expected_plan = f"output/betting_plan_{target_date.isoformat()}.csv"
    if lock is None or lock.get("plan_path") != expected_plan:
        raise ValueError("a valid matching plan lock is required before ingestion")
    plan_path = root / expected_plan
    ledger_path = root / "output" / "betting_ledger.csv"
    plan_bytes = _read_plan_bytes(plan_path)
    if hashlib.sha256(plan_bytes).hexdigest() != lock["plan_sha256"].lower():
        raise ValueError("paid plan bytes changed after lock validation")
    plan_rows = _parse_csv_bytes(plan_bytes)

    def mutate(_generation_id: str | None, current: dict[str, list[dict]]):
        existing_rows = current[ledger_path.name]
        canonical_evidence = _load_locked_plan_evidence(root, existing_rows)
        return {
            ledger_path.name: ingest_locked_plan(
                existing_rows,
                plan_rows,
                lock,
                canonical_evidence=canonical_evidence,
            )
        }

    return _mutate_ledger_rows(
        ledger_path.parent, (ledger_path.name,), mutate
    )[ledger_path.name]


def ingest_revalidated_receipts(
    root: Path, target_date: date, receipt_paths: list[Path]
) -> Path:
    """Route fully validated final confirmations into simulated ledgers."""
    from revalidation import read_valid_revalidation_receipt

    root = Path(root).resolve()
    if type(target_date) is not date or not isinstance(receipt_paths, list):
        raise ValueError("target date and receipt paths are invalid")
    settings = _read_revalidation_settings(root)
    prepared = []
    seen_paths = set()
    for supplied in receipt_paths:
        path = Path(supplied).resolve()
        if path in seen_paths:
            raise ValueError("duplicate final receipt path")
        seen_paths.add(path)
        evidence = read_valid_revalidation_receipt(
            root,
            path,
            target_date,
            expected_stage="t30",
            _capture_evidence=True,
        )
        candidate = _mutable_json(evidence.candidate)
        prepared.append((candidate, evidence))

    prepared.sort(
        key=lambda item: (
            _receipt_kickoff(item[0]),
            item[0]["provisional_rank"],
            item[0]["candidate_id"],
        )
    )
    rows = [
        _revalidation_ledger_row(target_date, candidate, evidence)
        for candidate, evidence in prepared
    ]

    paid_path = root / "output" / "betting_ledger.csv"
    observation_path = root / "output" / "observation_ledger.csv"
    rehearsal_path = root / "output" / "revalidation_rehearsal_ledger.csv"
    if settings["mode"] == "shadow":
        def mutate_rehearsal(
            _generation_id: str | None, current: dict[str, list[dict]]
        ):
            existing = _normalize_receipt_destination(
                root,
                current[rehearsal_path.name],
                destination="rehearsal",
            )
            merged, accepted = _append_receipt_rows(existing, rows)
            _validate_receipt_paid_caps(existing, accepted, target_date)
            return {rehearsal_path.name: merged}

        return _mutate_ledger_rows(
            rehearsal_path.parent, (rehearsal_path.name,), mutate_rehearsal
        )[rehearsal_path.name]

    active_rows = [
        row for row, (candidate, _evidence) in zip(rows, prepared)
        if candidate["route"] == "active"
    ]
    shadow_rows = [
        row for row, (candidate, _evidence) in zip(rows, prepared)
        if candidate["route"] == "shadow"
    ]
    observation_rows = []
    for source_row in shadow_rows:
        row = dict(source_row)
        row["stake"] = "0.00"
        row["row_payload_sha256"] = _row_payload_digest(row)
        observation_rows.append(row)

    transaction_names = tuple(
        name
        for name, selected in (
            (paid_path.name, active_rows),
            (observation_path.name, shadow_rows),
        )
        if selected
    )
    if transaction_names:
        def mutate_active(
            _generation_id: str | None, current: dict[str, list[dict]]
        ):
            updates = {}
            if active_rows:
                existing_paid = _normalize_receipt_destination(
                    root, current[paid_path.name], destination="paid"
                )
                paid, accepted_paid = _append_receipt_rows(
                    existing_paid, active_rows
                )
                _validate_receipt_paid_caps(
                    existing_paid, accepted_paid, target_date
                )
                updates[paid_path.name] = paid
            if shadow_rows:
                existing_observations = _normalize_receipt_destination(
                    root,
                    current[observation_path.name],
                    destination="observation",
                )
                observations, _accepted_observations = _append_receipt_rows(
                    existing_observations, observation_rows
                )
                updates[observation_path.name] = observations
            return updates

        committed = _mutate_ledger_rows(
            paid_path.parent, transaction_names, mutate_active
        )
        return committed[
            paid_path.name if active_rows else observation_path.name
        ]
    return paid_path


def _read_revalidation_settings(root: Path) -> dict:
    try:
        payload = json.loads((root / "betting_config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("betting config is missing or invalid") from exc
    settings = payload.get("pre_kickoff_revalidation")
    if not isinstance(settings, dict) or settings.get("mode") not in {"shadow", "active"}:
        raise ValueError("pre-kickoff revalidation mode is invalid")
    if settings.get("stake_unit") != 2:
        raise ValueError("pre-kickoff revalidation stake unit must be 2")
    return settings


def _receipt_kickoff(candidate: dict) -> datetime:
    return _aware_datetime(
        candidate.get("earliest_kickoff_at_bjt"), "candidate earliest kickoff"
    )


def _append_receipt_rows(
    existing: list[dict], new_rows: list[dict]
) -> tuple[list[dict], list[dict]]:
    rows = [dict(row) for row in existing]
    by_bet_id = {
        _required_text(row.get("bet_id"), "bet_id"): row for row in rows
    }
    by_candidate_id = {
        _required_text(row.get("candidate_id"), "candidate_id"): row
        for row in rows
        if _row_evidence_type(row) == "revalidation_receipt"
    }
    accepted = []
    for row in new_rows:
        bet_id = _required_text(row.get("bet_id"), "bet_id")
        candidate_id = _required_text(row.get("candidate_id"), "candidate_id")
        collisions = [
            existing_row
            for existing_row in (
                by_bet_id.get(bet_id),
                by_candidate_id.get(candidate_id),
            )
            if existing_row is not None
        ]
        if collisions:
            expected = _immutable_evidence_payload(row)
            if any(
                _immutable_evidence_payload(existing_row) != expected
                for existing_row in collisions
            ):
                raise ValueError(
                    "duplicate receipt bet_id or candidate_id conflicts with evidence"
                )
            continue
        copied = dict(row)
        rows.append(copied)
        accepted.append(copied)
        by_bet_id[bet_id] = copied
        by_candidate_id[candidate_id] = copied
    return rows, accepted


def _normalize_receipt_destination(
    root: Path, existing_rows: list[dict], *, destination: str
) -> list[dict]:
    if destination not in {"paid", "observation", "rehearsal"}:
        raise ValueError("receipt ledger destination is invalid")
    if destination in {"paid", "rehearsal"}:
        evidence = _load_locked_plan_evidence(root, existing_rows)
        normalized, _known = _normalize_existing_rows(
            existing_rows, canonical_evidence=evidence
        )
        return _dedupe_destination_rows(normalized, receipts_only=True)

    normalized = []
    for source_row in existing_rows:
        if not isinstance(source_row, dict):
            raise ValueError("existing observation row must be a mapping")
        row = dict(source_row)
        if _row_evidence_type(row) == "revalidation_receipt":
            _validate_existing_receipt_observation(root, row)
        else:
            expected_id = _validate_new_observation(row)
            if _required_text(row.get("bet_id"), "bet_id") != expected_id:
                raise ValueError("existing observation bet_id is invalid")
            _validate_zero_stake_status(row)
        normalized.append(row)
    return _dedupe_destination_rows(normalized, receipts_only=False)


def _dedupe_destination_rows(
    rows: list[dict], *, receipts_only: bool
) -> list[dict]:
    deduped = []
    by_bet_id: dict[str, dict] = {}
    by_candidate_id: dict[str, dict] = {}
    for row in rows:
        is_receipt = _row_evidence_type(row) == "revalidation_receipt"
        bet_id = _required_text(row.get("bet_id"), "bet_id")
        previous = by_bet_id.get(bet_id)
        if previous is not None:
            if _immutable_evidence_payload(previous) != _immutable_evidence_payload(row):
                raise ValueError("duplicate ledger bet_id conflicts with immutable payload")
            continue
        if is_receipt:
            candidate_id = _required_text(row.get("candidate_id"), "candidate_id")
            previous = by_candidate_id.get(candidate_id)
            if previous is not None:
                if _immutable_evidence_payload(previous) != _immutable_evidence_payload(row):
                    raise ValueError(
                        "duplicate ledger candidate_id conflicts with evidence"
                    )
                continue
            by_candidate_id[candidate_id] = row
        elif receipts_only:
            deduped.append(row)
            by_bet_id[bet_id] = row
            continue
        deduped.append(row)
        by_bet_id[bet_id] = row
    return deduped


def _validate_existing_receipt_observation(root: Path, row: dict) -> None:
    report_date = _strict_canonical_date(row.get("report_date"), "report_date")
    if _strict_canonical_date(row.get("date"), "date") != report_date:
        raise ValueError("observation date and report_date must match")
    candidate_id = _required_text(row.get("candidate_id"), "candidate_id")
    expected_id = _receipt_bet_id(
        report_date,
        candidate_id,
        _required_text(row.get("t30_receipt_sha256"), "T-30 receipt digest"),
    )
    if _required_text(row.get("bet_id"), "bet_id") != expected_id:
        raise ValueError("observation bet_id must equal its receipt identity")
    if row.get("plan_sha256") not in (None, ""):
        raise ValueError("receipt observation cannot claim plan-lock evidence")
    if _aware_datetime(row.get("locked_at_bjt"), "locked_at_bjt") != _aware_datetime(
        row.get("final_confirmed_at_bjt"), "final confirmed time"
    ):
        raise ValueError("receipt observation lock time is invalid")
    for field in (
        "candidate_payload_sha256",
        "t90_receipt_sha256",
        "t30_receipt_sha256",
        "live_odds_snapshot_sha256",
    ):
        if not _is_sha256(row.get(field)):
            raise ValueError("receipt observation evidence digest is invalid")
    for field in (
        "t90_receipt_path",
        "t30_receipt_path",
        "live_odds_snapshot_path",
    ):
        _required_text(row.get(field), field)
    _verify_row_payload_digest(row)
    expected = _expected_receipt_row(root, row)
    expected["stake"] = "0.00"
    expected["row_payload_sha256"] = _row_payload_digest(expected)
    if _immutable_evidence_payload(row) != _immutable_evidence_payload(expected):
        raise ValueError("receipt observation differs from immutable evidence")
    _validate_zero_stake_status(row)


def _expected_receipt_row(root: Path, row: dict) -> dict:
    from revalidation import read_valid_revalidation_receipt

    target_date = date.fromisoformat(_paid_ledger_effective_date(row))
    receipt_path = (Path(root) / row["t30_receipt_path"]).resolve()
    evidence = read_valid_revalidation_receipt(
        root,
        receipt_path,
        target_date,
        expected_stage="t30",
        _capture_evidence=True,
    )
    expected = _revalidation_ledger_row(
        target_date, _mutable_json(evidence.candidate), evidence
    )
    source = _required_text(expected.get("odds_source"), "odds_source").lower()
    _validate_new_paid_rows(
        [],
        [expected],
        source,
        target_date.isoformat(),
        _aware_datetime(expected["final_confirmed_at_bjt"], "final confirmed time"),
    )
    return expected


def _validate_zero_stake_status(row: dict) -> None:
    if _required_decimal(row.get("stake"), "observation stake") != MONEY_ZERO:
        raise ValueError("observation row must have zero stake")
    if _required_decimal(row.get("return"), "observation return") != MONEY_ZERO:
        raise ValueError("observation return must be zero")
    if _required_decimal(row.get("profit"), "observation profit") != MONEY_ZERO:
        raise ValueError("observation profit must be zero")
    status = row.get("status")
    settlement_fields = (
        "result_status",
        "result_source",
        "source_record_id",
        "captured_at_bjt",
        "score_scope",
        "settlement_minutes",
        "home_goals",
        "away_goals",
        "settled_at_bjt",
        "result_legs_json",
    )
    if status == PENDING:
        if any(row.get(field) not in (None, "") for field in settlement_fields):
            raise ValueError("pending observation contains settlement evidence")
        return
    if status in TERMINAL_STATUSES:
        _aware_iso(row.get("settled_at_bjt"), "settled_at_bjt")
        if _is_parlay(row):
            if not _settled_parlay_identities(row):
                raise ValueError("terminal observation parlay evidence is invalid")
            return
        identity = {
            "match_id": _canonical_match_id(row.get("match_id")),
            "market_type": _required_text(
                row.get("market_type"), "market_type"
            ).lower(),
            "selection": _required_text(row.get("selection"), "selection"),
            "line": _line_value(row.get("market_line", row.get("line", ""))),
        }
        if not _is_proven_result(row):
            raise ValueError("terminal observation result evidence is invalid")
        if status != _single_terminal_status(identity, row):
            raise ValueError("terminal observation result is inconsistent")
        return
    if status == ABNORMAL:
        if not _is_invalid_with_provenance(row):
            raise ValueError("abnormal observation requires invalid provenance")
        _aware_iso(row.get("settled_at_bjt"), "settled_at_bjt")
        return
    raise ValueError("observation status is invalid")


def _immutable_evidence_payload(row: dict) -> str:
    payload = {
        field: _row_payload_value(field, row)
        for field in IMMUTABLE_ROW_PAYLOAD_FIELDS
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _mutable_json(value: object) -> object:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    if isinstance(value, list):
        return [_mutable_json(item) for item in value]
    return value


def _validate_receipt_paid_caps(
    existing_rows: list[dict], new_rows: list[dict], target_date: date
) -> None:
    accepted = []
    for row in new_rows:
        _validate_new_paid_rows(
            [*existing_rows, *accepted],
            [row],
            _required_text(row.get("odds_source"), "odds_source").lower(),
            target_date.isoformat(),
            _aware_datetime(row["final_confirmed_at_bjt"], "final confirmed time"),
        )
        accepted.append(row)


def _revalidation_ledger_row(
    target_date: date,
    candidate: dict,
    evidence,
) -> dict:
    receipt = _mutable_json(evidence.receipt)
    snapshot = _mutable_json(evidence.snapshot)
    row = dict(candidate.get("source_plan_row") or {})
    if not row:
        raise ValueError("candidate source plan row is invalid")
    snapshot_relative = evidence.snapshot_path
    final_odds = _decimal_odds(receipt["current_odds"], "final odds")
    final_stake = _paid_stake(receipt["final_stake"])
    if final_stake > _paid_stake(candidate["provisional_stake"]):
        raise ValueError("receipt stake cannot exceed provisional stake")
    final_sha = evidence.receipt_sha256
    market_type = _required_text(row.get("market_type"), "market_type").lower()
    selection = row.get("selection")
    if market_type in {"had", "hhad"} and selection in THREE_WAY_SELECTIONS:
        row["selection"] = THREE_WAY_SELECTIONS[selection]
    elif market_type == "ttg" and selection in TOTAL_GOALS_SELECTIONS:
        row["selection"] = TOTAL_GOALS_SELECTIONS[selection]
    if market_type == "parlay":
        raw_legs = row.get("legs_json", row.get("legs"))
        if isinstance(raw_legs, str):
            try:
                raw_legs = json.loads(raw_legs)
            except json.JSONDecodeError as exc:
                raise ValueError("receipt parlay legs are invalid") from exc
        if not isinstance(raw_legs, list) or len(raw_legs) != 2:
            raise ValueError("receipt parlay must contain exactly two legs")
        matches = {
            match.get("match_id"): match
            for match in snapshot.get("matches", [])
            if isinstance(match, dict)
        }
        bound = {
            leg["match_id"]: leg
            for leg in candidate["execution_identity"]["legs"]
        }
        final_legs = []
        for source_leg in raw_legs:
            leg = dict(source_leg)
            leg_type = _required_text(
                leg.get("market_type"), "parlay leg market_type"
            ).lower()
            code = leg.get("selection")
            display = (
                THREE_WAY_SELECTIONS.get(code, code)
                if leg_type in {"had", "hhad"}
                else TOTAL_GOALS_SELECTIONS.get(code, code)
            )
            leg_evidence = bound.get(leg.get("match_id"))
            match = matches.get(leg.get("match_id"))
            if leg_evidence is None or match is None:
                raise ValueError("receipt parlay leg evidence is invalid")
            market = match.get("markets", {}).get(leg_type)
            if not isinstance(market, dict) or code not in market:
                raise ValueError("receipt parlay final market evidence is invalid")
            leg.update({
                "selection": display,
                "odds": str(market[code]),
                "locked_odds": str(market[code]),
                "odds_source": leg_evidence["source"],
                "odds_source_record_id": leg_evidence["source_record_id"],
                "odds_captured_at_bjt": snapshot["captured_at"],
            })
            final_legs.append(leg)
        row["legs_json"] = json.dumps(
            final_legs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        row["canonical_legs_json"] = json.dumps(
            _canonical_legs(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    bet_id = _receipt_bet_id(
        target_date.isoformat(), candidate["candidate_id"], final_sha
    )
    row.update({
        "bet_id": bet_id,
        "date": target_date.isoformat(),
        "report_date": target_date.isoformat(),
        "locked_at_bjt": receipt["checked_at_bjt"],
        "plan_sha256": "",
        "odds_captured_at_bjt": snapshot["captured_at"],
        "locked_odds": format(final_odds, "f"),
        "odds": format(final_odds, "f"),
        "stake": format(final_stake, ".2f"),
        "status": PENDING,
        "evidence_type": "revalidation_receipt",
        "candidate_id": candidate["candidate_id"],
        "candidate_payload_sha256": candidate["candidate_payload_sha256"],
        "t90_receipt_path": evidence.t90_receipt_path,
        "t90_receipt_sha256": evidence.t90_receipt_sha256,
        "t30_receipt_path": evidence.receipt_path,
        "t30_receipt_sha256": final_sha,
        "live_odds_snapshot_path": snapshot_relative,
        "live_odds_snapshot_sha256": evidence.snapshot_sha256,
        "final_confirmed_at_bjt": receipt["checked_at_bjt"],
    })
    row.setdefault("kelly_fraction", "0.25")
    if not _is_parlay(row):
        leg = candidate["execution_identity"]["legs"][0]
        row["odds_source"] = leg["source"]
        row["odds_source_record_id"] = leg["source_record_id"]
    for field in (
        "result_status", "result_source", "source_record_id", "captured_at_bjt",
        "score_scope", "settlement_minutes", "home_goals", "away_goals",
        "settled_at_bjt", "result_legs_json", "clv",
    ):
        row[field] = ""
    row["return"] = "0.00"
    row["profit"] = "0.00"
    row["row_payload_sha256"] = _row_payload_digest(row)
    return row


def settle_ledger(root: Path, results: dict, settled_at: datetime) -> Path:
    """Settle existing canonical ledger rows without regenerating any plan."""
    root = Path(root)
    ledger_path = root / "output" / "betting_ledger.csv"

    def mutate(_generation_id: str | None, current: dict[str, list[dict]]):
        source_rows = current[ledger_path.name]
        canonical_evidence = _load_locked_plan_evidence(root, source_rows)
        rows, _known_keys = _normalize_existing_rows(
            source_rows, canonical_evidence=canonical_evidence
        )
        return {
            ledger_path.name: settle_pending(rows, results, settled_at)
        }

    return _mutate_ledger_rows(
        ledger_path.parent, (ledger_path.name,), mutate
    )[ledger_path.name]


def _load_locked_plan_evidence(
    root: Path, existing_rows: list[dict]
) -> dict[tuple[str, str], str]:
    report_dates: set[str] = set()
    receipt_rows: list[dict] = []
    for row in existing_rows:
        if not isinstance(row, dict):
            raise ValueError("existing row must be a mapping")
        effective_date = _paid_ledger_effective_date(row)
        if _row_evidence_type(row) == "revalidation_receipt":
            _validate_existing_canonical_paid_row(row)
            receipt_rows.append(row)
            continue
        after_cutover = (
            date.fromisoformat(effective_date) >= CANONICAL_PAID_CUTOVER_DATE
        )
        if after_cutover or _claims_canonical_identity(row):
            if row.get("strategy_version") in NEW_PAID_STRATEGY_VERSIONS:
                try:
                    _validate_existing_canonical_paid_row(row)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"invalid existing canonical paid row: {exc}"
                    ) from exc
            report_dates.add(effective_date)
    evidence: dict[tuple[str, str], str] = {}
    for report_date in sorted(report_dates):
        target_date = date.fromisoformat(report_date)
        lock = read_valid_lock(root, target_date)
        expected_plan = f"output/betting_plan_{report_date}.csv"
        if lock is None or lock.get("plan_path") != expected_plan:
            raise ValueError("canonical ledger row lacks valid locked plan evidence")
        lock_source = _validate_lock(lock)
        plan_path = Path(root) / expected_plan
        plan_bytes = _read_plan_bytes(plan_path)
        if hashlib.sha256(plan_bytes).hexdigest() != lock["plan_sha256"].lower():
            raise ValueError("canonical ledger locked plan evidence hash differs")
        plan_rows = _parse_csv_bytes(plan_bytes)
        plan_ids: set[str] = set()
        for plan_row in plan_rows:
            if plan_row.get("date") != report_date:
                raise ValueError("locked plan evidence date differs")
            bet_id = stable_bet_id(plan_row)
            if bet_id in plan_ids:
                raise ValueError("duplicate canonical identity in locked plan evidence")
            plan_ids.add(bet_id)
            expected_row = _new_locked_row(plan_row, lock, lock_source, bet_id)
            allowed = {
                expected_row["row_payload_sha256"],
                _legacy_row_payload_digest(expected_row),
            }
            actual = next(
                (
                    row.get("row_payload_sha256")
                    for row in existing_rows
                    if row.get("bet_id") == bet_id
                    and _paid_ledger_effective_date(row) == report_date
                ),
                expected_row["row_payload_sha256"],
            )
            if actual not in allowed:
                raise ValueError(
                    "canonical row strategy_version or payload differs from locked plan evidence"
                )
            evidence[(report_date, bet_id)] = actual

    if receipt_rows:
        from revalidation import read_valid_revalidation_receipt

        for row in receipt_rows:
            report_date = _paid_ledger_effective_date(row)
            target_date = date.fromisoformat(report_date)
            receipt_path = (root / row["t30_receipt_path"]).resolve()
            validated = read_valid_revalidation_receipt(
                root,
                receipt_path,
                target_date,
                expected_stage="t30",
                _capture_evidence=True,
            )
            expected = _revalidation_ledger_row(
                target_date,
                _mutable_json(validated.candidate),
                validated,
            )
            if _immutable_evidence_payload(row) != _immutable_evidence_payload(
                expected
            ):
                raise ValueError("receipt ledger row differs from immutable evidence")
            evidence[(report_date, row["bet_id"])] = expected["row_payload_sha256"]
    return evidence


def _read_csv(path: Path) -> list[dict]:
    return _read_csv_file(resolve_ledger_path(path))


def _read_csv_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_plan_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _parse_csv_bytes(payload: bytes) -> list[dict]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("paid plan must be UTF-8 CSV") from exc
    with io.StringIO(text, newline="") as handle:
        return list(csv.DictReader(handle))


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest and settle locked paid betting plans.")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("--date", required=True, type=_parse_date)
    args = parser.parse_args()
    try:
        if args.command == "ingest":
            ingest_date(Path.cwd(), args.date)
    except (OSError, ValueError):
        return 1
    return 0


def _validate_lock(lock: dict) -> str:
    if not isinstance(lock, dict):
        raise ValueError("lock must be a mapping")
    report_date = lock.get("report_date")
    try:
        date.fromisoformat(report_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("lock report_date must be YYYY-MM-DD") from exc
    _aware_iso(lock.get("locked_at_bjt"), "lock locked_at_bjt")
    plan_hash = lock.get("plan_sha256")
    if not isinstance(plan_hash, str) or len(plan_hash) != 64 or any(char not in "0123456789abcdef" for char in plan_hash.lower()):
        raise ValueError("lock plan_sha256 must be a SHA-256 hex string")
    source = lock.get("odds_source")
    if not isinstance(source, str) or source.lower() not in DOMESTIC_ODDS_SOURCES:
        raise ValueError("lock odds_source must be a domestic source")
    return source.lower()


def _identity_payload(row: dict, *, allow_legacy_match: bool = False) -> dict:
    report_date = _identity_date(row)
    strategy_version = _required_text(row.get("strategy_version"), "strategy_version")
    play = _required_text(row.get("play"), "play")
    market_type = _required_text(row.get("market_type"), "market_type").lower()
    if (
        strategy_version == "value-v4"
        and market_type != "parlay"
        and "parlay" in play.lower()
    ):
        raise ValueError("play contradicts non-parlay market_type")
    if market_type == "parlay":
        legs = _canonical_legs(row, allow_legacy_match=allow_legacy_match)
        return {
            "report_date": report_date,
            "strategy_version": strategy_version,
            "play": play,
            "market_type": "parlay",
            "legs": legs,
        }
    return {
        "report_date": report_date,
        "strategy_version": strategy_version,
        "play": play,
        "market_type": market_type,
        "match_id": _canonical_match_id(row.get("match_id"), allow_legacy_match=allow_legacy_match),
        "selection": _required_text(row.get("selection"), "selection"),
        "line": _canonical_market_line(
            market_type, row.get("market_line", row.get("line", ""))
        ),
    }


def _canonical_legs(row: dict, *, allow_legacy_match: bool = False) -> list[dict]:
    raw_legs = row.get("legs")
    if raw_legs is None:
        raw_legs = row.get("legs_json")
    if isinstance(raw_legs, str):
        try:
            raw_legs = json.loads(raw_legs)
        except json.JSONDecodeError as exc:
            raise ValueError("legs_json must be JSON") from exc
    if not isinstance(raw_legs, list) or len(raw_legs) != 2:
        raise ValueError("parlay must contain exactly two legs")
    legs = []
    for leg in raw_legs:
        if not isinstance(leg, dict):
            raise ValueError("parlay leg must be a mapping")
        market_type = _required_text(
            leg.get("market_type"), "leg market_type"
        ).lower()
        legs.append({
            "match_id": _canonical_match_id(leg.get("match_id"), allow_legacy_match=allow_legacy_match),
            "market_type": market_type,
            "selection": _required_text(leg.get("selection"), "leg selection"),
            "line": _canonical_market_line(
                market_type, leg.get("line", leg.get("market_line", ""))
            ),
        })
    return sorted(legs, key=lambda leg: json.dumps(leg, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _existing_dedupe_key(row: dict) -> tuple[str, str]:
    if _row_evidence_type(row) == "revalidation_receipt":
        return "canonical", _required_text(row.get("bet_id"), "bet_id")
    try:
        return "canonical", stable_bet_id(row)
    except ValueError:
        return "legacy", _required_text(row.get("bet_id"), "bet_id")


def _normalize_existing_rows(
    existing_rows: list[dict],
    *,
    canonical_evidence: dict[tuple[str, str], str],
) -> tuple[list[dict], set[tuple[str, str]]]:
    if not isinstance(canonical_evidence, dict):
        raise ValueError("canonical locked-plan evidence is required")
    normalized: list[dict] = []
    known_keys: set[tuple[str, str]] = set()
    canonical_states: dict[str, str] = {}
    for source_row in existing_rows:
        if not isinstance(source_row, dict):
            raise ValueError("existing row must be a mapping")
        effective_date = _paid_ledger_effective_date(source_row)
        canonical_key = _canonical_evidence_key(
            source_row,
            effective_date,
            canonical_evidence,
        )
        anchored_canonical = (
            date.fromisoformat(effective_date) >= CANONICAL_PAID_CUTOVER_DATE
            or canonical_key is not None
            or _claims_canonical_identity(source_row)
        )
        if anchored_canonical:
            try:
                _validate_existing_canonical_paid_row(source_row)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid existing canonical paid row: {exc}") from exc
            if canonical_key is None:
                raise ValueError(
                    "canonical ledger row lacks locked plan evidence anchor"
                )
            row = dict(source_row)
        else:
            _validate_existing_legacy_economics(source_row)
            row = _migrate_existing_row(source_row)
        key_kind, identity_key = _existing_dedupe_key(row)
        dedupe_key = (key_kind, identity_key)
        if dedupe_key in known_keys:
            if (
                key_kind == "canonical"
                and canonical_states[identity_key] != _existing_canonical_state(row)
            ):
                raise ValueError("conflicting existing canonical ledger rows")
            continue
        known_keys.add(dedupe_key)
        if key_kind == "canonical":
            if anchored_canonical:
                evidence_key = (
                    _strict_canonical_date(row.get("report_date"), "report_date"),
                    identity_key,
                )
                if evidence_key not in canonical_evidence:
                    raise ValueError(
                        "canonical ledger row lacks locked plan evidence anchor"
                    )
                expected_digest = canonical_evidence[evidence_key]
                actual_digest = row.get("row_payload_sha256")
                if not _is_sha256(expected_digest) or not _is_sha256(actual_digest):
                    raise ValueError(
                        "canonical row payload digest is missing from locked plan evidence"
                    )
                if expected_digest != actual_digest:
                    raise ValueError(
                        "canonical row payload differs from locked plan evidence anchor"
                    )
            canonical_states[identity_key] = _existing_canonical_state(row)
        normalized.append(row)
    return normalized, known_keys


def _claims_canonical_identity(row: dict) -> bool:
    if _row_evidence_type(row) == "revalidation_receipt":
        return True
    provided_id = row.get("bet_id")
    if isinstance(provided_id, str) and provided_id in _canonical_identity_candidates(
        row
    ):
        return True
    if row.get("strategy_version") not in NEW_PAID_STRATEGY_VERSIONS:
        return False
    return any(
        row.get(field) not in (None, "")
        for field in ("row_payload_sha256", "plan_sha256", "locked_at_bjt")
    )


def _paid_ledger_effective_date(row: dict) -> str:
    populated: dict[str, str] = {}
    for field in ("date", "report_date"):
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        populated[field] = _strict_canonical_date(value, field)
    if not populated:
        raise ValueError(
            "paid ledger row requires a nonblank date or report_date"
        )
    if len(set(populated.values())) != 1:
        raise ValueError("paid ledger date and report_date must match")
    return next(iter(populated.values()))


def _canonical_evidence_key(
    row: dict,
    effective_date: str,
    canonical_evidence: dict[tuple[str, str], str],
) -> tuple[str, str] | None:
    provided_id = row.get("bet_id")
    if not isinstance(provided_id, str) or not provided_id:
        return None
    key = (effective_date, provided_id)
    if key not in canonical_evidence:
        return None
    if _row_evidence_type(row) == "revalidation_receipt":
        return key
    if provided_id not in _canonical_identity_candidates(row):
        return None
    return key


def _canonical_identity_candidates(row: dict) -> set[str]:
    candidates: set[str] = set()
    for strategy_version in NEW_PAID_STRATEGY_VERSIONS:
        candidate = dict(row)
        candidate["strategy_version"] = strategy_version
        try:
            candidates.add(stable_bet_id(candidate))
        except (TypeError, ValueError):
            continue
    return candidates


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _row_evidence_type(row: dict) -> str:
    value = row.get("evidence_type")
    if value in (None, ""):
        return "plan_lock"
    if value not in {"plan_lock", "revalidation_receipt"}:
        raise ValueError("ledger evidence_type is invalid")
    return value


def _receipt_bet_id(
    report_date: str, candidate_id: str, final_receipt_sha256: str
) -> str:
    if not _is_sha256(final_receipt_sha256):
        raise ValueError("final receipt digest is invalid")
    identity = {
        "report_date": _strict_canonical_date(report_date, "report_date"),
        "candidate_id": _required_text(candidate_id, "candidate_id"),
        "t30_receipt_sha256": final_receipt_sha256,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_existing_canonical_paid_row(row: dict) -> None:
    row_date = _strict_canonical_date(row.get("date"), "date")
    report_date = _strict_canonical_date(row.get("report_date"), "report_date")
    if row_date != report_date:
        raise ValueError("date and report_date must match")
    provided_id = _required_text(row.get("bet_id"), "bet_id")
    evidence_type = _row_evidence_type(row)
    if evidence_type == "revalidation_receipt":
        expected_id = _receipt_bet_id(
            report_date,
            _required_text(row.get("candidate_id"), "candidate_id"),
            _required_text(row.get("t30_receipt_sha256"), "T-30 receipt digest"),
        )
        if provided_id != expected_id:
            raise ValueError("bet_id must equal the stable receipt identity")
    elif provided_id != stable_bet_id(row):
        raise ValueError("bet_id must equal the stable canonical identity")
    _verify_row_payload_digest(row)
    locked_at = _aware_datetime(row.get("locked_at_bjt"), "locked_at_bjt")
    plan_hash = row.get("plan_sha256")
    if evidence_type == "plan_lock" and (
        not isinstance(plan_hash, str)
        or len(plan_hash) != 64
        or any(character not in "0123456789abcdef" for character in plan_hash.lower())
    ):
        raise ValueError("plan_sha256 must be a SHA-256 hex string")
    if evidence_type == "revalidation_receipt":
        if plan_hash not in (None, ""):
            raise ValueError("receipt-backed row cannot claim a plan lock digest")
        if locked_at != _aware_datetime(
            row.get("final_confirmed_at_bjt"), "final confirmed time"
        ):
            raise ValueError("receipt-backed lock time must equal confirmation time")
        for field in (
            "candidate_payload_sha256", "t90_receipt_sha256",
            "t30_receipt_sha256", "live_odds_snapshot_sha256",
        ):
            if not _is_sha256(row.get(field)):
                raise ValueError("receipt-backed evidence digest is invalid")
        for field in (
            "t90_receipt_path", "t30_receipt_path", "live_odds_snapshot_path"
        ):
            _required_text(row.get(field), field)
    source = _required_text(row.get("odds_source"), "odds_source").lower()
    _validate_new_paid_rows([], [row], source, report_date, locked_at)

    status = row.get("status")
    if status == PENDING:
        settlement_fields = (
            "result_status",
            "result_source",
            "source_record_id",
            "captured_at_bjt",
            "score_scope",
            "settlement_minutes",
            "home_goals",
            "away_goals",
            "settled_at_bjt",
            "result_legs_json",
        )
        if any(row.get(field) not in (None, "") for field in settlement_fields):
            raise ValueError("pending row contains settlement evidence")
        if _required_decimal(row.get("return"), "return") != MONEY_ZERO:
            raise ValueError("pending return must be zero")
        if _required_decimal(row.get("profit"), "profit") != MONEY_ZERO:
            raise ValueError("pending profit must be zero")
        return
    if status in TERMINAL_STATUSES:
        _validate_existing_terminal_economics(row)
        return
    if status == ABNORMAL:
        if not _is_invalid_with_provenance(row):
            raise ValueError("abnormal row requires invalid result provenance")
        _aware_iso(row.get("settled_at_bjt"), "settled_at_bjt")
        if _required_money(row.get("return"), "return") != MONEY_ZERO:
            raise ValueError("abnormal return must be zero")
        if _required_money(row.get("profit"), "profit") != MONEY_ZERO:
            raise ValueError("abnormal profit must be zero")
        return
    raise ValueError("status is not canonical")


def _strict_canonical_date(value: object, name: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{name} must be canonical YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be canonical YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be canonical YYYY-MM-DD")
    return value


def _row_payload_digest(row: dict) -> str:
    return _row_payload_digest_for(
        row, IMMUTABLE_ROW_PAYLOAD_FIELDS, ROW_PAYLOAD_SCHEMA_VERSION
    )


def _legacy_row_payload_digest(row: dict) -> str:
    return _row_payload_digest_for(
        row, IMMUTABLE_ROW_PAYLOAD_FIELDS[:-10], LEGACY_ROW_PAYLOAD_SCHEMA_VERSION
    )


def _row_payload_digest_for(
    row: dict, fields: tuple[str, ...], schema_version: int
) -> str:
    immutable = {
        field: _row_payload_value(field, row)
        for field in fields
    }
    payload = {
        "schema_version": schema_version,
        "immutable": immutable,
        "initial_economics": {
            "status": PENDING,
            "return": "0.00",
            "profit": "0.00",
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _row_payload_value(field: str, row: dict) -> object:
    if field == "market_line":
        market_type = _required_text(row.get("market_type"), "market_type").lower()
        return _canonical_market_line(
            market_type, row.get("market_line", row.get("line", ""))
        )
    return _existing_state_value(field, row.get(field, ""))


def _verify_row_payload_digest(row: dict) -> None:
    digest = row.get("row_payload_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or (
            digest != _row_payload_digest(row)
            and not (
                _row_evidence_type(row) == "plan_lock"
                and digest == _legacy_row_payload_digest(row)
            )
        )
    ):
        raise ValueError("canonical row payload digest is missing or invalid")


def _validate_existing_terminal_economics(row: dict) -> None:
    _aware_iso(row.get("settled_at_bjt"), "settled_at_bjt")
    stake = _paid_stake(row.get("stake"))
    returned = _required_money(row.get("return"), "return")
    profit = _required_money(row.get("profit"), "profit", allow_negative=True)
    if profit != returned - stake:
        raise ValueError("terminal profit must equal return minus stake")

    status = row["status"]
    if _is_parlay(row):
        if not _settled_parlay_identities(row):
            raise ValueError("terminal parlay result evidence is invalid")
        expected_return = MONEY_ZERO
        if status == REFUNDED:
            expected_return = stake
        elif status == WON:
            result_legs = json.loads(row["result_legs_json"])
            effective_odds = Decimal("1")
            for raw, result in zip(
                _sorted_raw_legs(_raw_legs(row)),
                _sorted_result_legs(result_legs),
            ):
                if result.get("result_status") != "refunded":
                    effective_odds *= _decimal_odds(
                        raw.get("locked_odds", raw.get("odds")),
                        "parlay leg locked odds",
                    )
            expected_return = _money(stake * effective_odds)
    else:
        if not _is_proven_result(row):
            raise ValueError("terminal result provenance is invalid")
        identity = _identity_payload(row)
        expected_status = _single_terminal_status(
            {
                "market_type": identity["market_type"],
                "selection": identity["selection"],
                "line": identity["line"],
            },
            row,
        )
        if expected_status != status:
            raise ValueError("terminal status differs from result")
        expected_return = (
            stake
            if status == REFUNDED
            else MONEY_ZERO
            if status == LOST
            else _money(stake * _decimal_odds(row.get("locked_odds"), "locked_odds"))
        )
    if returned != expected_return:
        raise ValueError("terminal return is inconsistent")


def _required_money(
    value: object,
    name: str,
    *,
    allow_negative: bool = False,
) -> Decimal:
    amount = _required_decimal(value, name)
    if (not allow_negative and amount < MONEY_ZERO) or amount != amount.quantize(
        MONEY_QUANTUM
    ):
        qualifier = "canonical money" if allow_negative else "nonnegative canonical money"
        raise ValueError(f"{name} must be {qualifier}")
    return amount


def _validate_existing_legacy_economics(row: dict) -> None:
    if row.get("stake") not in (None, ""):
        try:
            stake = _required_decimal(row.get("stake"), "legacy stake")
        except ValueError as exc:
            raise ValueError("invalid existing legacy economics") from exc
        if stake < MONEY_ZERO:
            raise ValueError("invalid existing legacy economics")
    if row.get("status") in TERMINAL_STATUSES:
        try:
            _required_decimal(row.get("profit"), "legacy profit")
        except ValueError as exc:
            raise ValueError("invalid existing legacy economics") from exc


def _existing_canonical_state(row: dict) -> str:
    identity = _identity_payload(row)
    state = {
        field: _existing_state_value(field, row.get(field, ""))
        for field in REQUIRED_FIELD_ORDER
        if field not in {
            "bet_id", "date", "report_date", "strategy_version", "play",
            "market_type", "market_line", "match_id", "selection",
        }
    }
    state["identity"] = identity
    state["effective_report_date"] = _effective_report_date(
        row, identity["report_date"]
    )
    if "legs" in row:
        state["legs"] = _existing_state_value("legs", row.get("legs"))
    return json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _effective_report_date(row: dict, identity_date: str) -> str:
    value = row.get("report_date")
    if value is None or (isinstance(value, str) and not value.strip()):
        return identity_date
    return _required_date(value)


def _existing_state_value(field: str, value: object) -> object:
    if field not in {"legs", "legs_json", "canonical_legs_json", "result_legs_json"}:
        return "" if value is None else str(value)
    if value in (None, ""):
        return ""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(parsed, list):
        parsed = [_canonical_existing_item(item) for item in parsed]
        return sorted(
            parsed,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return parsed


def _canonical_existing_item(item: object) -> object:
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    market_type = str(normalized.get("market_type") or "").strip().lower()
    if market_type:
        normalized["market_type"] = market_type
    for field in ("match_id", "selection"):
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = value.strip()
    line = normalized.get("line", normalized.get("market_line", ""))
    try:
        normalized["line"] = _canonical_market_line(market_type, line)
    except ValueError:
        normalized["line"] = _line_value(line)
    normalized.pop("market_line", None)
    return normalized


def _migrate_existing_row(source_row: dict) -> dict:
    if not isinstance(source_row, dict):
        raise ValueError("existing row must be a mapping")
    row = dict(source_row)
    if not row.get("bet_id"):
        identity_row = dict(row)
        identity_row.setdefault("date", row.get("report_date", ""))
        identity_row.setdefault("strategy_version", "legacy")
        identity_row.setdefault("play", row.get("play", "legacy"))
        identity_row.setdefault("market_type", row.get("market_type", "legacy"))
        identity_row.setdefault("selection", row.get("selection", "legacy"))
        if not identity_row.get("match_id"):
            identity_row["match_id"] = _legacy_match_id(row)
        row["bet_id"] = _stable_legacy_bet_id(identity_row)
    row.setdefault("status", PENDING)
    _set_settlement_defaults(row)
    return row


def _new_locked_row(source_row: dict, lock: dict, lock_source: str, bet_id: str) -> dict:
    row = dict(source_row)
    row_source = row.get("odds_source")
    if row_source not in (None, "") and (
        not isinstance(row_source, str) or row_source.lower() != lock_source
    ):
        raise ValueError("plan odds_source must match lock odds_source")
    row["bet_id"] = bet_id
    row["report_date"] = lock["report_date"]
    row["locked_at_bjt"] = lock["locked_at_bjt"]
    row["plan_sha256"] = lock["plan_sha256"].lower()
    row["odds_source"] = lock_source
    row["evidence_type"] = "plan_lock"
    if not row.get("locked_odds"):
        row["locked_odds"] = row.get("odds", "")
    if "odds" not in row:
        row["odds"] = row.get("locked_odds", "")
    if _is_parlay(row):
        canonical_legs = _canonical_legs(row)
        row["canonical_legs_json"] = json.dumps(canonical_legs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row["status"] = PENDING
    for field in (
        "result_status", "result_source", "source_record_id", "captured_at_bjt",
        "score_scope", "settlement_minutes", "home_goals", "away_goals",
        "settled_at_bjt", "result_legs_json", "clv",
    ):
        row[field] = ""
    row["return"] = "0.00"
    row["profit"] = "0.00"
    row["row_payload_sha256"] = _row_payload_digest(row)
    return row


def _set_settlement_defaults(row: dict) -> None:
    for field in ("result_status", "result_source", "source_record_id", "captured_at_bjt", "score_scope", "settlement_minutes", "home_goals", "away_goals", "settled_at_bjt", "result_legs_json", "clv"):
        row.setdefault(field, "")
    row.setdefault("return", "0.00")
    row.setdefault("profit", "0.00")


def _settle_single(row: dict, results: dict, settled_time: str) -> dict:
    result = _result_for(row.get("match_id"), results)
    if not _is_proven_result(result):
        if _is_invalid_with_provenance(result):
            return _apply_abnormal(row, result, settled_time)
        return row
    if result["result_status"] == "refunded":
        return _apply_settlement(row, REFUNDED, result, row.get("stake"), settled_time)
    outcome = _outcome(row.get("market_type"), row.get("selection"), row.get("market_line"), result)
    if outcome is None:
        return row
    if outcome:
        return _apply_settlement(row, WON, result, _money(row.get("stake")) * _odds(row.get("locked_odds", row.get("odds"))), settled_time)
    return _apply_settlement(row, LOST, result, MONEY_ZERO, settled_time)


def _settle_parlay(row: dict, results: dict, settled_time: str) -> dict:
    try:
        legs = _canonical_legs(row)
        raw_legs = _raw_legs(row)
    except ValueError:
        return row
    result_details = []
    effective_odds = Decimal("1")
    any_loss = False
    all_refunded = True
    for canonical, raw in zip(legs, _sorted_raw_legs(raw_legs)):
        result = _result_for(canonical["match_id"], results)
        if not _is_proven_result(result):
            if _is_invalid_with_provenance(result):
                detail = {**canonical, **_result_fields(result)}
                return _apply_abnormal(row, result, settled_time, [detail])
            return row
        detail = {**canonical, **_result_fields(result)}
        result_details.append(detail)
        if result["result_status"] == "refunded":
            continue
        all_refunded = False
        outcome = _outcome(canonical["market_type"], canonical["selection"], canonical["line"], result)
        if outcome is None:
            return row
        if not outcome:
            any_loss = True
        effective_odds *= _odds(raw.get("locked_odds", raw.get("odds")))
    provenance = _parlay_provenance(result_details)
    if all_refunded:
        return _apply_settlement(row, REFUNDED, result_details[0], _money(row.get("stake")), settled_time, provenance)
    if any_loss:
        return _apply_settlement(row, LOST, result_details[0], MONEY_ZERO, settled_time, provenance)
    return _apply_settlement(row, WON, result_details[0], _money(row.get("stake")) * effective_odds, settled_time, provenance)


def _apply_abnormal(
    row: dict,
    result: dict,
    settled_time: str,
    result_legs: list[dict] | None = None,
) -> dict:
    updated = dict(row)
    updated.update(_result_fields(result))
    updated["status"] = ABNORMAL
    updated["settled_at_bjt"] = settled_time
    if result_legs is not None:
        updated["result_legs_json"] = json.dumps(
            result_legs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return updated


def _apply_settlement(row: dict, status: str, result: dict, returned: object, settled_time: str, provenance: dict | None = None) -> dict:
    stake = _money(row.get("stake"))
    return_amount = _money(returned)
    updated = dict(row)
    updated.update(_result_fields(result))
    if provenance:
        updated.update(provenance)
    updated["status"] = status
    updated["return"] = _money_text(return_amount)
    updated["profit"] = _money_text(return_amount - stake)
    updated["settled_at_bjt"] = settled_time
    return updated


def _outcome(market_type: object, selection: object, line: object, result: dict) -> bool | None:
    market = str(market_type or "").strip().lower()
    selection_text = str(selection or "").strip()
    home = _goal(result.get("home_goals"))
    away = _goal(result.get("away_goals"))
    if home is None or away is None:
        return None
    if market == "had":
        return _three_way(selection_text, home, away)
    if market == "hhad":
        try:
            handicap = int(str(line).strip())
        except (TypeError, ValueError):
            return None
        return _three_way(selection_text, home + handicap, away)
    if market == "ttg":
        total = home + away
        expected = "7+球" if total >= 7 else f"{total}球"
        return _total_selection(selection_text) == expected
    return None


def _three_way(selection: str, home: int, away: int) -> bool | None:
    expected = "胜" if home > away else "平" if home == away else "负"
    normalized = {"h": "胜", "d": "平", "a": "负"}.get(selection.lower(), selection)
    return normalized == expected if normalized in {"胜", "平", "负"} else None


def _total_selection(selection: str) -> str:
    normalized = selection.lower().replace(" ", "")
    if normalized.startswith("s") and normalized[1:].isdigit():
        return "7+球" if normalized == "s7" else f"{int(normalized[1:])}球"
    return selection


def _result_for(match_id: object, results: dict) -> dict | None:
    if not isinstance(match_id, str) or not match_id:
        return None
    result = results.get(match_id)
    if not isinstance(result, dict) or result.get("match_id") != match_id:
        return None
    return result


def _is_proven_result(result: dict | None) -> bool:
    if not isinstance(result, dict) or not _has_provenance(result):
        return False
    status = result.get("result_status")
    if status == "refunded":
        return True
    return (
        status == "finished"
        and result.get("score_scope") == "regular_time_90"
        and str(result.get("settlement_minutes", "")).strip() == "90"
        and _goal(result.get("home_goals")) is not None
        and _goal(result.get("away_goals")) is not None
    )


def _is_invalid_with_provenance(result: dict | None) -> bool:
    return isinstance(result, dict) and result.get("result_status") == "invalid" and _has_provenance(result)


def _has_provenance(result: dict) -> bool:
    if not all(
        isinstance(result.get(field), str) and result[field].strip()
        for field in ("result_source", "source_record_id", "captured_at_bjt")
    ):
        return False
    if result["result_source"].strip().lower() not in DOMESTIC_ODDS_SOURCES:
        return False
    try:
        _aware_iso(result["captured_at_bjt"], "captured_at_bjt")
    except ValueError:
        return False
    return True


def _result_fields(result: dict) -> dict:
    return {field: result.get(field, "") for field in ("result_status", "result_source", "source_record_id", "captured_at_bjt", "score_scope", "settlement_minutes", "home_goals", "away_goals")}


def _parlay_provenance(details: list[dict]) -> dict:
    return {
        "result_source": "|".join(detail["result_source"] for detail in details),
        "source_record_id": "|".join(detail["source_record_id"] for detail in details),
        "captured_at_bjt": "|".join(detail["captured_at_bjt"] for detail in details),
        "score_scope": "regular_time_90",
        "settlement_minutes": "90",
        "result_status": "finished" if all(detail["result_status"] == "finished" for detail in details) else "refunded",
        "result_legs_json": json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }


def _raw_legs(row: dict) -> list[dict]:
    raw = row.get("legs", row.get("legs_json"))
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(leg, dict) for leg in raw):
        raise ValueError("parlay must contain two valid legs")
    return raw


def _sorted_raw_legs(legs: list[dict]) -> list[dict]:
    return sorted(legs, key=lambda leg: json.dumps({
        "match_id": _canonical_match_id(leg.get("match_id")),
        "market_type": _required_text(leg.get("market_type"), "leg market_type").lower(),
        "selection": _required_text(leg.get("selection"), "leg selection"),
        "line": _canonical_market_line(
            _required_text(leg.get("market_type"), "leg market_type").lower(),
            leg.get("line", leg.get("market_line", "")),
        ),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _is_parlay(row: dict) -> bool:
    return str(row.get("market_type", "")).strip().lower() == "parlay"


def _required_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("date is required")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    return parsed.isoformat()


def _identity_date(row: dict) -> str:
    value = row.get("date")
    if value is None or (isinstance(value, str) and not value.strip()):
        value = row.get("report_date")
    return _required_date(value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _canonical_match_id(value: object, *, allow_legacy_match: bool = False) -> str:
    text = _required_text(value, "match_id")
    if any(character.isspace() or not character.isprintable() for character in text):
        raise ValueError("match_id must be canonical")
    if text.startswith("legacy_match:") and not allow_legacy_match:
        raise ValueError("legacy_match namespace is reserved for migration")
    return text


def _hash_identity(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_legacy_bet_id(identity_row: dict) -> str:
    try:
        payload = _identity_payload(identity_row, allow_legacy_match=True)
    except ValueError:
        payload = _legacy_fallback_identity_payload(identity_row)
    return _hash_identity(payload)


def _legacy_fallback_identity_payload(row: dict) -> dict:
    return {
        "identity_namespace": "legacy_fallback_v2",
        "report_date": _legacy_text(row.get("report_date") or row.get("date")),
        "strategy_version": _legacy_text(row.get("strategy_version")),
        "play": _legacy_text(row.get("play")),
        "market_type": _legacy_text(row.get("market_type")),
        "selection": _legacy_text(row.get("selection")),
        "line": _legacy_text(row.get("market_line", row.get("line"))),
        "display": _legacy_display_payload(row),
        "legs": _legacy_leg_payload(row),
    }


def _legacy_leg_payload(row: dict) -> dict:
    raw_legs = row.get("legs")
    if raw_legs is None:
        if "legs_json" in row:
            raw_legs = row.get("legs_json")
        elif "legs" not in row:
            return {"format": "missing"}
    parsed_legs = raw_legs
    parsed = not isinstance(raw_legs, str)
    if isinstance(raw_legs, str):
        try:
            parsed_legs = json.loads(raw_legs)
            parsed = True
        except json.JSONDecodeError:
            parsed = False
    if (
        isinstance(parsed_legs, list)
        and all(isinstance(leg, dict) for leg in parsed_legs)
    ):
        legs = [_legacy_structured_leg_identity(leg) for leg in parsed_legs]
        return {
            "format": "structured",
            "items": sorted(
                legs,
                key=lambda leg: json.dumps(
                    leg,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        }
    if not parsed:
        return {"format": "raw_text", "value": _legacy_text(raw_legs)}
    try:
        normalized_raw = json.loads(json.dumps(
            parsed_legs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
    except (TypeError, ValueError):
        return {
            "format": "raw_value",
            "type": type(parsed_legs).__name__,
            "value": _legacy_text(parsed_legs),
        }
    return {"format": "raw_json", "value": normalized_raw}


def _legacy_structured_leg_identity(leg: dict) -> dict:
    identity = {
        "market_type": _legacy_text(leg.get("market_type")).lower(),
        "selection": _legacy_text(leg.get("selection")),
        "line": _legacy_text(leg.get("line", leg.get("market_line"))),
    }
    match_id = _legacy_text(leg.get("match_id"))
    if match_id:
        identity["match_id"] = match_id
    else:
        identity["legacy_match_identity"] = {
            "identity_namespace": "legacy_leg_match_v1",
            "display": {
                field: _legacy_text(leg.get(field))
                for field in (
                    "match", "fixture", "team_a", "team_b", "home_team",
                    "away_team", "homeTeam", "awayTeam", "home", "away",
                    "teams", "display", "display_label", "match_display",
                )
            },
        }
    return identity


def _legacy_display_payload(row: dict) -> dict:
    return {
        field: _legacy_text(row.get(field))
        for field in (
            "match_id", "match", "team_a", "team_b", "home_team", "away_team",
            "teams", "display", "display_label", "match_display",
        )
    }


def _legacy_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value).strip()


def _legacy_match_id(row: dict) -> str:
    text = row.get("match") or "|".join(str(row.get(field, "")) for field in ("team_a", "team_b"))
    if not isinstance(text, str) or not text.strip():
        text = json.dumps(
            _legacy_display_payload(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return "legacy_match:" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]


def _line_value(value: object) -> str:
    return "" if value is None else str(value).strip()


def _canonical_market_line(market_type: str, value: object) -> str:
    line = _line_value(value)
    if market_type == "hhad":
        try:
            return str(parse_handicap(line))
        except ValueError:
            return line
    return line


def _aware_iso(value: object, name: str) -> str:
    _aware_datetime(value, name)
    return value.isoformat() if isinstance(value, datetime) else value


def _aware_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    else:
        raise ValueError(f"{name} must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _capture_not_after_lock(
    value: object, name: str, locked_at: datetime
) -> datetime:
    captured_at = _aware_datetime(value, name)
    if captured_at > locked_at:
        raise ValueError(f"{name} cannot be after lock")
    return captured_at


def _goal(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _odds(value: object) -> Decimal:
    try:
        odds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("odds must be decimal") from exc
    if not odds.is_finite():
        raise ValueError("odds must be finite")
    if odds <= 0:
        raise ValueError("odds must be positive")
    return odds


def _abnormal_match_id(row: dict) -> object:
    if not _is_parlay(row):
        return row.get("match_id")
    raw_details = row.get("result_legs_json")
    if not isinstance(raw_details, str) or not raw_details:
        return None
    try:
        details = json.loads(raw_details)
    except json.JSONDecodeError:
        return None
    if not isinstance(details, list):
        return None
    for detail in details:
        if isinstance(detail, dict) and detail.get("result_status") == "invalid":
            match_id = detail.get("match_id")
            try:
                return _canonical_match_id(match_id)
            except ValueError:
                return None
    return None


def _money(value: object) -> Decimal:
    try:
        money = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("money must be decimal") from exc
    if not money.is_finite():
        raise ValueError("money must be finite")
    return money.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _money_text(value: object) -> str:
    return format(_money(value), ".2f")


def _csv_value(value: object) -> object:
    return "" if value is None else value


if __name__ == "__main__":
    raise SystemExit(main())
