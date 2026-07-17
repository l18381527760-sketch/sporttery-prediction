from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math

from official_markets import (
    OfficialMarket,
    THREE_WAY_SELECTIONS,
    TOTAL_GOALS_SELECTIONS,
    TRUSTED_SOURCES,
    parse_handicap,
    poisson_handicap_probabilities,
    poisson_total_probabilities,
)
from strategy_controls import apply_league_draw_calibration


@dataclass(frozen=True)
class ValueCandidate:
    candidate_id: str
    date: str
    match_id: str
    stage: str
    team_a: str
    team_b: str
    kickoff_at: str
    market_type: str
    play: str
    selection: str
    line: int | None
    official_odds: float
    official_market_probability: float
    raw_model_probability: float
    calibrated_model_probability: float
    conservative_probability: float
    probability_edge: float
    expected_value: float
    single_eligible: bool
    data_quality: str
    data_quality_multiplier: float
    volatility_band: str
    volatility_multiplier: float
    odds_source: str
    source_record_id: str
    captured_at_bjt: str
    correlation_tags: tuple[str, ...]
    paid_eligible: bool
    value_gate_reasons: tuple[str, ...]
    calibration_samples: int
    performance_multiplier: float = 1.0


@dataclass(frozen=True)
class OddsRisk:
    band: str
    multiplier: float
    eligible: bool


def conservative_probability(model: float, market: float, model_weight: float) -> float:
    value = _number(model)
    market_probability = _number(market)
    weight = _number(model_weight)
    if value is None or market_probability is None or weight is None:
        raise ValueError("probabilities and model weight must be finite")
    return min(0.999, max(0.001, market_probability + weight * (value - market_probability)))


def odds_volatility(opening_price: float | None, decision_price: float) -> OddsRisk:
    decision = _positive_price(decision_price)
    if decision is None:
        raise ValueError("decision price must be finite and greater than one")
    if opening_price is None:
        return OddsRisk("stable", 1.0, True)
    opening = _positive_price(opening_price)
    if opening is None:
        raise ValueError("opening price must be finite and greater than one")
    movement = abs(math.log(decision / opening))
    if movement <= 0.08:
        return OddsRisk("stable", 1.0, True)
    if movement <= 0.20:
        return OddsRisk("volatile", 0.75, True)
    return OddsRisk("unverified_jump", 0.0, False)


def build_candidates(
    predictions: list[dict],
    odds_by_match: dict,
    snapshot: dict,
    config: dict,
    league_calibrations: dict,
) -> list[ValueCandidate]:
    if not isinstance(predictions, list) or not isinstance(odds_by_match, dict):
        return []
    decision_matches, captured_at = _decision_matches(snapshot)
    if captured_at is None:
        return []
    opening_matches = _opening_matches(snapshot)
    value_config = config.get("value_strategy", {}) if isinstance(config, dict) else {}
    candidates = []
    for prediction in predictions:
        if not isinstance(prediction, dict):
            continue
        match_id = prediction.get("match_id")
        decision = decision_matches.get(match_id)
        markets = odds_by_match.get(match_id)
        if not isinstance(match_id, str) or decision is None or not isinstance(markets, dict):
            continue
        if not _same_match(prediction, decision, captured_at):
            continue
        for market_type, official_market in markets.items():
            market = _official_market(official_market)
            if market is None or market_type != market.market_type or market.match_id != match_id:
                continue
            if not _decision_market_matches(decision, market):
                continue
            opening = opening_matches.get(match_id)
            if not _same_snapshot_identity(prediction, opening):
                opening = None
            quality, quality_multiplier = _data_quality(market, opening)
            if quality == "low":
                continue
            selection_data = _selection_probabilities(prediction, market, league_calibrations)
            if selection_data is None:
                continue
            single_eligibility = decision.get("single_eligibility", {})
            single_eligible = bool(single_eligibility.get(market_type)) if isinstance(single_eligibility, dict) else False
            opening_market = _market_from_snapshot(opening, market_type)
            for selection, raw_probability, calibrated_probability, samples in selection_data:
                decision_price = market.prices[selection]
                risk = odds_volatility(
                    _snapshot_price(opening_market, selection), decision_price
                )
                if not risk.eligible:
                    continue
                model_weight = _model_weight(value_config, samples)
                conservative = conservative_probability(
                    calibrated_probability, market.fair_probabilities[selection], model_weight
                )
                edge = conservative - market.fair_probabilities[selection]
                expected_value = conservative * decision_price - 1.0
                reasons = _gate_reasons(value_config, samples, edge, expected_value)
                candidates.append(
                    ValueCandidate(
                        candidate_id=f"{match_id}:{market_type}:{selection}",
                        date=str(prediction.get("date", "")),
                        match_id=match_id,
                        stage=str(prediction.get("stage", "")),
                        team_a=str(prediction.get("team_a", "")),
                        team_b=str(prediction.get("team_b", "")),
                        kickoff_at=str(prediction.get("kickoff_at", "")),
                        market_type=market_type,
                        play=market_type.upper(),
                        selection=selection,
                        line=market.line,
                        official_odds=decision_price,
                        official_market_probability=market.fair_probabilities[selection],
                        raw_model_probability=raw_probability,
                        calibrated_model_probability=calibrated_probability,
                        conservative_probability=conservative,
                        probability_edge=edge,
                        expected_value=expected_value,
                        single_eligible=single_eligible,
                        data_quality=quality,
                        data_quality_multiplier=quality_multiplier,
                        volatility_band=risk.band,
                        volatility_multiplier=risk.multiplier,
                        odds_source=market.source,
                        source_record_id=market.source_record_id,
                        captured_at_bjt=market.captured_at_bjt,
                        correlation_tags=(f"league:{prediction.get('stage', '')}",),
                        paid_eligible=not reasons,
                        value_gate_reasons=reasons,
                        calibration_samples=samples,
                    )
                )
    return candidates


