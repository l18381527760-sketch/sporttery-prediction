# Task 5 Report

## RED

Before implementation, ran:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v
```

The new ledger suite failed with `ModuleNotFoundError: No module named 'betting_ledger'`. The new result-provenance tests also failed because direct rows lacked `match_id`, the fallback parser lacked `source_record_id`, old CSV columns were discarded, and conflicting scores overwrote a finished score. This is the expected RED baseline.

## GREEN

Implemented `betting_ledger.py` with canonical SHA-256 identities, first-row-wins ingestion, deterministic legacy migration, strict two-leg settlement, correction-only abnormal reopening, and atomic UTF-8-SIG CSV replacement. Updated `update_sporttery_results.py` to preserve CSV schema history and write canonical match/result provenance without guessing unresolved or conflicting results.

Verification before the feature commit:

```text
tests.test_betting_ledger tests.test_update_sporttery_results: 14 tests passed
tests.test_value_portfolio tests.test_report_status: 69 tests passed
py_compile: passed
git diff --check: passed
```

## Self-review

- Locked plan fields are copied only once; existing canonical IDs retain their original odds, probability, stake, and metadata.
- Scores settle only when a matching canonical `match_id` has explicit finished/refunded status and complete provenance.
- HAD, integer HHAD, all TTG buckets, and both-leg parlay/refund paths use decimal money serialized to two places.
- Repeating settlement preserves terminal rows unchanged, and atomic writing has deterministic field ordering and bytes.
- Result migrations retain old CSV columns and rows; a score disagreement remains a conflict with both source identities recorded.

## Concerns

- This task provides the ledger primitives and result schema only. The later plan-integration task must route valid plan locks through ledger ingestion and settlement commands.
- Existing legacy readers still key historical results by date/team. They are intentionally preserved until the planned Phase 3 migration.

## Commit

Feature commit: `04599c6` (`feat: add immutable idempotent betting ledger`).

## Review fixes

### Scope

- Kept conflict rows unavailable across repeated captures, preserved the first score, and merged `|`-delimited provenance as sorted unique tokens.
- Cleared all plan-supplied settlement state on new locked rows, stored authoritative normalized lock source and `plan_sha256`, and rejected mismatched row sources.
- Made correction mode reopening-only and idempotent, including canonical offending-leg tracking for abnormal parlays.
- Required parseable timezone-aware result capture timestamps, reserved `legacy_match:` for the private migration path, and retained full Decimal odds precision until money serialization.
- Replaced date/team result-row collapsing with ordered rows plus deterministic row selection so duplicate legacy rows and unknown columns survive.

### RED chronology and exact output

An initial test invocation stopped at import with `SyntaxError: invalid syntax` on a test fixture using `return` as a keyword. It was not counted as RED. After correcting only that test syntax, the required focused command produced the valid RED run below:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v

test_atomic_writer_is_deterministic_utf8_sig_and_preserves_unknown_fields ... FAIL
test_malformed_identity_fails_closed (match_id='legacy_match:forbidden') ... FAIL
test_malformed_identity_fails_closed (parlay leg match_id='legacy_match:forbidden') ... FAIL
test_new_locked_row_clears_plan_settlement_fields_and_uses_authoritative_lock_metadata ... FAIL
test_abnormal_parlay_reopens_by_offending_leg_then_requires_ordinary_settlement ... ERROR
test_correction_mode_never_settles_pending_rows ... FAIL
test_locked_odds_keep_full_decimal_precision_until_money_is_quantized ... FAIL
test_unproven_results_do_not_mutate_pending_and_correction_is_explicit (captured_at_bjt='not-a-timestamp') ... FAIL
test_unproven_results_do_not_mutate_pending_and_correction_is_explicit (captured_at_bjt='2026-07-17T11:00:00') ... FAIL
test_unproven_results_do_not_mutate_pending_and_correction_is_explicit ... FAIL
test_conflict_survives_repeated_and_later_captures_idempotently ... FAIL
test_duplicate_legacy_rows_and_unknown_columns_survive_migration_in_order ... FAIL

----------------------------------------------------------------------
Ran 20 tests in 0.134s

FAILED (failures=11, errors=1)
```

### GREEN verification

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v
Ran 20 tests in 0.064s
OK

$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_value_portfolio tests.test_report_status -v
Ran 69 tests in 1.161s
OK

.\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile betting_ledger.py update_sporttery_results.py tests\test_betting_ledger.py tests\test_update_sporttery_results.py
Exit code: 0

