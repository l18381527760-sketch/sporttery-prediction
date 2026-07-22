import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from betting_ledger import resolve_ledger_path
from decision_bundle import read_valid_decision_bundle
from import_sporttery import read_valid_import_manifest
from plan_lock import read_valid_lock
from provisional_plan import read_valid_provisional_state


BEIJING = timezone(timedelta(hours=8))
SCHEMA_VERSION = 2
PHASES = ("forecast", "decision", "provisional", "settlement")
FIXTURE_REQUIRED_FIELDS = frozenset(
    {
        "date", "kickoff_local", "stage", "team_a", "team_b", "neutral", "venue",
        "match_id",
    }
)
PREDICTION_REQUIRED_FIELDS = frozenset(
    {
        "date", "kickoff", "stage", "match_num", "match_id", "team_a", "team_b",
        "p_a", "p_draw", "p_b", "pick", "confidence",
    }
)
PLAN_REQUIRED_FIELDS = frozenset(
    {
        "date", "strategy_version", "stage", "match", "team_a", "team_b", "play",
        "selection", "probability", "odds", "stake",
    }
)
LEDGER_REQUIRED_FIELDS = frozenset(
    {
        "date", "strategy_version", "stage", "match", "play", "selection",
        "probability", "odds", "stake", "status", "profit",
    }
)
OFFICIAL_FIXTURE_SOURCES = frozenset({"竞彩网", "中国足彩网", "sporttery", "zgzcw"})


def base_status(report_date: date) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "report_date": report_date.isoformat(),
        "forecast_ready": False,
        "decision_snapshot_ready": False,
        "settlement_ready": False,
        "plan_ready": False,
        "settled_through": "",
        "decision_odds_at_bjt": "",
        "plan_locked_at_bjt": "",
        "report_stage": "forecast",
        "initial_report_ready": False,
        "provisional_plan_sha256": "",
        "provisional_candidate_count": 0,
        "confirmed_stake": 0,
        "provisional_stake": 0,
        "revalidation_ready": False,
    }


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _nonempty_market(markets: object) -> bool:
    if not isinstance(markets, dict):
        return False
    for name in ("had", "hhad", "ttg"):
        market = markets.get(name)
        if isinstance(market, dict) and any(
            value is not None and (not isinstance(value, str) or value.strip())
            for value in market.values()
        ):
            return True
    return False


