"""Generate conservative, value-gated 90-minute draw alerts."""

import argparse
import csv
import hashlib
import importlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from draw_alert_core import COLD_RESISTANCE_SIGNALS, DrawInputs, MarketEvidence, classify_candidate, fair_probabilities, is_finite_between, valid_odds
from strategy_controls import apply_league_draw_calibration, fit_league_draw_calibrations


ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))

FIELDS = [
    "date", "rank", "match_id", "match", "team_a", "team_b", "stage", "subtype", "selection", "domestic_draw_odds",
    "market_draw_probability", "model_draw_probability", "draw_edge", "expected_value", "xg_total",
    "global_calibrated_draw_probability", "league_calibration_samples", "league_calibration_enabled",
    "evidence_json", "data_quality", "captured_at", "alert_level", "additional_stake",
    "linked_main_stake", "hypothetical_stake", "settlement_mode", "strategy_version", "feature_version",
]

RANK_GATES = ((0.27, 0.04, 1.05), (0.29, 0.05, 1.07), (0.31, 0.06, 1.09), (0.33, 0.07, 1.11))
DRAW_MODEL_FEATURES = [
    "base_draw_probability", "market_draw_probability", "favorite_probability", "win_probability_gap",
    "xg_total", "favorite_movement", "regional_gap", "source_count", "is_knockout", "is_balanced",
]
DRAW_MODEL_FEATURE_RANGES = {
    "base_draw_probability": (0.0, 1.0),
    "market_draw_probability": (0.0, 1.0),
    "favorite_probability": (0.0, 1.0),
    "win_probability_gap": (0.0, 1.0),
    "xg_total": (0.0, 10.0),
    "favorite_movement": (-1.0, 1.0),
    "regional_gap": (-1.0, 1.0),
    "source_count": (0.0, 100.0),
    "is_knockout": (0.0, 1.0),
    "is_balanced": (0.0, 1.0),
}
DRAW_MODEL_INTEGER_FEATURES = {"source_count", "is_knockout", "is_balanced"}


def derive_structural_signals(
    stage: str,
    xg_a: float,
    xg_b: float,
    domestic_odds: tuple[float, float, float],
    model_probabilities: tuple[float, float, float],
    calibrated_draw_probability: float,
    config: dict,
) -> tuple[str, ...]:
    fair = fair_probabilities(*domestic_odds)
    if fair is None:
        return ()
    underdog_probability = min(model_probabilities[0], model_probabilities[2])
    signals = []
    if str(stage).casefold() in {str(value).casefold() for value in config.get("knockout_stages", [])}:
        signals.append("knockout_caution")
    if xg_a + xg_b <= 2.35:
        signals.append("low_total")
    if abs(fair[0] - fair[2]) <= 0.10:
        signals.append("similar_strength")
    if calibrated_draw_probability > underdog_probability and underdog_probability + calibrated_draw_probability >= 0.35:
        signals.append("underdog_resistance")
    return tuple(signals)


def same_match(alert: dict, row: dict) -> bool:
    if alert.get("match_id") and row.get("match_id"):
        if str(alert["match_id"]) != str(row["match_id"]):
            return False
        for key in ("date", "team_a", "team_b"):
            if alert.get(key) and row.get(key) and alert.get(key) != row.get(key):
                return False
        return True
    return (
        bool(alert.get("date") and alert.get("team_a") and alert.get("team_b"))
        and alert.get("date") == row.get("date")
        and alert.get("team_a") == row.get("team_a")
        and alert.get("team_b") == row.get("team_b")
    )


def _same_date_and_teams(alert: dict, leg: dict) -> bool:
    if (
        alert.get("match_id")
        and leg.get("match_id")
        and str(alert["match_id"]) != str(leg["match_id"])
    ):
        return False
    return (
        bool(alert.get("date") and alert.get("team_a") and alert.get("team_b"))
        and alert.get("date") == leg.get("date")
        and alert.get("team_a") == leg.get("team_a")
        and alert.get("team_b") == leg.get("team_b")
    )


