# Phase 2 Final Review Fix Wave Report

## Status

`DONE_WITH_CONCERNS`

The five final-review findings are implemented and verified. Activation remains
honestly fail-closed in `shadow` mode because no date from 2026-07-11 through
2026-07-18 has a qualifying prospective immutable decision bundle.

## Resolution

1. Added an atomically published, immutable, date-scoped decision bundle that
   binds one aware Beijing lock time, one approved domestic decision snapshot,
   exact predictions and real generation metadata, canonical date fixtures,
   configurations, ratings, histories, model code, market values, eligibility,
   identities, and explicit paid/reference roles. Identical publication is
   idempotent and conflicting publication fails closed.
2. Made generation, plan locking, the workflow, and activation audit consume
   that exact bundle. The plan lock derives `sporttery` or `zgzcw` from the
   validated bundle. Nonempty ZGZCW generation, locking, ingestion, and recovery
   are covered.
3. Added immutable date-scoped activation evidence with a hashed manifest and
   canonical as-of extracts. Audit rebuilds and readiness use only those copies;
   later shared fixture, ledger, and training-file changes do not stale a passed
   audit, while evidence mutation fails readiness.
4. Kept both Sporttery and ZGZCW as approved domestic result sources. Settlement
   requires a finished result, nonempty source record, aware capture time, and
   explicit `regular_time_90` / `90` scope. Arbitrary, ambiguous, missing-scope,
   and extra-time records remain unsettled.
5. Added fail-closed validation for every existing canonical paid row before
   account arithmetic, including identity, exact dates, stable bet ID, finite
   positive 2-yuan stake, domestic locked odds provenance, plan hash, settlement
   scope, and terminal/abnormal economics. Legacy economics remain separate and
   conservative.

## RED/GREEN Evidence

All Python commands below used
`.superpowers\sdd\runtime\verify-venv\Scripts\python.exe`.

### Decision bundle, generator, lock, and workflow

Initial bundle RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_decision_bundle -v
```

Result: import failed with one `ModuleNotFoundError` because
`decision_bundle.py` did not exist.

Focused generator RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_decision_bundle.DecisionBundleTest.test_value_generator_uses_only_inputs_selected_by_the_bundle -v
```

Result: one error because the generator did not accept a decision bundle.

Batch GREEN:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_decision_bundle tests.test_plan_lock tests.test_value_strategy tests.test_value_strategy_integration tests.test_workflow_schedule
```

Result: 109 tests passed.

### Immutable activation evidence and honest state

Immutable evidence RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_shadow_portfolio_audit.RepositoryAuditTest.test_readiness_uses_immutable_as_of_extracts_after_shared_files_advance -v
```

Result: one error, `KeyError: activation_evidence`.

Honest-state RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_shadow_portfolio_audit.RepositoryAuditTest.test_repository_configuration_is_simulation_only_and_shadow_until_prospective_audit -v
```

Result: one failure because repository activation was still `active`.

Batch GREEN:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_shadow_portfolio_audit
```

Result: 22 tests passed.

### Result provenance and canonical ledger validation

Focused RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_update_sporttery_results.ResultProvenanceTest.test_direct_sporttery_rows_keep_match_id_and_finished_provenance tests.test_update_sporttery_results.ResultProvenanceTest.test_fallback_resolves_only_proven_fixture_match_ids_and_preserves_legacy_rows tests.test_betting_ledger.SettlementTest.test_only_approved_proven_regular_time_90_results_can_settle tests.test_betting_ledger.SettlementTest.test_existing_canonical_paid_corruption_fails_before_account_math tests.test_betting_ledger.SettlementTest.test_malformed_legacy_economics_cannot_restore_paid_budget -v
```

Result: five tests were RED: result rows lacked explicit score scope, invalid
scope/source records settled, canonical corruption was accepted, and malformed
legacy economics reached account-cap arithmetic.

The same five-test command passed after implementation. The complete ledger and
result suites then passed 72 tests.

Self-review RED for negative terminal profit and immutable manifest completeness:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger.SettlementTest.test_valid_existing_canonical_loss_preserves_negative_profit tests.test_shadow_portfolio_audit.RepositoryAuditTest.test_readiness_uses_immutable_as_of_extracts_after_shared_files_advance -v
```

Result: two tests ran with one error and one failure. The same command then
passed both tests; the combined ledger/audit suites passed 78 tests.

Self-review RED for embedded bundle contract tampering:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_decision_bundle.DecisionBundleTest.test_tampered_embedded_contract_fields_invalidate_bundle -v
```

Result: one test with three failing subtests. After validation hardening,
`tests.test_decision_bundle tests.test_plan_lock` passed 27 tests.

Final self-review RED for abnormal economics and parlay result scope:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger.SettlementTest.test_existing_abnormal_and_parlay_rows_bind_economics_and_result_scope -v
```

