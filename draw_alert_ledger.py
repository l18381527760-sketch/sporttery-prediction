"""Settle 90-minute draw alerts and maintain subtype learning metrics."""

import csv
import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SUBTYPES = ("cold_draw", "balanced_draw")
SETTLED_STATUSES = {"命中", "未命中"}
SETTLEMENT_FIELDS = (
    "home_goals", "away_goals", "outcome", "status",
    "hypothetical_profit", "actual_profit", "clv",
)


def settle_alert(alert: dict, result: dict | None) -> dict:
    """Settle one draw alert from its stored 90-minute score only."""
    settled = dict(alert)
    home_goals = _goal_value(result.get("home_goals")) if result else None
    away_goals = _goal_value(result.get("away_goals")) if result else None
    settled["home_goals"] = "" if home_goals is None else home_goals
    settled["away_goals"] = "" if away_goals is None else away_goals
    if home_goals is None or away_goals is None:
        settled.update(outcome=None, status="未结算", hypothetical_profit=None, actual_profit=None)
        return settled

    outcome = 1.0 if home_goals == away_goals else 0.0
    hypothetical_stake = _number(alert.get("hypothetical_stake")) or 0.0
    odds = _number(alert.get("domestic_draw_odds")) or 0.0
    hypothetical_profit = hypothetical_stake * (odds - 1) if outcome else -hypothetical_stake
    actual_profit = _actual_profit(alert, outcome)
    settled.update(
        outcome=outcome,
        status="命中" if outcome else "未命中",
        hypothetical_profit=hypothetical_profit,
        actual_profit=actual_profit,
    )
    return settled


def compute_subtype_metrics(rows: list[dict], min_samples: int = 30, roi_gate: float = 0.05, max_drawdown: float = 100) -> dict:
    """Calculate promotion evidence from valid settled alerts of one subtype."""
    valid_rows = [row for row in rows if _is_valid_settled_sample(row)]
    outcomes = [_outcome(row) for row in valid_rows]
    count = len(valid_rows)
    hits = sum(outcome for outcome in outcomes if outcome is not None)
    hypothetical_profit = sum(_number(row.get("hypothetical_profit")) or 0.0 for row in valid_rows)
    hypothetical_stake = sum(_number(row.get("hypothetical_stake")) or 0.0 for row in valid_rows)
    roi = hypothetical_profit / hypothetical_stake if hypothetical_stake else None

    model_losses = _squared_errors(valid_rows, "model_draw_probability")
    market_losses = _squared_errors(valid_rows, "market_draw_probability")
    brier = _mean(model_losses)
    market_brier = _mean(market_losses)
    log_loss = _binary_log_loss(valid_rows)
    clv_values = [_number(row.get("clv")) for row in valid_rows]
    valid_clv = [value for value in clv_values if value is not None]
    average_clv = _mean(valid_clv)
    complete_clv = len(valid_clv) == count
    drawdown = _max_drawdown(valid_rows)
    recent_brier = _mean(model_losses[-10:])
    previous_brier = _mean(model_losses[-20:-10]) if len(model_losses) >= 20 else None
    recent_not_worse = previous_brier is None or (recent_brier is not None and recent_brier <= previous_brier)
    promoted = (
        count >= min_samples
        and roi is not None and roi > roi_gate
        and complete_clv and average_clv is not None and average_clv > 0
        and brier is not None and market_brier is not None and brier < market_brier
        and drawdown <= max_drawdown
        and recent_not_worse
    )
    return {
        "count": count,
        "hit_rate": hits / count if count else None,
        "roi": roi,
        "brier": brier,
        "market_brier": market_brier,
        "log_loss": log_loss,
        "average_clv": average_clv,
        "max_drawdown": drawdown,
        "recent_brier": recent_brier,
        "promoted": promoted,
    }


def update_draw_alert_ledger(root: Path = ROOT) -> tuple[Path, Path]:
    """Refresh the one-row-per-alert ledger and independent subtype metrics."""
    root = Path(root)
    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = output_dir / "draw_alert_ledger.csv"
    alerts = _latest_alert_rows(output_dir, ledger_path)
    results = {
        _match_key(row): row
        for row in _read_csv(root / "data" / "bet_results.csv")
    }
    snapshots = _load_snapshots(root / "data" / "odds_snapshots")
    ledger_rows = []
    for alert in alerts:
        settled = settle_alert(alert, results.get(_match_key(alert)))
        settled["clv"] = _closing_clv(alert, snapshots)
        ledger_rows.append(settled)

    fieldnames = _ledger_fieldnames(ledger_rows)
    _write_csv(ledger_path, fieldnames, ledger_rows)
    metrics = {
        "subtypes": {
            subtype: compute_subtype_metrics(
                [row for row in ledger_rows if row.get("subtype") == subtype]
            )
            for subtype in SUBTYPES
        }
    }
    metrics_path = output_dir / "draw_alert_metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return ledger_path, metrics_path


def _latest_alert_rows(output_dir: Path, ledger_path: Path) -> list[dict]:
    rows_by_key = {}
    for row in _read_csv(ledger_path):
        rows_by_key[_alert_key(row)] = row
    for path in sorted(output_dir.glob("draw_alert_*.csv")):
        if path == ledger_path:
            continue
        for row in _read_csv(path):
            rows_by_key[_alert_key(row)] = row
    return list(rows_by_key.values())


