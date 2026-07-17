"""Deterministic, simulation-only allocation for verified value candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Iterable

from official_markets import THREE_WAY_SELECTIONS, TOTAL_GOALS_SELECTIONS, TRUSTED_SOURCES
from value_candidates import ValueCandidate


SUPPORTED_MARKETS = frozenset({"had", "hhad", "ttg"})
PLAY_BY_MARKET = {"had": "HAD", "hhad": "HHAD", "ttg": "TTG"}
THREE_WAY_SELECTION_LABELS = frozenset(THREE_WAY_SELECTIONS.values())
TOTAL_GOALS_SELECTION_LABELS = frozenset(TOTAL_GOALS_SELECTIONS.values())
QUALITY_RANK = {"high": 0, "medium": 1}
VOLATILITY_RANK = {"stable": 0, "volatile": 1}
QUARTER_KELLY = 0.25
MAX_PERFORMANCE_MULTIPLIER = 1.15
HARD_MAX_MATCH_EXPOSURE = 200
HARD_MAX_SINGLE_STAKE = 200
HARD_SINGLE_BUDGET_CAP = 200
HARD_MAX_SINGLE_COUNT = 2
HARD_MAX_PARLAY_STAKE = 30
HARD_MAX_DAILY_STAKE = 500
HARD_MONTHLY_BUDGET_CAP = 5000
HARD_MONTHLY_STOP_LOSS = 5000


@dataclass(frozen=True)
class PortfolioLimits:
    bankroll: float = 5000
    kelly_fraction: float = QUARTER_KELLY
    stake_unit: int = 2
    max_match_exposure: int = 200
    max_single_stake: int = 200
    single_budget_cap: int = 200
    max_single_count: int = 2
    min_single_stake: int = 2
    max_parlay_stake: int = 30
    min_parlay_stake: int = 2
    max_daily_stake: int = 500
    monthly_budget_cap: int = 5000
    monthly_stop_loss: int = 5000
    settled_samples: int = 0
    strict_until_samples: int = 100
    min_combo_leg_probability: float = 0.45
    min_combo_leg_edge: float = 0.03
    min_combo_leg_ev: float = 0.02
    min_combo_ev: float = 0.10


@dataclass(frozen=True)
class ParlayCandidate:
    legs: tuple[ValueCandidate, ValueCandidate]
    pair_id: str
    combined_probability: float
    combined_odds: float
    expected_value: float
    expected_log_growth: float

    @property
    def match_ids(self) -> tuple[str, str]:
        return tuple(leg.match_id for leg in self.legs)


@dataclass(frozen=True)
class AllocatedSingle:
    candidate: ValueCandidate
    stake: int
    rank: int
    kelly_stake: int
    full_kelly: float
    kelly_fraction: float
    applied_limits: tuple[str, ...]


@dataclass(frozen=True)
class AllocatedParlay:
    parlay: ParlayCandidate
    stake: int
    rank: int
    kelly_stake: int
    full_kelly: float
    kelly_fraction: float
    applied_limits: tuple[str, ...]


@dataclass(frozen=True)
class RiskCheck:
    name: str
    value: float
    limit: float
    passed: bool


@dataclass(frozen=True)
class Portfolio:
    singles: tuple[AllocatedSingle, ...]
    parlay: AllocatedParlay | None
    rejections: tuple[str, ...] = ()
    limit_checks: tuple[RiskCheck, ...] = ()

    @property
    def total_stake(self) -> int:
        return sum(item.stake for item in self.singles) + (self.parlay.stake if self.parlay else 0)

    @property
    def parlays(self) -> tuple[AllocatedParlay, ...]:
        return (self.parlay,) if self.parlay else ()

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.rejections


def full_kelly(probability: float, odds: float) -> float:
    """Return the positive full-Kelly fraction for decimal odds, or zero."""
    probability = _finite_number(probability)
    odds = _finite_number(odds)
    if probability is None or odds is None or not 0.0 < probability < 1.0 or odds <= 1.0:
        return 0.0
    b = odds - 1.0
    return max(0.0, (b * probability - (1.0 - probability)) / b)


def stake_for(candidate: ValueCandidate, bankroll: float, kelly_fraction: float) -> int:
    """Size one candidate with Kelly only; portfolio limits are applied elsewhere."""
    bankroll = _finite_number(bankroll)
    kelly_fraction = _finite_number(kelly_fraction)
    multipliers = _candidate_multipliers(candidate)
    if (
        bankroll is None
        or kelly_fraction is None
        or bankroll <= 0
        or not 0 < kelly_fraction <= 1
        or multipliers is None
    ):
        return 0
    probability = _finite_number(candidate.conservative_probability)
    odds = _finite_number(candidate.official_odds)
    if probability is None or odds is None:
        return 0
    raw = bankroll * full_kelly(probability, odds) * kelly_fraction
    for multiplier in multipliers:
        raw *= multiplier
    return _round_down(raw, 2)


def build_two_leg_candidates(candidates: list[ValueCandidate], config: dict) -> list[ParlayCandidate]:
    """Build deterministic, legal two-leg parlay candidates from independent legs."""
    return _build_two_leg_candidates_with_reasons(candidates, config)[0]


def _build_two_leg_candidates_with_reasons(
    candidates: list[ValueCandidate], config: dict
) -> tuple[list[ParlayCandidate], tuple[str, ...]]:
    strategy = _strategy(config)
    gates = _combo_gates(strategy)
    if gates is None:
        return [], ("parlay_invalid_config",)
    unique_candidates, duplicate_reasons = _unique_candidates_with_reasons(candidates)
    rejections = list(duplicate_reasons)
    legs = []
    for candidate in unique_candidates:
        reason = _parlay_leg_reason(candidate, gates)
        if reason is not None:
            rejections.append(f"{candidate.candidate_id}:parlay_{reason}")
            continue
        legs.append(candidate)
    parlays = []
    for index, left in enumerate(legs):
        for right in legs[index + 1 :]:
            ordered = tuple(sorted((left, right), key=lambda candidate: candidate.candidate_id))
            pair_id = "|".join(candidate.candidate_id for candidate in ordered)
            if left.match_id == right.match_id:
                rejections.append(f"{pair_id}:parlay_same_match")
                continue
            if _correlated(left, right):
                rejections.append(f"{pair_id}:parlay_correlated")
                continue
            probability = left.conservative_probability * right.conservative_probability
            odds = left.official_odds * right.official_odds
            expected_value = probability * odds - 1.0
            if not _finite_positive(probability) or not _finite_number(odds) or odds <= 1.0 or expected_value <= 0:
                rejections.append(f"{pair_id}:parlay_invalid_combined_value")
                continue
            if expected_value < gates["combined_ev"]:
                rejections.append(f"{pair_id}:parlay_combined_ev")
                continue
            multiplier = _parlay_multiplier((left, right))
            if multiplier is None:
                rejections.append(f"{pair_id}:parlay_invalid_multiplier")
                continue
            growth = _expected_log_growth(probability, odds, QUARTER_KELLY * full_kelly(probability, odds) * multiplier)
            if growth is None or growth <= 0:
                rejections.append(f"{pair_id}:parlay_nonpositive_growth")
                continue
            parlays.append(
                ParlayCandidate(
                    legs=ordered,
                    pair_id=pair_id,
                    combined_probability=probability,
                    combined_odds=odds,
                    expected_value=expected_value,
                    expected_log_growth=growth,
                )
            )
    return (
        sorted(
            parlays,
            key=lambda parlay: (
                -parlay.expected_log_growth,
                -parlay.expected_value,
                parlay.pair_id,
            ),
        ),
        tuple(sorted(set(rejections))),
    )


def allocate_portfolio(
    candidates: list[ValueCandidate], limits: PortfolioLimits, account: dict
) -> Portfolio:
    """Allocate a bounded paid portfolio without mutating candidates or doing I/O."""
    limit_values = _validated_limits(limits)
    if limit_values is None:
        return Portfolio((), None, ("invalid_limits",))
    monthly_stake, realized_profit = _account_values(account)
    if monthly_stake is None or realized_profit is None:
        return Portfolio((), None, ("invalid_account",))
    if realized_profit <= -limit_values.monthly_stop_loss:
        return Portfolio(
            (),
            None,
            ("monthly_stop_loss",),
            _risk_checks((), None, limit_values, monthly_stake, realized_profit),
        )

    rejections: list[str] = []
    available_monthly = max(0.0, limit_values.monthly_budget_cap - monthly_stake)
    if available_monthly < limit_values.stake_unit:
        rejections.append("monthly_budget_cap")

    unique_candidates, duplicate_reasons = _unique_candidates_with_reasons(candidates)
    rejections.extend(duplicate_reasons)
    ranked: list[tuple[ValueCandidate, float]] = []
    for candidate in unique_candidates:
        reason, growth = _single_reason_and_growth(candidate, limit_values)
        if reason is not None:
            rejections.append(f"{candidate.candidate_id}:{reason}")
            continue
        ranked.append((candidate, growth))
    ranked.sort(key=lambda item: _single_sort_key(item[0], item[1]))

    singles: list[AllocatedSingle] = []
    selected_matches: set[str] = set()
    match_exposure: dict[str, int] = {}
    single_stake = 0
    daily_stake = 0
    for rank, (candidate, _) in enumerate(ranked, start=1):
        if candidate.match_id in selected_matches:
            rejections.append(f"{candidate.candidate_id}:single_match_already_selected")
            continue
        if len(singles) >= limit_values.max_single_count:
            rejections.append(f"{candidate.candidate_id}:max_single_count")
            continue
        raw_stake = stake_for(candidate, limit_values.bankroll, limit_values.kelly_fraction)
        stake, applied_limits = _capped_stake(
            raw_stake,
            limit_values.stake_unit,
            (
                ("max_single_stake", limit_values.max_single_stake),
                (
                    "max_match_exposure",
                    limit_values.max_match_exposure - match_exposure.get(candidate.match_id, 0),
                ),
                ("single_budget_cap", limit_values.single_budget_cap - single_stake),
                ("max_daily_stake", limit_values.max_daily_stake - daily_stake),
                ("monthly_budget_cap", available_monthly - daily_stake),
            ),
        )
        if stake < limit_values.min_single_stake:
            rejections.append(f"{candidate.candidate_id}:min_single_stake")
            continue
        singles.append(
            AllocatedSingle(
                candidate=candidate,
                stake=stake,
                rank=rank,
                kelly_stake=raw_stake,
                full_kelly=full_kelly(
                    candidate.conservative_probability, candidate.official_odds
                ),
                kelly_fraction=limit_values.kelly_fraction,
                applied_limits=applied_limits,
            )
        )
        selected_matches.add(candidate.match_id)
        match_exposure[candidate.match_id] = match_exposure.get(candidate.match_id, 0) + stake
        single_stake += stake
        daily_stake += stake

    parlay = None
    parlay_config = _combo_config_from_limits(limit_values)
    parlay_candidates, parlay_rejections = _build_two_leg_candidates_with_reasons(
        unique_candidates, parlay_config
    )
    rejections.extend(parlay_rejections)
    for candidate in parlay_candidates:
        if selected_matches.intersection(candidate.match_ids):
            rejections.append(f"{candidate.pair_id}:parlay_reuses_single_match")
            continue
        if parlay is not None:
            rejections.append(f"{candidate.pair_id}:max_parlay_count")
            continue
        raw_stake = _parlay_stake(candidate, limit_values)
        stake, applied_limits = _capped_stake(
            raw_stake,
            limit_values.stake_unit,
            (
                ("max_parlay_stake", limit_values.max_parlay_stake),
                *(
                    (
                        "max_match_exposure",
                        limit_values.max_match_exposure - match_exposure.get(match_id, 0),
                    )
                    for match_id in candidate.match_ids
                ),
                ("max_daily_stake", limit_values.max_daily_stake - daily_stake),
                ("monthly_budget_cap", available_monthly - daily_stake),
            ),
        )
        if stake < limit_values.min_parlay_stake:
            rejections.append(f"{candidate.pair_id}:min_parlay_stake")
            continue
        parlay = AllocatedParlay(
            parlay=candidate,
            stake=stake,
            rank=1,
            kelly_stake=raw_stake,
            full_kelly=full_kelly(candidate.combined_probability, candidate.combined_odds),
            kelly_fraction=limit_values.kelly_fraction,
            applied_limits=applied_limits,
        )
        for match_id in candidate.match_ids:
            match_exposure[match_id] = match_exposure.get(match_id, 0) + stake
        daily_stake += stake

    return Portfolio(
        tuple(singles),
        parlay,
        tuple(sorted(set(rejections))),
        _risk_checks(tuple(singles), parlay, limit_values, monthly_stake, realized_profit),
    )


def _single_reason_and_growth(candidate: ValueCandidate, limits: PortfolioLimits) -> tuple[str | None, float | None]:
    if candidate.paid_eligible is not True:
        return "not_paid_eligible", None
    if candidate.single_eligible is not True:
        return "not_single_eligible", None
    reason = _common_candidate_reason(candidate)
    if reason is not None:
        return reason, None
    if candidate.expected_value <= 0:
        return "nonpositive_expected_value", None
    growth = _candidate_growth(candidate, limits.kelly_fraction)
    if growth is None or growth <= 0 or stake_for(candidate, limits.bankroll, limits.kelly_fraction) <= 0:
        return "nonpositive_kelly", None
    return None, growth


def _parlay_leg_reason(candidate: ValueCandidate, gates: dict[str, float]) -> str | None:
    reason = _common_candidate_reason(candidate)
    if reason is not None:
        return reason
    if candidate.conservative_probability < gates["leg_probability"]:
        return "leg_probability"
    if candidate.probability_edge < gates["leg_edge"]:
        return "leg_edge"
    if candidate.expected_value <= 0 or candidate.expected_value < gates["leg_ev"]:
        return "leg_ev"
    return None


def _common_candidate_reason(candidate: ValueCandidate) -> str | None:
    if not _nonempty_text(candidate.match_id):
        return "invalid_match_id"
    if not isinstance(candidate.market_type, str) or candidate.market_type not in SUPPORTED_MARKETS:
        return "unsupported_market"
    if not isinstance(candidate.play, str) or candidate.play != PLAY_BY_MARKET[candidate.market_type]:
        return "unsupported_play"
    if not _valid_market_identity(candidate):
        return "invalid_market_identity"
    if not isinstance(candidate.data_quality, str) or candidate.data_quality not in QUALITY_RANK:
        return "data_quality"
    if not isinstance(candidate.volatility_band, str) or candidate.volatility_band not in VOLATILITY_RANK:
        return "volatility_band"
    if not isinstance(candidate.odds_source, str) or candidate.odds_source not in TRUSTED_SOURCES or not _nonempty_text(candidate.source_record_id) or not _nonempty_text(candidate.captured_at_bjt):
        return "unlocked_domestic_odds"
    probability = _finite_number(candidate.conservative_probability)
    market_probability = _finite_number(candidate.official_market_probability)
    probability_edge = _finite_number(candidate.probability_edge)
    probability_layers = tuple(
        _finite_number(value)
        for value in (
            candidate.raw_model_probability,
            candidate.calibrated_model_probability,
        )
    )
    odds = _finite_number(candidate.official_odds)
    expected_value = _finite_number(candidate.expected_value)
    multipliers = _candidate_multipliers(candidate)
    if (
        probability is None
        or not 0 < probability < 1
        or market_probability is None
        or not 0 < market_probability < 1
        or probability_edge is None
        or any(value is None or not 0 <= value <= 1 for value in probability_layers)
        or odds is None
        or odds <= 1
        or expected_value is None
        or multipliers is None
        or _nonnegative_integer(candidate.calibration_samples) is None
        or not _valid_correlation_tags(candidate.correlation_tags)
        or not isinstance(candidate.paid_eligible, bool)
        or not isinstance(candidate.single_eligible, bool)
        or not isinstance(candidate.value_gate_reasons, tuple)
        or not all(_nonempty_text(reason) for reason in candidate.value_gate_reasons)
    ):
        return "invalid_candidate_values"
    if not math.isclose(
        probability_edge,
        probability - market_probability,
        rel_tol=0.0,
        abs_tol=1e-12,
    ) or not math.isclose(
        expected_value,
        probability * odds - 1.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        return "inconsistent_candidate_values"
    return None


def _valid_market_identity(candidate: ValueCandidate) -> bool:
    if not isinstance(candidate.selection, str):
        return False
    if candidate.market_type == "had":
        return candidate.line is None and candidate.selection in THREE_WAY_SELECTION_LABELS
    if candidate.market_type == "hhad":
        return (
            isinstance(candidate.line, int)
            and not isinstance(candidate.line, bool)
            and candidate.selection in THREE_WAY_SELECTION_LABELS
        )
    if candidate.market_type == "ttg":
        return candidate.line is None and candidate.selection in TOTAL_GOALS_SELECTION_LABELS
    return False


def _valid_correlation_tags(value: object) -> bool:
    return isinstance(value, tuple) and all(_nonempty_text(tag) for tag in value)


def _candidate_growth(candidate: ValueCandidate, kelly_fraction: float) -> float | None:
    multipliers = _candidate_multipliers(candidate)
    if multipliers is None:
        return None
    fraction = kelly_fraction * full_kelly(candidate.conservative_probability, candidate.official_odds)
    for multiplier in multipliers:
        fraction *= multiplier
    return _expected_log_growth(candidate.conservative_probability, candidate.official_odds, fraction)


def _expected_log_growth(probability: float, odds: float, fraction: float) -> float | None:
    probability = _finite_number(probability)
    odds = _finite_number(odds)
    fraction = _finite_number(fraction)
    if probability is None or odds is None or fraction is None or not 0 < probability < 1 or odds <= 1 or not 0 < fraction < 1:
        return None
    win = 1.0 + fraction * (odds - 1.0)
    loss = 1.0 - fraction
    if win <= 0 or loss <= 0:
        return None
    growth = probability * math.log(win) + (1.0 - probability) * math.log(loss)
    return growth if math.isfinite(growth) else None


def _single_sort_key(candidate: ValueCandidate, growth: float) -> tuple:
    return (
        -growth,
        -candidate.expected_value,
        -_nonnegative_integer(candidate.calibration_samples),
        QUALITY_RANK[candidate.data_quality],
        VOLATILITY_RANK.get(candidate.volatility_band, 2),
        candidate.candidate_id,
    )


def _parlay_stake(parlay: ParlayCandidate, limits: PortfolioLimits) -> int:
    multiplier = _parlay_multiplier(parlay.legs)
    if multiplier is None:
        return 0
    raw = limits.bankroll * full_kelly(parlay.combined_probability, parlay.combined_odds) * limits.kelly_fraction * multiplier
    return _round_down(raw, limits.stake_unit)


def _correlated(left: ValueCandidate, right: ValueCandidate) -> bool:
    left_tags = {tag for tag in left.correlation_tags if isinstance(tag, str) and not tag.startswith("league:")}
    right_tags = {tag for tag in right.correlation_tags if isinstance(tag, str) and not tag.startswith("league:")}
    return bool(left_tags.intersection(right_tags))


def _combo_gates(strategy: dict) -> dict[str, float] | None:
    settled = _nonnegative_integer(strategy.get("settled_samples"))
    strict_until = _nonnegative_integer(strategy.get("strict_until_samples"))
    if settled is None or strict_until is None:
        return None
    strict = settled < strict_until
    prefix = "strict_" if strict else ""
    values = {
        "leg_probability": strategy.get("min_combo_leg_probability", 0.45),
        "leg_edge": strategy.get(
            f"{prefix}min_combo_leg_edge", 0.03 if strict else 0.02
        ),
        "leg_ev": strategy.get(f"{prefix}min_combo_leg_ev"),
        "combined_ev": strategy.get(f"{prefix}min_combo_ev"),
    }
    parsed = {name: _finite_number(value) for name, value in values.items()}
    if any(value is None for value in parsed.values()):
        return None
    if parsed["leg_probability"] < 0 or parsed["leg_probability"] >= 1 or parsed["leg_edge"] < 0:
        return None
    if parsed["leg_ev"] < 0 or parsed["combined_ev"] < 0:
        return None
    return parsed


def _strategy(config: dict) -> dict:
    value = config.get("value_strategy") if isinstance(config, dict) else None
    return value if isinstance(value, dict) else {}


def _combo_config_from_limits(limits: PortfolioLimits) -> dict:
    return {
        "value_strategy": {
            "settled_samples": limits.settled_samples,
            "strict_until_samples": limits.strict_until_samples,
            "min_combo_leg_probability": limits.min_combo_leg_probability,
            "strict_min_combo_leg_edge": limits.min_combo_leg_edge,
            "min_combo_leg_edge": limits.min_combo_leg_edge,
            "strict_min_combo_leg_ev": limits.min_combo_leg_ev,
            "min_combo_leg_ev": limits.min_combo_leg_ev,
            "strict_min_combo_ev": limits.min_combo_ev,
            "min_combo_ev": limits.min_combo_ev,
        }
    }


def _validated_limits(limits: PortfolioLimits) -> PortfolioLimits | None:
    if not isinstance(limits, PortfolioLimits):
        return None
    if _finite_number(limits.bankroll) is None or limits.bankroll <= 0:
        return None
    if _finite_number(limits.kelly_fraction) != QUARTER_KELLY or limits.stake_unit != 2:
        return None
    values = (
        limits.max_match_exposure,
        limits.max_single_stake,
        limits.single_budget_cap,
        limits.max_single_count,
        limits.min_single_stake,
        limits.max_parlay_stake,
        limits.min_parlay_stake,
        limits.max_daily_stake,
        limits.monthly_budget_cap,
        limits.monthly_stop_loss,
        limits.settled_samples,
        limits.strict_until_samples,
    )
    if any(_nonnegative_integer(value) is None for value in values):
        return None
    if limits.min_single_stake < 2 or limits.min_parlay_stake < 2:
        return None
    combo_values = (
        _finite_number(limits.min_combo_leg_probability),
        _finite_number(limits.min_combo_leg_edge),
        _finite_number(limits.min_combo_leg_ev),
        _finite_number(limits.min_combo_ev),
    )
    if (
        any(value is None for value in combo_values)
        or not 0 < combo_values[0] < 1
        or any(value < 0 for value in combo_values[1:])
    ):
        return None
    return replace(
        limits,
        max_match_exposure=min(limits.max_match_exposure, HARD_MAX_MATCH_EXPOSURE),
        max_single_stake=min(limits.max_single_stake, HARD_MAX_SINGLE_STAKE),
        single_budget_cap=min(limits.single_budget_cap, HARD_SINGLE_BUDGET_CAP),
        max_single_count=min(limits.max_single_count, HARD_MAX_SINGLE_COUNT),
        max_parlay_stake=min(limits.max_parlay_stake, HARD_MAX_PARLAY_STAKE),
        max_daily_stake=min(limits.max_daily_stake, HARD_MAX_DAILY_STAKE),
        monthly_budget_cap=min(limits.monthly_budget_cap, HARD_MONTHLY_BUDGET_CAP),
        monthly_stop_loss=min(limits.monthly_stop_loss, HARD_MONTHLY_STOP_LOSS),
    )


def _account_values(account: dict) -> tuple[float | None, float | None]:
    if not isinstance(account, dict) or "monthly_stake" not in account:
        return None, None
    stake = _finite_number(account.get("monthly_stake"))
    profit_value = account.get("monthly_realized_profit", account.get("monthly_profit"))
    profit = _finite_number(profit_value)
    if stake is None or profit is None or stake < 0:
        return None, None
    return stake, profit


def _unique_candidates(candidates: Iterable[ValueCandidate]) -> list[ValueCandidate]:
    return _unique_candidates_with_reasons(candidates)[0]


def _unique_candidates_with_reasons(candidates: Iterable[ValueCandidate]) -> tuple[list[ValueCandidate], list[str]]:
    grouped: dict[str, list[ValueCandidate]] = {}
    for candidate in candidates if isinstance(candidates, list) else ():
        if isinstance(candidate, ValueCandidate) and _nonempty_text(candidate.candidate_id):
            grouped.setdefault(candidate.candidate_id, []).append(candidate)
    selected = []
    reasons = []
    for candidate_id in sorted(grouped):
        group = grouped[candidate_id]
        representative = group[0]
        if any(candidate != representative for candidate in group[1:]):
            reasons.append(f"{candidate_id}:conflicting_duplicate_candidate_id")
            continue
        selected.append(representative)
        if len(group) > 1:
            reasons.append(f"{candidate_id}:duplicate_candidate_id")
    return selected, reasons


def _candidate_multipliers(candidate: ValueCandidate) -> tuple[float, float, float] | None:
    values = tuple(_finite_number(getattr(candidate, name, None)) for name in (
        "data_quality_multiplier", "volatility_multiplier", "performance_multiplier",
    ))
    if (
        any(value is None or value <= 0 for value in values)
        or values[0] > 1.0
        or values[1] > 1.0
        or values[2] > MAX_PERFORMANCE_MULTIPLIER
    ):
        return None
    return values


def _parlay_multiplier(legs: tuple[ValueCandidate, ValueCandidate]) -> float | None:
    multipliers = tuple(_candidate_multipliers(leg) for leg in legs)
    if any(values is None for values in multipliers):
        return None
    return (
        min(values[0] for values in multipliers)
        * min(values[1] for values in multipliers)
        * min(values[2] for values in multipliers)
    )


def _capped_stake(
    raw_stake: int,
    unit: int,
    caps: tuple[tuple[str, float], ...],
) -> tuple[int, tuple[str, ...]]:
    parsed_caps = tuple(
        (name, value)
        for name, raw_value in caps
        if (value := _finite_number(raw_value)) is not None
    )
    if len(parsed_caps) != len(caps):
        return 0, ("invalid_cap",)
    minimum = min(float(raw_stake), *(value for _, value in parsed_caps))
    stake = _round_down(minimum, unit)
    applied = []
    applied.extend(
        name
        for name, value in parsed_caps
        if math.isclose(value, minimum, rel_tol=0.0, abs_tol=1e-12)
    )
    if stake < minimum:
        applied.append("stake_unit")
    return stake, tuple(sorted(set(applied)))


def _risk_checks(
    singles: tuple[AllocatedSingle, ...],
    parlay: AllocatedParlay | None,
    limits: PortfolioLimits,
    monthly_stake: float,
    realized_profit: float,
) -> tuple[RiskCheck, ...]:
    stakes = [item.stake for item in singles]
    if parlay is not None:
        stakes.append(parlay.stake)
    match_exposure: dict[str, int] = {}
    single_counts: dict[str, int] = {}
    for item in singles:
        match_id = item.candidate.match_id
        match_exposure[match_id] = match_exposure.get(match_id, 0) + item.stake
        single_counts[match_id] = single_counts.get(match_id, 0) + 1
    if parlay is not None:
        for match_id in parlay.parlay.match_ids:
            match_exposure[match_id] = match_exposure.get(match_id, 0) + parlay.stake
    single_total = sum(item.stake for item in singles)
    parlay_total = parlay.stake if parlay is not None else 0
    daily_total = single_total + parlay_total
    checks = (
        RiskCheck(
            "stake_unit",
            float(sum(1 for stake in stakes if stake % limits.stake_unit)),
            0.0,
            all(stake >= limits.stake_unit and stake % limits.stake_unit == 0 for stake in stakes),
        ),
        RiskCheck(
            "max_match_exposure",
            float(max(match_exposure.values(), default=0)),
            float(limits.max_match_exposure),
            all(value <= limits.max_match_exposure for value in match_exposure.values()),
        ),
        RiskCheck(
            "max_single_stake",
            float(max((item.stake for item in singles), default=0)),
            float(limits.max_single_stake),
            all(item.stake <= limits.max_single_stake for item in singles),
        ),
        RiskCheck(
            "min_single_stake",
            float(min((item.stake for item in singles), default=0)),
            float(limits.min_single_stake),
            not singles or all(item.stake >= limits.min_single_stake for item in singles),
        ),
        RiskCheck(
            "max_one_single_per_match",
            float(max(single_counts.values(), default=0)),
            1.0,
            all(value <= 1 for value in single_counts.values()),
        ),
        RiskCheck(
            "max_single_count",
            float(len(singles)),
            float(limits.max_single_count),
            len(singles) <= limits.max_single_count,
        ),
        RiskCheck(
            "single_budget_cap",
            float(single_total),
            float(limits.single_budget_cap),
            single_total <= limits.single_budget_cap,
        ),
        RiskCheck(
            "max_parlay_stake",
            float(parlay_total),
            float(limits.max_parlay_stake),
            parlay_total <= limits.max_parlay_stake,
        ),
        RiskCheck(
            "min_parlay_stake",
            float(parlay_total),
            float(limits.min_parlay_stake),
            parlay is None or parlay_total >= limits.min_parlay_stake,
        ),
        RiskCheck(
            "max_daily_stake",
            float(daily_total),
            float(limits.max_daily_stake),
            daily_total <= limits.max_daily_stake,
        ),
        RiskCheck(
            "monthly_budget_cap",
            float(monthly_stake + daily_total),
            float(limits.monthly_budget_cap),
            monthly_stake + daily_total <= limits.monthly_budget_cap,
        ),
        RiskCheck(
            "monthly_stop_loss",
            float(max(0.0, -realized_profit)),
            float(limits.monthly_stop_loss),
            realized_profit > -limits.monthly_stop_loss,
        ),
        RiskCheck(
            "parlay_leg_count",
            float(len(parlay.parlay.legs) if parlay is not None else 0),
            2.0,
            parlay is None or len(parlay.parlay.legs) == 2,
        ),
        RiskCheck(
            "parlay_distinct_matches",
            float(len(set(parlay.parlay.match_ids)) if parlay is not None else 0),
            2.0,
            parlay is None or len(set(parlay.parlay.match_ids)) == 2,
        ),
        RiskCheck(
            "parlay_reuses_single_match",
            float(
                len(set(parlay.parlay.match_ids).intersection(single_counts))
                if parlay is not None
                else 0
            ),
            0.0,
            parlay is None or not set(parlay.parlay.match_ids).intersection(single_counts),
        ),
    )
    return checks


def _round_down(value: float, unit: int) -> int:
    value = _finite_number(value)
    if value is None or value <= 0 or not isinstance(unit, int) or unit <= 0:
        return 0
    return int(math.floor(value / unit)) * unit


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _finite_positive(value: object) -> bool:
    number = _finite_number(value)
    return number is not None and number > 0


def _nonnegative_integer(value: object) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
