# Value Portfolio and Immutable Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select only verified positive-value Sporttery markets, size simulated stakes with quarter Kelly under hard exposure limits, allow only approved singles and at most 2-leg parlays, and settle every locked bet exactly once in an auditable immutable ledger.

**Architecture:** Normalize domestic official markets into one typed candidate schema, derive market-specific model probabilities from the existing forecast/xG outputs, and pass candidates through one pure portfolio allocator. Lock the selected plan at decision time, append stable `bet_id` rows to a ledger, and permit only explicit settlement-state transitions. Introduce the new allocator in shadow mode before activating `value-v4` for new dates.

**Tech Stack:** Python 3.12, standard-library dataclasses/CSV/JSON/hashlib/math, unittest, existing prediction and calibration modules, GitHub Actions.

## Global Constraints

- Phase 1 must be deployed and pass one complete Beijing business-day rehearsal before Phase 2 activation.
- This remains simulation only. `real_money_automation` must stay `false`.
- Plan odds must come from Sporttery or a verified domestic Sporttery fallback; external professional markets are analysis evidence only.
- Allowed paid plays are win/draw/loss single, handicap win/draw/loss single, total-goals single, and 2-leg parlay.
- Score, half/full-time, and all 3-or-more-leg parlays are forbidden.
- Every candidate must have a verified match ID, pre-match locked odds, supported market identity, medium-or-high data quality, positive conservative edge, and positive configured EV.
- Use quarter Kelly (`0.25 * full Kelly`) in both strict and normal modes; strict mode remains conservative through higher edge/EV gates and lower exposure caps.
- Round every stake down to a multiple of 2 yuan. A result below 2 yuan becomes no bet.
- Aggregate exposure for one match across all paid selections must be at most 200 yuan.
- Total parlay stake per day must be at most 30 yuan.
- Total new simulated stake per day must be at most 500 yuan.
- Total simulated stake per calendar month must be at most 5000 yuan.
- Stop new simulated bets for the month when settled realized monthly profit is at most `-5000` yuan.
- Pending stakes consume monthly budget but do not count as realized profit or loss.
- Unused limits never roll forward and never force a bet.
- A rerun must reuse the first valid lock and must not duplicate a plan, a ledger row, a return, or profit.
- Historical locked odds, probabilities, stake, and model version are never rewritten after settlement data arrives.

## File Structure

- Create `official_markets.py`: de-vigging, Poisson market probabilities, handicap/total-goal settlement identities, and domestic market normalization.
- Create `value_candidates.py`: candidate construction, conservative probability blending, data-quality and odds-volatility controls.
- Create `value_portfolio.py`: quarter-Kelly staking, ranking, correlation rejection, and all hard limits.
- Create `betting_ledger.py`: stable bet IDs, append-only plan ingestion, allowed state transitions, returns, profit, and idempotent settlement.
- Modify `import_sporttery.py`: persist explicit single-play eligibility and normalized domestic source metadata.
- Modify `capture_odds_snapshot.py`: include HAD, HHAD, and TTG decision markets with match IDs.
- Modify `generate_betting_plan.py`: call the new candidate and allocator modules, support shadow mode, and write the expanded plan schema.
- Modify `update_sporttery_results.py`: preserve match ID, source, result status, and capture time.
- Modify `strategy_controls.py` and `betting_config.json`: exact quarter-Kelly and risk limits, always 2-leg parlays.
- Modify Phase 1 workflows to generate and lock the active strategy at the decision phase.
- Create focused tests under `tests/`.

---

### Task 1: Official Market Probability Math

**Files:**
- Create: `official_markets.py`
- Create: `tests/test_official_markets.py`

**Interfaces:**
- `devig(prices: dict[str, float]) -> dict[str, float]`
- `poisson_total_probabilities(xg_home: float, xg_away: float) -> dict[str, float]`
- `poisson_handicap_probabilities(xg_home: float, xg_away: float, handicap: int) -> dict[str, float]`
- `parse_handicap(value: object) -> int`
- `normalize_market(match_id: str, market_type: str, raw: dict) -> OfficialMarket | None`

