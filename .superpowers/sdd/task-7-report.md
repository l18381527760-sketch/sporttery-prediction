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
