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

## Follow-Up Finding 5: Fractional-Time Phase Boundary

### Root cause

`revalidation.due_stage` compared the exact seconds remaining before kickoff,
while `live_odds._minutes_to_kickoff` floored the same duration before assigning
the per-match snapshot phase. At 40:01 through 40:59, the scheduler therefore
requested T-90 while publication labeled the fixture T-30. The same
representation also labeled 105:01 through 105:59 as T-90 instead of too early.

### RED evidence

The first focused command was:

```powershell
& 'C:\Users\87562\AppData\Local\Temp\sporttery-test-venv\Scripts\python.exe' -m unittest tests.test_live_odds.LiveOddsTest.test_snapshot_labels_exact_fractional_phase_boundaries tests.test_live_odds.LiveOddsTest.test_fractional_t90_publication_uses_correct_single_and_mixed_fixture tests.test_revalidation.RevalidationTest.test_due_stage_uses_exact_fractional_phase_boundaries tests.test_revalidation.RevalidationTest.test_scheduler_binds_fractional_t90_evidence_in_single_and_mixed_batches -v
```

It ran four test methods and failed as expected:

- the direct snapshot assertion observed integer `40` instead of `41` at
  40:59;
- single-fixture and mixed-batch T-90 publication raised
  `requested pre-kickoff phase is outside its timing window`;
- both end-to-end scheduler cases failed at the same publication boundary;
- the scheduler-only exact-time matrix passed, isolating the defect to snapshot
  representation.

The ceiling conversion exposed a possible regression in the helper's existing
negative-time guard. This additional focused RED command:

```powershell
& 'C:\Users\87562\AppData\Local\Temp\sporttery-test-venv\Scripts\python.exe' -m unittest tests.test_live_odds.LiveOddsTest.test_minutes_to_kickoff_still_rejects_fractional_past_time -v
```

ran one test and failed because `ceil(-1 second / 60)` returned zero instead of
raising. The implementation was then tightened to reject negative exact seconds
before applying the ceiling.

### Implementation

- Keep the scheduler's existing exact-time comparisons unchanged.
- Publish `minutes_to_kickoff` as the ceiling of positive exact seconds divided
  by 60.
- Preserve exact integer boundaries:
  - more than 105:00 is too early;
  - 105:00 through strictly more than 40:00 is T-90;
  - 40:00 through strictly more than 10:00 is T-30;
  - scheduler missed-window behavior remains at `<=40:00` for provisional
    candidates and `<=10:00` for screened candidates.
- Preserve rejection of negative fractional kickoff time.
- Exercise actual publication and revalidation with one fixture and a mixed
  T-90/T-30 batch, asserting that the candidate binds its own correctly phased
  row.

### GREEN evidence

The final focused command covered all five regression methods and passed 5/5.

The required affected-suite command:

```powershell
& 'C:\Users\87562\AppData\Local\Temp\sporttery-test-venv\Scripts\python.exe' -m unittest tests.test_live_odds tests.test_revalidation -v
```

passed 58 tests with zero failures or errors.

The final compile command:

```powershell
& 'C:\Users\87562\AppData\Local\Temp\sporttery-test-venv\Scripts\python.exe' -m py_compile live_odds.py revalidation.py tests/test_live_odds.py tests/test_revalidation.py
```

passed, and `git diff --check` passed with only the repository's existing
LF-to-CRLF conversion notices.

### Files changed

- `live_odds.py`
- `tests/test_live_odds.py`
- `tests/test_revalidation.py`
- `.superpowers/sdd/email-final-fix-report.md`

### Commit

- Parent SHA: `e440d38d815bfde76708dbfc9a929ca6e23a500a`.
- Fractional-boundary fix SHA: this report's containing logical fix commit; its
  exact SHA is returned by the parent task because a commit cannot embed its own
  content-derived hash.

### Follow-up self-review

- `revalidation.py` required no runtime change; its exact fractional comparisons
  already matched the intended windows.
- Existing integer-boundary tests remain unchanged and passing.
- Snapshot construction and validation both use the same ceiling
  representation.
- Requested-phase publication still requires at least one row in that exact
  phase, while candidate evaluation binds the complete fixture identity.
- No shared helper module or unrelated refactor was introduced.

## Follow-Up Finding 6: Legacy Model-Metrics Fixture

### RED evidence

After the approved T-90/T-30 boundary change, the unchanged controller test was
run first:

```powershell
& 'C:\Users\87562\AppData\Local\Temp\sporttery-test-venv\Scripts\python.exe' -m unittest tests.test_model_metrics.ModelMetricsTest.test_counts_nested_live_pre_kickoff_phases_once_per_match -v
```

It failed deterministically:

```text
AssertionError: 1 != 2
Ran 1 test
FAILED (failures=1)
```

The fixture used 16:45 and 17:15 captures for an 18:00 kickoff. Both are now
correctly T-90 because both are strictly more than 40 minutes before kickoff.

### Fixture correction

Only `tests/test_model_metrics.py` changed. The second capture moved from 17:15
to 17:25, placing it 35 minutes before kickoff in the approved T-30 window.
Assertions remain unchanged: the test still requires two files, two match
observations, one unique fixture binding, one T-90 phase, one T-30 phase, and
one requested decision-phase binding.

### GREEN evidence

The focused test passed after the fixture correction:

```powershell
& 'C:\Users\87562\AppData\Local\Temp\sporttery-test-venv\Scripts\python.exe' -m unittest tests.test_model_metrics.ModelMetricsTest.test_counts_nested_live_pre_kickoff_phases_once_per_match -v
```

```text
Ran 1 test
OK
```

The complete affected command was:

```powershell
& 'C:\Users\87562\AppData\Local\Temp\sporttery-test-venv\Scripts\python.exe' -m unittest tests.test_model_metrics tests.test_live_odds tests.test_revalidation -v
```

It passed 67 tests with zero failures or errors. `git diff --check` also passed
with only the repository's existing LF-to-CRLF conversion notice.

### Scope and commit

- Test fixture: `tests/test_model_metrics.py`
- Required evidence report:
  `.superpowers/sdd/email-final-fix-report.md`
- Parent SHA: `40c3b554df2be4bf947e70c7ea8ebaa275658ffb`
- The focused correction commit SHA is returned by the parent task after this
  report is committed.
