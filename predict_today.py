import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"


@dataclass(frozen=True)
class TeamRating:
    team: str
    elo: float
    attack: float
    defense: float
    form: float
    injury: float
    rest_days: float
    home_adv: float


@dataclass(frozen=True)
class Fixture:
    match_date: date
    kickoff_local: str
    stage: str
    team_a: str
    team_b: str
    neutral: bool
    venue: str
    odds_a: float | None
    odds_draw: float | None
    odds_b: float | None
    market_odds_a: float | None = None
    market_odds_draw: float | None = None
    market_odds_b: float | None = None
    analysis_source: str = ""
    match_num: str = ""
    match_id: str = ""


def read_config() -> dict:
    with (ROOT / "config.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def to_float(value: str, default: float = 0.0) -> float:
    value = (value or "").strip()
    return float(value) if value else default


def to_optional_float(value: str) -> float | None:
    value = (value or "").strip()
    return float(value) if value else None


def load_ratings() -> dict[str, TeamRating]:
    path = DATA_DIR / "team_ratings.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = csv.DictReader(fh)
        ratings = {
            row["team"].strip(): TeamRating(
                team=row["team"].strip(),
                elo=to_float(row["elo"]),
                attack=to_float(row["attack"]),
                defense=to_float(row["defense"]),
                form=to_float(row["form"]),
                injury=to_float(row["injury"]),
                rest_days=to_float(row["rest_days"], 3.0),
                home_adv=to_float(row["home_adv"]),
            )
            for row in rows
        }
    history_path = DATA_DIR / "team_history_features.csv"
    if history_path.exists():
        with history_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                team = row["team"].strip()
                if team not in ratings:
                    continue
                base = ratings[team]
                sample = min(1.0, to_float(row.get("matches", "0")) / 6.0)
                weight = 0.65 * sample
                ratings[team] = TeamRating(
                    team=team,
                    elo=base.elo,
                    attack=(1 - weight) * base.attack + weight * to_float(row.get("attack", "")),
                    defense=(1 - weight) * base.defense + weight * to_float(row.get("defense", "")),
                    form=(1 - weight) * base.form + weight * to_float(row.get("form", "")),
                    injury=base.injury,
                    rest_days=to_float(row.get("rest_days", ""), base.rest_days),
                    home_adv=base.home_adv,
                )
    return ratings


def load_fixtures() -> list[Fixture]:
    path = DATA_DIR / "fixtures.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = csv.DictReader(fh)
        fixtures = []
        for row in rows:
            fixtures.append(
                Fixture(
                    match_date=datetime.strptime(row["date"].strip(), "%Y-%m-%d").date(),
                    kickoff_local=row["kickoff_local"].strip(),
                    stage=row["stage"].strip().lower(),
                    team_a=row["team_a"].strip(),
                    team_b=row["team_b"].strip(),
                    neutral=row["neutral"].strip().lower() in {"true", "1", "yes", "y"},
                    venue=row["venue"].strip(),
                    odds_a=to_optional_float(row.get("odds_a", "")),
                    odds_draw=to_optional_float(row.get("odds_draw", "")),
                    odds_b=to_optional_float(row.get("odds_b", "")),
                    market_odds_a=to_optional_float(row.get("market_odds_a", "")),
                    market_odds_draw=to_optional_float(row.get("market_odds_draw", "")),
                    market_odds_b=to_optional_float(row.get("market_odds_b", "")),
                    analysis_source=(row.get("analysis_source", "") or "").strip(),
                    match_num=(row.get("match_num", "") or "").strip(),
                    match_id=(row.get("match_id", "") or "").strip(),
                )
            )
        return fixtures


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def poisson_pmf(lam: float, max_goals: int) -> list[float]:
    values = [math.exp(-lam) * (lam**k) / math.factorial(k) for k in range(max_goals + 1)]
    total = sum(values)
    return [value / total for value in values]


def expected_goals(a: TeamRating, b: TeamRating, fixture: Fixture, config: dict) -> tuple[float, float]:
    elo_term = ((a.elo - b.elo) / 400.0) * config["elo_goal_weight"]
    rest_term = clamp(a.rest_days - b.rest_days, -3.0, 3.0) * config["rest_weight"]
    home_a = 0.0 if fixture.neutral else a.home_adv * config["home_adv_weight"]
    home_b = 0.0 if fixture.neutral else b.home_adv * config["home_adv_weight"]

    a_log = (
        math.log(config["base_goals"])
        + elo_term
        + a.attack * config["attack_weight"]
        - b.defense * config["defense_weight"]
        + (a.form - b.form) * config["form_weight"]
        + a.injury * config["injury_weight"]
        + rest_term
        + home_a
    )
    b_log = (
        math.log(config["base_goals"])
        - elo_term
        + b.attack * config["attack_weight"]
        - a.defense * config["defense_weight"]
        + (b.form - a.form) * config["form_weight"]
        + b.injury * config["injury_weight"]
        - rest_term
        + home_b
    )
    return clamp(math.exp(a_log), 0.15, 4.5), clamp(math.exp(b_log), 0.15, 4.5)


def score_distribution(lam_a: float, lam_b: float, max_goals: int, rho: float = -0.10) -> dict:
    dist_a = poisson_pmf(lam_a, max_goals)
    dist_b = poisson_pmf(lam_b, max_goals)
    p_a = p_draw = p_b = 0.0
    scores = []

    for goals_a, pa in enumerate(dist_a):
        for goals_b, pb in enumerate(dist_b):
            p = pa * pb
            if goals_a == 0 and goals_b == 0:
                p *= 1 - lam_a * lam_b * rho
            elif goals_a == 0 and goals_b == 1:
                p *= 1 + lam_a * rho
            elif goals_a == 1 and goals_b == 0:
                p *= 1 + lam_b * rho
            elif goals_a == 1 and goals_b == 1:
                p *= 1 - rho
            if goals_a > goals_b:
                p_a += p
            elif goals_a == goals_b:
                p_draw += p
            else:
                p_b += p
            scores.append(((goals_a, goals_b), p))

    total = p_a + p_draw + p_b
    p_a, p_draw, p_b = p_a / total, p_draw / total, p_b / total
    scores = [(score, probability / total) for score, probability in scores]
    scores.sort(key=lambda item: item[1], reverse=True)
    return {"p_a": p_a, "p_draw": p_draw, "p_b": p_b, "top_scores": scores[:5]}


def market_probabilities(fixture: Fixture) -> tuple[float, float, float] | None:
    odds = (fixture.market_odds_a, fixture.market_odds_draw, fixture.market_odds_b)
    if not all(odds):
        odds = (fixture.odds_a, fixture.odds_draw, fixture.odds_b)
    if not all(odds):
        return None
    implied = [1 / value for value in odds]
    total = sum(implied)
    return implied[0] / total, implied[1] / total, implied[2] / total


def blend_with_market(model: tuple[float, float, float], market: tuple[float, float, float] | None, weight: float) -> tuple[float, float, float]:
    if market is None:
        return model
    blended = tuple((1 - weight) * m + weight * o for m, o in zip(model, market))
    total = sum(blended)
    return blended[0] / total, blended[1] / total, blended[2] / total


def advancement_probabilities(p_a: float, p_draw: float, p_b: float, a: TeamRating, b: TeamRating) -> tuple[float, float]:
    strength_a = 1 / (1 + 10 ** (-(a.elo - b.elo) / 400))
    extra_time_penalty_mix = 0.58 * strength_a + 0.42 * 0.5
    return p_a + p_draw * extra_time_penalty_mix, p_b + p_draw * (1 - extra_time_penalty_mix)


def confidence(best_probability: float, config: dict) -> str:
    thresholds = config["confidence_thresholds"]
    if best_probability >= thresholds["high"]:
        return "高"
    if best_probability >= thresholds["medium"]:
        return "中"
    return "低"


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def predict_fixture(fixture: Fixture, ratings: dict[str, TeamRating], config: dict) -> dict:
    if fixture.team_a not in ratings or fixture.team_b not in ratings:
        missing = [team for team in [fixture.team_a, fixture.team_b] if team not in ratings]
        raise ValueError(f"缺少球队评分: {', '.join(missing)}")

    team_a = ratings[fixture.team_a]
    team_b = ratings[fixture.team_b]
    lam_a, lam_b = expected_goals(team_a, team_b, fixture, config)
    dist = score_distribution(lam_a, lam_b, int(config["max_goals"]), float(config.get("dixon_coles_rho", -0.10)))
    probs = blend_with_market(
        (dist["p_a"], dist["p_draw"], dist["p_b"]),
        market_probabilities(fixture),
        float(config["market_blend_weight"]),
    )

    is_knockout = fixture.stage in set(config["knockout_stages"])
    adv_a = adv_b = None
    if is_knockout:
        adv_a, adv_b = advancement_probabilities(probs[0], probs[1], probs[2], team_a, team_b)

    labels = [fixture.team_a, "平局", fixture.team_b]
    best_index = max(range(3), key=lambda idx: probs[idx])
    pick = labels[best_index]
    best_probability = probs[best_index]

    if is_knockout and adv_a is not None and adv_b is not None:
        pick = fixture.team_a if adv_a >= adv_b else fixture.team_b
        best_probability = max(adv_a, adv_b)

    return {
        "date": fixture.match_date.isoformat(),
        "kickoff": fixture.kickoff_local,
        "stage": fixture.stage,
        "venue": fixture.venue,
        "match_num": fixture.match_num,
        "match_id": fixture.match_id,
        "team_a": fixture.team_a,
        "team_b": fixture.team_b,
        "xg_a": lam_a,
        "xg_b": lam_b,
        "p_a": probs[0],
        "p_draw": probs[1],
        "p_b": probs[2],
        "adv_a": adv_a,
        "adv_b": adv_b,
        "pick": pick,
        "confidence": confidence(best_probability, config),
        "analysis_source": fixture.analysis_source or "竞彩足球市场",
        "top_scores": dist["top_scores"],
    }


def markdown_report(predictions: list[dict], target_date: date) -> str:
    lines = [f"# {target_date.isoformat()} 竞彩足球预测", ""]
    if not predictions:
        lines.append("当天没有在赛程表中找到比赛。")
        return "\n".join(lines) + "\n"

    for item in predictions:
        lines.extend(
            [
                f"## {item['kickoff']} {item['team_a']} vs {item['team_b']}",
                f"- 阶段：{item['stage']}；地点：{item['venue']}",
                f"- 预期进球：{item['team_a']} {item['xg_a']:.2f}，{item['team_b']} {item['xg_b']:.2f}",
                f"- 90 分钟概率：{item['team_a']} 胜 {percent(item['p_a'])}，平 {percent(item['p_draw'])}，{item['team_b']} 胜 {percent(item['p_b'])}",
            ]
        )
        if item["adv_a"] is not None:
            lines.append(
                f"- 晋级概率：{item['team_a']} {percent(item['adv_a'])}，{item['team_b']} {percent(item['adv_b'])}"
            )
        score_text = "；".join(
            f"{a}-{b} {percent(prob)}" for (a, b), prob in item["top_scores"]
        )
        lines.extend(
            [
                f"- 最可能比分：{score_text}",
                f"- 推荐判断：{item['pick']}，信心：{item['confidence']}",
                "",
            ]
        )
    return "\n".join(lines)


def write_csv(predictions: list[dict], target_date: date) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"predictions_{target_date.isoformat()}.csv"
    fields = [
        "date",
        "kickoff",
        "stage",
        "match_num",
        "match_id",
        "team_a",
        "team_b",
        "xg_a",
        "xg_b",
        "p_a",
        "p_draw",
        "p_b",
        "adv_a",
        "adv_b",
        "pick",
        "confidence",
        "analysis_source",
        "score_1",
        "score_1_prob",
        "score_2",
        "score_2_prob",
        "score_3",
        "score_3_prob",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in predictions:
            row = {field: item.get(field, "") for field in fields}
            for index, ((goals_a, goals_b), probability) in enumerate(item["top_scores"][:3], start=1):
                row[f"score_{index}"] = f"{goals_a}-{goals_b}"
                row[f"score_{index}_prob"] = probability
            writer.writerow(row)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="自动预测指定日期的世界杯比赛。")
    parser.add_argument("--date", default=date.today().isoformat(), help="预测日期，格式 YYYY-MM-DD。")
    parser.add_argument("--no-files", action="store_true", help="只输出到屏幕，不写入 output 文件夹。")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    config = read_config()
    ratings = load_ratings()
    fixtures = [fixture for fixture in load_fixtures() if fixture.match_date == target_date]
    predictions = [predict_fixture(fixture, ratings, config) for fixture in fixtures]
    report = markdown_report(predictions, target_date)

    print(report)
    if not args.no_files:
        OUTPUT_DIR.mkdir(exist_ok=True)
        md_path = OUTPUT_DIR / f"predictions_{target_date.isoformat()}.md"
        md_path.write_text(report, encoding="utf-8")
        csv_path = write_csv(predictions, target_date)
        print(f"已生成: {md_path}")
        print(f"已生成: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