git diff --check
Exit code: 0
```

Fix commit: `ed5950f0932f47fbf689ef15faa31f0dd25a5942` (`fix: harden betting ledger review invariants`).

Immediately after the fix commit, `git status --short` produced no output (clean worktree).

## Review fixes 2

### Files changed

- `update_sporttery_results.py` - canonical direct/fallback resolution, protected-score migration, and observation idempotency.
- `tests/test_update_sporttery_results.py` - focused canonical-resolution, duplicate-ID, protected-history, and byte-idempotency coverage.
- `betting_ledger.py` - Task 5 owned implementation file; unchanged in this review pass.
- `tests/test_betting_ledger.py` - Task 5 owned ledger test file; unchanged in this review pass.
- `.superpowers/sdd/task-5-report.md` - RED/GREEN chronology, commits, compatibility notes, and file inventory.

### RED

After adding the final focused test contract and before changing production code, ran:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v

test_ambiguous_fallback_ids_append_unavailable_without_touching_identified_rows ... FAIL
test_changed_score_same_record_conflicts_once_and_repeat_is_byte_idempotent ... FAIL
test_direct_mismatched_match_id_appends_without_touching_identified_row ... FAIL
test_duplicate_legacy_rows_and_unknown_columns_survive_migration_in_order ... FAIL
test_multiple_blank_fallback_candidates_are_ambiguous_and_remain_untouched ... FAIL
test_preexisting_score_without_status_is_protected_from_changed_capture ... FAIL
test_same_finished_observation_with_new_capture_time_is_byte_idempotent ... FAIL

----------------------------------------------------------------------
Ran 28 tests in 0.152s

FAILED (failures=7)
```

The failures showed the prior implementation selecting the first date/team row despite a mismatched canonical ID, collapsing fixture identities to one value, overwriting status-less historical scores, and extending timestamps for an already observed source record.

### GREEN

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v
Ran 28 tests in 0.195s
OK

$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_value_portfolio tests.test_report_status -v
Ran 69 tests in 1.388s
OK

.\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile betting_ledger.py update_sporttery_results.py tests\test_betting_ledger.py tests\test_update_sporttery_results.py
Exit code: 0

git diff --check
Exit code: 0
```

### Compatibility and fail-closed review

- Existing CSV rows retain input order and unknown columns; no legacy row is collapsed or deleted.
- Direct results update only an exact canonical ID or a sole blank date/team row. Other cases append a canonical row.
- Fallback resolution uses the set union of every exact date/team existing and fixture ID. Zero/ambiguous unions write only a blank-ID `unavailable` row and never alter identified history.
- Every parseable pre-existing score is protected even without `result_status`.
- Re-observing the same source/record does not extend timestamps; a new source record extends sorted provenance once.

Fix commit: `d9b95a5bfb92d78e764e5831ec4810ba8ee0a3f3` (`fix: make result migration canonical and idempotent`).

Immediately after the code/test fix commit, `git status --short` produced no output (clean worktree).

## Review fixes 3

### Scope

- Reused an existing blank-ID `unavailable` row when it already contains the same fallback `result_source` and `source_record_id` observation.
- Kept ambiguous canonical resolution unavailable and left every identified historical row unchanged.
- Made a blank or malformed fallback `source_record_id` fail closed as `unavailable`, even when fixture history proves one canonical match ID.

### Files changed

- `update_sporttery_results.py` - unavailable-observation reuse and missing-source fail-closed resolution.
- `tests/test_update_sporttery_results.py` - repeated ambiguous fallback byte-idempotency and missing-source coverage.
- `.superpowers/sdd/task-5-report.md` - this chronology and verification record.

### RED

After adding the two focused tests and before changing production code, ran:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v

test_fallback_without_source_record_id_is_unavailable_despite_unique_match_id ... FAIL
test_repeated_ambiguous_fallback_reuses_the_same_unavailable_observation ... FAIL

======================================================================
FAIL: test_fallback_without_source_record_id_is_unavailable_despite_unique_match_id
----------------------------------------------------------------------
AssertionError: 'unavailable' != 'finished'
- unavailable
+ finished

======================================================================
FAIL: test_repeated_ambiguous_fallback_reuses_the_same_unavailable_observation
----------------------------------------------------------------------
AssertionError: 4 != 5

----------------------------------------------------------------------
Ran 30 tests in 0.210s

FAILED (failures=2)
```

The first failure exposed a settleable status without a source record ID. The second showed that the repeated observation appended a fifth row instead of reusing the fourth row byte-for-byte.

### GREEN

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v
Ran 30 tests in 0.189s
OK

.\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile update_sporttery_results.py tests\test_update_sporttery_results.py
Exit code: 0

git diff --check
Exit code: 0
```

### Review

- Observation reuse searches only blank `match_id` candidates and therefore cannot rewrite identified historical rows.
- Reused ambiguous rows remain `unavailable`, retain their first capture timestamp, and produce identical bytes on a later same-source/same-record observation.
- Missing fallback source identity may preserve a uniquely proven canonical match ID for legacy lookup, but its `unavailable` status keeps it outside ledger settlement.
- Ordered legacy rows, duplicate candidates, and unknown CSV columns remain preserved by the existing collection and writer paths.

Code/test fix commit: `6ecaf08e24f1642e28e2b9ad7d0b13b9f5928166` (`fix: deduplicate unavailable fallback results`).

Immediately after the code/test fix commit, `git status --short` produced no output (clean worktree).
