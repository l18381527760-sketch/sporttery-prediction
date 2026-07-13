import csv
import json
import math
import textwrap
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from build_site import (
    SUBTYPE_LABELS,
    SETTLEMENT_LABELS,
    alert_amount,
    alert_rank,
    as_int,
    evidence_source_summary,
    external_text,
)


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
WEB_DIR = ROOT / "web"
WIDTH = 1600


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
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
    try:
        value = float(external_text(row.get(key)).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def alert_decimal(row: dict, key: str, *, signed: bool = False, percentage: bool = False) -> str:
    value = alert_number(row, key)
    if value is None or (percentage and not signed and not 0 <= value <= 1) or (key == "xg_total" and value < 0):
        return "-"
    if percentage:
        return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"
    return f"{value:+.3f}" if signed else f"{value:.3f}"


def alert_odds(row: dict) -> str:
    value = alert_number(row, "domestic_draw_odds")
    return external_text(row.get("domestic_draw_odds")).strip() if value is not None and value > 1 else "-"


def wrap(value: str, width: int) -> list[str]:
    return textwrap.wrap(value or "-", width=width, break_long_words=True) or ["-"]


def text_width(draw: ImageDraw.ImageDraw, value: str, text_font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), value, font=text_font)
    return box[2] - box[0]


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
        progress_items.append(f"{label} {max(0, as_int(item.get('count')))}/30")
    progress = " · ".join(progress_items)
    champion = registry.get("champion") if isinstance(registry.get("champion"), dict) else {}
    challenger = registry.get("challenger") if isinstance(registry.get("challenger"), dict) else {}
    champion_version = external_text(champion.get("version") or "暂无")
    challenger_version = external_text(challenger.get("version") or "暂无")
    challenger_state = (
        f"挑战者 {challenger_version} · 影子 {max(0, as_int(challenger.get('shadow_days')))} 天 · 样本/投注 {max(0, as_int(challenger.get('sample_count')))}/{max(0, as_int(challenger.get('bet_count')))}"
        if challenger else "挑战者 暂无"
    )
    leagues = registry.get("per_league") if isinstance(registry, dict) else {}
    paused_leagues = [
        external_text(league)
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


def observation_plan(report_date: str) -> list[dict]:
    return read_csv(OUTPUT_DIR / f"observation_plan_{report_date}.csv")


def draw_report() -> Path:
    report_date, plan = latest_plan()
    observations = observation_plan(report_date)
    ledger = read_csv(OUTPUT_DIR / "betting_ledger.csv")
    all_metrics = read_metrics()
    draw_alerts = sorted(
        (row for row in read_draw_alert(report_date) if isinstance(row, dict)),
        key=alert_rank,
    )[:4]
    draw_alert_metrics = read_draw_json("draw_alert_metrics.json")
    draw_model_registry = read_draw_json("draw_model_registry.json")
    metrics = all_metrics.get("overall", {})
    active_metrics = all_metrics.get("active_strategy", {})
    clv_metrics = all_metrics.get("clv", {})
    plan_height = max(1, len(plan)) * 124
    ledger_height = max(1, len(ledger)) * 76
    observation_height = (58 + len(observations) * 70) if observations else 0
    height = 590 + plan_height + observation_height + ledger_height + 100 + 170 * len(draw_alerts)
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
    draw.text((72, 112), f"方案日期：{report_date}　北京时间生成：{datetime.now().strftime('%Y-%m-%d %H:%M')}", font=font(24), fill="#dce9e1")

    total_stake = sum(number(row, "stake") for row in ledger)
    settled = [row for row in ledger if row.get("status") not in {"", "未结算"}]
    hits = sum(1 for row in settled if row.get("status") == "命中")
    profit = sum(number(row, "profit") for row in settled)
    today_stake = sum(number(row, "stake") for row in plan)
    brier = active_metrics.get("brier")
    roi = metrics.get("roi")
    average_clv = clv_metrics.get("average_clv")
    stats = [
        ("今日预算", f"{today_stake:.0f} 元"),
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
        value_color = red if label == "累计盈亏" and profit < 0 else green
        draw.text((x + 18, 280), value, font=font(30), fill=value_color)

    y = 385
    draw.text((70, y), "今日投注方案", font=font(34), fill=ink)
    y += 58
    if not plan:
        draw.text((75, y), "今日暂无符合条件的方案", font=font(24), fill=muted)
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
    y += 16
    subtype_progress, model_progress, paused_leagues = draw_alert_heading(draw_alert_metrics, draw_model_registry)
    draw.text((70, y), "平局预警", font=font(30), fill=ink)
    draw_fitted_text(draw, (250, y + 6), subtype_progress, font(18), muted, WIDTH - 70 - 250)
    draw_fitted_text(draw, (70, y + 28), model_progress, font(15), muted, 650)
    draw_fitted_text(draw, (760, y + 28), paused_leagues, font(15), muted, WIDTH - 70 - 760)
    training_error = external_text(draw_model_registry.get("last_training_error")).strip()
    if training_error:
        draw_fitted_text(draw, (70, y + 46), f"最近训练异常：{training_error}", font(14), red, WIDTH - 140)
    if not draw_alerts:
        draw.text((70, y + 65), "今日无符合门槛的平局预警", font=font(14), fill=muted)
    y += 84

    for alert in draw_alerts:
        subtype = SUBTYPE_LABELS.get(external_text(alert.get("subtype")), "待分类平局")
        settlement_mode = external_text(alert.get("settlement_mode"))
        state = SETTLEMENT_LABELS.get(settlement_mode, "待确认状态")
        draw.rounded_rectangle((70, y, WIDTH - 70, y + 154), radius=7, fill="white", outline=line)
        draw.text(
            (88, y + 14),
            f"第{alert_rank(alert)}场 · {subtype}",
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

    WEB_DIR.mkdir(exist_ok=True)
    output = WEB_DIR / "daily-report.png"
    image.save(output, optimize=True)
    print(f"Generated daily image: {output}")
    return output


if __name__ == "__main__":
    draw_report()
