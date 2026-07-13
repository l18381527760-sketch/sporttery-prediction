"""Collect comparable 90-minute match-winner market evidence."""

import argparse
import csv
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from draw_alert_core import fair_probabilities


ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
POLYMARKET_TIMEOUT_SECONDS = 10


class PublicMarketError(RuntimeError):
    """A public-market response that should be recorded, not fabricated."""


def probability_record(odds: tuple[float, float, float], volume: float | None) -> dict:
    home, draw, away = fair_probabilities(*odds)
    return {
        "market_scope": "90m",
        "market_type": "win_draw_loss",
        "settlement_minutes": 90,
        "includes_extra_time": False,
        "home_probability": home,
        "draw_probability": draw,
        "away_probability": away,
        "volume": volume,
    }


def odds_movement(
    opening: tuple[float, float, float] | None,
    latest: tuple[float, float, float] | None,
) -> float:
    if not opening or not latest:
        return 0.0
    latest_fair = fair_probabilities(*latest)
    favorite = 0 if latest_fair[0] >= latest_fair[2] else 2
    return latest[favorite] / opening[favorite] - 1.0


def parse_polymarket_90m(market: dict, team_a: str, team_b: str) -> dict | None:
    question = str(market.get("question") or "").casefold()
    blocked = (
        "qualify",
        "qualification",
        "advance",
        "win the world cup",
        "lift the trophy",
        "extra time",
        "penalt",
        "加时",
        "晋级",
    )
    if any(word in question for word in blocked):
        return None
    if team_a.casefold() not in question or team_b.casefold() not in question:
        return None
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        prices = [float(value) for value in json.loads(market.get("outcomePrices") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    mapping = {str(name).casefold(): price for name, price in zip(outcomes, prices)}
    home = mapping.get(team_a.casefold())
    away = mapping.get(team_b.casefold())
    draw = next((price for name, price in mapping.items() if name in {"draw", "tie"}), None)
    if any(value is None or value < 0 or value > 1 for value in (home, draw, away)):
        return None
    return {
        "market_scope": "90m",
        "market_type": "win_draw_loss",
        "settlement_minutes": 90,
        "includes_extra_time": False,
        "home_probability": home,
        "draw_probability": draw,
        "away_probability": away,
        "volume": _optional_float(market.get("volume")),
    }


def build_evidence(fixture: dict, snapshots: dict, polymarket: list[dict]) -> dict:
    sources: dict[str, dict] = {}
    domestic = _odds_from_fields(fixture, "odds_a", "odds_draw", "odds_b")
    if domestic:
        sources["domestic_sporttery"] = probability_record(domestic, volume=None)
    professional = _odds_from_fields(fixture, "market_odds_a", "market_odds_draw", "market_odds_b")
    if professional:
        sources["zgzcw_professional"] = probability_record(professional, volume=None)

    domestic_fair = fair_probabilities(*domestic) if domestic else None
    regional_gap = 0.0
    if domestic_fair and professional:
        favorite = 0 if domestic_fair[0] >= domestic_fair[2] else 2
        regional_gap = domestic_fair[favorite] - fair_probabilities(*professional)[favorite]

    for market in polymarket:
        parsed = parse_polymarket_90m(market, fixture["team_a"], fixture["team_b"])
        if parsed:
            sources["polymarket"] = parsed
            break

    movement = odds_movement(snapshots.get("open"), snapshots.get("latest"))
    source_count = len(sources)
    return {
        "match_id": fixture["match_id"],
        "team_a": fixture["team_a"],
        "team_b": fixture["team_b"],
        "kickoff_at": fixture.get("kickoff_at", ""),
        "market_scope": "90m",
        "sources": sources,
        "source_count": source_count,
        "favorite_movement": movement,
        "regional_gap": regional_gap,
        "quality": "high" if source_count >= 3 else "medium" if source_count >= 2 else "low",
    }


def fetch_polymarket(team_a: str, team_b: str) -> list[dict]:
    url = f"{POLYMARKET_SEARCH_URL}?{urlencode({'q': f'{team_a} {team_b}'})}"
    try:
        with urlopen(url, timeout=POLYMARKET_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise PublicMarketError(f"HTTP {error.code}") from error
    except URLError as error:
        reason = str(error.reason).casefold()
        label = "timeout" if "timeout" in reason else f"request failed: {error.reason}"
        raise PublicMarketError(label) from error
    except TimeoutError as error:
        raise PublicMarketError("timeout") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicMarketError("invalid JSON") from error
    if isinstance(payload, dict):
        payload = payload.get("markets", payload.get("data", []))
    if not isinstance(payload, list):
        raise PublicMarketError("invalid JSON shape")
    return [market for market in payload if isinstance(market, dict)]


def write_payload(path: Path, target_date: str, matches: list[dict], errors: list[str]) -> Path:
    payload = {
        "target_date": target_date,
        "captured_at": datetime.now(BEIJING).isoformat(),
        "matches": matches,
        "errors": errors,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def collect(target_date: date, offline: bool = False) -> Path:
    data_dir = ROOT / "data"
    fixtures = _load_fixtures(target_date, data_dir)
    sporttery = _load_json(data_dir / f"sporttery_odds_{target_date.isoformat()}.json", {})
    snapshots = _load_snapshots(target_date, data_dir / "odds_snapshots")
    matches: list[dict] = []
    errors: list[str] = []
    for fixture in fixtures:
        fixture = dict(fixture)
        _apply_official_odds(fixture, sporttery)
        match_snapshots = _match_snapshots(fixture, snapshots)
        markets: list[dict] = []
        polymarket_request_succeeded = False
        if not offline:
            try:
                markets = fetch_polymarket(fixture["team_a"], fixture["team_b"])
                polymarket_request_succeeded = True
            except PublicMarketError as error:
                errors.append(f"polymarket {fixture['match_id']}: {error}")
        evidence = build_evidence(fixture, match_snapshots, markets)
        if "domestic_sporttery" not in evidence["sources"]:
            errors.append(f"domestic_sporttery {fixture['match_id']}: missing 90m odds")
        if polymarket_request_succeeded and "polymarket" not in evidence["sources"]:
            errors.append(f"polymarket {fixture['match_id']}: no matching 90m market")
        matches.append(evidence)
    return write_payload(data_dir / f"market_heat_{target_date.isoformat()}.json", target_date.isoformat(), matches, errors)


def _load_fixtures(target_date: date, data_dir: Path) -> list[dict]:
    with (data_dir / "fixtures.csv").open(encoding="utf-8", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("date") == target_date.isoformat()]


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_snapshots(target_date: date, snapshot_dir: Path) -> list[dict]:
    snapshots: list[dict] = []
    for path in sorted(snapshot_dir.glob(f"{target_date.isoformat()}-*.json")):
        payload = _load_json(path, {})
        if isinstance(payload, dict) and isinstance(payload.get("matches"), list):
            snapshots.append(payload)
    return snapshots


def _apply_official_odds(fixture: dict, sporttery: Any) -> None:
    if not isinstance(sporttery, dict):
        return
    had = sporttery.get(str(fixture.get("match_id")), {}).get("had", {})
    official = _odds_from_fields(had, "h", "d", "a") if isinstance(had, dict) else None
    if official:
        fixture["odds_a"], fixture["odds_draw"], fixture["odds_b"] = official


def _match_snapshots(fixture: dict, snapshots: list[dict]) -> dict:
    values: list[tuple[float, float, float] | None] = []
    match_id = str(fixture.get("match_id") or "")
    match_num = str(fixture.get("match_num") or "")
    for snapshot in snapshots:
        for item in snapshot["matches"]:
            if not isinstance(item, dict) or not _snapshot_matches(item, fixture, match_id, match_num):
                continue
            odds = _odds_from_fields(item, "market_h", "market_d", "market_a")
            values.append(odds or _odds_from_fields(item, "h", "d", "a"))
    valid = [value for value in values if value]
    return {"open": valid[0], "latest": valid[-1]} if valid else {}


def _snapshot_matches(item: dict, fixture: dict, match_id: str, match_num: str) -> bool:
    item_num = str(item.get("match_num") or "")
    if item_num and item_num in {match_id, match_num}:
        return True
    return item.get("team_a") == fixture.get("team_a") and item.get("team_b") == fixture.get("team_b")


def _odds_from_fields(record: dict, *keys: str) -> tuple[float, float, float] | None:
    try:
        odds = tuple(float(record[key]) for key in keys)
    except (KeyError, TypeError, ValueError):
        return None
    return odds if len(odds) == 3 and all(value > 0 for value in odds) else None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect timestamped 90-minute market evidence.")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(BEIJING).date())
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    print(collect(args.date, offline=args.offline))


if __name__ == "__main__":
    main()
