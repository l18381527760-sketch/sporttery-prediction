# Pre-Kickoff Revalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the 14:00 daily betting report into an unaudited provisional shortlist, revalidate every candidate near T-90 and T-30 with genuinely fresh domestic odds, and record only final confirmed simulated bets.

**Architecture:** Keep the immutable midday import and prediction bundle as model evidence, add append-only live odds snapshots as execution evidence, and drive each candidate through a monotonic per-candidate state machine. Final T-30 receipts become the only new canonical ledger input; date-scoped status files and immutable revision images let Apps Script dispatch checks and send deduplicated update emails across Beijing midnight.

**Tech Stack:** Python 3.12 standard library, existing unittest suite, GitHub Actions YAML, Google Apps Script V8, Node.js built-in test runner, Pillow for report images.

## Global Constraints

- The system remains simulation-only and must never connect to or purchase from a real betting account.
- The 14:00 Beijing report is provisional and must not consume simulated stake or profit/loss.
- No candidate, market, selection, or parlay leg may be added after the provisional shortlist is published.
- A revalidated stake may stay unchanged or decrease; it may never exceed its provisional stake.
- Final paid odds must come from Sporttery, or from ZGZCW only after exact fixture and market mapping validation.
- Missing lineup, injury, or weather evidence must be labelled `unavailable`; it must never be invented or described as verified.
- Daily simulated stake is capped at 500 yuan, monthly stake at 5000 yuan, and new simulated bets stop when monthly realized loss reaches 5000 yuan.
- Existing locks, historical ledger rows, and settled profit/loss remain immutable.
- `value-v4` remains shadow-only until the existing prospective activation audit passes.
- One candidate may appear in at most one revalidation email; one workflow run groups all newly terminal candidates.
- All timestamps used for decisions must be timezone-aware and normalized to `Asia/Shanghai` / UTC+08:00.

---

## File Structure

**New production files**

- `live_odds.py`: fetch, normalize, publish, and validate append-only live domestic odds snapshots.
- `provisional_plan.py`: convert immutable decision-bundle outputs into stable active/shadow candidates and initialize date state.
- `revalidation.py`: determine due checkpoints, evaluate candidates, publish immutable receipts, and ingest final decisions.
- `revalidation_reporting.py`: publish per-date status, immutable revision images, and the two-date public index.
- `.github/workflows/pre-kickoff-revalidation.yml`: scheduled and dispatchable revalidation worker.

**Modified production files**

- `capture_odds_snapshot.py`, `decision_bundle.py`: retain historical snapshot behavior while accepting exact live evidence for provisional generation.
- `generate_betting_plan.py`, `betting_ledger.py`: expose deterministic candidate conversion and per-receipt canonical ingestion.
- `report_status.py`, `build_site.py`, `build_daily_image.py`: distinguish provisional stake from confirmed stake and bind public artifacts.
- `.github/workflows/daily-forecast.yml`, `draw-alert-refresh.yml`, `odds-snapshot.yml`, `noon-settlement.yml`, `email-report.yml`: enforce the new phase boundaries.
- `apps-script/Code.gs`, `apps-script/README.md`, `README.md`, `CLOUD_SETUP.md`: dispatch revalidation and send verified change reports.
- `betting_config.json`: add exact revalidation windows and cutover controls.

**New tests**

- `tests/test_live_odds.py`
- `tests/test_provisional_plan.py`
- `tests/test_revalidation.py`
- `tests/test_revalidation_reporting.py`

**Modified tests**

- `tests/test_decision_bundle.py`
- `tests/test_betting_ledger.py`
- `tests/test_report_status.py`
- `tests/test_report_build_metadata.py`
- `tests/test_workflow_schedule.py`
- `tests/apps_script_orchestrator.test.mjs`

---

### Task 1: Capture Genuinely Fresh Domestic Odds

**Files:**
- Create: `live_odds.py`
- Modify: `capture_odds_snapshot.py`
- Modify: `decision_bundle.py`
- Create: `tests/test_live_odds.py`
- Modify: `tests/test_capture_odds_snapshot.py`
- Modify: `tests/test_decision_bundle.py`

**Interfaces:**
- Produces: `capture_live_snapshot(root: Path, target_date: date, captured_at: datetime, preferred_source: str | None = None, sporttery_fetcher=None, sporttery_odds_fetcher=None, zgzcw_match_fetcher=None, zgzcw_odds_fetcher=None) -> Path`.
- Produces: `read_valid_live_snapshot(root: Path, path: Path, target_date: date, not_after: datetime | None = None) -> dict`.
- Modifies: `create_decision_bundle(root: Path, target_date: date, locked_at: datetime, decision_snapshot_path: Path | None = None) -> dict` so provisional generation can bind one exact live snapshot instead of selecting a same-day mutable filename.

