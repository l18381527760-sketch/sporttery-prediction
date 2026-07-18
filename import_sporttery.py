import csv
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BEIJING = timezone(timedelta(hours=8))
IMPORT_MANIFEST_SCHEMA_VERSION = 1
APPROVED_IMPORT_SOURCES = frozenset({"sporttery", "zgzcw"})
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
SINGLE_ELIGIBILITY_KEYS = {
    "had": "isSingleHad",
    "hhad": "isSingleHhad",
    "ttg": "isSingleTtg",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": "https://www.sporttery.cn/jc/zqsgkj/",
    "Origin": "https://www.sporttery.cn",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def import_manifest_path(root: Path, target_date: date) -> Path:
    return Path(root) / "data" / "import_manifests" / f"{target_date.isoformat()}.json"


def import_extract_paths(root: Path, target_date: date) -> tuple[Path, Path]:
    directory = Path(root) / "data" / "import_extracts" / target_date.isoformat()
    return directory / "fixtures.csv", directory / "odds.json"


def write_import_manifest(
    source: str,
    target_date: date,
    fixtures_path: Path,
    odds_path: Path,
    imported_at: datetime | None = None,
) -> Path:
    canonical_source = str(source).strip().lower()
    if canonical_source not in APPROVED_IMPORT_SOURCES:
        raise ValueError("import manifest source is not approved")
    imported = _aware_import_datetime(imported_at or datetime.now(BEIJING))
    root = DATA_DIR.resolve().parent
    fixtures = Path(fixtures_path).resolve()
    odds = Path(odds_path).resolve()
    for input_path in (fixtures, odds):
        try:
            input_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("import manifest input path is invalid") from exc
        if not input_path.is_file():
            raise ValueError("import manifest input is missing")
    return publish_import_manifest(
        canonical_source,
        target_date,
        fixtures.read_bytes(),
        odds.read_bytes(),
        imported,
    )


def publish_import_manifest(
    source: str,
    target_date: date,
    fixture_bytes: bytes,
    odds_bytes: bytes,
    imported_at: datetime | None = None,
) -> Path:
    canonical_source = str(source).strip().lower()
    if canonical_source not in APPROVED_IMPORT_SOURCES:
        raise ValueError("import manifest source is not approved")
    if not isinstance(fixture_bytes, bytes) or not isinstance(odds_bytes, bytes):
        raise ValueError("import manifest extracts must be bytes")
    imported = _aware_import_datetime(imported_at or datetime.now(BEIJING))
    root = DATA_DIR.resolve().parent
    path = import_manifest_path(root, target_date)
    if path.exists():
        existing = read_valid_import_manifest(root, target_date)
        _require_matching_import(
            existing, canonical_source, fixture_bytes, odds_bytes
        )
        return path

    fixtures, odds = import_extract_paths(root, target_date)
    _publish_immutable_bytes(fixtures, fixture_bytes)
    _publish_immutable_bytes(odds, odds_bytes)
    payload = {
        "schema_version": IMPORT_MANIFEST_SCHEMA_VERSION,
        "target_date": target_date.isoformat(),
        "source": canonical_source,
        "imported_at_bjt": imported.astimezone(BEIJING).isoformat(),
        "fixtures": _manifest_file_record(root, fixtures),
        "odds": _manifest_file_record(root, odds),
    }
    if path.exists() or not _atomic_publish_manifest(path, payload):
        existing = read_valid_import_manifest(root, target_date)
        _require_matching_import(existing, canonical_source, fixture_bytes, odds_bytes)
    return path


def _require_matching_import(
    existing: dict,
    source: str,
    fixture_bytes: bytes,
    odds_bytes: bytes,
) -> None:
    expected = {
        "fixtures": fixture_bytes,
        "odds": odds_bytes,
    }
    if existing.get("source") != source:
        raise ValueError("existing conflicting import manifest")
    for key, content in expected.items():
        record = existing.get(key, {})
        if (
            record.get("bytes") != len(content)
            or record.get("sha256") != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("existing conflicting import manifest")


def read_valid_import_manifest(root: Path, target_date: date) -> dict:
    root = Path(root).resolve()
    path = import_manifest_path(root, target_date)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("import manifest is missing or invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "schema_version", "target_date", "source", "imported_at_bjt",
            "fixtures", "odds",
        }
        or payload.get("schema_version") != IMPORT_MANIFEST_SCHEMA_VERSION
        or payload.get("target_date") != target_date.isoformat()
        or payload.get("source") not in APPROVED_IMPORT_SOURCES
    ):
        raise ValueError("import manifest contract is invalid")
    _aware_import_datetime(payload.get("imported_at_bjt"))
    expected = {
        "fixtures": f"data/import_extracts/{target_date.isoformat()}/fixtures.csv",
        "odds": f"data/import_extracts/{target_date.isoformat()}/odds.json",
    }
    for key, relative in expected.items():
        record = payload.get(key)
        if not isinstance(record, dict) or record.get("path") != relative:
            raise ValueError(f"import manifest {key} path is invalid")
        _verify_manifest_file_record(root, record)
    return payload


def _manifest_file_record(root: Path, path: Path) -> dict:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("import manifest path escapes repository root") from exc
    if not path.is_file():
        raise ValueError(f"import manifest input is missing: {relative}")
    payload = path.read_bytes()
    return {
        "path": relative,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _verify_manifest_file_record(root: Path, record: dict) -> None:
    relative = record.get("path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("import manifest file record is invalid")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("import manifest path escapes repository root") from exc
    expected_hash = record.get("sha256")
    expected_bytes = record.get("bytes")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
        or not path.is_file()
    ):
        raise ValueError("import manifest file record is invalid")
    payload = path.read_bytes()
    if len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError(f"import manifest hash mismatch: {relative}")


def _atomic_publish_manifest(path: Path, payload: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _publish_immutable_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"existing conflicting import extract: {path.name}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError(f"existing conflicting import extract: {path.name}")
    finally:
        temporary.unlink(missing_ok=True)


def _aware_import_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("import manifest timestamp must be ISO-8601") from exc
    else:
        raise ValueError("import manifest timestamp must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("import manifest timestamp must include a timezone")
    return parsed


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
        self.current_cell = ""
        self.capture_team = False
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
                    "market_h": "",
                    "market_d": "",
                    "market_a": "",
                    "isSingleHad": values.get("dg") == "1",
                }
        elif self.current is not None and tag == "td":
            classes = values.get("class", "").split()
            if "wh-4" in classes:
                self.current_cell = "homeTeam"
            elif "wh-6" in classes:
                self.current_cell = "awayTeam"
            else:
                self.current_cell = ""
        elif self.current is not None and tag == "a":
            title = values.get("title", "").strip()
            if title and "homeTeam" not in self.current:
                self.current["homeTeam"] = title
            elif title and "awayTeam" not in self.current:
                self.current["awayTeam"] = title
            self.capture_team = self.current_cell in {"homeTeam", "awayTeam"} and "soccer/team" in values.get("href", "")
        elif self.current is not None and tag == "span":
            title = values.get("title", "")
            if title.startswith("比赛时间:"):
                self.current["kickoff_at"] = title.removeprefix("比赛时间:").strip()
        elif self.current is not None and tag == "input":
            input_id = values.get("id", "")
            if input_id.startswith("ht_"):
                standard = values.get("value", "").split("|", 1)[0].split()
                if len(standard) == 3:
                    self.current["h"], self.current["d"], self.current["a"] = standard
            elif input_id.startswith("esp_"):
                professional = values.get("value", "").split()
                if len(professional) == 3:
                    self.current["market_h"], self.current["market_d"], self.current["market_a"] = professional

    def handle_data(self, data: str) -> None:
        if self.current is None or not self.capture_team:
            return
        text = data.strip()
        if text and not self.current.get(self.current_cell):
            self.current[self.current_cell] = text

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.capture_team = False
        elif tag == "td":
            self.current_cell = ""
        elif tag == "tr" and self.current is not None:
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
    if not isinstance(payload, dict):
        raise RuntimeError("invalid Sporttery match-list response: root must be an object")
    if str(payload.get("errorCode")) != "0":
        raise RuntimeError(payload.get("errorMessage", "竞彩网在售接口返回异常"))

    value = payload.get("value")
    if not isinstance(value, dict):
        raise RuntimeError("invalid Sporttery match-list response: value must be an object")
    match_days = value.get("matchInfoList")
    if not isinstance(match_days, list):
        raise RuntimeError(
            "invalid Sporttery match-list response: value.matchInfoList must be a list"
        )

    selected = []
    target_day_seen = False
    for day in match_days:
        if not isinstance(day, dict):
            raise RuntimeError(
                "invalid Sporttery match-list response: match day must be an object"
            )
        business_date = day.get("businessDate")
        try:
            parsed_business_date = date.fromisoformat(business_date)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "invalid Sporttery match-list response: businessDate must be YYYY-MM-DD"
            ) from exc
        if business_date != parsed_business_date.isoformat():
            raise RuntimeError(
                "invalid Sporttery match-list response: businessDate must be YYYY-MM-DD"
            )

        sub_matches = day.get("subMatchList")
        if not isinstance(sub_matches, list):
            raise RuntimeError(
                "invalid Sporttery match-list response: subMatchList must be a list"
            )
        if business_date == target_date.isoformat():
            target_day_seen = True
        for item in sub_matches:
            if not isinstance(item, dict):
                raise RuntimeError(
                    "invalid Sporttery match-list response: match must be an object"
                )
            if business_date != target_date.isoformat():
                continue
            match_id = item.get("matchId")
            if not (
                (isinstance(match_id, str) and bool(match_id.strip()))
                or type(match_id) is int
            ):
                raise RuntimeError(
                    "invalid Sporttery match-list response: target-day match matchId is required"
                )
            for field in ("homeTeam", "awayTeam", "matchStatus"):
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise RuntimeError(
                        f"invalid Sporttery match-list response: target-day match {field} is required"
                    )
            if item["matchStatus"] not in {"Selling", "Define"}:
                raise RuntimeError(
                    "invalid Sporttery match-list response: target-day match matchStatus must be Selling or Define"
                )
            selected.append(item)
    if match_days and not target_day_seen:
        raise RuntimeError(
            "invalid Sporttery match-list response: target date is missing"
        )
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


def is_single_eligible(value) -> bool:
    return value is True or (
        isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}
    )


def single_eligibility(item: dict) -> dict[str, bool]:
    return {
        market: is_single_eligible(item.get(key))
        for market, key in SINGLE_ELIGIBILITY_KEYS.items()
    }


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


def attach_professional_market(matches: list[dict], market_matches: list[dict]) -> list[dict]:
    """Attach European bookmaker consensus odds without replacing JC odds."""
    by_number = {match_number(item): item for item in market_matches if match_number(item)}
    by_teams = {(team_home(item), team_away(item)): item for item in market_matches}
    enriched = []
    for item in matches:
        row = dict(item)
        market = by_number.get(match_number(item)) or by_teams.get((team_home(item), team_away(item))) or {}
        row["market_h"] = market.get("market_h", row.get("market_h", ""))
        row["market_d"] = market.get("market_d", row.get("market_d", ""))
        row["market_a"] = market.get("market_a", row.get("market_a", ""))
        row["analysis_source"] = "中国足彩网专业欧赔市场" if all(row.get(key) for key in ("market_h", "market_d", "market_a")) else "竞彩足球市场"
        for _, key in SINGLE_ELIGIBILITY_KEYS.items():
            value = row[key] if key in row else market.get(key)
            row[key] = is_single_eligible(value)
        enriched.append(row)
    return enriched


def write_fixtures(
    matches: list[dict], target_date: date, *, path: Path | None = None
) -> Path:
    path = path or DATA_DIR / "fixtures.csv"
    fields = [
        "date",
        "kickoff_local",
        "kickoff_at",
        "stage",
        "team_a",
        "team_b",
        "neutral",
        "venue",
        "odds_a",
        "odds_draw",
        "odds_b",
        "market_odds_a",
        "market_odds_draw",
        "market_odds_b",
        "analysis_source",
        "is_single_had",
        "is_single_hhad",
        "is_single_ttg",
        "match_num",
        "match_id",
        "pool_status",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in matches:
            eligibility = single_eligibility(item)
            writer.writerow(
                {
                    "date": target_date.isoformat(),
                    "kickoff_local": match_number(item),
                    "kickoff_at": item.get("kickoff_at", ""),
                    "stage": league_name(item),
                    "team_a": team_home(item),
                    "team_b": team_away(item),
                    "neutral": "false",
                    "venue": item.get("venue") or ("ESPN备用数据" if item.get("source") == "ESPN" else "竞彩网"),
                    "odds_a": item.get("h", ""),
                    "odds_draw": item.get("d", ""),
                    "odds_b": item.get("a", ""),
                    "market_odds_a": item.get("market_h", ""),
                    "market_odds_draw": item.get("market_d", ""),
                    "market_odds_b": item.get("market_a", ""),
                    "analysis_source": item.get("analysis_source", "竞彩足球市场"),
                    "is_single_had": "true" if eligibility["had"] else "false",
                    "is_single_hhad": "true" if eligibility["hhad"] else "false",
                    "is_single_ttg": "true" if eligibility["ttg"] else "false",
                    "match_num": match_number(item),
                    "match_id": item.get("matchId", ""),
                    "pool_status": item.get("poolStatus", item.get("matchStatus", "")),
                }
            )
    return path


def count_written_fixtures(path: Path, target_date: date) -> int:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "date" not in reader.fieldnames:
                raise ValueError("fixtures CSV is missing the date header")
            return sum(1 for row in reader if row.get("date") == target_date.isoformat())
    except (OSError, csv.Error) as exc:
        raise ValueError("could not verify written fixture count") from exc


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
        edge = implied_home_edge(item.get("market_h") or item.get("h", ""), item.get("market_a") or item.get("a", ""))
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


def write_odds_data(
    odds: dict[str, dict], target_date: date, *, path: Path | None = None
) -> Path:
    path = path or DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json"
    path.write_text(json.dumps(odds, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_source_status(
    source: str,
    target_date: date,
    message: str = "",
    analysis_source: str = "专业欧赔市场",
    *,
    fixture_count: int,
) -> Path:
    if type(fixture_count) is not int or fixture_count < 0:
        raise ValueError("fixture_count must be a non-negative integer")
    path = DATA_DIR / "source_status.json"
    payload = {
        "source": source,
        "analysis_source": analysis_source,
        "target_date": target_date.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fallback": source != "竞彩网",
        "message": message,
        "fixture_count": fixture_count,
        "no_fixtures": fixture_count == 0,
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
    root = DATA_DIR.resolve().parent
    existing_manifest = import_manifest_path(root, target_date)
    if existing_manifest.exists():
        manifest = read_valid_import_manifest(root, target_date)
        _atomic_replace_bytes(
            DATA_DIR / "fixtures.csv",
            (root / manifest["fixtures"]["path"]).read_bytes(),
        )
        _atomic_replace_bytes(
            DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json",
            (root / manifest["odds"]["path"]).read_bytes(),
        )
        print(f"Reusing immutable import: {existing_manifest}")
        return 0
    source = "竞彩网"
    manifest_source = "sporttery"
    source_message = ""
    try:
        selected = fetch_selling_matches(target_date)
    except Exception as exc:
        source = "中国足彩网"
        manifest_source = "zgzcw"
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
            manifest_source = None
            source_message += f" 中国足彩网也不可用（{type(fallback_exc).__name__}），已切换 ESPN。"
            print(f"WARNING: {source_message}")
            selected = fetch_espn_matches(target_date)
            if not selected:
                raise RuntimeError(
                    "fallback sources could not verify an empty schedule"
                )
    matches = selected
    if args.include_finished:
        matches = fetch_matches(target_date)
        selected = matches
        manifest_source = "sporttery"
    odds_data = collect_odds(selected)
    selected = attach_had_odds(selected, odds_data)
    try:
        selected = attach_professional_market(selected, fetch_zgzcw_matches(target_date))
    except Exception as exc:
        print(f"WARNING: 专业欧赔市场暂不可用（{type(exc).__name__}），本次使用竞彩足球市场概率。")
        selected = attach_professional_market(selected, [])
    analysis_source = "中国足彩网专业欧赔市场" if any(item.get("analysis_source") == "中国足彩网专业欧赔市场" for item in selected) else "竞彩足球市场（专业欧赔暂缺）"
    with tempfile.TemporaryDirectory(prefix="import-stage-", dir=DATA_DIR) as tmp:
        staging = Path(tmp)
        staged_fixtures = write_fixtures(
            selected, target_date, path=staging / "fixtures.csv"
        )
        staged_odds = write_odds_data(
            odds_data, target_date, path=staging / "odds.json"
        )
        fixture_count = count_written_fixtures(staged_fixtures, target_date)
        manifest_path = write_import_manifest(
            manifest_source,
            target_date,
            staged_fixtures,
            staged_odds,
            datetime.now(BEIJING),
        )
        fixtures_path = DATA_DIR / "fixtures.csv"
        odds_path = DATA_DIR / f"sporttery_odds_{target_date.isoformat()}.json"
        _atomic_replace_bytes(fixtures_path, staged_fixtures.read_bytes())
        _atomic_replace_bytes(odds_path, staged_odds.read_bytes())
    ratings_path = write_ratings(selected)
    status_path = write_source_status(
        source,
        target_date,
        source_message,
        analysis_source,
        fixture_count=fixture_count,
    )
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
    print(f"Updated: {manifest_path}")
    print(f"Data source: {source}")
    print(f"Updated: {status_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
