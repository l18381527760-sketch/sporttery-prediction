import csv
import hashlib
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from betting_ledger import PENDING, resolve_ledger_path
from generate_betting_plan import StrategyOutputs
from provisional_plan import create_provisional_outputs
from revalidation import run_due_revalidation
from revalidation_reporting import publish_revalidation_report


BJT = timezone(timedelta(hours=8))
REPORT_DATE = date(2026, 7, 19)
KICKOFF = "2026-07-20T01:00:00+08:00"
PROVISIONAL_AT = datetime(2026, 7, 19, 14, 0, tzinfo=BJT)
T90_AT = datetime(2026, 7, 19, 23, 30, tzinfo=BJT)
T30_AT = datetime(2026, 7, 20, 0, 30, tzinfo=BJT)


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def plan_row():
    return {
        "date": REPORT_DATE.isoformat(),
        "strategy_version": "value-v4",
        "match_id": "match-1",
        "market_type": "had",
        "market_line": "",
        "selection": "h",
        "play": "HAD",
        "team_a": "Home",
        "team_b": "Away",
        "kickoff_local": KICKOFF,
        "odds": "2.50",
        "stake": "80",
        "conservative_probability": "0.50",
        "minimum_ev": "0.03",
        "odds_source": "sporttery",
        "odds_captured_at_bjt": "2026-07-19T13:45:00+08:00",
        "legs_json": "[]",
    }


def snapshot(captured_at, odds):
    minutes_to_kickoff = int(
        (datetime.fromisoformat(KICKOFF) - datetime.fromisoformat(captured_at)).total_seconds() // 60
    )
    if minutes_to_kickoff <= 45:
        capture_phase = "pre_kickoff_30"
    elif minutes_to_kickoff <= 105:
        capture_phase = "pre_kickoff_90"
    else:
        capture_phase = "monitoring"
    return {
        "schema_version": 2,
        "target_date": REPORT_DATE.isoformat(),
        "captured_at": captured_at,
        "source": "sporttery",
        "fetch_mode": "live",
        "capture_phase": "monitoring",
        "source_response_sha256": "0" * 64,
        "matches": [
            {
                "match_id": "match-1",
                "source_record_id": "source-1",
                "match_num": "Sunday001",
                "team_a": "Home",
                "team_b": "Away",
                "kickoff_at": KICKOFF,
                "sales_state": "Selling",
                "single_eligibility": {
                    "had": True,
                    "hhad": False,
                    "ttg": False,
                },
                "markets": {"had": {"h": odds}, "hhad": {}, "ttg": {}},
                "capture_phase": capture_phase,
                "minutes_to_kickoff": minutes_to_kickoff,
            }
        ],
    }


