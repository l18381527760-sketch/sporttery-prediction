import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import build_daily_image
import build_site
from build_site import render_draw_alert


class DrawAlertReportingTest(unittest.TestCase):
    def test_linked_alert_copy_does_not_claim_extra_stake(self):
        html = render_draw_alert([
            {
                "rank": "1",
                "subtype": "cold_draw",
                "match": "挪威 vs 英格兰",
                "settlement_mode": "linked",
                "linked_main_stake": "100",
                "model_draw_probability": "0.32",
                "market_draw_probability": "0.27",
                "domestic_draw_odds": "3.60",
                "expected_value": "1.15",
                "captured_at": "2026-07-12T13:30:00+08:00",
            }
        ])

        self.assertIn("冷门平局", html)
        self.assertIn("复用主方案金额", html)
        self.assertNotIn("额外投入 100", html)

    def test_empty_alert_has_neutral_copy(self):
        self.assertIn("今日无符合门槛", render_draw_alert([]))

    def test_external_evidence_is_html_escaped(self):
        html = render_draw_alert([
            {
                "rank": "1",
                "subtype": "cold_draw",
                "match": "A < B",
                "settlement_mode": "observation",
                "evidence_json": '{"source": "<script>alert(1)</script>"}',
            }
        ])

        self.assertNotIn("<script>", html)
        self.assertIn("A &lt; B", html)

    def test_malformed_evidence_is_safely_summarized(self):
        html = render_draw_alert([
            {
                "rank": "1",
                "subtype": "balanced_draw",
                "match": "A vs B",
                "settlement_mode": "observation",
                "evidence_json": "<b>not json</b>",
            }
        ])

        self.assertIn("证据来源", html)
        self.assertNotIn("<b>", html)

    def test_four_rows_keep_fixed_rank_order(self):
        alerts = [
            {
                "rank": str(rank),
                "subtype": "balanced_draw",
                "match": f"A{rank} vs B{rank}",
                "settlement_mode": "observation",
            }
            for rank in (4, 2, 1, 3)
        ]

        html = render_draw_alert(alerts)

        self.assertLess(html.index("第1场"), html.index("第2场"))
        self.assertLess(html.index("第2场"), html.index("第3场"))
        self.assertLess(html.index("第3场"), html.index("第4场"))

    def test_progress_and_registry_strings_are_html_escaped(self):
        html = render_draw_alert(
            [],
            {
                "subtypes": {
                    "cold_draw": {"count": 7},
                    "balanced_draw": {"count": 9},
                }
            },
            {
                "champion": {"version": "champion <v1>"},
                "challenger": {
                    "version": "challenger <v2>",
                    "shadow_days": 12,
                    "sample_count": 8,
                    "bet_count": 3,
                },
                "per_league": {"A < B": {"paused": True}},
                "last_training_error": "<error>",
            },
        )

        self.assertIn("7/30", html)
        self.assertIn("9/30", html)
        self.assertIn("champion &lt;v1&gt;", html)
        self.assertNotIn("<error>", html)

    def test_daily_image_alert_block_has_exact_fixed_height_delta(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text("match,play,odds,stake,selection\n", encoding="utf-8")
            (output / "betting_ledger.csv").write_text("date,play,match,selection,stake,status,profit\n", encoding="utf-8")
            (output / "draw_alert_2026-07-13.csv").write_text(
                "rank,subtype,match,settlement_mode,evidence_json\n"
                '2,cold_draw,B vs C,observation,"{}"\n'
                '1,balanced_draw,A vs B,standalone,"{}"\n',
                encoding="utf-8",
            )
            (output / "draw_alert_metrics.json").write_text("{}", encoding="utf-8")
            (output / "draw_model_registry.json").write_text("{}", encoding="utf-8")

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web):
                image_path = build_daily_image.draw_report()

            with build_daily_image.Image.open(image_path) as image:
                # Existing geometry for one empty plan and empty ledger is 790px.
                self.assertEqual((1600, 1230), image.size)

    def test_zero_alert_copy_stays_above_following_observation_heading(self):
        class RecordingDraw:
            def __init__(self, drawing, positions):
                self.drawing = drawing
                self.positions = positions

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.positions.append((text, xy))
                return self.drawing.text(xy, text, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text("match,play,odds,stake,selection\n", encoding="utf-8")
            (output / "betting_ledger.csv").write_text("date,play,match,selection,stake,status,profit\n", encoding="utf-8")
            (output / "observation_plan_2026-07-13.csv").write_text(
                "match,selection,odds,probability,raw_model_probability,market_probability\nA vs B,平,3.2,0.3,0.3,0.3\n",
                encoding="utf-8",
            )
            positions = []
            original_draw = build_daily_image.ImageDraw.Draw

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                build_daily_image.ImageDraw,
                "Draw",
                side_effect=lambda image: RecordingDraw(original_draw(image), positions),
            ):
                build_daily_image.draw_report()

            empty_y = next(y for text, (_, y) in positions if text == "今日无符合门槛的平局预警")
            observations_y = next(y for text, (_, y) in positions if text == "零金额观察单")
            self.assertLessEqual(empty_y + build_daily_image.font(20).getbbox("今日无符合门槛的平局预警")[3], observations_y)

    def test_daily_alert_reader_enriches_rows_from_alert_ledger(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            (output / "draw_alert_2026-07-13.csv").write_text(
                "date,rank,subtype,match,settlement_mode\n2026-07-13,1,cold_draw,A vs B,observation\n",
                encoding="utf-8",
            )
            (output / "draw_alert_ledger.csv").write_text(
                "date,subtype,match,status\n2026-07-13,cold_draw,A vs B,命中\n",
                encoding="utf-8",
            )

            with patch.object(build_site, "OUTPUT_DIR", output):
                alerts = build_site.read_draw_alert(date(2026, 7, 13))

            self.assertEqual("命中", alerts[0]["ledger_status"])

            with patch.object(build_daily_image, "OUTPUT_DIR", output):
                image_alerts = build_daily_image.read_draw_alert("2026-07-13")

            self.assertEqual("命中", image_alerts[0]["ledger_status"])


if __name__ == "__main__":
    unittest.main()
