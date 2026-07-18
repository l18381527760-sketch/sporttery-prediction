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
