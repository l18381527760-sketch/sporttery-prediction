import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from import_sporttery import fetch_zgzcw_matches
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
) -> Path | None:
    if phase not in CAPTURE_PHASES:
        raise ValueError(f"unsupported capture phase: {phase}")
    captured_at = _beijing_time(captured_at or datetime.now(BEIJING))
    matches = fetch_zgzcw_matches(target_date)
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
        normalized_matches.append(
            {
                "team_a": item.get("homeTeam", ""),
                "team_b": item.get("awayTeam", ""),
                "match_num": item.get("matchNumStr", ""),
                "kickoff_at": item.get("kickoff_at", ""),
                "capture_phase": match_phase,
                "minutes_to_kickoff": minutes_to_kickoff,
                "h": item.get("h", ""),
                "d": item.get("d", ""),
                "a": item.get("a", ""),
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
        "source": "zgzcw",
        "matches": normalized_matches,
    }
    if not payload["matches"]:
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
        snapshot_written = (
            isinstance(output, Path)
            and output.is_file()
            and output.stat().st_size > 0
        )
        if not snapshot_written and not verified_zero_fixture_day(ROOT, target_date):
            print(
                "Decision snapshot capture failed: no non-empty snapshot and no verified zero-fixture proof.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
