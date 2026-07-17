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
