# Task 7 Report: Workflow Integration and End-to-End Evidence Replay

## Status

Implemented from base commit
`57955379dd37839b8f448f2b410faa259c5b8858`.

Task 7 integrates the reviewed evidence contracts into fail-fast production
workflow order and adds a network-free replay using real temporary schema-2
manifests, the real result reconciler, the real strict-v2 live capture and
validator, and the real evidence-health gate. The controller's tracked plan
correction requiring strict live schema v2 is included.

## RED Evidence

Initial focused command:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_workflow_schedule tests.test_evidence_pipeline_replay -v
```

Result: exit `1`; 38 tests ran with 4 expected failures. Each failure showed
that a required daily, status-publication, monitoring-capture, or settlement
shell body lacked the explicit `set -euo pipefail` guard. The strict-v2 replay
already passed against the reviewed contracts.

Runtime fail-fast proof before the workflow fix:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_workflow_schedule.WorkflowScheduleTest.test_settlement_failure_stops_before_history_settlement_and_training -v
```

Result: exit `1`; the settlement body returned `0` instead of the injected
reconciliation failure code `41` and continued as far as Git. This proved
that downstream history, settlement, training, and reporting were not
explicitly protected by the workflow body itself.

The focused documentation test also failed for both `README.md` and
`CLOUD_SETUP.md` until the evidence-first order, strict-v2 filenames, retry
idempotency, and sole-sender contract were documented.

## GREEN Evidence

Focused workflow and replay command after the minimal integration:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_workflow_schedule tests.test_evidence_pipeline_replay -v
```

Result: exit `0`; 40 tests passed in 5.753 seconds.

The runtime settlement test now returns the injected code `41` and records
only `update_sporttery_results.py`; no history, ledger settlement, training,
metrics/reporting, or publication command runs afterward.

## Full Evidence

Complete Python suite:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

Result: exit `0`; 740 tests passed in 50.087 seconds.

Exact Node suite:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests/apps_script_orchestrator.test.mjs
```

Result: exit `0`; 63 tests passed in 179.363 milliseconds.

`git diff --check` completed with exit `0`.

## Workflow Ordering

- Daily forecast: immutable import, historical features, prediction,
  site/image build, evidence-gated schema-2 status, changed-only commit, then
  Pages publication. The health contract is evaluated before
  `forecast_ready` by `report_status`.
- Monitoring capture:
  `capture_odds_snapshot.py --date "$TARGET_DATE" --phase monitoring --live`,
  then immutable snapshot commit.
- Decision capture remains explicit `--phase decision --live`; T-90 and T-30
  remain explicit live phases in the reviewed revalidation path.
- Settlement: seven-day oldest-first reconciliation, proven historical
  features, confirmed-row settlement, shadow draw training, report rebuild,
  schema-2 status, changed-only commit, then Pages publication.
- Required Task 7 shell bodies start with `set -euo pipefail`; optional market
  and draw-alert steps retain their reviewed isolation.

## Replay Evidence

- Seven immutable schema-2 import manifests include fixtures, odds, ratings,
  byte sizes, SHA-256 hashes, and exact `+08:00` import provenance.
- `data/fixtures.csv` contains only the following business day while the
  previous seven days resolve through immutable manifests.
- The real fallback result path promotes a source-identified score to the
  manifest match ID. A fallback score without source-record identity remains
  `unavailable`.
- Running the real seven-day reconciliation twice produces identical
  `bet_results.csv` bytes.
- `capture_live_snapshot` receives only injected source fetch functions. It
  writes canonical immutable strict-v2 decision, T-90, and T-30 files.
- `read_valid_live_snapshot` validates every replay file; no filename or live
  payload is hand-written.
- Evidence health reports identity and result provenance rates of `1.0`,
  counts all three requested phases separately, and reports no hard blocker.
- All replay data lives under `TemporaryDirectory`; no network call is made.

## Files