- [ ] **Step 1: Write failing live-fetch tests**

Create `tests/test_live_odds.py` with fixed aware times and injected fetchers. The central test must prove no manifest odds reader is used:

```python
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import live_odds

BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 19)
NOW = datetime(2026, 7, 19, 20, 0, tzinfo=BJT)


class LiveOddsTest(TestCase):
    def test_capture_calls_live_sporttery_endpoints_and_never_manifest_odds(self):
        matches = [{
            "matchId": "m1", "matchNumStr": "周日001",
            "homeTeam": "主队", "awayTeam": "客队",
            "matchStatus": "Selling", "kickoff_at": "2026-07-19T22:00:00+08:00",
            "isSingleHad": True, "isSingleHhad": True, "isSingleTtg": True,
        }]
        odds = {"had": {"h": "2.80", "d": "3.10", "a": "2.25"}, "hhad": {}, "ttg": {}}
        with TemporaryDirectory() as tmp, patch(
            "live_odds.read_valid_import_manifest", side_effect=AssertionError("manifest odds read")
        ):
            path = live_odds.capture_live_snapshot(
                Path(tmp), DAY, NOW,
                sporttery_fetcher=lambda day: matches,
                sporttery_odds_fetcher=lambda match_id: odds,
            )
            payload = live_odds.read_valid_live_snapshot(Path(tmp), path, DAY, NOW)
        self.assertEqual("live", payload["fetch_mode"])
        self.assertEqual("sporttery", payload["source"])
        self.assertEqual("2.80", payload["matches"][0]["markets"]["had"]["h"])
```

Add separate tests for: exact ZGZCW fallback mapping by match number, teams, kickoff and market; fallback rejection on any mismatch; already-started filtering; invalid/naive kickoff rejection; source response with missing markets; and conflicting append-only filename rejection.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m unittest tests.test_live_odds -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'live_odds'`.

- [ ] **Step 3: Implement live normalization and append-only publication**

Implement the two exact public signatures listed in this task's **Interfaces** block. Define these module constants:

```python
BEIJING = timezone(timedelta(hours=8))
LIVE_SCHEMA_VERSION = 1
DOMESTIC_SOURCES = frozenset({"sporttery", "zgzcw"})
```

Use `fetch_selling_matches` and `fetch_odds` as default Sporttery dependencies. Use `fetch_zgzcw_matches` and `fetch_zgzcw_odds` only after a Sporttery exception or when `preferred_source == "zgzcw"`. Normalize only `had`, `hhad`, and `ttg`; reject a fixture with no non-empty supported market. Require exact aware `kickoff_at`, future kickoff, unique match IDs, source record IDs, market sales state, and single eligibility.

Publish canonical JSON under `data/live_odds_snapshots/YYYY-MM-DD/` with an exclusive `open("xb")`; derive the filename from timestamp, source, and payload SHA prefix. If the exact path exists, accept it only when bytes match.

- [ ] **Step 4: Bind an exact live snapshot into decision evidence**

Extend `create_decision_bundle` with `decision_snapshot_path=None`. When supplied, call `read_valid_live_snapshot`, require `captured_at_bjt <= locked_at`, validate the import-manifest fixture identities against the live snapshot, and record the live file path/hash in the existing `decision_snapshot` record. Keep current schema-3 bundles readable and current snapshot selection unchanged when the argument is omitted.

Add CLI support:

```text
python decision_bundle.py --date YYYY-MM-DD --locked-at <aware-iso> --decision-snapshot <path>
```

- [ ] **Step 5: Make the monitoring CLI explicitly live**

Add `--live` to `capture_odds_snapshot.py`. When present, delegate to `capture_live_snapshot`; without it preserve the current historical/manifest behavior for old tests and artifacts. A `decision` phase used by the new workflow must pass `--live` and print the exact new path.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_live_odds tests.test_capture_odds_snapshot tests.test_decision_bundle -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the live evidence boundary**

```powershell
git add live_odds.py capture_odds_snapshot.py decision_bundle.py tests/test_live_odds.py tests/test_capture_odds_snapshot.py tests/test_decision_bundle.py
git commit -m "feat: capture immutable live domestic odds"
```

---

### Task 2: Generate Provisional Candidates Without Ledger Stake

**Files:**
- Create: `provisional_plan.py`
- Modify: `generate_betting_plan.py`
- Modify: `report_status.py`
- Create: `tests/test_provisional_plan.py`
- Modify: `tests/test_report_status.py`

**Interfaces:**
- Consumes: an immutable schema-3 decision bundle bound to a live snapshot.
- Produces: `candidate_from_plan_row(row: dict, route: str, report_date: date, provisional_rank: int) -> dict`.
- Produces: `create_provisional_outputs(root: Path, target_date: date, generated_at: datetime, decision_bundle: dict) -> dict`.
- Produces: `read_valid_provisional_state(root: Path, target_date: date) -> dict`.

- [ ] **Step 1: Write failing candidate and no-ledger tests**

Create tests showing stable identity excludes mutable odds/stake, while the payload digest includes them:

```python
def test_candidate_id_is_stable_but_payload_digest_attests_odds_and_stake(self):
    first = candidate_from_plan_row(plan_row(odds="3.10", stake="80"), "active", DAY, 1)
    second = candidate_from_plan_row(plan_row(odds="3.20", stake="60"), "active", DAY, 1)
    self.assertEqual(first["candidate_id"], second["candidate_id"])
    self.assertNotEqual(first["candidate_payload_sha256"], second["candidate_payload_sha256"])
