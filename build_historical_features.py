import csv
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load_results() -> list[dict]:
    path = DATA_DIR / "bet_results.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("home_goals") != "" and row.get("away_goals") != ""]


def build_features() -> Path:
    rows = sorted(load_results(), key=lambda row: row["date"])
    appearances: dict[str, list[dict]] = defaultdict(list)
    total_goals = 0
    for row in rows:
        home_goals = int(row["home_goals"])
        away_goals = int(row["away_goals"])
        total_goals += home_goals + away_goals
        appearances[row["team_a"]].append({"date": row["date"], "gf": home_goals, "ga": away_goals, "points": 3 if home_goals > away_goals else 1 if home_goals == away_goals else 0})
        appearances[row["team_b"]].append({"date": row["date"], "gf": away_goals, "ga": home_goals, "points": 3 if away_goals > home_goals else 1 if away_goals == home_goals else 0})

    league_goals = total_goals / max(1, len(rows) * 2)
    output = DATA_DIR / "team_history_features.csv"
    fields = ["team", "matches", "attack", "defense", "form", "rest_days", "last_date"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for team in sorted(appearances):
            recent = appearances[team][-12:]
            weights = [0.88 ** (len(recent) - index - 1) for index in range(len(recent))]
            weight_sum = sum(weights)
            goals_for = sum(item["gf"] * weight for item, weight in zip(recent, weights)) / weight_sum
            goals_against = sum(item["ga"] * weight for item, weight in zip(recent, weights)) / weight_sum
            points = sum(item["points"] * weight for item, weight in zip(recent, weights)) / weight_sum
            shrink = min(1.0, len(recent) / 6.0)
            attack = clamp(math.log((goals_for + 0.35) / (league_goals + 0.35)) * shrink, -0.35, 0.35)
            defense = clamp(math.log((league_goals + 0.35) / (goals_against + 0.35)) * shrink, -0.35, 0.35)
            form = clamp((points / 3.0 - 0.5) * 0.5 * shrink, -0.20, 0.20)
            last_date = datetime.strptime(recent[-1]["date"], "%Y-%m-%d").date()
            writer.writerow({
                "team": team,
                "matches": len(recent),
                "attack": f"{attack:.4f}",
                "defense": f"{defense:.4f}",
                "form": f"{form:.4f}",
                "rest_days": max(1, min(14, (date.today() - last_date).days)),
                "last_date": last_date.isoformat(),
            })
    print(f"Updated historical features: {output}")
    return output


if __name__ == "__main__":
    build_features()
