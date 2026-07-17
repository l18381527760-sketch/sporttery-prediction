import csv
import html
import json
import math
import os
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
WEB_DIR = ROOT / "web"
ASSET_PATH = "assets/stadium-dashboard.png"
BUILD_ID = os.environ.get("REPORT_BUILD_ID", "local")

SUBTYPE_LABELS = {"cold_draw": "冷门平局", "balanced_draw": "均势平局"}
SETTLEMENT_LABELS = {
    "linked": "复用主方案金额，不重复投入",
    "observation": "零金额观察",
    "standalone": "独立小额模拟",
    "budget_capped_observation": "达到当日预警预算上限，零新增金额观察",
}
EVIDENCE_MAX_DEPTH = 16
EVIDENCE_MAX_NODES = 256
EVIDENCE_MAX_INPUT_CHARS = 32_768
EVIDENCE_MAX_SUMMARY_CHARS = 160
EVIDENCE_SOURCE_KEYS = ("source", "provider", "bookmaker", "name")
EVIDENCE_TOO_DEEP = "证据结构过深"
EVIDENCE_TRUNCATED = "证据来源已截断"


def read_source_status() -> dict:
    path = ROOT / "data" / "source_status.json"
    if not path.exists():
        return {"source": "竞彩网", "fallback": False, "message": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"source": "未知", "fallback": True, "message": "数据源状态文件无法读取。"}


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
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    except (OSError, UnicodeError, csv.Error):
        return []


def read_betting_plan(display_date: date | None) -> list[dict]:
    if display_date is None:
        return []
    return read_csv_file(OUTPUT_DIR / f"betting_plan_{display_date.isoformat()}.csv")


def read_observation_plan(display_date: date | None) -> list[dict]:
    if display_date is None:
        return []
    return read_csv_file(OUTPUT_DIR / f"observation_plan_{display_date.isoformat()}.csv")


def read_betting_ledger() -> list[dict]:
    return read_csv_file(OUTPUT_DIR / "betting_ledger.csv")


def read_draw_alert(display_date: date | None) -> list[dict]:
    if display_date is None:
        return []
    alerts = read_csv_file(OUTPUT_DIR / f"draw_alert_{display_date.isoformat()}.csv")
    ledger = {
        draw_alert_key(row): row
        for row in read_csv_file(OUTPUT_DIR / "draw_alert_ledger.csv")
    }
    enriched = []
    for alert in alerts:
        ledger_row = ledger.get(draw_alert_key(alert), {})
        enriched.append({**alert, "ledger_status": ledger_row.get("status", "")})
    return enriched


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_model_metrics() -> dict:
    return read_json_file(OUTPUT_DIR / "model_metrics.json")


def read_draw_alert_metrics() -> dict:
    return read_json_file(OUTPUT_DIR / "draw_alert_metrics.json")


def read_draw_model_registry() -> dict:
    return read_json_file(OUTPUT_DIR / "draw_model_registry.json")


def read_daily_decision(display_date: date | None) -> dict:
    if display_date is None:
        return {}
    return read_json_file(
        OUTPUT_DIR / f"daily_decision_{display_date.isoformat()}.json"
    )


def draw_alert_key(row: dict) -> tuple[str, str, str]:
    return (
        external_text(row.get("date")).strip(),
        external_text(row.get("subtype")).strip(),
        external_text(row.get("match")).strip(),
    )


def as_float(row: dict, key: str) -> float | None:
    raw_value = row.get(key)
    value = "" if raw_value is None else str(raw_value).strip()
    try:
        number = float(value) if value else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def positive_amount(row: dict, key: str) -> float:
    value = paid_amount(row, key)
    return value if value is not None and value > 0 else 0.0


def paid_amount(row: dict, key: str) -> float | None:
    value = as_float(row, key) if isinstance(row, dict) else None
    if value is None or not value.is_integer() or not 0 <= value <= 500:
        return None
    return value


def standalone_draw_alert_stake(alerts: list[dict] | None) -> float | None:
    total = 0.0
    for alert in alerts or []:
        if not isinstance(alert, dict):
            return None
        if external_text(alert.get("settlement_mode")).strip() != "standalone":
            continue
        value = paid_amount(alert, "additional_stake")
        if value is None:
            return None
        total += value
        if total > 500:
            return None
    return total