```

Add tests proving: active/shadow routes are separate; no candidate is less than 60 minutes from its earliest kickoff; a 2-leg parlay uses the earliest leg kickoff; duplicate normalized identities fail; minimum acceptable odds equals `(1 + minimum_ev) / conservative_probability`; initial state is `screened` with a T-90 receipt when kickoff is 60-105 minutes away; later candidates start `provisional`; and `output/betting_ledger.csv` bytes do not change during provisional generation.

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_provisional_plan -v
```

Expected: FAIL because `provisional_plan` does not exist.

- [ ] **Step 3: Expose deterministic strategy outputs without writing final artifacts**

Keep `build_strategy_outputs` pure. Add a helper in `generate_betting_plan.py`:

```python
def strategy_outputs_from_bundle(
    target_date: date,
    decision_bundle: dict,
    generated_at: datetime,
) -> StrategyOutputs:
    return build_strategy_outputs(
        target_date,
        locked_at=_aware_locked_at(generated_at),
        decision_bundle=decision_bundle,
    )
```

Do not call `write_plan`, `plan_lock.lock_plan`, or `betting_ledger.ingest_date` from this path.

- [ ] **Step 4: Implement candidate serialization and initial state**

In `provisional_plan.py`, implement the two exact public signatures listed in this task's **Interfaces** block and define:

```python
PROVISIONAL_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
MIN_INITIAL_MINUTES = 60
T90_EARLY_MINUTES = 105
```

Write `provisional_betting_plan_DATE.csv`, `provisional_shadow_plan_DATE.csv`, and `revalidation_state_DATE.json` atomically. Build `candidate_id` from date, route, strategy version, normalized market identity, and sorted parlay legs. Build `candidate_payload_sha256` from the complete normalized candidate excluding only that digest field.

For a candidate at 60-105 minutes, create an immutable T-90 receipt using the initial live snapshot and mark it `screened`. For a later candidate, mark it `provisional`. Reject any candidate below 60 minutes; do not silently retain it in a paid-looking output.

- [ ] **Step 5: Publish report status schema 2 for provisional readiness**

Extend `report_status.py` so schema 2 has:

```json
{
  "schema_version": 2,
  "report_stage": "provisional",
  "initial_report_ready": true,
  "provisional_plan_sha256": "<64 hex>",
  "confirmed_stake": 0,
  "provisional_stake": 140
}
```

Decision readiness must require the valid decision bundle, both provisional CSV headers, valid state, site, and image. It must not require a date-level `plan_lock`. Continue reading schema 1 for previously published dates.

- [ ] **Step 6: Run candidate and status tests**

```powershell
python -m unittest tests.test_provisional_plan tests.test_report_status tests.test_value_strategy_integration -v
```

Expected: PASS, including existing activation-mode assertions.

- [ ] **Step 7: Commit provisional generation**

```powershell
git add provisional_plan.py generate_betting_plan.py report_status.py tests/test_provisional_plan.py tests/test_report_status.py
git commit -m "feat: publish provisional betting candidates"
```

---

### Task 3: Implement the Monotonic T-90/T-30 State Machine

**Files:**
- Create: `revalidation.py`
- Create: `tests/test_revalidation.py`
- Modify: `betting_config.json`

**Interfaces:**
- Consumes: live snapshot reader from Task 1 and provisional state from Task 2.
- Produces: `due_stage(candidate: dict, now: datetime) -> str | None`.
- Produces: `evaluate_candidate(candidate: dict, snapshot: dict, stage: str, checked_at: datetime, config: dict, remaining_caps: dict | None = None) -> dict`.
- Produces: `run_due_revalidation(root: Path, now: datetime, target_dates: list[date] | None = None, snapshot_provider=capture_live_snapshot) -> list[dict]`.

- [ ] **Step 1: Write failing time-window and transition tests**

Use candidates with absolute aware kickoffs:

```python
def test_due_stage_uses_earliest_parlay_leg(self):
    candidate = parlay_candidate(
        kickoffs=["2026-07-20T01:30:00+08:00", "2026-07-20T03:00:00+08:00"],
        state="screened",
    )
    self.assertEqual(
        "t30",
        due_stage(candidate, datetime.fromisoformat("2026-07-20T00:55:00+08:00")),
    )
```

Add tests for: T-90 due at 105 minutes and not before; T-30 due at 40 minutes for screened only; `t90_window_missed` at 40 minutes for provisional; `t30_window_missed` at 10 minutes; no transition after kickoff; terminal state immutability; scan of today and yesterday after Beijing midnight; and deterministic ordering by earliest kickoff, provisional rank, candidate ID.

- [ ] **Step 2: Run and verify RED**

```powershell
python -m unittest tests.test_revalidation -v
```

Expected: FAIL because `revalidation` does not exist.

- [ ] **Step 3: Add exact configuration**

Add this block to `betting_config.json`:

```json
"pre_kickoff_revalidation": {
  "mode": "shadow",
  "minimum_initial_minutes": 60,
  "t90_open_minutes": 105,
  "t90_close_minutes": 40,
  "t30_open_minutes": 40,
  "t30_close_minutes": 10,
  "scan_business_days": 2,
  "stake_unit": 2,
  "max_notification_days": 30
}
```

Reject missing, boolean, non-integral, overlapping, or inverted window values. `mode` accepts only `shadow` and `active`.

- [ ] **Step 4: Implement stage evaluation**

`evaluate_candidate` must:

1. validate the candidate digest and current state;
2. match every normalized leg to the fresh snapshot;
3. require source, fixture, market line, selection, single eligibility, sales state, and pre-kickoff time;
4. preserve the candidate conservative probability;
5. calculate current EV and minimum acceptable odds with `Decimal`;
6. compute quarter-Kelly stake, round down to 2 yuan, and cap it at the provisional stake and supplied remaining caps;
7. cancel if any leg fails or final stake is below 2 yuan;
8. create a complete receipt payload with one of the fixed reason codes from the specification.

Use an explicit transition table:

```python
ALLOWED_TRANSITIONS = {
    ("provisional", "t90", "pass"): "screened",
    ("provisional", "t90", "cancel"): "cancelled",
    ("screened", "t30", "confirm"): "confirmed",
    ("screened", "t30", "cancel"): "cancelled",
}
```

- [ ] **Step 5: Publish receipts and state atomically**

Write receipts with exclusive create under `output/revalidation_receipts/DATE/`. Build the new state entirely in memory, verify every unchanged terminal receipt, then atomically replace the state file. If state publication fails, no ledger operation may run.

`run_due_revalidation` must fetch one new live snapshot per business date only when at least one candidate is due. It must process at most the configured last two business dates and never use the machine's local timezone.

Store a separate monotonic `ledger_status` per candidate: `not_applicable`, `pending`, or `ingested`. A newly confirmed candidate is `pending`; a cancelled candidate is `not_applicable`. Candidate state and ledger status are validated independently so an external write failure never requires an illegal candidate-state rollback.

- [ ] **Step 6: Run state-machine tests**

```powershell
python -m unittest tests.test_revalidation tests.test_live_odds tests.test_provisional_plan -v
```

Expected: PASS.

- [ ] **Step 7: Commit the state machine**

```powershell
git add revalidation.py betting_config.json tests/test_revalidation.py
git commit -m "feat: add pre-kickoff candidate state machine"
```

---

### Task 4: Ingest Only Final Receipt-Backed Simulated Bets

**Files:**
- Modify: `betting_ledger.py`
- Modify: `revalidation.py`
- Modify: `tests/test_betting_ledger.py`
- Modify: `tests/test_revalidation.py`

**Interfaces:**
- Produces: `ingest_revalidated_receipts(root: Path, target_date: date, receipt_paths: list[Path]) -> Path`.
- Produces: `read_valid_revalidation_receipt(root: Path, path: Path, target_date: date, expected_stage: str = "t30") -> dict`.

- [ ] **Step 1: Write failing canonical-ledger tests**

Add a test that starts with a valid T-90 receipt, final T-30 receipt, live snapshot and candidate digest, then asserts one pending paid row:

```python
def test_only_confirmed_active_receipt_enters_paid_ledger(self):
    path = ingest_revalidated_receipts(self.root, DAY, [self.write_confirmed_receipt(route="active")])
    rows = read_csv(path)
    self.assertEqual(1, len(rows))
    self.assertEqual("revalidation_receipt", rows[0]["evidence_type"])
    self.assertEqual("pending", rows[0]["status"])
    self.assertEqual("60.00", rows[0]["stake"])
```