Result: one test with four failing subtests. The same command then passed, and
the final bundle/lock/audit/result/ledger focused run passed 123 tests.

## Final Verification

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Result: 543 tests passed in 21.935 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_workflow_schedule
```

Result: 38 tests passed in 6.483 seconds, including workflow shell syntax and
ordering checks.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile activation_readiness.py audit_shadow_portfolio.py betting_ledger.py decision_bundle.py generate_betting_plan.py plan_lock.py predict_today.py update_sporttery_results.py tests\test_betting_ledger.py tests\test_decision_bundle.py tests\test_plan_lock.py tests\test_report_status.py tests\test_shadow_portfolio_audit.py tests\test_update_sporttery_results.py tests\test_value_strategy.py tests\test_value_strategy_integration.py tests\test_workflow_schedule.py
```

Result: exit 0 with no output.

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests\apps_script_orchestrator.test.mjs
```

Result: 42 tests passed, 0 failed.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe audit_shadow_portfolio.py --from 2026-07-11 --through 2026-07-18
```

Result: expected exit 1; zero checked dates, eight `excluded_missing` dates,
`passed: false`, and one `zero_checked_dates` violation.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -c "from pathlib import Path; from activation_readiness import assert_activation_ready; assert_activation_ready(Path.cwd())"
```

Result: expected exit 1 with `ValueError: activation audit has not passed`.

```powershell
git diff --check
```

Result: exit 0. Git emitted only informational LF-to-CRLF working-copy
warnings; no whitespace errors were reported.

## Activation and Evidence

- `betting_config.json`: `value_strategy.activation_mode` is `shadow`.
- Simulation-only and no-real-money controls remain unchanged.
- Persisted audit: `checked_dates: []`, `passed: false`.
- Excluded missing: every date from 2026-07-11 through 2026-07-18.
- Violation: `zero_checked_dates`.
- No July 18 prediction generation timestamp or pre-kickoff metadata was
  invented. Existing July 18 artifacts were excluded because they cannot meet
  the prospective immutable bundle contract honestly.
- No real historical activation-evidence directory was fabricated. Evidence
  publication and next-day shared-file mutation behavior are proven in isolated
  tests; the first real evidence must come from a future pre-kickoff bundle.

## Commits

- Implementation and tests:
  `e0e0b00a46adc28e65bf405326932cd780db227e`
- This report is committed separately; its local SHA is supplied in the final
  delivery response because a commit cannot contain its own final SHA.

## Concerns

Activation cannot be restored yet. A future business date must capture real
prediction metadata and the immutable decision bundle before kickoff, persist
the corresponding activation evidence, and pass the mechanical audit. Until
then, active routing correctly remains unavailable.

## Re-review Wave: Import Binding and Ledger Payload Integrity

This wave resolves the two Important findings raised on top of
`e0e0b00a46adc28e65bf405326932cd780db227e` and
`0cfd3111e4d9992844187d272647f9cf9e1c71c6`.

### Resolution

1. `import_sporttery.py` now atomically publishes one immutable manifest at
   `data/import_manifests/YYYY-MM-DD.json`. It binds the approved domestic
   source, target date, aware Beijing import timestamp, and exact fixture/odds
   paths, byte counts, and SHA-256 hashes. Identical reruns preserve the first
   manifest bytes; source conflicts and file tampering fail closed.
2. Production snapshot capture loads only the fixture and odds files named by
   that validated manifest. It performs no availability redetection or source
   refetch. The snapshot embeds the exact manifest record, and decision-bundle
   schema 2 requires the same live manifest payload, record, and source.
3. New canonical paid ledger rows persist `row_payload_sha256`. The digest
   covers canonical identity, date/lock/plan hash, teams and kickoff, market and
   legs, all known plan probability/calibration aliases, odds provenance,
   projected economics, stake, Kelly/risk controls, quality/rank/limits, and
   the initial pending/zero-return/zero-profit state. Existing canonical paid
   rows are checked before account arithmetic, and direct settlement verifies
   the digest before any state transition. Terminal result fields remain
   mutable through the existing settlement contract; explicit noncanonical
   legacy handling and stable bet identity are unchanged.

### Re-review RED/GREEN Evidence

Manifest/source binding RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_import_sporttery.ImportManifestTest.test_manifest_is_immutable_idempotent_and_hash_validated tests.test_capture_odds_snapshot.CaptureOddsSnapshotProductionTest.test_capture_uses_sporttery_import_when_later_availability_flips_to_zgzcw tests.test_capture_odds_snapshot.CaptureOddsSnapshotProductionTest.test_capture_uses_zgzcw_import_when_later_availability_flips_to_sporttery tests.test_decision_bundle.DecisionBundleTest.test_bundle_rejects_snapshot_source_divergent_from_import_manifest -v
```

