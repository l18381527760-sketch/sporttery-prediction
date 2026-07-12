import csv
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
API_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
MATCH_LIST_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry"
ODDS_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
ESPN_LEAGUES = {
    "fifa.world": "世界杯",
    "nor.1": "挪超",
    "swe.1": "瑞超",
    "fin.1": "芬超",
}
ZGZCW_HAD_URL = "https://cp.zgzcw.com/lottery/jchtplayvsForJsp.action?lotteryId=47&type=jcmini"
ZGZCW_ODDS_URLS = {
    "crs": "https://cp.zgzcw.com/lottery/jcplayvsForJsp.action?lotteryId=23",
    "ttg": "https://cp.zgzcw.com/lottery/jcplayvsForJsp.action?lotteryId=24",
    "hafu": "https://cp.zgzcw.com/lottery/jcplayvsForJsp.action?lotteryId=25",
}
CRS_KEYS = [
    "s01s00", "s02s00", "s02s01", "s03s00", "s03s01", "s03s02",
    "s04s00", "s04s01", "s04s02", "s05s00", "s05s01", "s05s02", "s-1sh",
    "s00s00", "s01s01", "s02s02", "s03s03", "s-1sd",
    "s00s01", "s00s02", "s01s02", "s00s03", "s01s03", "s02s03",
    "s00s04", "s01s04", "s02s04", "s00s05", "s01s05", "s02s05", "s-1sa",
]
TTG_KEYS = [f"s{index}" for index in range(8)]
HAFU_KEYS = ["hh", "hd", "ha", "dh", "dd", "da", "ah", "ad", "aa"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": "https://www.sporttery.cn/jc/zqsgkj/",
    "Origin": "https://www.sporttery.cn",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_json(url: str, retries: int = 3) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def fetch_text(url: str, retries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


class ZgzcwMatchParser(HTMLParser):
    def __init__(self, target_date: date):
        super().__init__(convert_charrefs=True)
        self.target_date = target_date.isoformat()
        self.current: dict | None = None
        self.matches: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "tr" and values.get("id", "").startswith("tr_"):
            row_date = values.get("t", "")[:10]
            self.current = None
            if row_date == self.target_date:
                self.current = {
                    "matchId": values["id"].removeprefix("tr_"),
                    "matchNumStr": values.get("mn", ""),
                    "leagueNameAbbr": values.get("m", ""),
                    "matchStatus": "ZGZCW",
                    "poolStatus": "",
                    "source": "中国足彩网",
                    "venue": "中国足彩网备用数据",
                    "h": "",
                    "d": "",
                    "a": "",
                }
        elif self.current is not None and tag == "a":
            title = values.get("title", "").strip()
            if title and "homeTeam" not in self.current:
                self.current["homeTeam"] = title
            elif title and "awayTeam" not in self.current:
                self.current["awayTeam"] = title
        elif self.current is not None and tag == "input":
            input_id = values.get("id", "")
            if input_id.startswith("ht_"):
                standard = values.get("value", "").split("|", 1)[0].split()
                if len(standard) == 3:
                    self.current["h"], self.current["d"], self.current["a"] = standard

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self.current is not None:
            if self.current.get("homeTeam") and self.current.get("awayTeam"):
                self.matches.append(self.current)
            self.current = None


class ZgzcwOddsParser(HTMLParser):
    def __init__(self, target_date: date):
        super().__init__(convert_charrefs=True)
        self.target_date = target_date.isoformat()
        self.current_match_id = ""
        self.odds: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "tr" and values.get("id", "").startswith("tr_"):
            self.current_match_id = ""
            if values.get("t", "")[:10] == self.target_date:
                self.current_match_id = values["id"].removeprefix("tr_")
        elif tag == "input" and self.current_match_id:
            if values.get("id") == f"ht_{self.current_match_id}":
                self.odds[self.current_match_id] = values.get("value", "").split()

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr":
            self.current_match_id = ""


def fetch_zgzcw_matches(target_date: date) -> list[dict]:
    parser = ZgzcwMatchParser(target_date)
    parser.feed(fetch_text(ZGZCW_HAD_URL))
    return parser.matches


def fetch_zgzcw_odds(target_date: date) -> dict[str, dict]:
    result: dict[str, dict] = {}
    key_sets = {"crs": CRS_KEYS, "ttg": TTG_KEYS, "hafu": HAFU_KEYS}
    for play, url in ZGZCW_ODDS_URLS.items():
        parser = ZgzcwOddsParser(target_date)
        parser.feed(fetch_text(url))
        keys = key_sets[play]
        for match_id, values in parser.odds.items():
            if len(values) != len(keys):
                print(f"WARNING: {match_id} {play}赔率数量异常：{len(values)}，应为{len(keys)}")
                continue
            result.setdefault(match_id, {})[play] = dict(zip(keys, values))
    return result


def fetch_matches(target_date: date) -> list[dict]:
    params = {
        "matchBeginDate": target_date.isoformat(),
        "matchEndDate": target_date.isoformat(),
        "leagueId": "",
        "pageSize": "100",
        "pageNo": "1",
        "isFix": "0",
        "matchPage": "1",
        "pcOrWap": "1",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)
    if str(payload.get("errorCode")) != "0":
        raise RuntimeError(payload.get("errorMessage", "竞彩网接口返回异常"))
    return payload.get("value", {}).get("matchResult", [])


def fetch_selling_matches(target_date: date) -> list[dict]:
    params = {"clientCode": "3001"}
    url = MATCH_LIST_URL + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)
    if str(payload.get("errorCode")) != "0":
        raise RuntimeError(payload.get("errorMessage", "竞彩网在售接口返回异常"))

    selected = []
    for day in payload.get("value", {}).get("matchInfoList", []):
        if day.get("businessDate") != target_date.isoformat():
            continue
        for item in day.get("subMatchList", []):
            if item.get("matchStatus") in {"Selling", "Define"}:
                selected.append(item)
    return selected


def fetch_espn_matches(target_date: date) -> list[dict]:
    matches: list[dict] = []
    seen: set[str] = set()
    for league_slug, league_label in ESPN_LEAGUES.items():
        params = {"dates": target_date.strftime("%Y%m%d"), "limit": "100"}
        url = ESPN_SCOREBOARD_URL.format(league=league_slug) + "?" + urllib.parse.urlencode(params)
        payload = fetch_json(url, retries=2)
        for event in payload.get("events", []):
            event_id = str(event.get("id", ""))
            if not event_id or event_id in seen:
                continue
            competition = (event.get("competitions") or [{}])[0]
            status = competition.get("status", {}).get("type", {})
            if status.get("completed") or status.get("state") == "post":
                continue
            competitors = competition.get("competitors", [])
            home = next((item for item in competitors if item.get("homeAway") == "home"), None)
            away = next((item for item in competitors if item.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            kickoff = event.get("date") or competition.get("date") or ""
            try:
                kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                kickoff_label = kickoff_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
            except ValueError:
                kickoff_label = kickoff
            venue = competition.get("venue", {}).get("fullName") or "ESPN"
            matches.append(
                {
                    "matchId": f"espn-{event_id}",
                    "matchNumStr": kickoff_label,
                    "leagueNameAbbr": league_label,
                    "homeTeam": home.get("team", {}).get("displayName", ""),
                    "awayTeam": away.get("team", {}).get("displayName", ""),
                    "matchStatus": "ESPN",
                    "poolStatus": "",
                    "source": "ESPN",
                    "venue": venue,
                }
            )
            seen.add(event_id)
    return matches


def latest_odds_record(records: list[dict]) -> dict:
    if not records:
        return {}
    return records[0]


def fetch_odds(match_id: str) -> dict:
    params = {"matchId": match_id, "clientCode": "3001"}
    url = ODDS_URL + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)
    if str(payload.get("errorCode")) != "0":
        return {}
    history = payload.get("value", {}).get("oddsHistory", {})
    return {
        "had": latest_odds_record(history.get("hadList", [])),
        "hhad": latest_odds_record(history.get("hhadList", [])),
        "ttg": latest_odds_record(history.get("ttgList", [])),
        "hafu": latest_odds_record(history.get("hafuList", [])),
        "crs": latest_odds_record(history.get("crsList", [])),
    }


def active_matches(matches: list[dict]) -> list[dict]:
    rows = []
    for item in matches:
        status = str(item.get("matchResultStatus", ""))
        if status == "2":
            continue
        rows.append(item)
    return rows


def implied_home_edge(home_odds: str, away_odds: str) -> float:
    try:
        home = 1 / float(home_odds)
        away = 1 / float(away_odds)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    total = home + away
    if total <= 0:
        return 0.0
    return max(-0.18, min(0.18, (home - away) / total * 0.28))


def team_home(item: dict) -> str:
    return item.get("homeTeam") or item.get("homeTeamAbbName") or item.get("homeTeamAllName") or ""


def team_away(item: dict) -> str:
    return item.get("awayTeam") or item.get("awayTeamAbbName") or item.get("awayTeamAllName") or ""


def league_name(item: dict) -> str:
    return item.get("leagueNameAbbr") or item.get("leagueAbbName") or item.get("leagueName") or item.get("leagueAllName") or ""


def match_number(item: dict) -> str:
    return item.get("matchNumStr") or item.get("matchNum") or ""


def attach_had_odds(matches: list[dict], odds_by_id: dict[str, dict]) -> list[dict]:
    enriched = []
    for item in matches:
        row = dict(item)
        had = odds_by_id.get(str(item.get("matchId", "")), {}).get("had", {})
        row["h"] = row.get("h") or had.get("h", "")
        row["d"] = row.get("d") or had.get("d", "")
        row["a"] = row.get("a") or had.get("a", "")
        enriched.append(row)
    return enriched


def write_fixtures(matches: list[dict], target_date: date) -> Path:
    path = DATA_DIR / "fixtures.csv"
    fields = [
        "date",
        "kickoff_local",
        "stage",
        "team_a",
        "team_b",
        "neutral",
        "venue",
        "odds_a",
        "odds_draw",
        "odds_b",
        "match_num",
        "match_id",
        "pool_status",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in matches:
            writer.writerow(
                {
                    "date": target_date.isoformat(),
                    "kickoff_local": match_number(item),
                    "stage": league_name(item),
                    "team_a": team_home(item),
                    "team_b": team_away(item),
                    "neutral": "false",
                    "venue": item.get("venue") or ("ESPN备用数据" if item.get("source") == "ESPN" else "竞彩网"),
                    "odds_a": item.get("h", ""),
                    "odds_draw": item.get("d", ""),
                    "odds_b": item.get("a", ""),
                    "match_num": match_number(item),
                    "match_id": item.get("matchId", ""),
                    "pool_status": item.get("poolStatus", item.get("matchStatus", "")),
                }
            )
    return path


def load_ratings() -> dict[str, dict]:
    path = DATA_DIR / "team_ratings.csv"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return {row["team"]: row for row in csv.DictReader(fh)}


def write_ratings(matches: list[dict]) -> Path:
    path = DATA_DIR / "team_ratings.csv"
    ratings = load_ratings()
    for item in matches:
        edge = implied_home_edge(item.get("h", ""), item.get("a", ""))
        home = team_home(item)
        away = team_away(item)
        if home and home not in ratings:
            ratings[home] = {
                "team": home,
                "elo": str(round(1850 + edge * 650)),
                "attack": f"{edge:.3f}",
                "defense": "0.000",
                "form": "0.000",
                "injury": "0.000",
                "rest_days": "4",
                "home_adv": "0.080",
            }
        if away and away not in ratings:
            ratings[away] = {
                "team": away,
                "elo": str(round(1850 - edge * 650)),
                "attack": f"{-edge:.3f}",
                "defense": "0.000",
                "form": "0.000",
                "injury": "0.000",
                "rest_days": "4",
                "home_adv": "0.000",
            }
    fields = ["team", "elo", "attack", "defense", "form", "injury", "rest_days", "home_adv"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for key in sorted(ratings):
            writer.writerow(ratings[key])
    return path


def write_odds(matches: list[dict], target_date: date) -> Path:
    path = DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json"
    odds = {}
    for item in matches:
        match_id = str(item.get("matchId", ""))
        if match_id:
            odds[match_id] = fetch_odds(match_id)
    path.write_text(json.dumps(odds, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def collect_odds(matches: list[dict]) -> dict[str, dict]:
    odds = {}
    for item in matches:
        match_id = str(item.get("matchId", ""))
        if item.get("source") == "中国足彩网":
            extra = item.get("zgzcw_odds", {})
            odds[match_id] = {
                "had": {"h": item.get("h", ""), "d": item.get("d", ""), "a": item.get("a", "")},
                "hhad": {},
                "ttg": extra.get("ttg", {}),
                "hafu": extra.get("hafu", {}),
                "crs": extra.get("crs", {}),
            }
        elif match_id and not match_id.startswith("espn-"):
            odds[match_id] = fetch_odds(match_id)
    return odds


def write_odds_data(odds: dict[str, dict], target_date: date) -> Path:
    path = DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json"
    path.write_text(json.dumps(odds, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_source_status(source: str, target_date: date, message: str = "") -> Path:
    path = DATA_DIR / "source_status.json"
    payload = {
        "source": source,
        "target_date": target_date.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fallback": source != "竞彩网",
        "message": message,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从竞彩网官方接口导入当天竞彩足球比赛。")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--include-finished", action="store_true", help="包含已开奖比赛，默认排除。")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    source = "竞彩网"
    source_message = ""
    try:
        selected = fetch_selling_matches(target_date)
    except Exception as exc:
        source = "中国足彩网"
        source_message = f"竞彩网云端接口暂时不可用（{type(exc).__name__}），已切换中国足彩网竞彩页面。"
        print(f"WARNING: {source_message}")
        try:
            selected = fetch_zgzcw_matches(target_date)
            if not selected:
                raise RuntimeError("中国足彩网当天没有可解析的竞彩比赛")
            fallback_odds = fetch_zgzcw_odds(target_date)
            for item in selected:
                item["zgzcw_odds"] = fallback_odds.get(str(item.get("matchId", "")), {})
        except Exception as fallback_exc:
            source = "ESPN"
            source_message += f" 中国足彩网也不可用（{type(fallback_exc).__name__}），已切换 ESPN。"
            print(f"WARNING: {source_message}")
            selected = fetch_espn_matches(target_date)
    matches = selected
    if args.include_finished:
        matches = fetch_matches(target_date)
        selected = matches
    odds_data = collect_odds(selected)
    selected = attach_had_odds(selected, odds_data)
    fixtures_path = write_fixtures(selected, target_date)
    ratings_path = write_ratings(selected)
    odds_path = write_odds_data(odds_data, target_date)
    status_path = write_source_status(source, target_date, source_message)
    print(f"竞彩网返回比赛: {len(matches)}")
    print(f"导入未开奖比赛: {len(selected)}")
    for item in selected:
        print(
            f"{match_number(item)} {league_name(item)} "
            f"{team_home(item)} vs {team_away(item)} "
            f"状态={item.get('matchStatus', item.get('matchResultStatus'))} 奖池={item.get('poolStatus', '')}"
        )
    print(f"Updated: {fixtures_path}")
    print(f"Updated: {ratings_path}")
    print(f"Updated: {odds_path}")
    print(f"Data source: {source}")
    print(f"Updated: {status_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