Add tests proving: provisional, screened and cancelled receipts never enter; revalidation `mode=shadow` writes only the rehearsal ledger; in `mode=active`, active confirmations enter the paid ledger and strategy-shadow confirmations enter only the observation ledger; receipt/snapshot/candidate hash tampering fails; T-30 capture after kickoff fails; T-30 before T-90 fails; final odds mismatch fails; stable rerun adds no duplicate; a simulated ledger-write failure leaves `ledger_status=pending` and the next run ingests exactly once; deterministic same-run budget ordering; stake cannot increase; daily/monthly/match/parlay caps remain enforced; monthly realized-loss stop remains enforced; and old schema-3 plan-lock rows remain valid.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
python -m unittest tests.test_betting_ledger.BettingLedgerTest -v
```

Expected: FAIL because `ingest_revalidated_receipts` is absent.

- [ ] **Step 3: Extend ledger schema without migrating history**

Append these fields to `LEDGER_FIELD_ORDER` and the canonical payload digest:

```python
"evidence_type", "candidate_id", "candidate_payload_sha256",
"t90_receipt_path", "t90_receipt_sha256",
"t30_receipt_path", "t30_receipt_sha256",
"live_odds_snapshot_path", "live_odds_snapshot_sha256",
"final_confirmed_at_bjt",
```

Existing rows default to `evidence_type == "plan_lock"` when a valid plan-lock evidence set is present. New rows must explicitly use `revalidation_receipt`. Do not backfill or rewrite terminal historical rows merely to populate blank new columns.

- [ ] **Step 4: Validate receipts and append atomically**

Implement `read_valid_revalidation_receipt` with full root-relative path containment, SHA-256, canonical payload, timestamp, candidate, snapshot, market, amount, and transition validation.

`ingest_revalidated_receipts` must sort valid confirmations by earliest kickoff, provisional rank, and candidate ID, recalculate remaining caps against existing paid rows after each accepted row, construct stable bet IDs from date/candidate/final receipt digest, and call `write_ledger_atomic` once. A failed receipt aborts the entire batch before any file replacement.

When `pre_kickoff_revalidation.mode == "shadow"`, write every confirmed receipt to `output/revalidation_rehearsal_ledger.csv` and leave paid and observation ledgers byte-identical. When that mode is `active`, route active confirmations to the paid ledger and strategy-shadow confirmations through a separate prepared observation row list; never include observation stake in paid account caps. This mode is independent of `value_strategy.activation_mode`.

- [ ] **Step 5: Call ledger ingestion only after state publication**

In `run_due_revalidation`, publish and re-read the state first. Then collect every `confirmed + ledger_status=pending` receipt path, not only confirmations created in the current process, and call the ledger API. After successful idempotent ingestion, atomically advance those candidates to `ledger_status=ingested`. If ledger ingestion fails, keep them pending and publish no user-visible `change_digest`; rerun must safely retry the same immutable receipts.

- [ ] **Step 6: Run ledger and settlement regressions**

```powershell
python -m unittest tests.test_betting_ledger tests.test_revalidation tests.test_update_sporttery_results tests.test_value_strategy_integration -v
```

Expected: PASS.

- [ ] **Step 7: Commit receipt-backed ingestion**

```powershell
git add betting_ledger.py revalidation.py tests/test_betting_ledger.py tests/test_revalidation.py
git commit -m "feat: ingest receipt-backed simulated bets"
```

---

### Task 5: Publish Date-Scoped Status, Immutable Images, and Clear UI States

**Files:**
- Create: `revalidation_reporting.py`
- Modify: `build_site.py`
- Modify: `build_daily_image.py`
- Modify: `report_status.py`
- Create: `tests/test_revalidation_reporting.py`
- Modify: `tests/test_report_build_metadata.py`
- Modify: `tests/test_report_status.py`

**Interfaces:**
- Produces: `publish_revalidation_report(root: Path, report_date: date, changed_candidates: list[dict], generated_at: datetime, source_commit_sha: str) -> dict`.
- Produces: `build_revalidation_index(root: Path, now: datetime) -> dict`.
- Modifies: `draw_report(output_path: Path | None = None, report_date: date | None = None, revalidation_changes: list[dict] | None = None) -> Path`.

- [ ] **Step 1: Write failing immutable-report tests**

Create `tests/test_revalidation_reporting.py` with two dates spanning midnight. Assert that publishing the second date does not modify the first date's image bytes:

```python
def test_cross_midnight_publication_keeps_prior_date_image_immutable(self):
    first = publish_revalidation_report(self.root, DAY, [confirmed("c1")], AT_2355, "abc")
    old_path = self.root / first["report_image_url"]
    old_bytes = old_path.read_bytes()
    publish_revalidation_report(self.root, DAY + timedelta(days=1), [cancelled("c2")], AT_0010, "def")
    self.assertEqual(old_bytes, old_path.read_bytes())