- `.github/workflows/daily-forecast.yml`
- `.github/workflows/odds-snapshot.yml`
- `.github/workflows/noon-settlement.yml`
- `tests/test_workflow_schedule.py`
- `tests/test_evidence_pipeline_replay.py`
- `README.md`
- `CLOUD_SETUP.md`
- `docs/superpowers/plans/2026-07-22-data-evidence-foundation.md`
- `.superpowers/sdd/task-7-report.md`

## Compatibility Checks

- Reconciliation remains bounded to `1..30`; production uses `7`, iterates
  oldest-first, and stops at the first failure.
- Shared per-row and batch result proof, exact Beijing provenance, conflict
  exclusion, refund economics, and training eligibility are unchanged and
  covered by the full Python suite.
- Historical schema-2 identity manifests remain immutable and fully bound.
- New live captures remain strict schema 2 with canonical immutable
  filenames. Legacy v1 remains readable but cannot synthesize phase proof.
- Status remains schema 2. Apps Script remains the sole sender and retains
  the Beijing 14:00-18:00 normal/failure behavior.
- GitHub Actions retries are safe through immutable/idempotent result,
  snapshot, generation, ledger, and mail-state contracts.
- `pre_kickoff_revalidation.mode` and value-v4 activation remain `shadow`;
  simulation mode remains active and `real_money_automation` remains `false`.
- No strategy, stake, real-money, Apps Script, sender, or data file changed.

## Self-Review

- Re-read the Task 7 brief and Project 1 acceptance gate against the final
  diff and test names.
- Confirmed reconciliation precedes every history, settlement, training, and
  reporting/publication command in the settlement body.
- Confirmed explicit live phase arguments, canonical strict-v2 replay paths,
  retry byte idempotency, and anonymous fallback rejection.
- Confirmed all required publication paths fail before commit/deploy and no
  required step uses `continue-on-error` or `|| true`.
- Confirmed the changed-file scope contains no strategy configuration,
  settlement economics, Apps Script, email workflow, or production Python.

## Concerns

- The checked-in `2026-07-22` import manifest still fails the real validator
  because `data/import_extracts/2026-07-22/odds.json` has a hash mismatch.
  This is inherited repository data state; the correct behavior remains
  fail-closed and Task 7 does not mutate or bypass it.
- Deployment observation is still required: observe seven daily production
  runs before Project 2 planning. The broader 30-day operational acceptance
  remains mandatory before any model or profitability claim.

---

## Review Remediation: Four Important Findings

### RED Evidence

The review-focused RED command was:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_workflow_schedule.WorkflowScheduleTest.test_live_snapshot_workflow_stages_the_actual_immutable_destination tests.test_workflow_schedule.DeploymentDocumentationTest.test_operator_docs_gate_project_two_and_claims_on_observed_maturity tests.test_revalidation.RevalidationTest.test_new_transition_requires_strict_v2_phase_evidence tests.test_revalidation.RevalidationTest.test_scheduler_requests_explicit_phase_for_each_due_transition tests.test_evidence_pipeline_replay -v
```

Result: exit `1`; 6 tests ran with 4 failures and 1 error. The failures proved
that the monitoring workflow staged `data/odds_snapshots`, both operator docs
lacked the two maturity gates, v1 could create a new transition, and the
snapshot provider did not receive the required keyword-only `phase`. The
expanded replay scenarios were already green against production contracts.

The first complete Python run after the production fix ran 745 tests and
reported 34 errors. Every error was the same compatibility fixture issue:
shared ledger and cross-midnight rehearsal providers still implemented the
old three-argument callback. No product assertion failed. Those fixtures were
updated to accept `phase` and write the corresponding canonical v2 capture.
Their focused 91-test suite then passed.

### GREEN Evidence

Required focused gate:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_workflow_schedule tests.test_evidence_pipeline_replay tests.test_revalidation tests.test_live_odds tests.test_report_status -v
```

Result: exit `0`; 149 tests passed in 13.914 seconds.

Complete Python gate:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s tests -v
```

Result: exit `0`; 745 tests passed in 47.398 seconds.

Exact Apps Script gate:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests/apps_script_orchestrator.test.mjs
```

Result: exit `0`; 63 tests passed, 0 failed, in 174.847 milliseconds.

