# Performance Reporting and Guarded Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one auditable daily/cumulative simulation performance model for the website and email image, rename the site to 博弈预测看板, preserve complete history online, and allow learning changes only through sample-gated shadow evaluation, pause controls, and reversible champion/challenger promotion.

**Architecture:** Derive immutable daily and cumulative summaries from the Phase 2 ledger, write a shared versioned reporting payload, and make both renderers consume that payload. Separate probability-quality metrics from economic metrics. Add pure model-governance rules that can reduce or pause exposure but cannot promote a challenger from short-term profit alone or rewrite historical plans.

**Tech Stack:** Python 3.12, standard-library CSV/JSON/dataclasses/statistics, Pillow, unittest, existing scikit-learn draw learner, HTML/CSS/vanilla JavaScript, GitHub Actions, GitHub Pages.

## Global Constraints

- Phase 1 reliability and Phase 2 immutable-ledger tests must pass before Phase 3 begins.
- The website title and first-viewport heading must be exactly `博弈预测看板`.
- Website and email image must read the same generated performance payload; neither renderer may independently recalculate profit.
- The website retains complete ledger and daily performance history.
- The email image contains today's plan/no-bet decision, previous-day settlement, at most 30 daily rows, cumulative summary, play summary, and current monthly controls.
- The email image must not grow with all-time ledger length.
- Pending rows are visibly pending and never presented as final daily profit or ROI.
- Realized profit is derived only from settled or refunded ledger rows.
- Probability metrics (Brier, log loss, calibration error, CLV) and economic metrics (stake, return, profit, ROI, drawdown) must be visually and structurally separate.
- Group metrics are required by play, league, strategy/model version, and data quality.
- A new calibration or model rule requires at least 30 chronologically settled shadow samples.
- Promotion requires out-of-time probability improvement and positive CLV; ROI is supporting evidence only.
- A short profitable streak cannot override worsening Brier/log loss/calibration.
- Training failure, missing artifacts, or failed gates keep the valid champion unchanged.
- Pause and rollback decisions affect only future plans and never mutate historical bets.
- Keep simulation-only and no-guaranteed-profit language in operator documentation.

## File Structure

- Create `performance_reporting.py`: daily rows, cumulative summary, grouped economics, and shared report payload.
- Create `model_governance.py`: probability/economic guardrails and champion/challenger promotion rules.
- Modify `model_metrics.py`: grouped probability metrics, governance state, and shared output references.
- Modify `strategy_controls.py` and `generate_betting_plan.py`: honor future-only play/league pauses and risk multipliers.
- Modify `draw_model_learning.py`: use the common promotion decision and preserve rollback metadata.
- Modify `build_site.py`: new title, report status band, complete daily/ledger history, and filters.
- Modify `build_daily_image.py`: bounded email composition with yesterday and 30-day sections.
- Modify Phase 1 report-status and workflow tests to require the shared report payload before readiness.
- Modify `README.md`, `CLOUD_SETUP.md`, and Apps Script email body documentation.
- Create focused reporting, governance, site, and image tests.

---

### Task 1: Canonical Daily and Cumulative Performance

**Files:**
- Create: `performance_reporting.py`
- Create: `tests/test_performance_reporting.py`

**Interfaces:**
- `daily_performance(rows: list[dict]) -> list[dict]`
- `cumulative_performance(rows: list[dict]) -> dict`
- `group_performance(rows: list[dict], key: str) -> dict[str, dict]`
- `build_report_payload(rows: list[dict], report_date: date, decision: dict, plan: list[dict], config: dict) -> dict`
- `write_report_payload(report_date: date) -> tuple[Path, Path]`

- [ ] **Step 1: Write failing daily-accounting tests**

Use a fixture with one win, one loss, one refund, and one pending bet across three dates. Assert each daily row contains:

```python
{
    "date": "2026-07-15",
    "bet_count": 2,
    "settled_count": 1,
    "pending_count": 1,
    "stake": 120.0,
    "settled_stake": 60.0,
    "return": 108.0,
    "realized_profit": 48.0,
    "roi": None,
    "provisional_roi": 0.8,
    "is_final": False,
    "pending_exposure": 60.0,
}
```

`roi` is `None` while any row for that date is pending. `provisional_roi` may show settled-only progress but must be labeled provisional by renderers.

- [ ] **Step 2: Write failing cumulative and drawdown tests**

Assert:

