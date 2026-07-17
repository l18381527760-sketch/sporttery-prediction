import csv
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from import_sporttery import ZGZCW_HAD_URL, fetch_matches, fetch_text


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BEIJING = timezone(timedelta(hours=8))
TEAM_ALIASES = {
    # The official feed and ZGZCW use different Chinese abbreviations.
    "奥尔格里": "厄格里特",
}
HISTORICAL_HALF_TIME = {
    # One legacy half/full-time plan predates the draw-only strategy.
    ("2026-07-11", "阿根廷", "瑞士"): ("1", "0"),
}
BASE_FIELDS = (
    "date", "team_a", "team_b", "home_goals", "away_goals",
    "half_home_goals", "half_away_goals", "match_id", "result_status",
    "result_source", "source_record_id", "captured_at_bjt",
)


def parse_score(value: str) -> tuple[str, str] | None:
    value = (value or "").strip()
    if ":" not in value:
        return None
    left, right = value.split(":", 1)
    if not left.strip().isdigit() or not right.strip().isdigit():
        return None
    return left.strip(), right.strip()


def read_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class ZgzcwResultParser(HTMLParser):
    """Parse finished scores and their source row identity from ZGZCW."""

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
            self.current = (
                {
                    "homeTeam": "",
                    "awayTeam": "",
                    "score": "",
                    "source_record_id": values["id"].removeprefix("tr_"),
                }
                if "endBet" in classes
                else None
            )
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
    captured_at = datetime.now(BEIJING).isoformat()
    rows = []
    for item in fetch_matches(target_date):
        if str(item.get("matchResultStatus", "")) != "2":
            continue
        full = parse_score(item.get("sectionsNo999", ""))
        if full is None:
            continue
        match_id = str(item.get("matchId", "")).strip()
        if not match_id:
            continue
        rows.append({
            "homeTeam": item.get("homeTeam", ""),
            "awayTeam": item.get("awayTeam", ""),
            "full": full,
            "half": parse_score(item.get("sectionsNo1", "")),
            "match_id": match_id,
            "result_status": "finished",
            "result_source": "sporttery",
            "source_record_id": match_id,
            "captured_at_bjt": captured_at,
        })
    return rows


def update_results(target_date: date) -> Path:
    path = DATA_DIR / "bet_results.csv"
    rows = read_existing(path)
    source = "sporttery"
    try:
        result_rows = official_result_rows(target_date)
        if not result_rows:
            raise RuntimeError("Sporttery returned no explicit finished results")
    except Exception as exc:
        source = "zgzcw"
        print(f"WARNING: 竞彩网赛果接口不可用（{type(exc).__name__}），切换中国足彩网历史赛果。")
        result_rows = [_fallback_result_row(item) for item in fetch_zgzcw_results(target_date) if parse_score(item.get("score", ""))]

    if not result_rows:
        raise RuntimeError(f"{target_date.isoformat()} 暂未抓到任何已完场赛果，稍后自动重试")

    fixture_ids = _fixture_match_ids(target_date)
    row_indexes = _index_rows(rows)
    updated = 0
    for item in result_rows:
        home_team = TEAM_ALIASES.get(item.get("homeTeam", ""), item.get("homeTeam", ""))
        away_team = TEAM_ALIASES.get(item.get("awayTeam", ""), item.get("awayTeam", ""))
        key = (target_date.isoformat(), home_team, away_team)
        row_index = _select_row_index(rows, row_indexes.get(key, []), item.get("match_id"))
        existing = rows[row_index] if row_index is not None else {}
        full = item.get("full")
        if not full:
            continue
        half = item.get("half") or HISTORICAL_HALF_TIME.get(key)
        incoming = dict(existing)
        incoming.update({
            "date": key[0],
            "team_a": key[1],
            "team_b": key[2],
            "half_home_goals": half[0] if half else existing.get("half_home_goals", ""),
            "half_away_goals": half[1] if half else existing.get("half_away_goals", ""),
        })
        match_id = item.get("match_id") or existing.get("match_id") or fixture_ids.get(key)
        if match_id:
            incoming["match_id"] = str(match_id)
            incoming.update(_result_provenance(item, "finished"))
        else:
            incoming["match_id"] = ""
            incoming.update(_result_provenance(item, "unavailable"))

        prior_protected = existing.get("result_status") in {"finished", "conflict"} and parse_score(
            f"{existing.get('home_goals', '')}:{existing.get('away_goals', '')}"
        )
        conflict = existing.get("result_status") == "conflict" or (
            prior_protected and tuple(full) != (existing.get("home_goals"), existing.get("away_goals"))
        )
        if conflict:
            incoming["home_goals"] = existing["home_goals"]
            incoming["away_goals"] = existing["away_goals"]
            incoming["result_status"] = "conflict"
            incoming["result_source"] = _joined_provenance(existing.get("result_source", ""), incoming["result_source"])
            incoming["source_record_id"] = _joined_provenance(existing.get("source_record_id", ""), incoming["source_record_id"])
            incoming["captured_at_bjt"] = _joined_provenance(existing.get("captured_at_bjt", ""), incoming["captured_at_bjt"])
        else:
            incoming["home_goals"] = full[0]
            incoming["away_goals"] = full[1]
        if row_index is None:
            rows.append(incoming)
            row_index = len(rows) - 1
            row_indexes.setdefault(key, []).append(row_index)
        else:
            rows[row_index] = incoming
        updated += 1

    _write_rows(path, rows)
    print(f"Data source: {source}; finished matches: {updated}")
    return path


