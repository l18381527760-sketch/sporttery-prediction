from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from import_sporttery import import_manifest_path, read_valid_import_manifest


FixtureKey = tuple[str, str, str]


def fixture_match_ids(
    root: Path, target_date: date
) -> dict[FixtureKey, frozenset[str]]:
    root = root.resolve()
    manifest_path = import_manifest_path(root, target_date)
    if manifest_path.exists():
        manifest = read_valid_import_manifest(root, target_date)
        path = root / manifest["fixtures"]["path"]
    else:
        path = root / "data" / "fixtures.csv"
    rows = _rows(path)
    target = target_date.isoformat()
    identities: dict[FixtureKey, set[str]] = {}
    owner: dict[str, FixtureKey] = {}
    for row in rows:
        if row.get("date") != target:
            continue
        home = str(row.get("team_a") or "").strip()
        away = str(row.get("team_b") or "").strip()
        match_id = str(row.get("match_id") or "").strip()
        if not home or not away or not match_id:
            raise ValueError("fixture identity is incomplete")
        key = (target, home, away)
        if match_id in owner and owner[match_id] != key:
            raise ValueError("fixture match_id is duplicated")
        owner[match_id] = key
        identities.setdefault(key, set()).add(match_id)
    return {key: frozenset(values) for key, values in identities.items()}


def fixture_identity_rate(root: Path, target_date: date) -> tuple[int, int]:
    identities = fixture_match_ids(root, target_date)
    return sum(len(ids) == 1 for ids in identities.values()), len(identities)


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("fixture identity source is invalid") from exc
