import csv
import html
import json
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
WEB_DIR = ROOT / "web"
ASSET_PATH = "assets/stadium-dashboard.png"


def read_predictions() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(OUTPUT_DIR.glob("predictions_*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if not row.get("team_a"):
                    continue
                rows.append(row)
    return rows


def read_csv_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def read_betting_plan(display_date: date | None) -> list[dict]:
    if display_date is None:
        return []
    return read_csv_file(OUTPUT_DIR / f"betting_plan_{display_date.isoformat()}.csv")


def read_betting_ledger() -> list[dict]:
    return read_csv_file(OUTPUT_DIR / "betting_ledger.csv")


def as_float(row: dict, key: str) -> float | None:
    value = (row.get(key) or "").strip()
    return float(value) if value else None


def pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def yuan(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}"


def match_date(row: dict) -> date:
    return datetime.strptime(row["date"], "%Y-%m-%d").date()


def choose_display_date(rows: list[dict]) -> date | None:
    if not rows:
        return None
    today = date.today()
    dates = sorted({match_date(row) for row in rows})
    if today in dates:
        return today
    future = [item for item in dates if item > today]
    return future[0] if future else dates[-1]


def group_by_date(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["date"], []).append(row)
    return grouped


def best_probability(row: dict) -> float:
    values = [as_float(row, "p_a") or 0, as_float(row, "p_draw") or 0, as_float(row, "p_b") or 0]
    if as_float(row, "adv_a") is not None and as_float(row, "adv_b") is not None:
        values.extend([as_float(row, "adv_a") or 0, as_float(row, "adv_b") or 0])
    return max(values)


def render_probability_bar(label: str, value: float | None, color: str) -> str:
    width = 0 if value is None else max(0, min(100, value * 100))
    return f"""
        <div class="prob-row">
          <div class="prob-label"><span>{html.escape(label)}</span><strong>{pct(value)}</strong></div>
          <div class="track"><div class="fill {color}" style="width: {width:.1f}%"></div></div>
        </div>
    """


def render_score_picks(row: dict) -> str:
    items = []
    for index in range(1, 4):
        score = (row.get(f"score_{index}") or "").strip()
        probability = as_float(row, f"score_{index}_prob")
        if not score:
            continue
        items.append(
            f"""
            <li>
              <span>{index}</span>
              <strong>{html.escape(score)}</strong>
              <small>{pct(probability)}</small>
            </li>
            """
        )
    if not items:
        return ""
    return f"""
        <section class="score-picks" aria-label="比分预测">
          <h3>比分预测</h3>
          <ol>
            {"".join(items)}
          </ol>
        </section>
    """


def render_match(row: dict) -> str:
    team_a = row["team_a"]
    team_b = row["team_b"]
    p_a = as_float(row, "p_a")
    p_draw = as_float(row, "p_draw")
    p_b = as_float(row, "p_b")
    adv_a = as_float(row, "adv_a")
    adv_b = as_float(row, "adv_b")
    xg_a = as_float(row, "xg_a") or 0
    xg_b = as_float(row, "xg_b") or 0
    confidence = row.get("confidence", "-")
    pick = row.get("pick", "-")
    scoreline = f"{xg_a:.2f} - {xg_b:.2f}"
    best = best_probability(row)

    advancement = ""
    if adv_a is not None and adv_b is not None:
        advancement = f"""
          <div class="advance">
            <span>晋级概率</span>
            <strong>{html.escape(team_a)} {pct(adv_a)}</strong>
            <strong>{html.escape(team_b)} {pct(adv_b)}</strong>
          </div>
        """

    return f"""
      <article class="match-card">
        <header class="match-head">
          <div>
            <p class="meta">{html.escape(row.get("kickoff", ""))} · {html.escape(row.get("stage", ""))} · {html.escape(row.get("venue", ""))}</p>
            <h2>{html.escape(team_a)} <span>vs</span> {html.escape(team_b)}</h2>
          </div>
          <div class="confidence" data-level="{html.escape(confidence)}">
            <span>信心</span>
            <strong>{html.escape(confidence)}</strong>
          </div>
        </header>
        <div class="match-grid">
          <section class="pick-panel">
            <span>推荐判断</span>
            <strong>{html.escape(pick)}</strong>
            <small>最高模型概率 {pct(best)}</small>
          </section>
          <section class="xg-panel">
            <span>预期进球</span>
            <strong>{scoreline}</strong>
            <small>{html.escape(team_a)} / {html.escape(team_b)}</small>
          </section>
        </div>
        <section class="prob-panel" aria-label="90分钟概率">
          {render_probability_bar(team_a + " 胜", p_a, "home")}
          {render_probability_bar("平局", p_draw, "draw")}
          {render_probability_bar(team_b + " 胜", p_b, "away")}
        </section>
        {render_score_picks(row)}
        {advancement}
      </article>
    """


def render_history(grouped: dict[str, list[dict]], display_date: date | None) -> str:
    items = []
    for date_key in sorted(grouped.keys(), reverse=True):
        rows = grouped[date_key]
        active = " active" if display_date and date_key == display_date.isoformat() else ""
        picks = "，".join(row.get("pick", "-") for row in rows)
        items.append(
            f"""
            <li class="history-item{active}">
              <span>{html.escape(date_key)}</span>
              <strong>{len(rows)} 场</strong>
              <small>{html.escape(picks)}</small>
            </li>
            """
        )
    return "\n".join(items)


def render_betting_plan(plan: list[dict]) -> str:
    if not plan:
        return """
        <section class="betting-section">
          <div class="section-title"><h2>模拟投注方案</h2><span>暂无方案</span></div>
          <div class="empty">还没有生成今天的模拟投注方案。</div>
        </section>
        """

    total_stake = sum(as_float(row, "stake") or 0 for row in plan)
    by_play: dict[str, float] = {}
    for row in plan:
        by_play[row["play"]] = by_play.get(row["play"], 0.0) + (as_float(row, "stake") or 0)
    play_summary = " / ".join(f"{html.escape(key)} {yuan(value)}" for key, value in by_play.items())
    rows = []
    for item in plan:
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.get("play", ""))}</td>
              <td>{html.escape(item.get("match", ""))}</td>
              <td><strong>{html.escape(item.get("selection", ""))}</strong></td>
              <td>{pct(as_float(item, "probability"))}</td>
              <td>{html.escape(item.get("odds", ""))}</td>
              <td>{yuan(as_float(item, "stake"))}</td>
              <td>{yuan(as_float(item, "expected_profit"))}</td>
              <td>{html.escape(item.get("reason", ""))}</td>
            </tr>
            """
        )
    return f"""
      <section class="betting-section">
        <div class="section-title">
          <h2>模拟投注方案</h2>
          <span>今日模拟投入 {yuan(total_stake)}；{play_summary}</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>玩法</th>
                <th>比赛</th>
                <th>选择</th>
                <th>概率</th>
                <th>赔率</th>
                <th>金额</th>
                <th>期望盈亏</th>
                <th>分析</th>
              </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </div>
      </section>
    """


def render_ledger(ledger: list[dict]) -> str:
    if not ledger:
        return ""
    total_stake = sum(as_float(row, "stake") or 0 for row in ledger)
    settled = [row for row in ledger if row.get("status") != "未结算"]
    profit = sum(as_float(row, "profit") or 0 for row in settled)
    hits = sum(1 for row in settled if row.get("status") == "命中")
    hit_rate = hits / len(settled) if settled else None
    return f"""
      <section class="ledger-strip">
        <div><span>累计模拟投入</span><strong>{yuan(total_stake)}</strong></div>
        <div><span>已结算注数</span><strong>{len(settled)}</strong></div>
        <div><span>命中率</span><strong>{pct(hit_rate)}</strong></div>
        <div><span>累计盈亏</span><strong>{yuan(profit)}</strong></div>
      </section>
    """


def render_site(rows: list[dict]) -> str:
    grouped = group_by_date(rows)
    display_date = choose_display_date(rows)
    display_rows = grouped.get(display_date.isoformat(), []) if display_date else []
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_matches = len(rows)
    high_confidence = sum(1 for row in rows if row.get("confidence") in {"高", "High"})
    betting_plan = read_betting_plan(display_date)
    betting_ledger = read_betting_ledger()

    match_cards = "\n".join(render_match(row) for row in display_rows)
    if not match_cards:
        match_cards = '<section class="empty">赛程表里还没有可展示的预测。更新 data/fixtures.csv 后重新运行每日脚本即可。</section>'

    display_label = display_date.isoformat() if display_date else "暂无日期"
    data_json = json.dumps(rows, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>世界杯每日预测看板</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f7f4;
      --ink: #17201b;
      --muted: #62706a;
      --line: #d9e1dc;
      --panel: #ffffff;
      --green: #168a56;
      --red: #c84b3d;
      --gold: #c9942b;
      --blue: #2c6fbb;
      --shadow: 0 18px 45px rgba(24, 42, 32, .13);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
      letter-spacing: 0;
    }}
    .hero {{
      min-height: 320px;
      background:
        linear-gradient(90deg, rgba(9, 19, 14, .88), rgba(9, 19, 14, .62) 42%, rgba(9, 19, 14, .08)),
        url("{ASSET_PATH}") center / cover no-repeat;
      color: #fff;
      display: flex;
      align-items: end;
      padding: 36px 22px;
    }}
    .hero-inner, main {{
      width: min(1180px, 100%);
      margin: 0 auto;
    }}
    .eyebrow {{
      margin: 0 0 10px;
      font-size: 13px;
      color: #bde7cd;
      font-weight: 700;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(32px, 5vw, 62px);
      line-height: 1.02;
      max-width: 760px;
    }}
    .hero p {{
      max-width: 760px;
      color: #dfece5;
      font-size: 16px;
      line-height: 1.7;
      margin: 16px 0 0;
    }}
    main {{
      padding: 22px;
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: -46px;
      margin-bottom: 18px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid rgba(255,255,255,.7);
      box-shadow: var(--shadow);
      border-radius: 8px;
      padding: 16px;
      min-height: 88px;
    }}
    .stat span, .pick-panel span, .xg-panel span, .confidence span, .advance span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .stat strong {{
      font-size: 24px;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 300px;
      gap: 18px;
      align-items: start;
    }}
    .section-title {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      margin: 10px 0 14px;
    }}
    .section-title h2 {{
      margin: 0;
      font-size: 22px;
    }}
    .section-title span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .matches {{
      display: grid;
      gap: 14px;
    }}
    .match-card, .side-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(24, 42, 32, .07);
    }}
    .match-card {{
      padding: 18px;
    }}
    .match-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      margin: 0 0 8px;
    }}
    .match-head h2 {{
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
    }}
    .match-head h2 span {{
      color: var(--muted);
      font-size: 16px;
      font-weight: 600;
    }}
    .confidence {{
      min-width: 78px;
      text-align: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #f8faf8;
    }}
    .confidence strong {{
      font-size: 18px;
      color: var(--green);
    }}
    .match-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin: 14px 0;
    }}
    .pick-panel, .xg-panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fbfcfb;
    }}
    .pick-panel strong, .xg-panel strong {{
      display: block;
      font-size: 26px;
      margin-bottom: 6px;
    }}
    small {{
      color: var(--muted);
      font-size: 12px;
    }}
    .prob-panel {{
      display: grid;
      gap: 11px;
    }}
    .prob-label {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 14px;
      margin-bottom: 6px;
    }}
    .track {{
      height: 10px;
      background: #edf1ee;
      border-radius: 999px;
      overflow: hidden;
    }}
    .fill {{
      height: 100%;
      border-radius: 999px;
    }}
    .fill.home {{ background: var(--green); }}
    .fill.draw {{ background: var(--gold); }}
    .fill.away {{ background: var(--blue); }}
    .advance {{
      margin-top: 14px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }}
    .advance span {{
      grid-column: 1 / -1;
      margin: 0;
    }}
    .advance strong {{
      padding: 10px;
      background: #f8faf8;
      border-radius: 8px;
      border: 1px solid var(--line);
      font-size: 14px;
    }}
    .score-picks {{
      margin-top: 14px;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }}
    .score-picks h3 {{
      margin: 0 0 10px;
      font-size: 15px;
    }}
    .score-picks ol {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .score-picks li {{
      min-height: 74px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfb;
      padding: 10px;
      display: grid;
      gap: 3px;
    }}
    .score-picks li span {{
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: var(--ink);
      color: #fff;
      font-size: 12px;
      font-weight: 800;
    }}
    .score-picks li strong {{
      font-size: 22px;
    }}
    .score-picks li small {{
      font-weight: 700;
    }}
    .side-panel {{
      padding: 16px;
      position: sticky;
      top: 16px;
    }}
    .side-panel h2 {{
      margin: 0 0 12px;
      font-size: 18px;
    }}
        .history {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }}
    .history-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      display: grid;
      gap: 4px;
      background: #fbfcfb;
    }}
    .history-item.active {{
      border-color: rgba(22, 138, 86, .45);
      background: #edf8f2;
    }}
    .history-item span {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .history-item strong {{
      font-size: 16px;
    }}
    .history-item small {{
      line-height: 1.45;
    }}
    .empty {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 24px;
      color: var(--muted);
    }}
    .betting-section {{
      margin-top: 22px;
    }}
    .table-wrap {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: auto;
      box-shadow: 0 8px 24px rgba(24, 42, 32, .07);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }}
    th, td {{
      text-align: left;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      vertical-align: top;
    }}
    th {{
      background: #f3f7f4;
      color: var(--muted);
      font-size: 12px;
    }}
    td strong {{
      font-size: 15px;
    }}
    .ledger-strip {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .ledger-strip div {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .ledger-strip span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .ledger-strip strong {{
      font-size: 22px;
    }}
    footer {{
      color: var(--muted);
      font-size: 12px;
      padding: 26px 0 10px;
      line-height: 1.6;
    }}
    @media (max-width: 860px) {{
      .toolbar, .layout, .match-grid {{
        grid-template-columns: 1fr;
      }}
      .toolbar {{
        margin-top: -30px;
      }}
      .match-head {{
        flex-direction: column;
      }}
      .confidence {{
        width: 100%;
        text-align: left;
      }}
      .side-panel {{
        position: static;
      }}
      .score-picks ol {{
        grid-template-columns: 1fr;
      }}
      .ledger-strip {{
        grid-template-columns: 1fr;
      }}
      .hero {{
        min-height: 280px;
      }}
    }}
  </style>
</head>
<body>
  <section class="hero">
    <div class="hero-inner">
      <p class="eyebrow">WORLD CUP FORECAST DESK</p>
      <h1>世界杯每日预测看板</h1>
      <p>每天自动汇总赛程、球队评分和模型概率，重点展示胜平负、晋级概率、预期进球和推荐判断。</p>
    </div>
  </section>

  <main>
    <section class="toolbar" aria-label="核心统计">
      <div class="stat"><span>当前显示日期</span><strong>{html.escape(display_label)}</strong></div>
      <div class="stat"><span>当天比赛</span><strong>{len(display_rows)}</strong></div>
      <div class="stat"><span>累计预测</span><strong>{total_matches}</strong></div>
      <div class="stat"><span>高信心判断</span><strong>{high_confidence}</strong></div>
    </section>

    <section class="layout">
      <div>
        <div class="section-title">
          <h2>比赛预测</h2>
          <span>最后更新：{html.escape(generated_at)}</span>
        </div>
        <div class="matches">
          {match_cards}
        </div>
      </div>

      <aside class="side-panel">
        <h2>预测历史</h2>
        <ul class="history">
          {render_history(grouped, display_date)}
        </ul>
      </aside>
    </section>

    {render_ledger(betting_ledger)}
    {render_betting_plan(betting_plan)}

    <footer>
      预测是概率判断，不是确定赛果。建议每天比赛前更新赛程、球队评分、伤停和赔率后重新运行每日脚本。
    </footer>
  </main>
  <script type="application/json" id="prediction-data">{html.escape(data_json)}</script>
</body>
</html>
"""


def main() -> int:
    WEB_DIR.mkdir(exist_ok=True)
    rows = read_predictions()
    html_text = render_site(rows)
    output = WEB_DIR / "index.html"
    output.write_text(html_text, encoding="utf-8")
    print(f"Generated website: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