def _fixture_rows(
    path: Path, report_date: date, source_status: object
) -> tuple[bool, list[dict]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not FIXTURE_REQUIRED_FIELDS.issubset(reader.fieldnames):
                return False, []
            rows = [row for row in reader if row.get("date") == report_date.isoformat()]
    except OSError:
        return False, []
    metadata_valid, declared_count = _source_fixture_count(source_status, report_date)
    if not metadata_valid:
        return False, []
    if declared_count is not None and declared_count != len(rows):
        return False, []
    if not rows and declared_count != 0:
        return False, []
    return True, rows


def _source_fixture_count(
    source_status: object, report_date: date
) -> tuple[bool, int | None]:
    if not isinstance(source_status, dict):
        return True, None
    if source_status.get("target_date") != report_date.isoformat():
        return True, None
    count_fields = ("fixture_count", "fixtures_count", "match_count")
    declared = [source_status[field] for field in count_fields if field in source_status]
    if not declared:
        no_fixtures = source_status.get("no_fixtures")
        return no_fixtures is not True and (
            no_fixtures is None or type(no_fixtures) is bool
        ), None
    if any(type(value) is not int or value < 0 for value in declared):
        return False, None
    if len(set(declared)) != 1:
        return False, None

    declared_count = declared[0]
    no_fixtures = source_status.get("no_fixtures")
    if no_fixtures is not None and (
        type(no_fixtures) is not bool or no_fixtures != (declared_count == 0)
    ):
        return False, None
    if declared_count == 0 and (
        "fixture_count" not in source_status or no_fixtures is not True
    ):
        return False, None
    return True, declared_count


def _official_source_for_date(source_status: object, report_date: date) -> bool:
    source = source_status.get("source") if isinstance(source_status, dict) else None
    return (
        isinstance(source_status, dict)
        and source_status.get("target_date") == report_date.isoformat()
        and isinstance(source, str)
        and source in OFFICIAL_FIXTURE_SOURCES
    )


def _verified_zero_fixture_state(
    source_status: object,
    report_date: date,
    fixtures_ready: bool,
    fixtures: list[dict],
) -> bool:
    metadata_valid, declared_count = _source_fixture_count(
        source_status, report_date
    )
    return (
        fixtures_ready
        and not fixtures
        and metadata_valid
        and declared_count == 0
        and _official_source_for_date(source_status, report_date)
    )


def verified_zero_fixture_day(root: Path, report_date: date) -> bool:
    data = root / "data"
    source_status = _read_json(data / "source_status.json")
    fixtures_ready, fixtures = _fixture_rows(
        data / "fixtures.csv", report_date, source_status
    )
    return _verified_zero_fixture_state(
        source_status, report_date, fixtures_ready, fixtures
    )


def _official_fixture_rows(
    fixtures: list[dict], source_status: object, report_date: date
) -> list[dict]:
    if not _official_source_for_date(source_status, report_date):
        return []
    return [
        row
        for row in fixtures
        if row.get("pool_status") != "ESPN"
        and not row.get("match_id", "").lower().startswith("espn-")
    ]


def _csv_with_header(path: Path, required_fields: frozenset[str]) -> tuple[bool, int]:
    try:
        path = resolve_ledger_path(path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not required_fields.issubset(reader.fieldnames):
                return False, 0
            return True, sum(1 for _ in reader)
    except (OSError, ValueError):
        return False, 0


def _matching_decision_snapshot(root: Path, report_date: date) -> tuple[bool, str]:
    prefix = f"{report_date.isoformat()}-"
    suffix = "-decision.json"
    candidates = sorted((root / "data" / "odds_snapshots").glob(f"{prefix}*{suffix}"))
    for path in reversed(candidates):
        timestamp = path.name.removeprefix(prefix).removesuffix(suffix)
        try:
            captured_at = datetime.strptime(timestamp, "%H%M%S").replace(
                year=report_date.year,
                month=report_date.month,
                day=report_date.day,
                tzinfo=BEIJING,
            )
        except ValueError:
            continue
        payload = _read_json(path)
        if isinstance(payload, dict) and (
            (payload.get("target_date") or payload.get("date")) == report_date.isoformat()
            and (payload.get("capture_phase") or payload.get("phase")) == "decision"
            and isinstance(payload.get("matches"), list)
            and payload["matches"]
        ):
            return True, captured_at.isoformat()
    return False, ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _manifest_compatibility_outputs_match(
    root: Path, report_date: date, manifest: dict
) -> bool:
    data = root / "data"
    live_paths = {
        "fixtures": data / "fixtures.csv",
        "odds": data / f"sporttery_odds_{report_date.isoformat()}.json",
        "ratings": data / "team_ratings.csv",
    }
    for key, path in live_paths.items():
        record = manifest.get(key, {})
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size != record.get("bytes") or _sha256_file(path) != record.get("sha256"):
            return False
    return True


def _verified_report_image(
    path: Path,
    report_date: date,
    expected_report_stage: str | None,
    expected_build_id: str | None,
) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    if expected_report_stage is None and expected_build_id is None:
        return True
    try:
        with Image.open(path) as image:
            return (
                image.format == "PNG"
                and image.info.get("report_date") == report_date.isoformat()
                and image.info.get("report_stage") == expected_report_stage
                and image.info.get("build_id") == expected_build_id
            )
    except (OSError, UnidentifiedImageError):
        return False


def artifact_state(
    root: Path,
    report_date: date,
    *,
    expected_report_stage: str | None = None,
    expected_build_id: str | None = None,
) -> dict:
    date_text = report_date.isoformat()
    data = root / "data"
    output = root / "output"
    web = root / "web"

    try:
        import_manifest = read_valid_import_manifest(root, report_date)
        import_manifest_ready = _manifest_compatibility_outputs_match(
            root, report_date, import_manifest
        )
    except ValueError:
        import_manifest_ready = False

    source_status = _read_json(data / "source_status.json")
    source_ready = isinstance(source_status, dict) and source_status.get("target_date") == date_text
    fixtures_ready, fixtures = _fixture_rows(
        data / "fixtures.csv", report_date, source_status
    )
    fixture_count = len(fixtures) if fixtures_ready else None
    zero_fixture_verified = _verified_zero_fixture_state(
        source_status, report_date, fixtures_ready, fixtures
    )
    fixture_ids = [row.get("match_id", "") for row in fixtures]
    official_fixtures = _official_fixture_rows(fixtures, source_status, report_date)
    official_fixture_ids = [row.get("match_id", "") for row in official_fixtures]
    official_fixture_count = len(official_fixtures) if fixtures_ready else None

    odds_payload = _read_json(data / f"sporttery_odds_{date_text}.json")
    odds_ready = isinstance(odds_payload, dict)
    covered = sum(
        1
        for match_id in fixture_ids
        if odds_ready and _nonempty_market(odds_payload.get(match_id))
    )
    official_covered = sum(
        1
        for match_id in official_fixture_ids
        if odds_ready and _nonempty_market(odds_payload.get(match_id))
    )
    if fixture_count is None:
        odds_coverage = None
    elif fixture_count == 0:
        odds_coverage = 1.0
    else:
        odds_coverage = covered / fixture_count
    if official_fixture_count is None:
        official_odds_coverage = None
    elif official_fixture_count:
        official_odds_coverage = official_covered / official_fixture_count
    elif zero_fixture_verified:
        official_odds_coverage = 1.0
    else:
        official_odds_coverage = 0.0
    official_odds_complete = (
        zero_fixture_verified
        and official_fixture_count == 0
        and official_covered == 0
        and official_odds_coverage == 1.0
    ) or (
        type(official_fixture_count) is int
        and official_fixture_count > 0
        and official_covered == official_fixture_count
        and official_odds_coverage == 1.0
    )

    predictions_ready, prediction_count = _csv_with_header(
        output / f"predictions_{date_text}.csv", PREDICTION_REQUIRED_FIELDS
    )
    plan_csv_ready, plan_count = _csv_with_header(
        output / f"betting_plan_{date_text}.csv", PLAN_REQUIRED_FIELDS
    )
    decision_payload = _read_json(output / f"daily_decision_{date_text}.json")
    decision_ready = (
        isinstance(decision_payload, dict)
        and decision_payload.get("date") == date_text
        and isinstance(decision_payload.get("status"), str)
        and bool(decision_payload["status"].strip())
    )
    lock_payload = read_valid_lock(root, report_date)
    snapshot_ready, decision_odds_at_bjt = _matching_decision_snapshot(root, report_date)
    snapshot_ready = snapshot_ready or zero_fixture_verified
    ledger_ready, ledger_count = _csv_with_header(
        output / "betting_ledger.csv", LEDGER_REQUIRED_FIELDS
    )
    try:
        read_valid_decision_bundle(root, report_date)
        decision_bundle_ready = True
    except ValueError:
        decision_bundle_ready = False
    try:
        provisional_state = read_valid_provisional_state(root, report_date)
        provisional_state_ready = True
        provisional_plan_ready = True
        provisional_shadow_ready = True
        provisional_plan_count = provisional_state["active_candidate_count"]
        provisional_shadow_count = provisional_state["shadow_candidate_count"]
    except ValueError:
        provisional_state = {}
        provisional_state_ready = False
        provisional_plan_ready = False
        provisional_shadow_ready = False
        provisional_plan_count = 0
        provisional_shadow_count = 0
    site_ready = (web / "index.html").is_file()
    image_path = web / "daily-report.png"
    image_ready = _verified_report_image(
        image_path, report_date, expected_report_stage, expected_build_id
    )

    return {
        "source_ready": source_ready,
        "fixtures_ready": fixtures_ready,
        "import_manifest_ready": import_manifest_ready,
        "zero_fixture_verified": zero_fixture_verified,
        "fixture_count": fixture_count,
        "official_fixture_count": official_fixture_count,
        "odds_ready": odds_ready,
        "odds_covered_fixture_count": covered,
        "official_odds_count": official_covered,
        "odds_coverage": odds_coverage,
        "official_odds_coverage_ratio": official_odds_coverage,
        "official_odds_complete": official_odds_complete,
        "predictions_ready": predictions_ready,
        "prediction_count": prediction_count,
        "plan_csv_ready": plan_csv_ready,
        "plan_count": plan_count,
        "decision_ready": decision_ready,
        "plan_lock_ready": lock_payload is not None,
        "plan_locked_at_bjt": lock_payload.get("locked_at_bjt", "") if lock_payload else "",
        "decision_snapshot_ready": snapshot_ready,
        "decision_odds_at_bjt": decision_odds_at_bjt,
        "ledger_ready": ledger_ready,
        "ledger_count": ledger_count,
        "decision_bundle_ready": decision_bundle_ready,
        "provisional_plan_ready": provisional_plan_ready,
        "provisional_plan_count": provisional_plan_count,
        "provisional_shadow_ready": provisional_shadow_ready,
        "provisional_shadow_count": provisional_shadow_count,
        "provisional_state_ready": provisional_state_ready,
        "provisional_plan_sha256": provisional_state.get("provisional_plan_sha256", ""),
        "provisional_stake": provisional_state.get(
            "active_provisional_stake", 0
        ),
        "site_ready": site_ready,
        "image_ready": image_ready,
        "image_sha256": _sha256_file(image_path) if image_ready else "",
        "source_status": source_status if isinstance(source_status, dict) else {},
    }


def _previous_status(root: Path, report_date: date) -> dict:
    previous = _read_json(root / "web" / "report-status.json")
    if isinstance(previous, dict) and (
        previous.get("schema_version") in {1, SCHEMA_VERSION}
        and previous.get("report_date") == report_date.isoformat()
    ):
        return {**base_status(report_date), **previous}
    return base_status(report_date)


def _data_quality(state: dict) -> dict:
    return {
        key: state[key]
        for key in (
            "source_ready",
            "fixtures_ready",
            "import_manifest_ready",
            "zero_fixture_verified",
            "odds_ready",
            "official_odds_complete",
            "predictions_ready",
            "plan_csv_ready",
            "decision_ready",
            "plan_lock_ready",
            "decision_bundle_ready",
            "provisional_plan_ready",
            "provisional_shadow_ready",
            "provisional_state_ready",
            "decision_snapshot_ready",
            "ledger_ready",
            "site_ready",
            "image_ready",
        )
    }


def _prior_settlement_date(status: dict) -> date | None:
    try:
        return date.fromisoformat(status.get("settled_through", ""))
    except (TypeError, ValueError):
        return None


def _latest_bjt_timestamp(*values: object) -> str:
    candidates = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            candidates.append(parsed.astimezone(BEIJING))
    return max(candidates).isoformat() if candidates else ""


def publish_status(
    root: Path,
    report_date: date,
    phase: str,
    build_id: str,
    source_commit_sha: str,
    generated_at: datetime,
    settled_through: date | None = None,
) -> dict:
    if phase not in PHASES:
        raise ValueError(f"unsupported phase: {phase}")
    if not build_id.strip() or not source_commit_sha.strip():
        raise ValueError("build_id and source_commit_sha must not be blank")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    if phase == "settlement" and settled_through is None:
        raise ValueError("settled_through is required for settlement")
    if phase != "settlement" and settled_through is not None:
        raise ValueError("settled_through is only valid for settlement")

    state = artifact_state(
        root,
        report_date,
        expected_report_stage=phase,
        expected_build_id=build_id,
    )
    status = _previous_status(root, report_date)
    forecast_ready = all(
        state[key]
        for key in (
            "source_ready", "fixtures_ready", "import_manifest_ready", "odds_ready",
            "official_odds_complete", "predictions_ready", "site_ready", "image_ready",
        )
    )
    snapshot_ready = state["decision_snapshot_ready"]
    plan_ready = state["plan_lock_ready"] and state["plan_csv_ready"]
    initial_report_ready = all(
        state[key]
        for key in (
            "decision_bundle_ready", "provisional_plan_ready", "provisional_shadow_ready",
            "provisional_state_ready", "site_ready", "image_ready",
        )
    )
    if phase == "provisional" and initial_report_ready:
        from revalidation_reporting import publish_revalidation_report

        revalidation_status = publish_revalidation_report(
            root,
            report_date,
            [],
            generated_at,
            source_commit_sha,
        )
        status["revalidation_ready"] = (
            revalidation_status.get("report_date") == report_date.isoformat()
            and type(revalidation_status.get("revision")) is int
            and revalidation_status["revision"] >= 0
            and bool(revalidation_status.get("status_sha256"))
        )
    if phase == "forecast":
        status["forecast_ready"] = forecast_ready
    elif phase == "decision":
        status["decision_snapshot_ready"] = snapshot_ready
        status["plan_ready"] = plan_ready
        status["decision_odds_at_bjt"] = _latest_bjt_timestamp(
            status.get("decision_odds_at_bjt"), state["decision_odds_at_bjt"]
        )
        status["plan_locked_at_bjt"] = _latest_bjt_timestamp(
            status.get("plan_locked_at_bjt"), state["plan_locked_at_bjt"]
        )
    elif phase == "provisional":
        status["initial_report_ready"] = initial_report_ready
        if not initial_report_ready:
            status["revalidation_ready"] = False
        status["provisional_plan_sha256"] = state["provisional_plan_sha256"] if initial_report_ready else ""
        status["confirmed_stake"] = 0
        status["provisional_stake"] = state["provisional_stake"] if initial_report_ready else 0
    else:
        prior_settled_through = _prior_settlement_date(status)
        effective_settled_through = max(
            settled_through,
            prior_settled_through or settled_through,
        )
        status["settled_through"] = effective_settled_through.isoformat()
        status["settlement_ready"] = (
            effective_settled_through >= report_date - timedelta(days=1)
            and state["ledger_ready"]
            and state["site_ready"]
            and state["image_ready"]
        )
        current_initial_ready = (
            bool(status["initial_report_ready"]) and initial_report_ready
        )
        status["initial_report_ready"] = current_initial_ready
        if not current_initial_ready:
            status["revalidation_ready"] = False
        status["provisional_plan_sha256"] = (
            state["provisional_plan_sha256"] if current_initial_ready else ""
        )
        status["provisional_stake"] = (
            state["provisional_stake"] if current_initial_ready else 0
        )

    if phase != "decision":
        status["decision_snapshot_ready"] = (
            bool(status["decision_snapshot_ready"]) and snapshot_ready
        )
        status["plan_ready"] = bool(status["plan_ready"]) and plan_ready
    if phase != "forecast":
        status["forecast_ready"] = bool(status["forecast_ready"]) and forecast_ready
    if phase != "settlement":
        prior_settled_through = _prior_settlement_date(status)
        settlement_ready = (
            prior_settled_through is not None
            and prior_settled_through >= report_date - timedelta(days=1)
            and state["ledger_ready"]
            and state["site_ready"]
            and state["image_ready"]
        )
        status["settlement_ready"] = (
            bool(status["settlement_ready"]) and settlement_ready
        )

    status.update(
        {
            "schema_version": SCHEMA_VERSION,
            "report_stage": "provisional" if phase == "provisional" else phase,
            "build_id": build_id,
            "generated_at_bjt": generated_at.astimezone(BEIJING).isoformat(),
            "image_sha256": state["image_sha256"],
            "source_commit_sha": source_commit_sha,
            "fixture_count": state["fixture_count"],
            "official_fixture_count": state["official_fixture_count"],
            "prediction_count": state["prediction_count"],
            "plan_count": state["plan_count"],
            "ledger_count": state["ledger_count"],
            "odds_covered_fixture_count": state["odds_covered_fixture_count"],
            "official_odds_count": state["official_odds_count"],
            "odds_coverage": state["odds_coverage"],
            "official_odds_coverage_ratio": state["official_odds_coverage_ratio"],
            "provisional_candidate_count": (
                state["provisional_plan_count"] + state["provisional_shadow_count"]
            ),
            "data_quality": _data_quality(state),
            "source_status": state["source_status"],
        }
    )
    status_path = root / "web" / "report-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = status_path.with_name(status_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(status, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(status_path)
    return status


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc
    if value != parsed.isoformat():
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    return parsed


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("generated-at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("generated-at must include a timezone")
    return parsed


def _nonblank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=_parse_date)
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--build-id", required=True, type=_nonblank)
    parser.add_argument("--source-commit", required=True, type=_nonblank)
    parser.add_argument("--generated-at", required=True, type=_parse_aware_datetime)
    parser.add_argument("--settled-through", type=_parse_date)
    args = parser.parse_args()
    if args.phase == "settlement" and args.settled_through is None:
        parser.error("--settled-through is required for settlement")
    if args.phase != "settlement" and args.settled_through is not None:
        parser.error("--settled-through is only valid for settlement")
    try:
        publish_status(
            Path.cwd(),
            args.date,
            args.phase,
            args.build_id,
            args.source_commit,
            args.generated_at,
            args.settled_through,
        )
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