def _fallback_result_row(item: dict) -> dict:
    full = parse_score(item.get("score", ""))
    return {
        **item,
        "full": full,
        "half": None,
        "result_source": "zgzcw",
        "source_record_id": str(item.get("source_record_id", "")).strip(),
        "captured_at_bjt": datetime.now(BEIJING).isoformat(),
    }


def _fixture_match_ids(target_date: date) -> dict[tuple[str, str, str], str]:
    path = DATA_DIR / "fixtures.csv"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {
                (row.get("date", ""), row.get("team_a", ""), row.get("team_b", "")): row["match_id"].strip()
                for row in csv.DictReader(handle)
                if row.get("date") == target_date.isoformat() and row.get("match_id", "").strip()
            }
    except (OSError, csv.Error):
        return {}


def _result_provenance(item: dict, status: str) -> dict:
    source = item.get("result_source", "sporttery")
    return {
        "result_status": status,
        "result_source": source,
        "source_record_id": item.get("source_record_id", "") or item.get("match_id", ""),
        "captured_at_bjt": item.get("captured_at_bjt", datetime.now(BEIJING).isoformat()),
    }


def _joined_provenance(first: str, second: str) -> str:
    tokens = {
        token.strip()
        for value in (first, second)
        if isinstance(value, str)
        for token in value.split("|")
        if token.strip()
    }
    return "|".join(sorted(tokens))


def _index_rows(rows: list[dict]) -> dict[tuple[str, str, str], list[int]]:
    indexes: dict[tuple[str, str, str], list[int]] = {}
    for index, row in enumerate(rows):
        key = (row.get("date", ""), row.get("team_a", ""), row.get("team_b", ""))
        indexes.setdefault(key, []).append(index)
    return indexes


def _select_row_index(rows: list[dict], candidates: list[int], match_id: object) -> int | None:
    canonical_match_id = str(match_id).strip() if match_id not in (None, "") else ""
    if canonical_match_id:
        for index in candidates:
            if rows[index].get("match_id", "").strip() == canonical_match_id:
                return index
    return candidates[0] if candidates else None


def _write_rows(path: Path, rows: list[dict]) -> None:
    unknown = sorted({field for row in rows for field in row} - set(BASE_FIELDS))
    fields = [*BASE_FIELDS, *unknown]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


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
