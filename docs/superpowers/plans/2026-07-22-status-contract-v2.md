# Status Contract V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the forecast, provisional, settlement, and email readiness contract with schema 2 so the cloud orchestrator advances exactly once through the new pre-kickoff workflow.

**Architecture:** `report_status.py` owns artifact-derived readiness, while `apps-script/Code.gs` consumes only the published schema 2 contract. Forecast readiness covers immutable import and prediction artifacts; refresh readiness is represented by `initial_report_ready`; normal email readiness requires the provisional evidence, settlement, revalidation index, matching image hash, and current report metadata. Legacy plan-lock flags remain readable but are not used to advance the new workflow.

**Tech Stack:** Python 3.12 `unittest`, Google Apps Script JavaScript, Node.js `node:test`, GitHub Actions.

## Global Constraints

- Keep all recommendations simulation-only; no real-money execution or external betting side effects.
- Domestic odds and fixture identity must remain fail-closed when live Sporttery and verified ZGZCW sources are unavailable.
- Apps Script remains the sole Gmail sender and may send only a hash-verified current-date image.
- Preserve the 14:00-18:00 Beijing normal-email window and the attachment-free 18:00 failure notice.
- Do not restore date-level plan locks or ingest provisional candidates into the paid simulation ledger.

---

### Task 1: Forecast Readiness Without Legacy Plan Artifacts

**Files:**
- Modify: `report_status.py`
- Test: `tests/test_report_status.py`

**Interfaces:**
- Consumes: `artifact_state(root, report_date, expected_report_stage, expected_build_id)`.
- Produces: `publish_status(..., phase="forecast")["forecast_ready"]` based on import, odds, predictions, site, and image artifacts only.

- [x] **Step 1: Write the failing tests**

Add a test that removes `betting_plan_DATE.csv` and `daily_decision_DATE.json` from otherwise valid forecast artifacts and still expects `forecast_ready is True`. Add a second test that removes the domestic odds JSON and expects `forecast_ready is False`.

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_report_status.ReportStatusTest.test_forecast_readiness_uses_prediction_phase_artifacts_only -v`

Expected: FAIL because the current implementation still requires `plan_csv_ready` and `decision_ready`.

- [x] **Step 3: Implement the schema 2 forecast gate**

Use these required artifact keys:

```python
forecast_ready = all(
    state[key]
    for key in (
        "source_ready",
        "fixtures_ready",
        "import_manifest_ready",
        "odds_ready",
        "official_odds_complete",
        "predictions_ready",
        "site_ready",
        "image_ready",
    )
)
```

- [x] **Step 4: Run the report-status tests**

Run: `python -m unittest tests.test_report_status -v`

Expected: PASS.

### Task 2: Apps Script Schema 2 Phase Dispatch

**Files:**
- Modify: `apps-script/Code.gs`
- Test: `tests/apps_script_orchestrator.test.mjs`

**Interfaces:**
- Consumes: schema 2 `web/report-status.json`.
- Produces: `phaseReady_(status, "forecast"|"refresh"|"settlement")` and `chooseDispatch_(clock, status, state)`.

- [x] **Step 1: Write failing dispatch tests**

Change test fixtures to schema 2 and assert:

```javascript
assert.equal(context.phaseReady_(status, "refresh"), status.initial_report_ready === true);
assert.equal(
  context.chooseDispatch_(clockAt(13, 45), {
    ...dispatchStatus(),
    forecast_ready: true,
    initial_report_ready: true,
  }, {}),
  "noon-settlement.yml",
);
```

Also assert schema 1 cannot skip any phase.

- [x] **Step 2: Run test to verify it fails**

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: FAIL because the current orchestrator accepts only schema 1 and waits for legacy decision/plan flags.

- [x] **Step 3: Implement schema 2 dispatch**

Require `status.schema_version === 2`. Keep forecast and settlement checks, and define refresh completion as:

```javascript
if (phase === "refresh") return status.initial_report_ready === true;
```

- [x] **Step 4: Run the Apps Script tests**

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: dispatch tests pass; email contract tests may remain red until Task 3.

### Task 3: Hash-Verified Initial Email Readiness

**Files:**
- Modify: `apps-script/Code.gs`
- Modify: `tests/apps_script_orchestrator.test.mjs`
- Modify: `apps-script/README.md`

**Interfaces:**
- Consumes: schema 2 status, provisional generation digest, data-quality flags, report image bytes, and revalidation readiness.
- Produces: `reportReadiness_(status, expectedDate, imageSha256)` that accepts only a complete current settlement build.

- [x] **Step 1: Write failing email-readiness tests**

Build the ready fixture with `initial_report_ready`, `revalidation_ready`, a canonical `provisional_plan_sha256`, and these required quality fields:

```javascript
[
  "source_ready",
  "fixtures_ready",
  "odds_ready",
  "predictions_ready",
  "decision_bundle_ready",
  "provisional_plan_ready",
  "provisional_shadow_ready",
  "provisional_state_ready",
  "ledger_ready",
  "site_ready",
  "image_ready",
]
```

Assert rejection for missing initial readiness, revalidation readiness, provisional digest, any required quality field, stale settlement, or image hash mismatch. Assert legacy `decision_snapshot_ready`, `plan_ready`, and plan-lock timestamps are not required.

- [x] **Step 2: Run tests to verify they fail**

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: FAIL on the old lock-based email contract.

- [x] **Step 3: Implement the new email gate**

Require schema 2, `forecast_ready`, `initial_report_ready`, `settlement_ready`, `revalidation_ready`, a current `settled_through`, valid report metadata, a 64-character provisional digest, every required quality flag, full official odds coverage, and an exact image SHA-256 match. Validate a zero-fixture report through official-source metadata and `zero_fixture_verified`, without requiring legacy decision timestamps. When provisional candidates exist, require the revalidation index to contain the current report date; allow an empty canonical index only for zero candidates. Rebind the settlement status digest and stake to the currently validated provisional generation.

- [x] **Step 4: Update operator documentation**

Document that forecast completion is prediction-only, refresh completion is `initial_report_ready`, and initial mail readiness no longer depends on obsolete plan-lock flags.

- [x] **Step 5: Run all local verification**

Run:

```powershell
python -m unittest discover -s tests -v
node --test tests/apps_script_orchestrator.test.mjs
git diff --check
```

Expected: 684 or more Python tests pass, all Node tests pass, and diff check is clean.

### Task 4: Publish and Exercise the Cloud Contract

**Files:**
- No additional source files.

**Interfaces:**
- Consumes: reviewed branch and passing tests.
- Produces: merged GitHub PR and one production workflow run whose phase progression is observable in `web/report-status.json`.

- [ ] **Step 1: Commit and open a narrowly scoped PR**

Commit only the status contract, tests, documentation, and this plan. Confirm the PR diff does not contain generated data or line-ending churn.

- [ ] **Step 2: Merge after review**

Squash merge with the verified head SHA and delete the remote temporary branch.

- [ ] **Step 3: Run the cloud workflow**

Run the daily forecast for the current Beijing business date and verify `forecast_ready=true`. Run the provisional refresh only when the live domestic source is available; if both approved live sources fail, require the workflow to remain failed and do not substitute cached odds.

- [ ] **Step 4: Record the Apps Script deployment boundary**

Confirm the repository contains the corrected `Code.gs`. Do not claim live Gmail orchestration is updated until that file and the schema 2 properties are synced to the existing online Apps Script project.
