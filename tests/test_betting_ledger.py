import copy
import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import betting_ledger as ledger_module
from betting_ledger import (
    ABNORMAL,
    LOST,
    PENDING,
    REFUNDED,
    WON,
    ingest_date,
    ingest_locked_plan,
    settle_pending,
    stable_bet_id,
    write_ledger_atomic,
)
from official_markets import THREE_WAY_SELECTIONS, TOTAL_GOALS_SELECTIONS
from plan_lock import sha256_file


BJT = timezone(timedelta(hours=8))
LOCKED_AT = datetime(2026, 7, 16, 13, 31, tzinfo=BJT)
SETTLED_AT = datetime(2026, 7, 17, 12, 0, tzinfo=BJT)


def plan_row(**overrides):
    row = {
        "date": "2026-07-16",
        "strategy_version": "value-v4",
        "model_version": "model-3",
        "match_id": "1001",
        "team_a": "甲队",
        "team_b": "乙队",
        "kickoff_local": "2026-07-16T20:00:00+08:00",
        "play": "HAD",
        "market_type": "had",
        "market_line": "",
        "selection": "胜",
        "odds": "2.00",
        "locked_odds": "2.00",
        "odds_source": "sporttery",
        "odds_source_record_id": "odds-1001",
        "odds_captured_at_bjt": "2026-07-16T13:30:00+08:00",
        "raw_probability": "0.54",
        "calibrated_probability": "0.53",
        "official_market_probability": "0.50",
        "conservative_probability": "0.51",
        "edge": "0.01",
        "net_ev": "0.02",
        "full_kelly": "0.02",
        "kelly_fraction": "0.25",
        "data_quality_multiplier": "1.0",
        "volatility_multiplier": "1.0",
        "performance_multiplier": "1.0",
        "portfolio_rank": "1",
        "binding_limits": "daily",
        "stake": "20",
        "data_quality": "high",
        "volatility_band": "low",
    }
    row.update(overrides)
    return row


def legacy_parlay_row(**overrides):
    row = {
        "date": "2026-07-16",
        "strategy_version": "legacy-v1",
        "match": "甲队 vs 乙队",
        "play": "2-leg parlay",
        "market_type": "parlay",
        "selection": "旧串关展示",
        "market_line": "",
        "odds": "4.20",
        "stake": "10",
        "legacy_note": "preserve",
    }
    row.update(overrides)
    return row


def lock(**overrides):
    payload = {
        "report_date": "2026-07-16",
        "locked_at_bjt": LOCKED_AT.isoformat(),
        "plan_sha256": "a" * 64,
        "odds_source": "sporttery",
    }
    payload.update(overrides)
    return payload


def finished(match_id, home, away, source_record_id=None):
    return {
        "match_id": match_id,
        "result_status": "finished",
        "home_goals": str(home),
        "away_goals": str(away),
        "result_source": "sporttery",
        "source_record_id": source_record_id or f"result-{match_id}",
        "captured_at_bjt": "2026-07-17T11:00:00+08:00",
    }


def v4_leg(match_id, market_type, selection, odds):
    return {
        "match_id": match_id,
        "market_type": market_type,
        "selection": selection,
        "line": "",
        "odds": odds,
        "odds_source": "sporttery",
        "odds_source_record_id": f"odds-{match_id}-{market_type}",
        "odds_captured_at_bjt": "2026-07-16T13:30:00+08:00",
    }


def v4_parlay_row(prefix="parlay", stake="10"):
    legs = [
        v4_leg(f"{prefix}-1", "had", "胜", "2.00"),
        v4_leg(f"{prefix}-2", "ttg", "2球", "3.00"),
    ]
    return plan_row(
        play="PARLAY",
        market_type="parlay",
        match_id="",
        odds="6.00",
        locked_odds="6.00",
        stake=stake,
        legs_json=json.dumps(legs, ensure_ascii=False),
    )