- cumulative stake includes paid pending positions;
- cumulative settled stake and return include only final rows;
- cumulative realized profit equals the sum of final row profits;
- cumulative ROI uses realized profit divided by settled stake;
- refunded stake has zero profit and counts as settled;
- pending rows never create an artificial loss;
- maximum drawdown is calculated from chronological cumulative realized daily profit;
- duplicate `bet_id` input raises `ValueError` instead of double counting;
- malformed money/status values raise an explicit data-integrity error.

- [ ] **Step 3: Run tests and verify the module is missing**

Run: `python -m unittest tests.test_performance_reporting -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'performance_reporting'`.

- [ ] **Step 4: Implement strict ledger normalization**

Accepted final statuses are `命中`, `未中`, and `退款`; `待结算` is pending; `异常` is neither settled nor profitable and must be counted separately. Parse money with `Decimal` internally, quantize persisted values to `0.01`, and reject NaN, infinity, negative stake, negative return, and missing bet IDs.

- [ ] **Step 5: Implement chronological daily and cumulative rows**

Daily output must be sorted ascending for cumulative calculations. Add these cumulative fields to every daily row:

```text
cumulative_stake
cumulative_settled_stake
cumulative_return
cumulative_realized_profit
cumulative_roi
cumulative_max_drawdown
cumulative_pending_count
```

Do not replace daily realized profit with expected profit.

- [ ] **Step 6: Implement grouped economic summaries**

Support exact group keys `play`, `stage`, `strategy_version`, `model_version`, and `data_quality`. Each group reports bet/settled/pending counts, stake, settled stake, return, realized profit, final ROI, hit rate, maximum drawdown, and date range.

- [ ] **Step 7: Write the shared versioned payload**

Write:

- `output/daily_performance.csv`: complete one-row-per-date history.
- `output/performance_report.json`: schema version, report date, today's plan/decision, prior-day rows, all daily rows, cumulative summary, group summaries, and monthly account state.

Use sibling temporary files and `Path.replace()`. The JSON must include a `source_ledger_sha256` so stale payloads can be rejected.

- [ ] **Step 8: Run focused tests and commit**

Run: `python -m unittest tests.test_performance_reporting -v`

Expected: PASS.

```bash
git add performance_reporting.py tests/test_performance_reporting.py
git commit -m "feat: build canonical simulation performance report"
```

---

### Task 2: Probability Metrics and Model Governance

**Files:**
- Create: `model_governance.py`
- Create: `tests/test_model_governance.py`
- Modify: `model_metrics.py`
- Modify: `tests/test_model_metrics.py`

**Interfaces:**
- `evaluate_guardrail(current: dict, recent: dict, previous: dict, config: dict) -> GuardrailDecision`
- `promotion_decision(champion: dict, challenger: dict, config: dict) -> PromotionDecision`
- `governance_by_group(rows: list[dict], clv_rows: list[dict], config: dict) -> dict`

- [ ] **Step 1: Write failing pause-guard tests**

Cover:

- fewer than 30 settled samples returns `insufficient_samples` and does not pause;
- average CLV at or below `-0.02` after 30 samples pauses that group;
- recent Brier worsening by at least `0.02` versus the previous chronological window plus nonpositive CLV pauses;
- drawdown over the configured group cap pauses even when ROI is positive;
- negative short-term ROI alone does not alter probability calibration;
- group decisions are isolated by play and league;
- a paused group emits machine-readable reasons and a future exposure multiplier of `0.0`.

- [ ] **Step 2: Write failing promotion tests**

Use exact gates:

- at least 30 out-of-time shadow samples;
- challenger Brier lower than champion by at least `0.005`;
- challenger log loss lower than champion;
- challenger calibration error no worse by more than `0.005`;
- challenger average CLV strictly positive;
- no leakage or artifact-integrity flag;
- ROI may be negative or positive but cannot independently pass a failed probability gate.

Assert a profitable challenger with worse Brier is rejected, and that a qualifying challenger returns a reversible promotion record with both artifact hashes.

- [ ] **Step 3: Run tests and verify the module is missing**

Run: `python -m unittest tests.test_model_governance -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'model_governance'`.

- [ ] **Step 4: Implement immutable decisions**

Use:

```python
@dataclass(frozen=True)
class GuardrailDecision:
    state: str
    reasons: tuple[str, ...]
    sample_count: int
    exposure_multiplier: float


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool
    reasons: tuple[str, ...]
    champion_version: str
    challenger_version: str
    champion_sha256: str
    challenger_sha256: str
```

