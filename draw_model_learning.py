"""Chronological champion/challenger learning for draw probabilities."""

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent
FEATURES = [
    "base_draw_probability",
    "market_draw_probability",
    "favorite_probability",
    "win_probability_gap",
    "xg_total",
    "favorite_movement",
    "regional_gap",
    "source_count",
    "is_knockout",
    "is_balanced",
]
SMALL_SAMPLE_FEATURES = FEATURES[:2]
SAMPLE_FIELDS = ["date", "match_id", "team_a", "team_b", "stage", "outcome", *FEATURES]
ARTIFACT_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
MIN_FULL_FEATURE_SAMPLES = 200
TRAINING_INTERVAL_DAYS = 7
WEIGHT_HALF_LIFE_DAYS = 180


def chronological_splits(dates: list[date], n_splits: int):
    indices = list(range(len(dates)))
    for train, validation in TimeSeriesSplit(n_splits=n_splits).split(indices):
        yield list(train), list(validation)


def promotion_decision(challenger: dict, champion: dict) -> bool:
    return all((
        challenger.get("shadow_days", 0) >= 28,
        challenger.get("sample_count", 0) >= 200,
        challenger.get("bet_count", 0) >= 100,
        challenger.get("brier_improvement", 0) >= 0.02,
        challenger.get("brier_skill", 0) > 0,
        challenger.get("clv", 0) > 0,
        challenger.get("roi", 0) > 0,
        challenger.get("max_drawdown", float("inf")) <= champion.get("max_drawdown", float("inf")),
    ))


def rollback_decision(current: dict, previous: dict) -> bool:
    for field in ("brier", "log_loss"):
        current_value = _number(current.get(field))
        previous_value = _number(previous.get(field))
        if current_value is None or previous_value is None:
            continue
        if previous_value == 0:
            if current_value > 0:
                return True
        elif current_value / previous_value >= 1.02 - 1e-12:
            return True
    return False


def league_pause_states(rows: list[dict]) -> dict:
    grouped = {}
    for row in rows:
        stage = str(row.get("stage") or "unknown")
        outcome = _binary_outcome(row.get("outcome"))
        probability = _number(row.get("model_draw_probability"))
        if outcome is None or probability is None or not 0 <= probability <= 1:
            continue
        grouped.setdefault(stage, []).append((row, outcome, probability))

    states = {}
    for stage, settled in grouped.items():
        stake = sum(_number(row.get("hypothetical_stake")) or 0.0 for row, _, _ in settled)
        profit = sum(_number(row.get("hypothetical_profit")) or 0.0 for row, _, _ in settled)
        roi = profit / stake if stake else 0.0
        previous_ten = settled[-20:-10]
        recent_ten = settled[-10:]
        previous_brier = _brier_from_tuples(previous_ten)
        recent_brier = _brier_from_tuples(recent_ten)
        paused = (
            len(settled) >= 30
            and roi < 0
            and previous_brier is not None
            and recent_brier is not None
            and recent_brier > previous_brier
        )
        states[stage] = {
            "paused": paused,
            "sample_count": len(settled),
            "roi": roi,
            "previous_ten_brier": previous_brier,
            "recent_ten_brier": recent_brier,
        }
    return states