def today_stake_totals(
    plan: list[dict] | None, alerts: list[dict] | None
) -> tuple[float | None, float | None, float | None]:
    main_stake = 0.0
    for row in plan or []:
        value = paid_amount(row, "stake") if isinstance(row, dict) else None
        if value is None:
            return None, None, None
        main_stake += value
        if main_stake > 500:
            return None, None, None
    draw_alert_stake = standalone_draw_alert_stake(alerts)
    if draw_alert_stake is None or main_stake + draw_alert_stake > 500:
        return None, None, None
    return main_stake, draw_alert_stake, main_stake + draw_alert_stake


def as_int(value: object, default: int = 0) -> int:
    try:
        number = float(str(value).strip())
        if not math.isfinite(number) or not number.is_integer() or abs(number) > 2_147_483_647:
            return default
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return default


def external_text(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def escaped(value: object) -> str:
    return html.escape(external_text(value))


def pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def yuan(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}"


def decimal(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


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


def render_betting_plan(
    plan: list[dict],
    draw_alerts: list[dict] | None = None,
    decision: dict | None = None,
) -> str:
    main_stake, draw_alert_stake, total_stake = today_stake_totals(
        plan, draw_alerts
    )
    if total_stake is None:
        return """
        <section class="betting-section">
          <div class="section-title"><h2>模拟投注方案</h2><span>金额数据异常</span></div>
          <div class="empty">金额数据异常，停止新增投入。请检查方案金额后重新生成。</div>
        </section>
        """
    if not plan:
        if draw_alert_stake > 0:
            return f"""
        <section class="betting-section">
          <div class="section-title"><h2>模拟投注方案</h2><span>今日模拟投入 {yuan(total_stake)}元</span></div>
          <div class="empty">主方案为空，但有平局预警投入 {yuan(draw_alert_stake)}元。具体场次和依据见下方平局预警。</div>
        </section>
        """
        decision = decision if isinstance(decision, dict) else {}
        no_bet_reason = external_text(decision.get("reason")).strip()
        no_bet_reason = no_bet_reason or "今天没有同时满足概率优势、赔率价值和风险条件的方案，因此不模拟投注。"
        return f"""
        <section class="betting-section">
          <div class="section-title"><h2>模拟投注方案</h2><span>暂无方案</span></div>
          <div class="empty">{escaped(no_bet_reason)}</div>
        </section>
        """

    by_play: dict[str, float] = {}
    for row in plan:
        play = row.get("play", "-")
        by_play[play] = by_play.get(play, 0.0) + positive_amount(row, "stake")
    summary_parts = [
        f"{html.escape(key)} {yuan(value)}" for key, value in by_play.items()
    ]
    if draw_alert_stake > 0:
        summary_parts.append(f"平局预警 {yuan(draw_alert_stake)}")
    play_summary = " / ".join(summary_parts)
    rows = []
    for item in plan:
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.get("play", ""))}</td>
              <td>{html.escape(item.get("match", ""))}</td>
              <td><strong>{html.escape(item.get("selection", ""))}</strong></td>
              <td>保守 {pct(as_float(item, "probability"))}<br><small>原模型 {pct(as_float(item, "raw_model_probability"))} / 联赛校准 {pct(as_float(item, "league_calibrated_probability"))} / 市场 {pct(as_float(item, "market_probability"))}</small></td>
              <td>{html.escape(item.get("odds", ""))}<br><small>优势 {pct(as_float(item, "value_edge"))}</small></td>
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
          <span>今日模拟投入 {yuan(total_stake)}元；{play_summary}</span>
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


