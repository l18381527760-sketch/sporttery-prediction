import argparse
import csv
import json
import os
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from betting_ledger import resolve_ledger_path
from provisional_plan import read_valid_provisional_state
from build_site import (
    SUBTYPE_LABELS,
    SETTLEMENT_LABELS,
    alert_amount,
    alert_level_label,
    alert_rank,
    alert_rank_label,
    as_int,
    draw_alert_value,
    evidence_source_summary,
    external_text,
    normalize_evidence_whitespace,
    today_stake_totals,
)


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
WEB_DIR = ROOT / "web"
WIDTH = 1600
BUILD_ID = os.environ.get("REPORT_BUILD_ID", "local")
BEIJING = timezone(timedelta(hours=8))
REPORT_STAGES = ("daily", "forecast", "provisional", "settlement")


def read_csv(path: Path) -> list[dict]:
    try:
        path = resolve_ledger_path(path)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error, ValueError):
        return []


def read_metrics() -> dict:
    path = OUTPUT_DIR / "model_metrics.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_draw_alert(report_date: str) -> list[dict]:
    alerts = read_csv(OUTPUT_DIR / f"draw_alert_{report_date}.csv")
    ledger = {
        (
            external_text(row.get("date")).strip(),
            external_text(row.get("subtype")).strip(),
            external_text(row.get("match")).strip(),
        ): row
        for row in read_csv(OUTPUT_DIR / "draw_alert_ledger.csv")
    }
    return [
        {
            **alert,
            "ledger_status": ledger.get(
                (
                    external_text(alert.get("date")).strip(),
                    external_text(alert.get("subtype")).strip(),
                    external_text(alert.get("match")).strip(),
                ),
                {},
            ).get("status", ""),
        }
        for alert in alerts
    ]


def read_draw_json(filename: str) -> dict:
    path = OUTPUT_DIR / filename
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def find_font() -> str:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return "DejaVuSans.ttf"


FONT_PATH = find_font()


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def number(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def alert_number(row: dict, key: str) -> float | None:
    return draw_alert_value(row, key)


def alert_decimal(row: dict, key: str, *, signed: bool = False, percentage: bool = False) -> str:
    value = alert_number(row, key)
    if value is None:
        return "-"
    if percentage:
        return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"
    return f"{value:+.3f}" if signed else f"{value:.3f}"


def alert_odds(row: dict) -> str:
    value = alert_number(row, "domestic_draw_odds")
    return external_text(row.get("domestic_draw_odds")).strip() if value is not None else "-"


def wrap(value: str, width: int) -> list[str]:
    return textwrap.wrap(value or "-", width=width, break_long_words=True) or ["-"]


def text_width(draw: ImageDraw.ImageDraw, value: str, text_font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), value, font=text_font)
    return box[2] - box[0]


def heading_text(value: object) -> str:
    return normalize_evidence_whitespace(value)


