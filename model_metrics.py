import csv
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"


def play_family(play: str) -> str:
    if "串" in play:
        return "胜平负串关" if "胜平负" in play else "其他串关"
    return play


def summarize(rows: list[dict]) -> dict:
    settled = [row for row in rows if row.get("status") in {"命中", "未中"}]
    by_play: dict[str, list[dict]] = {}
    for row in settled:
        by_play.setdefault(play_family(row.get("play", "")), []).append(row)

    def metrics(items: list[dict]) -> dict:
        if not items:
            return {"count": 0, "hits": 0, "hit_rate": None, "brier": None, "log_loss": None, "stake": 0.0, "profit": 0.0, "roi": None, "average_expected_return": None}
        probabilities = [max(0.001, min(0.999, float(row.get("probability") or 0.5))) for row in items]
        outcomes = [1.0 if row.get("status") == "命中" else 0.0 for row in items]
        stake = sum(float(row.get("stake") or 0) for row in items)
        profit = sum(float(row.get("profit") or 0) for row in items)
        expected = [probability * float(row.get("odds") or 0) for probability, row in zip(probabilities, items)]
        return {
            "count": len(items),
            "hits": int(sum(outcomes)),
            "hit_rate": sum(outcomes) / len(items),
            "brier": sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) / len(items),
            "log_loss": -sum(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability) for probability, outcome in zip(probabilities, outcomes)) / len(items),
            "stake": round(stake, 2),
            "profit": round(profit, 2),
            "roi": profit / stake if stake else None,
            "average_expected_return": sum(expected) / len(expected),
        }

    return {"overall": metrics(settled), "by_play": {key: metrics(items) for key, items in by_play.items()}}


def write_metrics() -> Path:
    ledger = OUTPUT_DIR / "betting_ledger.csv"
    rows = []
    if ledger.exists():
        with ledger.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    payload = summarize(rows)
    output = OUTPUT_DIR / "model_metrics.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Updated model metrics: {output}")
    return output


if __name__ == "__main__":
    write_metrics()
