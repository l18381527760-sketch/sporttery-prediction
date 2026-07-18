import csv
import io
import itertools
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from activation_readiness import assert_activation_ready
from decision_bundle import read_valid_decision_bundle
from betting_ledger import (
    PENDING,
    TERMINAL_STATUSES,
    settle_ledger,
    settled_market_identities,
    stable_bet_id,
    update_observation_ledger,
    write_ledger_atomic,
)
from model_metrics import play_family, summarize, write_metrics
from official_markets import normalize_market, parse_handicap
from plan_lock import read_valid_lock
from strategy_controls import (
    apply_league_draw_calibration,
    build_daily_decision,
    combo_leg_limit,
    fit_league_draw_calibrations,
    simulation_account_state,
)
from value_candidates import ValueCandidate, build_candidates
from value_portfolio import Portfolio, PortfolioLimits, _validated_limits, allocate_portfolio


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def as_float(row: dict, key: str, default: float = 0.0) -> float:
    value = (row.get(key) or "").strip()
    return float(value) if value else default


def money(value: float) -> int:
    return int(round(value / 10.0) * 10)


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def load_predictions(target_date: date) -> list[dict]:
    path = OUTPUT_DIR / f"predictions_{target_date.isoformat()}.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_odds(target_date: date) -> dict:
    path = DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_draw_training_samples() -> list[dict]:
    return load_csv(DATA_DIR / "draw_training_samples.csv")


def poisson_pmf(lam: float, max_goals: int = 7) -> list[float]:
    values = [math.exp(-lam) * (lam**k) / math.factorial(k) for k in range(max_goals + 1)]
    total = sum(values)
    return [value / total for value in values]


def total_goal_distribution(row: dict) -> dict[str, float]:
    lam = as_float(row, "xg_a") + as_float(row, "xg_b")
    values = poisson_pmf(lam, 7)
    return {str(index): values[index] for index in range(7)} | {"7": values[7]}


def top_half_full(row: dict) -> tuple[str, float]:
    xg_a = as_float(row, "xg_a")
    xg_b = as_float(row, "xg_b")
    first_a = xg_a * 0.45
    first_b = xg_b * 0.45
    second_a = xg_a * 0.55
    second_b = xg_b * 0.55

    first_dist_a = poisson_pmf(first_a, 5)
    first_dist_b = poisson_pmf(first_b, 5)
    second_dist_a = poisson_pmf(second_a, 6)
    second_dist_b = poisson_pmf(second_b, 6)
    combos: dict[str, float] = {}

    for ha, pha in enumerate(first_dist_a):
        for hb, phb in enumerate(first_dist_b):
            half = outcome(ha, hb)
            half_prob = pha * phb
            for sa, psa in enumerate(second_dist_a):
                for sb, psb in enumerate(second_dist_b):
                    full = outcome(ha + sa, hb + sb)
                    key = half + full
                    combos[key] = combos.get(key, 0.0) + half_prob * psa * psb

    return max(combos.items(), key=lambda item: item[1])


def outcome(a_goals: int, b_goals: int) -> str:
    if a_goals > b_goals:
        return "胜"
    if a_goals == b_goals:
        return "平"
    return "负"


def wdw_pick(row: dict) -> tuple[str, float]:
    options = [
        ("胜", as_float(row, "p_a")),
        ("平", as_float(row, "p_draw")),
        ("负", as_float(row, "p_b")),
    ]
    return max(options, key=lambda item: item[1])


def score_pick(row: dict) -> tuple[str, float]:
    return (row.get("score_1") or "", as_float(row, "score_1_prob"))


def official_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def score_key(score: str) -> str:
    home, away = score.split("-")
    return f"s{int(home):02d}s{int(away):02d}"


def hafu_key(selection: str) -> str:
    mapping = {"胜": "h", "平": "d", "负": "a"}
    return mapping[selection[0]] + mapping[selection[1]]


def make_item(
    row: dict,
    play: str,
    selection: str,
    probability: float,
    odds: float,
    stake: int,
    reason: str,
    legs=None,
    market_probability: float | None = None,
    value_edge: float | None = None,
    raw_model_probability: float | None = None,
    league_calibrated_probability: float | None = None,
    league_calibration_samples: int = 0,
    strategy_version: str = "",
) -> dict:
    return {
        "date": row["date"],
        "match_id": row.get("match_id", ""),
        "kickoff_local": row.get("kickoff_at", ""),
        "strategy_version": strategy_version,
        "stage": row.get("stage", ""),
        "match": f"{row['team_a']} vs {row['team_b']}",
        "team_a": row["team_a"],
        "team_b": row["team_b"],
        "play": play,
        "selection": selection,
        "probability": probability,
        "raw_model_probability": raw_model_probability if raw_model_probability is not None else probability,
        "league_calibrated_probability": league_calibrated_probability if league_calibrated_probability is not None else (raw_model_probability if raw_model_probability is not None else probability),
        "league_calibration_samples": league_calibration_samples,
        "odds": odds,
        "market_probability": market_probability if market_probability is not None else "",
        "value_edge": value_edge if value_edge is not None else "",
        "expected_value": probability * odds,
        "stake": stake,
        "expected_return": round(stake * probability * odds, 2),
        "expected_profit": round(stake * probability * odds - stake, 2),
        "reason": reason,
        "legs_json": json.dumps(legs or [], ensure_ascii=False),
    }


def confidence_band(probability: float) -> str:
    if probability >= 0.54:
        return "high"
    if probability >= 0.42:
        return "medium"
    return "low"


def allocate(items: list[dict], target_total: int, config: dict) -> list[dict]:
    if not items or target_total <= 0:
        return items
    min_stake = int(config["min_stake"])
    weighted = []
    for item in items:
        band = confidence_band(item["probability"])
        multiplier = config["confidence_multiplier"][band]
        weighted.append((item, max(0.01, item["probability"] * multiplier)))

    weight_sum = sum(weight for _, weight in weighted)
    for item, weight in weighted:
        item["stake"] = money(target_total * weight / weight_sum)
        if item["stake"] < min_stake:
            item["stake"] = min_stake

    total = sum(item["stake"] for item, _ in weighted)
    while total > target_total:
        candidates = [item for item, _ in weighted if item["stake"] > min_stake]
        if not candidates:
            break
        weakest = min(candidates, key=lambda item: item["probability"])
        weakest["stake"] -= 10
        total -= 10

    while total + 10 <= target_total:
        strongest = max((item for item, _ in weighted), key=lambda item: item["probability"])
        strongest["stake"] += 10
        total += 10

    return [item for item, _ in weighted if item["stake"] > 0]


