# Opportunity-Gated Email Final Fix Report

## Scope

- Worktree: `C:\Users\87562\Documents\世界杯预测\.worktrees\actions-runtime-regressions`
- Starting HEAD: `23dd7350ef71ed3bf7cbd4de1ea3aad2480f15ca`
- Design: `docs/superpowers/specs/2026-07-24-opportunity-gated-email-design.md`
- Plan: `docs/superpowers/plans/2026-07-24-opportunity-gated-email.md`
- Method: focused test-first RED/GREEN cycles for every behavioral change, followed by the requested integrated suites.

## Baseline

- Apps Script orchestrator: 71 tests passed.
- Requested Python module set: 174 tests passed.
- The full 771-test controller suite was intentionally not run, per the task instruction.

## Finding 1: Current Status Identity, Fail-Closed Mail Mode, and Cutoff Diagnostics

### RED evidence

Focused tests were added for:

- prior-date and unsupported-schema statuses at 21:00;
- readiness, revalidation-index/coverage, and opportunity diagnostics in the attachment-free cutoff notice;
- missing and misspelled `TEST_MODE` values across normal, failure-notice, and revalidation mail paths.

The first focused run reported 0 passed and 3 failed:

- stale and schema-2 statuses sent a cutoff email;
- the cutoff notice omitted the requested diagnostics;
- missing `TEST_MODE` entered production Gmail paths.

### Implementation

- Require exact schema 3 and `status.report_date === clock.date` before normalizing `email_opportunity` or sending a cutoff failure notice.
- Centralize mail mode parsing:
  - exact `TEST_MODE=true` is dry-run;
  - exact `TEST_MODE=false` is production;
  - every other or missing value is disabled.
- Return without Gmail calls or mail sent-state writes when delivery is disabled.
- At 21:00, validate report readiness using the status hash without fetching the report image.
- Include readiness failures, detailed revalidation-index failures, revalidation coverage failures, opportunity reasons, and the cutoff reason in the attachment-free notice.

### GREEN evidence

- Focused Apps Script regressions: 3/3 passed.
- Complete Apps Script orchestrator suite: 74/74 passed.

### Files changed

- `apps-script/Code.gs`
- `tests/apps_script_orchestrator.test.mjs`

### Commit

- `580031b` `fix: harden opportunity email delivery`

## Finding 2: Immutable Decision Evidence Lifecycle

### RED evidence

Focused evidence-health tests first failed with `TypeError` because no immutable decision context was accepted. Focused report-status tests then failed because:

- no bundle helper derived the future-fixture universe at capture;
- settlement publication more than 30 minutes after decision capture aged valid locked evidence into `decision_odds_stale`.

The RED cases covered:

- an already-started fixture excluded from decision coverage;
- a settlement retry at 15:00 preserving readiness for evidence locked at 13:31;
- genuine evidence older than 30 minutes at the immutable evaluation point remaining stale.

### Implementation

- Derive immutable expected decision bindings from bundle fixtures whose kickoff follows the decision snapshot capture.
- Use the validated bundle lock time as the decision freshness horizon.
- Keep publication-time snapshot coverage in the published metrics.
- Recompute only decision readiness coverage as of the immutable horizon.
- Later phases therefore revalidate bundle and snapshot integrity without aging valid locked evidence against arbitrary publication time.

### GREEN evidence

- Focused evidence-health and report-status regressions: 4/4 passed.
- Complete `test_evidence_health` plus `test_report_status` suites: 84/84 passed.

### Files changed

- `decision_bundle.py`
- `evidence_health.py`
- `report_status.py`
- `tests/test_evidence_health.py`
- `tests/test_report_status.py`

### Commit

- `e8cd3b6` `fix: preserve immutable decision evidence readiness`

## Finding 3: T-90/T-30 Transition Dead Zone

### RED evidence

Boundary regressions were added at 45, 41, 40, and 10 minutes. The first focused run produced five failures and four errors:

- `live_odds.py` labeled 41-45 minute evidence as T-30 while the scheduler requested T-90;
- missed T-90/T-30 windows still called the strict market snapshot provider;
- candidates could fail instead of reaching a deterministic terminal state.

