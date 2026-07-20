import csv
import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import provisional_plan
from generate_betting_plan import StrategyOutputs
from provisional_plan import (
    candidate_from_plan_row,
    create_provisional_outputs,
    read_valid_provisional_state,
)


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 18)
GENERATED_AT = datetime(2026, 7, 18, 13, 30, tzinfo=BJT)


def canonical_bytes(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def plan_row(
    *,
    match_id="001",
    kickoff="2026-07-18T16:00:00+08:00",
    odds="3.10",
    stake="80",
    market_type="had",
    selection="h",
    legs=None,
):
    return {
        "date": DAY.isoformat(),
        "strategy_version": "value-v4",
        "match_id": match_id,
        "market_type": market_type,
        "market_line": "",
        "selection": selection,
        "play": market_type.upper(),
        "team_a": "Home",
        "team_b": "Away",
        "kickoff_local": kickoff,
        "odds": odds,
        "stake": stake,
        "conservative_probability": "0.50",
        "minimum_ev": "0.05",
        "legs_json": json.dumps(legs or [], sort_keys=True),
    }


def bundle(*rows):
    source_rows = list(rows) or [plan_row(), plan_row(match_id="002")]
    legs = []
    for row in source_rows:
        if row["market_type"] == "parlay":
            legs.extend(json.loads(row["legs_json"]))
        else:
            legs.append(row)
    matches = {}
    for index, row in enumerate(legs, start=1):
        match_id = row["match_id"]
        market_type = row["market_type"]
        line = row.get("market_line", row.get("line", ""))
        selection = row["selection"]
        market = {selection: "3.10"}
        if market_type == "hhad":
            market["goalLine"] = line
        match = matches.setdefault(match_id, {
            "match_id": match_id,
            "source_record_id": f"source-{match_id}",
            "match_num": f"Match{index:03d}",
            "team_a": row.get("team_a", "Home"),
            "team_b": row.get("team_b", "Away"),
            "kickoff_at": row.get("kickoff_at") or row.get("kickoff_local"),
            "sales_state": "Selling",
            "single_eligibility": {"had": True, "hhad": True, "ttg": True},
            "markets": {"had": {}, "hhad": {}, "ttg": {}},
        })
        match["markets"][market_type] = market
    return {
        "schema_version": 3,
        "target_date": DAY.isoformat(),
        "locked_at_bjt": "2026-07-18T13:20:00+08:00",
        "decision_snapshot": {
            "path": "data/odds_snapshots/2026-07-18-132000-decision.json",
            "sha256": "a" * 64,
            "captured_at_bjt": "2026-07-18T13:20:00+08:00",
            "payload": {
                "target_date": DAY.isoformat(),
                "captured_at": "2026-07-18T13:20:00+08:00",
                "capture_phase": "decision",
                "source": "sporttery",
                "matches": list(matches.values()),
            },
        },
        "configuration": {
            "betting": {"payload": {"value_strategy": {"min_ev": 0.05}}},
        },
    }


@contextmanager
def provisional_environment(outputs, valid_bundle=None):
    payload = valid_bundle or bundle()
    with patch(
        "provisional_plan.strategy_outputs_from_bundle", return_value=outputs
    ), patch(
        "provisional_plan.read_valid_decision_bundle", return_value=payload, create=True
    ):
        yield payload


def pointer_path(root):
    return root / "output" / f"provisional_generation_{DAY.isoformat()}.json"


def pointer_payload(root):
    return json.loads(pointer_path(root).read_text(encoding="utf-8"))


def artifact_path(root, key):
    return root / pointer_payload(root)["artifacts"][key]["path"]


def rewrite_artifact_and_pointer(root, key, content):
    path = artifact_path(root, key)
    path.write_bytes(content)
    pointer = pointer_payload(root)
    pointer["artifacts"][key]["sha256"] = hashlib.sha256(content).hexdigest()
    pointer["artifacts"][key]["bytes"] = len(content)
    pointer_path(root).write_bytes(canonical_bytes(pointer) + b"\n")


def write_state_and_pointer(root, state):
    rewrite_artifact_and_pointer(root, "state", canonical_bytes(state) + b"\n")


def candidate_csv_bytes(path, candidates):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        fields = csv.DictReader(handle).fieldnames
    rows = []
    for candidate in candidates:
        row = {field: candidate.get(field, "") for field in fields}
        row["candidate_payload_json"] = canonical_bytes(candidate).decode("utf-8")
        rows.append(row)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.seek(0)
        return b"\xef\xbb\xbf" + handle.read().encode("utf-8")


def attest(candidate):
    candidate["candidate_payload_sha256"] = hashlib.sha256(
        canonical_bytes({
            key: value
            for key, value in candidate.items()
            if key != "candidate_payload_sha256"
        })
    ).hexdigest()


def legacy_v1_generation(root, target_date, row, evidence, generated_at):
    """Publish the exact pre-execution-identity V1 generation shape."""
    candidate = candidate_from_plan_row(row, "active", target_date, 1)
    candidate.pop("schema_version", None)
    attest(candidate)
    decision_snapshot = evidence["decision_snapshot"]
    initial_candidate = {
        key: value
        for key, value in candidate.items()
        if key not in {
            "candidate_payload_sha256",
            "initial_candidate_attestation_sha256",
            "t90_receipt_path",
            "t90_receipt_sha256",
        }
    }
    candidate["initial_candidate_attestation_sha256"] = hashlib.sha256(
        canonical_bytes({
            "candidate_id": candidate["candidate_id"],
            "initial_candidate": initial_candidate,
            "decision_snapshot_sha256": hashlib.sha256(
                canonical_bytes(decision_snapshot)
            ).hexdigest(),
        })
    ).hexdigest()
    attest(candidate)

    generated_at_bjt = generated_at.astimezone(BJT).isoformat()
    bundle_sha256 = hashlib.sha256(canonical_bytes(evidence)).hexdigest()
    generation_id = hashlib.sha256(canonical_bytes({
        "report_date": target_date.isoformat(),
        "generated_at_bjt": generated_at_bjt,
        "decision_bundle_sha256": bundle_sha256,
        "initial_candidate_attestations": [
            candidate["initial_candidate_attestation_sha256"]
        ],
    })).hexdigest()
    generation_dir = (
        root / "output" / "provisional_generations" / target_date.isoformat()
        / generation_id
    )
    generation_dir.mkdir(parents=True)

    def csv_bytes(candidates):
        with tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=provisional_plan._CSV_FIELDS,
                lineterminator="\n",
            )
            writer.writeheader()
            for value in candidates:
                csv_row = {
                    field: value.get(field, "")
                    for field in provisional_plan._CSV_FIELDS
                }
                csv_row["candidate_payload_json"] = canonical_bytes(value).decode(
                    "utf-8"
                )
                writer.writerow(csv_row)
            handle.seek(0)
            return b"\xef\xbb\xbf" + handle.read().encode("utf-8")

    active_bytes = csv_bytes([candidate])
    shadow_bytes = csv_bytes([])
    state = {
        "schema_version": 1,
        "report_date": target_date.isoformat(),
        "generation_id": generation_id,
        "generated_at_bjt": generated_at_bjt,
        "decision_bundle_path": f"output/decision_bundle_{target_date.isoformat()}.json",
        "decision_bundle_sha256": bundle_sha256,
        "provisional_plan_sha256": hashlib.sha256(active_bytes).hexdigest(),
        "provisional_shadow_plan_sha256": hashlib.sha256(shadow_bytes).hexdigest(),
        "active_candidate_count": 1,
        "shadow_candidate_count": 0,
        "active_provisional_stake": candidate["provisional_stake"],
        "candidates": [candidate],
    }
    state_bytes = canonical_bytes(state) + b"\n"
    artifacts = {}
    for key, filename, content in (
        ("active_plan", f"provisional_betting_plan_{target_date.isoformat()}.csv", active_bytes),
        ("shadow_plan", f"provisional_shadow_plan_{target_date.isoformat()}.csv", shadow_bytes),
        ("state", f"revalidation_state_{target_date.isoformat()}.json", state_bytes),
    ):
        path = generation_dir / filename
        path.write_bytes(content)
        artifacts[key] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
    pointer = {
        "schema_version": 1,
        "report_date": target_date.isoformat(),
        "generation_id": generation_id,
        "decision_bundle_path": state["decision_bundle_path"],
        "decision_bundle_sha256": bundle_sha256,
        "artifacts": artifacts,
    }
    pointer_file = root / "output" / f"provisional_generation_{target_date.isoformat()}.json"
    pointer_file.write_bytes(canonical_bytes(pointer) + b"\n")
    return candidate