def render_account_control(decision: dict) -> str:
    if not isinstance(decision, dict) or not decision:
        return ""
    account = decision.get("account") if isinstance(decision.get("account"), dict) else {}
    completed = max(0, as_int(account.get("completed_days")))
    required = max(30, as_int(account.get("required_settled_days"), 30))
    monthly_stake = as_float(account, "monthly_stake") or 0.0
    monthly_budget = as_float(account, "monthly_budget_cap") or 0.0
    monthly_profit = as_float(account, "monthly_profit") or 0.0
    stop_loss = as_float(account, "monthly_stop_loss") or 0.0
    review = "达到人工复核门槛" if account.get("review_ready") is True else "继续模拟观察"
    state = external_text(decision.get("status"))
    label = {
        "bet": "今日通过价值门槛",
        "no_bet": "今日主方案观望",
        "risk_paused": "今日风险暂停",
    }.get(state, "今日决策")
    return f"""
      <section class="account-control" aria-label="模拟账户控制">
        <div>
          <span>{escaped(label)}</span>
          <strong>模拟观察 {completed}/{required} 天</strong>
          <small>{escaped(decision.get("reason") or "-")}</small>
        </div>
        <dl>
          <div><dt>本月模拟投入</dt><dd>{yuan(monthly_stake)}/{yuan(monthly_budget)}元</dd></div>
          <div><dt>本月盈亏</dt><dd>{monthly_profit:+.0f}元</dd></div>
          <div><dt>月度止损</dt><dd>{yuan(stop_loss)}元</dd></div>
          <div><dt>账户状态</dt><dd>{review}</dd></div>
        </dl>
        <p>系统始终是模拟记录，不会自动转为真实投注。</p>
      </section>
    """


def render_play_metrics(model_metrics: dict) -> str:
    by_play = model_metrics.get("by_play_all") if isinstance(model_metrics, dict) else {}
    if not isinstance(by_play, dict) or not by_play:
        by_play = model_metrics.get("by_play") if isinstance(model_metrics, dict) else {}
    if not isinstance(by_play, dict) or not by_play:
        return ""
    rows = []
    for play, item in sorted(by_play.items()):
        if not isinstance(item, dict):
            continue
        profit = as_float(item, "profit")
        rows.append(f"""
          <tr>
            <td><strong>{escaped(play)}</strong></td>
            <td>{as_int(item.get("count"))}</td>
            <td>{pct(as_float(item, "hit_rate"))}</td>
            <td>{yuan(as_float(item, "stake"))}元</td>
            <td>{'-' if profit is None else f'{profit:+.0f}元'}</td>
            <td>{pct(as_float(item, "roi"))}</td>
            <td>{yuan(as_float(item, "max_drawdown"))}元</td>
          </tr>
        """)
    if not rows:
        return ""
    return f"""
      <section class="play-performance">
        <div class="section-title"><h2>各玩法独立表现</h2><span>分别检查收益与最大回撤</span></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>玩法</th><th>已结算</th><th>命中率</th><th>投入</th><th>盈亏</th><th>回报率</th><th>最大回撤</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </section>
    """


def render_league_calibrations(model_metrics: dict) -> str:
    table = model_metrics.get("league_draw_calibration") if isinstance(model_metrics, dict) else {}
    if not isinstance(table, dict) or not table:
        return """
      <section class="play-performance">
        <div class="section-title"><h2>联赛平局校准</h2><span>至少30场且后续验证改善才启用</span></div>
        <div class="empty">尚无联赛达到30场有效样本，当前继续使用全局模型，不进行联赛概率修正。</div>
      </section>
        """
    rows = []
    for league, item in sorted(table.items()):
        if not isinstance(item, dict):
            continue
        count = max(0, as_int(item.get("sample_count")))
        enabled = item.get("enabled") is True
        status = "已启用" if enabled else f"观察期 {count}/30"
        adjustment = as_float(item, "adjustment") or 0.0
        before = as_float(item, "validation_brier_before")
        after = as_float(item, "validation_brier_after")
        validation = f"{decimal(before)} → {decimal(after)}" if before is not None else "-"
        rows.append(f"""
          <tr>
            <td><strong>{escaped(league)}</strong></td>
            <td>{count}</td>
            <td>{status}</td>
            <td>{adjustment * 100:+.1f}%</td>
            <td>{validation}</td>
          </tr>
        """)
    if not rows:
        return ""
    return f"""
      <section class="play-performance">
        <div class="section-title"><h2>联赛平局校准</h2><span>至少30场且后续验证改善才启用</span></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>联赛</th><th>样本</th><th>状态</th><th>概率修正</th><th>验证Brier</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </section>
    """