### Implementation

- Align per-match snapshot phases with the scheduler at the 40-minute handoff.
- Use T-90 evidence from 105 through strictly more than 40 minutes.
- Deterministically cancel a still-provisional candidate at 40 minutes or later in the missed T-90 path.
- Use T-30 evidence for screened candidates from 40 through strictly more than 10 minutes.
- Deterministically cancel a screened candidate at 10 minutes or later in the missed T-30 path.
- Missed-window receipts bind no fabricated market snapshot; replay accepts those receipts while retaining compatibility with earlier snapshot-bound missed receipts.
- Existing monotonic-stake and no-post-kickoff behavior remains intact.

### GREEN evidence

- Focused boundary regressions: 5/5 passed.
- Complete `test_live_odds` plus `test_revalidation` suites: 53/53 passed.

### Files changed

- `live_odds.py`
- `revalidation.py`
- `tests/test_live_odds.py`
- `tests/test_revalidation.py`

### Commit

- `b75d6fa` `fix: close revalidation transition dead zone`

## Finding 4: Consumer-First Schema Rollout

### RED evidence

Documentation regression tests were added across all four rollout documents. The initial focused run failed for all four because the required consumer-first sequence was absent.

### Implementation

All rollout documents now require this order:

1. Pause the trigger or set exact `TEST_MODE=true`.
2. Deploy the schema 3 Apps Script consumer.
3. Prove the old schema 2 producer makes no Gmail call or sent-state write.
4. Publish the schema 3 producer.
5. Complete dry-run acceptance, then explicitly set exact `TEST_MODE=false`.

They also document that missing, misspelled, case-changed, padded, or otherwise non-exact values fail closed.

### GREEN evidence

- Focused rollout/documentation regressions: 3/3 passed.
- Complete `test_workflow_schedule` suite: 43/43 passed.

### Files changed

- `README.md`
- `CLOUD_SETUP.md`
- `apps-script/README.md`
- `docs/superpowers/specs/2026-07-24-opportunity-gated-email-design.md`
- `tests/test_workflow_schedule.py`

### Commit

- `99420d2` `docs: require consumer-first email rollout`

## Integrated Verification

- `node --test tests/apps_script_orchestrator.test.mjs`: 74 passed, 0 failed.
- `python -m unittest tests.test_evidence_health tests.test_report_status tests.test_live_odds tests.test_revalidation tests.test_workflow_schedule -v`: 180 passed, 0 failed.
- `python -m py_compile evidence_health.py decision_bundle.py report_status.py live_odds.py revalidation.py`: passed.
- `git diff --check`: passed; only repository line-ending conversion notices were emitted.

The commands used the exact Python and Node runtimes supplied in the task.

## Self-Review

- Confirmed cutoff mail cannot be triggered by stale dates, legacy schemas, malformed opportunities, or disabled mail mode.
- Confirmed cutoff diagnostics do not fetch or attach `daily-report.png`.
- Confirmed all three Gmail call sites are guarded by the same exact-value delivery mode.
- Confirmed mail state is written only after successful production Gmail calls.
- Confirmed public evidence-health metrics retain publication-time coverage while decision readiness uses immutable bundle context.
- Confirmed an immutable decision context cannot be supplied partially.
- Confirmed missed-window cancellations are terminal, zero-stake, reproducible, and do not claim market evidence that was never fetched.
- Confirmed strict evidence is still required for non-missed T-90/T-30 transitions.
- Searched the affected runtime and tests for the obsolete 45-minute threshold; none remains.
- Reviewed the cumulative diff for unrelated refactors and found none.
- A separate review-agent tool was unavailable in this environment, so the final review was performed manually.

## Concerns and Follow-Up

- No unresolved code concern was found in the requested scope.
- The full 771-test controller suite remains for the controller, as explicitly requested.
- No live Apps Script or Gmail deployment was performed. Production rollout must follow the newly documented consumer-first sequence.
- The audit-report commit SHA is reported by the parent task after this file is committed, avoiding a self-referential commit hash.