Never mutate metrics passed into either function.

- [ ] **Step 5: Extend `model_metrics.py` without changing historical rows**

Keep existing overall/by-play fields for compatibility. Add:

```json
{
  "schema_version": 2,
  "probability_metrics": {
    "overall": {},
    "by_play": {},
    "by_league": {},
    "by_strategy_version": {},
    "by_model_version": {},
    "by_data_quality": {}
  },
  "economic_metrics_ref": "performance_report.json",
  "governance": {
    "by_play": {},
    "by_league": {}
  }
}
```

Calculate probability metrics from locked pre-match probabilities only. Do not replace them with settlement-time probabilities. Extend `closing_line_value()` to match snapshots by immutable match ID, market type, handicap/total-goal line, and selection; calculate 2-leg closing price from its two exact leg markets. Ignore snapshots at or after kickoff and omit CLV when an exact comparable market is unavailable.

- [ ] **Step 6: Run metric and governance tests**

Run: `python -m unittest tests.test_model_governance tests.test_model_metrics -v`

Expected: PASS.

- [ ] **Step 7: Commit model governance**

```bash
git add model_governance.py model_metrics.py tests/test_model_governance.py tests/test_model_metrics.py
git commit -m "feat: separate model quality and economic guardrails"
```

---

### Task 3: Apply Future-Only Pauses and Reversible Learning

**Files:**
- Modify: `strategy_controls.py`
- Modify: `generate_betting_plan.py`
- Modify: `draw_model_learning.py`
- Modify: `betting_config.json`
- Modify: `tests/test_strategy_controls.py`
- Modify: `tests/test_draw_model_learning.py`
- Create: `tests/test_future_only_governance.py`

- [ ] **Step 1: Add failing future-only control tests**

Assert:

- a paused play or league is excluded from tomorrow's candidate pool;
- a group with insufficient samples remains active under existing conservative thresholds;
- a pause does not delete or alter yesterday's plan/ledger rows;
- removing a pause after new validated evidence affects only later plan dates;
- the 500/day, 5000/month, and 5000/month-loss hard limits remain authoritative even when governance multiplier is positive;
- no governance path can set `real_money_automation` true.

- [ ] **Step 2: Add failing draw-model promotion and rollback tests**

Require `draw_model_learning.py` to call `promotion_decision()`. Test:

- fewer than 30 shadow samples keeps the champion;
- positive ROI with worse probability metrics keeps the champion;
- passing all gates writes champion and previous-champion hashes to the registry;
- missing/corrupt challenger artifact keeps the champion and records an error;
- rollback restores the previous verified artifact without changing historical samples or plans.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `python -m unittest tests.test_strategy_controls tests.test_draw_model_learning tests.test_future_only_governance -v`

Expected: FAIL because current controls use ad hoc league ROI/Brier logic and the learner does not use the shared promotion decision.

- [ ] **Step 4: Add exact governance configuration**

```json
"model_governance": {
  "minimum_samples": 30,
  "recent_window": 15,
  "brier_worsening_limit": 0.02,
  "negative_clv_limit": -0.02,
  "group_max_drawdown": 500,
  "promotion_min_brier_improvement": 0.005,
  "promotion_max_calibration_regression": 0.005,
  "require_positive_clv": true,
  "auto_real_money": false
}
```

- [ ] **Step 5: Apply governance at candidate selection time**

Read the latest valid `output/model_metrics.json`. Reject paused play/league groups before portfolio allocation and record the exact reason in the daily decision and shadow audit. If metrics are missing, malformed, or for a different strategy version, retain strict mode; never infer a pause or promotion from broken data.

- [ ] **Step 6: Make champion/challenger changes atomic and reversible**

Write a new registry temporary file only after verifying both model artifacts and all metrics. Preserve `previous_champion_version`, `previous_champion_sha256`, `promoted_at_bjt`, and reasons. A rollback command must verify the previous hash before replacing the active pointer.

- [ ] **Step 7: Run tests and commit**

Run: `python -m unittest tests.test_strategy_controls tests.test_draw_model_learning tests.test_future_only_governance -v`

Expected: PASS.

```bash
git add strategy_controls.py generate_betting_plan.py draw_model_learning.py betting_config.json tests/test_strategy_controls.py tests/test_draw_model_learning.py tests/test_future_only_governance.py
git commit -m "feat: enforce future-only guarded model learning"
```