def render_observations(observations: list[dict]) -> str:
    if not observations:
        return ""
    rows = []
    for item in observations:
        rows.append(f"""
          <tr>
            <td>{html.escape(item.get("match", ""))}</td>
            <td><strong>{html.escape(item.get("selection", ""))}</strong></td>
            <td>{pct(as_float(item, "probability"))}</td>
            <td>{pct(as_float(item, "raw_model_probability"))}</td>
            <td>{pct(as_float(item, "league_calibrated_probability"))}</td>
            <td>{pct(as_float(item, "market_probability"))}</td>
            <td>{html.escape(item.get("odds", ""))}</td>
          </tr>
        """)
    return f"""
      <section class="betting-section">
        <div class="section-title"><h2>零金额观察单</h2><span>只用于概率校准与CLV，不计入投入和盈亏</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>比赛</th><th>观察结果</th><th>保守概率</th><th>原模型</th><th>联赛校准</th><th>市场概率</th><th>赔率</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table></div>
      </section>
    """


def evidence_json_exceeds_depth(value: str) -> bool:
    depth = 0
    in_string = False
    escaped_character = False
    for character in value:
        if in_string:
            if escaped_character:
                escaped_character = False
            elif character == "\\":
                escaped_character = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > EVIDENCE_MAX_DEPTH:
                return True
        elif character in "]}":
            depth = max(0, depth - 1)
    return False


def normalize_evidence_whitespace(value: object) -> str:
    return " ".join(external_text(value).split())


def evidence_source_summary(value: object) -> str:
    """Return a bounded summary of named evidence sources without echoing raw JSON."""
    raw_evidence = external_text(value).strip()
    if not raw_evidence:
        return "未提供可用来源"
    if len(raw_evidence) > EVIDENCE_MAX_INPUT_CHARS:
        return EVIDENCE_TRUNCATED
    if evidence_json_exceeds_depth(raw_evidence):
        return EVIDENCE_TOO_DEEP
    try:
        payload = json.loads(raw_evidence)
    except (TypeError, ValueError, RecursionError):
        return "未提供可用来源"

    sources: list[str] = []
    seen_sources: set[str] = set()
    stack: list[tuple[object, int]] = [(payload, 1 if isinstance(payload, (list, dict)) else 0)]
    visited_nodes = 0
    while stack:
        item, depth = stack.pop()
        visited_nodes += 1
        if visited_nodes > EVIDENCE_MAX_NODES:
            return EVIDENCE_TRUNCATED
        if depth > EVIDENCE_MAX_DEPTH:
            return EVIDENCE_TOO_DEEP
        if isinstance(item, dict):
            for key in EVIDENCE_SOURCE_KEYS:
                source = item.get(key)
                if not isinstance(source, (str, int, float)):
                    continue
                source_text = normalize_evidence_whitespace(source)
                if not source_text or source_text in seen_sources:
                    continue
                if len(source_text) > EVIDENCE_MAX_SUMMARY_CHARS:
                    return EVIDENCE_TRUNCATED
                seen_sources.add(source_text)
                if len(sources) < 3:
                    sources.append(source_text)
            children = list(item.values())
        elif isinstance(item, list):
            children = item
        else:
            continue
        if visited_nodes + len(stack) + len(children) > EVIDENCE_MAX_NODES:
            return EVIDENCE_TRUNCATED
        for child in reversed(children):
            child_depth = depth + 1 if isinstance(child, (list, dict)) else depth
            stack.append((child, child_depth))

    summary = normalize_evidence_whitespace("、".join(sources) if sources else "已记录来源")
    return summary if len(summary) <= EVIDENCE_MAX_SUMMARY_CHARS else EVIDENCE_TRUNCATED