- [ ] **Step 1: Write failing probability tests**

```python
import unittest

from official_markets import (
    devig,
    parse_handicap,
    poisson_handicap_probabilities,
    poisson_total_probabilities,
)


class OfficialMarketMathTest(unittest.TestCase):
    def test_devig_normalizes_every_valid_outcome(self):
        fair = devig({"胜": 1.90, "平": 3.60, "负": 4.20})
        self.assertAlmostEqual(1.0, sum(fair.values()), places=12)
        self.assertEqual({"胜", "平", "负"}, set(fair))

    def test_total_goals_has_zero_through_six_and_seven_plus(self):
        probabilities = poisson_total_probabilities(1.20, 1.05)
        self.assertEqual(
            {"0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"},
            set(probabilities),
        )
        self.assertAlmostEqual(1.0, sum(probabilities.values()), places=12)

    def test_plus_one_handicap_changes_the_three_way_result(self):
        probabilities = poisson_handicap_probabilities(1.00, 1.40, +1)
        self.assertAlmostEqual(1.0, sum(probabilities.values()), places=9)
        self.assertGreater(probabilities["胜"], 0)
        self.assertGreater(probabilities["平"], 0)
        self.assertGreater(probabilities["负"], 0)

    def test_only_integer_sporttery_handicaps_are_accepted(self):
        self.assertEqual(1, parse_handicap("+1"))
        self.assertEqual(-1, parse_handicap("-1"))
        with self.assertRaises(ValueError):
            parse_handicap("-0.5")
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `python -m unittest tests.test_official_markets -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'official_markets'`.

- [ ] **Step 3: Implement strict price normalization**

`devig()` must reject empty markets, non-finite values, prices `<= 1.0`, and partial markets. Calculate `inverse = 1 / price` and divide by the sum of all inverse prices. Do not round internal probabilities.

- [ ] **Step 4: Implement Poisson total and handicap probabilities**

For total goals, use the exact Poisson distribution with mean `xg_home + xg_away` for totals zero through six and put the remaining probability in `7+球`.

For handicap win/draw/loss, generate independent home and away Poisson vectors through 20 goals, renormalize the finite joint grid, apply the integer handicap to the home score, and aggregate into `胜/平/负`. Reject non-finite xG and xG outside `[0, 8]`.

- [ ] **Step 5: Add normalized market dataclass tests**

Use this immutable shape:

```python
@dataclass(frozen=True)
class OfficialMarket:
    match_id: str
    market_type: str       # had, hhad, ttg
    line: int | None
    prices: dict[str, float]
    fair_probabilities: dict[str, float]
    source: str
    source_record_id: str
    captured_at_bjt: str