---

### Task 4: Build the 博弈预测看板 Website

**Files:**
- Modify: `build_site.py`
- Create: `tests/test_site_performance_reporting.py`
- Modify: `tests/test_draw_alert_reporting.py`

- [ ] **Step 1: Write failing title and report-source tests**

Assert:

```python
html = build_site.render_site([])
self.assertIn("<title>博弈预测看板</title>", html)
self.assertIn("<h1>博弈预测看板</h1>", html)
self.assertNotIn("世界杯每日预测看板", html)
```

Patch `read_performance_report()` with a known payload and verify all displayed profit/stake/ROI values come from that payload, not an independent ledger sum.

- [ ] **Step 2: Write failing full-history and filter tests**

Require:

- a report status band with report date, Beijing generation time, decision odds time, data quality, and settlement-through date;
- today's plan or explicit no-bet reason;
- prior-day settlement summary;
- complete daily performance table;
- complete immutable bet ledger table;
- grouped performance by play, league, strategy version, model version, and data quality;
- visible pending badges and provisional ROI labels;
- filters implemented as `<select>` controls for play, league, status, strategy version, and model version;
- stable `data-*` attributes on ledger rows for filter JavaScript;
- no nested card containers around page sections.

- [ ] **Step 3: Run site tests and verify failure**

Run: `python -m unittest tests.test_site_performance_reporting tests.test_draw_alert_reporting -v`

Expected: FAIL on the old title, missing full ledger, and missing shared report source.

- [ ] **Step 4: Add strict performance-report loading**

`read_performance_report()` must validate schema, `report_date`, and `source_ledger_sha256`. If validation fails, render a prominent data-integrity warning and suppress numeric performance claims instead of falling back to a second calculation.

- [ ] **Step 5: Implement quiet operational layout**

Keep the current football visual asset in the hero, but use the literal product name as the H1. Use full-width unframed sections for status, today's plan, daily history, grouped metrics, and ledger history. Keep cards only for repeated plan/alert rows. Use existing colors plus neutral status colors; do not introduce a single-hue redesign, decorative gradients, or oversized dashboard headings.

All table wrappers must scroll horizontally on narrow screens. Compact panel headings must remain below 30px. Letter spacing remains `0`.

- [ ] **Step 6: Implement accessible history filters**

Use native labels/selects and one clear reset button with the existing icon library if available. Filter only the already-rendered immutable rows; do not edit values client-side. Show a no-results row when filters hide everything.

- [ ] **Step 7: Run site tests and build locally**

Run: `python -m unittest tests.test_site_performance_reporting tests.test_draw_alert_reporting -v`

Expected: PASS.

Run: `python build_site.py`

Expected: `web/index.html` builds successfully and contains the new title.

- [ ] **Step 8: Commit the website**

```bash
git add build_site.py tests/test_site_performance_reporting.py tests/test_draw_alert_reporting.py
git commit -m "feat: publish full simulation history dashboard"
```

---

### Task 5: Build a Bounded Daily Email Image

**Files:**
- Modify: `build_daily_image.py`
- Create: `tests/test_daily_performance_image.py`
- Modify: `tests/test_draw_alert_reporting.py`

- [ ] **Step 1: Write failing section and source tests**

Mock a `performance_report.json` containing 90 daily rows and assert rendered text includes:

1. report date/data status;
2. today's plan or no-bet decision;
3. each prior-day settled bet and a prior-day subtotal;
4. exactly the most recent 30 daily rows;
5. cumulative stake/return/profit/ROI/drawdown/pending exposure;
6. grouped play performance;
7. monthly stake, remaining budget, realized monthly profit, and stop state.

Assert day 31 and older are not drawn.

- [ ] **Step 2: Write failing bounded-height tests**

Build images from ledgers with 30, 300, and 3000 historical bets but the same last-30-day report payload. Assert all three images have the same dimensions. Also assert the PNG `build_id` metadata from Phase 1 remains present.

- [ ] **Step 3: Run tests and verify current all-ledger growth**

Run: `python -m unittest tests.test_daily_performance_image tests.test_draw_alert_reporting -v`

Expected: FAIL because current height uses `len(ledger)` and renders all rows.

- [ ] **Step 4: Replace direct ledger accounting with shared payload data**

Remove `total_stake`, `profit`, and daily-history calculations from `draw_report()`. Read and validate `output/performance_report.json`; on validation failure, render an integrity-error image with no numeric profit claim and let Phase 1 mark the report unready.