Result: 4 tests ran with 3 failures and 1 error. There was no manifest writer,
capture refetched the newly available source in both divergence directions,
and the bundle accepted a source divergent from import.

The identical command then passed all 4 tests. The surrounding import,
capture, and decision-bundle modules passed 43 tests.

Canonical payload digest RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger.SettlementTest.test_new_canonical_paid_row_persists_an_immutable_payload_digest tests.test_betting_ledger.SettlementTest.test_payload_digest_rejects_valid_looking_stake_odds_and_plan_hash_edits tests.test_betting_ledger.SettlementTest.test_payload_digest_rejects_coherent_terminal_odds_and_economics_edit -v
```

Result: 3 tests ran with 6 failing assertions/subtests. No digest was persisted,
and missing digest, stake reduction, coherent odds/economics edits, and an
arbitrary valid-looking plan hash were accepted. The identical command then
passed all 3 tests; the complete ledger module passed 60 tests.

Self-review projected-economics RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger.SettlementTest.test_payload_digest_rejects_valid_looking_stake_odds_and_plan_hash_edits -v
```

Result: 1 test ran with 1 failing subtest because coherent edits to
`expected_value`, `expected_return`, and `expected_profit` were not initially
covered. After expanding the immutable field set, the command passed 1 test
and the ledger module again passed all 60 tests.

The first expanded focused run exposed 30 synthetic bundle-fixture errors in
161 tests. The first full discovery exposed 12 errors in 548 tests: 2 old
capture callers, 3 direct canonical-ledger helpers, and 7 report-status bundle
fixtures. After fixing the first 5, the next discovery run reported the
remaining 7 report-status errors. Each fixture was changed to construct the
required manifest or enter through canonical ingestion. No runtime validator
was relaxed. The final focused and full commands below are green.

### Re-review Final Verification

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results tests.test_shadow_portfolio_audit tests.test_decision_bundle tests.test_plan_lock tests.test_import_sporttery tests.test_capture_odds_snapshot
```

Result: 161 tests passed in 4.714 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Result: 548 tests passed in 18.359 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_workflow_schedule
```

Result: 39 tests passed in 7.855 seconds, including shell syntax, command order,
exact-manifest workflow consumption, nonempty ZGZCW lock, and ingestion
recovery coverage.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile betting_ledger.py capture_odds_snapshot.py decision_bundle.py import_sporttery.py tests/test_betting_ledger.py tests/test_capture_odds_snapshot.py tests/test_collect_market_heat.py tests/test_decision_bundle.py tests/test_import_sporttery.py tests/test_plan_lock.py tests/test_report_status.py tests/test_shadow_portfolio_audit.py tests/test_value_strategy_integration.py tests/test_workflow_schedule.py
```

Result: 14 changed Python files compiled with exit 0 and no output.

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests\apps_script_orchestrator.test.mjs
```

Result: 42 tests passed, 0 failed.

The real audit CLI again exited 1 with `checked_dates: []`, all eight dates
from 2026-07-11 through 2026-07-18 in `excluded_missing`, `passed: false`, and
one violation. The readiness probe again exited 1 with
`ValueError: activation audit has not passed`. `git diff --check` exited 0.

### Re-review Activation, Commits, and Concerns

- Activation remains `shadow`; simulation-only/no-real-money controls are
  unchanged, and no July 18 metadata or evidence was fabricated.
- Base commits are `e0e0b00a46adc28e65bf405326932cd780db227e` and
  `0cfd3111e4d9992844187d272647f9cf9e1c71c6`. The local re-review commit SHA is
  supplied in the final delivery response because this report is part of that
  commit.
- The remaining concern is unchanged: activation needs a future prospective,
  pre-kickoff import manifest, snapshot, prediction metadata, immutable bundle,
  and passing audit. Until then, readiness correctly remains unavailable.

## Final-Finding Wave: Durable Extracts, Ledger Anchor, and Chronology

This wave resolves the three findings assigned on top of
`42518deabf7f4892ccbe1749bf152af0c2a662bf`.

### Resolution

1. Import publication now stages fixture and odds bytes, publishes immutable
   extracts at `data/import_extracts/YYYY-MM-DD/fixtures.csv` and `odds.json`,
   and atomically publishes a manifest that names and hashes those extracts.
   A valid same-date manifest is validated before compatibility-file writes;
   reruns restore changed shared fixture/odds files from the immutable extracts
   without source refetch. Conflicting extracts, source changes, manifest
   tampering, and hash mismatches fail closed. Later dates cannot invalidate
   prior manifests, bundles, or locks.