```

Assert `normalize_market()` maps HAD `h/d/a`, HHAD `h/d/a + goalLine`, and TTG `s0..s7` to canonical selections. It must return `None` for a partial three-way market, a missing handicap, or any malformed price.

- [ ] **Step 6: Run focused tests**

Run: `python -m unittest tests.test_official_markets -v`

Expected: PASS.

- [ ] **Step 7: Commit market math**

```bash
git add official_markets.py tests/test_official_markets.py
git commit -m "feat: normalize official sporttery markets"
```

---

### Task 2: Persist Market Eligibility and Decision Snapshots

**Files:**
- Modify: `import_sporttery.py`
- Modify: `capture_odds_snapshot.py`
- Create: `tests/test_official_market_import.py`
- Modify: `tests/test_value_strategy.py`

- [ ] **Step 1: Write failing import-normalization tests**

Cover:

- direct official rows preserve `isSingleHad`, `isSingleHhad`, and `isSingleTtg` independently;
- ZGZCW `dg="1"` proves only HAD single eligibility and never guesses HHAD/TTG eligibility;
- `fixtures.csv` contains `is_single_had`, `is_single_hhad`, and `is_single_ttg`;
- decision snapshots include `match_id`, `markets.had`, `markets.hhad`, `markets.ttg`, `capture_phase`, and `captured_at`;
- a snapshot ignores started matches;
- zero official fixtures returns a valid explicit zero-match snapshot payload instead of returning `None`.

- [ ] **Step 2: Run tests and verify the missing fields**

Run: `python -m unittest tests.test_official_market_import tests.test_value_strategy -v`

Expected: FAIL on missing single-market flags and missing snapshot markets.

- [ ] **Step 3: Normalize explicit single-play flags**

Add:

```python
SINGLE_ELIGIBILITY_KEYS = {
    "had": "isSingleHad",
    "hhad": "isSingleHhad",
    "ttg": "isSingleTtg",
}
```

Only a literal boolean true or normalized string `true/1/yes` counts as eligible. Missing values are false. Never infer eligibility merely because odds exist.

- [ ] **Step 4: Expand the decision snapshot schema**

`capture()` must accept injectable `matches` and `odds_by_match` for tests, and in production load the freshly written `data/sporttery_odds_YYYY-MM-DD.json` after fetching the match list. Each row must use:

```python
{
    "match_id": match_id,
    "team_a": team_a,
    "team_b": team_b,
    "match_num": match_num,
    "kickoff_at": kickoff,
    "capture_phase": phase,
    "minutes_to_kickoff": minutes_to_kickoff,
    "markets": {
        "had": odds.get("had", {}),
        "hhad": odds.get("hhad", {}),
        "ttg": odds.get("ttg", {}),
    },
    "single_eligibility": {
        "had": is_single_had,
        "hhad": is_single_hhad,
        "ttg": is_single_ttg,
    },
}
```

Write a snapshot even when `matches` is empty so Phase 1 can distinguish a verified zero-match day from a failed fetch.

- [ ] **Step 5: Run import and snapshot tests**

Run: `python -m unittest tests.test_official_market_import tests.test_value_strategy -v`

Expected: PASS.

- [ ] **Step 6: Commit source normalization**

```bash
git add import_sporttery.py capture_odds_snapshot.py tests/test_official_market_import.py tests/test_value_strategy.py
git commit -m "feat: capture eligible sporttery markets at decision time"
```

---

### Task 3: Build a Unified Value Candidate Pool

**Files:**
- Create: `value_candidates.py`
- Create: `tests/test_value_candidates.py`

**Interfaces:**
- `build_candidates(predictions: list[dict], odds_by_match: dict, snapshot: dict, config: dict, league_calibrations: dict) -> list[ValueCandidate]`
- `odds_volatility(opening_price: float | None, decision_price: float) -> OddsRisk`
- `conservative_probability(model: float, market: float, model_weight: float) -> float`

- [ ] **Step 1: Write failing candidate tests**

Use this core immutable shape:

```python
@dataclass(frozen=True)
class ValueCandidate:
    candidate_id: str
    date: str
    match_id: str
    stage: str
    team_a: str
    team_b: str
    kickoff_at: str
    market_type: str
    play: str
    selection: str
    line: int | None
    official_odds: float
    official_market_probability: float
    raw_model_probability: float
    calibrated_model_probability: float
    conservative_probability: float
    probability_edge: float
    expected_value: float
    single_eligible: bool
    data_quality: str
    data_quality_multiplier: float
    volatility_band: str
    volatility_multiplier: float
    odds_source: str
    source_record_id: str
    captured_at_bjt: str
    correlation_tags: tuple[str, ...]
