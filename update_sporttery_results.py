import csv
import urllib.parse
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from import_sporttery import ZGZCW_HAD_URL, fetch_matches, fetch_text


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def parse_score(value: str) -> tuple[str, str] | None:
    value = (value or "").strip()
    if ":" not in value:
        return None
    left, right = value.split(":", 1)
    if not left.strip().isdigit() or not right.strip().isdigit():
        return None
    return left.strip(), right.strip()


def read_existing(path: Path) -> dict[tuple[str, str, str], dict]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return {(row["date"], row["team_a"], row["team_b"]): row for row in csv.DictReader(fh)}


class ZgzcwResultParser(HTMLParser):
    """Parse finished scores from the historical JCZQ issue page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current: dict | None = None
        self.current_cell = ""
        self.capture_team = False
        self.results: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "tr" and values.get("id", "").startswith("tr_"):
            classes = values.get("class", "").split()
            self.current = {"homeTeam": "", "awayTeam": "", "score": ""} if "endBet" in classes else None
            self.current_cell = ""
        elif self.current is not None and tag == "td":
            classes = values.get("class", "").split()
            if "wh-4" in classes:
                self.current_cell = "home"
            elif "wh-5" in classes and "bf" in classes:
                self.current_cell = "score"
            elif "wh-6" in classes:
                self.current_cell = "away"
            else:
                self.current_cell = ""
        elif self.current is not None and tag == "a":
            self.capture_team = self.current_cell in {"home", "away"} and "soccer/team" in values.get("href", "")

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        text = data.strip()
        if not text:
            return
        if self.current_cell == "score" and parse_score(text):
            self.current["score"] = text
        elif self.capture_team and self.current_cell == "home":
            self.current["homeTeam"] += text
        elif self.capture_team and self.current_cell == "away":
            self.current["awayTeam"] += text

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.capture_team = False
        elif tag == "td":
            self.current_cell = ""
        elif tag == "tr" and self.current is not None:
            if self.current["homeTeam"] and self.current["awayTeam"] and parse_score(self.current["score"]):
                self.results.append(self.current)
            self.current = None


def fetch_zgzcw_results(target_date: date) -> list[dict]:
    query = urllib.parse.urlencode({"issue": target_date.isoformat()})
    parser = ZgzcwResultParser()
    parser.feed(fetch_text(f"{ZGZCW_HAD_URL}&{query}"))
    return parser.results


def official_result_rows(target_date: date) -> list[dict]:
    results = []
    for item in fetch_matches(target_date):
        if str(item.get("matchResultStatus", "")) != "2":
            continue
        full = parse_score(item.get("sectionsNo999", ""))
        half = parse_score(item.get("sectionsNo1", ""))
        if full is None:
            continue
        results.append(
            {
                "homeTeam": item.get("homeTeam", ""),
                "awayTeam": item.get("awayTeam", ""),
                "full": full,
                "half": half,
            }
        )
    return results


def update_results(target_date: date) -> Path:
    path = DATA_DIR / "bet_results.csv"
    rows = read_existing(path)
    source = "竞彩网"
    try:
        result_rows = official_result_rows(target_date)
        if not result_rows:
            raise RuntimeError("竞彩网暂未返回已完场赛果")
    except Exception as exc:
        source = "中国足彩网"
        print(f"WARNING: 竞彩网赛果接口不可用（{type(exc).__name__}），切换中国足彩网历史赛果。")
        result_rows = []
        for item in fetch_zgzcw_results(target_date):
            full = parse_score(item["score"])
            if full is not None:
                result_rows.append({**item, "full": full, "half": None})

    if not result_rows:
        raise RuntimeError(f"{target_date.isoformat()} 暂未抓到任何已完场赛果，稍后自动重试")

    updated = 0
    for item in result_rows:
        key = (target_date.isoformat(), item["homeTeam"], item["awayTeam"])
        existing = rows.get(key, {})
        half = item.get("half")
        rows[key] = {
            "date": key[0],
            "team_a": key[1],
            "team_b": key[2],
            "home_goals": item["full"][0],
            "away_goals": item["full"][1],
            "half_home_goals": half[0] if half else existing.get("half_home_goals", ""),
            "half_away_goals": half[1] if half else existing.get("half_away_goals", ""),
        }
        updated += 1

    fields = ["date", "team_a", "team_b", "home_goals", "away_goals", "half_home_goals", "half_away_goals"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow(rows[key])
    print(f"Data source: {source}; finished matches: {updated}")
    return path


def main() -> int:
    import argparse

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    parser = argparse.ArgumentParser(description="抓取已完场竞彩足球赛果并更新结算数据。")
    parser.add_argument("--date", default=yesterday)
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    path = update_results(target_date)
    print(f"Updated results: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
