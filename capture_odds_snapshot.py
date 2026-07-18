import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from import_sporttery import (
    fetch_selling_matches,
    fetch_zgzcw_matches,
    import_manifest_path,
    read_valid_import_manifest,
    single_eligibility,
)
from report_status import verified_zero_fixture_day


ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = ROOT / "data" / "odds_snapshots"
BEIJING = timezone(timedelta(hours=8))


CAPTURE_PHASES = {"opening", "decision", "monitoring"}


def capture(
    target_date: date,
    *,
    phase: str = "monitoring",
    captured_at: datetime | None = None,
    matches: list[dict] | None = None,
    odds_by_match: dict[str, dict] | None = None,
) -> Path | None:
    if phase not in CAPTURE_PHASES:
        raise ValueError(f"unsupported capture phase: {phase}")
    captured_at = _beijing_time(captured_at or datetime.now(BEIJING))
    source = "injected"
    manifest_record = None
    if matches is None and odds_by_match is None:
        manifest = read_valid_import_manifest(ROOT, target_date)
        source = manifest["source"]
        matches = _load_manifest_matches(ROOT, target_date, manifest)
        odds_by_match = _load_manifest_odds(ROOT, manifest)
        manifest_record = _manifest_record(ROOT, target_date)
        if read_valid_import_manifest(ROOT, target_date) != manifest:
            raise ValueError("import manifest changed during snapshot capture")
    elif matches is None or odds_by_match is None:
        raise ValueError("injected snapshot inputs must include matches and odds")
    normalized_matches = []
    for item in matches:
        if not item.get("homeTeam") or not item.get("awayTeam"):
            continue
        kickoff = _kickoff_time(item.get("kickoff_at"))
        if kickoff is not None and captured_at >= kickoff:
            continue
        minutes_to_kickoff = None
        match_phase = phase
        if kickoff is not None:
            minutes_to_kickoff = max(
                0, int((kickoff - captured_at).total_seconds() // 60)
            )
            if phase == "monitoring" and minutes_to_kickoff <= 60:
                match_phase = "pre_kickoff"
        match_id = str(item.get("matchId", ""))
        odds = odds_by_match.get(match_id, {})
        if not isinstance(odds, dict):
            odds = {}
        markets = {
            market: value if isinstance(value := odds.get(market), dict) else {}
            for market in ("had", "hhad", "ttg")
        }
        had = markets["had"]
        normalized_matches.append(
            {
                "match_id": match_id,
                "team_a": item.get("homeTeam", ""),
                "team_b": item.get("awayTeam", ""),
                "match_num": item.get("matchNumStr", ""),
                "kickoff_at": item.get("kickoff_at", ""),
                "capture_phase": match_phase,
                "minutes_to_kickoff": minutes_to_kickoff,
                "markets": markets,
                "single_eligibility": single_eligibility(item),
                "h": item.get("h", "") or had.get("h", ""),
                "d": item.get("d", "") or had.get("d", ""),
                "a": item.get("a", "") or had.get("a", ""),
                "market_h": item.get("market_h", ""),
                "market_d": item.get("market_d", ""),
                "market_a": item.get("market_a", ""),
                "market_type": "win_draw_loss",
                "settlement_minutes": 90,
                "includes_extra_time": False,
            }
        )
    payload = {
        "target_date": target_date.isoformat(),
        "captured_at": captured_at.isoformat(),
        "capture_phase": phase,
        "source": source,
        "matches": normalized_matches,
    }
    if manifest_record is not None:
        payload["import_manifest"] = manifest_record
    if not payload["matches"] and matches:
        print("No Sporttery matches available for this snapshot.")
        return None
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    output = SNAPSHOT_DIR / (
        f"{target_date.isoformat()}-{captured_at.strftime('%H%M%S')}-{phase}.json"
    )
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Captured odds snapshot: {output}; matches={len(payload['matches'])}")
    return output


def _beijing_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def _load_odds(target_date: date) -> dict[str, dict]:
    path = ROOT / "data" / f"sporttery_odds_{target_date.isoformat()}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_manifest_matches(root: Path, target_date: date, manifest: dict) -> list[dict]:
    path = root / manifest["fixtures"]["path"]
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("manifest fixtures are invalid") from exc
    matches = []
    seen = set()
    for row in rows:
        if row.get("date") != target_date.isoformat():
            continue
        match_id = str(row.get("match_id") or "").strip()
        home = str(row.get("team_a") or "").strip()
        away = str(row.get("team_b") or "").strip()
        if not match_id or not home or not away or match_id in seen:
            raise ValueError("manifest fixture identity is invalid")
        seen.add(match_id)
        matches.append({
            "matchId": match_id,
            "matchNumStr": row.get("match_num", ""),
            "homeTeam": home,
            "awayTeam": away,
            "kickoff_at": row.get("kickoff_at", ""),
            "isSingleHad": row.get("is_single_had", ""),
            "isSingleHhad": row.get("is_single_hhad", ""),
            "isSingleTtg": row.get("is_single_ttg", ""),
            "h": row.get("odds_a", ""),
            "d": row.get("odds_draw", ""),
            "a": row.get("odds_b", ""),
            "market_h": row.get("market_odds_a", ""),
            "market_d": row.get("market_odds_draw", ""),
            "market_a": row.get("market_odds_b", ""),
        })
    return matches


def _load_manifest_odds(root: Path, manifest: dict) -> dict[str, dict]:
    path = root / manifest["odds"]["path"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest odds are invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("manifest odds are invalid")
    return payload


def _manifest_record(root: Path, target_date: date) -> dict:
    path = import_manifest_path(root, target_date)
    payload = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _snapshot_has_matches(path: Path | None) -> bool:
    if not isinstance(path, Path) or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    matches = payload.get("matches") if isinstance(payload, dict) else None
    return isinstance(matches, list) and bool(matches)


def _kickoff_time(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _beijing_time(parsed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a pre-match odds snapshot.")
    parser.add_argument("--date", default=datetime.now(BEIJING).date().isoformat())
    parser.add_argument("--phase", choices=sorted(CAPTURE_PHASES), default="monitoring")
    args = parser.parse_args()
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    output = capture(target_date, phase=args.phase)
    if args.phase == "decision":
        if not _snapshot_has_matches(output) and not verified_zero_fixture_day(ROOT, target_date):
            print(
                "Decision snapshot capture failed: no snapshot matches and no verified zero-fixture proof.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