def draw_alert_value(alert: dict, key: str) -> float | None:
    value = as_float(alert, key)
    if value is None:
        return None
    if key == "domestic_draw_odds":
        return value if 1.01 <= value <= 100 else None
    if key == "expected_value":
        return value if 0 < value <= 100 else None
    if key == "xg_total":
        return value if 0 < value <= 10 else None
    if key in {"model_draw_probability", "market_draw_probability"}:
        return value if 0 <= value <= 1 else None
    if key == "draw_edge":
        return value if -1 <= value <= 1 else None
    return None


def format_draw_alert_odds(alert: dict) -> str:
    if draw_alert_value(alert, "domestic_draw_odds") is None:
        return "-"
    return external_text(alert.get("domestic_draw_odds")).strip()


def format_draw_alert_percentage(alert: dict, key: str) -> str:
    value = draw_alert_value(alert, key)
    return "-" if value is None else f"{value * 100:.1f}%"


def format_draw_alert_decimal(alert: dict, key: str) -> str:
    value = draw_alert_value(alert, key)
    return "-" if value is None else f"{value:.3f}"


def alert_rank(alert: dict) -> int:
    rank = as_int(alert.get("rank"), 0)
    return rank if 1 <= rank <= 4 else 5


def alert_rank_label(alert: dict) -> str:
    rank = alert_rank(alert)
    return f"第{rank}场" if rank <= 4 else "未排名"


def alert_level_label(alert: dict) -> str:
    level = external_text(alert.get("alert_level")).strip()
    return level if level in {"高级", "中级"} else ""


def alert_amount(alert: dict, settlement_mode: str) -> str:
    if settlement_mode == "linked":
        amount = paid_amount(alert, "linked_main_stake")
        return "复用金额数据异常" if amount is None else f"复用主方案金额 {yuan(amount)}"
    if settlement_mode == "standalone":
        amount = paid_amount(alert, "additional_stake")
        return "金额数据异常，停止新增投入" if amount is None else f"额外投入 {yuan(amount)}"
    return "零新增金额"


def render_draw_progress(metrics: dict, registry: dict) -> str:
    subtype_metrics = metrics.get("subtypes", metrics) if isinstance(metrics, dict) else {}
    subtype_metrics = subtype_metrics if isinstance(subtype_metrics, dict) else {}
    progress = []
    for key, label in SUBTYPE_LABELS.items():
        item = subtype_metrics.get(key, {})
        item = item if isinstance(item, dict) else {}
        count = max(0, as_int(item.get("count")))
        status = "已晋级" if item.get("promoted") is True else "观察期"
        progress.append(f'<span><strong>{label} {count}/30</strong><small>{status}</small></span>')

    registry = registry if isinstance(registry, dict) else {}
    champion = registry.get("champion") if isinstance(registry.get("champion"), dict) else {}
    challenger = registry.get("challenger") if isinstance(registry.get("challenger"), dict) else {}
    champion_version = escaped(champion.get("version") or "暂无")
    challenger_version = escaped(challenger.get("version") or "暂无")
    shadow_days = max(0, as_int(challenger.get("shadow_days")))
    sample_count = max(0, as_int(challenger.get("sample_count")))
    bet_count = max(0, as_int(challenger.get("bet_count")))
    leagues = registry.get("per_league")
    paused_leagues = []
    if isinstance(leagues, dict):
        for league, state in leagues.items():
            if isinstance(state, dict) and state.get("paused") is True:
                paused_leagues.append(escaped(league))
    paused = "、".join(paused_leagues) if paused_leagues else "无"
    error = external_text(registry.get("last_training_error")).strip()
    error_block = f'<span class="draw-training-error">最近训练异常：{escaped(error)}</span>' if error else ""
    return f"""
      <div class="draw-progress" aria-label="平局模型进度">
        <div class="draw-subtype-progress">{''.join(progress)}</div>
        <div class="draw-model-progress">
          <span>冠军 {champion_version}</span>
          <span>挑战者 {challenger_version} · 影子 {shadow_days} 天 · 样本/投注 {sample_count}/{bet_count}</span>
          <span>暂停联赛：{paused}</span>
          {error_block}
        </div>
        <p>进度仅用于观测与复盘，不代表模型改善或晋级保证。</p>
      </div>
    """


