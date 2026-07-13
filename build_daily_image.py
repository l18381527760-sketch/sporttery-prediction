import csv
import json
import textwrap
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
WEB_DIR = ROOT / "web"
WIDTH = 1600


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_metrics() -> dict:
    path = OUTPUT_DIR / "model_metrics.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


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


def wrap(value: str, width: int) -> list[str]:
    return textwrap.wrap(value or "-", width=width, break_long_words=True) or ["-"]


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
    metrics = all_metrics.get("overall", {})
    active_metrics = all_metrics.get("active_strategy", {})
    clv_metrics = all_metrics.get("clv", {})
    plan_height = max(1, len(plan)) * 124
    ledger_height = max(1, len(ledger)) * 76
    observation_height = (58 + len(observations) * 70) if observations else 0
    height = 590 + plan_height + observation_height + ledger_height
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
