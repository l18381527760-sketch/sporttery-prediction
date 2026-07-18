import argparse
import csv
import hashlib
import io
import json
import os
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

REQUIRED_FIELD_ORDER = (
    "bet_id", "date", "report_date", "strategy_version", "model_version",
    "locked_at_bjt", "plan_sha256", "match_id", "team_a", "team_b", "kickoff_local",
    "play", "market_type", "market_line", "selection", "legs_json",
    "canonical_legs_json", "odds_source", "odds_source_record_id",
    "odds_captured_at_bjt", "locked_odds", "odds", "raw_probability",
    "calibrated_probability", "official_market_probability",
    "conservative_probability", "edge", "net_ev", "full_kelly",
    "kelly_fraction", "data_quality_multiplier", "volatility_multiplier",
    "performance_multiplier", "portfolio_rank", "binding_limits", "stake", "data_quality",
    "volatility_band", "status", "result_status", "result_source",
    "source_record_id", "captured_at_bjt", "home_goals", "away_goals",
    "settled_at_bjt", "return", "profit", "result_legs_json", "clv",
)


def stable_bet_id(plan_row: dict) -> str:
    """Return the deterministic identity for one valid plan row."""
    if not isinstance(plan_row, dict):
        raise ValueError("plan row must be a mapping")
    return _hash_identity(_identity_payload(plan_row))


def ingest_locked_plan(existing_rows: list[dict], plan_rows: list[dict], lock: dict) -> list[dict]:
    """Migrate legacy rows and append only previously unseen locked plan identities."""
    lock_source = _validate_lock(lock)
    if not isinstance(existing_rows, list) or not isinstance(plan_rows, list):
        raise ValueError("ledger and plan rows must be lists")

    ingested: list[dict] = []
    known_keys: set[tuple[str, str]] = set()
    canonical_states: dict[str, str] = {}
    for source_row in existing_rows:
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
            canonical_states[identity_key] = _existing_canonical_state(row)
        ingested.append(row)

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
    )
    for source_row, bet_id in new_rows:
        ingested.append(_new_locked_row(source_row, lock, lock_source, bet_id))
    return ingested


def _validate_new_paid_rows(
    existing_rows: list[dict],
    new_rows: list[dict],
    lock_source: str,
    report_date: str,
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
        _aware_iso(row.get("odds_captured_at_bjt"), "odds_captured_at_bjt")
        display_odds = _decimal_odds(row.get("odds"), "odds")
        locked_odds = _decimal_odds(row.get("locked_odds"), "locked_odds")
        if not _is_parlay(row) and display_odds != locked_odds:
            raise ValueError("single odds and locked_odds must be exactly equal")
        if strategy_version == "value-v4":
            _value_v4_kelly(row.get("kelly_fraction"))

    _validate_paid_portfolio(new_rows, lock_source)
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


def _validate_paid_portfolio(plan_rows: list[dict], lock_source: str) -> None:
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
                _aware_iso(
                    leg.get("odds_captured_at_bjt"),
                    "parlay leg odds_captured_at_bjt",
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
        "home_goals", "away_goals", "settled_at_bjt", "result_legs_json", "clv",
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


def write_ledger_atomic(path: Path, rows: list[dict]) -> Path:
    """Persist a deterministic UTF-8-SIG ledger without exposing partial files."""
    path = Path(path)
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    path.parent.mkdir(parents=True, exist_ok=True)
    unknown_fields = sorted({key for row in rows if isinstance(row, dict) for key in row} - set(REQUIRED_FIELD_ORDER))
    fieldnames = [*REQUIRED_FIELD_ORDER, *unknown_fields]
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for source_row in rows:
                if not isinstance(source_row, dict):
                    raise ValueError("ledger row must be a mapping")
                writer.writerow({field: _csv_value(source_row.get(field, "")) for field in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


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
    existing_rows = _read_csv(ledger_path)
    return write_ledger_atomic(ledger_path, ingest_locked_plan(existing_rows, plan_rows, lock))


def settle_ledger(root: Path, results: dict, settled_at: datetime) -> Path:
    """Settle existing canonical ledger rows without regenerating any plan."""
    ledger_path = Path(root) / "output" / "betting_ledger.csv"
    return write_ledger_atomic(ledger_path, settle_pending(_read_csv(ledger_path), results, settled_at))


def _read_csv(path: Path) -> list[dict]:
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
    report_date = _required_date(row.get("date", row.get("report_date")))
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
        "line": _line_value(row.get("market_line", row.get("line", ""))),
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
        legs.append({
            "match_id": _canonical_match_id(leg.get("match_id"), allow_legacy_match=allow_legacy_match),
            "market_type": _required_text(leg.get("market_type"), "leg market_type").lower(),
            "selection": _required_text(leg.get("selection"), "leg selection"),
            "line": _line_value(leg.get("line", leg.get("market_line", ""))),
        })
    return sorted(legs, key=lambda leg: json.dumps(leg, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _existing_dedupe_key(row: dict) -> tuple[str, str]:
    try:
        return "canonical", stable_bet_id(row)
    except ValueError:
        return "legacy", _required_text(row.get("bet_id"), "bet_id")


def _existing_canonical_state(row: dict) -> str:
    state = {
        field: _existing_state_value(field, row.get(field, ""))
        for field in REQUIRED_FIELD_ORDER
        if field != "bet_id"
    }
    state["identity"] = _identity_payload(row)
    if "legs" in row:
        state["legs"] = _existing_state_value("legs", row.get("legs"))
    return json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
        "home_goals", "away_goals", "settled_at_bjt", "result_legs_json", "clv",
    ):
        row[field] = ""
    row["return"] = "0.00"
    row["profit"] = "0.00"
    return row


def _set_settlement_defaults(row: dict) -> None:
    for field in ("result_status", "result_source", "source_record_id", "captured_at_bjt", "home_goals", "away_goals", "settled_at_bjt", "result_legs_json", "clv"):
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
    return status == "finished" and _goal(result.get("home_goals")) is not None and _goal(result.get("away_goals")) is not None


def _is_invalid_with_provenance(result: dict | None) -> bool:
    return isinstance(result, dict) and result.get("result_status") == "invalid" and _has_provenance(result)


def _has_provenance(result: dict) -> bool:
    if not all(
        isinstance(result.get(field), str) and result[field].strip()
        for field in ("result_source", "source_record_id", "captured_at_bjt")
    ):
        return False
    try:
        _aware_iso(result["captured_at_bjt"], "captured_at_bjt")
    except ValueError:
        return False
    return True


def _result_fields(result: dict) -> dict:
    return {field: result.get(field, "") for field in ("result_status", "result_source", "source_record_id", "captured_at_bjt", "home_goals", "away_goals")}


def _parlay_provenance(details: list[dict]) -> dict:
    return {
        "result_source": "|".join(detail["result_source"] for detail in details),
        "source_record_id": "|".join(detail["source_record_id"] for detail in details),
        "captured_at_bjt": "|".join(detail["captured_at_bjt"] for detail in details),
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
        "line": _line_value(leg.get("line", leg.get("market_line", ""))),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _is_parlay(row: dict) -> bool:
    return str(row.get("market_type", "")).strip().lower() == "parlay"


def _required_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("date is required")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    return value


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


def _aware_iso(value: object, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
        text = value.isoformat()
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
        text = value
    else:
        raise ValueError(f"{name} must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return text


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