```

Add tests for: status/image SHA match; exclusive revision image names; monotonic revision; digest grouping of all new terminal candidates; two-date index; omission of completed and fully notified dates; index status-file hashes; no visible update before ledger ingestion succeeds; and report builders displaying provisional and confirmed stake separately.

- [ ] **Step 2: Run and verify RED**

```powershell
python -m unittest tests.test_revalidation_reporting -v
```

Expected: FAIL because `revalidation_reporting` does not exist.

- [ ] **Step 3: Make the image builder destination-aware**

Change `draw_report` to accept explicit output path, report date, and change rows while preserving the current zero-argument daily report behavior. Include PNG metadata keys `build_id`, `report_date`, `change_digest`, and `report_stage` without visibly rendering internal hashes.

The revalidation image must visibly contain: candidate match/market, confirmed or cancelled state, provisional/current odds, provisional/final stake, current EV, and short reason. It must never label provisional stake as invested stake.

- [ ] **Step 4: Implement status and index atomic publication**

`publish_revalidation_report` must write the immutable PNG first, then atomically write `web/revalidation/DATE/status.json`, verify both from disk, and finally atomically update `web/revalidation-index.json`. The index includes at most two business dates and stores each status path/hash/revision/next due time.

Use `change_digest = sha256(canonical_json(reportable_terminal_candidates))`, where a confirmed candidate is reportable only after `ledger_status=ingested` and a cancelled candidate is reportable immediately. A rerun with no newly reportable candidates must not create a revision or change the digest.

- [ ] **Step 5: Update website and daily image labels**

Read provisional CSV plus revalidation state. Display exactly these user-facing labels: `初选待复核`, `90分钟筛查通过`, `临场确认`, `临场降额`, `已撤销`. The daily and cumulative P/L tables continue to read only canonical paid ledger rows. Add a separate provisional total labelled `暂定金额（未计入盈亏）`.

- [ ] **Step 6: Run reporting regressions**

```powershell
python -m unittest tests.test_revalidation_reporting tests.test_report_build_metadata tests.test_report_status tests.test_draw_alert_reporting -v
```

Expected: PASS.

- [ ] **Step 7: Commit reporting changes**

```powershell
git add revalidation_reporting.py build_site.py build_daily_image.py report_status.py tests/test_revalidation_reporting.py tests/test_report_build_metadata.py tests/test_report_status.py
git commit -m "feat: publish immutable revalidation reports"
```

---

### Task 6: Rewire GitHub Actions Around Provisional and Final Phases

**Files:**
- Create: `.github/workflows/pre-kickoff-revalidation.yml`
- Modify: `.github/workflows/daily-forecast.yml`
- Modify: `.github/workflows/draw-alert-refresh.yml`
- Modify: `.github/workflows/odds-snapshot.yml`
- Modify: `.github/workflows/noon-settlement.yml`
- Modify: `.github/workflows/email-report.yml`
- Modify: `tests/test_workflow_schedule.py`

**Interfaces:**
- `daily-forecast.yml`: prediction only.
- `draw-alert-refresh.yml`: live snapshot plus provisional publication.
- `pre-kickoff-revalidation.yml`: scan, revalidate, report, commit and deploy.
- `odds-snapshot.yml`: live monitoring only.

- [ ] **Step 1: Write failing workflow contract tests**

Add assertions requiring:

```python
self.assertNotIn("generate_betting_plan.py", daily_forecast_generation_block)
self.assertIn("capture_odds_snapshot.py --date \"$TARGET_DATE\" --phase decision --live", refresh_text)
self.assertIn("provisional_plan.py --date \"$TARGET_DATE\"", refresh_text)
self.assertNotIn("betting_ledger.py ingest", refresh_text)
self.assertIn('cron: "*/10 * * * *"', revalidation_text)
self.assertIn("python revalidation.py run-due", revalidation_text)
self.assertIn("python revalidation_reporting.py", revalidation_text)
self.assertNotIn("schedule:", email_report_trigger_block)
```

Also require shared concurrency, latest-main checkout, Beijing timezone, manual `target_date`/`now` inputs, live monitoring mode, no stale manifest snapshot path, Pages deployment only after status publication, and settlement of confirmed canonical rows only.

- [ ] **Step 2: Run and verify RED**

```powershell
python -m unittest tests.test_workflow_schedule -v
```

Expected: FAIL on missing workflow and old phase commands.

- [ ] **Step 3: Fix base and provisional workflows**

Remove final plan generation from `daily-forecast.yml`; keep import, features, prediction, optional draw evidence, and base site publication. In `draw-alert-refresh.yml`, replace date-level lock/ledger commands with this required order:

```bash
python import_sporttery.py --date "$TARGET_DATE"
LIVE_PATH="$(python capture_odds_snapshot.py --date "$TARGET_DATE" --phase decision --live --print-path)"
python predict_today.py --date "$TARGET_DATE"
PROVISIONAL_AT_BJT="$(date --iso-8601=seconds)"
python decision_bundle.py --date "$TARGET_DATE" --locked-at "$PROVISIONAL_AT_BJT" --decision-snapshot "$LIVE_PATH"
python provisional_plan.py --date "$TARGET_DATE" --generated-at "$PROVISIONAL_AT_BJT"
```

Do not create `plan_lock_DATE.json` or ingest the paid ledger in this workflow.

- [ ] **Step 4: Add the revalidation workflow**

Support optional `target_date` and `now_bjt` inputs for deterministic manual rehearsal. Production schedule is `*/10 * * * *`. Run `python revalidation.py run-due`, inspect its machine-readable changed-date output, and only for changed dates rebuild site/images/status/index. Commit `data output web` only when changed. Use the shared repository concurrency group and Pages environment.

- [ ] **Step 5: Make monitoring snapshots live and disable GitHub email scheduling**

Change `odds-snapshot.yml` to `--phase monitoring --live`. Keep it observation-only: no state or ledger mutation.

Remove `schedule` from `email-report.yml`, rename it as a manual diagnostic sender, and add an environment guard `ALLOW_MANUAL_EMAIL_DIAGNOSTIC == "true"` so it cannot become a second production sender accidentally.

- [ ] **Step 6: Run workflow and full Python tests**

```powershell
python -m unittest tests.test_workflow_schedule -v
python -m unittest discover -s tests -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit workflow integration**