Diff validation:

```powershell
git diff --check
```

Result: exit `0`; no whitespace errors. Git printed only the existing Windows
working-tree LF-to-CRLF conversion warnings.

### Workflow Staging And Ordering

- `odds-snapshot.yml` now stages `data/live_odds_snapshots`, the actual
  destination of `capture_odds_snapshot.py --phase monitoring --live`.
- The existing `stefanzweifel/git-auto-commit-action@v5` changed-only
  behavior is preserved; no direct or allow-empty commit was added.
- No legacy odds path remains in the monitoring commit pattern because that
  workflow does not produce one.
- Existing fail-fast order is unchanged: required evidence generation
  precedes status, changed-only commit, and publication.

### Phase-Explicit Strict V2 Enforcement

- Each newly due T-90 stage calls the provider with
  `phase="pre_kickoff_90"`; each newly due T-30 stage uses
  `phase="pre_kickoff_30"`.
- A run with both stages captures and validates one immutable snapshot per
  requested stage instead of sharing a default monitoring snapshot.
- New screening and confirmation paths require schema 2, the matching
  requested phase, target date, and validator-compatible per-match phase and
  minute evidence before any receipt is created.
- Schema 1 is accepted only while reproducing an already-existing immutable
  receipt. It cannot enter the new-evaluation path, create a receipt, or
  synthesize phase proof.
- Receipt replay remains idempotent and does not refetch after a state-write
  or ledger-write interruption. Provider injection remains explicit and
  testable through the keyword phase contract.

### End-To-End Replay Acceptance

The network-free replay now proves all requested cases using temporary
artifacts and production readers:

- seven-day reconciliation is oldest-first and byte-identical on rerun;
- fallback without `source_record_id` remains `unavailable`;
- score-conflicting and provenance-ambiguous duplicate match IDs are absent
  from `generate_betting_plan.load_results()` settlement ingress;
- those duplicate IDs, plus the unavailable fallback, produce no draw
  training sample even when valid immutable feature snapshots exist;
- the sample set is empty until the fully proven manifest/result/snapshot
  fixture is added, then contains exactly that match;
- a real `identity_not_unique` health blocker makes actual
  `publish_status(..., phase="forecast")` readiness false while every ordinary
  forecast artifact gate remains true;
- real strict-v2 decision, T-90, and T-30 captures are validated and counted
  separately.

Only source fetch functions are injected for live capture. Result
reconciliation, batch conflict exclusion, settlement ingress, draw-snapshot
validation, training selection, evidence health, and report readiness all use
production contracts.

### Files Changed In Review Remediation

- `.github/workflows/odds-snapshot.yml`
- `revalidation.py`
- `tests/test_workflow_schedule.py`
- `tests/test_revalidation.py`
- `tests/test_evidence_pipeline_replay.py`
- `tests/test_betting_ledger.py`
- `tests/test_pre_kickoff_rehearsal.py`
- `README.md`
- `CLOUD_SETUP.md`
- `.superpowers/sdd/task-7-report.md`

### Compatibility And Self-Review

- Verified workflow command ordering, explicit phase arguments, fail-fast
  behavior, durable live-tree staging, and changed-only commit semantics.
- Verified v1 remains readable only through existing receipt replay and that
  v2 retries preserve receipt and ledger idempotency.
- Verified status remains schema 2 and Apps Script remains the sole email
  sender; no Apps Script or email workflow file changed.
- Verified no production strategy, stake, settlement economics, configuration
  value, data artifact, or real-money surface changed.
- `value_strategy.activation_mode` and pre-kickoff revalidation remain shadow;
  `real_money_automation` remains `false`.
- README and CLOUD_SETUP now require seven successful daily production runs
  before Project 2 planning and broader 30-day evidence maturity before model
  or profitability claims.

### Remaining Concerns

- No new code concern remains after the focused, complete Python, and exact
  Node gates.
- The operational acceptance gates are intentionally still pending real
  observation: seven successful production days for Project 2 planning and
  30-day evidence maturity for model or profitability claims.
