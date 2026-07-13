from dataclasses import dataclass


QUALITY = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class DrawInputs:
    match_id: str
    team_a: str
    team_b: str
    stage: str
    domestic_odds: tuple[float, float, float]
    model_probabilities: tuple[float, float, float]
    calibrated_draw_probability: float
    xg_total: float
    source_count: int
    market_scope: str
    favorite_movement: float
    regional_gap: float
    underdog_win_probability: float
    underdog_not_lose_probability: float
    structural_signals: tuple[str, ...]
    data_quality: str


@dataclass(frozen=True)
class DrawCandidate:
    inputs: DrawInputs
    subtype: str
    domestic_draw_probability: float
    draw_edge: float
    expected_value: float
    score: float


def fair_probabilities(home_odds: float, draw_odds: float, away_odds: float) -> tuple[float, float, float]:
    implied = (1 / home_odds, 1 / draw_odds, 1 / away_odds)
    total = sum(implied)
    return tuple(value / total for value in implied)


def classify_candidate(inputs: DrawInputs, config: dict) -> DrawCandidate | None:
    if inputs.market_scope != "90m" or inputs.source_count < 2 or inputs.data_quality == "low":
        return None
    fair = fair_probabilities(*inputs.domestic_odds)
    probability = inputs.calibrated_draw_probability
    edge = probability - fair[1]
    expected_value = probability * inputs.domestic_odds[1]
    if probability < config["min_draw_probability"] or edge < config["min_draw_edge"]:
        return None
    if expected_value < config["min_expected_value"] or inputs.xg_total > config["max_xg_total"]:
        return None
    favorite = max(fair[0], fair[2])
    win_gap = abs(fair[0] - fair[2])
    if favorite >= config["cold_favorite_probability"] or win_gap > config["balanced_max_win_gap"]:
        enough_heat = inputs.favorite_movement <= -0.04 or inputs.regional_gap >= 0.05
        enough_resistance = inputs.underdog_not_lose_probability >= 0.35 and probability > inputs.underdog_win_probability
        subtype = "cold_draw" if enough_heat and enough_resistance and len(inputs.structural_signals) >= 2 else ""
    else:
        subtype = "balanced_draw" if win_gap <= config["balanced_max_win_gap"] and inputs.xg_total <= config["balanced_max_xg_total"] and len(inputs.structural_signals) >= 2 else ""
    if not subtype:
        return None
    score = edge * 4 + (expected_value - 1) * 2 + probability + QUALITY[inputs.data_quality] * 0.02
    return DrawCandidate(inputs, subtype, fair[1], edge, expected_value, score)


def rank_candidates(candidates: list[DrawCandidate]) -> list[DrawCandidate]:
    return sorted(candidates, key=lambda item: (item.score, QUALITY[item.inputs.data_quality]), reverse=True)