def _decision_matches(snapshot: dict) -> tuple[dict[str, dict], datetime | None]:
    if not isinstance(snapshot, dict) or snapshot.get("capture_phase", snapshot.get("phase")) != "decision":
        return {}, None
    captured_at = _datetime(snapshot.get("captured_at"))
    matches = snapshot.get("matches")
    if captured_at is None or not isinstance(matches, list):
        return {}, None
    return {
        row["match_id"]: row
        for row in matches
        if isinstance(row, dict) and isinstance(row.get("match_id"), str)
    }, captured_at


def _opening_matches(snapshot: dict) -> dict[str, dict]:
    if not isinstance(snapshot, dict):
        return {}
    opening = snapshot.get("opening_matches")
    if opening is None and isinstance(snapshot.get("opening"), dict):
        opening = snapshot["opening"].get("matches")
    if not isinstance(opening, list):
        return {}
    return {
        row["match_id"]: row
        for row in opening
        if isinstance(row, dict) and isinstance(row.get("match_id"), str)
    }


def _same_match(prediction: dict, decision: dict, captured_at: datetime) -> bool:
    if not _same_snapshot_identity(prediction, decision):
        return False
    kickoff = _datetime(decision.get("kickoff_at"))
    return kickoff is not None and kickoff > captured_at


def _same_snapshot_identity(prediction: dict, snapshot_match: dict | None) -> bool:
    return isinstance(snapshot_match, dict) and all(
        prediction.get(key) == snapshot_match.get(key)
        for key in ("team_a", "team_b", "kickoff_at")
    )


def _official_market(value: object) -> OfficialMarket | None:
    if isinstance(value, OfficialMarket):
        return value
    if isinstance(value, dict) and isinstance(value.get("market"), OfficialMarket):
        return value["market"]
    return None


def _decision_market_matches(decision: dict, market: OfficialMarket) -> bool:
    prices = _market_from_snapshot(decision, market.market_type)
    if not isinstance(prices, dict):
        return False
    if market.market_type == "hhad":
        try:
            if parse_handicap(prices.get("goalLine")) != market.line:
                return False
        except ValueError:
            return False
    return all(_same_price(_snapshot_price(prices, selection), price) for selection, price in market.prices.items())