def write_snapshot(root, payload):
    raw = canonical_bytes(payload) + b"\n"
    captured = datetime.fromisoformat(payload["captured_at"])
    filename = (
        f"{captured.strftime('%Y%m%dT%H%M%S%z')}-sporttery-"
        f"{hashlib.sha256(raw).hexdigest()[:16]}.json"
    )
    path = (
        root
        / "data"
        / "live_odds_snapshots"
        / REPORT_DATE.isoformat()
        / filename
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def read_csv(path):
    with resolve_ledger_path(path).open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        return list(csv.DictReader(handle))


class PreKickoffCrossMidnightRehearsalTest(unittest.TestCase):
    def test_snapshot_helper_keeps_requested_phase_above_t90_window(self):
        payload = snapshot("2026-07-19T20:00:00+08:00", "2.50")

        self.assertEqual(300, payload["matches"][0]["minutes_to_kickoff"])
        self.assertEqual("monitoring", payload["matches"][0]["capture_phase"])

    def test_active_rehearsal_is_ordered_bounded_idempotent_and_cross_midnight_safe(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = {
                "mode": "active",
                "minimum_initial_minutes": 60,
                "t90_open_minutes": 105,
                "t90_close_minutes": 40,
                "t30_open_minutes": 40,
                "t30_close_minutes": 10,
                "scan_business_days": 2,
                "stake_unit": 2,
                "max_notification_days": 30,
                "reference_bankroll": 5000,
                "kelly_fraction": "0.25",
            }
            (root / "betting_config.json").write_text(
                json.dumps({"pre_kickoff_revalidation": settings}),
                encoding="utf-8",
            )

            initial_snapshot = snapshot("2026-07-19T13:45:00+08:00", "2.50")
            decision_bundle = {
                "schema_version": 3,
                "target_date": REPORT_DATE.isoformat(),
                "locked_at_bjt": "2026-07-19T13:50:00+08:00",
                "decision_snapshot": {
                    "path": "data/odds_snapshots/2026-07-19-134500-decision.json",
                    "sha256": "a" * 64,
                    "captured_at_bjt": initial_snapshot["captured_at"],
                    "payload": initial_snapshot,
                },
                "configuration": {
                    "betting": {"payload": {"value_strategy": {"min_ev": 0.03}}}
                },
            }
            row = plan_row()
            outputs = StrategyOutputs([row], [], [row], {})
            with patch(
                "provisional_plan.read_valid_decision_bundle",
                return_value=decision_bundle,
            ), patch(
                "provisional_plan.strategy_outputs_from_bundle",
                return_value=outputs,
            ):
                provisional = create_provisional_outputs(
                    root, REPORT_DATE, PROVISIONAL_AT, decision_bundle
                )

            by_route = {
                candidate["route"]: candidate
                for candidate in provisional["candidates"]
            }
            by_id = {
                candidate["candidate_id"]: candidate
                for candidate in provisional["candidates"]
            }
            self.assertEqual({"active", "shadow"}, set(by_route))
            self.assertEqual(PROVISIONAL_AT.isoformat(), provisional["generated_at_bjt"])

            snapshots = {
                T90_AT: snapshot("2026-07-19T23:30:00+08:00", "2.40"),
                T30_AT: snapshot("2026-07-20T00:30:00+08:00", "2.30"),
            }

            def provider(_root, target_date, checked_at):
                self.assertEqual(REPORT_DATE, target_date)
                return write_snapshot(root, snapshots[checked_at])

            with patch(
                "provisional_plan.read_valid_decision_bundle",
                return_value=decision_bundle,
            ):
                screened = run_due_revalidation(
                    root, T90_AT, target_dates=[REPORT_DATE], snapshot_provider=provider
                )
                confirmed = run_due_revalidation(
                    root, T30_AT, target_dates=[REPORT_DATE], snapshot_provider=provider
                )

            self.assertEqual({"screened"}, {item["state"] for item in screened})
            self.assertEqual({"confirmed"}, {item["state"] for item in confirmed})
            self.assertTrue(
                all(
                    item["receipt"]["final_stake"]
                    <= by_id[item["candidate_id"]]["provisional_stake"]
                    for item in confirmed
                )
            )

            active_id = by_route["active"]["candidate_id"]
            receipt_dir = (
                root / "output" / "revalidation_receipts" / REPORT_DATE.isoformat()
            )
            t90_receipt = json.loads(
                (receipt_dir / f"{active_id}-t90.json").read_text(encoding="utf-8")
            )
            t30_receipt = json.loads(
                (receipt_dir / f"{active_id}-t30.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "2026-07-19T23:30:00+08:00", t90_receipt["checked_at_bjt"]
            )
            self.assertEqual(
                "2026-07-20T00:30:00+08:00", t30_receipt["checked_at_bjt"]
            )
            self.assertNotEqual(
                t90_receipt["live_odds_snapshot_sha256"],
                t30_receipt["live_odds_snapshot_sha256"],
            )
            self.assertLessEqual(t30_receipt["final_stake"], t90_receipt["final_stake"])

            paid_rows = read_csv(root / "output" / "betting_ledger.csv")
            observation_rows = read_csv(root / "output" / "observation_ledger.csv")
            self.assertEqual(1, len(paid_rows))
            self.assertEqual(PENDING, paid_rows[0]["status"])
            self.assertEqual(active_id, paid_rows[0]["candidate_id"])
            self.assertGreater(float(paid_rows[0]["stake"]), 0)
            self.assertEqual(1, len(observation_rows))
            self.assertEqual("0.00", observation_rows[0]["stake"])

            status_path = (
                root
                / "web"
                / "revalidation"
                / REPORT_DATE.isoformat()
                / "status.json"
            )
            status = json.loads(status_path.read_text(encoding="utf-8"))
            prior_image = root / status["report_image_url"]
            prior_image_bytes = prior_image.read_bytes()
            before_retry = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with patch(
                "provisional_plan.read_valid_decision_bundle",
                return_value=decision_bundle,
            ):
                self.assertEqual(
                    [],
                    run_due_revalidation(
                        root,
                        T30_AT,
                        target_dates=[REPORT_DATE],
                        snapshot_provider=lambda *_args: self.fail(
                            "terminal retry must not capture a snapshot"
                        ),
                    ),
                )
            after_retry = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before_retry, after_retry)

            next_change = dict(status["changed_candidates"][0])
            next_change.update(
                {
                    "candidate_id": "next-day-cancelled",
                    "state": "cancelled",
                    "ledger_status": "not_applicable",
                    "final_stake": 0,
                    "reason": "rehearsal cancellation",
                }
            )
            publish_revalidation_report(
                root,
                REPORT_DATE + timedelta(days=1),
                [next_change],
                datetime(2026, 7, 20, 14, 0, tzinfo=BJT),
                "b" * 40,
            )
            self.assertEqual(prior_image_bytes, prior_image.read_bytes())


if __name__ == "__main__":
    unittest.main()