def top_for_budget(items: list[dict], target_total: int, config: dict) -> list[dict]:
    min_stake = int(config["min_stake"])
    if min_stake <= 0:
        return items
    max_items = max(1, target_total // min_stake)
    return sorted(items, key=lambda item: item["probability"], reverse=True)[:max_items]


def build_plan(target_date: date) -> list[dict]:
    config = read_json(ROOT / "betting_config.json")
    predictions = load_predictions(target_date)
    odds_by_match = load_odds(target_date)
    status_path = DATA_DIR / "source_status.json"
    odds_source = "竞彩网"
    if status_path.exists():
        odds_source = str(read_json(status_path).get("source") or odds_source)
    strategy = config["draw_strategy"]
    value_strategy = config.get("value_strategy", {})
    ledger_path = OUTPUT_DIR / "betting_ledger.csv"
    historical_rows = []
    if ledger_path.exists():
        with ledger_path.open("r", encoding="utf-8-sig", newline="") as handle:
            historical_rows = list(csv.DictReader(handle))
    performance = summarize(historical_rows).get("by_play", {})

    def performance_multiplier(family: str) -> float:
        metrics = performance.get(family, {})
        count = int(metrics.get("count") or 0)
        roi = metrics.get("roi")
        if count < int(value_strategy.get("min_history_samples", 5)) or roi is None:
            return 1.0
        influence = min(0.35, count / 60.0)
        return max(0.80, min(1.15, 1 + float(roi) * influence))

    def fair_probabilities(had: dict) -> dict[str, float]:
        values = {"胜": official_float(had.get("h")), "平": official_float(had.get("d")), "负": official_float(had.get("a"))}
        if not all(values.values()):
            return {}
        inverse = {key: 1 / value for key, value in values.items()}
        total = sum(inverse.values())
        return {key: value / total for key, value in inverse.items()}
    plan: list[dict] = []
    draw_candidates = []
    wdw_legs = []

    for row in predictions:
        match_id = row.get("match_id", "")
        official = odds_by_match.get(match_id, {})
        had = official.get("had", {})
        fair = fair_probabilities(had)
        draw_probability = as_float(row, "p_draw")
        draw_odds = official_float(had.get("d"))
        draw_market = fair.get("平")
        if draw_odds and draw_market is not None:
            draw_edge = draw_probability - draw_market
            expected_value = draw_probability * draw_odds
            if (
                draw_probability >= float(value_strategy.get("min_draw_probability", 0.25))
                and draw_edge >= float(value_strategy.get("min_draw_edge", 0.0))
                and expected_value >= float(value_strategy.get("min_draw_expected_return", 0.80))
            ):
                quality = draw_probability * expected_value * performance_multiplier("平局单场")
                draw_candidates.append((quality, row, "平", draw_probability, draw_odds, draw_market, draw_edge))

        match_wdw = []
        for selection, probability, odds_value in [
            ("胜", as_float(row, "p_a"), had.get("h")),
            ("平", as_float(row, "p_draw"), had.get("d")),
            ("负", as_float(row, "p_b"), had.get("a")),
        ]:
            odds = official_float(odds_value)
            market_probability = fair.get(selection)
            if odds and market_probability is not None:
                edge = probability - market_probability
                if probability >= float(value_strategy.get("min_combo_leg_probability", 0.48)) and edge >= float(value_strategy.get("min_combo_leg_edge", 0.0)):
                    quality = (probability * odds) * performance_multiplier("胜平负串关")
                    match_wdw.append((quality, row, selection, probability, odds, market_probability, edge))
        if match_wdw:
            # One selection per match, ranked by calibrated model probability.
            wdw_legs.append(max(match_wdw, key=lambda item: item[0]))

    ranked_draws = sorted(draw_candidates, key=lambda item: item[0], reverse=True)
    draw_count = 1 if ranked_draws else 0
    if len(ranked_draws) >= 2:
        top_probability = ranked_draws[0][3]
        second_probability = ranked_draws[1][3]
        if (
            second_probability >= float(strategy["second_draw_min_probability"])
            and top_probability - second_probability <= float(strategy["max_probability_gap"])
        ):
            draw_count = 2
    draw_stake = int(strategy["single_stake"] if draw_count == 1 else strategy["double_stake_each"])
    draw_stake = money(draw_stake * performance_multiplier("平局单场"))
    for _, row, selection, probability, odds, market_probability, value_edge in ranked_draws[:draw_count]:
        analysis_source = row.get("analysis_source") or "专业欧赔市场"
        plan.append(make_item(row, "平局单场", selection, probability, odds, draw_stake, f"模型概率{pct(probability)}，市场公平概率{pct(market_probability)}，概率优势{pct(value_edge)}；分析参考{analysis_source}，方案采用{odds_source}赔率{odds}", market_probability=market_probability, value_edge=value_edge))

    def combo_candidate(candidates: list[tuple], market: str) -> dict | None:
        min_legs = int(strategy["combo_min_legs"])
        max_legs = int(strategy["combo_max_legs"])
        if len(candidates) < min_legs:
            return None
        ranked = sorted(candidates, key=lambda item: item[0], reverse=True)
        best = None
        for combo_size in range(min_legs, min(max_legs, len(ranked)) + 1):
            selected_legs = ranked[:combo_size]
            probability = 1.0
            odds = 1.0
            labels = []
            legs = []
            market_probability = 1.0
            for _, row, selection, leg_probability, leg_odds, leg_market_probability, leg_edge in selected_legs:
                probability *= leg_probability
                odds *= leg_odds
                market_probability *= leg_market_probability
                labels.append(f"{row['team_a']}vs{row['team_b']} {selection}")
                legs.append({"date": row["date"], "team_a": row["team_a"], "team_b": row["team_b"], "kind": market, "selection": selection, "probability": leg_probability, "market_probability": leg_market_probability, "value_edge": leg_edge, "odds": leg_odds})
            candidate = {"market": market, "size": combo_size, "probability": probability, "market_probability": market_probability, "value_edge": probability - market_probability, "odds": round(odds, 2), "labels": labels, "legs": legs, "row": selected_legs[0][1], "value": probability * odds * performance_multiplier("胜平负串关")}
            if best is None or candidate["value"] > best["value"]:
                best = candidate
        return best

    combo = combo_candidate(wdw_legs, "胜平负")
    if combo and combo["probability"] * combo["odds"] >= float(value_strategy.get("min_combo_expected_return", 0.78)):
        selection = " × ".join(combo["labels"])
        play = f"胜平负{combo['size']}串1"
        analysis_source = combo["row"].get("analysis_source") or "专业欧赔市场"
        combo_stake = money(int(strategy["combo_stake"]) * performance_multiplier("胜平负串关"))
        plan.append(make_item(combo["row"], play, selection, combo["probability"], combo["odds"], combo_stake, f"组合概率{pct(combo['probability'])}，市场公平概率{pct(combo['market_probability'])}，概率优势{pct(combo['value_edge'])}；分析参考{analysis_source}，方案赔率采用{odds_source}，组合赔率{combo['odds']}", combo["legs"], market_probability=combo["market_probability"], value_edge=combo["value_edge"]))

    total = sum(item["stake"] for item in plan)
    if total > int(config["max_daily_budget"]):
        raise RuntimeError("今日模拟预算超过上限，请检查配置。")
    return plan


def build_legacy_value_plan(
    target_date: date,
    *,
    predictions: list[dict] | None = None,
    odds_by_match: dict | None = None,
    odds_source: str | None = None,
    config: dict | None = None,
    paid_history: list[dict] | None = None,
    observation_history: list[dict] | None = None,
    training_samples: list[dict] | None = None,
    account_metrics: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    config = read_json(ROOT / "betting_config.json") if config is None else config
    strategy_version = str(config.get("legacy_strategy_version") or "legacy-v3")
    value = config.get("value_strategy", {})
    predictions = load_predictions(target_date) if predictions is None else predictions
    odds_by_match = load_odds(target_date) if odds_by_match is None else odds_by_match
    status_path = DATA_DIR / "source_status.json"
    odds_source = odds_source or "竞彩网"
    if odds_source == "竞彩网" and status_path.exists():
        odds_source = str(read_json(status_path).get("source") or odds_source)

    if paid_history is None:
        paid_history = load_csv(OUTPUT_DIR / "betting_ledger.csv")
    if observation_history is None:
        observation_history = load_csv(OUTPUT_DIR / "observation_ledger.csv")
    if account_metrics is None:
        metrics_path = OUTPUT_DIR / "model_metrics.json"
        if metrics_path.exists():
            try:
                account_metrics = read_json(metrics_path)
            except (OSError, json.JSONDecodeError):
                account_metrics = {}
        else:
            account_metrics = {}
    if training_samples is None:
        training_samples = load_draw_training_samples()
    account_state = simulation_account_state(
        paid_history,
        observation_history,
        target_date,
        config.get("simulation_account", {}),
        account_metrics,
    )
    calibration_config = config.get("league_calibration", {})
    league_calibrations = fit_league_draw_calibrations(
        training_samples,
        min_samples=int(calibration_config.get("min_samples", 30)),
        prior_samples=int(calibration_config.get("prior_samples", 60)),
        max_adjustment=float(calibration_config.get("max_adjustment", 0.05)),
        validation_fraction=float(calibration_config.get("validation_fraction", 0.25)),
    )
    bet_metrics = summarize(paid_history, strategy_version)
    observation_metrics = summarize(observation_history, strategy_version)
    by_play = bet_metrics.get("by_play", {})
    paused_leagues = {league for league, item in bet_metrics.get("by_league", {}).items() if item.get("paused")}
    overall = observation_metrics.get("active_strategy", {})

    settled_count = int(overall.get("count") or 0)
    strict_mode = settled_count < int(value.get("strict_until_samples", 100))
    prior = float(value.get("calibration_prior", 100))
    base_weight = float(value.get("strict_model_edge_weight_base", 0.15) if strict_mode else value.get("model_edge_weight_base", 0.35))
    max_weight = float(value.get("strict_model_edge_weight_max", 0.35) if strict_mode else value.get("model_edge_weight_max", 0.75))
    model_weight = base_weight + (max_weight - base_weight) * settled_count / (settled_count + prior)
    if overall.get("brier") is not None and float(overall["brier"]) > 0.25:
        model_weight *= max(0.70, 1 - (float(overall["brier"]) - 0.25))

    def performance_multiplier(family: str) -> float:
        item = by_play.get(family, {})
        count = int(item.get("count") or 0)
        roi = item.get("roi")
        if count < int(value.get("min_history_samples", 30)) or roi is None:
            return 1.0
        influence = min(0.35, count / 100.0)
        return max(0.80, min(1.15, 1 + float(roi) * influence))

    def fair_probabilities(had: dict) -> dict[str, float]:
        offered = {"胜": official_float(had.get("h")), "平": official_float(had.get("d")), "负": official_float(had.get("a"))}
        if not all(offered.values()):
            return {}
        inverse = {key: 1 / price for key, price in offered.items()}
        total = sum(inverse.values())
        return {key: probability / total for key, probability in inverse.items()}

    def conservative(model_probability: float, market_probability: float) -> float:
        return market_probability + model_weight * (model_probability - market_probability)

    def kelly_stake(probability: float, odds: float, family: str) -> int:
        full_kelly = max(0.0, (probability * odds - 1) / (odds - 1))
        kelly_fraction = float(value.get("strict_kelly_fraction", 0.125) if strict_mode else value.get("kelly_fraction", 0.25))
        fraction = full_kelly * kelly_fraction
        raw = float(value.get("reference_bankroll", 5000)) * fraction * performance_multiplier(family)
        minimum = int(value.get("min_single_stake", 20))
        maximum = int(value.get("strict_max_single_stake", 50) if strict_mode else value.get("max_single_stake", 200))
        return max(minimum, min(maximum, money(raw))) if raw >= minimum else 0

    singles = []
    combo_legs = []
    observation_pool = []
    for row in predictions:
        league = row.get("stage", "") or "未知"
        if league in paused_leagues:
            continue
        had = odds_by_match.get(row.get("match_id", ""), {}).get("had", {})
        fair = fair_probabilities(had)
        per_match_combo = []
        per_match_observations = []
        single_eligible = str(row.get("is_single_had", "")).lower() in {"true", "1", "yes"}
        for selection, raw_probability, price in [
            ("胜", as_float(row, "p_a"), official_float(had.get("h"))),
            ("平", as_float(row, "p_draw"), official_float(had.get("d"))),
            ("负", as_float(row, "p_b"), official_float(had.get("a"))),
        ]:
            market_probability = fair.get(selection)
            if price is None or market_probability is None:
                continue
            league_probability = raw_probability
            league_state = {"enabled": False, "sample_count": 0}
            if selection == "平":
                league_probability, league_state = apply_league_draw_calibration(
                    raw_probability, league, league_calibrations
                )
            probability = conservative(league_probability, market_probability)
            edge = probability - market_probability
            expected_value = probability * price
            family = "平局单场" if selection == "平" else "胜平负单场"
            candidate = {
                "row": row, "selection": selection, "raw_probability": raw_probability,
                "league_calibrated_probability": league_probability,
                "league_calibration_samples": int(league_state.get("sample_count") or 0),
                "league_calibration_enabled": league_state.get("enabled") is True,
                "probability": probability, "market_probability": market_probability,
                "value_edge": edge, "odds": price, "expected_value": expected_value,
                "family": family,
            }
            per_match_observations.append(candidate)
            edge_threshold = float(value.get("strict_min_probability_edge", 0.05) if strict_mode else value.get("min_probability_edge", 0.03))
            return_threshold = float(value.get("strict_min_expected_return", 1.06) if strict_mode else value.get("min_expected_return", 1.03))
            if single_eligible and edge >= edge_threshold and expected_value >= return_threshold:
                stake = kelly_stake(probability, price, family)
                if stake:
                    fraction = stake / float(value.get("reference_bankroll", 5000))
                    candidate["stake"] = stake
                    candidate["quality"] = probability * math.log(1 + fraction * (price - 1)) + (1 - probability) * math.log(1 - fraction)
                    singles.append(candidate)
            if (
                probability >= float(value.get("min_combo_leg_probability", 0.45))
                and edge >= float(value.get("strict_min_combo_leg_edge", 0.03) if strict_mode else value.get("min_combo_leg_edge", 0.02))
                and expected_value >= float(value.get("strict_min_combo_leg_expected_return", 1.02) if strict_mode else value.get("min_combo_leg_expected_return", 1.0))
            ):
                per_match_combo.append(candidate)
        if per_match_combo:
            combo_legs.append(max(per_match_combo, key=lambda item: item["expected_value"]))
        if per_match_observations:
            observation_pool.append(max(per_match_observations, key=lambda item: item["raw_probability"]))

    plan = []
    used_matches: set[tuple[str, str]] = set()
    account_budget = int(account_state.get("remaining_monthly_budget") or 0) // 10 * 10
    if account_state.get("paused"):
        account_budget = 0
    daily_budget = min(int(config["max_daily_budget"]), account_budget)
    single_budget = min(
        daily_budget,
        int(value.get("strict_single_budget_cap", 100) if strict_mode else value.get("single_budget_cap", 200)),
    )
    max_singles = int(value.get("max_single_count", 2))
    for item in sorted(singles, key=lambda candidate: candidate["quality"], reverse=True):
        match_key = (item["row"]["team_a"], item["row"]["team_b"])
        if match_key in used_matches or len(plan) >= max_singles or single_budget < int(value.get("min_single_stake", 20)):
            continue
        stake = min(item["stake"], single_budget)
        play = "平局单场" if item["selection"] == "平" else "胜平负单场"
        source = item["row"].get("analysis_source") or "专业欧赔市场"
        league_note = (
            f"，联赛校准{pct(item['league_calibrated_probability'])}（样本{item['league_calibration_samples']}）"
            if item["selection"] == "平" and item["league_calibration_enabled"]
            else ""
        )
        reason = f"保守概率{pct(item['probability'])}（原模型{pct(item['raw_probability'])}{league_note}），市场公平概率{pct(item['market_probability'])}，概率优势{pct(item['value_edge'])}，期望值{item['expected_value']:.3f}；参考{source}，采用{odds_source}赔率"
        plan.append(make_item(item["row"], play, item["selection"], item["probability"], item["odds"], stake, reason, market_probability=item["market_probability"], value_edge=item["value_edge"], raw_model_probability=item["raw_probability"], league_calibrated_probability=item["league_calibrated_probability"], league_calibration_samples=item["league_calibration_samples"], strategy_version=strategy_version))
        used_matches.add(match_key)
        single_budget -= stake

    available = [item for item in combo_legs if (item["row"]["team_a"], item["row"]["team_b"]) not in used_matches]
    best_by_size = {}
    # The retained comparison path is deliberately fixed at two legs; v4 owns
    # the public configuration validator and the paid portfolio constraints.
    maximum_combo_legs = 2
    for size in range(int(value.get("combo_min_legs", 2)), maximum_combo_legs + 1):
        for selected in itertools.combinations(available, size):
            probability = math.prod(item["probability"] for item in selected)
            raw_probability = math.prod(item["raw_probability"] for item in selected)
            market_probability = math.prod(item["market_probability"] for item in selected)
            odds = math.prod(item["odds"] for item in selected)
            expected_value = probability * odds
            candidate = {"selected": selected, "probability": probability, "raw_probability": raw_probability, "market_probability": market_probability, "value_edge": probability - market_probability, "odds": round(odds, 2), "expected_value": expected_value}
            combo_threshold = float(value.get("strict_min_combo_expected_return", 1.10) if strict_mode else value.get("min_combo_expected_return", 1.03))
            if expected_value >= combo_threshold and (size not in best_by_size or expected_value > best_by_size[size]["expected_value"]):
                best_by_size[size] = candidate

    best_combo = best_by_size.get(2)
    trial_combo = best_by_size.get(3)
    if trial_combo and (best_combo is None or trial_combo["expected_value"] >= best_combo["expected_value"] * float(value.get("three_leg_value_premium", 1.05))):
        best_combo = trial_combo

    remaining_budget = daily_budget - sum(item["stake"] for item in plan)
    combo_stake = int(value.get("strict_combo_stake", 20) if strict_mode else value.get("combo_stake", 30))
    if best_combo and remaining_budget >= combo_stake:
        selected = best_combo["selected"]
        labels = [f"{item['row']['team_a']}vs{item['row']['team_b']} {item['selection']}" for item in selected]
        legs = [{"date": item["row"]["date"], "match_id": item["row"].get("match_id", ""), "team_a": item["row"]["team_a"], "team_b": item["row"]["team_b"], "kind": "胜平负", "market_type": "had", "line": "", "selection": item["selection"], "probability": item["probability"], "raw_model_probability": item["raw_probability"], "league_calibrated_probability": item["league_calibrated_probability"], "market_probability": item["market_probability"], "value_edge": item["value_edge"], "odds": item["odds"]} for item in selected]
        source = selected[0]["row"].get("analysis_source") or "专业欧赔市场"
        reason = f"保守组合概率{pct(best_combo['probability'])}（原模型{pct(best_combo['raw_probability'])}），市场公平概率{pct(best_combo['market_probability'])}，概率优势{pct(best_combo['value_edge'])}，期望值{best_combo['expected_value']:.3f}；参考{source}，采用{odds_source}赔率"
        plan.append(make_item(selected[0]["row"], f"胜平负{len(selected)}串1", " × ".join(labels), best_combo["probability"], best_combo["odds"], combo_stake, reason, legs, market_probability=best_combo["market_probability"], value_edge=best_combo["value_edge"], raw_model_probability=best_combo["raw_probability"], league_calibrated_probability=math.prod(item["league_calibrated_probability"] for item in selected), league_calibration_samples=min(item["league_calibration_samples"] for item in selected), strategy_version=strategy_version))

    total = sum(item["stake"] for item in plan)
    if total > int(config["max_daily_budget"]):
        raise RuntimeError("今日模拟预算超过上限，请检查配置。")
    observations = []
    for item in sorted(observation_pool, key=lambda candidate: candidate["raw_probability"], reverse=True)[: int(value.get("observation_count", 5))]:
        reason = f"零金额观察单；保守概率{pct(item['probability'])}，联赛校准概率{pct(item['league_calibrated_probability'])}，原模型{pct(item['raw_probability'])}，市场公平概率{pct(item['market_probability'])}，仅用于概率校准和CLV，不计入盈亏"
        observations.append(make_item(item["row"], "观察单", item["selection"], item["probability"], item["odds"], 0, reason, market_probability=item["market_probability"], value_edge=item["value_edge"], raw_model_probability=item["raw_probability"], league_calibrated_probability=item["league_calibrated_probability"], league_calibration_samples=item["league_calibration_samples"], strategy_version=strategy_version))
    return plan, observations


# Kept as a compatibility entry point for the pre-v4 comparison tests/readers.
build_value_plan = build_legacy_value_plan


BEIJING = timezone(timedelta(hours=8))
TRAINING_RESULT_MATURITY = timedelta(minutes=130)
_LAST_VALUE_V4_AUDIT: dict = {}
PLAN_FIELD_ORDER = (
    "date", "bet_id", "report_date", "strategy_version", "model_version",
    "stage", "match", "team_a", "team_b", "match_id", "kickoff_local",
    "play", "market_type", "market_line", "selection", "probability",
    "raw_probability", "raw_model_probability", "calibrated_probability",
    "league_calibrated_probability", "league_calibration_samples",
    "official_market_probability", "odds", "locked_odds", "locked_at_bjt",
    "odds_source", "odds_source_record_id", "odds_captured_at_bjt",
    "market_probability", "conservative_probability", "edge", "value_edge",
    "net_ev", "expected_value", "stake", "expected_return", "expected_profit",
    "data_quality", "volatility_band", "full_kelly", "kelly_fraction",
    "data_quality_multiplier", "volatility_multiplier", "performance_multiplier",
    "portfolio_rank", "binding_limits", "reason", "legs_json",
)


@dataclass(frozen=True)
class StrategyOutputs:
    active_plan: list[dict]
    observations: list[dict]
    shadow_plan: list[dict]
    audit: dict


@dataclass(frozen=True)
class ValueV4BuildResult:
    plan: list[dict]
    observations: list[dict]
    candidates: list[ValueCandidate]
    diagnostics: list[dict]
    audit: dict


def load_value_snapshot(target_date: date, *, locked_at: datetime | None = None) -> dict:
    """Load the latest decision snapshot at or before the decision lock."""
    cutoff = _aware_locked_at(locked_at) if locked_at is not None else None
    snapshots = DATA_DIR / "odds_snapshots"
    candidates = sorted(snapshots.glob(f"{target_date.isoformat()}-*-decision.json"))
    for path in reversed(candidates):
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        captured_at = _snapshot_datetime(payload.get("captured_at")) if isinstance(payload, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("target_date") == target_date.isoformat()
            and payload.get("capture_phase") == "decision"
            and captured_at is not None
            and (cutoff is None or captured_at <= cutoff)
        ):
            payload = dict(payload)
            payload["_snapshot_record_id"] = f"data/odds_snapshots/{path.name}"
            return payload
    return {"target_date": target_date.isoformat(), "capture_phase": "decision", "matches": []}


def load_official_decision_markets(
    target_date: date,
    *,
    snapshot: dict | None = None,
    diagnostics: list[dict] | None = None,
) -> dict[str, dict]:
    """Normalize only markets evidenced by the decision snapshot."""
    snapshot = load_value_snapshot(target_date) if snapshot is None else snapshot
    markets: dict[str, dict] = {}
    raw_matches = snapshot.get("matches") if isinstance(snapshot, dict) else None
    if not isinstance(raw_matches, list):
        _market_diagnostic(
            diagnostics,
            "snapshot_matches_invalid",
            {"target_date": target_date.isoformat()},
        )
        return markets
    for match_index, match in enumerate(raw_matches):
        if not isinstance(match, dict):
            _market_diagnostic(
                diagnostics,
                "snapshot_match_invalid",
                {"match_index": match_index},
            )
            continue
        match_id = match.get("match_id")
        if not (
            isinstance(match_id, str)
            and bool(match_id)
            and match_id == match_id.strip()
            and all(
                character.isprintable() and not character.isspace()
                for character in match_id
            )
        ):
            _market_diagnostic(
                diagnostics,
                "snapshot_match_id_invalid",
                {
                    "match_id": match_id if isinstance(match_id, str) else "",
                    "match_index": match_index,
                },
            )
            continue
        raw_markets = match.get("markets")
        if not isinstance(raw_markets, dict):
            _market_diagnostic(
                diagnostics,
                "snapshot_markets_invalid",
                {"match_id": match_id},
            )
            continue
        captured_at = snapshot.get("captured_at")
        source = snapshot.get("source")
        for market_key in sorted(raw_markets, key=str):
            if market_key not in {"had", "hhad", "ttg"}:
                _market_diagnostic(
                    diagnostics,
                    "unsupported_market_key",
                    {"match_id": match_id, "market_type": str(market_key)},
                )
        for market_type in ("had", "hhad", "ttg"):
            if market_type not in raw_markets:
                continue
            raw = raw_markets.get(market_type)
            if not isinstance(raw, dict):
                _market_diagnostic(
                    diagnostics,
                    "market_payload_invalid",
                    {"match_id": match_id, "market_type": market_type},
                )
                continue
            if not raw:
                continue
            enriched = dict(raw)
            enriched.setdefault("source", source)
            snapshot_record = snapshot.get("source_record_id") or snapshot.get("_snapshot_record_id")
            if not snapshot_record:
                _market_diagnostic(
                    diagnostics,
                    "market_source_record_missing",
                    {"match_id": match_id, "market_type": market_type},
                )
                continue
            enriched.setdefault("source_record_id", f"{snapshot_record}#{match_id}:{market_type}")
            enriched.setdefault("captured_at_bjt", captured_at)
            market = normalize_market(match_id, market_type, enriched)
            if market is not None:
                markets.setdefault(match_id, {})[market_type] = market
            else:
                _market_diagnostic(
                    diagnostics,
                    "market_normalization_rejected",
                    {"match_id": match_id, "market_type": market_type},
                )
    return markets


def _market_diagnostic(
    diagnostics: list[dict] | None,
    code: str,
    context: dict,
) -> None:
    if diagnostics is not None:
        diagnostics.append({"code": code, "context": context})


def _aware_locked_at(locked_at: datetime) -> datetime:
    if not isinstance(locked_at, datetime) or locked_at.tzinfo is None or locked_at.utcoffset() is None:
        raise ValueError("locked_at must be an aware ISO-8601 datetime")
    return locked_at.astimezone(BEIJING)


def _snapshot_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(BEIJING)


def ledger_history_as_of(
    rows: list[dict], target_date: date, locked_at: datetime
) -> list[dict]:
    """Return only ledger state knowable at the aware decision boundary."""
    boundary = _aware_locked_at(locked_at)
    available = []
    for index, source_row in enumerate(rows):
        if not isinstance(source_row, dict):
            raise ValueError(f"ledger row {index} must be a mapping")
        row = dict(source_row)
        report_value = row.get("report_date") or row.get("date")
        try:
            report_date = date.fromisoformat(str(report_value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"ledger row {index} report_date is invalid") from exc
        locked_value = row.get("locked_at_bjt")
        locked = _snapshot_datetime(locked_value)
        legacy_without_lock = (
            not str(locked_value or "").strip()
            and row.get("strategy_version") != "value-v4"
            and report_date < target_date
        )
        if locked is None and not legacy_without_lock:
            raise ValueError(f"ledger row {index} locked_at_bjt is invalid")
        if report_date > target_date or (locked is not None and locked > boundary):
            continue
        if row.get("status") in TERMINAL_STATUSES:
            settled_value = row.get("settled_at_bjt")
            settled = _snapshot_datetime(settled_value)
            if settled_value and settled is None:
                raise ValueError(f"ledger row {index} settled_at_bjt is invalid")
            if settled is None or settled > boundary:
                row.update({
                    "status": PENDING,
                    "result_status": "",
                    "result_source": "",
                    "source_record_id": "",
                    "captured_at_bjt": "",
                    "home_goals": "",
                    "away_goals": "",
                    "settled_at_bjt": "",
                    "result_legs_json": "",
                    "return": "0",
                    "profit": "0",
                    "clv": "",
                })
        available.append(row)
    return available


def training_samples_as_of(
    rows: list[dict], target_date: date, locked_at: datetime
) -> list[dict]:
    """Filter draw calibration samples to information available before target."""
    boundary = _aware_locked_at(locked_at)
    available = []
    for index, source_row in enumerate(rows):
        if not isinstance(source_row, dict):
            raise ValueError(f"training sample {index} must be a mapping")
        row = dict(source_row)
        try:
            sample_date = date.fromisoformat(str(row.get("date")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"training sample {index} date is invalid") from exc
        captured = _snapshot_datetime(row.get("captured_at"))
        if captured is None:
            raise ValueError(f"training sample {index} captured_at is invalid")
        kickoff = _snapshot_datetime(row.get("kickoff_at"))
        if kickoff is None:
            raise ValueError(f"training sample {index} kickoff_at is invalid")
        if captured >= kickoff:
            raise ValueError(f"training sample {index} capture is not pre-match")
        if (
            sample_date >= target_date
            or captured > boundary
            or kickoff + TRAINING_RESULT_MATURITY > boundary
        ):
            continue
        available.append(row)
    return available


def _snapshot_odds(snapshot: dict) -> dict[str, dict]:
    rows = snapshot.get("matches") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        row["match_id"]: row["markets"]
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("match_id"), str)
        and isinstance(row.get("markets"), dict)
    }


def _finalize_legacy_plan(
    rows: list[dict], markets: dict[str, dict], locked_at: datetime
) -> list[dict]:
    finalized = []
    for rank, source_row in enumerate(rows, start=1):
        row = dict(source_row)
        try:
            raw_legs = json.loads(row.get("legs_json") or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("legacy legs_json must be valid JSON") from exc
        if raw_legs:
            if len(raw_legs) != 2:
                raise ValueError("legacy paid parlay must have exactly two legs")
            legs = []
            evidence = []
            for leg in raw_legs:
                match_id = str(leg.get("match_id") or "").strip()
                market = markets.get(match_id, {}).get("had")
                if not match_id or market is None:
                    raise ValueError("legacy parlay leg lacks canonical official identity")
                selection = str(leg.get("selection") or "").strip()
                official_odds = market.prices.get(selection)
                if official_odds is None or not math.isclose(
                    float(leg.get("odds")), official_odds, rel_tol=0.0, abs_tol=1e-12
                ):
                    raise ValueError("legacy parlay odds do not match decision evidence")
                legs.append({
                    **leg,
                    "match_id": match_id,
                    "market_type": "had",
                    "line": "",
                    "odds": format(Decimal(str(official_odds)), "f"),
                    "odds_source": market.source,
                    "odds_source_record_id": market.source_record_id,
                    "odds_captured_at_bjt": market.captured_at_bjt,
                })
                evidence.append(market)
            if len({leg["match_id"] for leg in legs}) != 2:
                raise ValueError("legacy paid parlay legs must use distinct matches")
            locked_odds = Decimal("1")
            for leg in legs:
                locked_odds *= Decimal(leg["odds"])
            if not math.isclose(
                float(row.get("odds")), float(locked_odds),
                rel_tol=0.0, abs_tol=0.01,
            ):
                raise ValueError("legacy parlay price does not match its official legs")
            row.update(
                match_id="",
                market_type="parlay",
                market_line="",
                legs_json=json.dumps(legs, ensure_ascii=False, sort_keys=True),
                odds=format(locked_odds, "f"),
                odds_source=evidence[0].source,
                odds_source_record_id=json.dumps(
                    sorted(item.source_record_id for item in evidence), ensure_ascii=False
                ),
                odds_captured_at_bjt=max(item.captured_at_bjt for item in evidence),
                locked_odds=format(locked_odds, "f"),
            )
        else:
            match_id = str(row.get("match_id") or "").strip()
            market = markets.get(match_id, {}).get("had")
            if not match_id or market is None:
                raise ValueError("legacy paid single lacks canonical official identity")
            selection = str(row.get("selection") or "").strip()
            official_odds = market.prices.get(selection)
            if official_odds is None or not math.isclose(
                float(row.get("odds")), official_odds, rel_tol=0.0, abs_tol=1e-12
            ):
                raise ValueError("legacy single odds do not match decision evidence")
            row.update(
                match_id=match_id,
                market_type="had",
                market_line="",
                odds=format(Decimal(str(official_odds)), "f"),
                odds_source=market.source,
                odds_source_record_id=market.source_record_id,
                odds_captured_at_bjt=market.captured_at_bjt,
                locked_odds=format(Decimal(str(official_odds)), "f"),
            )
        probability = float(row.get("probability"))
        odds = float(row.get("locked_odds"))
        row.update(
            report_date=row.get("date", ""),
            model_version="legacy-v3",
            locked_at_bjt=locked_at.isoformat(),
            raw_probability=row.get("raw_model_probability", probability),
            calibrated_probability=row.get("league_calibrated_probability", probability),
            official_market_probability=row.get("market_probability", ""),
            conservative_probability=probability,
            edge=row.get("value_edge", ""),
            net_ev=probability * odds - 1.0,
            expected_value=probability * odds - 1.0,
            expected_return=probability * odds,
            data_quality="medium",
            volatility_band="stable",
            full_kelly=max(0.0, (probability * odds - 1.0) / (odds - 1.0)),
            kelly_fraction=0.25,
            data_quality_multiplier=0.6,
            volatility_multiplier=1.0,
            performance_multiplier=1.0,
            portfolio_rank=rank,
            binding_limits="[]",
        )
        row["bet_id"] = stable_bet_id(row)
        finalized.append(row)
    return finalized


def _v4_config(config: dict, settled_samples: int) -> dict:
    """Adapt the public net-EV names to the candidate builder's value gate."""
    payload = json.loads(json.dumps(config))
    value = payload.setdefault("value_strategy", {})
    value["settled_samples"] = settled_samples
    value["strict_min_expected_value"] = value.get("strict_min_ev")
    value["min_expected_value"] = value.get("min_ev")
    return payload


def _v4_limits(config: dict, account: dict, settled_samples: int) -> PortfolioLimits:
    value = config.get("value_strategy", {})
    strict = settled_samples < int(value.get("strict_until_samples", 100))
    return PortfolioLimits(
        bankroll=float(value.get("reference_bankroll", 5000)),
        kelly_fraction=float(value.get("strict_kelly_fraction" if strict else "kelly_fraction", 0.25)),
        stake_unit=int(value.get("stake_unit", 2)),
        max_match_exposure=int(value.get("max_match_exposure", 200)),
        max_single_stake=int(value.get("strict_max_single_stake" if strict else "max_single_stake", 200)),
        single_budget_cap=int(value.get("strict_single_budget_cap" if strict else "single_budget_cap", 200)),
        max_single_count=int(value.get("max_single_count", 2)),
        min_single_stake=int(value.get("min_single_stake", value.get("stake_unit", 2))),
        max_parlay_stake=int(value.get("max_daily_combo_stake", 30)),
        min_parlay_stake=int(value.get("stake_unit", 2)),
        max_daily_stake=int(config.get("max_daily_budget", 500)),
        monthly_budget_cap=int(account.get("monthly_budget_cap", 5000)),
        monthly_stop_loss=int(account.get("monthly_stop_loss", 5000)),
        settled_samples=settled_samples,
        strict_until_samples=int(value.get("strict_until_samples", 100)),
        min_combo_leg_probability=float(value.get("min_combo_leg_probability", 0.45)),
        min_combo_leg_edge=float(value.get("strict_min_combo_leg_edge" if strict else "min_combo_leg_edge", 0.01)),
        min_combo_leg_ev=float(value.get("strict_min_combo_leg_ev" if strict else "min_combo_leg_ev", 0.01)),
        min_combo_ev=float(value.get("strict_min_combo_ev" if strict else "min_combo_ev", 0.03)),
    )


def _settled_sample_count(
    history: list[dict], observation_history: list[dict], target_date: date
) -> int:
    """Count unique terminal v4 market units strictly before target_date."""
    market_units: set[tuple[str, str, str, str]] = set()
    for row in [*history, *observation_history]:
        if not isinstance(row, dict) or row.get("strategy_version") != "value-v4":
            continue
        if row.get("status") not in TERMINAL_STATUSES:
            continue
        try:
            row_date = date.fromisoformat(str(row.get("report_date") or row.get("date")))
        except ValueError:
            continue
        if row_date >= target_date:
            continue
        identities = settled_market_identities(row)
        units = [
            _market_sample_key(
                row_date,
                identity.get("match_id"),
                identity.get("market_type"),
                identity.get("line", ""),
            )
            for identity in identities
        ]
        if identities and all(unit is not None for unit in units):
            market_units.update(unit for unit in units if unit is not None)
    return len(market_units)


def _market_sample_key(
    report_date: date,
    match_id: object,
    market_type: object,
    line: object,
) -> tuple[str, str, str, str] | None:
    if not (
        isinstance(match_id, str)
        and bool(match_id)
        and match_id == match_id.strip()
        and all(
            character.isprintable() and not character.isspace()
            for character in match_id
        )
    ):
        return None
    market = str(market_type or "").strip().lower()
    if market not in {"had", "hhad", "ttg"}:
        return None
    line_text = "" if line is None else str(line).strip()
    if market == "hhad":
        try:
            canonical_line = str(parse_handicap(line_text))
        except ValueError:
            return None
    else:
        if line_text:
            return None
        canonical_line = ""
    return report_date.isoformat(), match_id, market, canonical_line


def _candidate_plan_row(
    candidate: ValueCandidate,
    stake: int,
    *,
    locked_at: datetime,
    portfolio_rank: int,
    full_kelly: float = 0.0,
    kelly_fraction: float = 0.25,
    binding_limits: tuple[str, ...] = (),
    market_type: str | None = None,
    play: str | None = None,
    selection: str | None = None,
    odds: float | Decimal | None = None,
    probability: float | None = None,
    legs: list[dict] | None = None,
) -> dict:
    market_type = market_type or candidate.market_type
    odds = candidate.official_odds if odds is None else odds
    odds_number = float(odds)
    serialized_odds = format(odds, "f") if isinstance(odds, Decimal) else odds
    probability = candidate.conservative_probability if probability is None else probability
    row = {
        "date": candidate.date,
        "report_date": candidate.date,
        "strategy_version": "value-v4",
        "model_version": "value-v4",
        "stage": candidate.stage,
        "match": f"{candidate.team_a} vs {candidate.team_b}",
        "team_a": candidate.team_a,
        "team_b": candidate.team_b,
        "match_id": candidate.match_id if market_type != "parlay" else "",
        "kickoff_local": candidate.kickoff_at,
        "play": play or candidate.play,
        "market_type": market_type,
        "market_line": (
            "" if market_type == "parlay" or candidate.line is None
            else str(candidate.line)
        ),
        "selection": selection or candidate.selection,
        "probability": probability,
        "raw_probability": candidate.raw_model_probability,
        "raw_model_probability": candidate.raw_model_probability,
        "calibrated_probability": candidate.calibrated_model_probability,
        "league_calibrated_probability": candidate.calibrated_model_probability,
        "league_calibration_samples": candidate.calibration_samples,
        "official_market_probability": candidate.official_market_probability,
        "market_probability": candidate.official_market_probability,
        "conservative_probability": probability,
        "edge": candidate.probability_edge,
        "value_edge": candidate.probability_edge,
        "net_ev": probability * odds_number - 1.0,
        "expected_value": probability * odds_number - 1.0,
        "expected_return": probability * odds_number,
        "expected_profit": stake * (probability * odds_number - 1.0),
        "odds": serialized_odds,
        "locked_odds": serialized_odds,
        "locked_at_bjt": locked_at.isoformat(),
        "odds_source": candidate.odds_source,
        "odds_source_record_id": candidate.source_record_id,
        "odds_captured_at_bjt": candidate.captured_at_bjt,
        "data_quality": candidate.data_quality,
        "data_quality_multiplier": candidate.data_quality_multiplier,
        "volatility_band": candidate.volatility_band,
        "volatility_multiplier": candidate.volatility_multiplier,
        "performance_multiplier": candidate.performance_multiplier,
        "full_kelly": full_kelly,
        "kelly_fraction": kelly_fraction,
        "portfolio_rank": portfolio_rank,
        "binding_limits": json.dumps(list(binding_limits), ensure_ascii=False),
        "stake": stake,
        "reason": "value-v4 verified candidate",
        "legs_json": json.dumps(legs or [], ensure_ascii=False, sort_keys=True),
    }
    row["bet_id"] = stable_bet_id(row)
    return row


def _portfolio_rows(portfolio: Portfolio, locked_at: datetime) -> list[dict]:
    rows = [
        _candidate_plan_row(
            item.candidate, item.stake, locked_at=locked_at, portfolio_rank=item.rank,
            full_kelly=item.full_kelly, kelly_fraction=item.kelly_fraction,
            binding_limits=item.applied_limits,
        )
        for item in portfolio.singles
    ]
    if portfolio.parlay is not None:
        item = portfolio.parlay
        legs = [
            {
                "match_id": leg.match_id, "market_type": leg.market_type,
                "selection": leg.selection, "line": "" if leg.line is None else str(leg.line),
                "odds": format(Decimal(str(leg.official_odds)), "f"),
                "locked_odds": format(Decimal(str(leg.official_odds)), "f"),
                "odds_source": leg.odds_source,
                "odds_source_record_id": leg.source_record_id,
                "odds_captured_at_bjt": leg.captured_at_bjt,
                "expected_value": (
                    leg.conservative_probability * float(leg.official_odds) - 1.0
                ),
                "net_ev": (
                    leg.conservative_probability * float(leg.official_odds) - 1.0
                ),
            }
            for leg in item.parlay.legs
        ]
        combined_odds = Decimal("1")
        for leg in legs:
            combined_odds *= Decimal(leg["odds"])
        rows.append(_candidate_plan_row(
            item.parlay.legs[0], item.stake, locked_at=locked_at, portfolio_rank=item.rank,
            full_kelly=item.full_kelly, kelly_fraction=item.kelly_fraction,
            binding_limits=item.applied_limits, market_type="parlay", play="PARLAY",
            selection=" + ".join(leg.selection for leg in item.parlay.legs),
            odds=combined_odds, probability=item.parlay.combined_probability, legs=legs,
        ))
    return rows


def _portfolio_audit(
    candidates: list[ValueCandidate],
    portfolio: Portfolio,
    limits: PortfolioLimits,
    diagnostics: list[dict] | None = None,
) -> dict:
    counts = {market: sum(candidate.market_type == market for candidate in candidates) for market in ("had", "hhad", "ttg")}
    candidate_diagnostics = sorted(
        (dict(item) for item in diagnostics or []),
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )
    risk_checks = [
        {"name": check.name, "value": check.value, "limit": check.limit, "passed": check.passed}
        for check in portfolio.limit_checks
    ]
    check_names = {check["name"] for check in risk_checks}
    selected = [*portfolio.singles, *portfolio.parlays]
    stakes = [item.stake for item in selected if item.stake > 0]

    def add_check(name: str, value: float, limit: float, passed: bool) -> None:
        if name not in check_names:
            risk_checks.append({
                "name": name, "value": value, "limit": limit, "passed": passed,
            })
            check_names.add(name)

    kelly_values = [item.kelly_fraction for item in selected]
    add_check(
        "kelly_fraction_cap",
        float(max(kelly_values, default=0.0)),
        0.25,
        all(value <= 0.25 for value in kelly_values),
    )
    add_check(
        "stake_unit",
        float(sum(1 for stake in stakes if stake % limits.stake_unit)),
        0.0,
        all(stake % limits.stake_unit == 0 for stake in stakes),
    )
    add_check(
        "max_single_count",
        float(len(portfolio.singles)),
        float(limits.max_single_count),
        len(portfolio.singles) <= limits.max_single_count,
    )
    add_check(
        "max_parlay_count",
        float(len(portfolio.parlays)),
        1.0,
        len(portfolio.parlays) <= 1,
    )
    return {
        "candidate_counts": counts,
        "rejection_reasons": [
            *sorted({*portfolio.rejections, *(reason for candidate in candidates for reason in candidate.value_gate_reasons)}),
            *candidate_diagnostics,
        ],
        "selected_shadow": [],
        "risk_checks": risk_checks,
        "risk_caps": {
            "bankroll": limits.bankroll,
            "reference_bankroll": limits.bankroll,
            "kelly_fraction": limits.kelly_fraction,
            "stake_unit": limits.stake_unit,
            "max_match_exposure": limits.max_match_exposure,
            "max_single_stake": limits.max_single_stake,
            "single_budget_cap": limits.single_budget_cap,
            "max_single_count": limits.max_single_count,
            "max_parlay_count": 1,
            "max_parlay_stake": limits.max_parlay_stake,
            "max_daily_combo_stake": limits.max_parlay_stake,
            "max_daily_stake": limits.max_daily_stake,
            "monthly_budget_cap": limits.monthly_budget_cap,
            "monthly_stop_loss": limits.monthly_stop_loss,
            "settled_samples": limits.settled_samples,
            "strict_until_samples": limits.strict_until_samples,
            "strict_mode": limits.settled_samples < limits.strict_until_samples,
        },
    }


def build_value_v4_from_inputs(
    target_date: date,
    *,
    locked_at: datetime,
    config: dict,
    predictions: list[dict],
    snapshot: dict,
    paid_history: list[dict],
    observation_history: list[dict],
    training_samples: list[dict],
) -> ValueV4BuildResult:
    """Build value-v4 deterministically from explicit, caller-owned inputs."""
    locked_time = _aware_locked_at(locked_at)
    paid_as_of = ledger_history_as_of(paid_history, target_date, locked_time)
    observations_as_of = ledger_history_as_of(
        observation_history, target_date, locked_time
    )
    training_as_of = training_samples_as_of(
        training_samples, target_date, locked_time
    )
    diagnostics: list[dict] = []
    markets = load_official_decision_markets(
        target_date, snapshot=snapshot, diagnostics=diagnostics
    )
    settled_samples = _settled_sample_count(
        paid_as_of, observations_as_of, target_date
    )
    account = simulation_account_state(
        paid_as_of,
        observations_as_of,
        target_date,
        config.get("simulation_account", {}),
    )
    calibrations_config = config.get("league_calibration", {})
    calibrations = fit_league_draw_calibrations(
        training_as_of,
        min_samples=int(calibrations_config.get("min_samples", 30)),
        prior_samples=int(calibrations_config.get("prior_samples", 60)),
        max_adjustment=float(calibrations_config.get("max_adjustment", 0.05)),
        validation_fraction=float(calibrations_config.get("validation_fraction", 0.25)),
    )
    candidates = build_candidates(
        predictions,
        markets,
        snapshot,
        _v4_config(config, settled_samples),
        calibrations,
        diagnostics=diagnostics,
    )
    limits = _validated_limits(_v4_limits(config, account, settled_samples))
    if limits is None:
        raise ValueError("value-v4 portfolio limits are invalid")
    portfolio = allocate_portfolio(candidates, limits, account)
    plan = _portfolio_rows(portfolio, locked_time)
    observations = [
        _candidate_plan_row(candidate, 0, locked_at=locked_time, portfolio_rank=0)
        for candidate in sorted(candidates, key=lambda item: item.candidate_id)
    ]
    audit = _portfolio_audit(candidates, portfolio, limits, diagnostics)
    audit["settled_samples"] = settled_samples
    audit["candidate_count"] = len(candidates)
    audit["observation_count"] = len(observations)
    audit["diagnostic_count"] = len(diagnostics)
    audit["selected_shadow"] = [
        {"bet_id": row["bet_id"], "stake": row["stake"], "market_type": row["market_type"]}
        for row in plan
    ]
    return ValueV4BuildResult(
        plan=plan,
        observations=observations,
        candidates=candidates,
        diagnostics=diagnostics,
        audit=audit,
    )


def build_value_v4_plan(
    target_date: date,
    *,
    locked_at: datetime,
    decision_bundle: dict | None = None,
    root: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    """Build a value-v4 portfolio from locked decision-market inputs only."""
    global _LAST_VALUE_V4_AUDIT
    locked_time = _aware_locked_at(locked_at)
    bundle_root = Path(root) if root is not None else ROOT
    if decision_bundle is not None:
        snapshot = dict(decision_bundle["decision_snapshot"]["payload"])
        snapshot["_snapshot_record_id"] = decision_bundle["decision_snapshot"]["path"]
        bundle_config = decision_bundle["configuration"]["betting"]["payload"]
        predictions = load_csv(bundle_root / decision_bundle["predictions"]["path"])
        histories = decision_bundle["history_inputs"]
        paid_history = histories["paid_history"]["rows"]
        observation_history = histories["observation_history"]["rows"]
        training_samples = histories["training_samples"]["rows"]
    else:
        bundle_config = read_json(ROOT / "betting_config.json")
        predictions = load_predictions(target_date)
        snapshot = load_value_snapshot(target_date, locked_at=locked_time)
        paid_history = load_csv(OUTPUT_DIR / "betting_ledger.csv")
        observation_history = load_csv(OUTPUT_DIR / "observation_ledger.csv")
        training_samples = load_draw_training_samples()
    result = build_value_v4_from_inputs(
        target_date,
        locked_at=locked_time,
        config=bundle_config,
        predictions=predictions,
        snapshot=snapshot,
        paid_history=paid_history,
        observation_history=observation_history,
        training_samples=training_samples,
    )
    _LAST_VALUE_V4_AUDIT = result.audit
    return result.plan, result.observations


def build_paid_plan_from_bundle(
    target_date: date,
    *,
    locked_at: datetime,
    decision_bundle: dict,
    root: Path | None = None,
) -> list[dict]:
    """Rebuild the paid plan using only immutable bundle-owned inputs."""
    if not isinstance(decision_bundle, dict):
        raise ValueError("decision_bundle must be a mapping")
    bundle_root = Path(root) if root is not None else ROOT
    config = decision_bundle["configuration"]["betting"]["payload"]
    mode = config.get("value_strategy", {}).get("activation_mode")
    if mode not in {"shadow", "active"}:
        raise ValueError("activation_mode must be shadow or active")
    if mode == "active":
        plan, _observations = build_value_v4_plan(
            target_date,
            locked_at=locked_at,
            decision_bundle=decision_bundle,
            root=bundle_root,
        )
        return plan

    snapshot = dict(decision_bundle["decision_snapshot"]["payload"])
    snapshot["_snapshot_record_id"] = decision_bundle["decision_snapshot"]["path"]
    predictions = load_csv(bundle_root / decision_bundle["predictions"]["path"])
    histories = decision_bundle["history_inputs"]
    legacy_plan, _observations = build_legacy_value_plan(
        target_date,
        predictions=predictions,
        odds_by_match=_snapshot_odds(snapshot),
        odds_source=str(snapshot.get("source") or ""),
        config=config,
        paid_history=histories["paid_history"]["rows"],
        observation_history=histories["observation_history"]["rows"],
        training_samples=histories["training_samples"]["rows"],
        account_metrics=histories["account_metrics"]["payload"],
    )
    markets = load_official_decision_markets(target_date, snapshot=snapshot)
    return _finalize_legacy_plan(legacy_plan, markets, _aware_locked_at(locked_at))


def build_strategy_outputs(
    target_date: date,
    *,
    locked_at: datetime,
    decision_bundle: dict | None = None,
) -> StrategyOutputs:
    """Select the paid strategy strictly from the configured activation mode."""
    config = (
        decision_bundle["configuration"]["betting"]["payload"]
        if decision_bundle is not None
        else read_json(ROOT / "betting_config.json")
    )
    mode = config.get("value_strategy", {}).get("activation_mode")
    if mode not in {"shadow", "active"}:
        raise ValueError("activation_mode must be shadow or active")
    if mode == "active":
        assert_activation_ready(ROOT, config=config)
    locked_time = _aware_locked_at(locked_at)
    legacy_plan = []
    if mode == "shadow":
        if decision_bundle is not None:
            legacy_plan = build_paid_plan_from_bundle(
                target_date,
                locked_at=locked_time,
                decision_bundle=decision_bundle,
            )
        else:
            snapshot = load_value_snapshot(target_date, locked_at=locked_time)
            predictions = load_predictions(target_date)
            markets = load_official_decision_markets(target_date, snapshot=snapshot)
            legacy_plan, _ = build_legacy_value_plan(
                target_date,
                predictions=predictions,
                odds_by_match=_snapshot_odds(snapshot),
                odds_source=str(snapshot.get("source") or ""),
            )
            legacy_plan = _finalize_legacy_plan(legacy_plan, markets, locked_time)
    v4_plan, observations = build_value_v4_plan(
        target_date,
        locked_at=locked_time,
        decision_bundle=decision_bundle,
    )
    audit = dict(_LAST_VALUE_V4_AUDIT)
    active_plan = legacy_plan if mode == "shadow" else v4_plan
    shadow_plan = v4_plan if mode == "shadow" else []
    audit.update({
        "activation_mode": mode,
        "selected_shadow": [
            {"bet_id": row["bet_id"], "stake": row["stake"], "market_type": row["market_type"]}
            for row in shadow_plan
        ],
        "shadow_paid_stake": 0,
        "comparison": {
            "active_paid_stake": sum(float(row.get("stake", 0) or 0) for row in active_plan),
            "shadow_paid_stake": 0,
            "shadow_candidate_stake": sum(float(row.get("stake", 0) or 0) for row in shadow_plan),
        },
    })
    return StrategyOutputs(active_plan, observations, shadow_plan, audit)


def write_shadow_audit(audit: dict, target_date: date) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"shadow_portfolio_audit_{target_date.isoformat()}.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_results() -> dict[str, dict]:
    path = DATA_DIR / "bet_results.csv"
    if not path.exists():
        return {}
    results = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            match_id = row.get("match_id")
            if match_id:
                results[str(match_id)] = row
    return results


def write_plan(plan: list[dict], target_date: date) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"betting_plan_{target_date.isoformat()}.csv"
    path.write_bytes(plan_csv_bytes(plan))
    return path


def plan_csv_bytes(plan: list[dict]) -> bytes:
    fields = _plan_fields(plan, list(PLAN_FIELD_ORDER))
    with io.StringIO(newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(plan)
        return b"\xef\xbb\xbf" + handle.getvalue().encode("utf-8")


def write_shadow_plan(plan: list[dict], target_date: date) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"shadow_betting_plan_{target_date.isoformat()}.csv"
    fields = _plan_fields(plan, list(PLAN_FIELD_ORDER))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(plan)
    return path


def write_observation_plan(observations: list[dict], target_date: date) -> Path:
    path = OUTPUT_DIR / f"observation_plan_{target_date.isoformat()}.csv"
    fields = _plan_fields(observations, list(PLAN_FIELD_ORDER))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(observations)
    return path


def _plan_fields(rows: list[dict], preferred: list[str]) -> list[str]:
    present = {key for row in rows if isinstance(row, dict) for key in row}
    return [*preferred, *sorted(present.difference(preferred))]


def load_all_observation_plans() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(OUTPUT_DIR.glob("observation_plan_*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def write_observation_ledger(
    results: dict | None = None,
    settled_at: datetime | None = None,
) -> Path:
    path = OUTPUT_DIR / "observation_ledger.csv"
    result_map = load_results() if results is None else results
    settlement_time = datetime.now(BEIJING) if settled_at is None else settled_at
    rows = update_observation_ledger(
        load_csv(path),
        load_all_observation_plans(),
        result_map,
        settlement_time,
    )
    return write_ledger_atomic(path, rows)


def write_daily_decision(
    target_date: date,
    plan: list[dict] | None = None,
    observations: list[dict] | None = None,
) -> Path:
    config = read_json(ROOT / "betting_config.json")
    plan = plan if plan is not None else load_csv(OUTPUT_DIR / f"betting_plan_{target_date.isoformat()}.csv")
    observations = observations if observations is not None else load_csv(OUTPUT_DIR / f"observation_plan_{target_date.isoformat()}.csv")
    betting_rows = load_csv(OUTPUT_DIR / "betting_ledger.csv")
    observation_rows = load_csv(OUTPUT_DIR / "observation_ledger.csv")
    metrics = {}
    metrics_path = OUTPUT_DIR / "model_metrics.json"
    if metrics_path.exists():
        try:
            metrics = read_json(metrics_path)
        except (OSError, json.JSONDecodeError):
            metrics = {}
    account = simulation_account_state(
        betting_rows,
        observation_rows,
        target_date,
        config.get("simulation_account", {}),
        metrics,
    )
    decision = build_daily_decision(
        plan,
        observations,
        target_date,
        len(load_predictions(target_date)),
        account,
        config.get("learning_policy", {}),
    )
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"daily_decision_{target_date.isoformat()}.json"
    path.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate daily simulated sports lottery plan.")
    parser.add_argument("--date", default=date.today().isoformat())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--settle-only", action="store_true", help="Only settle the existing paid ledger.")
    modes.add_argument("--generate-only", action="store_true", help="Write plan artifacts without ledger ingestion.")
    parser.add_argument("--locked-at", help="Required aware ISO-8601 decision timestamp for generation.")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    if args.settle_only:
        if args.locked_at:
            parser.error("--locked-at is only valid for generation")
        results = load_results()
        settled_at = datetime.now(BEIJING)
        ledger_path = settle_ledger(ROOT, results, settled_at)
        observation_path = write_observation_ledger(results, settled_at)
        print(f"Settled ledger: {ledger_path}")
        print(f"Settled observation ledger: {observation_path}")
        return 0

    if not args.locked_at:
        parser.error("--locked-at is required for generation")
    try:
        locked_at = _aware_locked_at(datetime.fromisoformat(args.locked_at.replace("Z", "+00:00")))
    except ValueError as exc:
        parser.error(str(exc))
    lock_path = OUTPUT_DIR / f"plan_lock_{target_date.isoformat()}.json"
    if lock_path.exists():
        if read_valid_lock(ROOT, target_date) is None:
            print(f"Invalid existing plan lock: {lock_path}")
            return 1
        print(f"Reusing locked betting plan: {OUTPUT_DIR / f'betting_plan_{target_date.isoformat()}.csv'}")
        return 0
    try:
        decision_bundle = read_valid_decision_bundle(
            ROOT,
            target_date,
            expected_locked_at=locked_at,
            verify_current_inputs=True,
        )
    except ValueError as exc:
        print(f"Invalid decision bundle: {exc}")
        return 1
    outputs = build_strategy_outputs(
        target_date,
        locked_at=locked_at,
        decision_bundle=decision_bundle,
    )
    plan_path = write_plan(outputs.active_plan, target_date)
    observation_path = write_observation_plan(outputs.observations, target_date)
    shadow_path = write_shadow_plan(outputs.shadow_plan, target_date)
    audit_path = write_shadow_audit(outputs.audit, target_date)
    decision_path = write_daily_decision(target_date, outputs.active_plan, outputs.observations)
    total = sum(float(item.get("stake", 0) or 0) for item in outputs.active_plan)
    print(f"Generated betting plan: {plan_path}")
    print(f"Generated observation plan: {observation_path}")
    print(f"Generated shadow plan: {shadow_path}")
    print(f"Generated shadow audit: {audit_path}")
    print(f"Updated daily decision: {decision_path}")
    print(f"Daily simulated stake: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