- [ ] **Step 5: Implement bounded section sizing**

Use:

```python
recent_daily = payload["daily"][-30:]
previous_day_rows = payload["previous_day"]["bets"]
```

Daily rows use fixed 38px tracks. Today's paid plan remains bounded by Phase 2's two singles plus one parlay. Previous-day rows are bounded by the same plan limits. Keep zero-to-four draw alerts and zero-stake observations in their existing bounded sections. Calculate image height from these bounded arrays only.

- [ ] **Step 6: Keep long text within fixed tracks**

Use the existing `fit_text`, `draw_fitted_text`, and bounded wrapping helpers. Add regression assertions that every text bounding box ends before `WIDTH - 70` and that no section's final y-coordinate crosses the next section's heading.

- [ ] **Step 7: Run image tests and inspect file size**

Run: `python -m unittest tests.test_daily_performance_image tests.test_draw_alert_reporting -v`

Expected: PASS.

Run: `python build_daily_image.py`

Expected: `web/daily-report.png` builds, has nonzero dimensions, preserves build metadata, and remains under 10 MB.

- [ ] **Step 8: Commit the email image**

```bash
git add build_daily_image.py tests/test_daily_performance_image.py tests/test_draw_alert_reporting.py
git commit -m "feat: render bounded daily and cumulative email report"
```

---

### Task 6: Require Shared Reporting in Automation

**Files:**
- Modify: `report_status.py`
- Modify: `.github/workflows/daily-forecast.yml`
- Modify: `.github/workflows/draw-alert-refresh.yml`
- Modify: `.github/workflows/noon-settlement.yml`
- Modify: `tests/test_report_status.py`
- Modify: `tests/test_workflow_schedule.py`

- [ ] **Step 1: Write failing readiness and workflow tests**

Require report readiness to fail when `performance_report.json` is missing, for the wrong date, has a wrong ledger hash, or lacks yesterday's settlement state. Require every report-writing workflow to call:

```bash
python performance_reporting.py --date "$TARGET_DATE"
```

after any plan/settlement change and before `build_site.py`, `build_daily_image.py`, and `report_status.py`.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m unittest tests.test_report_status tests.test_workflow_schedule -v`

Expected: FAIL because the shared payload is not yet required.

- [ ] **Step 3: Validate payload provenance in report status**

`report_status.artifact_state()` must hash `output/betting_ledger.csv`, compare it with `performance_report.json.source_ledger_sha256`, and expose `performance_report_ready`. Add it as a required `plan_ready`/`settlement_ready` dependency without changing the public schema version unless compatibility demands it.

- [ ] **Step 4: Update workflow order**

Forecast and decision runs generate the payload after their active plan/ledger state. Settlement regenerates it after results and idempotent settlement. Both renderers consume it, and only then may status publication mark the phase ready.

- [ ] **Step 5: Run automation regression tests**

Run: `python -m unittest tests.test_report_status tests.test_workflow_schedule -v`

Expected: PASS.

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: PASS without Apps Script changes to the public readiness contract.

- [ ] **Step 6: Commit automation integration**

```bash
git add report_status.py .github/workflows/daily-forecast.yml .github/workflows/draw-alert-refresh.yml .github/workflows/noon-settlement.yml tests/test_report_status.py tests/test_workflow_schedule.py
git commit -m "feat: gate report delivery on canonical performance data"
```

---

### Task 7: Documentation and Email Copy

**Files:**
- Modify: `README.md`
- Modify: `CLOUD_SETUP.md`
- Modify: `apps-script/Code.gs`
- Modify: `apps-script/README.md`
- Modify: `tests/apps_script_orchestrator.test.mjs`
- Create: `tests/test_reporting_docs.py`

- [ ] **Step 1: Write failing documentation and subject tests**

Require docs and Apps Script subject/body to use `博弈预测看板`, explain simulation-only accounting, define daily versus cumulative realized profit, explain pending/provisional rows, list 500/day and 5000/month limits plus the 5000 realized-loss stop, and state that probability accuracy and ROI are evaluated separately.

- [ ] **Step 2: Run tests and verify old branding remains**

Run: `python -m unittest tests.test_reporting_docs -v`

Expected: FAIL on old World Cup branding and old report description.

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: FAIL on the old email subject/body copy.

- [ ] **Step 3: Update operator and user-facing documentation**

Document:

- the exact report sections and website history filters;
- how pending rows affect realized profit and ROI;
- the difference between probability quality, odds value, and realized P/L;
- shadow sample and promotion gates;
- future-only pause/rollback behavior;
- simulation-only status and no guarantee of profit;
- how to inspect `performance_report.json`, `model_metrics.json`, plan locks, and `bet_id` rows during an audit.

- [ ] **Step 4: Update Apps Script email copy only**

Keep deduplication and attachment behavior unchanged. Normal subject format:

```text
博弈预测看板｜YYYY-MM-DD 模拟方案与盈亏日报
```

Failure subject format:

```text
博弈预测看板｜YYYY-MM-DD 日报生成异常
```

The failure email still has no report attachment.

- [ ] **Step 5: Run docs and Apps Script tests**

Run: `python -m unittest tests.test_reporting_docs -v`

Expected: PASS.

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: PASS.

- [ ] **Step 6: Commit documentation and copy**

```bash
git add README.md CLOUD_SETUP.md apps-script/Code.gs apps-script/README.md tests/apps_script_orchestrator.test.mjs tests/test_reporting_docs.py
git commit -m "docs: explain simulation performance and guarded learning"
```

---

### Task 8: Full Verification, Visual Inspection, and Rollout

**Files:**
- Verify only; add regression tests for any defect found.

- [ ] **Step 1: Run complete automated verification**

```bash
python -m py_compile performance_reporting.py model_governance.py model_metrics.py strategy_controls.py generate_betting_plan.py draw_model_learning.py build_site.py build_daily_image.py report_status.py
python -m unittest discover -s tests -v
node --test tests/apps_script_orchestrator.test.mjs
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Verify accounting invariants on the repository ledger**

