import csv
import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import build_daily_image
import build_site
from build_site import render_draw_alert


def deeply_nested_evidence(depth: int = 1200) -> str:
    return '{"nested":' * depth + '{"source":"<& deep source"}' + "}" * depth


EVIDENCE_LINE_BREAKS = ("\n", "\r", "\t", "\v", "\f", "\u0085", "\u2028", "\u2029")


def multiline_evidence_source() -> tuple[str, str]:
    segments = [f"普通中文符号段{index:02d}甲乙" for index in range(10)]
    segments[0] = "普通中文<&符号段00甲乙"
    separators = ["\n", "\r", "\r\n", "\t", "\v", "\f", "\u0085", "\u2028", "\u2029"]
    source = segments[0]
    for separator, segment in zip(separators, segments[1:]):
        source += separator + segment
    return source, " ".join(segments)


class DrawAlertReportingTest(unittest.TestCase):
    def test_account_control_shows_simulation_gate_and_hard_limits(self):
        html = build_site.render_account_control(
            {
                "status": "no_bet",
                "reason": "今日主方案观望。",
                "account": {
                    "mode": "simulation",
                    "completed_days": 12,
                    "required_settled_days": 30,
                    "monthly_stake": 860,
                    "monthly_budget_cap": 3000,
                    "monthly_profit": -120,
                    "monthly_stop_loss": 500,
                    "review_ready": False,
                    "real_money_automation": False,
                },
            }
        )

        self.assertIn("模拟观察 12/30 天", html)
        self.assertIn("860/3000元", html)
        self.assertIn("今日主方案观望", html)
        self.assertIn("不会自动转为真实投注", html)

    def test_play_metrics_show_profit_roi_and_risk_separately(self):
        html = build_site.render_play_metrics(
            {
                "by_play": {
                    "平局单场": {
                        "count": 10,
                        "hit_rate": 0.4,
                        "stake": 500,
                        "profit": 80,
                        "roi": 0.16,
                        "max_drawdown": 70,
                    },
                    "胜平负串关": {
                        "count": 4,
                        "hit_rate": 0.25,
                        "stake": 120,
                        "profit": -30,
                        "roi": -0.25,
                        "max_drawdown": 60,
                    },
                }
            }
        )

        self.assertIn("平局单场", html)
        self.assertIn("胜平负串关", html)
        self.assertIn("+80元", html)
        self.assertIn("-25.0%", html)
        self.assertIn("最大回撤", html)

    def test_league_calibration_status_is_visible_and_sample_gated(self):
        html = build_site.render_league_calibrations(
            {
                "league_draw_calibration": {
                    "联赛A": {"enabled": False, "sample_count": 12, "adjustment": 0},
                    "联赛B": {
                        "enabled": True,
                        "sample_count": 42,
                        "adjustment": 0.03,
                        "validation_brier_before": 0.220,
                        "validation_brier_after": 0.205,
                    },
                }
            }
        )

        self.assertIn("联赛A", html)
        self.assertIn("观察期 12/30", html)
        self.assertIn("联赛B", html)
        self.assertIn("已启用", html)
        self.assertIn("+3.0%", html)

    def test_no_bet_panel_uses_auditable_daily_reason(self):
        html = build_site.render_betting_plan(
            [],
            [],
            {"status": "no_bet", "reason": "所有候选均未通过正期望值门槛。"},
        )

        self.assertIn("所有候选均未通过正期望值门槛", html)

    def test_site_totals_include_only_valid_paid_stakes(self):
        alerts = [
            {"settlement_mode": "standalone", "additional_stake": "30"},
            {"settlement_mode": "linked", "additional_stake": "99"},
            {"settlement_mode": "observation", "additional_stake": "88"},
            {"settlement_mode": "budget_capped_observation", "additional_stake": "nan"},
            {"settlement_mode": "linked", "linked_main_stake": "inf"},
            {"settlement_mode": "observation", "hypothetical_stake": "-5"},
        ]

        main, draw_alert, total = build_site.today_stake_totals(
            [{"stake": "100"}, {"stake": "0"}], alerts
        )

        self.assertEqual((100.0, 30.0, 130.0), (main, draw_alert, total))

    def test_invalid_paid_amounts_or_combined_budget_fail_closed(self):
        invalid_values = ("499.5", "nan", "inf", "-1", "501", "")
        for value in invalid_values:
            with self.subTest(source="main", value=value):
                self.assertEqual(
                    (None, None, None),
                    build_site.today_stake_totals([{"stake": value}], []),
                )
            with self.subTest(source="alert", value=value):
                self.assertEqual(
                    (None, None, None),
                    build_site.today_stake_totals(
                        [],
                        [{"settlement_mode": "standalone", "additional_stake": value}],
                    ),
                )

        self.assertEqual(
            (None, None, None),
            build_site.today_stake_totals(
                [{"stake": "480"}],
                [{"settlement_mode": "standalone", "additional_stake": "30"}],
            ),
        )

    def test_invalid_paid_amount_renders_stop_investment_warning(self):
        html = build_site.render_betting_plan(
            [{"stake": "499.5", "play": "胜平负"}],
            [{"settlement_mode": "standalone", "additional_stake": "30"}],
        )

        self.assertIn("金额数据异常，停止新增投入", html)
        self.assertNotIn("529.5", html)
        self.assertNotIn("530元", html)

    def test_draw_alert_numeric_fields_use_domain_bounds(self):
        valid = {
            "domestic_draw_odds": ("1.01", "100"),
            "model_draw_probability": ("0", "1"),
            "market_draw_probability": ("0", "1"),
            "draw_edge": ("-1", "1"),
            "expected_value": ("0.001", "100"),
            "xg_total": ("0.001", "10"),
        }
        invalid = {
            "domestic_draw_odds": ("1", "100.01"),
            "model_draw_probability": ("-0.01", "1.01"),
            "market_draw_probability": ("-0.01", "1.01"),
            "draw_edge": ("-1.01", "1.01"),
            "expected_value": ("0", "100.01"),
            "xg_total": ("0", "-0.01", "10.01"),
        }
        for key, values in valid.items():
            for value in values:
                with self.subTest(key=key, value=value, valid=True):
                    self.assertIsNotNone(build_site.draw_alert_value({key: value}, key))
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value, valid=False):
                    self.assertIsNone(build_site.draw_alert_value({key: value}, key))

    def test_empty_main_plan_reports_paid_draw_alert_instead_of_no_bet(self):
        html = build_site.render_betting_plan(
            [],
            [{"settlement_mode": "standalone", "additional_stake": "30"}],
        )

        self.assertIn("今日模拟投入 30元", html)
        self.assertIn("主方案为空，但有平局预警投入 30元", html)
        self.assertNotIn("因此不模拟投注", html)

    def test_alert_level_is_displayed_only_for_supported_levels(self):
        high = render_draw_alert(
            [
                {
                    "rank": "1",
                    "subtype": "cold_draw",
                    "match": "A vs B",
                    "settlement_mode": "observation",
                    "alert_level": "高级",
                }
            ]
        )
        invalid = render_draw_alert(
            [
                {
                    "rank": "1",
                    "subtype": "cold_draw",
                    "match": "A vs B",
                    "settlement_mode": "observation",
                    "alert_level": "rank_999<script>",
                }
            ]
        )

        self.assertIn("高级", high)
        self.assertNotIn("rank_999", invalid)
        self.assertNotIn("&lt;script&gt;", invalid)

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

    def test_daily_image_totals_standalone_alert_and_shows_level(self):
        class RecordingDraw:
            def __init__(self, drawing, texts):
                self.drawing = drawing
                self.texts = texts

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, value, *args, **kwargs):
                self.texts.append(str(value))
                return self.drawing.text(xy, value, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text(
                "match,play,odds,stake,selection\nA vs B,胜平负,2.0,100,胜\n",
                encoding="utf-8",
            )
            (output / "betting_ledger.csv").write_text(
                "date,play,match,selection,stake,status,profit\n", encoding="utf-8"
            )
            with (output / "draw_alert_2026-07-13.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "date",
                        "rank",
                        "subtype",
                        "match",
                        "settlement_mode",
                        "additional_stake",
                        "linked_main_stake",
                        "hypothetical_stake",
                        "alert_level",
                    ],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "date": "2026-07-13",
                            "rank": "1",
                            "subtype": "cold_draw",
                            "match": "C vs D",
                            "settlement_mode": "standalone",
                            "additional_stake": "30",
                            "alert_level": "高级",
                        },
                        {
                            "date": "2026-07-13",
                            "rank": "2",
                            "subtype": "balanced_draw",
                            "match": "E vs F",
                            "settlement_mode": "linked",
                            "additional_stake": "99",
                            "linked_main_stake": "100",
                            "hypothetical_stake": "50",
                        },
                    ]
                )
            (output / "draw_alert_metrics.json").write_text("{}", encoding="utf-8")
            (output / "draw_model_registry.json").write_text("{}", encoding="utf-8")
            texts = []
            original_draw = build_daily_image.ImageDraw.Draw

            def recording_draw(image):
                return RecordingDraw(original_draw(image), texts)

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(
                build_daily_image, "WEB_DIR", web
            ), patch.object(build_daily_image.ImageDraw, "Draw", side_effect=recording_draw):
                build_daily_image.draw_report()

            self.assertIn("130 元", texts)
            self.assertTrue(any("高级" in value for value in texts), texts)

            (output / "betting_plan_2026-07-13.csv").write_text(
                "match,play,odds,stake,selection\n", encoding="utf-8"
            )
            alert_path = output / "draw_alert_2026-07-13.csv"
            alert_path.write_text(
                alert_path.read_text(encoding="utf-8").replace("高级", "中级"),
                encoding="utf-8",
            )
            texts.clear()
            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(
                build_daily_image, "WEB_DIR", web
            ), patch.object(
                build_daily_image.ImageDraw, "Draw", side_effect=recording_draw
            ):
                build_daily_image.draw_report()

            self.assertIn("30 元", texts)
            self.assertIn("主方案为空，但有平局预警投入 30 元", texts)
            self.assertTrue(any("中级" in value for value in texts), texts)

    def test_daily_image_fails_closed_on_invalid_paid_amounts(self):
        class RecordingDraw:
            def __init__(self, drawing, texts):
                self.drawing = drawing
                self.texts = texts

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, value, *args, **kwargs):
                self.texts.append(str(value))
                return self.drawing.text(xy, value, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2099-01-01.csv").write_text(
                "match,play,odds,stake,selection\nA vs B,spf,2.0,499.5,draw\n",
                encoding="utf-8",
            )
            (output / "betting_ledger.csv").write_text(
                "date,play,match,selection,stake,status,profit\n", encoding="utf-8"
            )
            (output / "draw_alert_2099-01-01.csv").write_text(
                "date,rank,subtype,match,settlement_mode,additional_stake\n"
                "2099-01-01,1,cold_draw,C vs D,standalone,30\n",
                encoding="utf-8",
            )
            texts = []
            original_draw = build_daily_image.ImageDraw.Draw

            def recording_draw(image):
                return RecordingDraw(original_draw(image), texts)

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(
                build_daily_image, "WEB_DIR", web
            ), patch.object(build_daily_image.ImageDraw, "Draw", side_effect=recording_draw):
                build_daily_image.draw_report()

            self.assertIn("停止投入", texts)
            self.assertIn("金额数据异常，停止新增投入", texts)
            self.assertFalse(any("529.5" in value or "530 元" in value for value in texts))
            self.assertFalse(any("499.5" in value for value in texts))

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
                self.positions.append((text, xy, kwargs))
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

            empty_y, empty_font = next((y, kwargs["font"]) for text, (_, y), kwargs in positions if text == "今日无符合门槛的平局预警")
            observations_y = next(y for text, (_, y), _ in positions if text == "零金额观察单")
            self.assertLessEqual(empty_y + empty_font.getbbox("今日无符合门槛的平局预警")[3], observations_y)

    def test_crowded_zero_alert_header_lines_do_not_overlap_inside_fixed_block(self):
        class RecordingDraw:
            def __init__(self, drawing, calls):
                self.drawing = drawing
                self.calls = calls

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.calls.append((text, xy, kwargs))
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
            (output / "draw_alert_metrics.json").write_text(
                json.dumps(
                    {
                        "subtypes": {
                            "cold_draw": {"count": 29},
                            "balanced_draw": {"count": 30, "promoted": True},
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (output / "draw_model_registry.json").write_text(
                json.dumps(
                    {
                        "champion": {"version": "冠军模型版本" * 12},
                        "challenger": {
                            "version": "挑战者模型版本" * 12,
                            "shadow_days": 28,
                            "sample_count": 30,
                            "bet_count": 12,
                        },
                        "per_league": {"超长暂停联赛名称" * 12: {"paused": True}},
                        "last_training_error": "训练错误详情" * 40,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            calls = []
            original_draw = build_daily_image.ImageDraw.Draw

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                build_daily_image.ImageDraw,
                "Draw",
                side_effect=lambda image: RecordingDraw(original_draw(image), calls),
            ):
                image_path = build_daily_image.draw_report()

            with build_daily_image.Image.open(image_path) as image:
                self.assertEqual((1600, 1018), image.size)

        def find_call(prefix: str, *, exact: bool = False):
            return next(
                call
                for call in calls
                if (call[0] == prefix if exact else call[0].startswith(prefix))
            )

        header_calls = {
            "title": find_call("平局预警", exact=True),
            "subtypes": find_call("冷门平局"),
            "model": find_call("冠军 "),
            "paused": find_call("暂停联赛："),
            "error": find_call("最近训练异常："),
            "empty": find_call("今日无符合门槛的平局预警", exact=True),
        }
        following_y = find_call("零金额观察单", exact=True)[1][1]
        header_top = following_y - 100
        header_bottom = following_y
        measurement = build_daily_image.ImageDraw.Draw(build_daily_image.Image.new("RGB", (1600, 10)))
        boxes = {
            key: measurement.textbbox(call[1], call[0], font=call[2]["font"])
            for key, call in header_calls.items()
        }

        for key, box in boxes.items():
            self.assertGreaterEqual(box[1], header_top, key)
            self.assertLessEqual(box[3], header_bottom, key)
            self.assertGreaterEqual(box[0], 70, key)
            self.assertLessEqual(box[2], build_daily_image.WIDTH - 70, key)

        rows = [
            ("title", "subtypes"),
            ("model", "paused"),
            ("error",),
            ("empty",),
        ]
        row_bands = [
            (min(boxes[key][1] for key in row), max(boxes[key][3] for key in row))
            for row in rows
        ]
        for upper, lower in zip(row_bands, row_bands[1:]):
            self.assertLessEqual(upper[1], lower[0], (upper, lower))

        box_values = list(boxes.items())
        for index, (left_key, left) in enumerate(box_values):
            for right_key, right in box_values[index + 1:]:
                intersects = left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]
                self.assertFalse(intersects, (left_key, left, right_key, right))

    def test_crowded_zero_alert_header_normalizes_all_external_line_separators(self):
        class RecordingDraw:
            def __init__(self, drawing, calls):
                self.drawing = drawing
                self.calls = calls

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.calls.append((text, xy, kwargs))
                return self.drawing.text(xy, text, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text(
                "match,play,odds,stake,selection\n",
                encoding="utf-8",
            )
            (output / "betting_ledger.csv").write_text(
                "date,play,match,selection,stake,status,profit\n",
                encoding="utf-8",
            )
            (output / "observation_plan_2026-07-13.csv").write_text(
                "match,selection,odds,probability,raw_model_probability,market_probability\n"
                "A vs B,平,3.2,0.3,0.3,0.3\n",
                encoding="utf-8",
            )
            (output / "draw_alert_metrics.json").write_text(
                json.dumps(
                    {
                        "subtypes": {
                            "cold_draw": {"count": 29},
                            "balanced_draw": {"count": 30, "promoted": True},
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (output / "draw_model_registry.json").write_text(
                json.dumps(
                    {
                        "champion": {"version": "冠\r军\n模型\u0085版本"},
                        "challenger": {
                            "version": "挑\n战者\r\n模型\u2028版本",
                            "shadow_days": 28,
                            "sample_count": 30,
                            "bet_count": 12,
                        },
                        "per_league": {"英\r\n超\u2029联赛\t测试\v名称": {"paused": True}},
                        "last_training_error": "训练\r\n错误\u2028详情\u2029补充\u0085信息\f" * 20,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            calls = []
            original_draw = build_daily_image.ImageDraw.Draw
            subtype_labels = {
                "cold_draw": "冷\r\n门\u2028平局",
                "balanced_draw": "均衡\u2029平局",
            }

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(
                build_daily_image,
                "WEB_DIR",
                web,
            ), patch.object(build_daily_image, "SUBTYPE_LABELS", subtype_labels), patch.object(
                build_daily_image.ImageDraw,
                "Draw",
                side_effect=lambda image: RecordingDraw(original_draw(image), calls),
            ):
                image_path = build_daily_image.draw_report()

            with build_daily_image.Image.open(image_path) as image:
                self.assertEqual((1600, 1018), image.size)

        following_y = next(y for text, (_, y), _ in calls if text == "零金额观察单")
        header_top = following_y - 100
        header_calls = [
            call
            for call in calls
            if header_top <= call[1][1] < following_y
        ]
        self.assertEqual(6, len(header_calls))

        for text, _, _ in header_calls:
            self.assertEqual(" ".join(text.split()), text, repr(text))
            for separator in EVIDENCE_LINE_BREAKS:
                self.assertNotIn(separator, text, repr(text))

        header_text = " | ".join(text for text, _, _ in header_calls)
        for expected in (
            "冷 门 平局",
            "冠军 冠 军 模型 版本",
            "挑战者 挑 战者 模型 版本",
            "暂停联赛：英 超 联赛 测试 名称",
            "最近训练异常：训练 错误 详情 补充 信息",
            "今日无符合门槛的平局预警",
        ):
            self.assertIn(expected, header_text)

        measurement = build_daily_image.ImageDraw.Draw(build_daily_image.Image.new("RGB", (1600, 10)))
        boxes = [
            measurement.textbbox(xy, text, font=kwargs["font"])
            for text, xy, kwargs in header_calls
        ]
        for box in boxes:
            self.assertGreaterEqual(box[0], 70, box)
            self.assertLessEqual(box[2], build_daily_image.WIDTH - 70, box)
            self.assertGreaterEqual(box[1], header_top, box)
            self.assertLessEqual(box[3], following_y, box)

        for index, left in enumerate(boxes):
            for right in boxes[index + 1:]:
                intersects = left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]
                self.assertFalse(intersects, (left, right))

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

    def test_daily_image_heading_lists_paused_leagues(self):
        _, _, paused = build_daily_image.draw_alert_heading(
            {},
            {
                "per_league": {
                    "<英超>&": {"paused": True},
                    "西甲": {"paused": False},
                }
            },
        )

        self.assertIn("暂停联赛：<英超>&", paused)

    def test_daily_image_alert_metrics_show_dash_for_missing_or_invalid_values(self):
        class RecordingDraw:
            def __init__(self, drawing, calls):
                self.drawing = drawing
                self.calls = calls

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.calls.append((text, xy, kwargs))
                return self.drawing.text(xy, text, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text("match,play,odds,stake,selection\n", encoding="utf-8")
            (output / "betting_ledger.csv").write_text("date,play,match,selection,stake,status,profit\n", encoding="utf-8")
            (output / "draw_alert_2026-07-13.csv").write_text(
                "date,rank,subtype,match,settlement_mode,domestic_draw_odds,model_draw_probability,market_draw_probability,draw_edge,expected_value,xg_total\n"
                "2026-07-13,1,cold_draw,A vs B,observation,invalid,,,not-a-number,none,\n",
                encoding="utf-8",
            )
            calls = []
            original_draw = build_daily_image.ImageDraw.Draw

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                build_daily_image.ImageDraw,
                "Draw",
                side_effect=lambda image: RecordingDraw(original_draw(image), calls),
            ):
                build_daily_image.draw_report()

        metrics = next(text for text, _, _ in calls if text.startswith("官方平赔"))
        self.assertIn("官方平赔 -", metrics)
        self.assertIn("模型 -", metrics)
        self.assertIn("市场 -", metrics)
        self.assertIn("优势 -", metrics)
        self.assertIn("期望值 -", metrics)
        self.assertIn("xG总和 -", metrics)
        self.assertNotIn("0.0%", metrics)

    def test_daily_image_alert_formatters_reject_out_of_range_values(self):
        alert = {
            "domestic_draw_odds": "0",
            "model_draw_probability": "1.2",
            "market_draw_probability": "-0.1",
            "draw_edge": "nan",
            "expected_value": "inf",
            "xg_total": "-0.5",
        }

        self.assertEqual("-", build_daily_image.alert_odds(alert))
        self.assertEqual("-", build_daily_image.alert_decimal(alert, "model_draw_probability", percentage=True))
        self.assertEqual("-", build_daily_image.alert_decimal(alert, "market_draw_probability", percentage=True))
        self.assertEqual("-", build_daily_image.alert_decimal(alert, "draw_edge", signed=True, percentage=True))
        self.assertEqual("-", build_daily_image.alert_decimal(alert, "expected_value"))
        self.assertEqual("-", build_daily_image.alert_decimal(alert, "xg_total"))

    def test_daily_image_alert_edge_keeps_valid_negative_values(self):
        self.assertEqual("-2.0%", build_daily_image.alert_decimal({"draw_edge": "-0.02"}, "draw_edge", signed=True, percentage=True))

    def test_four_long_alert_rows_keep_raw_text_and_stay_within_image_bounds(self):
        class RecordingDraw:
            def __init__(self, drawing, calls):
                self.drawing = drawing
                self.calls = calls

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.calls.append((text, xy, kwargs))
                return self.drawing.text(xy, text, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text("match,play,odds,stake,selection\n", encoding="utf-8")
            (output / "betting_ledger.csv").write_text("date,play,match,selection,stake,status,profit\n", encoding="utf-8")
            rows = [
                "2026-07-13,{rank},cold_draw,{text},budget_capped_observation,3.60,0.32,0.27,0.05,1.15,2.10,\"{{\\\"source\\\": \\\"{text}\\\"}}\",{text},2026-07-13T13:30:00+08:00\n".format(
                    rank=rank,
                    text="<&超长外部比赛名称和状态文本" * 18,
                )
                for rank in range(1, 5)
            ]
            (output / "draw_alert_2026-07-13.csv").write_text(
                "date,rank,subtype,match,settlement_mode,domestic_draw_odds,model_draw_probability,market_draw_probability,draw_edge,expected_value,xg_total,evidence_json,data_quality,captured_at\n"
                + "".join(rows),
                encoding="utf-8",
            )
            (output / "draw_alert_metrics.json").write_text("{}", encoding="utf-8")
            (output / "draw_model_registry.json").write_text(
                '{"champion":{"version":"<&冠军"},"challenger":{"version":"<&挑战者","shadow_days":28,"sample_count":30,"bet_count":12},"per_league":{"<&超长暂停联赛": {"paused": true}},"last_training_error":"<&超长训练错误"}',
                encoding="utf-8",
            )
            calls = []
            original_draw = build_daily_image.ImageDraw.Draw

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                build_daily_image.ImageDraw,
                "Draw",
                side_effect=lambda image: RecordingDraw(original_draw(image), calls),
            ):
                build_daily_image.draw_report()

        rendered = "\n".join(text for text, _, _ in calls)
        self.assertIn("<&", rendered)
        self.assertNotIn("&lt;", rendered)
        self.assertNotIn("&amp;", rendered)
        measurement = build_daily_image.ImageDraw.Draw(build_daily_image.Image.new("RGB", (1600, 10)))
        for text, (x, _), kwargs in calls:
            width = measurement.textbbox((0, 0), text, font=kwargs["font"])[2]
            self.assertLessEqual(x + width, build_daily_image.WIDTH - 70, text)

    def test_website_draw_metrics_use_field_specific_validation(self):
        invalid_alerts = [
            {
                "rank": "1",
                "subtype": "cold_draw",
                "match": "missing values",
                "settlement_mode": "observation",
            },
            {
                "rank": "2",
                "subtype": "cold_draw",
                "match": "non numeric values",
                "settlement_mode": "observation",
                "domestic_draw_odds": "not-a-number",
                "model_draw_probability": "bad",
                "market_draw_probability": "bad",
                "draw_edge": "bad",
                "expected_value": "bad",
                "xg_total": "bad",
            },
            {
                "rank": "3",
                "subtype": "cold_draw",
                "match": "non finite values",
                "settlement_mode": "observation",
                "domestic_draw_odds": "nan",
                "model_draw_probability": "nan",
                "market_draw_probability": "inf",
                "draw_edge": "-inf",
                "expected_value": "nan",
                "xg_total": "inf",
            },
            {
                "rank": "4",
                "subtype": "cold_draw",
                "match": "out of range values",
                "settlement_mode": "observation",
                "domestic_draw_odds": "0",
                "model_draw_probability": "1.2",
                "market_draw_probability": "-0.1",
                "draw_edge": "1.1",
                "expected_value": "-0.01",
                "xg_total": "0",
            },
        ]

        html = render_draw_alert(invalid_alerts)

        labels = [
            "\u5b98\u65b9\u5e73\u8d54",
            "\u6a21\u578b",
            "\u5e02\u573a",
            "\u4f18\u52bf",
            "\u671f\u671b\u503c",
            "xG \u603b\u548c",
        ]
        for label in labels:
            self.assertEqual(4, html.count(f"<span>{label} <strong>-</strong></span>"), label)

        valid_html = render_draw_alert([
            {
                "rank": "1",
                "subtype": "balanced_draw",
                "match": "valid boundary values",
                "settlement_mode": "observation",
                "domestic_draw_odds": "3.60",
                "model_draw_probability": "0",
                "market_draw_probability": "1",
                "draw_edge": "-1",
                "expected_value": "0.001",
                "xg_total": "0.001",
            }
        ])
        self.assertIn("<strong>3.60</strong>", valid_html)
        self.assertIn("<strong>0.0%</strong>", valid_html)
        self.assertIn("<strong>100.0%</strong>", valid_html)
        self.assertIn("<strong>-100.0%</strong>", valid_html)
        self.assertEqual(2, valid_html.count("<strong>0.001</strong>"))

        nonpositive_html = render_draw_alert([
            {
                "rank": "1",
                "subtype": "balanced_draw",
                "match": "nonpositive values",
                "settlement_mode": "observation",
                "domestic_draw_odds": "-2",
                "model_draw_probability": "0.5",
                "market_draw_probability": "0.5",
                "draw_edge": "0",
                "expected_value": "0",
                "xg_total": "-0.001",
            }
        ])
        for label in ("\u5b98\u65b9\u5e73\u8d54", "\u671f\u671b\u503c", "xG \u603b\u548c"):
            self.assertIn(f"<span>{label} <strong>-</strong></span>", nonpositive_html)
        self.assertIn("<span>\u4f18\u52bf <strong>0.0%</strong></span>", nonpositive_html)

    def test_as_int_and_web_alert_ranks_handle_nonfinite_and_huge_values(self):
        for value in ("inf", "-inf", "nan", "1e308", "1e999", "9" * 400):
            self.assertEqual(999, build_site.as_int(value, 999), value)

        html = render_draw_alert([
            {"rank": "inf", "subtype": "cold_draw", "match": "invalid inf", "settlement_mode": "observation"},
            {"rank": "3", "subtype": "cold_draw", "match": "valid three", "settlement_mode": "observation"},
            {"rank": "nan", "subtype": "cold_draw", "match": "invalid nan", "settlement_mode": "observation"},
            {"rank": "1", "subtype": "cold_draw", "match": "valid one", "settlement_mode": "observation"},
        ])

        positions = [html.index(match) for match in ("valid one", "valid three", "invalid inf", "invalid nan")]
        self.assertEqual(sorted(positions), positions)
        self.assertEqual(2, html.count("\u672a\u6392\u540d"))
        self.assertNotIn("\u7b2c999\u573a", html)

        huge_rank_html = render_draw_alert([
            {"rank": "9" * 400, "subtype": "cold_draw", "match": "invalid huge", "settlement_mode": "observation"}
        ])
        self.assertIn("\u672a\u6392\u540d", huge_rank_html)

    def test_daily_image_invalid_ranks_sort_after_valid_and_invalid_metrics_show_dash(self):
        class RecordingDraw:
            def __init__(self, drawing, calls):
                self.drawing = drawing
                self.calls = calls

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.calls.append((text, xy, kwargs))
                return self.drawing.text(xy, text, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text("match,play,odds,stake,selection\n", encoding="utf-8")
            (output / "betting_ledger.csv").write_text("date,play,match,selection,stake,status,profit\n", encoding="utf-8")
            (output / "draw_alert_2026-07-13.csv").write_text(
                "date,rank,subtype,match,settlement_mode,domestic_draw_odds,model_draw_probability,market_draw_probability,draw_edge,expected_value,xg_total\n"
                "2026-07-13,inf,cold_draw,invalid inf,observation,0,1.2,nan,1.1,-0.1,0\n"
                "2026-07-13,2,cold_draw,valid rank,observation,not-a-number,bad,bad,bad,bad,bad\n"
                "2026-07-13,nan,cold_draw,invalid nan,observation,inf,-0.1,2,-1.1,nan,-0.5\n",
                encoding="utf-8",
            )
            calls = []
            original_draw = build_daily_image.ImageDraw.Draw

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                build_daily_image.ImageDraw,
                "Draw",
                side_effect=lambda image: RecordingDraw(original_draw(image), calls),
            ):
                image_path = build_daily_image.draw_report()

            with build_daily_image.Image.open(image_path) as image:
                self.assertEqual((1600, 1400), image.size)

        titles = [
            text
            for text, _, _ in calls
            if text.startswith("\u7b2c") or text.startswith("\u672a\u6392\u540d")
        ]
        self.assertEqual(3, len(titles))
        self.assertTrue(titles[0].startswith("\u7b2c2\u573a"), titles)
        self.assertTrue(all(title.startswith("\u672a\u6392\u540d") for title in titles[1:]), titles)
        metrics = [text for text, _, _ in calls if text.startswith("\u5b98\u65b9\u5e73\u8d54")]
        self.assertEqual(3, len(metrics))
        for metric in metrics:
            for label in ("\u5b98\u65b9\u5e73\u8d54", "\u6a21\u578b", "\u5e02\u573a", "\u4f18\u52bf", "\u671f\u671b\u503c", "xG\u603b\u548c"):
                self.assertIn(f"{label} -", metric)

    def test_deep_evidence_json_falls_back_safely_on_website(self):
        evidence = deeply_nested_evidence()
        try:
            html = render_draw_alert([
                {
                    "rank": "1",
                    "subtype": "cold_draw",
                    "match": "deep evidence",
                    "settlement_mode": "observation",
                    "evidence_json": evidence,
                }
            ])
        except RecursionError as error:
            self.fail(f"deep website evidence raised RecursionError: {error}")

        self.assertIn("\u8bc1\u636e\u7ed3\u6784\u8fc7\u6df1", html)
        self.assertNotIn("deep source", html)
        self.assertLessEqual(len(build_site.evidence_source_summary(evidence)), 160)

    def test_evidence_summary_limits_node_and_text_budgets(self):
        many_nodes = json.dumps(
            {"items": [{"source": f"provider-{index}"} for index in range(300)]},
            ensure_ascii=False,
        )
        long_source = json.dumps({"source": "<&\u8d85\u957f\u6765\u6e90" * 200}, ensure_ascii=False)

        self.assertEqual("\u8bc1\u636e\u6765\u6e90\u5df2\u622a\u65ad", build_site.evidence_source_summary(many_nodes))
        long_summary = build_site.evidence_source_summary(long_source)
        self.assertEqual("\u8bc1\u636e\u6765\u6e90\u5df2\u622a\u65ad", long_summary)
        self.assertLessEqual(len(long_summary), 160)

    def test_evidence_summary_and_web_normalize_all_line_break_whitespace(self):
        source, expected = multiline_evidence_source()
        evidence = json.dumps({"source": source}, ensure_ascii=False)

        summary = build_site.evidence_source_summary(evidence)
        self.assertEqual(expected, summary)
        for character in EVIDENCE_LINE_BREAKS:
            self.assertNotIn(character, summary)

        html = render_draw_alert([
            {
                "rank": "1",
                "subtype": "cold_draw",
                "match": "multiline evidence",
                "settlement_mode": "observation",
                "evidence_json": evidence,
            }
        ])
        evidence_fragment = html.split("<p><span>\u8bc1\u636e\u6765\u6e90</span>", 1)[1].split("</p>", 1)[0]
        self.assertIn("\u666e\u901a\u4e2d\u6587&lt;&amp;\u7b26\u53f7", evidence_fragment)
        for character in EVIDENCE_LINE_BREAKS:
            self.assertNotIn(character, evidence_fragment)

    def test_evidence_input_limit_is_applied_before_json_parsing(self):
        oversized = json.dumps(
            {"source": "x" * (build_site.EVIDENCE_MAX_INPUT_CHARS + 1)},
            ensure_ascii=False,
        )
        self.assertGreater(len(oversized), build_site.EVIDENCE_MAX_INPUT_CHARS)

        with patch.object(build_site.json, "loads", side_effect=AssertionError("oversized evidence was parsed")) as loads:
            summary = build_site.evidence_source_summary(oversized)

        self.assertEqual(build_site.EVIDENCE_TRUNCATED, summary)
        loads.assert_not_called()

    def test_joined_evidence_sources_respect_160_character_summary_limit(self):
        evidence = json.dumps(
            [{"source": "甲" * 80}, {"provider": "乙" * 80}],
            ensure_ascii=False,
        )

        summary = build_site.evidence_source_summary(evidence)

        self.assertEqual(build_site.EVIDENCE_TRUNCATED, summary)
        self.assertEqual(160, getattr(build_site, "EVIDENCE_MAX_SUMMARY_CHARS", None))
        self.assertLessEqual(len(summary), 160)

    def test_daily_image_evidence_draw_calls_are_single_line_and_stay_in_alert_row(self):
        class RecordingDraw:
            def __init__(self, drawing, calls):
                self.drawing = drawing
                self.calls = calls

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.calls.append((text, xy, kwargs))
                return self.drawing.text(xy, text, *args, **kwargs)

        source, expected = multiline_evidence_source()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text("match,play,odds,stake,selection\n", encoding="utf-8")
            (output / "betting_ledger.csv").write_text("date,play,match,selection,stake,status,profit\n", encoding="utf-8")
            alert_path = output / "draw_alert_2026-07-13.csv"
            fieldnames = ["date", "rank", "subtype", "match", "settlement_mode", "evidence_json"]
            with alert_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {
                        "date": "2026-07-13",
                        "rank": "1",
                        "subtype": "cold_draw",
                        "match": "multiline evidence",
                        "settlement_mode": "observation",
                        "evidence_json": json.dumps({"source": source}, ensure_ascii=False),
                    }
                )
            calls = []
            original_draw = build_daily_image.ImageDraw.Draw

            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                build_daily_image.ImageDraw,
                "Draw",
                side_effect=lambda image: RecordingDraw(original_draw(image), calls),
            ):
                image_path = build_daily_image.draw_report()

            with build_daily_image.Image.open(image_path) as image:
                self.assertEqual((1600, 1060), image.size)

        for text, _, _ in calls:
            for character in EVIDENCE_LINE_BREAKS:
                self.assertNotIn(character, text)
        _, (_, title_y), _ = next(call for call in calls if call[0].startswith("\u7b2c1\u573a"))
        alert_top = title_y - 14
        evidence_calls = [call for call in calls if call[1][1] in {alert_top + 105, alert_top + 125}]
        self.assertGreaterEqual(len(evidence_calls), 1)
        self.assertLessEqual(len(evidence_calls), 2)
        rendered_evidence = "".join(text for text, _, _ in evidence_calls)
        self.assertIn("<&", rendered_evidence)
        self.assertIn(expected.split(" ", 1)[0], rendered_evidence)
        measurement = build_daily_image.ImageDraw.Draw(build_daily_image.Image.new("RGB", (1600, 10)))
        for text, (x, y), kwargs in evidence_calls:
            bounds = measurement.textbbox((x, y), text, font=kwargs["font"])
            self.assertLessEqual(bounds[3], alert_top + 154, text)

    def test_deep_evidence_json_falls_back_safely_in_daily_image(self):
        class RecordingDraw:
            def __init__(self, drawing, calls):
                self.drawing = drawing
                self.calls = calls

            def __getattr__(self, name):
                return getattr(self.drawing, name)

            def text(self, xy, text, *args, **kwargs):
                self.calls.append((text, xy, kwargs))
                return self.drawing.text(xy, text, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2026-07-13.csv").write_text("match,play,odds,stake,selection\n", encoding="utf-8")
            (output / "betting_ledger.csv").write_text("date,play,match,selection,stake,status,profit\n", encoding="utf-8")
            alert_path = output / "draw_alert_2026-07-13.csv"
            fieldnames = ["date", "rank", "subtype", "match", "settlement_mode", "evidence_json"]
            with alert_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {
                        "date": "2026-07-13",
                        "rank": "1",
                        "subtype": "cold_draw",
                        "match": "deep evidence",
                        "settlement_mode": "observation",
                        "evidence_json": deeply_nested_evidence(),
                    }
                )
            calls = []
            original_draw = build_daily_image.ImageDraw.Draw

            try:
                with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                    build_daily_image.ImageDraw,
                    "Draw",
                    side_effect=lambda image: RecordingDraw(original_draw(image), calls),
                ):
                    image_path = build_daily_image.draw_report()
            except RecursionError as error:
                self.fail(f"deep image evidence raised RecursionError: {error}")

            with build_daily_image.Image.open(image_path) as image:
                self.assertEqual((1600, 1060), image.size)

        evidence_calls = [call for call in calls if call[0].startswith("\u8bc1\u636e\u6765\u6e90\uff1a")]
        self.assertGreaterEqual(len(evidence_calls), 1)
        self.assertLessEqual(len(evidence_calls), 2)
        self.assertIn("\u8bc1\u636e\u7ed3\u6784\u8fc7\u6df1", "".join(text for text, _, _ in evidence_calls))
        self.assertNotIn("&lt;", "".join(text for text, _, _ in evidence_calls))
        measurement = build_daily_image.ImageDraw.Draw(build_daily_image.Image.new("RGB", (1600, 10)))
        for text, (x, _), kwargs in evidence_calls:
            width = measurement.textbbox((0, 0), text, font=kwargs["font"])[2]
            self.assertLessEqual(x + width, build_daily_image.WIDTH - 70, text)


if __name__ == "__main__":
    unittest.main()