2. Persisted canonical ledger rows are now reconciled to the exact plan bytes
   named by each date's valid plan lock. Settlement and idempotent ingestion
   rebuild the expected canonical row digest from that external evidence before
   account arithmetic. Recomputed local digests cannot bless a valid stake
   reduction, coherent odds/economics edits, or an arbitrary plan hash.
   Canonical-shaped rows cannot enter legacy migration by changing
   `strategy_version`; genuine legacy migration and terminal settlement fields
   remain compatible.
3. Decision-bundle validation now enforces
   `imported_at_bjt <= captured_at <= locked_at` with aware timestamps normalized
   to Beijing time. A future-dated import manifest is excluded rather than
   accepted.

### RED/GREEN Evidence

All Python commands used
`.superpowers\sdd\runtime\verify-venv\Scripts\python.exe`.

The initial six-regression RED command was:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_import_sporttery.ImportManifestTest.test_next_day_import_does_not_invalidate_prior_date_extracts tests.test_import_sporttery.ImportManifestTest.test_same_date_valid_manifest_is_reused_before_shared_writers tests.test_decision_bundle.DecisionBundleTest.test_bundle_rejects_manifest_imported_after_snapshot_capture tests.test_plan_lock.PlanLockTest.test_next_day_shared_fixture_update_preserves_prior_lock tests.test_betting_ledger.LockedIngestCommandTest.test_settlement_rejects_tamper_even_after_row_digest_is_recomputed tests.test_betting_ledger.LockedIngestCommandTest.test_strategy_downgrade_cannot_migrate_a_canonical_shaped_row -v
```

Result: 6 tests ran with 2 failures and 4 errors. The old shared paths
invalidated day-N evidence, future import chronology was accepted, and the old
shared fixture invalidated a prior lock. Two errors were test-harness issues;
they were corrected without production changes. The refined remaining RED
command was:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_import_sporttery.ImportManifestTest.test_same_date_valid_manifest_is_reused_before_shared_writers tests.test_betting_ledger.LockedIngestCommandTest.test_settlement_rejects_tamper_even_after_row_digest_is_recomputed tests.test_betting_ledger.LockedIngestCommandTest.test_strategy_downgrade_cannot_migrate_a_canonical_shaped_row -v
```

Result: 3 tests ran with 3 expected failures: same-date reuse refetched, a
recomputed digest self-blessed tampering, and a strategy downgrade entered
legacy migration.