```powershell
git add .github/workflows tests/test_workflow_schedule.py
git commit -m "feat: schedule provisional and pre-kickoff phases"
```

---

### Task 7: Dispatch Revalidation and Send Deduplicated Change Emails

**Files:**
- Modify: `apps-script/Code.gs`
- Modify: `apps-script/appsscript.json`
- Modify: `tests/apps_script_orchestrator.test.mjs`
- Modify: `apps-script/README.md`

**Interfaces:**
- Produces: `revalidationIndex_(config)`.
- Produces: `chooseRevalidationDispatch_(clock, index, state)`.
- Produces: `pendingRevalidationEmails_(index, state)`.
- Produces: `sendRevalidationUpdate_(entry, status, imageBytes, config)`.

- [ ] **Step 1: Write failing Apps Script tests**

Add Node tests for: due previous-day candidate after midnight; due revalidation has priority over non-urgent daily phase dispatch; cooldown; status-file hash mismatch; revision image hash mismatch; no update when initial email was never sent; grouped terminal candidates; confirmed-with-unchanged-terms still sends one final confirmation; same `report_date + change_digest` sends once; Gmail failure does not mark sent; 30-day digest pruning; and initial 18:00 failure cutoff not blocking a previous date's valid revalidation update.

Use this representative assertion:

```javascript
test("00:10 dispatches previous business day revalidation", () => {
  const { context, dispatches } = makeContext({
    now: "2026-07-20T00:10:00+08:00",
    revalidationIndex: indexWithDueDate("2026-07-19", "2026-07-20T00:05:00+08:00"),
  });
  context.runAutomation();
  assert.match(dispatches[0].url, /pre-kickoff-revalidation\.yml\/dispatches$/);
});
```

- [ ] **Step 2: Run and verify RED**

```powershell
node --test tests/apps_script_orchestrator.test.mjs
```

Expected: FAIL because the revalidation helpers and workflow constant are absent.

- [ ] **Step 3: Add configuration and hash-verified index loading**

Require Script Property `REVALIDATION_INDEX_URL`. Fetch with cache-busting, verify schema, allow at most two entries, fetch each status path, compare its bytes to the index SHA-256, then validate the date/revision/change digest/image URL fields.

Do not log tokens, response headers, report bytes, or Gmail credentials.

- [ ] **Step 4: Dispatch time-critical revalidation first**

In `runAutomation`, after acquiring the script lock and reading state, choose at most one dispatch. A due revalidation is higher priority than forecast/refresh/settlement because missing a kickoff window is irreversible. Use independent attempt/confirmed keys:

```text
LAST_REVALIDATION_DISPATCH_DATE
LAST_REVALIDATION_DISPATCH_AT
LAST_REVALIDATION_DISPATCH_ATTEMPT_DATE
LAST_REVALIDATION_DISPATCH_ATTEMPT_AT
```

Pass the due `target_date` and current aware Beijing `now_bjt` as workflow inputs.

- [ ] **Step 5: Send and deduplicate update images**

Send only when the corresponding initial report date is recorded, status and immutable image hashes match, and the digest is absent from `SENT_REVALIDATION_DIGESTS`. The subject is:

```text
[临场确认] YYYY-MM-DD 博弈预测方案更新
```