class ProvisionalPlanTest(unittest.TestCase):
    def test_candidate_id_is_stable_but_payload_digest_attests_odds_and_stake(self):
        first = candidate_from_plan_row(plan_row(odds="3.10", stake="80"), "active", DAY, 1)
        second = candidate_from_plan_row(plan_row(odds="3.20", stake="60"), "active", DAY, 1)
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertNotEqual(first["candidate_payload_sha256"], second["candidate_payload_sha256"])

    def test_active_and_shadow_routes_have_distinct_identities(self):
        active = candidate_from_plan_row(plan_row(), "active", DAY, 1)
        shadow = candidate_from_plan_row(plan_row(), "shadow", DAY, 1)
        self.assertNotEqual(active["candidate_id"], shadow["candidate_id"])
        self.assertEqual("active", active["route"])
        self.assertEqual("shadow", shadow["route"])

    def test_rejects_candidate_less_than_sixty_minutes_from_earliest_kickoff(self):
        row = plan_row(kickoff="2026-07-18T14:29:00+08:00")
        outputs = StrategyOutputs([row], [], [], {})
        evidence = bundle(row)
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs, evidence):
            with self.assertRaisesRegex(ValueError, "60 minutes"):
                create_provisional_outputs(Path(tmp), DAY, GENERATED_AT, evidence)

    def test_parlay_uses_earliest_leg_kickoff_for_initial_state(self):
        legs = [
            {"match_id": "002", "market_type": "had", "selection": "h", "kickoff_at": "2026-07-18T17:00:00+08:00"},
            {"match_id": "001", "market_type": "had", "selection": "a", "kickoff_at": "2026-07-18T14:45:00+08:00"},
        ]
        row = plan_row(market_type="parlay", match_id="", legs=legs)
        outputs = StrategyOutputs([row], [], [], {})
        evidence = bundle(row)
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs, evidence):
            create_provisional_outputs(Path(tmp), DAY, GENERATED_AT, evidence)
            candidate = read_valid_provisional_state(Path(tmp), DAY)["candidates"][0]
        self.assertEqual("2026-07-18T14:45:00+08:00", candidate["earliest_kickoff_at_bjt"])
        self.assertEqual("screened", candidate["state"])

    def test_duplicate_normalized_identity_fails(self):
        outputs = StrategyOutputs([plan_row(), plan_row()], [], [], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                create_provisional_outputs(Path(tmp), DAY, GENERATED_AT, bundle())

    def test_minimum_acceptable_odds_is_derived_from_minimum_ev_and_probability(self):
        candidate = candidate_from_plan_row(plan_row(), "active", DAY, 1)
        self.assertEqual("2.100000", candidate["minimum_acceptable_odds"])

    def test_fractional_provisional_stake_is_rejected_before_publication(self):
        with self.assertRaisesRegex(ValueError, "integral"):
            candidate_from_plan_row(plan_row(stake="80.5"), "active", DAY, 1)

    def test_t90_receipt_is_create_only_reused_and_non_circular(self):
        row = plan_row(kickoff="2026-07-18T14:45:00+08:00")
        outputs = StrategyOutputs([row], [], [], {})
        evidence = bundle(row)
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs, evidence) as valid:
            root = Path(tmp)
            first = create_provisional_outputs(root, DAY, GENERATED_AT, evidence)
            first_candidate = first["candidates"][0]
            receipt_path = root / first_candidate["t90_receipt_path"]
            receipt_bytes = receipt_path.read_bytes()

            second = create_provisional_outputs(
                root, DAY, GENERATED_AT + timedelta(minutes=1), evidence
            )
            receipt = json.loads(receipt_bytes)

            self.assertEqual(first, second)
            self.assertEqual(receipt_bytes, receipt_path.read_bytes())
            self.assertNotIn("candidate_payload_sha256", receipt)
            self.assertEqual(first_candidate["candidate_id"], receipt["candidate_id"])
            self.assertRegex(receipt["initial_candidate_attestation_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(valid["decision_snapshot"], receipt["decision_snapshot"])
            self.assertEqual(
                hashlib.sha256(receipt_bytes).hexdigest(),
                first_candidate["t90_receipt_sha256"],
            )

    def test_later_candidates_begin_provisional_with_zero_confirmed_stake(self):
        outputs = StrategyOutputs([plan_row()], [], [], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            create_provisional_outputs(Path(tmp), DAY, GENERATED_AT, bundle())
            candidate = read_valid_provisional_state(Path(tmp), DAY)["candidates"][0]
            self.assertEqual("provisional", candidate["state"])
            self.assertEqual(0, candidate["confirmed_stake"])
            self.assertEqual(80, candidate["provisional_stake"])

    def test_publication_binds_complete_execution_identity_from_decision_snapshot(self):
        row = plan_row()
        outputs = StrategyOutputs([row], [], [], {})
        evidence = bundle(row)
        unbound_id = candidate_from_plan_row(row, "active", DAY, 1)["candidate_id"]
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs, evidence):
            root = Path(tmp)
            state = create_provisional_outputs(root, DAY, GENERATED_AT, evidence)
            candidate = state["candidates"][0]
            with artifact_path(root, "active_plan").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                csv_payload = json.loads(
                    next(csv.DictReader(handle))["candidate_payload_json"]
                )

        self.assertEqual(unbound_id, candidate["candidate_id"])
        self.assertEqual(candidate, csv_payload)
        self.assertEqual(
            {
                "source", "source_record_id", "match_id", "match_num",
                "team_a", "team_b", "kickoff_at_bjt", "market_type",
                "market_line", "selection", "sales_state", "single_eligible",
            },
            set(candidate["execution_identity"]["legs"][0]),
        )
        self.assertIsNone(candidate["execution_identity"]["legs"][0]["market_line"])

    def test_new_publication_uses_execution_identity_bound_v2_schemas(self):
        row = plan_row()
        evidence = bundle(row)
        outputs = StrategyOutputs([row], [], [], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(
            outputs, evidence
        ):
            root = Path(tmp)
            state = create_provisional_outputs(root, DAY, GENERATED_AT, evidence)
            pointer = pointer_payload(root)

        self.assertEqual(2, pointer["schema_version"])
        self.assertEqual(2, state["schema_version"])
        self.assertEqual(2, state["candidates"][0]["schema_version"])

    def test_reader_adapts_exact_v1_generation_without_rewriting_published_bytes(self):
        row = plan_row()
        evidence = bundle(row)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "provisional_plan.read_valid_decision_bundle", return_value=evidence
        ):
            root = Path(tmp)
            published = legacy_v1_generation(root, DAY, row, evidence, GENERATED_AT)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            state = read_valid_provisional_state(root, DAY)

            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
        candidate = state["candidates"][0]
        self.assertEqual(1, state["schema_version"])
        self.assertEqual(published["candidate_payload_sha256"], candidate["candidate_payload_sha256"])
        self.assertNotIn("execution_identity", published)
        self.assertEqual("sporttery", candidate["execution_identity"]["legs"][0]["source"])
        self.assertEqual(before, after)

    def test_reader_rejects_execution_identity_forged_in_state_and_csv(self):
        outputs = StrategyOutputs([plan_row()], [], [], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            root = Path(tmp)
            create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
            state = json.loads(artifact_path(root, "state").read_text(encoding="utf-8"))
            active_path = artifact_path(root, "active_plan")
            candidate = state["candidates"][0]
            candidate["execution_identity"]["legs"][0]["match_num"] = "Forged999"
            attest(candidate)
            active_bytes = candidate_csv_bytes(active_path, [candidate])
            state["provisional_plan_sha256"] = hashlib.sha256(active_bytes).hexdigest()
            rewrite_artifact_and_pointer(root, "active_plan", active_bytes)
            write_state_and_pointer(root, state)

            with self.assertRaisesRegex(ValueError, "execution identity|provenance"):
                read_valid_provisional_state(root, DAY)

    def test_conflicting_reruns_cannot_add_candidates_or_raise_stake(self):
        changed_outputs = (
            StrategyOutputs([plan_row(), plan_row(match_id="002")], [], [], {}),
            StrategyOutputs([plan_row(stake="120")], [], [], {}),
        )
        for changed in changed_outputs:
            with self.subTest(changed=changed.active_plan), tempfile.TemporaryDirectory() as tmp, patch(
                "provisional_plan.read_valid_decision_bundle", return_value=bundle(), create=True
            ), patch("provisional_plan.strategy_outputs_from_bundle") as strategy:
                root = Path(tmp)
                strategy.return_value = StrategyOutputs([plan_row()], [], [], {})
                create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
                strategy.return_value = changed
                with self.assertRaisesRegex(ValueError, "conflicting provisional publication"):
                    create_provisional_outputs(
                        root, DAY, GENERATED_AT + timedelta(minutes=1), bundle()
                    )
                self.assertEqual(80, read_valid_provisional_state(root, DAY)["active_provisional_stake"])

    def test_failed_generation_is_invisible_and_identical_rerun_recovers(self):
        outputs = StrategyOutputs([plan_row()], [], [plan_row(match_id="002")], {})
        real_write = provisional_plan._write_create_only
        failed = False

        def fail_shadow_once(path, content):
            nonlocal failed
            if not failed and path.name.startswith("provisional_shadow_plan_"):
                failed = True
                raise OSError("shadow publication failed")
            real_write(path, content)

        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            root = Path(tmp)
            with patch("provisional_plan._write_create_only", side_effect=fail_shadow_once, create=True):
                with self.assertRaisesRegex(OSError, "shadow publication failed"):
                    create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
            with self.assertRaisesRegex(ValueError, "manifest|pointer|missing"):
                read_valid_provisional_state(root, DAY)

            recovered = create_provisional_outputs(root, DAY, GENERATED_AT, bundle())

            self.assertEqual(recovered, read_valid_provisional_state(root, DAY))
            self.assertTrue(pointer_path(root).is_file())

    def test_only_pointer_selected_generation_artifacts_are_public(self):
        outputs = StrategyOutputs([plan_row()], [], [plan_row(match_id="002")], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            root = Path(tmp)
            create_provisional_outputs(root, DAY, GENERATED_AT, bundle())

            pointer = pointer_payload(root)
            for key, filename in provisional_plan._ARTIFACT_FILENAMES.items():
                self.assertTrue((root / pointer["artifacts"][key]["path"]).is_file())
                self.assertFalse(
                    (root / "output" / filename.format(date=DAY)).exists()
                )

    def test_generation_requires_the_exact_validated_immutable_bundle(self):
        outputs = StrategyOutputs([plan_row()], [], [], {})
        supplied = bundle()
        supplied["decision_snapshot"]["payload"]["source"] = "unapproved"
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs, bundle()):
            with self.assertRaisesRegex(ValueError, "validated decision bundle"):
                create_provisional_outputs(Path(tmp), DAY, GENERATED_AT, supplied)

    def test_reader_validates_bundle_digest_and_provenance(self):
        outputs = StrategyOutputs([plan_row()], [], [], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            root = Path(tmp)
            create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
            changed = bundle()
            changed["locked_at_bjt"] = "2026-07-18T13:21:00+08:00"
            with patch(
                "provisional_plan.read_valid_decision_bundle",
                return_value=changed,
                create=True,
            ):
                with self.assertRaisesRegex(ValueError, "decision bundle"):
                    read_valid_provisional_state(root, DAY)

    def test_reader_rejects_state_candidate_not_joined_to_csv(self):
        outputs = StrategyOutputs([plan_row()], [], [], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            root = Path(tmp)
            create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
            state = json.loads(artifact_path(root, "state").read_text(encoding="utf-8"))
            state["candidates"][0]["provisional_stake"] = 500
            attest(state["candidates"][0])
            state["active_provisional_stake"] = 500
            write_state_and_pointer(root, state)

            with self.assertRaisesRegex(ValueError, "join|CSV"):
                read_valid_provisional_state(root, DAY)

    def test_reader_rejects_duplicate_ids_wrong_routes_and_fractional_stake(self):
        mutations = ("duplicate", "route", "fractional", "probability")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp, provisional_environment(
                StrategyOutputs([plan_row()], [], [], {})
            ):
                root = Path(tmp)
                create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
                state = json.loads(artifact_path(root, "state").read_text(encoding="utf-8"))
                active_path = artifact_path(root, "active_plan")
                candidates = [dict(state["candidates"][0])]
                if mutation == "duplicate":
                    candidates.append(dict(candidates[0]))
                    state["candidates"].append(dict(candidates[0]))
                elif mutation == "route":
                    candidates[0]["route"] = "shadow"
                    state["candidates"][0]["route"] = "shadow"
                    stable_identity = {
                        "report_date": DAY.isoformat(),
                        "route": "shadow",
                        "strategy_version": candidates[0]["strategy_version"],
                        "market": candidates[0]["normalized_market_identity"],
                    }
                    forged_id = "candidate-" + hashlib.sha256(
                        canonical_bytes(stable_identity)
                    ).hexdigest()
                    candidates[0]["candidate_id"] = forged_id
                    state["candidates"][0]["candidate_id"] = forged_id
                    attest(candidates[0])
                    attest(state["candidates"][0])
                elif mutation == "fractional":
                    candidates[0]["provisional_stake"] = 80.5
                    state["candidates"][0]["provisional_stake"] = 80.5
                    attest(candidates[0])
                    attest(state["candidates"][0])
                    state["active_provisional_stake"] = 80.5
                else:
                    candidates[0]["conservative_probability"] = "0"
                    state["candidates"][0]["conservative_probability"] = "0"
                    attest(candidates[0])
                    attest(state["candidates"][0])
                active_bytes = candidate_csv_bytes(active_path, candidates)
                state["provisional_plan_sha256"] = hashlib.sha256(active_bytes).hexdigest()
                rewrite_artifact_and_pointer(root, "active_plan", active_bytes)
                write_state_and_pointer(root, state)

                with self.assertRaisesRegex(
                    ValueError, "duplicate|route|integral|probability"
                ):
                    read_valid_provisional_state(root, DAY)

    def test_reader_parses_and_validates_candidate_payload_json(self):
        outputs = StrategyOutputs([plan_row()], [], [], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            root = Path(tmp)
            create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
            active_path = artifact_path(root, "active_plan")
            with active_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                rows = list(reader)
            rows[0]["candidate_payload_json"] = "{"
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
                handle.seek(0)
                active_bytes = b"\xef\xbb\xbf" + handle.read().encode("utf-8")
            state = json.loads(artifact_path(root, "state").read_text(encoding="utf-8"))
            state["provisional_plan_sha256"] = hashlib.sha256(active_bytes).hexdigest()
            rewrite_artifact_and_pointer(root, "active_plan", active_bytes)
            write_state_and_pointer(root, state)

            with self.assertRaisesRegex(ValueError, "candidate payload JSON"):
                read_valid_provisional_state(root, DAY)

    def test_all_persisted_bjt_timestamps_are_normalized_to_shanghai(self):
        row = plan_row(kickoff="2026-07-18T07:00:00+00:00")
        row["odds_captured_at_bjt"] = "2026-07-18T05:20:00+00:00"
        outputs = StrategyOutputs([row], [], [], {})
        generated_utc = datetime(2026, 7, 18, 5, 30, tzinfo=timezone.utc)
        evidence = bundle(row)
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs, evidence):
            root = Path(tmp)
            state = create_provisional_outputs(root, DAY, generated_utc, evidence)
            candidate = state["candidates"][0]
            receipt = json.loads((root / candidate["t90_receipt_path"]).read_text(encoding="utf-8"))

            self.assertEqual("2026-07-18T13:30:00+08:00", state["generated_at_bjt"])
            self.assertEqual("2026-07-18T15:00:00+08:00", candidate["earliest_kickoff_at_bjt"])
            self.assertEqual(
                "2026-07-18T13:20:00+08:00",
                candidate["source_plan_row"]["odds_captured_at_bjt"],
            )
            self.assertEqual("2026-07-18T13:30:00+08:00", receipt["generated_at_bjt"])

    def test_generation_does_not_modify_the_betting_ledger(self):
        outputs = StrategyOutputs([plan_row()], [], [plan_row(match_id="002")], {})
        with tempfile.TemporaryDirectory() as tmp, provisional_environment(outputs):
            root = Path(tmp)
            ledger = root / "output" / "betting_ledger.csv"
            ledger.parent.mkdir()
            ledger.write_bytes(b"immutable ledger bytes\n")
            before = ledger.read_bytes()
            create_provisional_outputs(root, DAY, GENERATED_AT, bundle())
            self.assertEqual(before, ledger.read_bytes())
            self.assertFalse((root / "output" / f"plan_lock_{DAY.isoformat()}.json").exists())


if __name__ == "__main__":
    unittest.main()
