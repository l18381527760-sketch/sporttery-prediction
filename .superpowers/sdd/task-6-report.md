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

## Review Remediation

The Task 6 review findings were fixed in one follow-up pass. This section
supersedes the original legacy-validation concern above.

### Review RED Evidence

Command:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_evidence_health tests.test_model_metrics tests.test_report_status -v
```

Result: exit `1`; 73 tests ran.

```text
FAILED (failures=7, errors=3)
```

The expected failures demonstrated the reviewed gaps: relabelled fixture IDs
could satisfy decision coverage, legacy JSON without strict provenance could
contribute, report status accepted the date alias and bad manifest proof, and
coverage did not retain full fixture bindings.

### Review GREEN Evidence

The same focused command after implementation:

```text
Ran 73 tests
OK
```

The end-to-end adversarial fixture-binding regression was also run directly:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_evidence_health.EvidenceHealthTest.test_relabelled_fixture_ids_do_not_satisfy_decision_coverage -v
```

Result: exit `0`; 1 test passed.

Final requested regression command:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_evidence_health tests.test_model_metrics tests.test_report_status tests.test_report_build_metadata tests.test_capture_odds_snapshot tests.test_import_sporttery tests.test_fixture_identity -v
```

Result: exit `0`.

```text
Ran 122 tests in 6.974s
OK
```

`git diff --check` completed with exit `0`. The import smoke check:

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -c "import legacy_snapshot, model_metrics, evidence_health, report_status, capture_odds_snapshot; print('imports-ok')"
```

completed with exit `0` and output `imports-ok`.

### Review Files

- `legacy_snapshot.py`
- `evidence_health.py`
- `model_metrics.py`
- `report_status.py`
- `tests/test_evidence_health.py`
- `tests/test_model_metrics.py`
- `tests/test_report_status.py`
- `.superpowers/sdd/task-6-report.md`

### Review Contract And Compatibility Checks

- Canonical fixture proof remains a complete
  `(target_date, team_a, team_b, match_id)` binding through coverage and the
  health gate. Repeated captures deduplicate only an identical full binding.
- Decision coverage intersects those bindings exactly. The two-valid-ID
  relabelling regression keeps identity confirmation at `1.0` while producing
  `decision_snapshot_incomplete`.
- Coverage exposes only the JSON-serializable
  `bindings_by_requested_phase` detail needed by evidence health.
- `read_valid_legacy_snapshot` is shared by
  `model_metrics.snapshot_coverage` and
  `report_status._matching_decision_snapshot`. It validates the canonical
  filename/date/time/phase contract, aware Beijing capture time, production
  source, immutable schema-2 manifest and embedded file proof, source/date
  agreement, fixture bindings, and per-match phase shape.
- Missing source/team/manifest proof, injected source, bad filename, bad
  embedded hash, changed manifest input, and invalid per-match phase evidence
  all fail closed. No historical snapshot or import artifact is mutated or
  synthesized.
- Genuine producer timestamps are compared to the filename at its actual
  whole-second resolution, preserving production captures that retain
  microseconds in `captured_at`.
- Chronological ordering compares aware datetimes across offsets. Future
  decision evidence blocks as future, exactly 30 minutes is accepted, more
  than 30 minutes is stale, and naive `now` remains rejected.
- The positional `snapshot_coverage(snapshot_dir, live_snapshot_dir,
  target_date)` signature is unchanged. The existing no-argument,
  one-position, and three-position callers remain valid.
- Report status remains schema `2`, with additive `evidence_health`.
  Forecast readiness reads only forecast blockers; decision and provisional
  readiness read decision blockers. A decision failure does not retroactively
  invalidate a completed forecast.
- The dependency direction is
  `report_status -> evidence_health -> model_metrics -> legacy_snapshot`.
  The shared validator imports only lower-level fixture/import contracts, and
  the import smoke check confirms there is no circular import.

### Review Self-Review And Concerns

- Confirmed no simulation-strategy, Apps Script, email-delivery, or data files
  changed.
- Confirmed malformed or unproven legacy files cannot contribute to metrics or
  report readiness.
- Confirmed strict live v2 validation and genuine phase-less v1 behavior are
  unchanged.
- The checked-in `2026-07-22` manifest hash mismatch noted in the original
  report remains an external data-state concern. Legacy snapshots without the
  required immutable manifest proof now intentionally contribute no readiness
  evidence. No unresolved implementation concerns remain.