After `GmailApp.sendEmail` succeeds, append `{report_date, change_digest, sent_at_bjt, candidate_ids}`. Prune entries older than 30 business dates and write the property once. On Gmail failure, preserve the old property unchanged.

Keep `LAST_SENT_DATE` as a read-only compatibility alias during migration; new initial sends write `LAST_INITIAL_SENT_DATE`.

- [ ] **Step 6: Run all Apps Script tests**

```powershell
node --test tests/apps_script_orchestrator.test.mjs
```

Expected: PASS with no real network or Gmail call.

- [ ] **Step 7: Commit Apps Script changes**

```powershell
git add apps-script/Code.gs apps-script/appsscript.json apps-script/README.md tests/apps_script_orchestrator.test.mjs
git commit -m "feat: email verified pre-kickoff updates"
```

---

### Task 8: Document Cutover, Rehearse, and Verify the Complete System

**Files:**
- Modify: `README.md`
- Modify: `CLOUD_SETUP.md`
- Modify: `apps-script/README.md`
- Modify: `betting_config.json` only when switching `pre_kickoff_revalidation.mode` after rehearsal.
- Test/verify all changed production files.

- [ ] **Step 1: Add documentation contract assertions**

Extend `tests/test_workflow_schedule.py` to require the docs to state: 14:00 is provisional; provisional stake is excluded from P/L; T-90/T-30 windows; final stake cannot increase; missed windows cancel; corrections may cross midnight; Apps Script is the sole sender; GitHub scheduled email is disabled; and rollback means zero new simulated bets rather than restoring early locking.

- [ ] **Step 2: Run documentation tests and verify RED**

```powershell
python -m unittest tests.test_workflow_schedule -v
```

Expected: FAIL on missing operational text.

- [ ] **Step 3: Update operator documentation**

Document exact artifact paths, reason codes, manual workflow inputs, Script Property `REVALIDATION_INDEX_URL`, digest pruning, shadow/active cutover, failure recovery, and rollback. Clearly distinguish predictive evidence, execution odds, provisional candidates, confirmed simulated bets, and observation-only shadow rows.

- [ ] **Step 4: Run static and full regression verification**

```powershell
python -m py_compile live_odds.py provisional_plan.py revalidation.py revalidation_reporting.py betting_ledger.py report_status.py build_site.py build_daily_image.py
python -m unittest discover -s tests -v
node --test tests/apps_script_orchestrator.test.mjs
git diff --check origin/main..HEAD
```

Expected: every command exits 0 with no warnings attributable to new code.

- [ ] **Step 5: Run a deterministic cross-midnight rehearsal**

Use test fixtures in a temporary root with `pre_kickoff_revalidation.mode="active"` only inside that isolated rehearsal, and these aware timestamps:

```text
provisional: 2026-07-19T14:00:00+08:00
t90:         2026-07-19T23:30:00+08:00
t30:         2026-07-20T00:30:00+08:00
kickoff:     2026-07-20T01:00:00+08:00
```

Assert: two different live snapshot hashes; valid ordered receipts; final amount no greater than provisional; one canonical pending active row; no shadow stake in paid ledger; prior-date immutable image still matches status after the next date is published; repeated revalidation changes no bytes and sends no second email.

- [ ] **Step 6: Commit docs and rehearsal fixes**

```powershell
git add README.md CLOUD_SETUP.md apps-script/README.md betting_config.json tests
git commit -m "docs: explain pre-kickoff simulation controls"
```

- [ ] **Step 7: Request independent code review**

Use `superpowers:requesting-code-review` against `origin/main..HEAD`. Resolve every Critical and Important finding with a new failing regression test before changing production code. Re-run Step 4 after the final fix.

- [ ] **Step 8: Shadow rollout and activation gate**

Merge with `pre_kickoff_revalidation.mode == "shadow"`. Observe one complete business date containing staggered kickoffs and verify live source timestamps, receipt ordering, cancellation reasons, cross-midnight handling, and email deduplication. Only after that evidence passes may a separate reviewed commit change the mode to `active`; that change still does not activate value-v4.

---

## Plan Self-Review Result

- Every design requirement maps to a task: live odds (Task 1), provisional separation (Task 2), timing/state (Task 3), immutable ledger evidence (Task 4), cross-midnight artifacts (Task 5), automation (Task 6), email (Task 7), rollout and rollback (Task 8).
- Existing schema-3 bundles, plan locks, ledgers, settlement, value-v4 activation, and historical P/L retain explicit regression coverage.
- Production interfaces use consistent names across tasks: `capture_live_snapshot`, `create_provisional_outputs`, `run_due_revalidation`, `ingest_revalidated_receipts`, and `publish_revalidation_report`.
- The plan contains no deferred implementation placeholders; uncertain third-party lineup data is intentionally represented by the explicit `unavailable` state required by the approved design.
