import csv
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from betting_ledger import resolve_ledger_path
from legacy_snapshot import read_valid_legacy_snapshot
from live_odds import LIVE_SCHEMA_VERSION, read_valid_live_snapshot
from strategy_controls import fit_league_draw_calibrations


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = ROOT / "data" / "odds_snapshots"
LIVE_SNAPSHOT_DIR = ROOT / "data" / "live_odds_snapshots"
BEIJING = timezone(timedelta(hours=8))
SNAPSHOT_PHASES = (
    "opening",
    "decision",
    "monitoring",
    "pre_kickoff",
    "pre_kickoff_90",
    "pre_kickoff_30",
)


def play_family(play: str) -> str:
    if "串" in play:
        return "胜平负串关" if "胜平负" in play else "其他串关"
    return play


def summarize(rows: list[dict], active_strategy: str = "") -> dict:
    settled_all = [row for row in rows if row.get("status") in {"命中", "未中"}]
    settled = [row for row in settled_all if not active_strategy or row.get("strategy_version") == active_strategy]
    by_play: dict[str, list[dict]] = {}
    by_play_all: dict[str, list[dict]] = {}
    by_league: dict[str, list[dict]] = {}
    for row in settled:
        by_play.setdefault(play_family(row.get("play", "")), []).append(row)
        by_league.setdefault(row.get("stage") or "未知", []).append(row)
    for row in settled_all:
        by_play_all.setdefault(play_family(row.get("play", "")), []).append(row)

    def metrics(items: list[dict]) -> dict:
        if not items:
            return {
                "count": 0, "hits": 0, "hit_rate": None, "brier": None,
                "log_loss": None, "calibration_error": None, "stake": 0.0,
                "profit": 0.0, "roi": None, "average_expected_return": None,
                "max_drawdown": 0.0, "max_losing_streak": 0,
                "current_losing_streak": 0, "settled_days": 0,
            }
        items = sorted(
            items,
            key=lambda row: (
                str(row.get("date") or ""),
                str(row.get("match") or ""),
                str(row.get("play") or ""),
            ),
        )
        probabilities = [max(0.0, min(1.0, float(row.get("probability") or 0.5))) for row in items]
        safe_probabilities = [max(0.001, min(0.999, probability)) for probability in probabilities]
        outcomes = [1.0 if row.get("status") == "命中" else 0.0 for row in items]
        stake = sum(float(row.get("stake") or 0) for row in items)
        profit = sum(float(row.get("profit") or 0) for row in items)
        expected = [probability * float(row.get("odds") or 0) for probability, row in zip(probabilities, items)]
        cumulative = peak = max_drawdown = 0.0
        losing_streak = max_losing_streak = 0
        for row in items:
            cumulative += float(row.get("profit") or 0)
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
            if row.get("status") in {"未中", "未命中"}:
                losing_streak += 1
                max_losing_streak = max(max_losing_streak, losing_streak)
            else:
                losing_streak = 0
        return {
            "count": len(items),
            "hits": int(sum(outcomes)),
            "hit_rate": sum(outcomes) / len(items),
            "brier": sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) / len(items),
            "log_loss": -sum(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability) for probability, outcome in zip(safe_probabilities, outcomes)) / len(items),
            "calibration_error": _expected_calibration_error(probabilities, outcomes),
            "stake": round(stake, 2),
            "profit": round(profit, 2),
            "roi": profit / stake if stake else None,
            "average_expected_return": sum(expected) / len(expected),
            "max_drawdown": round(max_drawdown, 2),
            "max_losing_streak": max_losing_streak,
            "current_losing_streak": losing_streak,
            "settled_days": len({str(row.get("date") or "") for row in items if row.get("date")}),
        }

    league_metrics = {}
    for league, items in by_league.items():
        item_metrics = metrics(items)
        recent = metrics(items[-10:]) if len(items) >= 10 else item_metrics
        previous = metrics(items[-20:-10]) if len(items) >= 20 else None
        worsening = bool(previous and recent.get("brier") is not None and previous.get("brier") is not None and recent["brier"] > previous["brier"])
        item_metrics["recent_brier"] = recent.get("brier")
        item_metrics["paused"] = len(items) >= 20 and (item_metrics.get("roi") or 0) < 0 and worsening
        league_metrics[league] = item_metrics
    return {
        "overall": metrics(settled_all),
        "active_strategy": {"version": active_strategy, **metrics(settled)},
        "by_play": {key: metrics(items) for key, items in by_play.items()},
        "by_play_all": {key: metrics(items) for key, items in by_play_all.items()},
        "by_league": league_metrics,
    }


