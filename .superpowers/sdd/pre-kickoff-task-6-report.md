# Pre-Kickoff Task 6 Report

## RED

- Added workflow contract tests for prediction-only daily forecast, live decision capture and provisional generation, revalidation scheduling, observation-only monitoring, confirmed-row settlement, manual-only email diagnostics, and machine-readable CLI output.
- `python -m unittest tests.test_workflow_schedule.WorkflowScheduleTest -v` failed as expected before implementation: the revalidation workflow and CLI commands were absent, and existing workflows still used plan locks, paid ingestion, and scheduled email.

## GREEN

- `daily-forecast.yml` now imports, builds features, predicts, and publishes only the base forecast.
- `draw-alert-refresh.yml` captures a live decision snapshot, records it in the decision bundle, and creates an immutable provisional generation without a plan lock or paid-ledger ingest.
- `pre-kickoff-revalidation.yml` runs due work every ten minutes, accepts manual `target_date` and `now_bjt`, reads `changed_dates` JSON, and rebuilds, commits, and deploys only changed output.
- `odds-snapshot.yml` captures live monitoring observations only. `email-report.yml` is dispatch-only and guarded by `ALLOW_MANUAL_EMAIL_DIAGNOSTIC == "true"`.
- Added minimal CLIs: `provisional_plan.py --date --generated-at`, `revalidation.py run-due --target-date --now-bjt`, and `revalidation_reporting.py rebuild-index --now-bjt`.

## Cron Mapping

| Workflow | UTC cron | Beijing time |
| --- | --- | --- |
| Daily forecast | `15 4 * * *` | 12:15 |
| Provisional decision refresh | `30 5 * * *` | 13:30 |
| Settlement | `45 5 * * *`, `5 6 * * *` | 13:45, 14:05 |
| Live monitoring snapshot | `*/30 * * * *` | Every 30 minutes |
| Pre-kickoff revalidation | `*/10 * * * *` | Every 10 minutes |

## CLI Commands

```bash
python provisional_plan.py --date "$TARGET_DATE" --generated-at "$PROVISIONAL_AT_BJT"
python revalidation.py run-due --target-date "$TARGET_DATE" --now-bjt "$NOW_BJT"
python revalidation_reporting.py rebuild-index --now-bjt "$NOW_BJT"
```

## Validation

- RED workflow contract run observed expected missing-workflow and old-command failures.
- Focused workflow suite: `python -m unittest tests.test_workflow_schedule -v` passed.
- Every multiline workflow `run` body passed a separate `bash -n` sweep using `C:\Program Files\Git\bin\bash.exe`.
- Full discovery: `C:\Users\87562\AppData\Local\Python\bin\python.exe -m unittest discover -s tests -v` passed, 660 tests.

## Commit

- SHA: `ecaff13`

## Review Remediation

### RED Evidence

- Added pointer-selected provisional builder tests, exact PNG metadata checks,
  strict CLI date tests, malformed changed-JSON execution tests, and a two-date
  cross-midnight workflow rehearsal before production changes.
- Focused RED command:
  `.superpowers/sdd/runtime/verify-venv/Scripts/python.exe -m unittest` with
  the 10 new regression methods. Result: 10 methods ran with 13 failing
  subtests and 6 errors. Missing `report_stage`/`report_date` builder APIs,
  absent metadata verification, compact dates accepted by `revalidation.py`,
  no `display_date` output, and two global builder/status calls reproduced the
  independent review findings. The live snapshot-path execution rehearsal
  passed because that existing command chain was already correct.

### GREEN Evidence

- `build_site.py` and `build_daily_image.py` now accept exact `--date` and
  `--stage`, retain zero-argument operation, and use only
  `read_valid_provisional_state(root, report_date)` for provisional rows.
- Main PNGs bind `report_date`, `report_stage`, and `build_id`.
  `report_status.py` now requires exact matching PNG metadata before image
  readiness and hashes only the verified image.
- `provisional_plan.py`, `revalidation.py`, both builders, and
  `report_status.py` reject noncanonical dates at argparse exit 2.
- Focused builder/status sweep: 109 tests passed. Collected workflow suite:
  33 tests passed. Fresh full discovery: 675 tests passed. Remaining failures:
  0.

### Test Migration

- Removed `LegacyWorkflowScheduleContract` and its obsolete lock/paid-plan
  assertions instead of suppressing collection.
- Migrated still-valid contracts into `WorkflowScheduleTest`: exact target-date
  validation, workflow-dispatch environment bridging, optional failure
  isolation, shared checkout/concurrency, cron mapping, shell syntax,
  settlement date roles, dependency setup, changed-only commits, status-before-
  Pages publication, and the manual-email guard.
- Added execution-level coverage for the printed live snapshot path, empty and
  malformed changed JSON, exact-date CLI failures, and global command counts.

### Cross-Midnight Evidence

- The due-step rehearsal processes changed dates `2026-07-18` and `2026-07-19`
  at `2026-07-19T00:05:00+08:00` and selects `2026-07-19` as the deterministic
  display date.
- The rebuild rehearsal preserves both date-scoped status/image byte pairs and
  observes exactly one `build_site.py`, one `build_daily_image.py`, one
  `report_status.py`, and one revalidation-index command, all global builders
  targeting `2026-07-19`.