def fit_text(draw: ImageDraw.ImageDraw, value: object, text_font: ImageFont.FreeTypeFont, max_width: int) -> str:
    text = external_text(value).strip() or "-"
    if text_width(draw, text, text_font) <= max_width:
        return text
    ellipsis = "…"
    if text_width(draw, ellipsis, text_font) > max_width:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if text_width(draw, text[:middle] + ellipsis, text_font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + ellipsis


def wrap_image_text(
    draw: ImageDraw.ImageDraw,
    value: object,
    text_font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    remaining = external_text(value).strip() or "-"
    lines = []
    while remaining and len(lines) < max_lines:
        fitted = fit_text(draw, remaining, text_font, max_width)
        if fitted == remaining:
            lines.append(fitted)
            remaining = ""
            break
        prefix = fitted.removesuffix("…")
        break_at = prefix.rfind(" ")
        if break_at > 0:
            prefix = prefix[:break_at]
        lines.append(prefix)
        remaining = remaining[len(prefix):].lstrip()
    if remaining and lines:
        lines[-1] = fit_text(draw, lines[-1] + "…", text_font, max_width)
    return lines or ["-"]


def draw_fitted_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    value: object,
    text_font: ImageFont.FreeTypeFont,
    fill: str,
    max_width: int,
) -> None:
    draw.text(xy, fit_text(draw, value, text_font, max_width), font=text_font, fill=fill)


def draw_alert_heading(metrics: dict, registry: dict) -> tuple[str, str, str]:
    subtypes = metrics.get("subtypes", metrics) if isinstance(metrics, dict) else {}
    subtypes = subtypes if isinstance(subtypes, dict) else {}
    progress_items = []
    for key, label in SUBTYPE_LABELS.items():
        item = subtypes.get(key)
        item = item if isinstance(item, dict) else {}
        progress_items.append(f"{heading_text(label)} {max(0, as_int(item.get('count')))}/30")
    progress = " · ".join(progress_items)
    champion = registry.get("champion") if isinstance(registry.get("champion"), dict) else {}
    challenger = registry.get("challenger") if isinstance(registry.get("challenger"), dict) else {}
    champion_version = heading_text(champion.get("version") or "暂无")
    challenger_version = heading_text(challenger.get("version") or "暂无")
    challenger_state = (
        f"挑战者 {challenger_version} · 影子 {max(0, as_int(challenger.get('shadow_days')))} 天 · 样本/投注 {max(0, as_int(challenger.get('sample_count')))}/{max(0, as_int(challenger.get('bet_count')))}"
        if challenger else "挑战者 暂无"
    )
    leagues = registry.get("per_league") if isinstance(registry, dict) else {}
    paused_leagues = [
        heading_text(league)
        for league, state in leagues.items()
        if isinstance(state, dict) and state.get("paused") is True
    ] if isinstance(leagues, dict) else []
    paused = f"暂停联赛：{'、'.join(paused_leagues) if paused_leagues else '无'}"
    return progress, f"冠军 {champion_version} · {challenger_state}", paused


def latest_plan() -> tuple[str, list[dict]]:
    today_path = OUTPUT_DIR / f"betting_plan_{date.today().isoformat()}.csv"
    if today_path.exists():
        return date.today().isoformat(), read_csv(today_path)
    paths = sorted(OUTPUT_DIR.glob("betting_plan_*.csv"))
    if not paths:
        return date.today().isoformat(), []
    path = paths[-1]
    return path.stem.removeprefix("betting_plan_"), read_csv(path)


def validated_provisional_rows(
    root: Path, report_date: date
) -> tuple[list[dict], list[dict]]:
    """Return active and shadow rows from the pointer-selected generation."""
    state = read_valid_provisional_state(root, report_date)
    routes = {"active": [], "shadow": []}
    for candidate in state["candidates"]:
        source = candidate.get("source_plan_row")
        if not isinstance(source, dict):
            raise ValueError("validated provisional candidate source row is invalid")
        rendered = {
            **source,
            **candidate,
            "date": report_date.isoformat(),
            "stake": candidate["provisional_stake"],
        }
        routes[candidate["route"]].append(rendered)
    return routes["active"], routes["shadow"]


def observation_plan(report_date: str) -> list[dict]:
    return read_csv(OUTPUT_DIR / f"observation_plan_{report_date}.csv")


def daily_decision(report_date: str) -> dict:
    return read_draw_json(f"daily_decision_{report_date}.json")


def revalidation_label(change: dict) -> str:
    state = external_text(change.get("state")).strip()
    if state == "cancelled":
        return "已撤销"
    if state == "confirmed":
        if number(change, "final_stake") < number(change, "provisional_stake"):
            return "临场降额"
        return "临场确认"
    if state == "screened":
        return "90分钟筛查通过"
    return "初选待复核"


def _draw_revalidation_report(
    output_path: Path,
    report_date: date,
    changes: list[dict],
    change_digest: str,
) -> Path:
    height = max(440, 240 + len(changes) * 150)
    image = Image.new("RGB", (WIDTH, height), "#f3f6f4")
    draw = ImageDraw.Draw(image)
    ink, muted, green, red, gold, line = "#17201b", "#617068", "#147d50", "#bd4337", "#b98216", "#d7dfda"
    draw.rectangle((0, 0, WIDTH, 175), fill="#10271d")
    draw.text((70, 42), "博弈预测看板 · 临场复核更新", font=font(44), fill="white")
    draw.text((72, 110), f"业务日期：{report_date.isoformat()}　北京时间", font=font(23), fill="#dce9e1")
    y = 215
    for change in changes:
        label = revalidation_label(change)
        label_color = red if label == "已撤销" else gold if label == "临场降额" else green
        draw.rounded_rectangle((70, y, WIDTH - 70, y + 122), radius=7, fill="white", outline=line)
        draw.text((88, y + 15), label, font=font(25), fill=label_color)
        draw_fitted_text(draw, (320, y + 17), change.get("match", change.get("match_id", "-")), font(24), ink, 520)
        draw_fitted_text(draw, (860, y + 17), change.get("market", change.get("selection", "-")), font(22), ink, WIDTH - 950)
        odds = f"初选赔率 {change.get('odds', change.get('initial_odds', change.get('provisional_odds', '-')))}　当前赔率 {change.get('current_odds', '-') }"
        stakes = f"暂定金额（未计入盈亏） {change.get('provisional_stake', '-')}元　最终金额 {change.get('final_stake', change.get('stake', '-'))}元"
        detail = f"{odds}　{stakes}　当前EV {change.get('current_ev', '-')}"
        draw_fitted_text(draw, (88, y + 55), detail, font(18), muted, WIDTH - 176)
        draw_fitted_text(draw, (88, y + 87), f"原因：{change.get('reason', change.get('reason_code', '-'))}", font(17), muted, WIDTH - 176)
        y += 150
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("build_id", BUILD_ID)
    pnginfo.add_text("report_date", report_date.isoformat())
    pnginfo.add_text("change_digest", change_digest)
    pnginfo.add_text("report_stage", "revalidation")
    image.save(output_path, optimize=True, pnginfo=pnginfo)
    return output_path


def draw_report(
    output_path: Path | None = None,
    report_date: date | None = None,
    revalidation_changes: list[dict] | None = None,
    change_digest: str = "",
    report_stage: str = "daily",
) -> Path:
    if revalidation_changes is not None:
        if report_date is None:
            raise ValueError("report_date is required for revalidation images")
        destination = Path(output_path) if output_path is not None else WEB_DIR / "daily-report.png"
        return _draw_revalidation_report(destination, report_date, revalidation_changes, change_digest)
    if report_stage not in REPORT_STAGES:
        raise ValueError("report_stage is invalid")
    if report_stage == "provisional":
        if report_date is None:
            raise ValueError("report_date is required for provisional rendering")
        report_date_text = report_date.isoformat()
        plan, observations = validated_provisional_rows(ROOT, report_date)
    elif report_date is None:
        report_date_text, plan = latest_plan()
        observations = observation_plan(report_date_text)
    else:
        report_date_text, plan = report_date.isoformat(), read_csv(OUTPUT_DIR / f"betting_plan_{report_date.isoformat()}.csv")
        observations = observation_plan(report_date_text)
    report_date = report_date_text
    decision = daily_decision(report_date)
    ledger = read_csv(OUTPUT_DIR / "betting_ledger.csv")
    all_metrics = read_metrics()
    all_draw_alerts = [
        row for row in read_draw_alert(report_date) if isinstance(row, dict)
    ]
    draw_alerts = sorted(
        all_draw_alerts,
        key=alert_rank,
    )[:4]
    draw_alert_metrics = read_draw_json("draw_alert_metrics.json")
    draw_model_registry = read_draw_json("draw_model_registry.json")
    metrics = all_metrics.get("overall", {})
    active_metrics = all_metrics.get("active_strategy", {})
    clv_metrics = all_metrics.get("clv", {})
    play_metrics = all_metrics.get("by_play_all") or all_metrics.get("by_play", {})
    play_metrics = play_metrics if isinstance(play_metrics, dict) else {}
    league_calibrations = all_metrics.get("league_draw_calibration", {})
    league_calibrations = league_calibrations if isinstance(league_calibrations, dict) else {}
    enabled_league_calibrations = sum(
        1 for item in league_calibrations.values()
        if isinstance(item, dict) and item.get("enabled") is True
    )
    control_visible = bool(decision) or bool(active_metrics) or bool(metrics)
    _, standalone_alert_stake, today_stake = today_stake_totals(
        plan, all_draw_alerts
    )
    stake_data_invalid = today_stake is None
    plan_height = max(1, 0 if stake_data_invalid else len(plan)) * 124
    ledger_height = max(1, len(ledger)) * 76
    observation_height = (58 + len(observations) * 70) if observations else 0
    play_metrics_height = (115 + 54 * len(play_metrics)) if play_metrics else 0
    control_height = 60 if control_visible else 0
    height = 590 + control_height + plan_height + observation_height + play_metrics_height + ledger_height + 100 + 170 * len(draw_alerts)
    image = Image.new("RGB", (WIDTH, height), "#f3f6f4")
    draw = ImageDraw.Draw(image)

    ink = "#17201b"
    muted = "#617068"
    green = "#147d50"
    red = "#bd4337"
    gold = "#b98216"
    line = "#d7dfda"

    draw.rectangle((0, 0, WIDTH, 205), fill="#10271d")
    draw.text((70, 42), "竞彩足球每日方案与盈亏总表", font=font(48), fill="white")
    generated_at = datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M')
    draw.text((72, 112), f"方案日期：{report_date}　北京时间生成：{generated_at}", font=font(24), fill="#dce9e1")

    total_stake = sum(number(row, "stake") for row in ledger)
    settled = [row for row in ledger if row.get("status") not in {"", "未结算"}]
    hits = sum(1 for row in settled if row.get("status") == "命中")
    profit = sum(number(row, "profit") for row in settled)
    brier = active_metrics.get("brier")
    roi = metrics.get("roi")
    average_clv = clv_metrics.get("average_clv")
    calibration_error = active_metrics.get("calibration_error")
    max_drawdown = metrics.get("max_drawdown")
    max_losing_streak = metrics.get("max_losing_streak")
    account = decision.get("account", {}) if isinstance(decision.get("account"), dict) else {}
    completed_days = int(account.get("completed_days") or 0)
    required_days = max(30, int(account.get("required_settled_days") or 30))
    stats = [
        ("今日预算", "停止投入" if stake_data_invalid else f"{today_stake:.0f} 元"),
        ("Brier误差", f"{brier:.3f}" if brier is not None else "-"),
        ("平均CLV", f"{average_clv * 100:+.1f}%" if average_clv is not None else "-"),
        ("实际回报率", f"{roi * 100:+.1f}%" if roi is not None else "-"),
        ("累计盈亏", f"{profit:+.0f} 元"),
    ]
    card_width = 278
    for index, (label, value) in enumerate(stats):
        x = 70 + index * (card_width + 20)
        draw.rounded_rectangle((x, 230, x + card_width, 335), radius=8, fill="white", outline=line)
        draw.text((x + 18, 247), label, font=font(20), fill=muted)
        value_color = red if (stake_data_invalid and index == 0) or (label == "累计盈亏" and profit < 0) else green
        draw.text((x + 18, 280), value, font=font(30), fill=value_color)

    decision_reason = heading_text(decision.get("reason"))
    if control_visible:
        risk_line = (
            f"最大回撤 {max_drawdown:.0f}元" if isinstance(max_drawdown, (int, float)) else "最大回撤 -"
        )
        risk_line += (
            f"　最长连续未中 {int(max_losing_streak)}"
            if isinstance(max_losing_streak, (int, float))
            else "　最长连续未中 -"
        )
        risk_line += (
            f"　概率校准误差 {calibration_error:.3f}"
            if isinstance(calibration_error, (int, float))
            else "　概率校准误差 -"
        )
        risk_line += f"　联赛校准 {enabled_league_calibrations} 个　模拟观察 {completed_days}/{required_days}天　不会自动转为真实投注"
        draw_fitted_text(draw, (70, 350), risk_line, font(18), muted, WIDTH - 140)
        if decision_reason:
            draw_fitted_text(draw, (70, 380), f"今日决策：{decision_reason}", font(18), ink, WIDTH - 140)
        y = 420
    else:
        y = 385
    draw.text((70, y), "今日投注方案", font=font(34), fill=ink)
    y += 58
    if stake_data_invalid:
        draw.text((75, y), "金额数据异常，停止新增投入", font=font(24), fill=red)
        y += 124
    elif not plan:
        empty_copy = (
            f"主方案为空，但有平局预警投入 {standalone_alert_stake:.0f} 元"
            if standalone_alert_stake > 0
            else decision_reason or "今日暂无符合条件的方案"
        )
        draw_fitted_text(draw, (75, y), empty_copy, font(24), muted, WIDTH - 145)
        y += 124
    else:
        for index, row in enumerate(plan, start=1):
            draw.rounded_rectangle((70, y, WIDTH - 70, y + 104), radius=7, fill="white", outline=line)
            draw.text((88, y + 17), f"{index}. {row.get('play', '-')}", font=font(25), fill=gold if "串1" in row.get("play", "") else green)
            draw.text((320, y + 17), row.get("match", "-"), font=font(24), fill=ink)
            draw.text((900, y + 17), f"赔率 {row.get('odds', '-')}　金额 {row.get('stake', '-')}元", font=font(23), fill=ink)
            selection_lines = wrap(row.get("selection", "-"), 62)
            draw.text((320, y + 57), selection_lines[0], font=font(21), fill=muted)
            market_probability = number(row, "market_probability")
            value_edge = number(row, "value_edge")
            draw.text((960, y + 57), f"保守 {number(row, 'probability') * 100:.1f}%  原模型 {number(row, 'raw_model_probability') * 100:.1f}%  市场 {market_probability * 100:.1f}%  优势 {value_edge * 100:+.1f}%", font=font(19), fill=muted)
            y += 124

    draw.line((70, y, WIDTH - 70, y), fill=line, width=2)
    alert_header_y = y
    subtype_progress, model_progress, paused_leagues = draw_alert_heading(draw_alert_metrics, draw_model_registry)
    draw.text((70, alert_header_y + 7), "平局预警", font=font(30), fill=ink)
    draw_fitted_text(draw, (250, alert_header_y + 13), subtype_progress, font(18), muted, WIDTH - 70 - 250)
    draw_fitted_text(draw, (70, alert_header_y + 45), model_progress, font(15), muted, 650)
    draw_fitted_text(draw, (760, alert_header_y + 45), paused_leagues, font(15), muted, WIDTH - 70 - 760)
    training_error = heading_text(draw_model_registry.get("last_training_error"))
    if training_error:
        draw_fitted_text(draw, (70, alert_header_y + 63), f"最近训练异常：{training_error}", font(14), red, WIDTH - 140)
    if not draw_alerts:
        draw.text((70, alert_header_y + 80), "今日无符合门槛的平局预警", font=font(14), fill=muted)
    y = alert_header_y + 100

    for alert in draw_alerts:
        subtype = SUBTYPE_LABELS.get(external_text(alert.get("subtype")), "待分类平局")
        settlement_mode = external_text(alert.get("settlement_mode"))
        state = SETTLEMENT_LABELS.get(settlement_mode, "待确认状态")
        level = alert_level_label(alert)
        level_suffix = f" · {level}" if level else ""
        draw.rounded_rectangle((70, y, WIDTH - 70, y + 154), radius=7, fill="white", outline=line)
        draw.text(
            (88, y + 14),
            f"{alert_rank_label(alert)} · {subtype}{level_suffix}",
            font=font(22),
            fill=gold,
        )
        draw_fitted_text(draw, (315, y + 14), alert.get("match") or "-", font(23), ink, WIDTH - 70 - 315)
        metric_line = "  ".join(
            [
                f"官方平赔 {alert_odds(alert)}",
                f"模型 {alert_decimal(alert, 'model_draw_probability', percentage=True)}",
                f"市场 {alert_decimal(alert, 'market_draw_probability', percentage=True)}",
                f"优势 {alert_decimal(alert, 'draw_edge', signed=True, percentage=True)}",
                f"期望值 {alert_decimal(alert, 'expected_value')}",
                f"xG总和 {alert_decimal(alert, 'xg_total')}",
            ]
        )
        draw_fitted_text(draw, (88, y + 50), metric_line, font(17), muted, WIDTH - 70 - 88)
        detail_line = (
            f"{state} · {alert_amount(alert, settlement_mode)} · 账本 {external_text(alert.get('ledger_status') or '未结算')} "
            f"· 数据质量 {external_text(alert.get('data_quality') or '-')} · 捕获 {external_text(alert.get('captured_at') or '-')}"
        )
        draw_fitted_text(draw, (88, y + 78), detail_line, font(16), muted, WIDTH - 70 - 88)
        evidence = wrap_image_text(
            draw,
            f"证据来源：{evidence_source_summary(alert.get('evidence_json'))}",
            font(16),
            WIDTH - 70 - 88,
            2,
        )
        for index, line_text in enumerate(evidence):
            draw.text((88, y + 105 + index * 20), line_text, font=font(16), fill=muted)
        y += 170

    if observations:
        draw.text((70, y), "零金额观察单", font=font(30), fill=ink)
        draw.text((350, y + 5), "仅用于概率校准与CLV，不计入盈亏", font=font(20), fill=muted)
        y += 48
        for row in observations:
            draw.rectangle((70, y, WIDTH - 70, y + 58), fill="white", outline=line)
            draw.text((88, y + 15), row.get("match", "-"), font=font(19), fill=ink)
            draw.text((520, y + 15), f"观察 {row.get('selection', '-')}  赔率 {row.get('odds', '-')}", font=font(19), fill=ink)
            draw.text((930, y + 15), f"保守 {number(row, 'probability') * 100:.1f}%  原模型 {number(row, 'raw_model_probability') * 100:.1f}%  市场 {number(row, 'market_probability') * 100:.1f}%", font=font(18), fill=muted)
            y += 70

    if play_metrics:
        draw.line((70, y, WIDTH - 70, y), fill=line, width=2)
        y += 30
        draw.text((70, y), "各玩法独立表现", font=font(30), fill=ink)
        y += 45
        headers = [(75, "玩法"), (430, "已结算"), (570, "投入"), (740, "盈亏"), (900, "回报率"), (1080, "最大回撤")]
        for x, header in headers:
            draw.text((x, y), header, font=font(17), fill=muted)
        y += 30
        for play, item in sorted(play_metrics.items()):
            if not isinstance(item, dict):
                continue
            row_profit = item.get("profit")
            row_roi = item.get("roi")
            row_drawdown = item.get("max_drawdown")
            draw.rectangle((70, y, WIDTH - 70, y + 44), fill="white", outline=line)
            draw_fitted_text(draw, (75, y + 11), play, font(17), ink, 330)
            draw.text((430, y + 11), str(item.get("count", 0)), font=font(17), fill=ink)
            draw.text((570, y + 11), f"{float(item.get('stake') or 0):.0f}", font=font(17), fill=ink)
            draw.text((740, y + 11), "-" if row_profit is None else f"{float(row_profit):+.0f}", font=font(17), fill=green if (row_profit or 0) >= 0 else red)
            draw.text((900, y + 11), "-" if row_roi is None else f"{float(row_roi) * 100:+.1f}%", font=font(17), fill=ink)
            draw.text((1080, y + 11), "-" if row_drawdown is None else f"{float(row_drawdown):.0f}", font=font(17), fill=ink)
            y += 54

    draw.line((70, y, WIDTH - 70, y), fill=line, width=2)
    y += 35
    draw.text((70, y), "全部盈亏记录", font=font(34), fill=ink)
    y += 58
    headers = ["日期", "玩法", "比赛/选择", "投入", "状态", "盈亏"]
    xs = [75, 245, 485, 1190, 1300, 1430]
    for x, header in zip(xs, headers):
        draw.text((x, y), header, font=font(20), fill=muted)
    y += 38
    draw.line((70, y, WIDTH - 70, y), fill=line, width=2)
    y += 10

    if not ledger:
        draw.text((75, y + 12), "尚无历史记录", font=font(22), fill=muted)
    else:
        for row in ledger:
            status = row.get("status", "未结算")
            row_profit = number(row, "profit")
            fill = "#ffffff" if (y // 76) % 2 else "#f8faf9"
            draw.rectangle((70, y, WIDTH - 70, y + 66), fill=fill)
            draw.text((75, y + 18), row.get("date", "-"), font=font(18), fill=ink)
            draw.text((245, y + 18), row.get("play", "-"), font=font(18), fill=ink)
            detail = f"{row.get('match', '-')}｜{row.get('selection', '-')}"
            draw.text((485, y + 18), wrap(detail, 48)[0], font=font(18), fill=ink)
            draw.text((1190, y + 18), f"{number(row, 'stake'):.0f}", font=font(18), fill=ink)
            status_color = green if status == "命中" else red if status == "未中" else muted
            draw.text((1300, y + 18), status, font=font(18), fill=status_color)
            profit_color = green if row_profit > 0 else red if row_profit < 0 else muted
            draw.text((1430, y + 18), f"{row_profit:+.0f}", font=font(18), fill=profit_color)
            y += 76

    output = Path(output_path) if output_path is not None else WEB_DIR / "daily-report.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("build_id", BUILD_ID)
    pnginfo.add_text("report_date", report_date)
    pnginfo.add_text("change_digest", "")
    pnginfo.add_text("report_stage", report_stage)
    image.save(output, optimize=True, pnginfo=pnginfo)
    print(f"Generated daily image: {output}")
    return output


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be exact YYYY-MM-DD") from exc
    if value != parsed.isoformat():
        raise argparse.ArgumentTypeError("date must be exact YYYY-MM-DD")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the daily report image.")
    parser.add_argument("--date", type=_parse_date)
    parser.add_argument("--stage", choices=REPORT_STAGES, default="daily")
    args = parser.parse_args()
    if args.stage == "provisional" and args.date is None:
        parser.error("--date is required for provisional rendering")
    try:
        draw_report(report_date=args.date, report_stage=args.stage)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
