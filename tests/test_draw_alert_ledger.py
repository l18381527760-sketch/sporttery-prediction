import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from draw_alert_ledger import compute_subtype_metrics, main, settle_alert, update_draw_alert_ledger


def settled_row(**overrides):
    return {
        "status": "命中",
        "model_draw_probability": "0.34",
        "market_draw_probability": "0.28",
        "hypothetical_stake": "10",
        "hypothetical_profit": "22",
        "clv": "0.01",
        **overrides,
    }


class DrawAlertLedgerTest(unittest.TestCase):
    def test_cli_rejects_missing_or_unknown_arguments_without_updating(self):
        with patch("draw_alert_ledger.update_draw_alert_ledger") as update:
            for arguments in ([], ["--unknown"]):
                with self.assertRaises(SystemExit) as error:
                    main(arguments)
                self.assertEqual(2, error.exception.code)

        update.assert_not_called()

    def test_settle_cli_calls_ledger_update(self):
        with patch(
            "draw_alert_ledger.update_draw_alert_ledger",
            return_value=(Path("ledger.csv"), Path("metrics.json")),
        ) as update:
            self.assertEqual(0, main(["--settle"]))

        update.assert_called_once_with()

    def test_settle_cli_returns_nonzero_when_ledger_update_fails(self):
        with patch("draw_alert_ledger.update_draw_alert_ledger", side_effect=RuntimeError("write failed")):
            self.assertEqual(1, main(["--settle"]))

    def test_90_minute_draw_wins_even_when_team_wins_extra_time(self):
        alert = {"date": "2026-07-11", "match": "挪威 vs 英格兰", "domestic_draw_odds": "3.60", "hypothetical_stake": "10", "settlement_mode": "observation"}
        result = {"home_goals": "1", "away_goals": "1"}

        settled = settle_alert(alert, result)

        self.assertEqual("命中", settled["status"])
        self.assertEqual(26.0, settled["hypothetical_profit"])

    def test_all_knockout_regressions_settle_on_90_minutes(self):
        for match in ("阿根廷 vs 瑞士", "阿根廷 vs 佛得角", "德国 vs 巴拉圭"):
            alert = {"date": "2026-07-11", "match": match, "domestic_draw_odds": "3.20", "hypothetical_stake": "10", "settlement_mode": "observation"}
            self.assertEqual("命中", settle_alert(alert, {"home_goals": "1", "away_goals": "1"})["status"])

    def test_linked_alert_has_no_duplicate_actual_profit(self):
        alert = {"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "settlement_mode": "linked", "additional_stake": "0"}

        settled = settle_alert(alert, {"home_goals": "0", "away_goals": "0"})

        self.assertEqual(0.0, settled["actual_profit"])

    def test_observation_alert_has_no_actual_profit(self):
        alert = {"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "settlement_mode": "budget_capped_observation", "additional_stake": "20"}

        settled = settle_alert(alert, {"home_goals": "0", "away_goals": "0"})

        self.assertEqual(0.0, settled["actual_profit"])

    def test_each_subtype_needs_its_own_30_samples(self):
        rows = [settled_row() for _ in range(29)]

        metrics = compute_subtype_metrics(rows, min_samples=30, roi_gate=0.05, max_drawdown=100)

        self.assertFalse(metrics["promoted"])

    def test_invalid_model_probabilities_do_not_count_or_affect_metrics(self):
        invalid_probabilities = [float("nan"), float("inf"), -0.01, 1.01, None] * 5
        invalid_probabilities += [float("-inf"), -1, 2, None]
        rows = []
        for probability in invalid_probabilities:
            row = settled_row(
                status="未命中",
                model_draw_probability=probability,
                market_draw_probability="0.90",
                hypothetical_stake="0.01",
                hypothetical_profit="-0.01",
                clv="0.50",
            )
            if probability is None:
                row.pop("model_draw_probability")
            rows.append(row)
        rows.append(settled_row())

        metrics = compute_subtype_metrics(rows, min_samples=30, roi_gate=0.05, max_drawdown=100)

        self.assertEqual((1, False), (metrics["count"], metrics["promoted"]))
        self.assertEqual(1.0, metrics["hit_rate"])
        self.assertAlmostEqual(2.2, metrics["roi"])
        self.assertAlmostEqual((0.34 - 1.0) ** 2, metrics["brier"])
        self.assertAlmostEqual((0.28 - 1.0) ** 2, metrics["market_brier"])
        self.assertAlmostEqual(-math.log(0.34), metrics["log_loss"])
        self.assertAlmostEqual(0.01, metrics["average_clv"])
        self.assertEqual(0.0, metrics["max_drawdown"])

    def test_invalid_promotion_numbers_do_not_count_or_block_valid_samples(self):
        invalid_values = [
            ("market_draw_probability", float("nan")),
            ("market_draw_probability", float("inf")),
            ("market_draw_probability", -0.01),
            ("market_draw_probability", 1.01),
            ("market_draw_probability", None),
            ("hypothetical_stake", float("nan")),
            ("hypothetical_stake", float("inf")),
            ("hypothetical_stake", 0),
            ("hypothetical_stake", -1),
            ("hypothetical_stake", 501),
            ("hypothetical_stake", None),
            ("hypothetical_profit", float("nan")),
            ("hypothetical_profit", float("inf")),
            ("hypothetical_profit", None),
            ("clv", float("nan")),
            ("clv", float("inf")),
            ("clv", -1.01),
            ("clv", 1.01),
            ("clv", None),
        ]
        invalid_rows = []
        for field, value in invalid_values:
            row = settled_row(**{field: value})
            if value is None:
                row.pop(field)
            invalid_rows.append(row)

        metrics = compute_subtype_metrics(
            [settled_row(), *invalid_rows],
            min_samples=1,
            roi_gate=0.05,
            max_drawdown=100,
        )

        self.assertEqual((1, True), (metrics["count"], metrics["promoted"]))
        self.assertAlmostEqual(2.2, metrics["roi"])
        self.assertAlmostEqual(0.01, metrics["average_clv"])

    def test_missing_result_stays_unsettled_for_retry(self):
        settled = settle_alert({"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "additional_stake": "20", "settlement_mode": "standalone"}, None)

        self.assertEqual("未结算", settled["status"])
        self.assertIsNone(settled["outcome"])
        self.assertIsNone(settled["hypothetical_profit"])
        self.assertIsNone(settled["actual_profit"])

    def test_invalid_or_missing_90_minute_goals_stay_unsettled(self):
        alert = {"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "additional_stake": "20", "settlement_mode": "standalone"}
        for result in ({"home_goals": "", "away_goals": "1"}, {"home_goals": "one", "away_goals": "1"}, {"home_goals": "1.5", "away_goals": "1"}):
            self.assertEqual("未结算", settle_alert(alert, result)["status"])

    def test_standalone_loss_counts_once(self):
        alert = {"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "additional_stake": "20", "settlement_mode": "standalone"}

        settled = settle_alert(alert, {"home_goals": "1", "away_goals": "0"})

        self.assertEqual(-10.0, settled["hypothetical_profit"])
        self.assertEqual(-20.0, settled["actual_profit"])

    def test_missing_clv_blocks_promotion(self):
        rows = [settled_row(clv="") for _ in range(30)]

        metrics = compute_subtype_metrics(rows, min_samples=30, roi_gate=0.05, max_drawdown=100)

        self.assertIsNone(metrics["average_clv"])
        self.assertFalse(metrics["promoted"])

    def test_recent_brier_deterioration_blocks_promotion(self):
        rows = [settled_row(model_draw_probability="1.0", market_draw_probability="0.1") for _ in range(10)]
        rows += [settled_row(status="未命中", model_draw_probability="0.0", market_draw_probability="0.5", hypothetical_profit="-10") for _ in range(10)]
        rows += [settled_row(status="未命中", model_draw_probability="0.6", market_draw_probability="0.7", hypothetical_profit="-10") for _ in range(10)]

        metrics = compute_subtype_metrics(rows, min_samples=30, roi_gate=0.05, max_drawdown=1000)

        self.assertEqual(0.36, metrics["recent_brier"])
        self.assertFalse(metrics["promoted"])

    def test_max_drawdown_blocks_promotion(self):
        rows = [settled_row() for _ in range(20)]
        rows += [settled_row(status="未命中", hypothetical_profit="-50") for _ in range(10)]

        metrics = compute_subtype_metrics(rows, min_samples=30, roi_gate=0.05, max_drawdown=100)

        self.assertGreater(metrics["max_drawdown"], 100)
        self.assertFalse(metrics["promoted"])

    def test_update_deduplicates_alerts_and_keeps_subtype_metrics_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_alert(root, "draw_alert_2026-07-11.csv", [
                self._alert_row(subtype="cold_draw"),
                self._alert_row(subtype="cold_draw", domestic_draw_odds="3.60"),
            ])
            self._write_results(root, [("2026-07-11", "A", "B", "1", "1")])
            snapshots = root / "data" / "odds_snapshots"
            snapshots.mkdir(parents=True)
            self._write_snapshot(snapshots / "latest.json", "2026-07-11T11:00:00+00:00", "3.20")

            ledger_path, metrics_path = update_draw_alert_ledger(root)

            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(rows))
            self.assertEqual("3.60", rows[0]["domestic_draw_odds"])
            self.assertEqual(1, metrics["subtypes"]["cold_draw"]["count"])
            self.assertEqual(0, metrics["subtypes"]["balanced_draw"]["count"])
            self.assertFalse(metrics["subtypes"]["balanced_draw"]["promoted"])

    def test_update_uses_latest_qualifying_pre_kickoff_snapshot_for_clv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_alert(root, "draw_alert_2026-07-11.csv", [self._alert_row(market_draw_probability="0.25")])
            self._write_results(root, [("2026-07-11", "A", "B", "1", "1")])
            snapshots = root / "data" / "odds_snapshots"
            snapshots.mkdir(parents=True)
            self._write_snapshot(snapshots / "early.json", "2026-07-11T10:00:00+00:00", "4.00")
            self._write_snapshot(snapshots / "latest.json", "2026-07-11T11:00:00+00:00", "3.20")
            self._write_snapshot(snapshots / "post_kickoff.json", "2026-07-11T13:00:00+00:00", "2.00")
            self._write_snapshot(snapshots / "extra_time.json", "2026-07-11T11:30:00+00:00", "2.00", settlement_minutes=120)

            ledger_path, _ = update_draw_alert_ledger(root)

            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertAlmostEqual((1 / 3.2) / (1 / 2 + 1 / 3.2 + 1 / 2) - 0.25, float(row["clv"]))

    def test_pre_kickoff_snapshot_handles_offset_free_kickoff_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_alert(root, "draw_alert_2026-07-11.csv", [self._alert_row(market_draw_probability="0.25")])
            self._write_results(root, [("2026-07-11", "A", "B", "1", "1")])
            snapshots = root / "data" / "odds_snapshots"
            snapshots.mkdir(parents=True)
            self._write_snapshot(snapshots / "offset_free_kickoff.json", "2026-07-11T11:00:00+00:00", "3.20", kickoff_at="2026-07-11T12:00:00")

            ledger_path, _ = update_draw_alert_ledger(root)

            with ledger_path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertNotEqual("", next(csv.DictReader(handle))["clv"])

    @staticmethod
    def _alert_row(**overrides):
        return {
            "date": "2026-07-11",
            "team_a": "A",
            "team_b": "B",
            "subtype": "cold_draw",
            "domestic_draw_odds": "3.20",
            "market_draw_probability": "0.28",
            "model_draw_probability": "0.34",
            "hypothetical_stake": "10",
            "additional_stake": "0",
            "linked_main_stake": "0",
            "settlement_mode": "observation",
            **overrides,
        }

    @staticmethod
    def _write_alert(root, filename, rows):
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        with (output / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_results(root, rows):
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        with (data / "bet_results.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "team_a", "team_b", "home_goals", "away_goals", "half_home_goals", "half_away_goals"])
            writer.writerows(rows)

    @staticmethod
    def _write_snapshot(path, captured_at, draw_odds, settlement_minutes=90, kickoff_at="2026-07-11T12:00:00+00:00"):
        path.write_text(json.dumps({
            "target_date": "2026-07-11",
            "captured_at": captured_at,
            "matches": [{
                "team_a": "A", "team_b": "B", "kickoff_at": kickoff_at,
                "h": "2.00", "d": draw_odds, "a": "2.00", "market_type": "win_draw_loss",
                "settlement_minutes": settlement_minutes, "includes_extra_time": False,
            }],
        }), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