Run `performance_reporting.py` for the latest report date and independently assert:

- unique `bet_id` count equals ledger row count;
- sum of daily stake equals cumulative stake;
- sum of final returns equals cumulative return;
- sum of final profit equals cumulative realized profit;
- no pending row contributes profit;
- grouped totals reconcile with overall totals;
- monthly stake and realized profit match `simulation_account_state()`.

- [ ] **Step 3: Build both report artifacts**

```bash
TARGET_DATE="$(TZ=Asia/Shanghai date +%F)"
SETTLED_THROUGH="$(TZ=Asia/Shanghai date -d yesterday +%F)"
GENERATED_AT="$(TZ=Asia/Shanghai date --iso-8601=seconds)"
SOURCE_COMMIT="$(git rev-parse HEAD)"
python build_site.py
python build_daily_image.py
python report_status.py --date "$TARGET_DATE" --phase settlement --build-id local-final-check --source-commit "$SOURCE_COMMIT" --generated-at "$GENERATED_AT" --settled-through "$SETTLED_THROUGH"
```

Expected: HTML, PNG, and status all reference the same build and ledger payload hashes.

- [ ] **Step 4: Inspect desktop and mobile website views**

Open `web/index.html` in the in-app browser at 1440x1000 and 390x844. Capture screenshots and verify:

- the first viewport shows `博弈预测看板` and report freshness;
- no text, table, filter, plan row, or alert overlaps;
- table wrappers scroll rather than widening mobile layout;
- filters work and a no-results state appears correctly;
- the next section remains discoverable below the first viewport;
- the existing football asset loads.

- [ ] **Step 5: Inspect the PNG at original resolution**

Open `web/daily-report.png` at original resolution. Verify all bounded sections, 30 daily rows maximum, Chinese font rendering, no clipped long team/play names, prior-day settlement labels, monthly controls, and cumulative values. Confirm its SHA-256 equals status.

- [ ] **Step 6: Rehearse governance failure paths**

With test fixtures only, exercise insufficient samples, worsening Brier with positive ROI, negative CLV, corrupt challenger artifact, promotion, and rollback. Confirm no path changes historical ledger bytes or enables real-money automation.

- [ ] **Step 7: Deploy and observe one complete report**

Push the feature branch through the normal review/merge path, sync the committed Apps Script copy to the existing project, and observe one complete Beijing report cycle. Verify the email subject, attachment, website title, previous-day settlement, recent-30-day table, cumulative totals, and once-only delivery.

- [ ] **Step 8: Record production versions**

Record the deployed Git commit, Apps Script version, active strategy version, model champion version/hash, report schema version, and deployment time in `apps-script/README.md`. Do not include tokens, recipient email, or any credential value.