class IdentityAndIngestionTest(unittest.TestCase):
    def test_legacy_v3_new_rows_share_canonical_market_and_parlay_validation(self):
        valid_single = plan_row(
            strategy_version="legacy-v3",
            play="legacy display label",
            kelly_fraction="",
        )
        valid_parlay = v4_parlay_row("legacy-valid")
        valid_parlay.update(
            strategy_version="legacy-v3",
            play="legacy combo display",
            kelly_fraction="",
        )
        self.assertEqual(1, len(ingest_locked_plan([], [valid_single], lock())))
        legacy_display_only = {**valid_single, "play": "2-leg parlay display"}
        self.assertEqual(
            1, len(ingest_locked_plan([], [legacy_display_only], lock()))
        )
        self.assertEqual(1, len(ingest_locked_plan([], [valid_parlay], lock())))

        def changed_parlay(change):
            row = copy.deepcopy(valid_parlay)
            legs = json.loads(row["legs_json"])
            change(row, legs)
            row["legs_json"] = json.dumps(legs, ensure_ascii=False)
            return row

        cases = (
            (
                "score single",
                plan_row(
                    strategy_version="legacy-v3",
                    play="legacy score",
                    market_type="score",
                    selection="1-0",
                    kelly_fraction="",
                ),
                "market",
            ),
            (
                "half full single",
                plan_row(
                    strategy_version="legacy-v3",
                    play="legacy half full",
                    market_type="half_full",
                    selection="win-win",
                    kelly_fraction="",
                ),
                "market",
            ),
            (
                "unsupported single selection",
                {**valid_single, "selection": "1-0"},
                "selection",
            ),
            (
                "same match parlay",
                changed_parlay(
                    lambda _row, legs: legs[1].update(match_id=legs[0]["match_id"])
                ),
                "distinct",
            ),
            (
                "unsupported parlay leg",
                changed_parlay(
                    lambda _row, legs: legs[1].update(
                        market_type="score", selection="1-0"
                    )
                ),
                "market",
            ),
            (
                "invalid handicap line",
                changed_parlay(
                    lambda _row, legs: legs[1].update(
                        market_type="hhad",
                        selection=THREE_WAY_SELECTIONS["h"],
                        line="+0.5",
                    )
                ),
                "integer handicap",
            ),
            (
                "missing leg source record",
                changed_parlay(
                    lambda _row, legs: legs[1].update(odds_source_record_id="")
                ),
                "record",
            ),
            (
                "naive leg capture",
                changed_parlay(
                    lambda _row, legs: legs[1].update(
                        odds_captured_at_bjt="2026-07-16T13:30:00"
                    )
                ),
                "timezone",
            ),
            (
                "wrong leg source",
                changed_parlay(
                    lambda _row, legs: legs[1].update(odds_source="zgzcw")
                ),
                "source",
            ),
            (
                "tampered leg price",
                changed_parlay(
                    lambda _row, legs: legs[1].update(odds="3.10")
                ),
                "product",
            ),
            (
                "tampered combined price",
                changed_parlay(
                    lambda row, _legs: row.update(odds="6.01", locked_odds="6.01")
                ),
                "product",
            ),
        )
        for name, row, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                ingest_locked_plan([], [row], lock())

        second_parlay = v4_parlay_row("legacy-second")
        second_parlay.update(
            strategy_version="legacy-v3",
            play="legacy combo display",
            kelly_fraction="",
        )
        with self.assertRaisesRegex(ValueError, "parlay count"):
            ingest_locked_plan([], [valid_parlay, second_parlay], lock())

    def test_existing_canonical_rows_dedupe_by_derived_identity_and_preserve_first(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "bet_id": "spoofed-first"}
        equivalent = {**canonical, "bet_id": "spoofed-second"}

        deduplicated = ingest_locked_plan([first, equivalent], [], lock())

        self.assertEqual([first], deduplicated)
        rerun = ingest_locked_plan([first, equivalent], [plan_row()], lock())
        self.assertEqual([first], rerun)

    def test_existing_canonical_empty_date_uses_report_date_for_dedupe(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "date": "", "bet_id": "spoofed-first"}
        equivalent = {**canonical, "date": "", "bet_id": "spoofed-second"}

        deduplicated = ingest_locked_plan([first, equivalent], [], lock())
        self.assertEqual([first], deduplicated)

        rerun = ingest_locked_plan([first, equivalent], [plan_row()], lock())
        self.assertEqual([first], rerun)

    def test_existing_canonical_empty_date_conflict_fails_closed(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "date": "", "bet_id": "spoofed-first"}
        conflict = {
            **canonical,
            "date": "",
            "bet_id": "spoofed-second",
            "stake": "22",
        }

        with self.assertRaisesRegex(ValueError, "conflicting existing canonical"):
            ingest_locked_plan([first, conflict], [], lock())

    def test_stable_identity_canonicalizes_whitespace_and_compact_dates(self):
        expected = stable_bet_id(plan_row())
        aliases = (
            plan_row(date="   ", report_date=" 2026-07-16 "),
            plan_row(date=" 2026-07-16 "),
            plan_row(date="20260716"),
        )

        actual = []
        for row in aliases:
            try:
                actual.append(stable_bet_id(row))
            except ValueError:
                actual.append("invalid")
        self.assertEqual([expected] * len(aliases), actual)

    def test_existing_canonical_date_aliases_dedupe_or_conflict_by_values(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "bet_id": "spoofed-first"}
        aliases = (
            {**canonical, "date": "   ", "bet_id": "spoofed-whitespace"},
            {**canonical, "date": "20260716", "bet_id": "spoofed-compact"},
        )

        for alias in aliases:
            with self.subTest(date=alias["date"]):
                self.assertEqual(
                    [first], ingest_locked_plan([first, alias], [], lock())
                )
                conflict = {**alias, "stake": "22"}
                with self.assertRaisesRegex(
                    ValueError, "conflicting existing canonical"
                ):
                    ingest_locked_plan([first, conflict], [], lock())

    def test_settle_ledger_dedupes_canonical_date_aliases_before_profit(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "bet_id": "spoofed-first"}
        aliases = (
            {**canonical, "date": "   ", "bet_id": "spoofed-whitespace"},
            {**canonical, "date": "20260716", "bet_id": "spoofed-compact"},
        )

        for alias in aliases:
            with self.subTest(date=alias["date"]), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                ledger_path = root / "output" / "betting_ledger.csv"
                write_ledger_atomic(ledger_path, [first, alias])

                ledger_module.settle_ledger(
                    root, {"1001": finished("1001", 2, 1)}, SETTLED_AT
                )

                with ledger_path.open(
                    encoding="utf-8-sig", newline=""
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(1, len(rows))
                self.assertEqual("spoofed-first", rows[0]["bet_id"])
                self.assertEqual("20.00", rows[0]["profit"])

    def test_hhad_single_line_aliases_share_identity_dedupe_and_settle_once(self):
        plus = plan_row(
            play="HHAD",
            market_type="hhad",
            market_line="+1",
            selection=THREE_WAY_SELECTIONS["h"],
        )
        plain = {**plus, "market_line": "1"}
        self.assertEqual(stable_bet_id(plus), stable_bet_id(plain))

        first = {
            **ingest_locked_plan([], [plus], lock())[0],
            "bet_id": "spoofed-first",
        }
        equivalent = {
            **ingest_locked_plan([], [plain], lock())[0],
            "bet_id": "spoofed-second",
        }
        self.assertEqual(
            [first], ingest_locked_plan([first, equivalent], [], lock())
        )
        with self.assertRaisesRegex(ValueError, "conflicting existing canonical"):
            ingest_locked_plan(
                [first, {**equivalent, "stake": "22"}], [], lock()
            )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "output" / "betting_ledger.csv"
            write_ledger_atomic(ledger_path, [first, equivalent])
            ledger_module.settle_ledger(
                root, {"1001": finished("1001", 1, 1)}, SETTLED_AT
            )
            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(1, len(rows))
        self.assertEqual("20.00", rows[0]["profit"])

    def test_hhad_parlay_line_aliases_share_identity_dedupe_and_settle_once(self):
        plus = v4_parlay_row("hhad-alias")
        plus_legs = json.loads(plus["legs_json"])
        plus_legs[0].update(
            market_type="hhad",
            selection=THREE_WAY_SELECTIONS["h"],
            line="+1",
        )
        plus["legs_json"] = json.dumps(plus_legs, ensure_ascii=False)
        plain = copy.deepcopy(plus)
        plain_legs = json.loads(plain["legs_json"])
        plain_legs[0]["line"] = "1"
        plain["legs_json"] = json.dumps(plain_legs, ensure_ascii=False)
        self.assertEqual(stable_bet_id(plus), stable_bet_id(plain))

        first = {
            **ingest_locked_plan([], [plus], lock())[0],
            "bet_id": "spoofed-first",
        }
        equivalent = {
            **ingest_locked_plan([], [plain], lock())[0],
            "bet_id": "spoofed-second",
        }
        self.assertEqual(
            [first], ingest_locked_plan([first, equivalent], [], lock())
        )
        with self.assertRaisesRegex(ValueError, "conflicting existing canonical"):
            ingest_locked_plan(
                [first, {**equivalent, "stake": "12"}], [], lock()
            )

        results = {
            "hhad-alias-1": finished("hhad-alias-1", 1, 1),
            "hhad-alias-2": finished("hhad-alias-2", 2, 0),
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "output" / "betting_ledger.csv"
            write_ledger_atomic(ledger_path, [first, equivalent])
            ledger_module.settle_ledger(root, results, SETTLED_AT)
            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(1, len(rows))
        self.assertEqual("50.00", rows[0]["profit"])

    def test_existing_canonical_duplicate_conflicts_fail_closed(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "bet_id": "spoofed-first"}
        conflicts = (
            {**canonical, "bet_id": "spoofed-second", "stake": "22"},
            {
                **canonical,
                "bet_id": "spoofed-second",
                "status": WON,
                "result_status": "finished",
                "result_source": "sporttery",
                "source_record_id": "result-conflict",
                "captured_at_bjt": "2026-07-17T11:00:00+08:00",
                "home_goals": "2",
                "away_goals": "1",
                "settled_at_bjt": SETTLED_AT.isoformat(),
                "return": "40.00",
                "profit": "20.00",
            },
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict), self.assertRaisesRegex(
                ValueError, "conflicting existing canonical"
            ):
                ingest_locked_plan([first, conflict], [], lock())

    def test_existing_equivalent_duplicates_do_not_double_count_caps_or_profit(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {
            **canonical,
            "bet_id": "spoofed-first",
            "status": LOST,
            "result_status": "finished",
            "result_source": "sporttery",
            "source_record_id": "result-loss",
            "captured_at_bjt": "2026-07-17T11:00:00+08:00",
            "home_goals": "0",
            "away_goals": "1",
            "settled_at_bjt": SETTLED_AT.isoformat(),
            "return": "0.00",
            "profit": "-2500.00",
        }
        equivalent = {**first, "bet_id": "spoofed-second"}

        ingested = ingest_locked_plan(
            [first, equivalent],
            [plan_row(match_id="account-new", stake="20")],
            lock(),
        )

        self.assertEqual(2, len(ingested))
        self.assertEqual(first, ingested[0])

    def test_new_paid_rows_reject_invalid_boundary_fields_and_versions(self):
        valid = plan_row()
        cases = (
            ("unknown version", [{**valid, "strategy_version": "value-v5"}], "strategy_version"),
            ("zero stake", [{**valid, "stake": "0"}], "positive"),
            ("nonfinite stake", [{**valid, "stake": "NaN"}], "stake"),
            ("wrong stake unit", [{**valid, "stake": "3"}], "2-yuan"),
            ("unsupported source", [{**valid, "odds_source": "external"}], "source"),
            ("lock source mismatch", [{**valid, "odds_source": "zgzcw"}], "source"),
            ("missing source record", [{**valid, "odds_source_record_id": ""}], "record"),
            ("naive capture", [{**valid, "odds_captured_at_bjt": "2026-07-16T13:30:00"}], "timezone"),
            ("nonfinite odds", [{**valid, "odds": "NaN"}], "odds"),
            ("invalid locked odds", [{**valid, "locked_odds": "1.00"}], "locked_odds"),
            ("single price mismatch", [{**valid, "odds": "2.01"}], "equal"),
            ("zero Kelly", [{**valid, "kelly_fraction": "0"}], "Kelly"),
            ("excess Kelly", [{**valid, "kelly_fraction": "0.26"}], "Kelly"),
            ("nonfinite Kelly", [{**valid, "kelly_fraction": "NaN"}], "Kelly"),
            ("duplicate identity", [valid, copy.deepcopy(valid)], "duplicate"),
        )
        for name, rows, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                ingest_locked_plan([], rows, lock())

        legacy = plan_row(strategy_version="legacy-v3", kelly_fraction="")
        self.assertEqual(1, len(ingest_locked_plan([], [legacy], lock())))

    def test_new_paid_rows_enforce_daily_monthly_loss_and_portfolio_caps(self):
        def history(
            bet_id,
            *,
            row_date="2026-07-16",
            stake="0",
            market_type="historical",
            match_id="history",
            status=PENDING,
            profit="0.00",
        ):
            return {
                "bet_id": bet_id,
                "date": row_date,
                "strategy_version": "historical-v1",
                "play": "historical",
                "market_type": market_type,
                "match_id": match_id,
                "selection": "historical",
                "stake": stake,
                "status": status,
                "profit": profit,
            }

        cap_cases = (
            (
                "daily stake",
                [history("daily", stake="490")],
                [plan_row(match_id="daily-new", stake="20")],
                "daily",
            ),
            (
                "monthly stake",
                [history("monthly", row_date="2026-07-01", stake="4990")],
                [plan_row(match_id="monthly-new", stake="20")],
                "monthly",
            ),
            (
                "monthly stop loss",
                [history(
                    "loss", row_date="2026-07-01", stake="2", status=LOST, profit="-5000"
                )],
                [plan_row(match_id="loss-new", stake="20")],
                "stop loss",
            ),
            (
                "match exposure",
                [history("match", stake="190", market_type="had", match_id="1001")],
                [plan_row(match_id="1001", stake="20")],
                "match exposure",
            ),
            (
                "canonical existing parlay match exposure",
                [
                    {
                        **history(
                            "canonical-parlay",
                            stake="30",
                            market_type="parlay",
                            match_id="",
                        ),
                        "canonical_legs_json": json.dumps([
                            {
                                "match_id": "shared-match",
                                "market_type": "had",
                                "selection": THREE_WAY_SELECTIONS["h"],
                                "line": "",
                            },
                            {
                                "match_id": "other-match",
                                "market_type": "ttg",
                                "selection": TOTAL_GOALS_SELECTIONS["s2"],
                                "line": "",
                            },
                        ], ensure_ascii=False),
                    },
                    history(
                        "shared-single",
                        stake="160",
                        market_type="had",
                        match_id="shared-match",
                    ),
                ],
                [plan_row(match_id="shared-match", stake="20")],
                "match exposure",
            ),
            ("parlay stake", [], [v4_parlay_row("large", stake="32")], "parlay stake"),
            (
                "parlay count",
                [],
                [v4_parlay_row("first"), v4_parlay_row("second")],
                "parlay count",
            ),
            (
                "single count",
                [],
                [plan_row(match_id=f"single-{index}", stake="2") for index in range(3)],
                "single count",
            ),
            (
                "single stake",
                [],
                [plan_row(match_id="single-large", stake="202")],
                "single stake",
            ),
            (
                "single budget",
                [],
                [
                    plan_row(match_id="single-a", stake="102"),
                    plan_row(match_id="single-b", stake="102"),
                ],
                "single budget",
            ),
        )
        for name, existing, rows, message in cap_cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                ingest_locked_plan(existing, rows, lock())

        stale_month = [
            history(
                "stale-month",
                row_date="2026-06-30",
                stake="5000",
                status=LOST,
                profit="-5000",
            )
        ]
        self.assertEqual(
            2,
            len(ingest_locked_plan(
                stale_month,
                [plan_row(match_id="current-month", stake="20")],
                lock(),
            )),
        )

    def test_idempotent_rerun_at_allocation_and_monthly_caps_adds_no_exposure(self):
        prior = {
            "bet_id": "prior-month-stake",
            "date": "2026-07-01",
            "strategy_version": "historical-v1",
            "play": "historical",
            "market_type": "historical",
            "match_id": "prior",
            "selection": "historical",
            "stake": "4770",
            "status": PENDING,
            "profit": "0.00",
        }
        plan = [
            plan_row(match_id="cap-single-1", stake="100"),
            plan_row(match_id="cap-single-2", stake="100"),
            v4_parlay_row("cap-parlay", stake="30"),
        ]

        first = ingest_locked_plan([prior, copy.deepcopy(prior)], plan, lock())
        rerun = ingest_locked_plan(first, plan, lock())

        self.assertEqual(first, rerun)
        self.assertEqual(4, len(rerun))
        self.assertEqual(Decimal("5000"), sum(Decimal(row["stake"]) for row in rerun))

    def test_value_v4_parlay_requires_leg_evidence_and_exact_decimal_product(self):
        legs = [
            v4_leg("1001", "had", "胜", "2.00"),
            v4_leg("1002", "ttg", "2球", "3.00"),
        ]
        row = plan_row(
            play="PARLAY",
            market_type="parlay",
            odds="6.00",
            locked_odds="6.00",
            legs_json=json.dumps(legs, ensure_ascii=False),
        )

        self.assertEqual(1, len(ingest_locked_plan([], [row], lock())))
        identity_variant = copy.deepcopy(row)
        identity_legs = json.loads(identity_variant["legs_json"])
        identity_legs[1].update({
            "odds": "9.99",
            "odds_source": "zgzcw",
            "odds_source_record_id": "changed-record",
            "odds_captured_at_bjt": "2026-07-16T13:31:00+08:00",
        })
        identity_variant["legs_json"] = json.dumps(identity_legs, ensure_ascii=False)
        identity_variant["odds"] = "19.98"
        identity_variant["locked_odds"] = "19.98"
        self.assertEqual(stable_bet_id(row), stable_bet_id(identity_variant))

        invalid_rows = []
        for name, field, value in (
            ("inconsistent domestic source", "odds_source", "zgzcw"),
            ("unsupported source", "odds_source", "external"),
            ("missing record", "odds_source_record_id", ""),
            ("naive capture", "odds_captured_at_bjt", "2026-07-16T13:30:00"),
            ("tampered leg odds", "odds", "3.01"),
            ("nonfinite leg odds", "odds", "NaN"),
            ("invalid leg odds", "odds", "1.00"),
        ):
            changed = copy.deepcopy(row)
            changed_legs = json.loads(changed["legs_json"])
            changed_legs[1][field] = value
            changed["legs_json"] = json.dumps(changed_legs, ensure_ascii=False)
            invalid_rows.append((name, changed))
        missing_odds = copy.deepcopy(row)
        missing_legs = json.loads(missing_odds["legs_json"])
        del missing_legs[1]["odds"]
        missing_odds["legs_json"] = json.dumps(missing_legs, ensure_ascii=False)
        invalid_rows.append(("missing leg odds", missing_odds))
        combined = {**row, "odds": "6.01", "locked_odds": "6.01"}
        invalid_rows.append(("tampered combined odds", combined))

        for name, invalid in invalid_rows:
            with self.subTest(name=name), self.assertRaises(ValueError):
                ingest_locked_plan([], [invalid], lock())

    def test_value_v4_ingestion_rejects_invalid_portfolio_semantics(self):
        valid_legs = [
            {"match_id": "1001", "market_type": "had", "selection": "胜", "line": ""},
            {"match_id": "1002", "market_type": "ttg", "selection": "2球", "line": ""},
        ]
        invalid_plans = (
            [plan_row(play="SCORE", market_type="score")],
            [plan_row(play="SCORE", market_type="had")],
            [plan_row(selection="1-0")],
            [plan_row(play="HHAD", market_type="hhad", market_line="+0.5")],
            [plan_row(play="TTG", market_type="ttg", selection="8球")],
            [plan_row(
                play="PARLAY", market_type="parlay",
                legs_json=json.dumps([
                    valid_legs[0],
                    {**valid_legs[1], "market_type": "score"},
                ], ensure_ascii=False),
            )],
            [plan_row(
                play="PARLAY", market_type="parlay",
                legs_json=json.dumps([
                    valid_legs[0],
                    {**valid_legs[1], "market_type": "hhad", "line": "+0.5", "selection": "胜"},
                ], ensure_ascii=False),
            )],
            [plan_row(
                play="PARLAY", market_type="parlay",
                legs_json=json.dumps([
                    valid_legs[0],
                    {**valid_legs[1], "match_id": "1001"},
                ], ensure_ascii=False),
            )],
            [
                plan_row(
                    play="PARLAY", market_type="parlay",
                    legs_json=json.dumps(valid_legs, ensure_ascii=False),
                ),
                plan_row(
                    play="PARLAY-2", market_type="parlay",
                    legs_json=json.dumps([
                        {**valid_legs[0], "match_id": "2001"},
                        {**valid_legs[1], "match_id": "2002"},
                    ], ensure_ascii=False),
                ),
            ],
        )

        for rows in invalid_plans:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                ingest_locked_plan([], rows, lock())

    def test_identity_uses_only_canonical_immutable_fields(self):
        first = plan_row()
        changed = plan_row(
            odds="9.99",
            locked_odds="9.99",
            stake="200",
            raw_probability="0.99",
            locked_at_bjt="2026-07-16T14:00:00+08:00",
            status=WON,
            return_amount="200.00",
        )

        identifier = stable_bet_id(first)
        self.assertRegex(identifier, r"^[0-9a-f]{64}$")
        self.assertEqual(identifier, stable_bet_id(changed))

        for field, value in (
            ("date", "2026-07-17"),
            ("strategy_version", "value-v5"),
            ("match_id", "1002"),
            ("play", "HHAD"),
            ("market_type", "hhad"),
            ("selection", "平"),
            ("market_line", "+1"),
        ):
            with self.subTest(field=field):
                candidate = plan_row(**{field: value})
                self.assertNotEqual(identifier, stable_bet_id(candidate))

    def test_parlay_identity_is_invariant_to_leg_and_json_key_order(self):
        legs = [
            {"match_id": "1002", "market_type": "ttg", "selection": "2球", "line": ""},
            {"match_id": "1001", "market_type": "had", "selection": "胜", "line": ""},
        ]
        first = plan_row(play="2-leg parlay", market_type="parlay", selection="展示标签", legs_json=json.dumps(legs, ensure_ascii=False))
        second = plan_row(play="2-leg parlay", market_type="parlay", selection="另一个标签", legs_json=json.dumps(list(reversed(legs)), ensure_ascii=False, sort_keys=True))

        self.assertEqual(stable_bet_id(first), stable_bet_id(second))

    def test_market_type_is_authoritative_for_new_parlay_identity(self):
        legs = [
            {"match_id": "1001", "market_type": "had", "selection": "胜", "line": ""},
            {"match_id": "1002", "market_type": "ttg", "selection": "2球", "line": ""},
        ]
        localized = plan_row(
            play="胜负串",
            market_type=" ParLay ",
            selection="展示标签",
            legs_json=json.dumps(legs, ensure_ascii=False),
        )
        normalized = plan_row(
            play="胜负串",
            market_type="parlay",
            selection="另一个展示标签",
            legs_json=json.dumps(list(reversed(legs)), ensure_ascii=False),
        )
        single_market = plan_row(
            play="胜负串",
            market_type="had",
            legs_json=json.dumps(legs, ensure_ascii=False),
        )

        self.assertEqual(stable_bet_id(localized), stable_bet_id(normalized))
        self.assertNotEqual(stable_bet_id(localized), stable_bet_id(single_market))
        with self.assertRaises(ValueError):
            stable_bet_id(plan_row(
                play="2-leg parlay",
                market_type="had",
                legs_json=json.dumps(legs, ensure_ascii=False),
            ))

    def test_malformed_identity_fails_closed(self):
        for row in (
            plan_row(match_id=""),
            plan_row(match_id="legacy_match:forbidden"),
            plan_row(date="not-a-date"),
            plan_row(play="2-leg parlay", market_type="parlay", legs_json="not-json"),
            plan_row(play="2-leg parlay", market_type="parlay", legs_json="[]"),
            plan_row(
                play="2-leg parlay",
                market_type="parlay",
                legs_json=json.dumps([
                    {"match_id": "legacy_match:forbidden", "market_type": "had", "selection": "胜", "line": ""},
                    {"match_id": "1002", "market_type": "had", "selection": "胜", "line": ""},
                ], ensure_ascii=False),
            ),
        ):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    stable_bet_id(row)

    def test_ingestion_migrates_legacy_keeps_first_row_and_never_overwrites_locked_values(self):
        legacy = {"date": "2026-07-16", "match": "甲队 vs 乙队", "play": "HAD", "selection": "胜", "odds": "1.80", "stake": "10", "legacy_note": "keep"}
        initial = ingest_locked_plan([legacy], [plan_row()], lock())
        self.assertEqual("keep", initial[0]["legacy_note"])
        self.assertRegex(initial[0]["bet_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(PENDING, initial[0]["status"])

        plan = plan_row(odds="2.00", locked_odds="2.00", stake="20")
        once = ingest_locked_plan([], [plan], lock())
        rerun = ingest_locked_plan(once, [plan_row(odds="7.00", locked_odds="7.00", stake="900")], lock())
        duplicate = copy.deepcopy(rerun[0])
        duplicate["locked_odds"] = "99.00"
        duplicate["stake"] = "999"
        with self.assertRaisesRegex(ValueError, "conflicting existing canonical"):
            ingest_locked_plan([rerun[0], duplicate], [], lock())

        self.assertEqual(1, len(rerun))
        self.assertEqual("2.00", rerun[0]["locked_odds"])
        self.assertEqual("20", rerun[0]["stake"])
        self.assertEqual(plan, plan_row())

    def test_legacy_parlay_without_legs_uses_deterministic_fallback_identity(self):
        legacy = {
            "date": "2026-07-16",
            "strategy_version": "legacy-v1",
            "match": "甲队 vs 乙队",
            "play": "2-leg parlay",
            "market_type": "parlay",
            "selection": "甲胜串总进球2",
            "market_line": "",
            "odds": "4.20",
            "stake": "10",
            "legacy_note": "preserve",
        }
        original = copy.deepcopy(legacy)

        migrated = ingest_locked_plan([legacy], [], lock())
        identical = ingest_locked_plan([copy.deepcopy(legacy)], [], lock())
        rerun = ingest_locked_plan(migrated, [], lock())

        self.assertEqual(original, legacy)
        self.assertEqual(1, len(migrated))
        self.assertRegex(migrated[0]["bet_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(migrated[0]["bet_id"], identical[0]["bet_id"])
        self.assertEqual(migrated, rerun)
        self.assertNotIn("match_id", migrated[0])
        for field, value in original.items():
            self.assertEqual(value, migrated[0][field], field)

        for field, value in (("match", "甲队 vs 丙队"), ("selection", "不同展示")):
            with self.subTest(field=field):
                variant = {**legacy, field: value}
                variant_id = ingest_locked_plan([variant], [], lock())[0]["bet_id"]
                self.assertNotEqual(migrated[0]["bet_id"], variant_id)

    def test_legacy_fallback_distinguishes_structured_leg_identities_and_keeps_rows(self):
        legs = [
            {"match_id": "1001", "market_type": "had", "selection": "胜", "line": "", "odds": "2.00"},
            {"match_id": "1002", "market_type": "ttg", "selection": "2球", "line": "", "odds": "3.00"},
            {"match_id": "1003", "market_type": "hhad", "selection": "平", "line": "+1", "odds": "4.00"},
        ]
        first = legacy_parlay_row(legs=copy.deepcopy(legs))
        changed_legs = copy.deepcopy(legs)
        changed_legs[2]["match_id"] = "2003"
        second = legacy_parlay_row(legs=changed_legs)

        migrated = ingest_locked_plan([first, second], [], lock())

        self.assertEqual(2, len(migrated))
        self.assertNotEqual(migrated[0]["bet_id"], migrated[1]["bet_id"])
        for source, row in zip((first, second), migrated):
            self.assertNotIn("match_id", row)
            for field, value in source.items():
                self.assertEqual(value, row[field], field)

    def test_legacy_fallback_structured_legs_ignore_order_key_order_and_mutable_values(self):
        legs = [
            {
                "match_id": "1001", "market_type": "had", "selection": "胜",
                "line": "", "odds": "2.00", "team_a": "甲队", "team_b": "乙队",
            },
            {"match_id": "1002", "market_type": "ttg", "selection": "2球", "line": "", "stake": "3"},
            {"match_id": "1003", "market_type": "hhad", "selection": "平", "line": "+1", "probability": "0.55"},
        ]
        reordered = [
            {
                "probability": "0.99",
                "market_line": leg["line"],
                "selection": leg["selection"],
                "market_type": leg["market_type"],
                "match_id": leg["match_id"],
                "odds": "99.00",
                "team_a": "变化后的展示主队",
                "team_b": "变化后的展示客队",
            }
            for leg in reversed(legs)
        ]
        first = legacy_parlay_row(legs_json=json.dumps(legs, ensure_ascii=False))
        equivalent = legacy_parlay_row(
            legs_json=json.dumps(reordered, ensure_ascii=False, sort_keys=True)
        )
        without_legs = legacy_parlay_row()

        first_id = ingest_locked_plan([first], [], lock())[0]["bet_id"]
        equivalent_id = ingest_locked_plan([equivalent], [], lock())[0]["bet_id"]
        without_legs_id = ingest_locked_plan([without_legs], [], lock())[0]["bet_id"]

        self.assertEqual(first_id, equivalent_id)
        self.assertNotEqual(first_id, without_legs_id)

    def test_legacy_fallback_no_id_legs_use_team_identity_and_preserve_rows(self):
        first_legs = [
            {
                "team_a": "甲队", "team_b": "乙队", "market_type": "had",
                "selection": "胜", "line": "", "odds": "2.00",
            },
            {
                "home_team": "丙队", "away_team": "丁队", "market_type": "ttg",
                "selection": "2球", "line": "", "probability": "0.55",
            },
        ]
        second_legs = copy.deepcopy(first_legs)
        second_legs[1]["away_team"] = "戊队"
        first = legacy_parlay_row(legs=first_legs, legacy_note="first")
        second = legacy_parlay_row(legs=second_legs, legacy_note="second")
        originals = copy.deepcopy((first, second))

        migrated = ingest_locked_plan([first, second], [], lock())
        rerun = ingest_locked_plan(migrated, [], lock())

        self.assertEqual(2, len(migrated))
        self.assertNotEqual(migrated[0]["bet_id"], migrated[1]["bet_id"])
        self.assertEqual(migrated, rerun)
        self.assertEqual(originals, (first, second))
        for source, row in zip(originals, migrated):
            self.assertNotIn("match_id", row)
            for field, value in source.items():
                self.assertEqual(value, row[field], field)

    def test_legacy_fallback_no_id_legs_normalize_order_and_ignore_mutable_values(self):
        first_legs = [
            {
                "match": "甲队 vs 乙队", "fixture": {"home": "甲队", "away": "乙队"},
                "team_a": "甲队", "team_b": "乙队", "home_team": "甲队",
                "away_team": "乙队", "homeTeam": "甲队", "awayTeam": "乙队",
                "home": "甲队", "away": "乙队", "teams": ["甲队", "乙队"],
                "display": "甲队-乙队", "display_label": "第一场",
                "match_display": "甲队 对 乙队", "market_type": "had",
                "selection": "胜", "line": "", "odds": "2.00",
                "locked_odds": "2.00", "probability": "0.51",
            },
            {
                "fixture": "丙队 vs 丁队", "home": "丙队", "away": "丁队",
                "market_type": "ttg", "selection": "2球", "market_line": "",
                "stake": "10", "result_status": "finished",
            },
        ]
        equivalent_legs = []
        for leg in reversed(first_legs):
            equivalent = {
                key: copy.deepcopy(value)
                for key, value in reversed(tuple(leg.items()))
            }
            equivalent.update({
                "odds": "99.00", "locked_odds": "88.00", "stake": "999",
                "probability": "0.99", "result_status": "conflict",
            })
            equivalent_legs.append(equivalent)
        changed_legs = copy.deepcopy(equivalent_legs)
        changed_legs[-1]["display_label"] = "不同场次"

        first_id = ingest_locked_plan([
            legacy_parlay_row(legs=first_legs)
        ], [], lock())[0]["bet_id"]
        equivalent_id = ingest_locked_plan([
            legacy_parlay_row(
                legs_json=json.dumps(equivalent_legs, ensure_ascii=False, sort_keys=True)
            )
        ], [], lock())[0]["bet_id"]
        changed_id = ingest_locked_plan([
            legacy_parlay_row(legs=changed_legs)
        ], [], lock())[0]["bet_id"]

        self.assertEqual(first_id, equivalent_id)
        self.assertNotEqual(first_id, changed_id)

    def test_legacy_fallback_unparseable_leg_text_is_distinct_and_idempotent(self):
        first = legacy_parlay_row(legs_json="not-json-a")
        second = legacy_parlay_row(legs_json="not-json-b")

        migrated = ingest_locked_plan([first, second], [], lock())
        rerun = ingest_locked_plan(migrated, [], lock())

        self.assertEqual(2, len(migrated))
        self.assertNotEqual(migrated[0]["bet_id"], migrated[1]["bet_id"])
        self.assertEqual(migrated, rerun)
        self.assertEqual(("not-json-a", "not-json-b"), tuple(
            row["legs_json"] for row in migrated
        ))
        self.assertTrue(all("match_id" not in row for row in migrated))

        missing_id = ingest_locked_plan([legacy_parlay_row()], [], lock())[0]["bet_id"]
        empty_id = ingest_locked_plan([
            legacy_parlay_row(legs_json="")
        ], [], lock())[0]["bet_id"]
        null_id = ingest_locked_plan([
            legacy_parlay_row(legs_json="null")
        ], [], lock())[0]["bet_id"]
        self.assertEqual(3, len({missing_id, empty_id, null_id}))

    def test_ingestion_requires_a_valid_matching_domestic_lock(self):
        invalid_locks = (
            lock(report_date="2026-07-17"),
            lock(locked_at_bjt="2026-07-16T13:31:00"),
            lock(plan_sha256=""),
            lock(odds_source="external-market"),
        )
        for payload in invalid_locks:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ingest_locked_plan([], [plan_row()], payload)

    def test_new_paid_odds_capture_cannot_be_after_lock_in_any_allowed_version(self):
        for strategy_version in ("legacy-v3", "value-v4"):
            with self.subTest(strategy_version=strategy_version, row="single"):
                single = plan_row(
                    strategy_version=strategy_version,
                    play=("legacy display" if strategy_version == "legacy-v3" else "HAD"),
                    kelly_fraction=("" if strategy_version == "legacy-v3" else "0.25"),
                    odds_captured_at_bjt="2026-07-16T05:31:00.000001+00:00",
                )
                with self.assertRaisesRegex(ValueError, "after lock"):
                    ingest_locked_plan([], [single], lock())

            with self.subTest(strategy_version=strategy_version, row="parlay leg"):
                parlay = v4_parlay_row(f"post-lock-{strategy_version}")
                if strategy_version == "legacy-v3":
                    parlay.update(
                        strategy_version="legacy-v3",
                        play="legacy combo display",
                        kelly_fraction="",
                    )
                legs = json.loads(parlay["legs_json"])
                legs[1]["odds_captured_at_bjt"] = (
                    "2026-07-16T05:31:00.000001+00:00"
                )
                parlay["legs_json"] = json.dumps(legs, ensure_ascii=False)
                with self.assertRaisesRegex(ValueError, "after lock"):
                    ingest_locked_plan([], [parlay], lock())

    def test_new_paid_odds_capture_accepts_lock_boundary_across_offsets(self):
        boundary = "2026-07-16T05:31:00+00:00"
        single = plan_row(odds_captured_at_bjt=boundary)
        self.assertEqual(1, len(ingest_locked_plan([], [single], lock())))

        parlay = v4_parlay_row("boundary")
        parlay["odds_captured_at_bjt"] = boundary
        legs = json.loads(parlay["legs_json"])
        for leg in legs:
            leg["odds_captured_at_bjt"] = boundary
        parlay["legs_json"] = json.dumps(legs, ensure_ascii=False)
        self.assertEqual(1, len(ingest_locked_plan([], [parlay], lock())))

    def test_new_locked_row_clears_plan_settlement_fields_and_uses_authoritative_lock_metadata(self):
        polluted = plan_row(**{
            "odds_source": "SPORTTERY",
            "status": WON,
            "result_status": "finished",
            "result_source": "untrusted",
            "source_record_id": "old-result",
            "captured_at_bjt": "2020-01-01T00:00:00+08:00",
            "home_goals": "9",
            "away_goals": "0",
            "settled_at_bjt": "2020-01-01T01:00:00+08:00",
            "return": "999.99",
            "profit": "979.99",
            "result_legs_json": "polluted",
            "clv": "0.99",
        })
        row = ingest_locked_plan([], [polluted], lock(odds_source="SportTery"))[0]

        self.assertEqual(PENDING, row["status"])
        for field in (
            "result_status", "result_source", "source_record_id", "captured_at_bjt",
            "home_goals", "away_goals", "settled_at_bjt", "result_legs_json", "clv",
        ):
            self.assertEqual("", row[field], field)
        self.assertEqual("0.00", row["return"])
        self.assertEqual("0.00", row["profit"])
        self.assertEqual("sporttery", row["odds_source"])
        self.assertEqual("a" * 64, row["plan_sha256"])

        with self.assertRaises(ValueError):
            ingest_locked_plan([], [plan_row(odds_source="zgzcw")], lock(odds_source="sporttery"))


class SettlementTest(unittest.TestCase):
    def settle_one(self, row, results):
        return settle_pending(ingest_locked_plan([], [row], lock()), results, SETTLED_AT)[0]

    def test_settle_ledger_dedupes_equivalent_canonical_rows_before_profit(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "bet_id": "spoofed-first"}
        duplicate = {**canonical, "bet_id": "spoofed-second"}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "output" / "betting_ledger.csv"
            write_ledger_atomic(ledger_path, [first, duplicate])

            ledger_module.settle_ledger(
                root, {"1001": finished("1001", 2, 1)}, SETTLED_AT
            )

            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(1, len(rows))
        self.assertEqual("spoofed-first", rows[0]["bet_id"])
        self.assertEqual(WON, rows[0]["status"])
        self.assertEqual("20.00", rows[0]["profit"])

    def test_settle_ledger_rejects_conflicting_canonical_duplicates(self):
        canonical = ingest_locked_plan([], [plan_row()], lock())[0]
        first = {**canonical, "bet_id": "spoofed-first"}
        conflict = {**canonical, "bet_id": "spoofed-second", "stake": "22"}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "output" / "betting_ledger.csv"
            write_ledger_atomic(ledger_path, [first, conflict])
            before = ledger_path.read_bytes()

            with self.assertRaisesRegex(
                ValueError, "conflicting existing canonical"
            ):
                ledger_module.settle_ledger(
                    root, {"1001": finished("1001", 2, 1)}, SETTLED_AT
                )

            self.assertEqual(before, ledger_path.read_bytes())

    def test_settle_ledger_preserves_unparseable_legacy_migration(self):
        legacy = legacy_parlay_row(
            bet_id="legacy-existing-id",
            legs_json="not-json",
            status=PENDING,
        )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "output" / "betting_ledger.csv"
            write_ledger_atomic(ledger_path, [legacy])

            ledger_module.settle_ledger(root, {}, SETTLED_AT)

            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(1, len(rows))
        self.assertEqual("legacy-existing-id", rows[0]["bet_id"])
        self.assertEqual("not-json", rows[0]["legs_json"])
        self.assertEqual(PENDING, rows[0]["status"])

    def test_had_each_three_way_selection_settles_from_matching_90_minute_score(self):
        for selection, score in (("胜", (2, 1)), ("平", (1, 1)), ("负", (0, 1))):
            with self.subTest(selection=selection, score=score):
                settled = self.settle_one(
                    plan_row(selection=selection),
                    {"1001": finished("1001", *score)},
                )
                self.assertEqual(WON, settled["status"])

    def test_hhad_and_each_total_goal_bucket_settle_from_explicit_90_minute_scores(self):
        self.assertEqual(WON, self.settle_one(plan_row(play="HHAD", market_type="hhad", market_line="+1", selection="胜"), {"1001": finished("1001", 1, 1)})["status"])
        self.assertEqual(LOST, self.settle_one(plan_row(play="HHAD", market_type="hhad", market_line="-1", selection="胜"), {"1001": finished("1001", 1, 1)})["status"])
        for total in range(7):
            with self.subTest(total=total):
                row = plan_row(play="TTG", market_type="ttg", selection=f"{total}球")
                self.assertEqual(WON, self.settle_one(row, {"1001": finished("1001", total, 0)})["status"])
        self.assertEqual(WON, self.settle_one(plan_row(play="TTG", market_type="ttg", selection="7+球"), {"1001": finished("1001", 4, 3)})["status"])

    def test_two_leg_parlay_requires_both_legs_and_handles_loss_and_refunds(self):
        legs = [
            v4_leg("1001", "had", "胜", "2.00"),
            v4_leg("1002", "ttg", "2球", "3.00"),
        ]
        row = plan_row(play="胜负串", market_type=" PARLAY ", legs_json=json.dumps(legs, ensure_ascii=False), odds="6.00", locked_odds="6.00", stake="10")
        won = self.settle_one(row, {"1001": finished("1001", 1, 0), "1002": finished("1002", 2, 0)})
        self.assertEqual((WON, "60.00", "50.00"), (won["status"], won["return"], won["profit"]))

        lost = self.settle_one(row, {"1001": finished("1001", 0, 1), "1002": finished("1002", 2, 0)})
        self.assertEqual((LOST, "0.00", "-10.00"), (lost["status"], lost["return"], lost["profit"]))

        partial = self.settle_one(row, {"1001": finished("1001", 0, 1)})
        self.assertEqual(PENDING, partial["status"])

        refunded = {"match_id": "1002", "result_status": "refunded", "result_source": "sporttery", "source_record_id": "refund-1002", "captured_at_bjt": "2026-07-17T11:00:00+08:00"}
        mixed = self.settle_one(row, {"1001": finished("1001", 1, 0), "1002": refunded})
        self.assertEqual((WON, "20.00", "10.00"), (mixed["status"], mixed["return"], mixed["profit"]))

        fully_refunded = self.settle_one(plan_row(), {"1001": {**refunded, "match_id": "1001"}})
        self.assertEqual((REFUNDED, "20.00", "0.00"), (fully_refunded["status"], fully_refunded["return"], fully_refunded["profit"]))

    def test_settlement_uses_market_type_not_legacy_english_play_label(self):
        legacy_single = plan_row(
            bet_id="legacy-existing-id",
            play="2-leg parlay",
            market_type="had",
            status=PENDING,
        )

        settled = settle_pending(
            [legacy_single],
            {"1001": finished("1001", 2, 1)},
            SETTLED_AT,
        )[0]

        self.assertEqual(WON, settled["status"])

    def test_unproven_results_do_not_mutate_pending_and_correction_is_explicit(self):
        pending = ingest_locked_plan([], [plan_row()], lock())
        baseline = copy.deepcopy(pending)
        cases = (
            {},
            {"1001": {**finished("1001", 1, 0), "result_status": "conflict"}},
            {"1001": {**finished("1001", 1, 0), "result_status": "unavailable"}},
            {"1001": {**finished("1001", "x", 0)}},
            {"1001": {**finished("1001", 1, 0), "captured_at_bjt": "not-a-timestamp"}},
            {"1001": {**finished("1001", 1, 0), "captured_at_bjt": "2026-07-17T11:00:00"}},
            {"wrong": finished("wrong", 1, 0)},
        )
        for results in cases:
            with self.subTest(results=results):
                self.assertEqual(baseline, settle_pending(pending, results, SETTLED_AT))

        invalid = settle_pending(pending, {"1001": {**finished("1001", 1, 0), "result_status": "invalid"}}, SETTLED_AT)
        self.assertEqual(ABNORMAL, invalid[0]["status"])
        unchanged = settle_pending(invalid, {"1001": finished("1001", 1, 0, "result-1001")}, SETTLED_AT, allow_correction=True)
        self.assertEqual(ABNORMAL, unchanged[0]["status"])
        reopened = settle_pending(invalid, {"1001": finished("1001", 1, 0, "changed")}, SETTLED_AT, allow_correction=True)
        self.assertEqual(PENDING, reopened[0]["status"])
        correction_repeat = settle_pending(reopened, {"1001": finished("1001", 1, 0, "changed")}, SETTLED_AT, allow_correction=True)
        self.assertEqual(reopened, correction_repeat)
        settled = settle_pending(reopened, {"1001": finished("1001", 1, 0, "changed")}, SETTLED_AT)
        self.assertEqual(WON, settled[0]["status"])

    def test_correction_mode_never_settles_pending_rows(self):
        pending = ingest_locked_plan([], [plan_row()], lock())

        self.assertEqual(
            pending,
            settle_pending(pending, {"1001": finished("1001", 1, 0)}, SETTLED_AT, allow_correction=True),
        )

    def test_abnormal_parlay_reopens_by_offending_leg_then_requires_ordinary_settlement(self):
        legs = [
            v4_leg("1001", "had", "胜", "2.00"),
            v4_leg("1002", "ttg", "2球", "3.00"),
        ]
        pending = ingest_locked_plan([], [plan_row(
            play="2-leg parlay",
            market_type="parlay",
            legs_json=json.dumps(legs, ensure_ascii=False),
            odds="6.00",
            locked_odds="6.00",
            stake="10",
        )], lock())
        invalid_leg = {**finished("1002", 2, 0, "bad-1002"), "result_status": "invalid"}
        abnormal = settle_pending(
            pending,
            {"1001": finished("1001", 1, 0), "1002": invalid_leg},
            SETTLED_AT,
        )

        self.assertEqual(ABNORMAL, abnormal[0]["status"])
        self.assertEqual("1002", json.loads(abnormal[0]["result_legs_json"])[0]["match_id"])

        corrected_results = {
            "1001": finished("1001", 1, 0),
            "1002": finished("1002", 2, 0, "fixed-1002"),
        }
        reopened = settle_pending(abnormal, corrected_results, SETTLED_AT, allow_correction=True)
        self.assertEqual(PENDING, reopened[0]["status"])
        self.assertEqual(reopened, settle_pending(reopened, corrected_results, SETTLED_AT, allow_correction=True))
        self.assertEqual(WON, settle_pending(reopened, corrected_results, SETTLED_AT)[0]["status"])

    def test_locked_odds_keep_full_decimal_precision_until_money_is_quantized(self):
        settled = self.settle_one(
            plan_row(locked_odds="1.23456", odds="1.23456", stake="10"),
            {"1001": finished("1001", 1, 0)},
        )

        self.assertEqual("12.35", settled["return"])
        self.assertEqual("2.35", settled["profit"])

    def test_settlement_is_byte_idempotent_and_only_changes_allowed_fields(self):
        pending = ingest_locked_plan([], [plan_row()], lock())
        settled = settle_pending(pending, {"1001": finished("1001", 1, 0)}, SETTLED_AT)
        second = settle_pending(settled, {"1001": finished("1001", 0, 1, "later-source")}, SETTLED_AT)
        self.assertEqual(settled, second)
        changed = {key for key in settled[0] if settled[0].get(key) != pending[0].get(key)}
        self.assertTrue(changed.issubset({"status", "result_status", "result_source", "source_record_id", "captured_at_bjt", "home_goals", "away_goals", "return", "profit", "result_legs_json", "settled_at_bjt"}))

    def test_canonical_observation_lifecycle_preserves_fields_and_settles_all_markets(self):
        observations = [
            plan_row(
                match_id="obs-had",
                stake="0",
                model_version="model-observation",
                raw_probability="0.61",
            ),
            plan_row(
                match_id="obs-hhad",
                play="HHAD",
                market_type="hhad",
                market_line="-1",
                selection=THREE_WAY_SELECTIONS["d"],
                stake="0",
            ),
            plan_row(
                match_id="obs-ttg",
                play="TTG",
                market_type="ttg",
                selection=TOTAL_GOALS_SELECTIONS["s3"],
                stake="0",
            ),
        ]
        results = {
            "obs-had": finished("obs-had", 2, 1),
            "obs-hhad": finished("obs-hhad", 2, 1),
            "obs-ttg": finished("obs-ttg", 2, 1),
        }

        settled = ledger_module.update_observation_ledger(
            [], observations, results, SETTLED_AT
        )

        self.assertEqual(3, len(settled))
        self.assertEqual({WON}, {row["status"] for row in settled})
        self.assertEqual({"0"}, {str(row["stake"]) for row in settled})
        self.assertEqual(
            {stable_bet_id(row) for row in observations},
            {row["bet_id"] for row in settled},
        )
        self.assertEqual("model-observation", settled[0]["model_version"])
        self.assertEqual("0.61", settled[0]["raw_probability"])
        self.assertTrue(all(row["odds_source_record_id"] for row in settled))

        repeated = ledger_module.update_observation_ledger(
            settled,
            observations,
            {
                "obs-had": finished("obs-had", 0, 1, "later"),
                "obs-hhad": finished("obs-hhad", 0, 3, "later"),
                "obs-ttg": finished("obs-ttg", 0, 0, "later"),
            },
            SETTLED_AT + timedelta(days=1),
        )
        self.assertEqual(settled, repeated)

    def test_new_observations_reject_nonzero_unsupported_or_malformed_rows(self):
        valid = plan_row(stake="0")
        cases = (
            ("nonzero stake", {**valid, "stake": "2"}, "zero stake"),
            ("unsupported version", {**valid, "strategy_version": "legacy-v3"}, "strategy_version"),
            ("unsupported market", {**valid, "market_type": "parlay"}, "market"),
            ("spoofed identity", {**valid, "bet_id": "not-canonical"}, "bet_id"),
            ("price mismatch", {**valid, "odds": "2.01"}, "equal"),
            ("missing evidence", {**valid, "odds_source_record_id": ""}, "record"),
        )
        for name, row, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                ledger_module.update_observation_ledger([], [row], {}, SETTLED_AT)

    def test_observation_outcome_vectors_keep_selection_in_stable_identity(self):
        observations = [
            plan_row(
                match_id="vector-had",
                selection=selection,
                stake="0",
                odds_source_record_id=f"vector-{code}",
            )
            for code, selection in THREE_WAY_SELECTIONS.items()
        ]

        rows = ledger_module.update_observation_ledger(
            [], observations, {}, SETTLED_AT
        )

        self.assertEqual(3, len(rows))
        self.assertEqual(3, len({row["bet_id"] for row in rows}))
        self.assertEqual(
            set(THREE_WAY_SELECTIONS.values()),
            {row["selection"] for row in rows},
        )


class LockedIngestCommandTest(unittest.TestCase):
    def _write_plan(self, path, row):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)

    def _prepare_locked_plan(self, root, row):
        output = root / "output"
        data = root / "data"
        output.mkdir()
        data.mkdir()
        plan_path = output / "betting_plan_2026-07-16.csv"
        self._write_plan(plan_path, row)
        odds_path = data / "sporttery_odds_2026-07-16.json"
        odds_path.write_text("{}", encoding="utf-8")
        lock_payload = {
            "schema_version": 1,
            "report_date": "2026-07-16",
            "locked_at_bjt": LOCKED_AT.isoformat(),
            "plan_path": "output/betting_plan_2026-07-16.csv",
            "plan_sha256": sha256_file(plan_path),
            "odds_path": "data/sporttery_odds_2026-07-16.json",
            "odds_sha256": sha256_file(odds_path),
            "odds_source": "sporttery",
        }
        (output / "plan_lock_2026-07-16.json").write_text(
            json.dumps(lock_payload), encoding="utf-8"
        )
        return plan_path

    def test_ingest_rejects_plan_bytes_changed_after_lock_validation(self):
        target_date = date(2026, 7, 16)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._prepare_locked_plan(root, plan_row())
            tampered_path = root / "tampered.csv"
            self._write_plan(tampered_path, plan_row(stake="99"))
            with (
                patch.object(
                    ledger_module, "_read_plan_bytes", return_value=tampered_path.read_bytes()
                ) as read_bytes,
                self.assertRaises(ValueError),
            ):
                ingest_date(root, target_date)

        read_bytes.assert_called_once()

    def test_ingest_parses_verified_captured_bytes_if_file_changes_after_read(self):
        target_date = date(2026, 7, 16)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan_path = self._prepare_locked_plan(root, plan_row())
            tampered_path = root / "tampered.csv"
            self._write_plan(tampered_path, plan_row(stake="99"))
            tampered_bytes = tampered_path.read_bytes()

            def capture_then_change(path):
                captured = path.read_bytes()
                path.write_bytes(tampered_bytes)
                return captured

            with patch.object(
                ledger_module, "_read_plan_bytes", side_effect=capture_then_change
            ) as read_bytes:
                ledger_path = ingest_date(root, target_date)
            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        read_bytes.assert_called_once_with(plan_path)
        self.assertEqual("20", rows[0]["stake"])

    def test_ingest_reads_only_the_matching_valid_locked_paid_plan(self):
        target_date = date(2026, 7, 16)
        row = plan_row()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            data = root / "data"
            output.mkdir()
            data.mkdir()
            plan_path = output / "betting_plan_2026-07-16.csv"
            with plan_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            odds_path = data / "sporttery_odds_2026-07-16.json"
            odds_path.write_text("{}", encoding="utf-8")
            lock_payload = {
                "schema_version": 1,
                "report_date": target_date.isoformat(),
                "locked_at_bjt": LOCKED_AT.isoformat(),
                "plan_path": "output/betting_plan_2026-07-16.csv",
                "plan_sha256": sha256_file(plan_path),
                "odds_path": "data/sporttery_odds_2026-07-16.json",
                "odds_sha256": sha256_file(odds_path),
                "odds_source": "sporttery",
            }
            (output / "plan_lock_2026-07-16.json").write_text(
                json.dumps(lock_payload), encoding="utf-8"
            )
            shadow = output / "shadow_betting_plan_2026-07-16.csv"
            shadow.write_text("date,stake\n2026-07-16,999\n", encoding="utf-8")

            ledger_path = ingest_date(root, target_date)

            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(1, len(rows))
        self.assertEqual("20", rows[0]["stake"])


class AtomicWriteTest(unittest.TestCase):
    def test_atomic_writer_is_deterministic_utf8_sig_and_preserves_unknown_fields(self):
        rows = ingest_locked_plan([], [plan_row(
            legacy_field="legacy",
            performance_multiplier="0.75",
        )], lock())
        self.assertEqual("0.75", rows[0]["performance_multiplier"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.csv"
            self.assertEqual(path, write_ledger_atomic(path, rows))
            first = path.read_bytes()
            self.assertTrue(first.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\n", first)
            write_ledger_atomic(path, rows)
            self.assertEqual(first, path.read_bytes())
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertIn("plan_sha256", reader.fieldnames)
                self.assertEqual(
                    reader.fieldnames.index("volatility_multiplier") + 1,
                    reader.fieldnames.index("performance_multiplier"),
                )
                self.assertLess(
                    reader.fieldnames.index("performance_multiplier"),
                    reader.fieldnames.index("portfolio_rank"),
                )
                self.assertEqual("legacy", next(reader)["legacy_field"])


if __name__ == "__main__":
    unittest.main()
