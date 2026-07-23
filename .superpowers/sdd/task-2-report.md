# Phase 2 Task 2 Report

## Files Changed

- `import_sporttery.py`: added explicit per-market single-play normalization and CSV columns for HAD, HHAD, and TTG eligibility.
- `capture_odds_snapshot.py`: added injectable snapshot inputs, fresh odds-file loading, complete decision markets, per-market eligibility, match IDs, and explicit empty-list snapshots.
- `tests/test_official_market_import.py`: added focused import and fallback-eligibility coverage.
- `tests/test_value_strategy.py`: added focused decision-snapshot coverage while retaining existing value-strategy tests.

## RED Evidence

Command:

```text
.superpowers/sdd/runtime/verify-venv/Scripts/python.exe -m unittest tests.test_official_market_import tests.test_value_strategy -v
```

Result: expected failure, exit code 1. Five new tests failed because `fixtures.csv` lacked `is_single_hhad` and `is_single_ttg`, and `capture()` did not accept injected `matches` or `odds_by_match` inputs. The pre-existing five value-strategy tests passed.

## GREEN Result

The same focused command passed: 10 tests, exit code 0.

Relevant compatibility verification also passed:

- `tests.test_capture_odds_snapshot`: 10 tests, exit code 0.
- `tests.test_collect_market_heat tests.test_import_sporttery tests.test_report_status`: 79 tests, exit code 0 with `OPENBLAS_NUM_THREADS=1`.
- `py_compile` for both production modules and both focused test modules: exit code 0.
- `git diff --check`: exit code 0.

## Commit

Implementation commit: `394e305` (`feat: capture eligible sporttery markets at decision time`).

## Self-Review

- Eligibility is true only for literal `True` or normalized `true`, `1`, and `yes` strings; odds presence never implies eligibility.
- The ZGZCW `dg` marker remains HAD-only, leaving HHAD and TTG false unless explicitly supplied.
- Legacy flat snapshot odds are retained for Phase 1 consumers; the new `markets` and `single_eligibility` fields are additive.
- A started-only match list preserves prior no-artifact behavior, while an explicitly empty official list produces a valid zero-match snapshot.

## Concerns

No code concerns found. The combined broader test invocation initially failed before discovery because OpenBLAS could not allocate its default worker threads; rerunning the affected modules with `OPENBLAS_NUM_THREADS=1` passed all 79 tests.

## Rejected Review Fix

### Files Changed

- `capture_odds_snapshot.py`: parse decision snapshot JSON before treating it as successful, fetch direct Sporttery selling matches before exception-only ZGZCW fallback, and normalize HAD/HHAD/TTG snapshot values to dictionaries.
- `tests/test_capture_odds_snapshot.py`: cover proven and unproven empty snapshot files, direct-market eligibility preservation, exception fallback eligibility, and a legitimate empty direct schedule that must not fall back.
- `tests/test_value_strategy.py`: cover malformed HAD/HHAD/TTG values becoming empty dictionaries in the decision snapshot.

### RED Evidence

After adding the regression coverage, the focused snapshot run failed with four expected assertion failures:

```text
OPENBLAS_NUM_THREADS=1 .superpowers/sdd/runtime/verify-venv/Scripts/python.exe -m unittest tests.test_capture_odds_snapshot tests.test_value_strategy.DecisionSnapshotTest -v
```

- An unproven `{"matches": []}` decision snapshot returned zero.
- The production path did not call `fetch_selling_matches`, so direct HHAD and TTG eligibility could not survive.
- The fallback path likewise did not attempt the direct source first.
- Malformed `hhad` and `ttg` values were written as `[]` and `null` instead of `{}`.

The explicit-empty-direct-list regression was also mutation-checked: temporarily falling back when `fetch_selling_matches` returned `[]` caused the test to fail with `output unexpectedly None`; the exception-only fallback was then restored.

### GREEN Result

- `tests.test_capture_odds_snapshot` plus `tests.test_value_strategy.DecisionSnapshotTest`: 19 tests passed.
- `tests.test_official_market_import tests.test_value_strategy`: 11 tests passed.
- `tests.test_capture_odds_snapshot tests.test_report_status tests.test_import_sporttery tests.test_collect_market_heat`: 94 tests passed.

All test invocations used `OPENBLAS_NUM_THREADS=1`.

### Self-Review

- An explicit zero-match snapshot is still written, but decision success now requires either a parsed non-empty `matches` list or `verified_zero_fixture_day`.
- Direct Sporttery empty lists stay authoritative and do not invoke the fallback; only raised exceptions invoke ZGZCW.
- HHAD and TTG eligibility comes from direct Sporttery rows and ZGZCW remains HAD-only.
- All three required snapshot markets are dictionaries, including malformed source values.

### Commit

Implementation commit: `4043619` (`fix: harden decision snapshot reliability`).

### Concerns

No code concerns found.

## Remaining Review Finding Fix

### Files Changed