def render_draw_alert(alerts: list[dict], metrics: dict | None = None, registry: dict | None = None) -> str:
    metrics = metrics if isinstance(metrics, dict) else {}
    registry = registry if isinstance(registry, dict) else {}
    ranked_alerts = sorted((alert for alert in alerts if isinstance(alert, dict)), key=alert_rank)[:4]
    if not ranked_alerts:
        return f"""
        <section class="draw-alert">
          <div class="section-title"><h2>平局预警</h2><span>零到四场，按预警排名展示</span></div>
          {render_draw_progress(metrics, registry)}
          <p class="draw-empty">今日无符合门槛的平局预警</p>
        </section>
        """

    rows = []
    for alert in ranked_alerts:
        subtype = SUBTYPE_LABELS.get(external_text(alert.get("subtype")), "待分类平局")
        settlement_mode = external_text(alert.get("settlement_mode"))
        state = SETTLEMENT_LABELS.get(settlement_mode, "待确认状态")
        level = alert_level_label(alert)
        level_suffix = f" · {escaped(level)}" if level else ""
        rows.append(f"""
          <article class="draw-alert-row">
            <header>
              <span>{alert_rank_label(alert)} · {subtype}{level_suffix}</span>
              <strong>{escaped(alert.get("match") or "-")}</strong>
            </header>
            <div class="draw-alert-metrics">
              <span>官方平赔 <strong>{escaped(format_draw_alert_odds(alert))}</strong></span>
              <span>模型 <strong>{escaped(format_draw_alert_percentage(alert, "model_draw_probability"))}</strong></span>
              <span>市场 <strong>{escaped(format_draw_alert_percentage(alert, "market_draw_probability"))}</strong></span>
              <span>优势 <strong>{escaped(format_draw_alert_percentage(alert, "draw_edge"))}</strong></span>
              <span>期望值 <strong>{escaped(format_draw_alert_decimal(alert, "expected_value"))}</strong></span>
              <span>xG 总和 <strong>{escaped(format_draw_alert_decimal(alert, "xg_total"))}</strong></span>
            </div>
            <div class="draw-alert-detail">
              <p><span>状态</span>{state} · {alert_amount(alert, settlement_mode)}</p>
              <p><span>证据来源</span>{escaped(evidence_source_summary(alert.get("evidence_json")))}</p>
              <p><span>数据质量</span>{escaped(alert.get("data_quality") or "-")} · <span>捕获时间</span>{escaped(alert.get("captured_at") or "-")}</p>
              <p><span>账本状态</span>{escaped(alert.get("ledger_status") or "未结算")}</p>
            </div>
          </article>
        """)
    return f"""
      <section class="draw-alert">
        <div class="section-title"><h2>平局预警</h2><span>零到四场，按预警排名展示</span></div>
        {render_draw_progress(metrics, registry)}
        <div class="draw-alert-list">{''.join(rows)}</div>
      </section>
    """


