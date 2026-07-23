# Phase 1 Final Review Fix Report

## Scope

Resolved every Important and Minor finding from the whole-branch review of
`e97ae07..9698f07` while preserving simulation-only operation, the existing
risk limits, Apps Script sender ownership, and the seven-run/30-day evidence
observation gates.

## Fixes

- Preserved one canonical proven result while recording corroboration as
  versioned structured observations.
- Made decision evidence freshness fixture-scoped and bounded by the Beijing
  as-of time.
- Removed future snapshot evidence from decision-time replay assertions.
- Bounded training results by their Beijing capture timestamp.
- Accepted only manifest-proven zero-fixture days as reconciliation no-ops.
- Refreshed model metrics after settlement and training, before report builds.
- Derived per-match pre-kickoff phases from timing windows and required each
  requested T-90/T-30 batch to contain at least one matching fixture.
- Replaced canonical result writes atomically after flush and fsync.
- Versioned coverage counting semantics while retaining legacy observation
  counts and publishing unique-binding counts additively.
- Restored the public positional argument order for live odds capture and
  removed the unused fixture-identity test import.

## Additional Full-Suite Regression Fix

The first full-suite run exposed a shared phase-validation regression that the
focused review tests did not cover: a valid multi-fixture T-90/T-30 batch was
rejected when another fixture in the same batch was outside that requested
window. The batch request proof and per-fixture derived phase are now separate.
Missing decision evidence also retains the existing incomplete-plus-stale
blocker contract.

## Verification

- Focused review regressions: 166 passed.
- Affected live odds, revalidation, ledger, evidence health, and report status:
  208 passed.
- Full Python suite: 757 passed in 47.164 seconds.
- Apps Script Node suite: 63 passed, 0 failed.
- `git diff --check e97ae07ee6aa16de7cef6e1d90ac6d4f50ff100b`: clean.
