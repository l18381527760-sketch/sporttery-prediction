# Task 5: Explicit Live Odds Capture Phases Report

Base commit: `88f7e9d321d299b79f6b06febf9c21c43e65aabd`

## Implementation

- Added the live-only allowed phase set: `opening`, `decision`, `monitoring`, `pre_kickoff_90`, and `pre_kickoff_30`.
- Extended `capture_live_snapshot(..., phase="monitoring")`; it rejects an invalid phase before any source fetch.
- Added canonical snapshot `capture_phase` and per-match `capture_phase` plus `minutes_to_kickoff` evidence.
- Calculates minutes from Beijing-aware kickoff and capture instants. Matches at 45 minutes or less are `pre_kickoff_30`; those at 105 minutes or less are `pre_kickoff_90`; all others retain the requested phase.
- Reader validation now requires an allowed snapshot phase, non-negative non-boolean whole minutes matching the recomputed value, and the exact derived per-match phase.
- The capture CLI forwards `--phase` for live snapshots and permits the two pre-kickoff phases only with `--live`.
- Immutable filename calculation, canonical JSON, source/raw-response digest evidence, exclusive create (`xb`), no-overwrite conflict protection, and existing revalidation behavior were not changed.

## RED Evidence

Command:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_live_odds.LiveOddsTest.test_capture_records_requested_and_per_match_phases tests.test_live_odds.LiveOddsTest.test_capture_rejects_invalid_phase_before_fetching -v
```

Result: both tests errored with `TypeError: capture_live_snapshot() got an unexpected keyword argument 'phase'`.

Command:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_capture_odds_snapshot.CaptureOddsSnapshotCliTest.test_live_flag_delegates_to_immutable_live_capture_and_prints_path -v
```

Result: errored with `KeyError: 'phase'` because the live CLI did not forward the option.

## GREEN And Regression Evidence

- New phase and CLI tests: 3 passed.
- Focused live/revalidation/pre-kickoff suite: 57 passed.
- Betting-ledger consumer regression suite: 89 passed.
- Full Python suite:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests
```

Result: 710 passed.

- Exact Node suite:

```powershell
& 'C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --test tests/apps_script_orchestrator.test.mjs
```

Result: 63 passed, 0 failed.

## Files

- `live_odds.py`
- `capture_odds_snapshot.py`
- `tests/test_live_odds.py`
- `tests/test_capture_odds_snapshot.py`
- `tests/test_revalidation.py`
- `tests/test_pre_kickoff_rehearsal.py`
- `tests/test_betting_ledger.py`

## Self-Review

- Confirmed phase validation is before live source fetchers are invoked.
- Confirmed read validation rejects stale, negative, boolean, or mismatched minute evidence and mismatched match phases.
- Confirmed immutable filenames and existing overwrite behavior remain unchanged.
- Updated revalidation and pre-kickoff fixture builders so their immutable test payloads satisfy the new live evidence contract without changing consumer logic.

## Concerns

None. Phase-less live snapshot payloads are intentionally rejected by the newly phase-aware validator.