def render_ledger(ledger: list[dict], model_metrics: dict) -> str:
    if not ledger:
        return ""
    total_stake = sum(as_float(row, "stake") or 0 for row in ledger)
    settled = [row for row in ledger if row.get("status") != "未结算"]
    profit = sum(as_float(row, "profit") or 0 for row in settled)
    hits = sum(1 for row in settled if row.get("status") == "命中")
    hit_rate = hits / len(settled) if settled else None
    overall = model_metrics.get("overall", {})
    active = model_metrics.get("active_strategy", {})
    roi = overall.get("roi")
    brier = active.get("brier")
    log_loss = active.get("log_loss")
    calibration_error = active.get("calibration_error")
    average_expected_return = active.get("average_expected_return")
    clv = model_metrics.get("clv", {})
    snapshot_coverage = model_metrics.get("snapshot_coverage", {})
    snapshot_phases = snapshot_coverage.get("phases", {}) if isinstance(snapshot_coverage, dict) else {}
    snapshot_phases = snapshot_phases if isinstance(snapshot_phases, dict) else {}
    return f"""
      <section class="ledger-strip">
        <div><span>累计模拟投入</span><strong>{yuan(total_stake)}</strong></div>
        <div><span>已结算注数</span><strong>{len(settled)}</strong></div>
        <div><span>命中率</span><strong>{pct(hit_rate)}</strong></div>
        <div><span>累计盈亏</span><strong>{yuan(profit)}</strong></div>
        <div><span>实际回报率</span><strong>{pct(roi)}</strong></div>
        <div><span>当前策略样本</span><strong>{active.get("count", 0)}</strong></div>
        <div><span>Brier概率误差</span><strong>{decimal(brier)}</strong></div>
        <div><span>Log Loss</span><strong>{decimal(log_loss)}</strong></div>
        <div><span>概率校准误差</span><strong>{decimal(calibration_error)}</strong></div>
        <div><span>最大回撤</span><strong>{yuan(overall.get("max_drawdown"))}</strong></div>
        <div><span>最长连续未中</span><strong>{as_int(overall.get("max_losing_streak"))}</strong></div>
        <div><span>当前连续未中</span><strong>{as_int(overall.get("current_losing_streak"))}</strong></div>
        <div><span>平均赔率价值</span><strong>{pct((average_expected_return - 1) if average_expected_return is not None else None)}</strong></div>
        <div><span>平均CLV</span><strong>{pct(clv.get("average_clv"))}</strong></div>
        <div><span>赔率快照覆盖</span><strong>开{as_int(snapshot_phases.get("opening"))} / 决{as_int(snapshot_phases.get("decision"))} / 临{as_int(snapshot_phases.get("pre_kickoff"))}</strong></div>
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
    observation_plan = read_observation_plan(display_date)
    betting_ledger = read_betting_ledger()
    model_metrics = read_model_metrics()
    draw_alerts = read_draw_alert(display_date)
    draw_alert_metrics = read_draw_alert_metrics()
    draw_model_registry = read_draw_model_registry()
    daily_decision = read_daily_decision(display_date)
    source_status = read_source_status()
    source_name = str(source_status.get("source") or "未知")
    analysis_source = str(source_status.get("analysis_source") or "专业欧赔市场")
    source_message = str(source_status.get("message") or "")
    if source_status.get("fallback"):
        source_alert = f'''<section class="source-alert warning"><strong>赛程及方案赔率：{html.escape(source_name)}（备用）</strong><span>模型分析：{html.escape(analysis_source)}。{html.escape(source_message)} 缺失赔率的选项不会进入方案。</span></section>'''
    else:
        source_alert = f'''<section class="source-alert ok"><strong>赛程及方案赔率：{html.escape(source_name)}</strong><span>模型分析：{html.escape(analysis_source)}；投注赔率仅采用竞彩足球官方赔率。</span></section>'''

    match_cards = "\n".join(render_match(row) for row in display_rows)
    if not match_cards:
        match_cards = '<section class="empty">赛程表里还没有可展示的预测。更新 data/fixtures.csv 后重新运行每日脚本即可。</section>'

    display_label = display_date.isoformat() if display_date else "暂无日期"
    data_json = json.dumps(rows, ensure_ascii=False)

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="report-build-id" content="{html.escape(BUILD_ID, quote=True)}">
  <title>博弈预测看板</title>
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
    .source-alert {{
      display: grid;
      gap: 4px;
      margin: 22px 0 0;
      padding: 13px 16px;
      border: 1px solid;
      background: #fff;
    }}
    .source-alert.warning {{ border-color: #d7a23a; background: #fff8e8; color: #704d0c; }}
    .source-alert.ok {{ border-color: #77b796; background: #edf8f1; color: #145b39; }}
    .source-alert span {{ font-size: 13px; line-height: 1.5; }}
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
    .account-control {{
      margin-top: 20px;
      padding: 16px 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      display: grid;
      grid-template-columns: minmax(220px, .8fr) minmax(0, 1.6fr);
      gap: 14px 24px;
      align-items: center;
    }}
    .account-control > div {{ display: grid; gap: 5px; }}
    .account-control span {{ color: var(--green); font-size: 12px; font-weight: 700; }}
    .account-control strong {{ font-size: 21px; }}
    .account-control small {{ line-height: 1.5; }}
    .account-control dl {{
      margin: 0;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }}
    .account-control dl div {{ min-width: 0; }}
    .account-control dt {{ color: var(--muted); font-size: 11px; font-weight: 700; }}
    .account-control dd {{ margin: 5px 0 0; font-size: 15px; font-weight: 700; overflow-wrap: anywhere; }}
    .account-control p {{ grid-column: 1 / -1; margin: 0; color: var(--muted); font-size: 12px; }}
    .play-performance {{ margin-top: 20px; }}
    .draw-alert {{
      margin-top: 22px;
      padding: 18px 0 2px;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }}
    .draw-progress {{
      display: grid;
      gap: 10px;
      padding: 12px 0 16px;
      border-bottom: 1px solid var(--line);
    }}
    .draw-subtype-progress, .draw-model-progress {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px 18px;
    }}
    .draw-subtype-progress span, .draw-model-progress span {{
      min-width: 0;
      overflow-wrap: anywhere;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .draw-subtype-progress strong {{
      color: var(--ink);
      display: block;
      font-size: 14px;
    }}
    .draw-subtype-progress small {{
      font-size: 12px;
    }}
    .draw-training-error {{
      color: var(--red) !important;
    }}
    .draw-progress p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .draw-alert-list {{
      display: grid;
      gap: 10px;
      padding: 14px 0 16px;
    }}
    .draw-alert-row {{
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(24, 42, 32, .07);
      padding: 14px;
    }}
    .draw-alert-row header {{
      display: grid;
      grid-template-columns: minmax(130px, .38fr) minmax(0, 1fr);
      gap: 12px;
      align-items: baseline;
    }}
    .draw-alert-row header span {{
      color: var(--gold);
      font-size: 13px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .draw-alert-row header strong {{
      min-width: 0;
      font-size: 17px;
      overflow-wrap: anywhere;
    }}
    .draw-alert-metrics {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
      margin-top: 12px;
    }}
    .draw-alert-metrics span {{
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .draw-alert-metrics strong {{
      display: block;
      color: var(--ink);
      font-size: 14px;
      margin-top: 3px;
    }}
    .draw-alert-detail {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px 16px;
      margin-top: 12px;
      padding-top: 11px;
      border-top: 1px solid var(--line);
    }}
    .draw-alert-detail p {{
      min-width: 0;
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }}
    .draw-alert-detail span {{
      color: var(--ink);
      font-weight: 700;
      margin-right: 5px;
    }}
    .draw-empty {{
      margin: 14px 0 16px;
      color: var(--muted);
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
      .account-control, .account-control dl {{
        grid-template-columns: 1fr;
      }}
      .draw-subtype-progress, .draw-model-progress, .draw-alert-detail {{
        grid-template-columns: 1fr;
      }}
      .draw-alert-metrics {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
      .hero {{
        min-height: 280px;
      }}
    }}
    @media (max-width: 420px) {{
      .draw-alert-row header, .draw-alert-metrics {{
        grid-template-columns: 1fr;
      }}
      .draw-alert-row header {{
        gap: 5px;
      }}
    }}
  </style>
</head>
<body>
  <section class="hero">
    <div class="hero-inner">
      <p class="eyebrow">WORLD CUP FORECAST DESK</p>
      <h1>博弈预测看板</h1>
      <p>每天自动汇总赛程、球队评分和模型概率，重点展示胜平负、晋级概率、预期进球和推荐判断。</p>
    </div>
  </section>

  <main>
    {source_alert}
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

    {render_ledger(betting_ledger, model_metrics)}
    {render_account_control(daily_decision)}
    {render_play_metrics(model_metrics)}
    {render_league_calibrations(model_metrics)}
    {render_betting_plan(betting_plan, draw_alerts, daily_decision)}
    {render_draw_alert(draw_alerts, draw_alert_metrics, draw_model_registry)}
    {render_observations(observation_plan)}

    <footer>
      预测是概率判断，不是确定赛果。建议每天比赛前更新赛程、球队评分、伤停和赔率后重新运行每日脚本。
    </footer>
  </main>
  <script type="application/json" id="prediction-data">{html.escape(data_json)}</script>
</body>
</html>
"""
    return "\n".join(line.rstrip() for line in page.splitlines()) + "\n"


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