def _expected_calibration_error(
    probabilities: list[float], outcomes: list[float], bins: int = 5
) -> float | None:
    if not probabilities or len(probabilities) != len(outcomes):
        return None
    total = len(probabilities)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        members = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes)
            if lower <= probability <= upper
            and (index == bins - 1 or probability < upper)
        ]
        if not members:
            continue
        confidence = sum(item[0] for item in members) / len(members)
        accuracy = sum(item[1] for item in members) / len(members)
        error += len(members) / total * abs(confidence - accuracy)
    return error


def closing_line_value(rows: list[dict]) -> dict:
    snapshots = []
    if SNAPSHOT_DIR.exists():
        for path in sorted(SNAPSHOT_DIR.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                captured = datetime.fromisoformat(payload.get("captured_at", ""))
            except ValueError:
                continue
            for match in payload.get("matches", []):
                kickoff_text = match.get("kickoff_at", "")
                if not kickoff_text:
                    continue
                try:
                    kickoff = datetime.strptime(kickoff_text, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
                except ValueError:
                    continue
                if captured <= kickoff:
                    snapshots.append({**match, "target_date": payload.get("target_date", ""), "captured": captured, "kickoff": kickoff})

    def close_price(target_date: str, team_a: str, team_b: str, selection: str) -> float | None:
        key = {"胜": "h", "平": "d", "负": "a"}.get(selection)
        if key is None:
            return None
        candidates = [item for item in snapshots if item["target_date"] == target_date and item["team_a"] == team_a and item["team_b"] == team_b and item.get(key)]
        if not candidates:
            return None
        try:
            return float(max(candidates, key=lambda item: item["captured"])[key])
        except (TypeError, ValueError):
            return None

    values = []
    for row in rows:
        try:
            initial = float(row.get("odds") or 0)
        except ValueError:
            continue
        closing = None
        if "串" in row.get("play", ""):
            try:
                legs = json.loads(row.get("legs_json") or "[]")
            except json.JSONDecodeError:
                legs = []
            prices = []
            for leg in legs:
                if leg.get("kind") != "胜平负":
                    prices = []
                    break
                price = close_price(leg["date"], leg["team_a"], leg["team_b"], leg.get("selection", ""))
                if price is None:
                    prices = []
                    break
                prices.append(price)
            if prices:
                closing = math.prod(prices)
        else:
            teams = (row.get("match") or "").split(" vs ", 1)
            if len(teams) == 2:
                closing = close_price(row.get("date", ""), teams[0], teams[1], row.get("selection", ""))
        if closing and initial:
            values.append(initial / closing - 1)
    return {
        "count": len(values),
        "average_clv": sum(values) / len(values) if values else None,
        "positive_rate": sum(1 for value in values if value > 0) / len(values) if values else None,
    }


def snapshot_coverage(
    snapshot_dir: Path = SNAPSHOT_DIR,
    live_snapshot_dir: Path = LIVE_SNAPSHOT_DIR,
    target_date: date | None = None,
    not_after: datetime | None = None,
) -> dict:
    records = []
    file_captures = []
    paths = []
    snapshot_dir = Path(snapshot_dir)
    live_snapshot_dir = Path(live_snapshot_dir)
    if snapshot_dir.exists():
        paths.extend(("legacy", path) for path in snapshot_dir.glob("*.json"))
    if live_snapshot_dir.exists():
        paths.extend(("live", path) for path in live_snapshot_dir.rglob("*.json"))

    legacy_root = _snapshot_root(snapshot_dir, "odds_snapshots")
    live_root = _snapshot_root(live_snapshot_dir, "live_odds_snapshots")
    files = 0
    for kind, path in sorted(set(paths), key=lambda item: str(item[1])):
        parsed = (
            _legacy_snapshot_records(
                legacy_root,
                path,
                target_date,
                not_after,
            )
            if kind == "legacy"
            else _live_snapshot_records(
                live_root,
                path,
                target_date,
                not_after,
            )
        )
        if parsed is None:
            continue
        captured, snapshot_records = parsed
        files += 1
        file_captures.append(captured)
        records.extend(snapshot_records)

    identity_labels: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for record in records:
        identity = record["identity"]
        owner = (identity[0], identity[3])
        identity_labels.setdefault(owner, set()).add((identity[1], identity[2]))
    conflicts = {
        owner
        for owner, labels in identity_labels.items()
        if len(labels) > 1
    }
    valid_records = [
        record
        for record in records
        if (record["identity"][0], record["identity"][3]) not in conflicts
    ]

    bindings = {record["identity"] for record in valid_records}
    phase_ids = {phase: set() for phase in SNAPSHOT_PHASES}
    phase_observations = {phase: 0 for phase in SNAPSHOT_PHASES}
    requested_ids: dict[str, set[tuple[str, str, str, str]]] = {}
    latest_by_phase: dict[str, datetime] = {}
    latest_by_requested_phase: dict[str, datetime] = {}
    latest_by_binding_by_requested_phase: dict[
        str,
        dict[tuple[str, str, str, str], datetime],
    ] = {}
    for record in valid_records:
        binding = record["identity"]
        captured = record["captured"]
        phase = record["phase"]
        requested = record["requested"]
        if phase is not None:
            phase_ids[phase].add(binding)
            phase_observations[phase] += 1
            latest_by_phase[phase] = max(
                latest_by_phase.get(phase, captured),
                captured,
            )
        if requested is not None:
            requested_ids.setdefault(requested, set()).add(binding)
            by_binding = latest_by_binding_by_requested_phase.setdefault(
                requested,
                {},
            )
            by_binding[binding] = max(
                by_binding.get(binding, captured),
                captured,
            )
            latest_by_requested_phase[requested] = max(
                latest_by_requested_phase.get(requested, captured),
                captured,
            )

    return {
        "coverage_schema_version": 2,
        "counting_units": {
            "files": "validated_snapshot_files",
            "matches": "validated_match_observations",
            "phases": "validated_match_observations",
            "unique_fixture_bindings": "full_fixture_bindings",
            "unique_phases": "full_fixture_bindings",
            "requested_phases": "full_fixture_bindings",
        },
        "files": files,
        "matches": len(valid_records),
        "phases": {
            phase: phase_observations[phase]
            for phase in SNAPSHOT_PHASES
        },
        "unique_fixture_bindings": len(bindings),
        "unique_phases": {
            phase: len(phase_ids[phase])
            for phase in SNAPSHOT_PHASES
        },
        "requested_phases": {
            phase: len(ids)
            for phase, ids in sorted(requested_ids.items())
        },
        "latest": max(file_captures).isoformat() if file_captures else None,
        "latest_by_phase": {
            phase: captured.isoformat()
            for phase, captured in sorted(latest_by_phase.items())
        },
        "latest_by_requested_phase": {
            phase: captured.isoformat()
            for phase, captured in sorted(latest_by_requested_phase.items())
        },
        "bindings_by_requested_phase": {
            phase: [list(binding) for binding in sorted(ids)]
            for phase, ids in sorted(requested_ids.items())
        },
        "latest_by_binding_by_requested_phase": {
            phase: [
                {
                    "binding": list(binding),
                    "captured_at": captured.isoformat(),
                }
                for binding, captured in sorted(by_binding.items())
            ]
            for phase, by_binding in sorted(
                latest_by_binding_by_requested_phase.items()
            )
        },
    }


def _legacy_snapshot_records(
    root: Path | None,
    path: Path,
    target_date: date | None,
    not_after: datetime | None,
) -> tuple[datetime, list[dict]] | None:
    if root is None:
        return None
    try:
        payload, captured = read_valid_legacy_snapshot(
            root,
            path,
            target_date,
            not_after,
        )
    except (OSError, ValueError):
        return None
    payload_date = payload["target_date"]
    requested = payload["capture_phase"]
    records = []
    for row in payload["matches"]:
        records.append(
            _snapshot_record(
                row,
                payload_date,
                row["match_id"],
                captured,
                row["capture_phase"],
                requested,
            )
        )
    return captured, records


def _live_snapshot_records(
    root: Path | None,
    path: Path,
    target_date: date | None,
    not_after: datetime | None,
) -> tuple[datetime, list[dict]] | None:
    if root is None:
        return None
    candidate_date = target_date
    if candidate_date is None:
        try:
            candidate_date = date.fromisoformat(path.parent.name)
        except ValueError:
            return None
    try:
        payload = read_valid_live_snapshot(
            root,
            path,
            candidate_date,
            not_after,
        )
    except ValueError:
        return None
    captured = _snapshot_datetime(payload.get("captured_at"))
    if captured is None:
        return None
    records = []
    phase_bearing = payload.get("schema_version") == LIVE_SCHEMA_VERSION
    requested = payload.get("capture_phase") if phase_bearing else None
    for row in payload["matches"]:
        match_id = _canonical_snapshot_text(row.get("match_id"))
        if match_id is None:
            continue
        phase = row.get("capture_phase") if phase_bearing else None
        effective_requested = requested
        if (
            requested in {"pre_kickoff_90", "pre_kickoff_30"}
            and phase != requested
        ):
            effective_requested = None
        records.append(
            _snapshot_record(
                row,
                payload["target_date"],
                match_id,
                captured,
                phase,
                effective_requested,
            )
        )
    return captured, records


def _snapshot_record(
    row: dict,
    target_date: str,
    match_id: str,
    captured: datetime,
    phase: str | None,
    requested: str | None,
) -> dict:
    team_a = _canonical_snapshot_text(row.get("team_a")) or ""
    team_b = _canonical_snapshot_text(row.get("team_b")) or ""
    return {
        "identity": (target_date, team_a, team_b, match_id),
        "captured": captured,
        "phase": phase,
        "requested": requested,
    }


def _snapshot_root(path: Path, directory_name: str) -> Path | None:
    resolved = path.resolve()
    if resolved.name != directory_name or resolved.parent.name != "data":
        return None
    return resolved.parent.parent


def _snapshot_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(BEIJING)


def _canonical_snapshot_text(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


def write_metrics() -> Path:
    ledger = resolve_ledger_path(OUTPUT_DIR / "betting_ledger.csv")
    observation_ledger = resolve_ledger_path(
        OUTPUT_DIR / "observation_ledger.csv"
    )
    rows = []
    observation_rows = []
    if ledger.exists():
        with ledger.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    if observation_ledger.exists():
        with observation_ledger.open("r", encoding="utf-8-sig", newline="") as handle:
            observation_rows = list(csv.DictReader(handle))
    config_path = ROOT / "betting_config.json"
    active_strategy = ""
    if config_path.exists():
        try:
            active_strategy = str(json.loads(config_path.read_text(encoding="utf-8")).get("strategy_version") or "")
        except (OSError, json.JSONDecodeError):
            active_strategy = ""
    payload = summarize(rows, active_strategy)
    observation_payload = summarize(observation_rows, active_strategy)
    payload["active_betting_strategy"] = payload["active_strategy"]
    payload["active_strategy"] = observation_payload["active_strategy"]
    payload["calibration_by_league"] = observation_payload["by_league"]
    active_observations = [row for row in observation_rows if not active_strategy or row.get("strategy_version") == active_strategy]
    payload["clv"] = closing_line_value(active_observations)
    payload["snapshot_coverage"] = snapshot_coverage()
    calibration_config = {}
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}
    calibration_config = config.get("league_calibration", {})
    training_rows = []
    training_path = DATA_DIR / "draw_training_samples.csv"
    if training_path.exists():
        with training_path.open("r", encoding="utf-8-sig", newline="") as handle:
            training_rows = list(csv.DictReader(handle))
    payload["league_draw_calibration"] = fit_league_draw_calibrations(
        training_rows,
        min_samples=int(calibration_config.get("min_samples", 30)),
        prior_samples=int(calibration_config.get("prior_samples", 60)),
        max_adjustment=float(calibration_config.get("max_adjustment", 0.05)),
        validation_fraction=float(calibration_config.get("validation_fraction", 0.25)),
    )
    output = OUTPUT_DIR / "model_metrics.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Updated model metrics: {output}")
    return output


if __name__ == "__main__":
    write_metrics()