The final six-regression GREEN command (with the recovery test's final name)
was:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_import_sporttery.ImportManifestTest.test_next_day_import_does_not_invalidate_prior_date_extracts tests.test_import_sporttery.ImportManifestTest.test_same_date_manifest_recovers_changed_shared_files_without_refetch tests.test_decision_bundle.DecisionBundleTest.test_bundle_rejects_manifest_imported_after_snapshot_capture tests.test_plan_lock.PlanLockTest.test_next_day_shared_fixture_update_preserves_prior_lock tests.test_betting_ledger.LockedIngestCommandTest.test_settlement_rejects_tamper_even_after_row_digest_is_recomputed tests.test_betting_ledger.LockedIngestCommandTest.test_strategy_downgrade_cannot_migrate_a_canonical_shaped_row -v
```

Result: 6 tests passed in 0.285 seconds. The recomputed-digest test includes
three attack subtests: stake, coherent odds/economics, and plan hash.

### Final Verification

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results tests.test_shadow_portfolio_audit tests.test_decision_bundle tests.test_plan_lock
```

Result: the prior focused review surface plus new regressions passed 131 tests
in 4.678 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Result: 554 tests passed in 18.065 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_workflow_schedule
```

Result: 39 tests passed in 7.616 seconds, including shell syntax, command order,
immutable manifest consumption, ZGZCW lock/ingestion recovery, and idempotent
settlement.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile betting_ledger.py decision_bundle.py import_sporttery.py tests\test_betting_ledger.py tests\test_capture_odds_snapshot.py tests\test_decision_bundle.py tests\test_import_sporttery.py tests\test_plan_lock.py tests\test_report_status.py tests\test_shadow_portfolio_audit.py tests\test_workflow_schedule.py
```

Result: all 11 changed Python files compiled with exit 0 and no output.

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests\apps_script_orchestrator.test.mjs
```

Result: 42 tests passed, 0 failed.

The real audit CLI exited 1 as required with `checked_dates: []`, all eight
dates from 2026-07-11 through 2026-07-18 in `excluded_missing`, `passed: false`,
and one violation. The readiness probe exited 1 with
`ValueError: activation audit has not passed`. `git diff --check` exited 0.

### Activation, Files, Commits, and Concerns

- Activation remains `shadow`; simulation-only and no-real-money controls are
  unchanged. No July 18 metadata, import, snapshot, or evidence was fabricated.
- Production files changed: `import_sporttery.py`, `decision_bundle.py`, and
  `betting_ledger.py`.
- Test evidence changed: `tests/test_import_sporttery.py`,
  `tests/test_capture_odds_snapshot.py`, `tests/test_decision_bundle.py`,
  `tests/test_plan_lock.py`, `tests/test_betting_ledger.py`,
  `tests/test_report_status.py`, `tests/test_shadow_portfolio_audit.py`, and
  `tests/test_workflow_schedule.py`.
- Implementation commit:
  `f06b0a8deca097d6df2024c3caccfce04f113c03`.
- Fresh implementation diff package:
  `.superpowers/sdd/review-phase2-final-fixes3-42518de..f06b0a8.diff`, generated
  from the literal `42518de..HEAD` range while `HEAD` was the implementation
  commit. The report/package commit SHA is supplied in the delivery response.
- Remaining concern: activation still requires a future prospective pre-kickoff
  bundle and passing immutable audit. Readiness correctly remains unavailable
  until that real evidence exists.

## Final Re-review Wave: Plan Evidence, Canonical Identity, and Recovery

This wave resolves the two Important findings and the ancillary-output Minor
assigned on top of `c7444182b2c5d5edb9c89affcdfcf14a896e9785`.

### Resolution

1. Decision-bundle schema 3 now binds every decision-time input used by the
   paid shadow plan, including account metrics, and contains deterministic paid
   plan evidence: exact rows, row digest, exact CSV digest, byte count, and row
   count. The generator consumes those bundle-owned inputs, and `plan_lock`
   refuses any plan whose bytes differ from the immutable evidence. Editing a
   single or parlay plan together with its lock therefore cannot create a valid
   ledger anchor.
2. Persisted canonical ledger rows are classified against locked-plan stable
   identities before legacy migration. Canonical evidence is mandatory, its
   digest must be explicit and nonempty, and settlement/ingestion fail closed
   when the matching lock is absent. Stripped markers and parlay strategy
   downgrades fail while genuine historical rows retain stable legacy IDs,
   idempotent migration, and compatible terminal settlement mutations.
3. An existing validated import manifest now atomically regenerates
   `team_ratings.csv` and `source_status.json` from its immutable fixture
   extract before restoring shared fixture/odds files. A crash immediately
   after manifest publication is recoverable without source refetch, including
   nonempty team ratings and the exact fixture count.

### RED/GREEN Evidence

All Python commands used
`.superpowers\sdd\runtime\verify-venv\Scripts\python.exe`.

Real single/parlay joint plan-and-lock tamper RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_plan_lock.PlanLockTest.test_real_lock_rejects_joint_plan_and_lock_tamper_for_single_and_parlay -v
```

Result before implementation: 1 test ran with 2 failing subtests; both edited
plan/lock pairs remained valid after their hashes and ledger row digests were
recomputed. The identical command then passed 1 test in 0.294 seconds. The
decision-bundle and plan-lock modules together passed 31 tests in 4.059
seconds.

Canonical stripped-marker/missing-lock RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger.LockedIngestCommandTest.test_locked_identity_rejects_a_single_with_all_new_markers_stripped tests.test_betting_ledger.LockedIngestCommandTest.test_locked_identity_rejects_a_parlay_strategy_downgrade tests.test_betting_ledger.LockedIngestCommandTest.test_stripped_canonical_identity_fails_closed_when_lock_is_missing -v
```

Result before implementation: 3 tests ran with 3 failures because every row
entered legacy migration. The identical command then passed all 3 tests in
0.032 seconds; the complete ledger module passed 65 tests in 0.312 seconds.

Ancillary crash-recovery RED:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_import_sporttery.ImportSportterySourceStatusTest.test_manifest_crash_recovery_preserves_ratings_and_source_status -v
```

Result before implementation: 1 test failed because a valid manifest survived
the simulated crash but the no-refetch rerun left `team_ratings.csv` absent.
The strengthened nonempty-fixture version of the identical command passed 1
test in 0.071 seconds, and the import module passed 23 tests in 0.320 seconds.

The first full discovery after the focused fixes ran 559 tests with 5 failures
and 9 errors. These were test-harness contract gaps only: seven report-status
fixtures used an incomplete betting config, two integration bundles omitted
paid-plan evidence, and five workflow stubs attempted the pre-evidence plan
contract. Fixtures were updated to use real config, exact bundle-owned plan
bytes, and deterministic bundle-first workflow generation. No production
validator was relaxed.

### Final Verification

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results tests.test_shadow_portfolio_audit tests.test_decision_bundle tests.test_plan_lock tests.test_import_sporttery tests.test_capture_odds_snapshot
```

Result: the prior review surface plus all new focused regressions passed 172
tests in 6.294 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Result: 559 tests passed in 19.499 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_workflow_schedule -q
```

Result: 39 tests passed in 7.823 seconds, including shell/order validation,
bundle-before-plan generation, exact plan-evidence consumption, ZGZCW
lock/ingestion recovery, and idempotent settlement.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile betting_ledger.py decision_bundle.py generate_betting_plan.py import_sporttery.py plan_lock.py tests\test_betting_ledger.py tests\test_decision_bundle.py tests\test_import_sporttery.py tests\test_plan_lock.py tests\test_report_status.py tests\test_value_strategy_integration.py tests\test_workflow_schedule.py
```

Result: all 12 changed Python files compiled with exit 0 and no output.

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests\apps_script_orchestrator.test.mjs
```

Result: 42 tests passed, 0 failed.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe audit_shadow_portfolio.py --from 2026-07-11 --through 2026-07-18
```

Result: expected exit 1 with `checked_dates: []`, all eight dates in
`excluded_missing`, `passed: false`, and one violation.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -c "from pathlib import Path; from activation_readiness import assert_activation_ready; assert_activation_ready(Path.cwd())"
```

Result: expected exit 1 with `ValueError: activation audit has not passed`.
`git diff --check` exited 0 with only informational LF-to-CRLF warnings.

### Activation, Files, Commits, and Concerns

- Activation remains `shadow`; simulation-only/no-real-money controls are
  unchanged. No July 18 import, snapshot, prediction, or lock evidence was
  captured or fabricated.
- Production files changed: `betting_ledger.py`, `decision_bundle.py`,
  `generate_betting_plan.py`, `import_sporttery.py`, and `plan_lock.py`.
- Test files changed: `tests/test_betting_ledger.py`,
  `tests/test_decision_bundle.py`, `tests/test_import_sporttery.py`,
  `tests/test_plan_lock.py`, `tests/test_report_status.py`,
  `tests/test_value_strategy_integration.py`, and
  `tests/test_workflow_schedule.py`.
- Implementation commit:
  `a56557203a033ffee4d3397528ad00003d91f892`.
- Fresh diff package:
  `.superpowers/sdd/review-phase2-final-fixes4-c744418..a565572.diff`, generated
  from the literal implementation range before this report/package commit.
- Remaining concern: schema-2 decision bundles intentionally cannot authorize
  new paid ingestion because they lack deterministic plan evidence. Activation
  still requires a future prospective pre-kickoff schema-3 bundle and passing
  immutable audit; readiness correctly remains unavailable until then.

## Final Targeted Wave: Canonical Cutover and Immutable Ratings

This wave resolves the remaining Important and Minor findings assigned on top
of `ade37645a8ad4139ae9d33fd7304a8ce50bff168`.

### Resolution

1. `betting_ledger.py` now applies an audited canonical paid-ledger cutover of
   `2026-07-18`. Every row whose effective report date is on or after the
   cutover is classified as canonical before inspecting `strategy_version`,
   `bet_id`, row digest, or any other optional marker. Settlement therefore
   requires a valid schema-3 plan lock and exact immutable plan-evidence
   membership. Replacing the supplied ID and stripping all markers fails for
   both singles and parlays, with a valid lock or with the lock missing.
   Genuine pre-cutover legacy rows, including the audited 2026-07-11 and
   2026-07-12 history, retain legacy migration and stable IDs.
2. Import-manifest schema 2 now publishes and hashes
   `data/import_extracts/<date>/ratings.csv` alongside exact fixture and odds
   extracts. Prediction metadata and decision bundles consume that immutable
   ratings record. Existing-manifest recovery restores the exact manifest-era
   ratings bytes, so later shared Elo edits or newly added teams cannot leak
   into a same-date recovery.
3. Temporary raw review artifacts
   `review-phase2-final-fixes3-42518de..f06b0a8.diff` and
   `review-phase2-final-fixes4-c744418..a565572.diff` were removed. The
   human-readable report and the earlier f1/f2 historical packages remain.

### RED/GREEN Evidence

All Python commands used
`.superpowers\sdd\runtime\verify-venv\Scripts\python.exe`.

Canonical-cutover RED/GREEN:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_plan_lock.PlanLockTest.test_cutover_rejects_replaced_ids_and_stripped_markers_with_valid_lock tests.test_plan_lock.PlanLockTest.test_cutover_rejects_replaced_ids_and_stripped_markers_without_lock tests.test_plan_lock.PlanLockTest.test_pre_cutover_legacy_row_migrates_without_a_lock -v
```

RED: 3 tests ran; four attack subtests failed because changed IDs with stripped
markers entered legacy migration, while the pre-cutover control passed. GREEN:
the identical command passed all 3 tests in 0.475 seconds. The combined ledger
and plan-lock suites then passed 89 tests in 4.250 seconds.

Immutable-ratings RED/GREEN:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_import_sporttery.ImportManifestTest.test_manifest_is_immutable_idempotent_and_hash_validated tests.test_import_sporttery.ImportManifestTest.test_same_date_manifest_recovers_changed_shared_files_without_refetch -v
```

RED: 2 tests ran with 1 error (`KeyError: ratings`) and 1 failure because
recovery retained the mutated shared ratings bytes. GREEN: the identical
command passed both tests in 0.100 seconds. The complete import suite passed 23
tests in 0.345 seconds, and the cross-evidence suite passed 111 tests in 8.722
seconds.

### Final Verification

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results tests.test_shadow_portfolio_audit tests.test_decision_bundle tests.test_plan_lock tests.test_import_sporttery tests.test_capture_odds_snapshot
```

Result: 175 focused Python tests passed in 7.086 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Result: 562 tests passed in 20.791 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_workflow_schedule -q
```

Result: 39 workflow shell/order tests passed in 7.946 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile betting_ledger.py decision_bundle.py import_sporttery.py tests\test_capture_odds_snapshot.py tests\test_decision_bundle.py tests\test_import_sporttery.py tests\test_plan_lock.py tests\test_report_status.py tests\test_shadow_portfolio_audit.py tests\test_workflow_schedule.py
```

Result: all 10 changed Python modules compiled with exit 0 and no output.

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests\apps_script_orchestrator.test.mjs
```

Result: 42 Node tests passed, 0 failed.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe audit_shadow_portfolio.py --from 2026-07-11 --through 2026-07-18
```

Result: expected exit 1 with `checked_dates: []`, all eight dates in
`excluded_missing`, `passed: false`, and one violation.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -c "from pathlib import Path; from activation_readiness import assert_activation_ready; assert_activation_ready(Path.cwd())"
```

Result: expected exit 1 with `ValueError: activation audit has not passed`.

```powershell
git diff --check ed7dba6aeb21ed54bed080a55b554d59d95cb26a..HEAD
```

Result: exit 0 with no output.

### Activation, Files, Commits, and Concerns

- Activation remains `shadow`; simulation-only/no-real-money controls are
  unchanged. No July 18 import, snapshot, prediction, lock, or other real
  evidence was captured or fabricated.
- Production files changed: `betting_ledger.py`, `decision_bundle.py`, and
  `import_sporttery.py`.
- Tests/callers changed: `tests/test_import_sporttery.py`,
  `tests/test_plan_lock.py`, `tests/test_capture_odds_snapshot.py`,
  `tests/test_decision_bundle.py`, `tests/test_report_status.py`,
  `tests/test_shadow_portfolio_audit.py`, and
  `tests/test_workflow_schedule.py`.
- Production/tests commit:
  `f8c682236789e01ad89acdf90d3e8874142fc1cb`.
- The cleanup/report commit SHA is supplied in the delivery response so this
  report does not contain a circular self-reference.
- Remaining concern: existing schema-1 import manifests do not contain an
  immutable ratings extract and therefore cannot satisfy the prospective
  schema-2 evidence contract. Activation still requires a new honest
  pre-kickoff schema-2 import, schema-3 decision bundle/lock, and passing
  immutable audit. Readiness correctly remains unavailable until then.

## Final Narrow Wave: Ledger Dates and Prediction Provenance

This wave resolves the two Important findings assigned on top of
`6f6e38b9d987b1a9cee82986a1d700565fba3b02`, including the fixture/history
provenance addendum.

### Resolution

1. Every existing paid-ledger row now passes one strict effective-date
   validator before lock discovery or canonical/legacy classification. At
   least one of `date` and `report_date` must be nonblank, every populated
   field must be canonical `YYYY-MM-DD`, and two populated fields must match.
   The validated effective date alone determines the `2026-07-18` cutover.
   Blank, malformed, conflicting, and pre/post-cutover alias attacks therefore
   fail before lock lookup or legacy migration for singles and parlays, with
   valid or missing locks. Genuine 2026-07-11 and 2026-07-12 legacy rows with
   only `date` retain compatible migration.
2. `predict_today.py` now requires a valid schema-2 import manifest for every
   production CLI prediction, including `--no-files`. It reads and rechecks
   the exact manifest-bound fixture and ratings bytes before parsing them.
   Mutable `data/fixtures.csv` and `data/team_ratings.csv` are not prediction
   inputs on that path.
3. The optional mutable `data/team_history_features.csv` overlay is explicitly
   omitted from the manifest-bound production path. Prediction-metadata schema
   2 records both fixture and ratings manifest records, derives its canonical
   fixture rows from the immutable extract, and verifies that the records
   forwarded by the predictor still equal the validated manifest. No raw diff
   package was created.

### RED/GREEN Evidence

All Python commands used
`.superpowers\sdd\runtime\verify-venv\Scripts\python.exe`.

Strict effective-date RED/GREEN:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_plan_lock.PlanLockTest.test_effective_date_validation_precedes_classification_and_lock_lookup tests.test_plan_lock.PlanLockTest.test_pre_cutover_legacy_row_migrates_without_a_lock -v
```

RED: 2 test methods ran in 2.596 seconds. Eighteen of the twenty single/parlay,
valid/missing-lock attack subtests failed under the old classification path;
both genuine pre-cutover controls passed. GREEN: the identical command passed
both methods, including all twenty attack subtests and both legacy controls,
in 2.442 seconds. The complete ledger/plan-lock surface passed 90 tests in
7.024 seconds.

Manifest-bound prediction RED/GREEN:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_decision_bundle.DecisionBundleTest.test_prediction_metadata_records_real_generation_and_model_inputs tests.test_decision_bundle.DecisionBundleTest.test_prediction_cli_consumes_manifest_inputs_without_mutable_history tests.test_decision_bundle.DecisionBundleTest.test_prediction_cli_requires_schema_two_manifest_even_without_files -v
```

RED: 3 test methods ran with 4 failures: fixture provenance was absent,
prediction consumed the divergent shared fixture/rating inputs, and missing or
schema-1 manifests did not stop `--no-files`. GREEN: the identical command
passed all 3 methods in 0.139 seconds. The complete decision-bundle suite then
passed 12 tests in 0.858 seconds.

### Final Verification

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_betting_ledger tests.test_update_sporttery_results tests.test_shadow_portfolio_audit tests.test_decision_bundle tests.test_plan_lock tests.test_import_sporttery tests.test_capture_odds_snapshot tests.test_official_market_import
```

Result: 183 focused Python tests passed in 10.353 seconds.

The first full discovery ran 565 tests with one transient Windows
`PermissionError` in the pre-existing concurrent same-date plan-lock test. The
isolated reproduction passed 1 test in 0.428 seconds, and the required fresh
full rerun completed cleanly:

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Result: 565 tests passed in 23.932 seconds.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_workflow_schedule -q
```

Result: 39 workflow shell/order tests passed in 7.936 seconds.

```powershell
C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test tests\apps_script_orchestrator.test.mjs
```

Result: 42 Node tests passed, 0 failed.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile betting_ledger.py decision_bundle.py predict_today.py tests\test_betting_ledger.py tests\test_decision_bundle.py tests\test_plan_lock.py
```

Result: all 6 changed Python files compiled with exit 0 and no output.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe audit_shadow_portfolio.py --from 2026-07-11 --through 2026-07-18
```

Result: expected exit 1 with `checked_dates: []`, all eight dates in
`excluded_missing`, `passed: false`, and one violation.

```powershell
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -c "from pathlib import Path; from activation_readiness import assert_activation_ready; assert_activation_ready(Path.cwd())"
```

Result: expected exit 1 with `ValueError: activation audit has not passed`.

### Activation, Files, Commits, and Concerns

- Activation remains `shadow`; simulation-only/no-real-money controls are
  unchanged. No real import, prediction, snapshot, plan, or lock evidence was
  captured or fabricated.
- Production files changed: `betting_ledger.py`, `decision_bundle.py`, and
  `predict_today.py`.
- Test files changed: `tests/test_betting_ledger.py`,
  `tests/test_decision_bundle.py`, and `tests/test_plan_lock.py`.
- Production/tests commit:
  `c46223fa3a6ed64a43f7ccbc052b5543342ec1c9`.
- The cleanup/report commit SHA is supplied in the delivery response to avoid
  a circular self-reference.
- Remaining concern: old prediction-metadata schema 1 and old import-manifest
  schema 1 cannot satisfy the prospective immutable fixture/ratings contract.
  Activation still requires honest schema-2 import and prediction evidence, a
  schema-3 decision bundle/lock, and a passing immutable audit.
