from __future__ import annotations

from datetime import datetime


ALLOWED_RESULT_SOURCES = frozenset({"sporttery", "zgzcw"})


def proven_result_provenance(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        source = _text(row.get("result_source")).lower()
        _text(row.get("source_record_id"))
        captured = datetime.fromisoformat(_text(row.get("captured_at_bjt")))
    except (TypeError, ValueError):
        return False
    return (
        source in ALLOWED_RESULT_SOURCES
        and captured.tzinfo is not None
        and captured.utcoffset() is not None
    )


def normalized_result(row: dict) -> dict | None:
    try:
        if not proven_result_provenance(row):
            return None
        if row.get("result_status") != "finished":
            return None
        if row.get("score_scope") != "regular_time_90":
            return None
        if str(row.get("settlement_minutes") or "") != "90":
            return None
        match_id = _text(row.get("match_id"))
        source = _text(row.get("result_source")).lower()
        record_id = _text(row.get("source_record_id"))
        captured = _text(row.get("captured_at_bjt"))
        parsed = datetime.fromisoformat(captured)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        home = int(str(row.get("home_goals")))
        away = int(str(row.get("away_goals")))
        if home < 0 or away < 0:
            return None
    except (TypeError, ValueError):
        return None
    return {
        "match_id": match_id,
        "home_goals": home,
        "away_goals": away,
        "result_source": source,
        "source_record_id": record_id,
        "captured_at_bjt": parsed.isoformat(),
    }


def proven_90_minute_result(row: dict) -> bool:
    return normalized_result(row) is not None


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required result text is missing")
    return value.strip()