- `report_status.py`: require a matching decision snapshot to contain a nonempty `matches` list.
- `tests/test_report_status.py`: add RED coverage for missing, non-list, and empty `matches`; preserve valid producer and verified zero-fixture behavior.
- `.superpowers/sdd/task-2-report.md`: append this evidence.

### RED Evidence

Command:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_report_status.ReportStatusTest.test_matching_decision_snapshot_requires_a_nonempty_matches_list -v
```

Result: exit code 1. One test ran with three expected failures: matching date/phase snapshots with `matches: null`, non-list `matches`, and `matches: []` each incorrectly returned `(True, "2026-07-16T13:30:00+08:00")`.

### GREEN Evidence

Commands and exact results:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_report_status -v
Ran 41 tests in 0.974s
OK
exit code 0

$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_capture_odds_snapshot -v
Ran 15 tests in 0.154s
OK
exit code 0

$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_official_market_import tests.test_value_strategy -v
Ran 11 tests in 0.034s
OK
exit code 0

\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile report_status.py tests/test_report_status.py capture_odds_snapshot.py tests/test_capture_odds_snapshot.py import_sporttery.py tests/test_official_market_import.py tests/test_value_strategy.py
exit code 0

git diff --check
exit code 0
```

The valid producer payload with at least one match remains ready. An officially verified zero-fixture day remains the only empty-day readiness path and keeps `decision_odds_at_bjt` blank.

### Commit

Implementation commit: `de347035d39c0eaa93aec628c5bd5cd321af825c` (`fix: require nonempty decision snapshot matches`).

### Self-Review

- The shared zero-fixture proof is unchanged; it remains the separate readiness fallback in `artifact_state`.
- Malformed or empty snapshots no longer supply a decision timestamp.
- No Phase 1 contracts or unrelated files were changed.

### Concerns

No code concerns found.

## Historical Identity Result Resolution

### Initial Implementation

- Routed fallback fixture identity through `fixture_identity.fixture_match_ids(ROOT, target_date)` and converted immutable values to the resolver's existing mutable-set interface.
- Added fallback provenance coverage for canonical prior-day identity after a mutable `fixtures.csv` overwrite.
- Commit: `ab4ecd3fc0e31a15f348c20139d074ce236fdb38` (`fix: resolve results from historical manifests`).

## Historical Identity Review Fix

### Files Changed

- `update_sporttery_results.py`: loads fixture identity only after the official source fails, fallback returns at least one score-bearing result, and fallback matching is required.
- `tests/test_update_sporttery_results.py`: adds the direct-source no-fixture regression and replaces the mocked historical fixture identity with a schema-2 immutable-manifest integration test built by `import_sporttery.write_import_manifest`.
- `.superpowers/sdd/task-2-report.md`: appends this review-fix evidence.

### RED Evidence

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_update_sporttery_results.ResultProvenanceTest.test_direct_result_does_not_require_fallback_fixture_identity -v
```

```text
ERROR: test_direct_result_does_not_require_fallback_fixture_identity
ValueError: fixture identity source is invalid
Ran 1 test in 0.025s
FAILED (errors=1)
```

The failure proved the direct Sporttery path was incorrectly loading fallback-only fixture identity when no manifest or mutable fixture source existed.

### GREEN Evidence

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_update_sporttery_results.ResultProvenanceTest.test_direct_result_does_not_require_fallback_fixture_identity tests.test_update_sporttery_results.ResultProvenanceTest.test_fallback_uses_historical_manifest_after_current_fixture_overwrite -v
```

```text
Ran 2 tests in 0.065s
OK
exit code 0
```

The manifest integration test creates target-date fixtures and a schema-2 validated manifest with the real `write_import_manifest` contract, overwrites `data/fixtures.csv` with a later date, and mocks only the result providers during `update_results`. It therefore fails if `ROOT` or `target_date` is routed incorrectly.

### Covering Test Evidence

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_update_sporttery_results -v
```

```text
Ran 19 tests in 0.202s
OK
exit code 0
```

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_fixture_identity -v
```

```text
Ran 3 tests in 0.038s
OK
exit code 0
```

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_import_sporttery -v
```

```text
Ran 23 tests in 0.360s
OK
exit code 0
```

All three commands completed with no unittest failures or errors. Their normal
test-fixture source diagnostics were emitted, and every unittest summary ended
with `OK`.

### Self-Review

- Direct, source-identified Sporttery rows now proceed without reading fixtures, manifests, or fallback identity.
- Fallback identity is read only after fallback result rows exist; an invalid manifest or fixture identity source still raises `ValueError` and is not swallowed or guessed.
- `_resolve_fallback_target` is unchanged: source-record identity remains mandatory, multiple canonical IDs remain unavailable, and conflicts retain existing scores.
- The real manifest test uses the target date in both manifest and resolver call, while the only mutable fixture row is on a different date; incorrect `ROOT` or target-date routing cannot resolve `2040580`.

### Concerns

No implementation concerns identified.
