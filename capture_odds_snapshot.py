import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from import_sporttery import fetch_zgzcw_matches


ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = ROOT / "data" / "odds_snapshots"
BEIJING = timezone(timedelta(hours=8))


def capture(target_date: date) -> Path | None:
    captured_at = datetime.now(BEIJING)
    matches = fetch_zgzcw_matches(target_date)
    payload = {
        "target_date": target_date.isoformat(),
        "captured_at": captured_at.isoformat(),
        "source": "zgzcw_professional",
        "matches": [
            {
                "team_a": item.get("homeTeam", ""),
                "team_b": item.get("awayTeam", ""),
                "match_num": item.get("matchNumStr", ""),
                "kickoff_at": item.get("kickoff_at", ""),
                "h": item.get("h", ""),
                "d": item.get("d", ""),
                "a": item.get("a", ""),
                "market_h": item.get("h", ""),
                "market_d": item.get("d", ""),
                "market_a": item.get("a", ""),
                "market_type": "win_draw_loss",
                "settlement_minutes": 90,
                "includes_extra_time": False,
            }
            for item in matches
            if item.get("homeTeam") and item.get("awayTeam")
        ],
    }
    if not payload["matches"]:
        print("No Sporttery matches available for this snapshot.")
        return None
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    output = SNAPSHOT_DIR / f"{target_date.isoformat()}-{captured_at.strftime('%H%M')}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Captured odds snapshot: {output}; matches={len(payload['matches'])}")
    return output


if __name__ == "__main__":
    capture(datetime.now(BEIJING).date())
