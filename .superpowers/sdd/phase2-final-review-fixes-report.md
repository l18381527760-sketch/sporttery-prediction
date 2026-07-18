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
