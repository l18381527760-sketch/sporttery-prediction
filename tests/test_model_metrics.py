import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import live_odds
from model_metrics import snapshot_coverage, summarize


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 21)


def live_match():
    return {
        "matchId": "m1",
        "matchNumStr": "Monday001",
        "homeTeam": "Home",
        "awayTeam": "Away",
        "matchStatus": "Selling",
        "kickoff_at": "2026-07-21T18:00:00+08:00",
        "isSingleHad": True,
        "isSingleHhad": False,
        "isSingleTtg": False,
    }


def live_odds_payload():
    return {
        "had": {"h": "2.80", "d": "3.10", "a": "2.25"},
        "hhad": {},
        "ttg": {},
    }


class ModelMetricsTest(unittest.TestCase):
    def test_risk_and_calibration_metrics_are_reported(self):
        rows = [
            {"date": "2026-07-01", "play": "平局单场", "stage": "联赛A", "probability": "0.60", "odds": "2", "stake": "10", "status": "命中", "profit": "10"},
            {"date": "2026-07-02", "play": "平局单场", "stage": "联赛A", "probability": "0.60", "odds": "2", "stake": "10", "status": "未中", "profit": "-10"},
            {"date": "2026-07-03", "play": "胜平负2串1", "stage": "联赛B", "probability": "0.40", "odds": "3", "stake": "10", "status": "未中", "profit": "-10"},
            {"date": "2026-07-04", "play": "胜平负2串1", "stage": "联赛B", "probability": "0.40", "odds": "3", "stake": "10", "status": "命中", "profit": "20"},
        ]

        metrics = summarize(rows)
        overall = metrics["overall"]

        self.assertEqual(20.0, overall["max_drawdown"])
        self.assertEqual(2, overall["max_losing_streak"])
        self.assertEqual(0, overall["current_losing_streak"])
        self.assertIsNotNone(overall["calibration_error"])
        self.assertEqual(2, metrics["by_play"]["平局单场"]["count"])
        self.assertEqual(2, metrics["by_play"]["胜平负串关"]["count"])
        self.assertIn("profit", metrics["by_play"]["胜平负串关"])
        self.assertEqual(metrics["by_play"], metrics["by_play_all"])

    def test_snapshot_coverage_separates_opening_decision_and_closing_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, phase in enumerate(("opening", "decision", "pre_kickoff")):
                (root / f"snapshot-{index}.json").write_text(
                    json.dumps(
                        {
                            "target_date": "2026-07-12",
                            "captured_at": f"2026-07-12T1{index}:00:00+08:00",
                            "capture_phase": "monitoring",
                            "matches": [{
                                "match_id": f"match-{index}",
                                "capture_phase": phase,
                            }],
                        }
                    ),
                    encoding="utf-8",
                )

            coverage = snapshot_coverage(root)

        self.assertEqual(3, coverage["files"])
        self.assertEqual(1, coverage["phases"]["opening"])
        self.assertEqual(1, coverage["phases"]["decision"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff"])

    def test_counts_nested_live_pre_kickoff_phases_once_per_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            for captured_at in (
                datetime(2026, 7, 21, 16, 45, tzinfo=BJT),
                datetime(2026, 7, 21, 17, 15, tzinfo=BJT),
            ):
                live_odds.capture_live_snapshot(
                    root,
                    DAY,
                    captured_at,
                    phase="decision",
                    sporttery_fetcher=lambda target_date: [live_match()],
                    sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
                )

            coverage = snapshot_coverage(
                legacy,
                root / "data" / "live_odds_snapshots",
                DAY,
            )

        self.assertEqual(2, coverage["files"])
        self.assertEqual(1, coverage["matches"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff_90"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff_30"])
        self.assertEqual(1, coverage["requested_phases"]["decision"])
        self.assertEqual(["m1"], coverage["match_ids_by_requested_phase"]["decision"])

    def test_valid_v1_live_snapshot_does_not_synthesize_phase_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = live_odds.capture_live_snapshot(
                root,
                DAY,
                datetime(2026, 7, 21, 16, 45, tzinfo=BJT),
                phase="decision",
                sporttery_fetcher=lambda target_date: [live_match()],
                sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
            )
            payload = json.loads(original.read_text(encoding="utf-8"))
            payload["schema_version"] = 1
            payload.pop("capture_phase")
            payload["matches"][0].pop("capture_phase")
            payload["matches"][0].pop("minutes_to_kickoff")
            raw = live_odds._canonical_json_bytes(payload)
            captured = datetime.fromisoformat(payload["captured_at"])
            historical = original.with_name(
                live_odds._filename(captured, payload["source"], raw)
            )
            historical.write_bytes(raw)
            original.unlink()

            coverage = snapshot_coverage(
                root / "legacy",
                root / "data" / "live_odds_snapshots",
                DAY,
            )

        self.assertEqual(1, coverage["files"])
        self.assertEqual(1, coverage["matches"])
        self.assertEqual({}, coverage["requested_phases"])
        self.assertTrue(all(count == 0 for count in coverage["phases"].values()))

    def test_forged_live_phase_evidence_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = live_odds.capture_live_snapshot(
                root,
                DAY,
                datetime(2026, 7, 21, 16, 45, tzinfo=BJT),
                phase="decision",
                sporttery_fetcher=lambda target_date: [live_match()],
                sporttery_odds_fetcher=lambda match_id: live_odds_payload(),
            )
            payload = json.loads(original.read_text(encoding="utf-8"))
            payload["matches"][0]["capture_phase"] = "decision"
            raw = live_odds._canonical_json_bytes(payload)
            captured = datetime.fromisoformat(payload["captured_at"])
            forged = original.with_name(
                live_odds._filename(captured, payload["source"], raw)
            )
            forged.write_bytes(raw)
            original.unlink()

            coverage = snapshot_coverage(
                root / "legacy",
                root / "data" / "live_odds_snapshots",
                DAY,
            )

        self.assertEqual(0, coverage["files"])
        self.assertEqual(0, coverage["matches"])
        self.assertEqual({}, coverage["requested_phases"])


if __name__ == "__main__":
    unittest.main()