```

Test that:

- HAD uses forecast `p_a/p_draw/p_b` and league draw calibration only for the draw selection;
- HHAD uses Poisson handicap probabilities from `xg_a/xg_b` and the official integer line;
- TTG uses Poisson total probabilities;
- every market probability is de-vigged within its own market;
- expected value is `conservative_probability * official_odds - 1`;
- missing domestic odds, unmatched IDs, started matches, low data quality, and unsupported markets produce no candidate;
- an offered selection can be a parlay leg even when it is not single-eligible, but it cannot become a paid single;
- external consensus odds never replace `official_odds`.

- [ ] **Step 2: Run tests and verify the module is missing**

Run: `python -m unittest tests.test_value_candidates -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'value_candidates'`.

- [ ] **Step 3: Implement conservative probability layers**

Preserve four distinct values. Apply the existing sample-gated model weight:

```python
conservative = market_probability + model_weight * (
    calibrated_model_probability - market_probability
)
```

Clamp only to `[0.001, 0.999]`; never overwrite raw or calibrated probability fields.

- [ ] **Step 4: Implement data-quality and volatility controls**

Use these exact multipliers:

- verified direct domestic source plus matching decision snapshot: `high`, multiplier `1.0`;
- verified domestic fallback or missing opening snapshot with a valid decision snapshot: `medium`, multiplier `0.6`;
- missing/conflicting domestic identity or missing decision snapshot: `low`, excluded.

Compare opening and decision decimal prices with `abs(log(decision / opening))`:

- `<= 0.08`: `stable`, multiplier `1.0`;
- `> 0.08` and `<= 0.20`: `volatile`, multiplier `0.75`;
- `> 0.20` without a documented independent explanation: `unverified_jump`, excluded.

- [ ] **Step 5: Apply configured value gates without staking**

The candidate builder records all valid research candidates, but marks paid eligibility only when probability edge and EV clear the strict/normal thresholds. It must not calculate a stake or consume a budget.

- [ ] **Step 6: Run focused tests**

Run: `python -m unittest tests.test_official_markets tests.test_value_candidates -v`

Expected: PASS.

- [ ] **Step 7: Commit candidate construction**

```bash
git add value_candidates.py tests/test_value_candidates.py
git commit -m "feat: build unified positive-value candidate pool"
```

---

### Task 4: Quarter-Kelly Portfolio Allocation

**Files:**
- Create: `value_portfolio.py`
- Create: `tests/test_value_portfolio.py`

**Interfaces:**
- `full_kelly(probability: float, odds: float) -> float`
- `stake_for(candidate: ValueCandidate, bankroll: float, kelly_fraction: float) -> int`
- `build_two_leg_candidates(candidates: list[ValueCandidate], config: dict) -> list[ParlayCandidate]`
- `allocate_portfolio(candidates: list[ValueCandidate], limits: PortfolioLimits, account: dict) -> Portfolio`

- [ ] **Step 1: Write failing Kelly tests**

```python
class KellyTest(unittest.TestCase):
    def test_quarter_kelly_is_applied_before_quality_multipliers(self):
        candidate = sample_candidate(
            conservative_probability=0.60,
            official_odds=2.00,
            data_quality_multiplier=0.60,
            volatility_multiplier=0.75,
        )
        # Full Kelly = 0.20; 5000 * .20 * .25 * .60 * .75 = 112.5.
        self.assertEqual(112, stake_for(candidate, 5000, 0.25))

    def test_nonpositive_edge_has_zero_stake(self):
        candidate = sample_candidate(conservative_probability=0.40, official_odds=2.00)
        self.assertEqual(0, stake_for(candidate, 5000, 0.25))

    def test_stake_rounds_down_to_two_yuan(self):
        candidate = sample_candidate(conservative_probability=0.57, official_odds=2.00)
        self.assertEqual(174, stake_for(candidate, 5000, 0.25))

    def test_stake_below_two_yuan_is_zero(self):
        candidate = sample_candidate(conservative_probability=0.51, official_odds=2.00)
        self.assertEqual(0, stake_for(candidate, 10, 0.25))