def _combo_draw_matches(alert: dict, row: dict) -> bool:
    raw_legs = row.get("legs_json")
    if not isinstance(raw_legs, str) or len(raw_legs) > 65_536:
        return False
    try:
        legs = json.loads(raw_legs)
    except (TypeError, json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(legs, list):
        return False
    return any(
        isinstance(leg, dict)
        and leg.get("selection") == "平"
        and _same_date_and_teams(alert, leg)
        for leg in legs
    )


def _stake_amount(value: object, maximum: int) -> int | None:
    parsed = _number(value)
    if (
        parsed is None
        or not parsed.is_integer()
        or parsed < 0
        or parsed > maximum
    ):
        return None
    return int(parsed)


def _total_valid_stakes(rows: list[dict], key: str, maximum: int) -> int | None:
    total = 0
    for row in rows:
        stake = _stake_amount(row.get(key), maximum)
        if stake is None:
            return None
        total += stake
    return total


def select_alerts(candidates: list[dict], rank_gates=RANK_GATES, max_alerts: int = 4, max_per_league: int = 2) -> list[dict]:
    selected = []
    league_counts = {}
    valid_candidates = []
    for candidate in candidates:
        try:
            score = float(candidate["score"])
            probability = float(candidate["model_draw_probability"])
            edge = float(candidate["draw_edge"])
            expected_value = float(candidate["expected_value"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (
            is_finite_between(score, -10.0, 10.0)
            and is_finite_between(probability, 0.0, 1.0)
            and is_finite_between(edge, -1.0, 1.0)
            and is_finite_between(expected_value, 0.0, 100.0)
        ):
            continue
        valid_candidates.append((candidate, score, probability, edge, expected_value))
    for candidate, score, candidate_probability, candidate_edge, candidate_expected_value in sorted(valid_candidates, key=lambda item: (item[1], item[0]["match_id"]), reverse=True):
        if len(selected) == max_alerts:
            break
        league = candidate.get("stage") or "unknown"
        if league_counts.get(league, 0) >= max_per_league:
            continue
        probability, edge, expected_value = rank_gates[len(selected)]
        if candidate_probability < probability or candidate_edge < edge or candidate_expected_value < expected_value:
            continue
        row = {**candidate, "rank": len(selected) + 1}
        selected.append(row)
        league_counts[league] = league_counts.get(league, 0) + 1
    return selected


def attach_stake(alert: dict, main_plan: list[dict], existing_alerts: list[dict], subtype_metrics: dict, daily_budget: int, alert_budget: int, requested_stake: int, minimum_stake: int = 10) -> dict:
    result = dict(alert)
    linked = next(
        (
            row
            for row in main_plan
            if (same_match(alert, row) and row.get("selection") == "平")
            or _combo_draw_matches(alert, row)
        ),
        None,
    )
    result["hypothetical_stake"] = 10
    if linked:
        linked_stake = _stake_amount(linked.get("stake"), daily_budget)
        if linked_stake is None:
            result.update(
                additional_stake=0,
                linked_main_stake=0,
                settlement_mode="observation",
            )
        else:
            result.update(additional_stake=0, linked_main_stake=linked_stake, settlement_mode="linked")
    elif not subtype_metrics.get("promoted"):
        result.update(additional_stake=0, linked_main_stake=0, settlement_mode="observation")
    else:
        used = _total_valid_stakes(main_plan, "stake", daily_budget)
        alert_used = _total_valid_stakes(
            existing_alerts, "additional_stake", alert_budget
        )
        if used is None or alert_used is None:
            result.update(
                additional_stake=0,
                linked_main_stake=0,
                settlement_mode="budget_capped_observation",
            )
            return result
        available = max(0, min(daily_budget - used - alert_used, alert_budget - alert_used))
        stake = min(requested_stake, available) if available >= minimum_stake else 0
        state = "standalone" if stake else "budget_capped_observation"
        result.update(additional_stake=stake, linked_main_stake=0, settlement_mode=state)
    return result


def generate_alerts(
    target_date: str,
    root: Path = ROOT,
    *,
    snapshot_time: datetime | None = None,
) -> Path:
    root = Path(root)
    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    draw_config, app_config = _load_configs(root)
    predictions = _load_csv(output_dir / f"predictions_{target_date}.csv")
    market_heat = _load_json(root / "data" / f"market_heat_{target_date}.json", {})
    domestic_odds = _load_json(root / "data" / f"sporttery_odds_{target_date}.json", {})
    main_plan = _load_csv(output_dir / f"betting_plan_{target_date}.csv")
    metrics = _load_json(output_dir / "draw_alert_metrics.json", {})
    model_registry = _load_json(output_dir / "draw_model_registry.json", {})
    calibration_config = app_config.get("league_calibration", {})
    league_calibrations = fit_league_draw_calibrations(
        _load_csv(root / "data" / "draw_training_samples.csv"),
        min_samples=int(calibration_config.get("min_samples", 30)),
        prior_samples=int(calibration_config.get("prior_samples", 60)),
        max_adjustment=float(calibration_config.get("max_adjustment", 0.05)),
        validation_fraction=float(calibration_config.get("validation_fraction", 0.25)),
    )
    daily_decision = _load_json(
        output_dir / f"daily_decision_{target_date}.json", {}
    )
    account = daily_decision.get("account", {}) if isinstance(daily_decision, dict) else {}
    write_time = _snapshot_write_time(snapshot_time)
    evidence_by_match = {
        str(row.get("match_id") or ""): row
        for row in market_heat.get("matches", [])
        if isinstance(row, dict) and row.get("match_id") not in (None, "")
    }
    candidates = []
    for prediction in predictions:
        candidate = _candidate_from_rows(
            prediction,
            evidence_by_match.get(str(prediction.get("match_id") or "")),
            domestic_odds,
            write_time,
            draw_config,
            app_config,
            root,
            league_calibrations,
        )
        if candidate:
            candidates.append(candidate)
    rank_gates = tuple(
        (float(gate["min_probability"]), float(gate["min_edge"]), float(gate["min_expected_value"]))
        for gate in draw_config.get("rank_gates", [])
    ) or RANK_GATES
    selected = select_alerts(
        candidates,
        rank_gates=rank_gates,
        max_alerts=int(draw_config.get("max_alerts", 4)),
        max_per_league=int(draw_config.get("max_per_league", 2)),
    )
    rows = []
    for alert in selected:
        requested_stake = _quarter_kelly_stake(
            float(alert["model_draw_probability"]),
            float(alert["domestic_draw_odds"]),
            int(draw_config.get("min_promoted_stake", 10)),
            int(draw_config.get("max_promoted_stake", 30)),
            int(app_config.get("max_daily_budget", 500)),
        )
        subtype_metrics = _subtype_metrics(metrics, alert["subtype"])
        if _league_is_paused(model_registry, alert["stage"]):
            subtype_metrics = {**subtype_metrics, "promoted": False}
        main_stake = _total_valid_stakes(
            main_plan, "stake", int(app_config.get("max_daily_budget", 500))
        )
        remaining_monthly = _number(account.get("remaining_monthly_budget"))
        alert_budget = int(draw_config.get("daily_additional_budget", 80))
        if account.get("paused") is True or main_stake is None:
            subtype_metrics = {**subtype_metrics, "promoted": False}
        elif remaining_monthly is not None:
            alert_budget = max(0, min(alert_budget, int(remaining_monthly - main_stake)))
            if alert_budget < int(draw_config.get("min_promoted_stake", 10)):
                subtype_metrics = {**subtype_metrics, "promoted": False}
        result = attach_stake(
            alert,
            main_plan,
            rows,
            subtype_metrics,
            int(app_config.get("max_daily_budget", 500)),
            alert_budget,
            requested_stake,
            int(draw_config.get("min_promoted_stake", 10)),
        )
        rows.append(result)
    path = output_dir / f"draw_alert_{target_date}.csv"
    _atomic_write_csv(path, rows)
    return path


def _candidate_from_rows(
    prediction: dict,
    evidence: dict | None,
    domestic: dict,
    snapshot_time: datetime,
    draw_config: dict,
    app_config: dict,
    root: Path = ROOT,
    league_calibrations: dict | None = None,
) -> dict | None:
    if not evidence:
        return None
    match_id = str(prediction.get("match_id") or "")
    odds = _domestic_odds(domestic, match_id)
    if not odds:
        return None
    market_sources = _qualifying_market_sources(evidence)
    source_count = len(market_sources)
    if source_count > 100:
        return None
    try:
        model_probabilities = (float(prediction["p_a"]), float(prediction["p_draw"]), float(prediction["p_b"]))
        xg_a, xg_b = float(prediction["xg_a"]), float(prediction["xg_b"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not all(
            is_finite_between(value, 0.0, 1.0) for value in model_probabilities
        )
        or not math.isclose(sum(model_probabilities), 1.0, abs_tol=0.02)
        or not all(is_finite_between(value, 0.0, 10.0) for value in (xg_a, xg_b))
        or not is_finite_between(xg_a + xg_b, 0.0, 10.0)
    ):
        return None
    favorite_movement = _number(evidence.get("favorite_movement"))
    regional_gap = _number(evidence.get("regional_gap"))
    if not (
        favorite_movement is not None
        and is_finite_between(favorite_movement, -1.0, 1.0)
        and regional_gap is not None
        and is_finite_between(regional_gap, -1.0, 1.0)
    ):
        return None
    fair = fair_probabilities(*odds)
    if fair is None:
        return None
    stage = str(prediction.get("stage", ""))
    knockout_stages = {
        str(value).casefold() for value in app_config.get("knockout_stages", [])
    }
    features = {
        "base_draw_probability": model_probabilities[1],
        "market_draw_probability": fair[1],
        "favorite_probability": max(model_probabilities[0], model_probabilities[2]),
        "win_probability_gap": abs(model_probabilities[0] - model_probabilities[2]),
        "xg_total": xg_a + xg_b,
        "favorite_movement": favorite_movement,
        "regional_gap": regional_gap,
        "source_count": source_count,
        "is_knockout": int(stage.casefold() in knockout_stages),
        "is_balanced": int(
            abs(model_probabilities[0] - model_probabilities[2])
            <= float(draw_config.get("balanced_max_win_gap", 0.10))
        ),
    }
    _capture_feature_snapshot(
        root,
        prediction,
        evidence,
        snapshot_time,
        odds[1],
        features,
    )
    global_calibrated_probability = _calibrated_probability(
        features, model_probabilities[1], root=root
    )
    calibrated_draw_probability, league_state = apply_league_draw_calibration(
        global_calibrated_probability,
        stage,
        league_calibrations or {},
    )
    signals = derive_structural_signals(
        prediction.get("stage", ""), xg_a, xg_b, odds, model_probabilities,
        calibrated_draw_probability, app_config,
    )
    underdog_win = min(model_probabilities[0], model_probabilities[2])
    inputs = DrawInputs(
        match_id=match_id,
        team_a=prediction.get("team_a", ""),
        team_b=prediction.get("team_b", ""),
        stage=prediction.get("stage", ""),
        domestic_odds=odds,
        model_probabilities=model_probabilities,
        calibrated_draw_probability=calibrated_draw_probability,
        xg_total=xg_a + xg_b,
        source_count=source_count,
        market_sources=tuple(market_sources),
        market_scope=evidence.get("market_scope", ""),
        favorite_movement=favorite_movement,
        regional_gap=regional_gap,
        underdog_win_probability=underdog_win,
        underdog_not_lose_probability=underdog_win + calibrated_draw_probability,
        structural_signals=signals,
        data_quality=evidence.get("quality", "low"),
    )
    classified = classify_candidate(inputs, draw_config)
    if not classified:
        return None
    structure_count = len(set(signals))
    if classified.subtype == "cold_draw":
        structure_count = len(set(signals) & COLD_RESISTANCE_SIGNALS)
    alert_level = (
        "高级"
        if calibrated_draw_probability >= 0.32
        and classified.draw_edge >= 0.06
        and classified.expected_value >= 1.08
        and structure_count >= 3
        else "中级"
    )
    return {
        "date": prediction.get("date", ""),
        "match_id": match_id,
        "match": f"{inputs.team_a} vs {inputs.team_b}",
        "team_a": inputs.team_a,
        "team_b": inputs.team_b,
        "stage": inputs.stage,
        "subtype": classified.subtype,
        "selection": "平",
        "domestic_draw_odds": odds[1],
        "market_draw_probability": classified.domestic_draw_probability,
        "model_draw_probability": calibrated_draw_probability,
        "global_calibrated_draw_probability": global_calibrated_probability,
        "league_calibration_samples": int(league_state.get("sample_count") or 0),
        "league_calibration_enabled": league_state.get("enabled") is True,
        "draw_edge": classified.draw_edge,
        "expected_value": classified.expected_value,
        "xg_total": inputs.xg_total,
        "evidence_json": json.dumps(_qualifying_source_records(evidence), ensure_ascii=False, sort_keys=True),
        "data_quality": inputs.data_quality,
        "captured_at": snapshot_time.isoformat(),
        "alert_level": alert_level,
        "strategy_version": app_config.get("strategy_version", ""),
        "feature_version": draw_config.get("feature_version", ""),
        "score": classified.score,
    }


def _load_configs(root: Path) -> tuple[dict, dict]:
    betting = _load_json(root / "betting_config.json", {})
    app_config = _load_json(root / "config.json", {})
    return betting.get("draw_alert", {}), app_config | {
        "max_daily_budget": betting.get("max_daily_budget", 500),
        "strategy_version": betting.get("strategy_version", ""),
        "league_calibration": betting.get("league_calibration", {}),
    }


def _qualifying_market_sources(evidence: dict) -> list[MarketEvidence]:
    records = _qualifying_source_records(evidence)
    qualified = []
    for source, record in records.items():
        item = MarketEvidence(
            source=str(source),
            market_type=str(record.get("market_type", "")),
            settlement_minutes=_integer(record.get("settlement_minutes")),
            includes_extra_time=record.get("includes_extra_time"),
        )
        qualified.append(item)
    return qualified


def _qualifying_source_records(evidence: dict) -> dict:
    records = evidence.get("sources", {})
    if not isinstance(records, dict):
        return {}
    return {
        str(source): record
        for source, record in records.items()
        if isinstance(record, dict)
        and record.get("market_type") == "win_draw_loss"
        and _integer(record.get("settlement_minutes")) == 90
        and record.get("includes_extra_time") is False
        and _source_record_is_valid(record)
    }


def _source_record_is_valid(record: dict) -> bool:
    fields = ("home_probability", "draw_probability", "away_probability")
    probabilities = [_number(record.get(field)) for field in fields]
    if not all(
        value is not None and is_finite_between(value, 0.0, 1.0)
        for value in probabilities
    ):
        return False
    if not math.isclose(sum(probabilities), 1.0, abs_tol=0.05):
        return False
    raw_volume = record.get("volume")
    if raw_volume not in (None, ""):
        volume = _number(raw_volume)
        if volume is None or not is_finite_between(volume, 0.0, 1_000_000_000_000.0):
            return False
    try:
        json.dumps(record, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _domestic_odds(domestic: dict, match_id: str) -> tuple[float, float, float] | None:
    try:
        had = domestic[str(match_id)]["had"]
        odds = (float(had["h"]), float(had["d"]), float(had["a"]))
    except (KeyError, TypeError, ValueError):
        return None
    return odds if valid_odds(odds) else None


def _calibrated_probability(features: dict, fallback: float, root: Path = ROOT) -> float:
    try:
        predictor = importlib.import_module("draw_model_learning").predict_draw_probability
        probability = float(predictor(features, root=root))
        return probability if 0 < probability < 1 else fallback
    except Exception:
        return fallback


def _capture_feature_snapshot(
    root: Path,
    prediction: dict,
    evidence: dict,
    snapshot_time: datetime,
    domestic_draw_odds: float,
    features: dict,
) -> Path | None:
    captured = _snapshot_write_time(snapshot_time)
    kickoff = _timestamp(evidence.get("kickoff_at"))
    if kickoff is None or captured > kickoff:
        return None
    if not _valid_snapshot_features(features):
        return None
    draw_odds = _number(domestic_draw_odds)
    if draw_odds is None or not 1.01 <= draw_odds <= 100.0:
        return None
    payload = {
        "snapshot_schema_version": 1,
        "date": str(prediction.get("date") or ""),
        "match_id": str(prediction.get("match_id") or ""),
        "team_a": str(prediction.get("team_a") or ""),
        "team_b": str(prediction.get("team_b") or ""),
        "stage": str(prediction.get("stage") or ""),
        "captured_at": captured.isoformat(),
        "kickoff_at": str(evidence.get("kickoff_at") or ""),
        "domestic_draw_odds": draw_odds,
        "features": features,
    }
    if not all(payload[name] for name in ("date", "match_id", "team_a", "team_b")):
        return None
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    timestamp = captured.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(root) / "data" / "draw_feature_snapshots" / f"{timestamp}-{digest}.json"
    _atomic_create_json(path, payload)
    return path


def _valid_snapshot_features(features: dict) -> bool:
    if not isinstance(features, dict) or list(features) != DRAW_MODEL_FEATURES:
        return False
    for name, bounds in DRAW_MODEL_FEATURE_RANGES.items():
        value = _number(features.get(name))
        if value is None or not bounds[0] <= value <= bounds[1]:
            return False
        if name in DRAW_MODEL_INTEGER_FEATURES and not value.is_integer():
            return False
    return True


def _snapshot_write_time(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(BEIJING)
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def _atomic_create_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=BEIJING)


def _quarter_kelly_stake(probability: float, odds: float, minimum: int, maximum: int, bankroll: int) -> int:
    full_kelly = max(0.0, (probability * odds - 1) / (odds - 1))
    stake = int(bankroll * full_kelly * 0.25)
    return min(maximum, max(minimum, stake))


def _subtype_metrics(metrics: dict, subtype: str) -> dict:
    subtypes = metrics.get("subtypes", metrics)
    value = subtypes.get(subtype, {}) if isinstance(subtypes, dict) else {}
    return value if isinstance(value, dict) else {}


def _league_is_paused(registry: dict, stage: str) -> bool:
    per_league = registry.get("per_league", {}) if isinstance(registry, dict) else {}
    state = per_league.get(str(stage), {}) if isinstance(per_league, dict) else {}
    return isinstance(state, dict) and state.get("paused") is True


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _number(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily draw alerts.")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    print(generate_alerts(args.date))


if __name__ == "__main__":
    main()
