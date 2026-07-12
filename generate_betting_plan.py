import csv
import json
import math
from datetime import date, datetime
from pathlib import Path

from model_metrics import play_family, summarize, write_metrics


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


def make_item(row: dict, play: str, selection: str, probability: float, odds: float, stake: int, reason: str, legs=None, market_probability: float | None = None, value_edge: float | None = None) -> dict:
    return {
        "date": row["date"],
        "match": f"{row['team_a']} vs {row['team_b']}",
        "team_a": row["team_a"],
        "team_b": row["team_b"],
        "play": play,
        "selection": selection,
        "probability": probability,
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


def load_results() -> dict[tuple[str, str, str], dict]:
    path = DATA_DIR / "bet_results.csv"
    if not path.exists():
        return {}
    results = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if not row.get("home_goals") or not row.get("away_goals"):
                continue
            key = (row["date"], row["team_a"], row["team_b"])
            results[key] = row
    return results


def settle_item(item: dict, result: dict | None) -> tuple[str, float]:
    selection = item["selection"]
    stake = as_float(item, "stake")
    odds = as_float(item, "odds")
    if "串1" in item["play"] and item.get("legs_json"):
        results = load_results()
        try:
            legs = json.loads(item.get("legs_json") or "[]")
        except json.JSONDecodeError:
            legs = []
        won = bool(legs)
        for leg in legs:
            leg_result = results.get((leg["date"], leg["team_a"], leg["team_b"]))
            if leg_result is None:
                return "未结算", 0.0
            home_goals = int(leg_result["home_goals"])
            away_goals = int(leg_result["away_goals"])
            kind = leg.get("kind", "比分")
            if kind == "胜平负":
                actual = outcome(home_goals, away_goals)
                won_leg = actual == leg.get("selection")
            elif kind == "总进球":
                goals = home_goals + away_goals
                actual = "7+球" if goals >= 7 else f"{goals}球"
                won_leg = actual == leg.get("selection")
            else:
                actual = f"{home_goals}-{away_goals}"
                won_leg = actual == (leg.get("selection") or leg.get("score"))
            if not won_leg:
                won = False
        profit = stake * (odds - 1) if won else -stake
        return ("命中" if won else "未中", round(profit, 2))

    if result is None:
        return "未结算", 0.0
    home = int(result["home_goals"])
    away = int(result["away_goals"])
    half_home = int(result.get("half_home_goals") or 0)
    half_away = int(result.get("half_away_goals") or 0)
    won = False

    if item["play"] in {"胜平负", "平局单场"}:
        won = selection == outcome(home, away)
    elif item["play"] == "半全场":
        if result.get("half_home_goals") == "" or result.get("half_away_goals") == "":
            return "未结算", 0.0
        won = selection == outcome(half_home, half_away) + outcome(home, away)
    elif item["play"] == "比分":
        won = selection == f"{home}-{away}"
    elif item["play"] == "总进球":
        total_goals = home + away
        actual = "7+球" if total_goals >= 7 else f"{total_goals}球"
        won = selection == actual
    profit = stake * (odds - 1) if won else -stake
    return ("命中" if won else "未中", round(profit, 2))


def write_plan(plan: list[dict], target_date: date) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"betting_plan_{target_date.isoformat()}.csv"
    fields = [
        "date",
        "match",
        "team_a",
        "team_b",
        "play",
        "selection",
        "probability",
        "odds",
        "market_probability",
        "value_edge",
        "expected_value",
        "stake",
        "expected_return",
        "expected_profit",
        "reason",
        "legs_json",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(plan)
    return path


def load_all_plans() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(OUTPUT_DIR.glob("betting_plan_*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def write_ledger(plan: list[dict] | None = None) -> Path:
    path = OUTPUT_DIR / "betting_ledger.csv"
    results = load_results()
    if plan is None:
        plan = load_all_plans()
    fields = [
        "date",
        "match",
        "play",
        "selection",
        "probability",
        "odds",
        "market_probability",
        "value_edge",
        "expected_value",
        "stake",
        "status",
        "profit",
        "reason",
        "legs_json",
    ]
    rows = []
    for item in plan:
        result = results.get((item["date"], item["team_a"], item["team_b"]))
        status, profit = settle_item(item, result)
        rows.append(
            {
                "date": item["date"],
                "match": item["match"],
                "play": item["play"],
                "selection": item["selection"],
                "probability": item["probability"],
                "odds": item["odds"],
                "market_probability": item.get("market_probability", ""),
                "value_edge": item.get("value_edge", ""),
                "expected_value": item.get("expected_value", ""),
                "stake": item["stake"],
                "status": status,
                "profit": profit,
                "reason": item["reason"],
                "legs_json": item.get("legs_json", ""),
            }
        )
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate daily simulated sports lottery plan.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--settle-only", action="store_true", help="Only update ledger from existing plans and results.")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    if args.settle_only:
        ledger_path = write_ledger()
        write_metrics()
        print(f"Updated ledger: {ledger_path}")
        return 0

    plan = build_plan(target_date)
    plan_path = write_plan(plan, target_date)
    ledger_path = write_ledger()
    write_metrics()
    total = sum(item["stake"] for item in plan)
    print(f"Generated betting plan: {plan_path}")
    print(f"Updated ledger: {ledger_path}")
    print(f"Daily simulated stake: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
