# Task 1 Report: Historical Fixture Identity Reader

## Implementation Summary

Implemented the manifest-backed historical fixture identity reader in `fixture_identity.py`.

- Uses `import_sporttery.import_manifest_path` and `read_valid_import_manifest` when the target-date manifest exists.
- Reads the immutable manifest fixture extract rather than the newer mutable `data/fixtures.csv`.
- Falls back to `data/fixtures.csv` only when the target-date manifest is absent.
- Filters rows to the exact target date and normalizes identity fields with string conversion and trimming.
- Rejects incomplete fixture identities and provider match IDs reused by different fixture keys.
- Returns `dict[tuple[str, str, str], frozenset[str]]` and the `(identified, total)` identity rate tuple.

The corrected plan source uses the authoritative import manifest schema version `2` in both import-manifest examples. No version-1 compatibility or changes to `read_valid_import_manifest` were added.

## Files Changed

- `fixture_identity.py` - new historical fixture identity reader.
- `tests/test_fixture_identity.py` - prescribed historical-manifest and duplicate-provider-ID tests.
- `docs/superpowers/plans/2026-07-22-data-evidence-foundation.md` - preserves the requested schema-version-2 corrections in the committed plan source.

## RED

Command:

```text
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_fixture_identity -v
```

Output:

```text
ImportError: Failed to import test module: test_fixture_identity
ModuleNotFoundError: No module named 'fixture_identity'
FAILED (errors=1)
```

This was expected because the prescribed tests were added before the production module, as required by strict TDD. The test failed for the missing module rather than for a test or environment error.

## GREEN

Focused identity and import-manifest regression command:

```text
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_fixture_identity tests.test_import_sporttery -v
```

Result:

```text
Ran 25 tests in 0.342s
OK
```

The focused run includes both new identity tests and all existing import-manifest tests.

## Full-Suite Results

Python:

```text
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s tests -v
Ran 689 tests in 46.307s
OK
```

Node:

```text
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests/apps_script_orchestrator.test.mjs
ℹ tests 63
ℹ pass 63
ℹ fail 0
ℹ cancelled 0
ℹ skipped 0
```

## Self-Review Findings

- Exact spec compliance: the public functions, key shape, return types, manifest selection, date filtering, validation messages, and identity-rate calculation match the corrected brief.
- Edge cases: missing or malformed fixture sources are converted to `ValueError`; blank identity fields are rejected; duplicate provider IDs are permitted only when they refer to the same fixture key; empty target-day results produce an empty mapping and `(0, 0)`.
- Scope: only the new reader, its prescribed tests, and the two requested plan-source schema corrections changed. Existing public schemas and import-manifest validation remain untouched.
- Test quality: tests exercise immutable historical selection and cross-fixture provider-ID collision using real temporary files and the real manifest validator. The focused suite also confirms existing import-manifest behavior remains green.
- `git diff --check` reported no whitespace errors.

## Concerns

None identified. The initial brief/schema mismatch was resolved by using schema version `2` as clarified and recorded in the plan source.