```

- [ ] **Step 2: Write failing hard-limit and parlay tests**

Cover:

- strict and normal modes both use Kelly fraction `0.25`;
- strict exposure caps still reduce the final amount;
- all selections from one match sum to at most 200;
- at most one paid single is selected from a match;
- no more than two paid singles are selected under the retained conservative `max_single_count=2` control;
- total daily stake is at most 500;
- available monthly budget can reduce a final stake and is rounded down to 2;
- realized monthly loss at `-5000` returns an empty paid portfolio;
- no bet is forced when all EVs are nonpositive;
- a parlay has exactly two distinct match IDs;
- parlay legs do not reuse a match already selected as a paid single;
- both legs independently clear leg edge, EV, probability, and quality gates;
- intersecting correlation tags other than the neutral league tag reject a pair;
- combined parlay EV remains positive;
- total parlay stake is at most 30;
- no score, half/full, or 3-leg output can be constructed.

- [ ] **Step 3: Run tests and verify the allocator is missing**

Run: `python -m unittest tests.test_value_portfolio -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'value_portfolio'`.

- [ ] **Step 4: Implement Kelly stake calculation**

Use:

```python
def full_kelly(probability: float, odds: float) -> float:
    if not 0 < probability < 1 or odds <= 1:
        return 0.0
    b = odds - 1.0
    return max(0.0, (b * probability - (1.0 - probability)) / b)


