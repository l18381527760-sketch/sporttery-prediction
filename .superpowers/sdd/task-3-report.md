# Task 3 Report: Shared Proven 90-Minute Result Contract

## Implementation and Files

- `result_evidence.py`: added the canonical `proven_result_provenance`,
  `normalized_result`, and `proven_90_minute_result` contract. It accepts only
  approved `sporttery` or `zgzcw` provenance, aware capture timestamps, finished
  regular-time 90-minute scores, nonempty canonical match IDs, and nonnegative
  integer goals.
- `build_historical_features.py`: filters `bet_results.csv` through
  `proven_90_minute_result`; legacy display rows remain stored but cannot build
  historical features.
- `draw_model_learning.py`: normalizes proven results once, indexes them by
  canonical `match_id`, and removes the prior date/team and local goal predicate.
- `betting_ledger.py`: delegates finished-result eligibility to
  `proven_90_minute_result` and refund/invalid provenance to
  `proven_result_provenance`. Settlement return, profit, effective-odds, and
  correction branches were not changed.
- `tests/test_result_evidence.py`: adds the prescribed evidence matrix plus
  malformed-row and negative-goal fail-closed coverage.
- `tests/test_build_historical_features.py`: proves only canonical proven rows
  feed historical features.
- `tests/test_draw_model_learning.py`: upgrades existing fixtures to canonical
  evidence and proves training joins only by canonical match ID.
- `tests/test_betting_ledger.py`: proves an unapproved refund source cannot
  settle while existing single and parlay refund economics remain covered.
- `tests/test_collect_market_heat.py`: upgrades the verified training integration
  fixture to the shared canonical result shape.

## RED Evidence

Command:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_result_evidence -v
```

Expected output, exit code 1:

```text
ImportError: Failed to import test module: test_result_evidence
ModuleNotFoundError: No module named 'result_evidence'
Ran 1 test in 0.000s
FAILED (errors=1)
```

The failure was the prescribed missing-module RED before any production
implementation.

## GREEN Evidence

Canonical contract:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_result_evidence -v
```

```text
Ran 4 tests in 0.000s
OK
```

Focused consumers and ledger:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_result_evidence tests.test_build_historical_features tests.test_draw_model_learning tests.test_betting_ledger -v
```

```text
Ran 151 tests in 14.861s
OK
```

The first complete Python run found one stale legacy-shaped result fixture in
`test_collect_market_heat`. The run completed `698` tests with `1` failure.
After adding the required canonical evidence fields, the failing integration
test passed in isolation:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_collect_market_heat.MarketHeatCollectorTest.test_collector_payload_flows_to_verified_training_snapshot -v
```

```text
Ran 1 test in 0.048s
OK
```

Final complete Python suite:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
```

```text
Ran 698 tests in 45.250s
OK
```

Exact Node suite:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --test tests/apps_script_orchestrator.test.mjs
```

```text
tests 63
pass 63
fail 0
cancelled 0
skipped 0
```

## Self-Review

- Settlement regressions: refund rows still require provenance only; single
  refunds still return the stake, fully refunded parlays still return the stake,
  and mixed parlays still remove refunded-leg odds from the effective product.
  No economic calculation changed.
- Duplicated predicates: historical features and ledger finished settlement use
  the shared boolean predicate; draw training consumes the shared normalized
  result; ledger refund and invalid handling use the shared provenance predicate.
  The obsolete draw goal parser and ledger provenance implementation were
  removed. The remaining ledger `_goal` function computes market outcomes after
  evidence acceptance and is not an eligibility predicate.
- Malformed input: non-dicts, missing or blank proof fields, unapproved sources,
  naive or invalid timestamps, non-finished status, non-90-minute scope, malformed
  goals, and negative goals fail closed. `_result_for` continues to require the
  result payload's match ID to equal the requested canonical ID.
- Legacy behavior: legacy result rows remain readable by storage/display paths
  but are intentionally excluded from history features, draw training, and
  canonical finished settlement.
- Scope: changes are limited to the shared result contract, its three consumers,
  corresponding tests, one affected integration fixture, and this report.
  Import manifest schema version `2`, official/fallback independence, and result
  import logic are unchanged.
- `git diff --check` reported no whitespace errors; only existing Git line-ending
  conversion warnings were emitted.

## Concerns

No implementation concerns identified. The checked-in historical result CSV is
legacy-shaped, so those rows are intentionally ineligible until refreshed with
canonical identity and provenance.