def _alert_key(row: dict) -> tuple[str, str, str, str]:
    return (str(row.get("date", "")), str(row.get("team_a", "")), str(row.get("team_b", "")), str(row.get("subtype", "")))


def _match_key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("date", "")), str(row.get("team_a", "")), str(row.get("team_b", "")))


def _teams_key(row: dict) -> tuple[str, str]:
    return (str(row.get("team_a", "")), str(row.get("team_b", "")))


def _actual_profit(alert: dict, outcome: float) -> float:
    if alert.get("settlement_mode") in {"linked", "observation", "budget_capped_observation"}:
        return 0.0
    stake = _number(alert.get("additional_stake")) or 0.0
    odds = _number(alert.get("domestic_draw_odds")) or 0.0
    return stake * (odds - 1) if outcome else -stake


def _goal_value(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value.strip()):
        return None
    return int(value)


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _probability(value) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 1 else None


def _is_valid_settled_sample(row: dict) -> bool:
    return (
        row.get("status") in SETTLED_STATUSES
        and _outcome(row) is not None
        and _probability(row.get("model_draw_probability")) is not None
        and _probability(row.get("market_draw_probability")) is not None
        and _number(row.get("hypothetical_stake")) is not None
        and _number(row.get("hypothetical_profit")) is not None
        and _number(row.get("clv")) is not None
    )


def _outcome(row: dict) -> float | None:
    value = _number(row.get("outcome"))
    if value in {0.0, 1.0}:
        return value
    if row.get("status") == "命中":
        return 1.0
    if row.get("status") == "未命中":
        return 0.0
    return None


def _squared_errors(rows: list[dict], probability_field: str) -> list[float]:
    errors = []
    for row in rows:
        probability = _probability(row.get(probability_field))
        outcome = _outcome(row)
        if probability is not None and outcome is not None:
            errors.append((probability - outcome) ** 2)
    return errors


def _binary_log_loss(rows: list[dict]) -> float | None:
    losses = []
    for row in rows:
        probability = _probability(row.get("model_draw_probability"))
        outcome = _outcome(row)
        if probability is None or outcome is None:
            continue
        clipped = min(max(probability, 1e-15), 1 - 1e-15)
        losses.append(-(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped)))
    return _mean(losses)


def _max_drawdown(rows: list[dict]) -> float:
    cumulative = peak = drawdown = 0.0
    for row in rows:
        cumulative += _number(row.get("hypothetical_profit")) or 0.0
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _load_snapshots(snapshot_dir: Path) -> list[tuple[dict, Path]]:
    snapshots = []
    for path in sorted(snapshot_dir.glob("*.json")) if snapshot_dir.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            snapshots.append((payload, path))
    return snapshots


def _closing_clv(alert: dict, snapshots: list[tuple[dict, Path]]) -> float | str:
    stored_probability = _number(alert.get("market_draw_probability"))
    if stored_probability is None:
        return ""
    candidates = []
    for payload, path in snapshots:
        if str(payload.get("target_date", "")) != str(alert.get("date", "")):
            continue
        captured_at = _timestamp(payload.get("captured_at"))
        for match in payload.get("matches", []):
            if not isinstance(match, dict) or _teams_key(match) != _teams_key(alert):
                continue
            if not _qualifying_snapshot_match(match):
                continue
            kickoff_at = _timestamp(match.get("kickoff_at"))
            if captured_at is not None and kickoff_at is not None and captured_at > kickoff_at:
                continue
            closing_probability = _de_vig_draw_probability(match)
            if closing_probability is not None:
                candidates.append((captured_at, path.name, closing_probability))
    if not candidates:
        return ""
    candidates.sort(key=lambda candidate: (candidate[0] is not None, candidate[0].timestamp() if candidate[0] else 0, candidate[1]))
    return candidates[-1][2] - stored_probability


def _qualifying_snapshot_match(match: dict) -> bool:
    return (
        match.get("market_type") == "win_draw_loss"
        and _number(match.get("settlement_minutes")) == 90
        and match.get("includes_extra_time") is False
    )


def _de_vig_draw_probability(match: dict) -> float | None:
    odds = [_number(match.get(field)) for field in ("h", "d", "a")]
    if any(value is None or value <= 0 for value in odds):
        return None
    implied = [1 / value for value in odds]
    return implied[1] / sum(implied)


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _ledger_fieldnames(rows: list[dict]) -> list[str]:
    fieldnames = []
    for row in rows:
        for field in row:
            if field not in fieldnames and field not in SETTLEMENT_FIELDS:
                fieldnames.append(field)
    return fieldnames + list(SETTLEMENT_FIELDS)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settle", action="store_true", help="refresh the draw alert ledger")
    args = parser.parse_args(argv)
    if not args.settle:
        parser.error("--settle is required")
    try:
        ledger_path, metrics_path = update_draw_alert_ledger()
    except Exception as error:
        print(f"draw alert ledger update failed: {error}", file=sys.stderr)
        return 1
    print(ledger_path)
    print(metrics_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
