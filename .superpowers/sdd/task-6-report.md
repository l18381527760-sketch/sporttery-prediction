# Task 6 Report: Unified Snapshot Coverage and Evidence Health

## Status

Implemented from base commit `e25be1c15be19aff56633c56a137063fa49883fd`.

The change adds the read-only evidence health contract, unifies legacy and
live snapshot coverage, and separates forecast blockers from
decision/provisional blockers. Simulation strategy and email delivery were
not changed.

## RED Evidence

Command:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_evidence_health tests.test_model_metrics tests.test_report_status -v
```

Result: exit `1`; 63 tests ran with 9 expected interface errors.

- `evidence_health` did not exist.
- `snapshot_coverage` rejected the new three-position call.
- `report_status` did not expose `build_evidence_health`.
- Published status did not contain `evidence_health`.

## GREEN Evidence

Focused health/metrics/status command:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_evidence_health tests.test_model_metrics tests.test_report_status -v
```

Result: exit `0`; 66 tests passed.

Prescribed metadata regression command:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_evidence_health tests.test_model_metrics tests.test_report_status tests.test_report_build_metadata -v
```

Result: exit `0`; 74 tests passed.

## Full Evidence

Complete Python suite, run once:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s tests -p "test_*.py"
```

Result: exit `0`; 728 tests passed in 49.338 seconds.

Exact Node suite, run once:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests/apps_script_orchestrator.test.mjs
```

Result: exit `0`; 63 tests passed.

`git diff --check` also completed with exit `0`.

## Files

- `evidence_health.py`
- `model_metrics.py`
- `report_status.py`
- `tests/test_evidence_health.py`
- `tests/test_model_metrics.py`
- `tests/test_report_status.py`
- `.superpowers/sdd/task-6-report.md`

## Contract And Compatibility Checks

- `snapshot_coverage` now has the requested positional signature:
  `snapshot_dir`, `live_snapshot_dir`, `target_date`.
- The existing one-argument positional test caller and the no-argument
  `write_metrics` caller remain valid.
- Live snapshots use `read_valid_live_snapshot`; canonical/path/schema/phase
  failures are skipped.
- Genuine live schema v1 snapshots contribute file/match presence but never
  synthesized requested or per-match phase evidence.
- Phase and requested-phase coverage counts unique `match_id` values.
  Repeated captures cannot inflate fixture coverage.
- Conflicting snapshot identity labels are excluded from proof.
- Result rates use `resolve_result_batch`; conflicting result rows remain in
  the denominator identity set but cannot count as proven.
- Decision coverage IDs are intersected with uniquely identified fixtures, so
  unrelated match IDs cannot satisfy completeness.
- Report status remains schema `2`; `evidence_health` is optional/additive.
- Forecast readiness reads only `forecast_blockers`.
- Decision snapshot and provisional readiness read `decision_blockers`.
  A later decision blocker does not clear a completed forecast.
- A zero-fixture day is healthy only when
  `verified_zero_fixture_day` supplies proof.

## Self-Review

- Re-read the Task 6 brief and checked each blocker, rate, timestamp, v1/v2,
  conflict, duplicate, schema, and readiness requirement against code/tests.
- Confirmed no changes under simulation strategy, Apps Script, or email
  delivery files.
- Confirmed the worktree remained based at the requested base commit before
  this Task 6 commit.
- Confirmed malformed JSON fails closed as incomplete/stale decision evidence.
- Confirmed report metadata and exact Node consumers tolerate the additive
  status field.

## Concerns

- Legacy static snapshots have no canonical schema validator, so they receive
  conservative structural validation; strict canonical validation is applied
  to all live v1/v2 snapshots through the existing public reader.
- The checked-in `2026-07-22` import manifest currently has an odds-extract
  hash mismatch at `data/import_extracts/2026-07-22/odds.json`. A smoke health
  read therefore reports `identity_not_unique`, which is the intended
  fail-closed behavior for the current data state.
