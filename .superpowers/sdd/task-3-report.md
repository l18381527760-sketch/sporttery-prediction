# Phase 2 Task 3 Report

## Files Changed

- `value_candidates.py`: added the immutable candidate and odds-risk models plus candidate construction for official HAD, HHAD, and TTG markets.
- `tests/test_value_candidates.py`: added focused coverage for probability layers, eligibility separation, identity and quality rejection, volatility controls, and domestic odds preservation.

## RED Evidence

Initial command:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_value_candidates -v
```

Result: exit code 1 with the expected `ModuleNotFoundError: No module named 'value_candidates'`.

Two later regression cycles also observed expected RED failures before their minimal fixes:

- An externally constructed non-domestic `OfficialMarket` was accepted despite matching prices.
- A mismatched opening snapshot incorrectly upgraded direct official odds to `high` quality.

## GREEN Result

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_official_markets tests.test_value_candidates -v
Ran 21 tests in 0.004s
OK
```

Also passed:

```text
.\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile value_candidates.py tests\test_value_candidates.py
git diff --check
```

## Commit

Implementation commit: `1db2fe3` (`feat: build unified positive-value candidate pool`).

## Self-Review

- `ValueCandidate` is frozen and contains the resolved `paid_eligible`, `value_gate_reasons`, `calibration_samples`, and `performance_multiplier=1.0` fields.
- `paid_eligible` only records probability-edge and EV gates; it stays independent from Task 2's official `single_eligible` flag and no stake or budget data is calculated.
- Official prices come only from trusted domestic `OfficialMarket` objects and match the decision snapshot's exact match ID, identity, market, line, and prices. External consensus data is ignored.
- HAD retains raw, calibrated, market, and conservative probabilities separately; league calibration applies only to draws. HHAD and TTG use the Task 1 Poisson helpers.
- Started, unsupported, malformed, identity-conflicting, missing-decision, non-domestic, and unverified-jump markets are excluded. Missing or mismatched opening evidence is medium quality rather than high.

## Concerns

No code concerns found. Callers that want `high` quality must supply a same-identity opening record through `snapshot["opening_matches"]` (or `snapshot["opening"]["matches"]`); the Task 2 decision-only payload remains valid and intentionally produces `medium` quality.

## Review-Fix Evidence

### RED

Before the fixes, the focused regression command was:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_value_candidates -v
```

It completed with exit code 1:

```text
Ran 15 tests in 0.009s
FAILED (failures=3, errors=5)
TypeError: can't compare offset-naive and offset-aware datetimes
AssertionError: 0.28695652173913044 != 0.3 within 7 places
AssertionError: 'medium' != 'high'
```

Those failures covered the naive Beijing kickoff versus aware decision-capture comparison, the draw-calibration sample fallback changing the global gate/weight, and bare or malformed opening evidence being accepted as high quality or used for volatility.

### GREEN

After the fixes:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_official_markets tests.test_value_candidates -v
Ran 28 tests in 0.009s
OK

.\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile value_candidates.py tests\test_value_candidates.py
exit code 0

git diff --check
exit code 0
```

### Review-Fix Commit

`8e3acbd` (`fix: harden value candidate evidence gates`).

### Review-Fix Scope

- Naive timestamps are interpreted as Beijing time; aware timestamps retain their supplied offset, and invalid values exclude candidates without raising.
- All candidate model weights and strict/normal value gates use one nonnegative `value_strategy.settled_samples` value, defaulting to `0`; draw calibration samples remain a candidate reliability field.
- High quality now requires a complete embedded `snapshot["opening"]` payload with the direct `sporttery` source, opening phase, valid pre-decision/pre-kickoff capture time, matching identity/market/handicap line, and complete valid prices. Invalid opening material remains medium and cannot influence volatility.
- Cross-artifact opening assembly remains deferred to Task 6.

### Review-Fix Concerns

No code concerns found. The earlier `opening_matches` concern is superseded: a bare list is now intentionally treated as missing opening evidence and remains medium quality; assembling separate snapshot artifacts is Task 6 work.