raw = (
    bankroll
    * full_kelly(candidate.conservative_probability, candidate.official_odds)
    * 0.25
    * candidate.data_quality_multiplier
    * candidate.volatility_multiplier
    * candidate.performance_multiplier
)
stake = int(raw // 2) * 2
```

The final allocator, not `stake_for()`, applies match, play, daily, and monthly remaining caps so all constraints are checked together.

- [ ] **Step 5: Implement deterministic portfolio ranking**

Sort paid single candidates by:

1. positive expected log growth after multipliers;
2. conservative EV;
3. calibration reliability/sample count;
4. data quality;
5. lower volatility;
6. stable `candidate_id` as the final tie breaker.

Track match exposure, paid selection identity, daily remaining budget, and monthly remaining budget while allocating. If a cap truncates an amount, round down to 2; drop zero stakes.

- [ ] **Step 6: Implement one best 2-leg parlay**

Build pairs only after single allocation, exclude every match already used by a paid single, and choose at most one pair. Calculate combined probability and odds as products, then quarter Kelly on the combined position. Cap the resulting stake by 30 yuan, daily remaining budget, and monthly remaining budget.

- [ ] **Step 7: Run allocator tests**

Run: `python -m unittest tests.test_value_portfolio -v`

Expected: PASS.

- [ ] **Step 8: Commit the allocator**

```bash
git add value_portfolio.py tests/test_value_portfolio.py
git commit -m "feat: allocate quarter-kelly simulated portfolio"
```

---

### Task 5: Immutable Bet IDs and Settlement Ledger

**Files:**
- Create: `betting_ledger.py`
- Create: `tests/test_betting_ledger.py`
- Modify: `update_sporttery_results.py`
- Modify: `tests/test_update_sporttery_results.py`

**Interfaces:**
- `stable_bet_id(plan_row: dict) -> str`
- `ingest_locked_plan(existing_rows: list[dict], plan_rows: list[dict], lock: dict) -> list[dict]`
- `settle_pending(rows: list[dict], results: dict, settled_at: datetime) -> list[dict]`
- `write_ledger_atomic(path: Path, rows: list[dict]) -> Path`

- [ ] **Step 1: Write failing identity and immutability tests**

Assert:

- identical canonical plan rows produce the same 64-character `bet_id`;
- parlay leg order does not change `bet_id`;
- rerunning plan ingestion does not append a second row;
- a later plan with changed odds cannot overwrite the first locked odds for the same canonical bet identity;
- settlement changes only status, official result fields, return, profit, result source, and settlement time;
- a second settlement pass is byte-for-byte idempotent;
- pending rows remain pending when result data is absent or conflicted;
- legacy ledger rows receive deterministic migration IDs without losing fields.

- [ ] **Step 2: Write failing settlement tests for every allowed play**

Cover 90-minute HAD, `+1` and `-1` HHAD, each TTG bucket including `7+球`, 2-leg win, 2-leg loss, one refunded leg at effective odds `1.0`, and full refund. Expected formulas:

```text
single win return = stake * locked_odds
single loss return = 0
parlay win return = stake * product(effective_leg_odds)
profit = return - stake
```

All money fields retain two decimal places.

- [ ] **Step 3: Run tests and verify the module is missing**

Run: `python -m unittest tests.test_betting_ledger -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'betting_ledger'`.

- [ ] **Step 4: Implement canonical IDs and append-only ingestion**

Hash canonical UTF-8 JSON with sorted keys. Identity fields are report date, strategy version, play, market type, selection/line, and sorted leg identities containing match ID, market type, selection, and line. Deliberately exclude odds, stake, probabilities, and settlement data from identity so reruns cannot create a duplicate merely because a mutable input changed.

Persist at least the fields required by design: lock time, match IDs, teams, kickoff, play, selection/line, source and locked odds, four probability layers, edge, EV, Kelly fractions/multipliers, stake, strategy/model versions, data quality, status, 90-minute result, result source/time, return, profit, and CLV.

- [ ] **Step 5: Implement explicit state transitions**

Allowed transitions:

```python
ALLOWED_TRANSITIONS = {
    "待结算": {"命中", "未中", "退款", "异常"},
    "命中": set(),
    "未中": set(),
    "退款": set(),
    "异常": {"待结算"},  # only after corrected source data is supplied
}
```

Do not silently reopen a settled row. Require a separate `--allow-correction` CLI flag plus a changed result-source record ID to move `异常` back to `待结算`.

- [ ] **Step 6: Expand result provenance**

Modify `update_sporttery_results.py` to write `match_id`, `result_status`, `result_source`, `source_record_id`, and `captured_at_bjt`. Preserve old rows during schema migration. Official finished status must be explicit; a missing score or source conflict remains unavailable rather than guessed.

- [ ] **Step 7: Run ledger and result tests**

Run: `python -m unittest tests.test_betting_ledger tests.test_update_sporttery_results -v`

Expected: PASS.

- [ ] **Step 8: Commit ledger primitives**

```bash
git add betting_ledger.py update_sporttery_results.py tests/test_betting_ledger.py tests/test_update_sporttery_results.py
git commit -m "feat: add immutable idempotent betting ledger"
```

---

### Task 6: Integrate Shadow Portfolio and Exact Risk Configuration

**Files:**
- Modify: `generate_betting_plan.py`
- Modify: `strategy_controls.py`
- Modify: `betting_config.json`
- Modify: `tests/test_value_strategy.py`
- Modify: `tests/test_strategy_controls.py`
- Create: `tests/test_value_strategy_integration.py`

- [ ] **Step 1: Update failing configuration and control tests**

Require:

```json
{
  "strategy_version": "value-v4",
  "max_daily_budget": 500,
  "value_strategy": {
    "activation_mode": "shadow",
    "strict_min_ev": 0.06,
    "min_ev": 0.03,
    "strict_min_combo_leg_ev": 0.02,
    "min_combo_leg_ev": 0.01,
    "strict_min_combo_ev": 0.10,
    "min_combo_ev": 0.03,
    "strict_kelly_fraction": 0.25,
    "kelly_fraction": 0.25,
    "reference_bankroll": 5000,
    "stake_unit": 2,
    "max_match_exposure": 200,
    "max_single_count": 2,
    "combo_min_legs": 2,
    "combo_max_legs": 2,
    "max_daily_combo_stake": 30
  },
  "simulation_account": {
    "mode": "simulation",
    "monthly_budget_cap": 5000,
    "monthly_stop_loss": 5000,
    "real_money_automation": false
  }
}
```

Delete `three_leg_min_settled_days` and `three_leg_value_premium`. Change `combo_leg_limit()` to always return `2` for valid configuration and reject any configured maximum other than 2.

Remove gross-return threshold keys `strict_min_expected_return`, `min_expected_return`, `strict_min_combo_leg_expected_return`, `min_combo_leg_expected_return`, `strict_min_combo_expected_return`, and `min_combo_expected_return`. In v4, `expected_return = probability * odds` and net `expected_value = expected_return - 1`; all new threshold keys above use the net value.

- [ ] **Step 2: Run control tests and verify failure**

Run: `python -m unittest tests.test_strategy_controls tests.test_value_strategy -v`

Expected: FAIL on old `0.125`, 3000/500 monthly limits, and 3-leg behavior.

- [ ] **Step 3: Refactor plan generation behind explicit strategy functions**

Rename the current implementation to `build_legacy_value_plan()`. Add the exact interfaces `build_value_v4_plan(target_date: date, *, locked_at: datetime) -> tuple[list[dict], list[dict]]` and `build_strategy_outputs(target_date: date, *, locked_at: datetime) -> StrategyOutputs`. The first loads predictions, official decision markets, calibrations, governance state, and account limits before calling `build_candidates()` and `allocate_portfolio()`; the second selects shadow versus active outputs solely from `activation_mode`.

`StrategyOutputs` contains `active_plan`, `observations`, `shadow_plan`, and an audit dictionary. In shadow mode, the legacy plan remains the only paid ledger input and `output/shadow_betting_plan_YYYY-MM-DD.csv` receives v4. In active mode, v4 is the paid plan and the legacy comparison becomes optional research output.

- [ ] **Step 4: Expand the plan schema**

Add `bet_id`, `match_id`, `market_type`, `market_line`, `locked_at_bjt`, `odds_source`, `odds_source_record_id`, `odds_captured_at_bjt`, `locked_odds`, `data_quality`, `volatility_band`, `full_kelly`, `kelly_fraction`, `data_quality_multiplier`, `volatility_multiplier`, `model_version`, and `portfolio_rank`. Keep old columns required by the website until Phase 3 migrates its readers.

- [ ] **Step 5: Route all ledger writes through `betting_ledger.py`**

Replace the current rebuild-from-all-plan-files behavior. Normal generation writes plan/observation/decision artifacts but does not touch the paid ledger. After `plan_lock.py` publishes a valid lock, `python betting_ledger.py ingest --date YYYY-MM-DD` ingests that plan, preserves existing rows, and atomically writes the ledger. `--settle-only` must not regenerate plans.

- [ ] **Step 6: Add shadow audit output**

Write `output/shadow_portfolio_audit_YYYY-MM-DD.json` containing candidate counts by market, rejection reasons, selected shadow stakes, every risk-limit check, and a comparison with the active plan. Never include the shadow stake in paid daily/monthly totals or profit.

- [ ] **Step 7: Run integration tests**

Test fixtures must prove:

- one HAD, one HHAD, and one TTG candidate can independently qualify;
- unsupported plays never enter the plan;
- shadow mode writes no v4 paid ledger rows;
- active mode writes only v4 rows;
- zero candidates produce a valid no-bet decision and zero paid stake;
- a locked rerun preserves odds and bet IDs;
- daily/monthly/match/parlay limits remain intact after integration.

Run: `python -m unittest tests.test_value_strategy tests.test_strategy_controls tests.test_value_strategy_integration -v`

Expected: PASS.

- [ ] **Step 8: Commit shadow integration**

```bash
git add generate_betting_plan.py strategy_controls.py betting_config.json tests/test_value_strategy.py tests/test_strategy_controls.py tests/test_value_strategy_integration.py
git commit -m "feat: run value-v4 portfolio in shadow mode"
```

---

### Task 7: Shadow Audit and Controlled Activation

**Files:**
- Create: `audit_shadow_portfolio.py`
- Create: `tests/test_shadow_portfolio_audit.py`
- Modify: `betting_config.json`
- Modify: `README.md`

- [ ] **Step 1: Write failing audit-gate tests**

The gate must fail when any audited day has a forbidden play, 3-leg parlay, non-domestic locked price, nonpositive configured EV, non-2-yuan stake, match exposure over 200, parlay stake over 30, daily stake over 500, monthly stake over 5000, or duplicate canonical identity. It must not require positive historical ROI to prove mechanical correctness.

- [ ] **Step 2: Run tests and verify the audit module is missing**

Run: `python -m unittest tests.test_shadow_portfolio_audit -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'audit_shadow_portfolio'`.

- [ ] **Step 3: Implement a deterministic recent-date audit**

CLI:

```bash
python audit_shadow_portfolio.py --from 2026-07-11 --through 2026-07-16
```

Read saved predictions, domestic odds, fixtures, and decision snapshots. Do not use post-match scores to select candidates. Write `output/shadow_portfolio_activation_audit.json` with `passed`, checked dates, counts, limit maxima, rejected violations, and source coverage.

- [ ] **Step 4: Run the audit in shadow mode**

Run the unit suite first, then run the CLI over every repository date with complete pre-match artifacts. Expected: exit 0 and `"passed": true`. If a date lacks a required decision snapshot, record it as excluded for missing pre-match evidence; do not fabricate one from later odds.

- [ ] **Step 5: Activate only new plans**

After the mechanical audit passes and the Phase 1 plan lock remains valid, change only:

```json
"activation_mode": "active"
```

Do not rewrite old plan CSVs or old ledger rows. Add a test proving historical rows retain their original `strategy_version`.

- [ ] **Step 6: Run the complete strategy suite**

```bash
python -m unittest tests.test_official_markets tests.test_official_market_import tests.test_value_candidates tests.test_value_portfolio tests.test_betting_ledger tests.test_value_strategy tests.test_strategy_controls tests.test_value_strategy_integration tests.test_shadow_portfolio_audit -v
python -m unittest discover -s tests -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit activation**

```bash
git add audit_shadow_portfolio.py tests/test_shadow_portfolio_audit.py betting_config.json README.md output/shadow_portfolio_activation_audit.json
git commit -m "feat: activate audited value-v4 simulation strategy"
```

---

### Task 8: Workflow and End-to-End Ledger Verification

**Files:**
- Modify: `.github/workflows/draw-alert-refresh.yml`
- Modify: `.github/workflows/noon-settlement.yml`
- Modify: `tests/test_workflow_schedule.py`
- Modify: `apps-script/README.md`

- [ ] **Step 1: Write failing workflow-order tests**

Require decision refresh to capture official odds, generate the active v4 plan with an explicit `--locked-at`, publish the plan lock, idempotently ingest the locked plan, then build the report. Require settlement to update official results before calling `generate_betting_plan.py --settle-only`. Status publication must remain after the final site/image build.

- [ ] **Step 2: Run the workflow test and verify failure**

Run: `python -m unittest tests.test_workflow_schedule -v`

Expected: FAIL until the explicit lock timestamp and immutable ingestion order are present.

- [ ] **Step 3: Update the decision workflow**

Generate one `LOCKED_AT_BJT="$(date --iso-8601=seconds)"`, pass it to the plan CLI, and pass the same value to `plan_lock.py`. The required order is `generate_betting_plan.py --generate-only`, `plan_lock.py lock`, then `betting_ledger.py ingest`. On rerun, a valid lock bypasses generation but still runs idempotent ingestion so a prior failure between lock and ingest can recover. An invalid hash fails the workflow rather than silently replacing the lock.

- [ ] **Step 4: Update settlement workflow and documentation**

Settlement retries may update only pending ledger rows. Document that the 5000 monthly stake cap and 5000 realized-loss stop are separate controls even though the stake cap usually triggers first.

- [ ] **Step 5: Run two identical local business-day passes**

Using a temporary workspace and fixed date/time:

1. generate, lock, and ingest one plan;
2. capture the ledger bytes and bet IDs;
3. rerun generation/ingestion with changed mocked source odds;
4. assert ledger row count, locked odds, stake, and bet IDs are unchanged;
5. settle with fixed 90-minute results;
6. rerun settlement and assert byte-identical ledger output.

- [ ] **Step 6: Run complete verification**

```bash
python -m py_compile official_markets.py value_candidates.py value_portfolio.py betting_ledger.py generate_betting_plan.py update_sporttery_results.py
python -m unittest discover -s tests -v
node --test tests/apps_script_orchestrator.test.mjs
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 7: Commit workflow integration**

```bash
git add .github/workflows/draw-alert-refresh.yml .github/workflows/noon-settlement.yml tests/test_workflow_schedule.py apps-script/README.md
git commit -m "feat: lock and settle value portfolio idempotently"
```