def predict_draw_probability(features: dict, *, root: Path = ROOT) -> float:
    fallback = float(features["base_draw_probability"])
    root = Path(root)
    try:
        registry = _read_registry(root / "output" / "draw_model_registry.json")
        champion = registry.get("champion")
        if not isinstance(champion, dict):
            return fallback
        artifact_path = root / champion["artifact"]
        artifact = _load_artifact(artifact_path)
        feature_order = artifact["feature_order"]
        values = [[_feature_value(features, name, fallback) for name in feature_order]]
        probability = float(artifact["model"].predict_proba(values)[0][1])
        if not math.isfinite(probability):
            return fallback
        return min(0.70, max(0.03, probability))
    except (OSError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return fallback


def build_training_samples(root: Path = ROOT, as_of: date | None = None) -> list[dict]:
    root = Path(root)
    cutoff = as_of or date.today()
    results = _read_csv(root / "data" / "bet_results.csv")
    result_by_match = {
        (str(row.get("date", "")), str(row.get("team_a", "")), str(row.get("team_b", ""))): row
        for row in results
    }
    odds_cache = {}
    evidence_cache = {}
    config = _read_json(root / "config.json", {})
    knockout_stages = {str(value).casefold() for value in config.get("knockout_stages", [])}
    samples = []
    seen = set()

    for path in sorted((root / "output").glob("predictions_*.csv")):
        for prediction in _read_csv(path):
            target_date = _parse_date(prediction.get("date"))
            if target_date is None or target_date > cutoff:
                continue
            key = (
                target_date.isoformat(),
                str(prediction.get("team_a", "")),
                str(prediction.get("team_b", "")),
            )
            result = result_by_match.get(key)
            if result is None:
                continue
            home_goals = _goal(result.get("home_goals"))
            away_goals = _goal(result.get("away_goals"))
            if home_goals is None or away_goals is None:
                continue
            sample_key = (*key, str(prediction.get("match_id", "")))
            if sample_key in seen:
                continue
            numeric = _prediction_numbers(prediction)
            if numeric is None:
                continue
            seen.add(sample_key)
            p_a, p_draw, p_b, xg_a, xg_b = numeric
            odds = odds_cache.setdefault(
                target_date.isoformat(),
                _read_json(root / "data" / f"sporttery_odds_{target_date.isoformat()}.json", {}),
            )
            market_probability = _market_draw_probability(
                odds, str(prediction.get("match_id", ""))
            )
            evidence_by_match = evidence_cache.setdefault(
                target_date.isoformat(),
                _market_evidence(root, target_date),
            )
            evidence = evidence_by_match.get(str(prediction.get("match_id", "")), {})
            stage = str(prediction.get("stage", ""))
            samples.append({
                "date": target_date.isoformat(),
                "match_id": str(prediction.get("match_id", "")),
                "team_a": key[1],
                "team_b": key[2],
                "stage": stage,
                "outcome": int(home_goals == away_goals),
                "base_draw_probability": p_draw,
                "market_draw_probability": market_probability if market_probability is not None else p_draw,
                "favorite_probability": max(p_a, p_b),
                "win_probability_gap": abs(p_a - p_b),
                "xg_total": xg_a + xg_b,
                "favorite_movement": _number(evidence.get("favorite_movement")) or 0.0,
                "regional_gap": _number(evidence.get("regional_gap")) or 0.0,
                "source_count": _source_count(evidence),
                "is_knockout": int(stage.casefold() in knockout_stages),
                "is_balanced": int(abs(p_a - p_b) <= 0.10),
            })
    samples.sort(key=lambda row: (row["date"], row["match_id"], row["team_a"], row["team_b"]))
    return samples


def update_draw_model(
    root: Path = ROOT,
    as_of: date | None = None,
    force_train: bool = False,
) -> Path:
    root = Path(root)
    current_date = as_of or date.today()
    model_dir = root / "data" / "models"
    output_dir = root / "output"
    model_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_path = output_dir / "draw_model_registry.json"
    registry = _load_or_initialize_registry(registry_path)

    try:
        samples = build_training_samples(root, as_of=current_date)
        _atomic_write_csv(root / "data" / "draw_training_samples.csv", SAMPLE_FIELDS, samples)
        registry["per_league"] = league_pause_states(
            _read_csv(output_dir / "draw_alert_ledger.csv")
        )
        _rollback_if_needed(root, registry, samples, current_date)
        resolved_challenger = _advance_challenger(root, registry, samples, current_date)

        if (
            registry.get("challenger") is None
            and not resolved_challenger
            and _training_is_due(registry, current_date, force_train)
        ):
            artifact = _train_artifact(samples, as_of=current_date)
            challenger_path = model_dir / "draw_challenger.joblib"
            _atomic_dump_artifact(artifact, challenger_path)
            registry["challenger"] = _challenger_entry(
                artifact, root, challenger_path, samples, current_date
            )
            registry["last_training_date"] = current_date.isoformat()
            registry["last_training_error"] = None
    except Exception as error:
        registry["last_training_error"] = f"{type(error).__name__}: {error}"

    registry["schema_version"] = REGISTRY_SCHEMA_VERSION
    registry["updated_at"] = current_date.isoformat()
    _atomic_write_json(registry_path, registry)
    return registry_path


def _train_artifact(rows: list[dict], as_of: date) -> dict:
    if len(rows) < 2:
        raise ValueError("at least two settled samples are required")
    feature_order = list(
        SMALL_SAMPLE_FEATURES if len(rows) < MIN_FULL_FEATURE_SAMPLES else FEATURES
    )
    model_kind = (
        "sigmoid_calibrator"
        if len(rows) < MIN_FULL_FEATURE_SAMPLES
        else "full_feature_logistic"
    )
    x_values = np.asarray(
        [[float(row[name]) for name in feature_order] for row in rows], dtype=float
    )
    outcomes = np.asarray([int(row["outcome"]) for row in rows], dtype=int)
    if len(set(outcomes.tolist())) < 2:
        raise ValueError("training samples must contain both draw and non-draw outcomes")
    weights = _sample_weights(rows, as_of)
    fold_metrics = _cross_validation_metrics(
        rows, x_values, outcomes, weights, feature_order, model_kind
    )
    model = _new_model(model_kind)
    _fit_model(model, x_values, outcomes, weights, model_kind)
    digest_input = [
        [row.get("date"), row.get("match_id"), row.get("outcome"), *[row.get(name) for name in feature_order]]
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(digest_input, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    version = f"draw-{as_of.strftime('%Y%m%d')}-{digest}"
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "feature_order": feature_order,
        "metadata": {
            "version": version,
            "model_kind": model_kind,
            "trained_as_of": as_of.isoformat(),
            "sample_count": len(rows),
            "weight_half_life_days": WEIGHT_HALF_LIFE_DAYS,
            "fold_metrics": fold_metrics,
        },
        "model": model,
    }


def _cross_validation_metrics(rows, x_values, outcomes, weights, feature_order, model_kind):
    if len(rows) < 3:
        return []
    metrics = []
    n_splits = min(5, len(rows) - 1)
    dates = [_parse_date(row["date"]) for row in rows]
    for fold, (train, validation) in enumerate(
        chronological_splits(dates, n_splits=n_splits), start=1
    ):
        if len(set(outcomes[train].tolist())) < 2:
            continue
        model = _new_model(model_kind)
        _fit_model(model, x_values[train], outcomes[train], weights[train], model_kind)
        probabilities = model.predict_proba(x_values[validation])[:, 1]
        market = np.asarray(
            [float(rows[index]["market_draw_probability"]) for index in validation]
        )
        actual = outcomes[validation]
        metrics.append({
            "fold": fold,
            "training_end": max(dates[index] for index in train).isoformat(),
            "validation_start": min(dates[index] for index in validation).isoformat(),
            "validation_count": len(validation),
            "brier": float(brier_score_loss(actual, probabilities)),
            "log_loss": float(log_loss(actual, probabilities, labels=[0, 1])),
            "market_brier": float(brier_score_loss(actual, market)),
            "market_log_loss": float(log_loss(actual, market, labels=[0, 1])),
        })
    return metrics


def _new_model(model_kind):
    logistic = LogisticRegression(C=0.5, max_iter=1000, random_state=42)
    if model_kind == "full_feature_logistic":
        return Pipeline([("standardscaler", StandardScaler()), ("logisticregression", logistic)])
    return logistic


def _fit_model(model, x_values, outcomes, weights, model_kind):
    if model_kind == "full_feature_logistic":
        model.fit(x_values, outcomes, logisticregression__sample_weight=weights)
    else:
        model.fit(x_values, outcomes, sample_weight=weights)


def _sample_weights(rows, as_of):
    decay = math.log(2) / WEIGHT_HALF_LIFE_DAYS
    return np.asarray([
        math.exp(-decay * max(0, (as_of - _parse_date(row["date"])).days))
        for row in rows
    ])


def _advance_challenger(root, registry, samples, current_date):
    challenger = registry.get("challenger")
    if not isinstance(challenger, dict):
        return False
    created_on = _parse_date(challenger.get("created_on"))
    challenger["shadow_days"] = max(0, (current_date - created_on).days) if created_on else 0
    shadow_start = created_on or current_date
    artifact_path = _registry_artifact_path(root, challenger)
    try:
        artifact = _load_artifact(artifact_path)
    except (OSError, ValueError, TypeError, KeyError):
        registry["last_training_error"] = "Invalid active challenger artifact"
        registry["challenger"] = None
        return True

    shadow_samples = [
        row
        for row in samples
        if _parse_date(row.get("date")) is not None
        and _parse_date(row.get("date")) > shadow_start
    ]
    challenger.update(_shadow_metrics(artifact, shadow_samples, root, since=shadow_start))
    if promotion_decision(challenger, registry.get("champion") or {}):
        _promote_challenger(root, registry, artifact, challenger, current_date)
        return True
    if challenger["shadow_days"] >= 28:
        registry["last_model_event"] = {
            "type": "rejection",
            "version": challenger.get("version"),
            "date": current_date.isoformat(),
        }
        registry["challenger"] = None
        return True
    return False


def _promote_challenger(root, registry, artifact, challenger, current_date):
    champion_path = root / "data" / "models" / "draw_champion.joblib"
    previous_path = root / "data" / "models" / "draw_previous_champion.joblib"
    old_champion = registry.get("champion")
    if isinstance(old_champion, dict):
        old_artifact = _load_artifact(_registry_artifact_path(root, old_champion))
        _atomic_dump_artifact(old_artifact, previous_path)
        registry["previous_champion"] = {
            **old_champion,
            "artifact": _relative_path(root, previous_path),
        }
    _atomic_dump_artifact(artifact, champion_path)
    registry["champion"] = {
        **challenger,
        "artifact": _relative_path(root, champion_path),
        "promoted_on": current_date.isoformat(),
    }
    registry["challenger"] = None
    registry["last_model_event"] = {
        "type": "promotion",
        "version": challenger.get("version"),
        "date": current_date.isoformat(),
    }


def _rollback_if_needed(root, registry, samples, current_date):
    champion = registry.get("champion")
    previous = registry.get("previous_champion")
    if not isinstance(champion, dict) or not isinstance(previous, dict) or len(samples) < 50:
        return
    current_artifact = _load_artifact(_registry_artifact_path(root, champion))
    previous_artifact = _load_artifact(_registry_artifact_path(root, previous))
    latest = samples[-50:]
    current_metrics = _artifact_metrics(current_artifact, latest)
    previous_metrics = _artifact_metrics(previous_artifact, latest)
    champion["recent_50"] = current_metrics
    previous["recent_50"] = previous_metrics
    if not rollback_decision(current_metrics, previous_metrics):
        return
    champion_path = root / "data" / "models" / "draw_champion.joblib"
    previous_path = root / "data" / "models" / "draw_previous_champion.joblib"
    _atomic_dump_artifact(previous_artifact, champion_path)
    _atomic_dump_artifact(current_artifact, previous_path)
    registry["champion"] = {**previous, "artifact": _relative_path(root, champion_path)}
    registry["previous_champion"] = {**champion, "artifact": _relative_path(root, previous_path)}
    registry["last_model_event"] = {
        "type": "rollback",
        "from_version": champion.get("version"),
        "to_version": previous.get("version"),
        "date": current_date.isoformat(),
        "current_recent_50": current_metrics,
        "previous_recent_50": previous_metrics,
    }


def _challenger_entry(artifact, root, path, samples, current_date):
    metadata = artifact["metadata"]
    entry = {
        "version": metadata["version"],
        "artifact": _relative_path(root, path),
        "feature_order": artifact["feature_order"],
        "model_kind": metadata["model_kind"],
        "created_on": current_date.isoformat(),
        "shadow_days": 0,
        "sample_count": len(samples),
        "fold_metrics": metadata["fold_metrics"],
    }
    entry.update(_shadow_metrics(artifact, [], root, since=current_date))
    return entry


def _shadow_metrics(artifact, samples, root, since):
    metrics = _artifact_metrics(artifact, samples) if samples else {}
    base_brier = _probability_brier(samples, "base_draw_probability")
    market_brier = _probability_brier(samples, "market_draw_probability")
    model_brier = metrics.get("brier")
    ledger = [
        row
        for row in _read_csv(root / "output" / "draw_alert_ledger.csv")
        if _binary_outcome(row.get("outcome")) is not None
        and _parse_date(row.get("date")) is not None
        and _parse_date(row.get("date")) > since
    ]
    stakes = [_number(row.get("hypothetical_stake")) or 0.0 for row in ledger]
    profits = [_number(row.get("hypothetical_profit")) or 0.0 for row in ledger]
    clv_values = [value for value in (_number(row.get("clv")) for row in ledger) if value is not None]
    return {
        **metrics,
        "sample_count": len(samples),
        "bet_count": len(ledger),
        "brier_improvement": ((base_brier - model_brier) / base_brier) if base_brier and model_brier is not None else 0.0,
        "brier_skill": (1.0 - model_brier / market_brier) if market_brier and model_brier is not None else 0.0,
        "clv": sum(clv_values) / len(clv_values) if clv_values else 0.0,
        "roi": sum(profits) / sum(stakes) if sum(stakes) else 0.0,
        "max_drawdown": _max_drawdown(profits),
    }


def _artifact_metrics(artifact, rows):
    if not rows:
        return {}
    feature_order = artifact["feature_order"]
    values = [[_feature_value(row, name, float(row["base_draw_probability"])) for name in feature_order] for row in rows]
    outcomes = [int(row["outcome"]) for row in rows]
    probabilities = artifact["model"].predict_proba(values)
    probabilities = [float(row[1]) for row in probabilities]
    return {
        "brier": float(brier_score_loss(outcomes, probabilities)),
        "log_loss": float(log_loss(outcomes, probabilities, labels=[0, 1])),
    }


def _training_is_due(registry, current_date, force_train):
    if force_train:
        return True
    last_training = _parse_date(registry.get("last_training_date"))
    return last_training is None or (current_date - last_training).days >= TRAINING_INTERVAL_DAYS


def _load_or_initialize_registry(path):
    if not path.exists():
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "champion": None,
            "challenger": None,
            "previous_champion": None,
            "per_league": {},
            "last_training_date": None,
            "last_training_error": None,
        }
    registry = _read_registry(path)
    if not isinstance(registry, dict):
        raise ValueError("draw model registry must be a JSON object")
    registry.setdefault("champion", None)
    registry.setdefault("challenger", None)
    registry.setdefault("previous_champion", None)
    registry.setdefault("per_league", {})
    registry.setdefault("last_training_date", None)
    registry.setdefault("last_training_error", None)
    return registry


def _atomic_dump_artifact(artifact, path):
    path = Path(path)
    temporary = _temporary_path(path)
    try:
        joblib.dump(artifact, temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path, payload):
    path = Path(path)
    temporary = _temporary_path(path)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_csv(path, fieldnames, rows):
    path = Path(path)
    temporary = _temporary_path(path)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_path(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    return Path(name)


def _load_artifact(path):
    artifact = joblib.load(path)
    if not isinstance(artifact, dict):
        raise ValueError("model artifact must be a dictionary")
    if artifact.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported model artifact schema")
    feature_order = artifact.get("feature_order")
    if not isinstance(feature_order, list) or not feature_order:
        raise ValueError("model artifact has no feature order")
    if not hasattr(artifact.get("model"), "predict_proba"):
        raise ValueError("model artifact cannot predict probabilities")
    metadata = artifact.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("version"):
        raise ValueError("model artifact has no version metadata")
    return artifact


def _market_evidence(root, target_date):
    payload = _read_json(root / "data" / f"market_heat_{target_date.isoformat()}.json", {})
    return {
        str(row.get("match_id", "")): row
        for row in payload.get("matches", [])
        if isinstance(row, dict) and row.get("match_id") not in (None, "")
    }


def _source_count(evidence):
    sources = evidence.get("sources", {}) if isinstance(evidence, dict) else {}
    if not isinstance(sources, dict):
        return 0
    return sum(
        1
        for record in sources.values()
        if isinstance(record, dict)
        and record.get("market_type") == "win_draw_loss"
        and _number(record.get("settlement_minutes")) == 90
        and record.get("includes_extra_time") is False
    )


def _market_draw_probability(payload, match_id):
    try:
        odds = payload[str(match_id)]["had"]
        values = [float(odds[name]) for name in ("h", "d", "a")]
        if any(value <= 0 for value in values):
            return None
        implied = [1.0 / value for value in values]
        return implied[1] / sum(implied)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _prediction_numbers(row):
    values = [_number(row.get(name)) for name in ("p_a", "p_draw", "p_b", "xg_a", "xg_b")]
    if any(value is None for value in values):
        return None
    p_a, p_draw, p_b, xg_a, xg_b = values
    if not all(0 <= value <= 1 for value in (p_a, p_draw, p_b)):
        return None
    return p_a, p_draw, p_b, xg_a, xg_b


def _feature_value(features, name, fallback):
    if name == "market_draw_probability":
        return _number(features.get(name)) if _number(features.get(name)) is not None else fallback
    value = _number(features.get(name))
    return value if value is not None else 0.0


def _probability_brier(rows, field):
    if not rows:
        return None
    outcomes = [int(row["outcome"]) for row in rows]
    probabilities = [float(row[field]) for row in rows]
    return float(brier_score_loss(outcomes, probabilities))


def _brier_from_tuples(rows):
    if not rows:
        return None
    return sum((probability - outcome) ** 2 for _, outcome, probability in rows) / len(rows)


def _max_drawdown(profits):
    cumulative = peak = drawdown = 0.0
    for profit in profits:
        cumulative += profit
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def _read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_registry(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _relative_path(root, path):
    return path.relative_to(root).as_posix()


def _registry_artifact_path(root, entry):
    return root / str(entry["artifact"])


def _parse_date(value):
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _goal(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and str(value).strip() == str(number) else None


def _binary_outcome(value):
    number = _number(value)
    return int(number) if number in (0.0, 1.0) else None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", action="store_true", help="update the draw model registry")
    parser.add_argument("--date", help="training cutoff in YYYY-MM-DD format")
    parser.add_argument("--force", action="store_true", help="ignore the weekly training interval")
    arguments = parser.parse_args(argv)
    if not arguments.train:
        parser.error("--train is required")
    try:
        target_date = date.fromisoformat(arguments.date) if arguments.date else None
        path = update_draw_model(as_of=target_date, force_train=arguments.force)
        print(path)
        return 0
    except Exception as error:
        print(f"draw model orchestration failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
