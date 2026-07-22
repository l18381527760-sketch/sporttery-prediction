"""Create and validate one immutable decision input bundle per business date."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from import_sporttery import import_manifest_path, read_valid_import_manifest
from live_odds import read_valid_live_snapshot


BEIJING = timezone(timedelta(hours=8))
BUNDLE_SCHEMA_VERSION = 3
PREDICTION_METADATA_SCHEMA_VERSION = 2
DOMESTIC_DECISION_SOURCES = frozenset({"sporttery", "zgzcw"})
MODEL_CODE_PATHS = (
    "predict_today.py",
    "generate_betting_plan.py",
    "value_candidates.py",
    "value_portfolio.py",
    "official_markets.py",
    "betting_ledger.py",
    "strategy_controls.py",
)
MODEL_REFERENCE_INPUTS = (
    "import_manifest",
    "fixture_extract",
    "prediction_config",
    "ratings",
    "history_inputs",
    "model_code",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_fixture_rows(
    root: Path,
    target_date: date,
    *,
    import_manifest: dict | None = None,
) -> list[dict]:
    root = Path(root)
    manifest = import_manifest or read_valid_import_manifest(root, target_date)
    rows = _read_csv(root / manifest["fixtures"]["path"], required=True)
    selected = [
        {str(key): "" if value is None else str(value) for key, value in row.items()}
        for row in rows
        if row.get("date") == target_date.isoformat()
    ]
    for row in selected:
        _required_text(row.get("match_id"), "fixture match_id")
        _required_text(row.get("team_a"), "fixture team_a")
        _required_text(row.get("team_b"), "fixture team_b")
        _match_datetime(row.get("kickoff_at"), "fixture kickoff_at")
    return sorted(
        selected,
        key=lambda row: json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def write_prediction_metadata(
    root: Path,
    target_date: date,
    generated_at: datetime,
    *,
    consumed_manifest_inputs: dict | None = None,
) -> Path:
    root = Path(root).resolve()
    generated = _aware_datetime(generated_at, "prediction generated_at").astimezone(BEIJING)
    date_text = target_date.isoformat()
    prediction_path = root / "output" / f"predictions_{date_text}.csv"
    predictions = _read_csv(prediction_path, required=True)
    _validate_prediction_rows(predictions, target_date)
    import_manifest = read_valid_import_manifest(root, target_date)
    fixtures = canonical_fixture_rows(
        root,
        target_date,
        import_manifest=import_manifest,
    )
    manifest_inputs = {
        key: dict(import_manifest[key])
        for key in ("fixtures", "ratings")
    }
    if (
        consumed_manifest_inputs is not None
        and consumed_manifest_inputs != manifest_inputs
    ):
        raise ValueError("prediction consumed inputs differ from import manifest")
    payload = {
        "schema_version": PREDICTION_METADATA_SCHEMA_VERSION,
        "target_date": date_text,
        "generated_at_bjt": generated.isoformat(),
        "predictions": _file_record(root, prediction_path),
        "fixture_extract": {
            "rows": fixtures,
            "sha256": canonical_json_sha256(fixtures),
            "match_count": len(fixtures),
        },
        "model_inputs": {
            "config": _file_record(root, root / "config.json"),
            **manifest_inputs,
            "prediction_code": _file_record(root, root / "predict_today.py"),
        },
    }
    path = root / "output" / f"predictions_{date_text}.meta.json"
    _atomic_write_json(path, payload)
    return path


def create_decision_bundle(
    root: Path,
    target_date: date,
    locked_at: datetime,
    decision_snapshot_path: Path | None = None,
) -> dict:
    root = Path(root).resolve()
    locked = _aware_datetime(locked_at, "locked_at").astimezone(BEIJING)
    metadata_path = root / "output" / f"predictions_{target_date.isoformat()}.meta.json"
    try:
        metadata = _read_json(metadata_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("prediction metadata is missing or invalid") from exc
    _validate_prediction_metadata(
        root,
        metadata,
        target_date,
        locked,
        verify_current_inputs=True,
    )
    import_manifest = read_valid_import_manifest(root, target_date)
    manifest_record = _file_record(root, import_manifest_path(root, target_date))
    if decision_snapshot_path is None:
        snapshot_path, snapshot = _select_snapshot(
            root,
            target_date,
            locked,
            import_manifest=import_manifest,
            manifest_record=manifest_record,
        )
    else:
        snapshot_path = Path(decision_snapshot_path)
        snapshot = read_valid_live_snapshot(root, snapshot_path, target_date, locked)
        _validate_live_snapshot_at_lock(snapshot, locked)
        if not snapshot_path.is_absolute():
            snapshot_path = root / snapshot_path
        snapshot_path = snapshot_path.resolve()
    predictions_path = root / metadata["predictions"]["path"]
    predictions = _read_csv(predictions_path, required=True)
    fixtures = metadata["fixture_extract"]["rows"]
    _validate_cross_artifact_identities(
        snapshot,
        predictions,
        fixtures,
        require_match_num=_is_live_snapshot_path(root, snapshot_path, target_date),
    )

    betting_config = _read_json(root / "betting_config.json")
    prediction_config = _read_json(root / "config.json")
    history_inputs = {
        "paid_history": _inline_rows(
            _read_csv(root / "output" / "betting_ledger.csv", required=False)
        ),
        "observation_history": _inline_rows(
            _read_csv(root / "output" / "observation_ledger.csv", required=False)
        ),
        "training_samples": _inline_rows(
            _read_csv(root / "data" / "draw_training_samples.csv", required=False)
        ),
        "account_metrics": _inline_payload(
            _read_optional_json(root / "output" / "model_metrics.json")
        ),
    }
    model_code = {
        path: _file_record(root, root / path) for path in MODEL_CODE_PATHS
    }
    match_identities = _snapshot_match_identities(snapshot)
    paid_market_values = _snapshot_paid_market_values(snapshot)
    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "target_date": target_date.isoformat(),
        "locked_at_bjt": locked.isoformat(),
        "import_manifest": {
            **manifest_record,
            "payload": import_manifest,
        },
        "decision_snapshot": {
            **_file_record(root, snapshot_path),
            "source": snapshot["source"].lower(),
            "captured_at_bjt": _aware_datetime(
                snapshot["captured_at"], "snapshot captured_at"
            ).astimezone(BEIJING).isoformat(),
            "match_identities": match_identities,
            "paid_market_values": paid_market_values,
            "payload": snapshot,
        },
        "predictions": {
            **metadata["predictions"],
            "generated_at_bjt": metadata["generated_at_bjt"],
            "metadata": _file_record(root, metadata_path),
            "model_inputs": metadata["model_inputs"],
        },
        "fixture_extract": metadata["fixture_extract"],
        "configuration": {
            "betting": {
                "payload": betting_config,
                "sha256": canonical_json_sha256(betting_config),
            },
            "prediction": {
                "payload": prediction_config,
                "sha256": canonical_json_sha256(prediction_config),
            },
        },
        "ratings": metadata["model_inputs"]["ratings"],
        "model_code": model_code,
        "history_inputs": history_inputs,
        "roles": {
            "paid_odds": "decision_snapshot",
            "paid_plan": "deterministic_bundle_evidence",
            "model_reference_inputs": list(MODEL_REFERENCE_INPUTS),
        },
    }
    payload["paid_plan_evidence"] = _build_paid_plan_evidence(
        root, target_date, locked, payload
    )
    path = _bundle_path(root, target_date)
    if path.exists() or not _atomic_publish_json(path, payload):
        _require_matching_existing_bundle(path, payload)
    return read_valid_decision_bundle(
        root,
        target_date,
        expected_locked_at=locked,
        verify_current_inputs=True,
    )


def read_valid_decision_bundle(
    root: Path,
    target_date: date,
    *,
    expected_locked_at: datetime | None = None,
    verify_current_inputs: bool = False,
) -> dict:
    root = Path(root).resolve()
    try:
        payload = _read_json(_bundle_path(root, target_date))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("decision bundle is missing or invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ValueError("decision bundle schema is invalid")
    if payload.get("target_date") != target_date.isoformat():
        raise ValueError("decision bundle target date is invalid")
    locked = _aware_datetime(payload.get("locked_at_bjt"), "bundle locked_at")
    if expected_locked_at is not None:
        expected = _aware_datetime(expected_locked_at, "expected locked_at")
        if locked.astimezone(BEIJING) != expected.astimezone(BEIJING):
            raise ValueError("decision bundle lock timestamp mismatch")

    snapshot_record = payload.get("decision_snapshot")
    prediction_record = payload.get("predictions")
    manifest_record = payload.get("import_manifest")
    if (
        not isinstance(snapshot_record, dict)
        or not isinstance(prediction_record, dict)
        or not isinstance(manifest_record, dict)
    ):
        raise ValueError("decision bundle artifact records are invalid")
    _verify_file_record(root, manifest_record)
    _verify_file_record(root, snapshot_record)
    _verify_file_record(root, prediction_record)
    _verify_file_record(root, prediction_record.get("metadata"))
    snapshot_path = (root / snapshot_record["path"]).resolve()
    snapshot_is_live = _is_live_snapshot_path(root, snapshot_path, target_date)
    snapshot = snapshot_record.get("payload")
    if not isinstance(snapshot, dict):
        raise ValueError("decision bundle snapshot is invalid")
    if snapshot_is_live:
        if read_valid_live_snapshot(root, snapshot_path, target_date, locked) != snapshot:
            raise ValueError("decision bundle live snapshot is inconsistent")
        _validate_live_snapshot_at_lock(snapshot, locked)
    else:
        try:
            bound_snapshot = _read_json(snapshot_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("decision bundle snapshot is missing or invalid") from exc
        if bound_snapshot != snapshot:
            raise ValueError("decision bundle snapshot is inconsistent")
        _validate_snapshot(snapshot, target_date, locked.astimezone(BEIJING))
    import_manifest = read_valid_import_manifest(root, target_date)
    actual_manifest_record = _file_record(root, import_manifest_path(root, target_date))
    if (
        manifest_record.get("payload") != import_manifest
        or any(
            manifest_record.get(key) != actual_manifest_record.get(key)
            for key in ("path", "sha256", "bytes")
        )
    ):
        raise ValueError("decision bundle import manifest is inconsistent")
    if not snapshot_is_live:
        _validate_snapshot_import(snapshot, import_manifest, actual_manifest_record)
    if snapshot_record.get("source") != snapshot.get("source", "").lower():
        raise ValueError("decision bundle source is inconsistent")
    if snapshot_record.get("captured_at_bjt") != _aware_datetime(
        snapshot.get("captured_at"), "snapshot captured_at"
    ).astimezone(BEIJING).isoformat():
        raise ValueError("decision bundle capture timestamp is inconsistent")
    if snapshot_record.get("match_identities") != _snapshot_match_identities(snapshot):
        raise ValueError("decision bundle match identity summary is inconsistent")
    if snapshot_record.get("paid_market_values") != _snapshot_paid_market_values(snapshot):
        raise ValueError("decision bundle paid market summary is inconsistent")

    metadata = _read_json(root / prediction_record["metadata"]["path"])
    _validate_prediction_metadata(
        root,
        metadata,
        target_date,
        locked.astimezone(BEIJING),
        verify_current_inputs=verify_current_inputs,
    )
    if prediction_record.get("generated_at_bjt") != metadata.get("generated_at_bjt"):
        raise ValueError("decision bundle prediction generation is inconsistent")
    if prediction_record.get("model_inputs") != metadata.get("model_inputs"):
        raise ValueError("decision bundle prediction inputs are inconsistent")
    metadata_prediction = metadata.get("predictions")
    if (
        not isinstance(metadata_prediction, dict)
        or any(
            prediction_record.get(key) != metadata_prediction.get(key)
            for key in ("path", "sha256", "bytes")
        )
    ):
        raise ValueError("decision bundle prediction artifact is inconsistent")

    fixtures = payload.get("fixture_extract")
    if not isinstance(fixtures, dict) or not isinstance(fixtures.get("rows"), list):
        raise ValueError("decision bundle fixture extract is invalid")
    if fixtures.get("sha256") != canonical_json_sha256(fixtures["rows"]):
        raise ValueError("decision bundle fixture extract digest mismatch")
    if fixtures.get("match_count") != len(fixtures["rows"]):
        raise ValueError("decision bundle fixture count mismatch")
    if fixtures != metadata.get("fixture_extract"):
        raise ValueError("decision bundle fixture extract differs from prediction metadata")
    if verify_current_inputs and fixtures["rows"] != canonical_fixture_rows(root, target_date):
        raise ValueError("decision bundle current fixtures differ")

    configurations = payload.get("configuration")
    if not isinstance(configurations, dict):
        raise ValueError("decision bundle configuration is invalid")
    for key, relative in (("betting", "betting_config.json"), ("prediction", "config.json")):
        record = configurations.get(key)
        if not isinstance(record, dict) or record.get("sha256") != canonical_json_sha256(record.get("payload")):
            raise ValueError(f"decision bundle {key} configuration digest mismatch")
        if verify_current_inputs and record["payload"] != _read_json(root / relative):
            raise ValueError(f"decision bundle current {key} configuration differs")

    histories = payload.get("history_inputs")
    if not isinstance(histories, dict):
        raise ValueError("decision bundle history inputs are invalid")
    for key in ("paid_history", "observation_history", "training_samples"):
        record = histories.get(key)
        if not isinstance(record, dict) or not isinstance(record.get("rows"), list):
            raise ValueError(f"decision bundle {key} is invalid")
        if record.get("sha256") != canonical_json_sha256(record["rows"]):
            raise ValueError(f"decision bundle {key} digest mismatch")
    account_metrics = histories.get("account_metrics")
    if (
        not isinstance(account_metrics, dict)
        or account_metrics.get("sha256")
        != canonical_json_sha256(account_metrics.get("payload"))
    ):
        raise ValueError("decision bundle account metrics are invalid")

    _validate_paid_plan_evidence(payload.get("paid_plan_evidence"), target_date)

    ratings = payload.get("ratings")
    model_code = payload.get("model_code")
    if not isinstance(ratings, dict) or not isinstance(model_code, dict):
        raise ValueError("decision bundle model input records are invalid")
    if ratings != metadata["model_inputs"].get("ratings"):
        raise ValueError("decision bundle ratings record is inconsistent")
    if model_code.get("predict_today.py") != metadata["model_inputs"].get(
        "prediction_code"
    ):
        raise ValueError("decision bundle prediction code record is inconsistent")
    if verify_current_inputs:
        _verify_file_record(root, ratings)
        for relative in MODEL_CODE_PATHS:
            _verify_file_record(root, model_code.get(relative))

    roles = payload.get("roles")
    if roles != {
        "paid_odds": "decision_snapshot",
        "paid_plan": "deterministic_bundle_evidence",
        "model_reference_inputs": list(MODEL_REFERENCE_INPUTS),
    }:
        raise ValueError("decision bundle odds role is invalid")

    predictions = _read_csv(root / prediction_record["path"], required=True)
    _validate_cross_artifact_identities(
        snapshot,
        predictions,
        fixtures["rows"],
        require_match_num=snapshot_is_live,
    )
    return payload


def _validate_prediction_metadata(
    root: Path,
    payload: object,
    target_date: date,
    locked_at: datetime,
    *,
    verify_current_inputs: bool,
) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != PREDICTION_METADATA_SCHEMA_VERSION:
        raise ValueError("prediction metadata schema is invalid")
    if payload.get("target_date") != target_date.isoformat():
        raise ValueError("prediction metadata target date is invalid")
    generated = _aware_datetime(payload.get("generated_at_bjt"), "prediction generated_at")
    if generated > locked_at:
        raise ValueError("prediction metadata was generated after lock")
    _verify_file_record(root, payload.get("predictions"))
    fixtures = payload.get("fixture_extract")
    if not isinstance(fixtures, dict) or not isinstance(fixtures.get("rows"), list):
        raise ValueError("prediction metadata fixture extract is invalid")
    if fixtures.get("sha256") != canonical_json_sha256(fixtures["rows"]):
        raise ValueError("prediction metadata fixture digest mismatch")
    if fixtures.get("match_count") != len(fixtures["rows"]):
        raise ValueError("prediction metadata fixture count mismatch")
    inputs = payload.get("model_inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "config", "fixtures", "ratings", "prediction_code"
    }:
        raise ValueError("prediction metadata model inputs are invalid")
    import_manifest = read_valid_import_manifest(root, target_date)
    for input_name in ("fixtures", "ratings"):
        manifest_record = import_manifest[input_name]
        if any(
            inputs[input_name].get(key) != manifest_record.get(key)
            for key in ("path", "sha256", "bytes")
        ):
            raise ValueError(
                f"prediction metadata {input_name} differ from import manifest"
            )
    if verify_current_inputs:
        for record in inputs.values():
            _verify_file_record(root, record)
        if fixtures["rows"] != canonical_fixture_rows(
            root,
            target_date,
            import_manifest=import_manifest,
        ):
            raise ValueError("prediction metadata current fixtures differ")


def _select_snapshot(
    root: Path,
    target_date: date,
    locked_at: datetime,
    *,
    import_manifest: dict,
    manifest_record: dict,
) -> tuple[Path, dict]:
    candidates = []
    for path in sorted(
        (root / "data" / "odds_snapshots").glob(
            f"{target_date.isoformat()}-*-decision.json"
        )
    ):
        try:
            payload = _read_json(path)
            captured = _validate_snapshot(payload, target_date, locked_at)
            _validate_snapshot_import(payload, import_manifest, manifest_record)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        candidates.append((captured, path, payload))
    if not candidates:
        raise ValueError("no valid decision snapshot exists at the bundle lock")
    _captured, path, payload = max(candidates, key=lambda item: (item[0], item[1].name))
    return path, payload


def _validate_snapshot_import(
    snapshot: dict,
    import_manifest: dict,
    manifest_record: dict,
) -> None:
    expected_record = {
        key: manifest_record[key] for key in ("path", "sha256", "bytes")
    }
    if snapshot.get("import_manifest") != expected_record:
        raise ValueError("decision snapshot import manifest is invalid")
    if snapshot.get("source") != import_manifest.get("source"):
        raise ValueError("decision snapshot import manifest source differs")
    imported_at = _aware_datetime(
        import_manifest.get("imported_at_bjt"), "import manifest imported_at"
    ).astimezone(BEIJING)
    captured_at = _aware_datetime(
        snapshot.get("captured_at"), "snapshot captured_at"
    ).astimezone(BEIJING)
    if imported_at > captured_at:
        raise ValueError("import timestamp must not follow snapshot capture")


def _validate_snapshot(
    payload: object,
    target_date: date,
    locked_at: datetime,
) -> datetime:
    if not isinstance(payload, dict):
        raise ValueError("decision snapshot must be a mapping")
    if payload.get("target_date") != target_date.isoformat():
        raise ValueError("decision snapshot date is invalid")
    if payload.get("capture_phase") != "decision":
        raise ValueError("decision snapshot phase is invalid")
    source = payload.get("source")
    if not isinstance(source, str) or source.lower() not in DOMESTIC_DECISION_SOURCES:
        raise ValueError("decision snapshot source is invalid")
    captured = _aware_datetime(payload.get("captured_at"), "snapshot captured_at").astimezone(BEIJING)
    if captured > locked_at.astimezone(BEIJING):
        raise ValueError("decision snapshot was captured after lock")
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise ValueError("decision snapshot matches are invalid")
    seen = set()
    for row in matches:
        if not isinstance(row, dict):
            raise ValueError("decision snapshot match is invalid")
        match_id = _canonical_match_id(row.get("match_id"))
        if match_id in seen:
            raise ValueError("decision snapshot match IDs are duplicated")
        seen.add(match_id)
        _required_text(row.get("team_a"), "snapshot team_a")
        _required_text(row.get("team_b"), "snapshot team_b")
        kickoff = _match_datetime(row.get("kickoff_at"), "snapshot kickoff_at")
        if locked_at.astimezone(BEIJING) >= kickoff:
            raise ValueError("decision bundle lock is not pre-kickoff")
        markets = row.get("markets")
        if not isinstance(markets, dict) or set(markets) != {"had", "hhad", "ttg"}:
            raise ValueError("decision snapshot markets are invalid")
        if any(not isinstance(markets[key], dict) for key in markets):
            raise ValueError("decision snapshot market values are invalid")
        eligibility = row.get("single_eligibility")
        if not isinstance(eligibility, dict) or set(eligibility) != {"had", "hhad", "ttg"}:
            raise ValueError("decision snapshot eligibility is invalid")
        if any(not isinstance(eligibility[key], bool) for key in eligibility):
            raise ValueError("decision snapshot eligibility values are invalid")
    return captured


def _validate_cross_artifact_identities(
    snapshot: dict,
    predictions: list[dict],
    fixtures: list[dict],
    *,
    require_match_num: bool | None = None,
) -> None:
    if require_match_num is None:
        require_match_num = snapshot.get("fetch_mode") == "live"

    def identities(rows: list[dict], label: str) -> dict[str, tuple[str, ...]]:
        result = {}
        for row in rows:
            match_id = _canonical_match_id(row.get("match_id"))
            if match_id in result:
                raise ValueError(f"duplicate {label} match identity")
            identity = (
                _required_text(row.get("team_a"), f"{label} team_a"),
                _required_text(row.get("team_b"), f"{label} team_b"),
                _match_datetime(row.get("kickoff_at"), f"{label} kickoff_at")
                .astimezone(BEIJING)
                .isoformat(),
            )
            if require_match_num:
                identity = (
                    _canonical_match_num(row.get("match_num"), f"{label} match_num"),
                    *identity,
                )
            result[match_id] = identity
        return result

    snapshot_ids = identities(snapshot["matches"], "snapshot")
    prediction_ids = identities(predictions, "prediction")
    fixture_ids = identities(fixtures, "fixture")
    if prediction_ids != fixture_ids:
        raise ValueError("decision bundle match identities differ across artifacts")

    expected_snapshot_ids = fixture_ids
    if snapshot.get("fetch_mode") == "live":
        captured_at = _aware_datetime(
            snapshot.get("captured_at"), "snapshot captured_at"
        ).astimezone(BEIJING)
        expected_snapshot_ids = {}
        for row in fixtures:
            kickoff_at = _match_datetime(row.get("kickoff_at"), "fixture kickoff_at")
            if kickoff_at > captured_at:
                match_id = _canonical_match_id(row.get("match_id"))
                expected_snapshot_ids[match_id] = fixture_ids[match_id]
    if snapshot_ids != expected_snapshot_ids:
        raise ValueError("decision bundle match identities differ across artifacts")


def _is_live_snapshot_path(root: Path, path: Path, target_date: date) -> bool:
    live_directory = (root / "data" / "live_odds_snapshots" / target_date.isoformat()).resolve()
    try:
        path.resolve().relative_to(live_directory)
    except ValueError:
        return False
    return True


def _validate_live_snapshot_at_lock(snapshot: dict, locked_at: datetime) -> None:
    matches = snapshot.get("matches")
    if not isinstance(matches, list):
        raise ValueError("live snapshot matches are invalid")
    locked = locked_at.astimezone(BEIJING)
    for row in matches:
        if not isinstance(row, dict):
            raise ValueError("live snapshot match is invalid")
        kickoff = _match_datetime(row.get("kickoff_at"), "live snapshot kickoff_at")
        if kickoff.astimezone(BEIJING) <= locked:
            raise ValueError("decision bundle lock is not pre-kickoff")


def _validate_prediction_rows(rows: list[dict], target_date: date) -> None:
    for row in rows:
        if row.get("date") != target_date.isoformat():
            raise ValueError("prediction row date differs from target date")


def _inline_rows(rows: list[dict]) -> dict:
    normalized = [
        {str(key): "" if value is None else str(value) for key, value in row.items()}
        for row in rows
    ]
    return {"rows": normalized, "sha256": canonical_json_sha256(normalized)}


def _inline_payload(payload: object) -> dict:
    return {"payload": payload, "sha256": canonical_json_sha256(payload)}


def _read_optional_json(path: Path) -> object:
    if not path.is_file():
        return {}
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_paid_plan_evidence(
    root: Path,
    target_date: date,
    locked_at: datetime,
    bundle: dict,
) -> dict:
    from generate_betting_plan import build_paid_plan_from_bundle, plan_csv_bytes

    plan = build_paid_plan_from_bundle(
        target_date,
        locked_at=locked_at,
        decision_bundle=bundle,
        root=root,
    )
    serialized = plan_csv_bytes(plan)
    with io.StringIO(serialized.decode("utf-8-sig"), newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {
        "schema_version": 1,
        "plan_sha256": hashlib.sha256(serialized).hexdigest(),
        "bytes": len(serialized),
        "row_count": len(rows),
        "rows": rows,
        "rows_sha256": canonical_json_sha256(rows),
    }


def _validate_paid_plan_evidence(record: object, target_date: date) -> None:
    from generate_betting_plan import plan_csv_bytes

    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("decision bundle paid plan evidence is invalid")
    rows = record.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("decision bundle paid plan evidence rows are invalid")
    if any(row.get("date") != target_date.isoformat() for row in rows):
        raise ValueError("decision bundle paid plan evidence date differs")
    serialized = plan_csv_bytes(rows)
    if (
        record.get("row_count") != len(rows)
        or record.get("rows_sha256") != canonical_json_sha256(rows)
        or record.get("bytes") != len(serialized)
        or record.get("plan_sha256") != hashlib.sha256(serialized).hexdigest()
    ):
        raise ValueError("decision bundle paid plan evidence digest mismatch")


def _snapshot_match_identities(snapshot: dict) -> list[dict]:
    return [
        {
            key: row.get(key, "")
            for key in ("match_id", "team_a", "team_b", "match_num", "kickoff_at")
        }
        for row in snapshot["matches"]
    ]


def _snapshot_paid_market_values(snapshot: dict) -> list[dict]:
    return [
        {
            "match_id": row["match_id"],
            "markets": row["markets"],
            "single_eligibility": row["single_eligibility"],
        }
        for row in snapshot["matches"]
    ]


def _file_record(root: Path, path: Path) -> dict:
    root = Path(root).resolve()
    path = Path(path).resolve()
    _require_within_root(root, path)
    if not path.is_file():
        raise ValueError(f"required bundle input is missing: {path.relative_to(root).as_posix()}")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _verify_file_record(root: Path, record: object) -> None:
    if not isinstance(record, dict):
        raise ValueError("decision bundle file record is invalid")
    relative = record.get("path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("decision bundle file path is invalid")
    path = (root / relative).resolve()
    _require_within_root(root, path)
    expected_hash = record.get("sha256")
    expected_bytes = record.get("bytes")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
        or not path.is_file()
    ):
        raise ValueError(f"decision bundle file record is invalid: {relative}")
    if path.stat().st_size != expected_bytes or sha256_file(path) != expected_hash:
        raise ValueError(f"decision bundle file hash mismatch: {relative}")


def _read_csv(path: Path, *, required: bool) -> list[dict]:
    from betting_ledger import resolve_ledger_path

    logical = Path(path)
    try:
        path = resolve_ledger_path(logical)
    except ValueError as exc:
        raise ValueError(f"CSV is invalid: {logical}") from exc
    if not path.is_file():
        if required:
            raise ValueError(f"required CSV is missing: {logical}")
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"CSV is invalid: {logical}") from exc


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _atomic_publish_json(path: Path, payload: dict) -> bool:
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


def _require_matching_existing_bundle(path: Path, payload: dict) -> None:
    try:
        existing = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("existing decision bundle is invalid") from exc
    if existing != payload:
        raise ValueError("existing conflicting decision bundle")


def _bundle_path(root: Path, target_date: date) -> Path:
    return Path(root) / "output" / f"decision_bundle_{target_date.isoformat()}.json"


def _canonical_match_id(value: object) -> str:
    text = _required_text(value, "match_id")
    if any(character.isspace() or not character.isprintable() for character in text):
        raise ValueError("match_id is not canonical")
    return text


def _canonical_match_num(value: object, name: str) -> str:
    text = _required_text(value, name)
    if any(character.isspace() or not character.isprintable() for character in text):
        raise ValueError(f"{name} is not canonical")
    return text


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty canonical text")
    return value


def _aware_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    else:
        raise ValueError(f"{name} must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _match_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def _require_within_root(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("decision bundle path escapes repository root") from exc


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        return _aware_datetime(value, "locked-at")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one immutable daily decision bundle.")
    parser.add_argument("--date", required=True, type=_parse_date)
    parser.add_argument("--locked-at", required=True, type=_parse_datetime)
    parser.add_argument("--decision-snapshot", type=Path)
    args = parser.parse_args()
    try:
        create_decision_bundle(
            Path.cwd(), args.date, args.locked_at, args.decision_snapshot
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
