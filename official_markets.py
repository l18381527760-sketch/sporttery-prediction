from dataclasses import dataclass
import math
import re


THREE_WAY_SELECTIONS = {"h": "胜", "d": "平", "a": "负"}
TOTAL_GOALS_SELECTIONS = {
    **{f"s{goals}": f"{goals}球" for goals in range(7)},
    "s7": "7+球",
}
COMPLETE_SELECTION_SETS = (
    frozenset(THREE_WAY_SELECTIONS),
    frozenset(THREE_WAY_SELECTIONS.values()),
    frozenset(TOTAL_GOALS_SELECTIONS),
    frozenset(TOTAL_GOALS_SELECTIONS.values()),
)
TRUSTED_SOURCES = frozenset({"sporttery", "竞彩网", "zgzcw", "中国足彩网"})
METADATA_KEYS = frozenset(
    {
        "source",
        "source_record_id",
        "sourceRecordId",
        "captured_at_bjt",
        "capturedAtBjt",
        "captured_at",
    }
)
HANDICAP_PATTERN = re.compile(r"^[+-]?\d+$")


@dataclass(frozen=True)
class OfficialMarket:
    match_id: str
    market_type: str
    line: int | None
    prices: dict[str, float]
    fair_probabilities: dict[str, float]
    source: str
    source_record_id: str
    captured_at_bjt: str


def devig(prices: dict[str, float]) -> dict[str, float]:
    if not isinstance(prices, dict) or len(prices) < 3:
        raise ValueError("market must contain every outcome")
    if frozenset(prices) not in COMPLETE_SELECTION_SETS:
        raise ValueError("market must contain every outcome")

    normalized_prices: dict[str, float] = {}
    for selection, value in prices.items():
        try:
            price = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("market price must be numeric") from exc
        if not math.isfinite(price) or price <= 1.0:
            raise ValueError("market price must be finite and greater than 1.0")
        normalized_prices[selection] = price

    inverse_prices = {selection: 1.0 / price for selection, price in normalized_prices.items()}
    total_inverse = sum(inverse_prices.values())
    if not math.isfinite(total_inverse) or total_inverse <= 0.0:
        raise ValueError("market probabilities must be finite")
    return {selection: inverse / total_inverse for selection, inverse in inverse_prices.items()}


def poisson_total_probabilities(xg_home: float, xg_away: float) -> dict[str, float]:
    home = _validate_xg(xg_home)
    away = _validate_xg(xg_away)
    mean = home + away
    probabilities = _poisson_vector(mean, 6)
    totals = {f"{goals}球": probability for goals, probability in enumerate(probabilities)}
    totals["7+球"] = 1.0 - sum(probabilities)
    return totals


def poisson_handicap_probabilities(
    xg_home: float, xg_away: float, handicap: int
) -> dict[str, float]:
    home = _validate_xg(xg_home)
    away = _validate_xg(xg_away)
    line = parse_handicap(handicap)
    home_probabilities = _poisson_vector(home, 20)
    away_probabilities = _poisson_vector(away, 20)
    joint_total = sum(home_probability * away_probability for home_probability in home_probabilities for away_probability in away_probabilities)
    if not math.isfinite(joint_total) or joint_total <= 0.0:
        raise ValueError("poisson grid must be finite")

    outcomes = {"胜": 0.0, "平": 0.0, "负": 0.0}
    for home_goals, home_probability in enumerate(home_probabilities):
        for away_goals, away_probability in enumerate(away_probabilities):
            probability = home_probability * away_probability / joint_total
            adjusted_home_goals = home_goals + line
            if adjusted_home_goals > away_goals:
                outcomes["胜"] += probability
            elif adjusted_home_goals == away_goals:
                outcomes["平"] += probability
            else:
                outcomes["负"] += probability
    return outcomes


def parse_handicap(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("handicap must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        raise ValueError("handicap must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if HANDICAP_PATTERN.fullmatch(text):
            return int(text)
    raise ValueError("handicap must be an integer")


def normalize_market(match_id: str, market_type: str, raw: dict) -> OfficialMarket | None:
    if not _is_canonical_match_id(match_id) or not isinstance(raw, dict):
        return None
    if not isinstance(market_type, str):
        return None

    market = market_type.strip().lower()
    selection_map = _selection_map(market)
    if selection_map is None:
        return None

    nested_prices = "prices" in raw
    prices_raw = raw["prices"] if nested_prices else raw
    if not isinstance(prices_raw, dict):
        return None
    selection_keys = set(selection_map)
    price_keys = set(prices_raw)
    if not nested_prices:
        price_keys -= METADATA_KEYS
    if market == "hhad":
        price_keys.discard("goalLine")
    if price_keys != selection_keys:
        return None
    allowed_raw_keys = METADATA_KEYS | ({"prices"} if nested_prices else selection_keys)
    if market == "hhad":
        allowed_raw_keys = allowed_raw_keys | {"goalLine"}
    if not set(raw).issubset(allowed_raw_keys):
        return None
    if market == "hhad" and "goalLine" not in raw and "goalLine" not in prices_raw:
        return None

    try:
        prices = {selection_map[key]: float(prices_raw[key]) for key in selection_map}
        fair_probabilities = devig(prices)
        line = (
            parse_handicap(raw.get("goalLine", prices_raw.get("goalLine")))
            if market == "hhad"
            else None
        )
    except (TypeError, ValueError):
        return None

    source = _trusted_source(raw)
    source_record_id = _required_string(raw, "source_record_id", "sourceRecordId")
    captured_at_bjt = _required_string(raw, "captured_at_bjt", "capturedAtBjt", "captured_at")
    if source is None or source_record_id is None or captured_at_bjt is None:
        return None

    return OfficialMarket(
        match_id=match_id,
        market_type=market,
        line=line,
        prices=prices,
        fair_probabilities=fair_probabilities,
        source=source,
        source_record_id=source_record_id,
        captured_at_bjt=captured_at_bjt,
    )


def _validate_xg(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("xG must be a finite value between 0 and 8")
    try:
        xg = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("xG must be a finite value between 0 and 8") from exc
    if not math.isfinite(xg) or not 0.0 <= xg <= 8.0:
        raise ValueError("xG must be a finite value between 0 and 8")
    return xg


def _poisson_vector(mean: float, maximum: int) -> list[float]:
    probabilities = [math.exp(-mean)]
    for goals in range(1, maximum + 1):
        probabilities.append(probabilities[-1] * mean / goals)
    return probabilities


def _selection_map(market_type: str) -> dict[str, str] | None:
    if market_type in {"had", "hhad"}:
        return THREE_WAY_SELECTIONS
    if market_type == "ttg":
        return TOTAL_GOALS_SELECTIONS
    return None


def _required_string(raw: dict, *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _trusted_source(raw: dict) -> str | None:
    source = _required_string(raw, "source")
    if source is None:
        return None
    normalized = source.lower() if source.isascii() else source
    return normalized if normalized in TRUSTED_SOURCES else None


def _is_canonical_match_id(match_id: object) -> bool:
    return (
        isinstance(match_id, str)
        and bool(match_id)
        and match_id == match_id.strip()
        and all(not character.isspace() and character.isprintable() for character in match_id)
    )