def _data_quality(market: OfficialMarket, opening: dict | None) -> tuple[str, float]:
    if market.source not in TRUSTED_SOURCES:
        return "low", 0.0
    if market.source == "sporttery" and _market_from_snapshot(opening, market.market_type) is not None:
        return "high", 1.0
    if market.source:
        return "medium", 0.6
    return "low", 0.0


def _selection_probabilities(
    prediction: dict, market: OfficialMarket, league_calibrations: dict
) -> list[tuple[str, float, float, int]] | None:
    if market.market_type == "had":
        raw = {
            THREE_WAY_SELECTIONS["h"]: _probability(prediction.get("p_a")),
            THREE_WAY_SELECTIONS["d"]: _probability(prediction.get("p_draw")),
            THREE_WAY_SELECTIONS["a"]: _probability(prediction.get("p_b")),
        }
        if any(value is None for value in raw.values()):
            return None
        draw, state = apply_league_draw_calibration(
            raw[THREE_WAY_SELECTIONS["d"]], str(prediction.get("stage", "")), league_calibrations
        )
        samples = int(state.get("sample_count") or 0)
        return [
            (selection, probability, draw if selection == THREE_WAY_SELECTIONS["d"] else probability, samples if selection == THREE_WAY_SELECTIONS["d"] else 0)
            for selection, probability in raw.items()
        ]
    xg_a = _number(prediction.get("xg_a"))
    xg_b = _number(prediction.get("xg_b"))
    if xg_a is None or xg_b is None:
        return None
    try:
        probabilities = (
            poisson_handicap_probabilities(xg_a, xg_b, market.line)
            if market.market_type == "hhad"
            else poisson_total_probabilities(xg_a, xg_b)
            if market.market_type == "ttg"
            else None
        )
    except ValueError:
        return None
    if probabilities is None:
        return None
    return [(selection, probability, probability, 0) for selection, probability in probabilities.items()]


def _model_weight(value_config: dict, samples: int) -> float:
    settled = int(value_config.get("settled_samples", samples) or 0)
    strict = settled < int(value_config.get("strict_until_samples", 100))
    base = _number(value_config.get("strict_model_edge_weight_base" if strict else "model_edge_weight_base"))
    maximum = _number(value_config.get("strict_model_edge_weight_max" if strict else "model_edge_weight_max"))
    base = 0.0 if base is None else base
    maximum = base if maximum is None else maximum
    prior = max(1.0, _number(value_config.get("calibration_prior")) or 100.0)
    return min(1.0, max(0.0, base + (maximum - base) * settled / (settled + prior)))


def _gate_reasons(value_config: dict, samples: int, edge: float, expected_value: float) -> tuple[str, ...]:
    strict = int(value_config.get("settled_samples", samples) or 0) < int(value_config.get("strict_until_samples", 100))
    edge_key = "strict_min_probability_edge" if strict else "min_probability_edge"
    return_key = "strict_min_expected_return" if strict else "min_expected_return"
    edge_threshold = _number(value_config.get(edge_key)) or 0.0
    expected_threshold = _number(value_config.get(
        "strict_min_expected_value" if strict else "min_expected_value",
        value_config.get(return_key, 0.0),
    )) or 0.0
    if expected_threshold >= 1.0:
        expected_threshold -= 1.0
    return tuple(
        reason
        for reason, passed in (("probability_edge", edge >= edge_threshold), ("expected_value", expected_value >= expected_threshold))
        if not passed
    )


def _market_from_snapshot(row: dict | None, market_type: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    markets = row.get("markets")
    if not isinstance(markets, dict):
        return None
    market = markets.get(market_type)
    return market if isinstance(market, dict) else None


def _snapshot_price(market: dict | None, selection: str) -> float | None:
    if not isinstance(market, dict):
        return None
    for key, label in {**THREE_WAY_SELECTIONS, **TOTAL_GOALS_SELECTIONS}.items():
        if label == selection:
            return _positive_price(market.get(key))
    return None


def _same_price(left: object, right: object) -> bool:
    left_number = _positive_price(left)
    right_number = _positive_price(right)
    return left_number is not None and right_number is not None and math.isclose(left_number, right_number, rel_tol=0.0, abs_tol=1e-12)


def _probability(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and 0.0 <= number <= 1.0 else None


def _positive_price(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 1.0 else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
