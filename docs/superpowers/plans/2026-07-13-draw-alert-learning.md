# Draw Alert Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add zero to four daily value-gated 90-minute draw alerts, split into cold-draw and balanced-draw subtypes, with independent settlement, reporting, and guarded long-term learning.

**Architecture:** Keep the existing forecast and betting-plan pipeline intact. Add focused modules for market evidence, pure draw classification, alert generation, settlement metrics, and champion/challenger learning; exchange versioned JSON/CSV files so a failed optional source cannot block the daily report.

**Tech Stack:** Python 3.12, standard-library CSV/JSON/urllib, scikit-learn logistic regression and time-series validation, Pillow, unittest, GitHub Actions, GitHub Pages.

## Global Constraints

- Start execution from `origin/main`, then carry the approved design and this plan onto the feature branch; local `daily-forecast.yml` is stale while `origin/main` already uses 12:15 Beijing time.
- Display and settle plans with Sporttery odds; use ZGZCW Sporttery data only as the domestic fallback.
- External professional markets provide analysis evidence and never replace the saved domestic plan price.
- Compare only 90-minute win/draw/loss markets; never mix qualification, extra time, or penalties into the draw label.
- Publish at most four draw alerts per day, at most two from the same league, and publish none when probability, expected value, or data-quality gates fail.
- Require calibrated draw probability `>= 0.27`, edge over the de-vigged domestic draw probability `>= 0.04`, and `probability * domestic_draw_odds >= 1.05`.
- Apply progressively stricter rank gates: `29%/5pp/1.07` for the second alert, `31%/6pp/1.09` for the third, and `33%/7pp/1.11` for the fourth.
- Cold draws and balanced draws each remain zero-additional-stake observations until that subtype has 30 settled alerts and passes every promotion gate.
- A draw alert that overlaps the main plan must reuse the main stake and settlement; it must never add a second stake or duplicate profit.
- Cap total additional draw-alert stake at 80 yuan per day before applying the 500-yuan overall cap.
- Total daily additional simulated stake must remain `<= 500` yuan.
- Generate the base forecast at 12:15, refresh the draw alert around 13:30, settle at 13:45, and email the rebuilt report at 14:00 Beijing time.
- Model training must be time ordered, reproducible, versioned, reversible, and free of post-match data leakage.
- No component may claim guaranteed profit.

## File Structure

- Create `draw_alert_core.py`: pure probability normalization, data classes, subtype classification, and candidate ranking.
- Create `collect_market_heat.py`: build timestamped same-scope market evidence from Sporttery snapshots, ZGZCW professional odds, and optional Polymarket public data.
- Create `generate_draw_alert.py`: join predictions, evidence, main plan, and subtype metrics; write zero to four ranked daily alerts.
- Create `draw_alert_ledger.py`: settle alerts on 90-minute scores, calculate independent subtype gates, and write the ledger and metrics.
- Create `draw_model_learning.py`: build chronological samples, train/evaluate champion and challenger models, maintain registry state, and supply calibrated draw probabilities.
- Modify `betting_config.json`: add exact draw-alert thresholds, stake limits, feature version, and learning gates.
- Modify `build_site.py` and `build_daily_image.py`: render the same alert, data timestamp, evidence, overlap state, and model status.
- Modify GitHub workflows: collect at 12:15, refresh at 13:30, settle/train at 13:45, and install pinned Python dependencies.
- Create focused tests under `tests/`; keep existing value-strategy tests unchanged except where a shared fixture is useful.

---

### Task 1: Pure Draw-Alert Domain Rules

**Files:**
- Create: `draw_alert_core.py`
- Create: `tests/test_draw_alert_core.py`

**Interfaces:**
- Consumes: normalized fixture/prediction dictionaries containing `p_a`, `p_draw`, `p_b`, `xg_a`, `xg_b`, `stage`, and market evidence.
- Produces: `fair_probabilities(home_odds, draw_odds, away_odds) -> tuple[float, float, float]`, `classify_candidate(inputs: DrawInputs, config: dict) -> DrawCandidate | None`, and `rank_candidates(candidates: list[DrawCandidate]) -> list[DrawCandidate]`.

- [ ] **Step 1: Write failing probability and classification tests**

```python
import unittest

from draw_alert_core import MarketEvidence, DrawInputs, classify_candidate, fair_probabilities, rank_candidates


CFG = {
    "min_draw_probability": 0.27,
    "min_draw_edge": 0.04,
    "min_expected_value": 1.05,
    "max_xg_total": 2.50,
    "cold_favorite_probability": 0.55,
    "balanced_max_win_gap": 0.10,
    "balanced_max_xg_total": 2.35,
}


def sample(**changes):
    values = dict(
        match_id="001", team_a="A", team_b="B", stage="quarter-final",
        domestic_odds=(1.60, 4.00, 6.00), model_probabilities=(0.54, 0.32, 0.14),
        calibrated_draw_probability=0.32, xg_total=2.10, source_count=3,
        market_sources=(
            MarketEvidence("sporttery", "win_draw_loss", 90, False),
            MarketEvidence("zgzcw", "win_draw_loss", 90, False),
        ),
        market_scope="90m", favorite_movement=-0.06, regional_gap=0.07,
        underdog_win_probability=0.14, underdog_not_lose_probability=0.46,
        structural_signals=("knockout_caution", "underdog_defense"), data_quality="high",
    )
    values.update(changes)
    return DrawInputs(**values)


class DrawAlertCoreTest(unittest.TestCase):
    def test_fair_probabilities_remove_overround(self):
        fair = fair_probabilities(1.90, 3.60, 4.00)
        self.assertAlmostEqual(1.0, sum(fair), places=9)

    def test_norway_england_shape_is_cold_draw(self):
        candidate = classify_candidate(sample(), CFG)
        self.assertEqual("cold_draw", candidate.subtype)

    def test_balanced_low_goal_match_is_balanced_draw(self):
        candidate = classify_candidate(sample(
            stage="K联赛", domestic_odds=(2.70, 3.10, 2.60),
            model_probabilities=(0.33, 0.34, 0.33), calibrated_draw_probability=0.34,
            xg_total=2.05, favorite_movement=-0.01, regional_gap=0.01,
            structural_signals=("low_total", "similar_strength"),
        ), CFG)
        self.assertEqual("balanced_draw", candidate.subtype)

    def test_named_balanced_regressions_use_balanced_path(self):
        for match_id in ("jeju-daejeon", "seoul-gangwon"):
            candidate = classify_candidate(sample(
                match_id=match_id, stage="K联赛", domestic_odds=(2.70, 3.10, 2.60),
                model_probabilities=(0.33, 0.34, 0.33), calibrated_draw_probability=0.34,
                xg_total=2.05, favorite_movement=-0.01, regional_gap=0.01,
                structural_signals=("low_total", "similar_strength"),
            ), CFG)
            self.assertEqual("balanced_draw", candidate.subtype)

    def test_named_knockout_regressions_use_cold_path(self):
        for match_id in ("norway-england", "argentina-switzerland", "argentina-cape-verde", "germany-paraguay"):
            candidate = classify_candidate(sample(match_id=match_id), CFG)
            self.assertEqual("cold_draw", candidate.subtype)

    def test_favorite_risk_does_not_force_a_draw(self):
        self.assertIsNone(classify_candidate(sample(calibrated_draw_probability=0.25), CFG))

    def test_non_90m_market_is_rejected(self):
        self.assertIsNone(classify_candidate(sample(market_scope="qualification"), CFG))

    def test_ranking_prefers_value_then_data_quality(self):
        low = classify_candidate(sample(match_id="low", calibrated_draw_probability=0.31), CFG)
        high = classify_candidate(sample(match_id="high", calibrated_draw_probability=0.34), CFG)
        self.assertEqual("high", rank_candidates([low, high])[0].inputs.match_id)
```

- [ ] **Step 2: Run the new tests and verify the module is missing**

Run: `python -m unittest tests.test_draw_alert_core -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'draw_alert_core'`.

- [ ] **Step 3: Implement the pure domain module**

```python
from dataclasses import dataclass


QUALITY = {"medium": 1, "high": 2}


@dataclass(frozen=True)
class MarketEvidence:
    source: str
    market_type: str
    settlement_minutes: int
    includes_extra_time: bool


@dataclass(frozen=True)
class DrawInputs:
    match_id: str
    team_a: str
    team_b: str
    stage: str
    domestic_odds: tuple[float, float, float]
    model_probabilities: tuple[float, float, float]
    calibrated_draw_probability: float
    xg_total: float
    source_count: int
    market_sources: tuple[MarketEvidence, ...]
    market_scope: str
    favorite_movement: float
    regional_gap: float
    underdog_win_probability: float
    underdog_not_lose_probability: float
    structural_signals: tuple[str, ...]
    data_quality: str


@dataclass(frozen=True)
class DrawCandidate:
    inputs: DrawInputs
    subtype: str
    domestic_draw_probability: float
    draw_edge: float
    expected_value: float
    score: float


def fair_probabilities(home_odds: float, draw_odds: float, away_odds: float) -> tuple[float, float, float]:
    implied = (1 / home_odds, 1 / draw_odds, 1 / away_odds)
    total = sum(implied)
    return tuple(value / total for value in implied)


def classify_candidate(inputs: DrawInputs, config: dict) -> DrawCandidate | None:
    if inputs.market_scope != "90m" or inputs.data_quality not in QUALITY:
        return None
    valid_sources = {
        evidence.source
        for evidence in inputs.market_sources
        if (
            evidence.market_type == "win_draw_loss"
            and evidence.settlement_minutes == 90
            and evidence.includes_extra_time is False
        )
    }
    if len(valid_sources) < 2:
        return None
    fair = fair_probabilities(*inputs.domestic_odds)
    probability = inputs.calibrated_draw_probability
    edge = probability - fair[1]
    expected_value = probability * inputs.domestic_odds[1]
    if probability < config["min_draw_probability"] or edge < config["min_draw_edge"]:
        return None
    if expected_value < config["min_expected_value"] or inputs.xg_total > config["max_xg_total"]:
        return None
    favorite = max(fair[0], fair[2])
    win_gap = abs(fair[0] - fair[2])
    if favorite >= config["cold_favorite_probability"]:
        enough_heat = inputs.favorite_movement <= -0.04 or inputs.regional_gap >= 0.05
        enough_resistance = inputs.underdog_not_lose_probability >= 0.35 and probability > inputs.underdog_win_probability
        subtype = "cold_draw" if enough_heat and enough_resistance and len(inputs.structural_signals) >= 2 else ""
    else:
        subtype = "balanced_draw" if win_gap <= config["balanced_max_win_gap"] and inputs.xg_total <= config["balanced_max_xg_total"] and len(inputs.structural_signals) >= 2 else ""
    if not subtype:
        return None
    score = edge * 4 + (expected_value - 1) * 2 + probability + QUALITY[inputs.data_quality] * 0.02
    return DrawCandidate(inputs, subtype, fair[1], edge, expected_value, score)


def rank_candidates(candidates: list[DrawCandidate]) -> list[DrawCandidate]:
    return sorted(candidates, key=lambda item: (item.score, QUALITY[item.inputs.data_quality]), reverse=True)
```

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest tests.test_draw_alert_core -v`

Expected: 13 tests PASS.

- [ ] **Step 5: Commit the pure rules**

```bash
git add draw_alert_core.py tests/test_draw_alert_core.py
git commit -m "feat: add draw alert domain rules"
```

### Task 2: Timestamped Market-Evidence Collector

**Files:**
- Create: `collect_market_heat.py`
- Create: `tests/test_collect_market_heat.py`
- Modify: `capture_odds_snapshot.py`

**Interfaces:**
- Consumes: `data/fixtures.csv`, `data/sporttery_odds_<date>.json`, and `data/odds_snapshots/*.json`.
- Produces: `data/market_heat_<date>.json` with `captured_at`, per-source 90-minute probabilities, movement, volume when available, source errors, and a `quality` value.

- [ ] **Step 1: Write failing collector tests with mocked public-market data**

```python
import json
import tempfile
import unittest
from pathlib import Path

import collect_market_heat as collector


class MarketHeatCollectorTest(unittest.TestCase):
    def test_build_evidence_keeps_sources_separate(self):
        fixture = {"match_id": "001", "team_a": "Norway", "team_b": "England", "odds_a": "3.8", "odds_draw": "3.6", "odds_b": "1.95", "market_odds_a": "3.9", "market_odds_draw": "3.5", "market_odds_b": "1.90"}
        evidence = collector.build_evidence(fixture, {"open": (3.9, 3.7, 2.0), "latest": (3.8, 3.6, 1.95)}, [])
        self.assertEqual("90m", evidence["market_scope"])
        self.assertEqual(2, evidence["source_count"])
        self.assertIn("domestic_sporttery", evidence["sources"])
        self.assertIn("zgzcw_professional", evidence["sources"])
        self.assertTrue(all(item["market_type"] == "win_draw_loss" for item in evidence["sources"].values()))

    def test_qualification_market_is_not_attached(self):
        market = {"question": "Will England qualify?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.7", "0.3"]'}
        self.assertIsNone(collector.parse_polymarket_90m(market, "Norway", "England"))

    def test_write_payload_records_optional_source_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "heat.json"
            collector.write_payload(path, "2026-07-12", [], ["polymarket: timeout"])
            self.assertEqual(["polymarket: timeout"], json.loads(path.read_text(encoding="utf-8"))["errors"])
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m unittest tests.test_collect_market_heat -v`

Expected: FAIL because `collect_market_heat` does not exist.

- [ ] **Step 3: Implement collector and snapshot schema**

Implement `collect_market_heat.py` with these exact public functions and output fields:

```python
def probability_record(odds: tuple[float, float, float], volume: float | None) -> dict:
    home, draw, away = fair_probabilities(*odds)
    return {
        "market_scope": "90m", "market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False,
        "home_probability": home, "draw_probability": draw, "away_probability": away,
        "volume": volume,
    }


def odds_movement(opening: tuple[float, float, float] | None, latest: tuple[float, float, float] | None) -> float:
    if not opening or not latest:
        return 0.0
    latest_fair = fair_probabilities(*latest)
    favorite = 0 if latest_fair[0] >= latest_fair[2] else 2
    return latest[favorite] / opening[favorite] - 1.0


def parse_polymarket_90m(market: dict, team_a: str, team_b: str) -> dict | None:
    question = str(market.get("question") or "").lower()
    blocked = ("qualify", "advance", "win the world cup", "lift the trophy")
    if any(word in question for word in blocked) or team_a.lower() not in question or team_b.lower() not in question:
        return None
    outcomes = json.loads(market.get("outcomes") or "[]")
    prices = [float(value) for value in json.loads(market.get("outcomePrices") or "[]")]
    mapping = {str(name).lower(): price for name, price in zip(outcomes, prices)}
    draw = next((price for name, price in mapping.items() if name in {"draw", "tie"}), None)
    if draw is None:
        return None
    return {"market_scope": "90m", "market_type": "win_draw_loss", "settlement_minutes": 90, "includes_extra_time": False, "draw_probability": draw, "volume": float(market.get("volume") or 0)}


def build_evidence(fixture: dict, snapshots: dict, polymarket: list[dict]) -> dict:
    sources = {}
    domestic = tuple(float(fixture[key]) for key in ("odds_a", "odds_draw", "odds_b"))
    sources["domestic_sporttery"] = probability_record(domestic, volume=None)
    domestic_fair = fair_probabilities(*domestic)
    favorite = 0 if domestic_fair[0] >= domestic_fair[2] else 2
    regional_gap = 0.0
    if all(fixture.get(key) for key in ("market_odds_a", "market_odds_draw", "market_odds_b")):
        professional = tuple(float(fixture[key]) for key in ("market_odds_a", "market_odds_draw", "market_odds_b"))
        sources["zgzcw_professional"] = probability_record(professional, volume=None)
        regional_gap = domestic_fair[favorite] - fair_probabilities(*professional)[favorite]
    for market in polymarket:
        parsed = parse_polymarket_90m(market, fixture["team_a"], fixture["team_b"])
        if parsed:
            sources["polymarket"] = parsed
            break
    movement = odds_movement(snapshots.get("open"), snapshots.get("latest"))
    return {
        "match_id": fixture["match_id"], "team_a": fixture["team_a"], "team_b": fixture["team_b"],
        "market_scope": "90m", "sources": sources, "source_count": len(sources),
        "favorite_movement": movement, "regional_gap": regional_gap,
        "quality": "high" if len(sources) >= 3 else "medium" if len(sources) >= 2 else "low",
    }


def write_payload(path: Path, target_date: str, matches: list[dict], errors: list[str]) -> Path:
    payload = {
        "target_date": target_date,
        "captured_at": datetime.now(BEIJING).isoformat(),
        "matches": matches,
        "errors": errors,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
```

Use the official public Polymarket search endpoint `https://gamma-api.polymarket.com/public-search` with URL-encoded `q=<team_a> <team_b>` and a 10-second timeout. Record timeout, HTTP, JSON, and matching failures in the payload `errors` array; never synthesize probabilities. Extend `capture_odds_snapshot.py` to retain `market_h`, `market_d`, `market_a`, `market_type="win_draw_loss"`, `settlement_minutes=90`, and `includes_extra_time=false`. Source dictionary keys are stable provider identifiers and Task 3 converts each source record into `MarketEvidence`; snapshots from the same provider never count as additional independent sources.

The collector CLI accepts `--date YYYY-MM-DD` and `--offline`. Offline mode reads only committed fixtures and snapshots and skips external HTTP, which makes the end-to-end smoke test deterministic.

- [ ] **Step 4: Run collector tests and the existing suite**

Run: `python -m unittest tests.test_collect_market_heat tests.test_value_strategy -v`

Expected: all tests PASS and no real network call occurs in tests.

- [ ] **Step 5: Commit the evidence collector**

```bash
git add collect_market_heat.py capture_odds_snapshot.py tests/test_collect_market_heat.py
git commit -m "feat: collect timestamped draw market evidence"
```

### Task 3: Daily Alert Generation and Main-Plan Deduplication

**Files:**
- Create: `generate_draw_alert.py`
- Create: `tests/test_generate_draw_alert.py`
- Modify: `betting_config.json`

**Interfaces:**
- Consumes: daily predictions CSV, domestic odds JSON, market-heat JSON, main betting plan CSV, draw metrics JSON, and optional champion model.
- Produces: `output/draw_alert_<date>.csv` containing zero to four ranked rows with `subtype`, probabilities, value, evidence, data timestamp, `additional_stake`, `linked_main_stake`, `hypothetical_stake`, and `settlement_mode`.

- [ ] **Step 1: Write failing selection and duplicate-stake tests**

```python
import unittest
from generate_draw_alert import attach_stake, select_alerts


class GenerateDrawAlertTest(unittest.TestCase):
    def test_selects_up_to_four_with_progressive_gates(self):
        candidates = [
            {"score": 0.50, "match_id": "A", "stage": "L1", "model_draw_probability": 0.34, "draw_edge": 0.08, "expected_value": 1.12},
            {"score": 0.49, "match_id": "B", "stage": "L1", "model_draw_probability": 0.33, "draw_edge": 0.07, "expected_value": 1.11},
            {"score": 0.48, "match_id": "C", "stage": "L1", "model_draw_probability": 0.32, "draw_edge": 0.07, "expected_value": 1.10},
            {"score": 0.47, "match_id": "D", "stage": "L2", "model_draw_probability": 0.33, "draw_edge": 0.07, "expected_value": 1.12},
            {"score": 0.46, "match_id": "E", "stage": "L3", "model_draw_probability": 0.34, "draw_edge": 0.08, "expected_value": 1.13},
        ]
        selected = select_alerts(candidates)
        self.assertEqual(["A", "B", "D", "E"], [row["match_id"] for row in selected])
        self.assertEqual([1, 2, 3, 4], [row["rank"] for row in selected])

    def test_fourth_alert_must_pass_fourth_gate(self):
        candidates = [{"score": 1 - index / 10, "match_id": str(index), "stage": f"L{index}", "model_draw_probability": 0.32, "draw_edge": 0.06, "expected_value": 1.10} for index in range(5)]
        self.assertEqual(3, len(select_alerts(candidates)))

    def test_overlap_reuses_main_stake_without_additional_money(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{"match_id": "001", "stake": "100", "selection": "平"}]
        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual(100, result["linked_main_stake"])
        self.assertEqual("linked", result["settlement_mode"])

    def test_overlap_without_match_id_uses_date_and_teams(self):
        alert = {"match_id": "001", "date": "2026-07-12", "team_a": "A", "team_b": "B", "subtype": "cold_draw"}
        main = [{"date": "2026-07-12", "team_a": "A", "team_b": "B", "stake": "100", "selection": "平"}]
        result = attach_stake(alert, main, [], {"promoted": True}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual(100, result["linked_main_stake"])
        self.assertEqual("linked", result["settlement_mode"])

    def test_unpromoted_subtype_is_zero_stake_observation(self):
        result = attach_stake({"match_id": "002", "subtype": "balanced_draw"}, [], [], {"promoted": False}, 500, 80, 30)
        self.assertEqual(0, result["additional_stake"])
        self.assertEqual("observation", result["settlement_mode"])

    def test_alert_budget_caps_total_additional_stake_at_80(self):
        existing = [{"additional_stake": 30}, {"additional_stake": 30}]
        result = attach_stake({"match_id": "003", "subtype": "cold_draw"}, [], existing, {"promoted": True}, 500, 80, 30)
        self.assertEqual(20, result["additional_stake"])
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m unittest tests.test_generate_draw_alert -v`

Expected: FAIL because `generate_draw_alert` does not exist.

- [ ] **Step 3: Add exact configuration**

Add this object to `betting_config.json`:

```json
"draw_alert": {
  "feature_version": "draw-v1",
  "min_draw_probability": 0.27,
  "min_draw_edge": 0.04,
  "min_expected_value": 1.05,
  "max_xg_total": 2.5,
  "cold_favorite_probability": 0.55,
  "balanced_max_win_gap": 0.1,
  "balanced_max_xg_total": 2.35,
  "observation_samples_per_subtype": 30,
  "hypothetical_stake": 10,
  "min_promoted_stake": 10,
  "max_promoted_stake": 30,
  "max_alerts": 4,
  "max_per_league": 2,
  "daily_additional_budget": 80,
  "rank_gates": [
    {"min_probability": 0.27, "min_edge": 0.04, "min_expected_value": 1.05},
    {"min_probability": 0.29, "min_edge": 0.05, "min_expected_value": 1.07},
    {"min_probability": 0.31, "min_edge": 0.06, "min_expected_value": 1.09},
    {"min_probability": 0.33, "min_edge": 0.07, "min_expected_value": 1.11}
  ],
  "promotion_roi": 0.05,
  "max_drawdown": 100.0
}
```

- [ ] **Step 4: Implement alert generation**

Implement `select_alerts` and `attach_stake` with progressive gates and the tested linked/observation/standalone states:

```python
RANK_GATES = ((0.27, 0.04, 1.05), (0.29, 0.05, 1.07), (0.31, 0.06, 1.09), (0.33, 0.07, 1.11))


def same_match(alert: dict, row: dict) -> bool:
    if alert.get("match_id") and row.get("match_id"):
        return alert["match_id"] == row["match_id"]
    return (
        alert.get("date") == row.get("date")
        and alert.get("team_a") == row.get("team_a")
        and alert.get("team_b") == row.get("team_b")
    )


def select_alerts(candidates: list[dict], rank_gates=RANK_GATES, max_alerts: int = 4, max_per_league: int = 2) -> list[dict]:
    selected = []
    league_counts = {}
    for candidate in sorted(candidates, key=lambda item: (float(item["score"]), item["match_id"]), reverse=True):
        if len(selected) == max_alerts:
            break
        league = candidate.get("stage") or "未知"
        if league_counts.get(league, 0) >= max_per_league:
            continue
        probability, edge, expected_value = rank_gates[len(selected)]
        if candidate["model_draw_probability"] < probability or candidate["draw_edge"] < edge or candidate["expected_value"] < expected_value:
            continue
        row = {**candidate, "rank": len(selected) + 1}
        selected.append(row)
        league_counts[league] = league_counts.get(league, 0) + 1
    return selected


def attach_stake(alert: dict, main_plan: list[dict], existing_alerts: list[dict], subtype_metrics: dict, daily_budget: int, alert_budget: int, requested_stake: int) -> dict:
    result = dict(alert)
    linked = next((row for row in main_plan if same_match(alert, row) and row.get("selection") == "平"), None)
    result["hypothetical_stake"] = 10
    if linked:
        result.update(additional_stake=0, linked_main_stake=int(float(linked.get("stake") or 0)), settlement_mode="linked")
    elif not subtype_metrics.get("promoted"):
        result.update(additional_stake=0, linked_main_stake=0, settlement_mode="observation")
    else:
        used = sum(int(float(row.get("stake") or 0)) for row in main_plan)
        alert_used = sum(int(float(row.get("additional_stake") or 0)) for row in existing_alerts)
        available = max(0, min(daily_budget - used - alert_used, alert_budget - alert_used))
        stake = min(requested_stake, available)
        state = "standalone" if stake else "budget_capped_observation"
        result.update(additional_stake=stake, linked_main_stake=0, settlement_mode=state)
    return result
```

Use a quarter-Kelly fraction capped to 10-30 yuan only for promoted standalone alerts, allocate in rank order, then apply both the 80-yuan alert cap and `500 - main stakes - earlier alert stakes`. Pass `rank_gates`, `max_alerts`, and `max_per_league` from `betting_config.json` into `select_alerts`. Existing main-plan rows do not always contain `match_id`; deduplicate by non-empty `match_id` when both rows provide it, otherwise by exact `date`, `team_a`, and `team_b`. Build `DrawInputs` from prediction/evidence rows, reject missing domestic draw odds, and write an empty CSV with headers when nothing qualifies. Load `draw_model_learning.predict_draw_probability()` lazily; until Task 5 creates a valid champion model, or when model loading fails, use the existing blended `p_draw` value without interrupting generation.

Derive structural signals before classification with one deterministic function: add `knockout_caution` when `stage` is in the configured knockout stages; add `low_total` when `xg_a + xg_b <= 2.35`; add `similar_strength` when the two de-vigged win probabilities differ by at most 0.10; add `underdog_resistance` when calibrated draw probability exceeds the underdog win probability and underdog non-loss probability is at least 0.35. Do not infer injuries or lineups when no timestamped source exists.

Use this output schema exactly:

```python
FIELDS = [
    "date", "rank", "match_id", "match", "team_a", "team_b", "stage", "subtype", "selection", "domestic_draw_odds",
    "market_draw_probability", "model_draw_probability", "draw_edge", "expected_value", "xg_total",
    "evidence_json", "data_quality", "captured_at", "alert_level", "additional_stake",
    "linked_main_stake", "hypothetical_stake", "settlement_mode", "strategy_version", "feature_version",
]
```

- [ ] **Step 5: Run generation and budget tests**

Run: `python -m unittest tests.test_generate_draw_alert tests.test_draw_alert_core tests.test_value_strategy -v`

Expected: all tests PASS; the overlap test proves total stake is unchanged.

- [ ] **Step 6: Commit generation**

```bash
git add betting_config.json generate_draw_alert.py tests/test_generate_draw_alert.py
git commit -m "feat: generate one value-gated draw alert"
```

### Task 4: 90-Minute Settlement and Independent Subtype Gates

**Files:**
- Create: `draw_alert_ledger.py`
- Create: `tests/test_draw_alert_ledger.py`

**Interfaces:**
- Consumes: all `output/draw_alert_*.csv`, `data/bet_results.csv`, and pre-kickoff odds snapshots.
- Produces: `output/draw_alert_ledger.csv` and `output/draw_alert_metrics.json` with separate `cold_draw` and `balanced_draw` blocks.
- Public orchestration entry point: `update_draw_alert_ledger(root: Path = ROOT) -> tuple[Path, Path]`.
- Match alerts to results by exact `date`, `team_a`, and `team_b`; never guess or reverse unmatched team names.

- [ ] **Step 1: Write failing settlement and promotion tests**

```python
import unittest
from draw_alert_ledger import compute_subtype_metrics, settle_alert


class DrawAlertLedgerTest(unittest.TestCase):
    def test_90_minute_draw_wins_even_when_team_wins_extra_time(self):
        alert = {"date": "2026-07-11", "match": "挪威 vs 英格兰", "domestic_draw_odds": "3.60", "hypothetical_stake": "10", "settlement_mode": "observation"}
        result = {"home_goals": "1", "away_goals": "1"}
        settled = settle_alert(alert, result)
        self.assertEqual("命中", settled["status"])
        self.assertEqual(26.0, settled["hypothetical_profit"])

    def test_all_knockout_regressions_settle_on_90_minutes(self):
        for match in ("阿根廷 vs 瑞士", "阿根廷 vs 佛得角", "德国 vs 巴拉圭"):
            alert = {"date": "2026-07-11", "match": match, "domestic_draw_odds": "3.20", "hypothetical_stake": "10", "settlement_mode": "observation"}
            self.assertEqual("命中", settle_alert(alert, {"home_goals": "1", "away_goals": "1"})["status"])

    def test_linked_alert_has_no_duplicate_actual_profit(self):
        alert = {"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "settlement_mode": "linked", "additional_stake": "0"}
        settled = settle_alert(alert, {"home_goals": "0", "away_goals": "0"})
        self.assertEqual(0.0, settled["actual_profit"])

    def test_each_subtype_needs_its_own_30_samples(self):
        rows = [{"status": "命中", "model_draw_probability": "0.34", "market_draw_probability": "0.28", "hypothetical_profit": "22", "clv": "0.01"} for _ in range(29)]
        self.assertFalse(compute_subtype_metrics(rows, min_samples=30, roi_gate=0.05, max_drawdown=100)["promoted"])

    def test_missing_result_stays_unsettled_for_retry(self):
        settled = settle_alert({"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "additional_stake": "20", "settlement_mode": "standalone"}, None)
        self.assertEqual("未结算", settled["status"])

    def test_standalone_loss_counts_once(self):
        alert = {"domestic_draw_odds": "3.20", "hypothetical_stake": "10", "additional_stake": "20", "settlement_mode": "standalone"}
        settled = settle_alert(alert, {"home_goals": "1", "away_goals": "0"})
        self.assertEqual(-10.0, settled["hypothetical_profit"])
        self.assertEqual(-20.0, settled["actual_profit"])
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m unittest tests.test_draw_alert_ledger -v`

Expected: FAIL because `draw_alert_ledger` does not exist.

- [ ] **Step 3: Implement settlement and metrics**

`settle_alert()` must use only `home_goals` and `away_goals` from the stored 90-minute result. Calculate hypothetical profit from a fixed stake for every alert, actual profit only from `additional_stake`, and zero actual profit for linked alerts. `compute_subtype_metrics()` must return:

```python
{
    "count": count,
    "hit_rate": hits / count if count else None,
    "roi": hypothetical_profit / hypothetical_stake if hypothetical_stake else None,
    "brier": mean((model_probability - outcome) ** 2),
    "market_brier": mean((market_probability - outcome) ** 2),
    "log_loss": binary_log_loss,
    "average_clv": mean_clv_or_none,
    "max_drawdown": running_peak_to_trough,
    "recent_brier": recent_ten_brier,
    "promoted": count >= 30 and roi > 0.05 and average_clv > 0 and brier < market_brier and max_drawdown <= 100 and recent_not_worse,
}
```

Use `outcome = 1.0` only when the two 90-minute goal fields are equal. Compute drawdown by accumulating hypothetical profit, tracking the running peak, and taking the largest `peak - cumulative`; compare recent-ten Brier with the immediately preceding ten when available. Treat missing CLV as a failed promotion gate, not as zero or positive CLV.

`update_draw_alert_ledger()` reads every daily alert CSV, keeps exactly one row per `date + team_a + team_b + subtype`, and settles it against the exact result key. Preserve all alert fields and append `home_goals`, `away_goals`, `outcome`, `status`, `hypothetical_profit`, `actual_profit`, and `clv`. Observation and `budget_capped_observation` rows always have zero actual profit; linked rows also have zero actual profit because the main betting ledger owns that money. Unresolved rows keep blank outcome/profit fields and `status="未结算"` so a later run can settle them.

For each alert, find the latest qualifying snapshot with the same date and teams from `data/odds_snapshots/*.json`. A qualifying snapshot must have `market_type="win_draw_loss"`, `settlement_minutes=90`, and `includes_extra_time=false`; when parseable timestamps are present, require `captured_at <= kickoff_at`. Calculate probability CLV as `closing_de_vig_draw_probability - stored_market_draw_probability`; leave CLV blank when no qualifying closing snapshot exists. Generate metrics separately from settled rows for `cold_draw` and `balanced_draw`, even when one subtype has zero rows.

Preserve unresolved rows as `未结算`. Store one ledger row per alert, so linked rows cannot duplicate the main betting ledger.

- [ ] **Step 4: Run ledger tests**

Run: `python -m unittest tests.test_draw_alert_ledger -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit settlement**

```bash
git add draw_alert_ledger.py tests/test_draw_alert_ledger.py
git commit -m "feat: settle draw alerts by subtype"
```

### Task 5: Guarded Champion/Challenger Learning

**Files:**
- Create: `draw_model_learning.py`
- Create: `tests/test_draw_model_learning.py`
- Create: `requirements.txt`
- Create: `data/models/.gitkeep`
- Create: `data/draw_feature_snapshots/.gitkeep`
- Modify: `generate_draw_alert.py`
- Modify: `tests/test_generate_draw_alert.py`

**Interfaces:**
- Consumes: chronological prediction CSVs, results, market evidence, alert ledger, and current registry.
- Produces: immutable versioned artifacts below `data/models/`, `data/draw_training_samples.csv`, timestamped files below `data/draw_feature_snapshots/`, and `output/draw_model_registry.json`; exports `predict_draw_probability(features: dict) -> float`.
- Public orchestration entry point: `update_draw_model(root: Path = ROOT, as_of: date | None = None, force_train: bool = False) -> Path`.

- [ ] **Step 1: Write failing time-order and promotion tests**

```python
import unittest
from datetime import date, timedelta

from draw_model_learning import chronological_splits, promotion_decision


class DrawModelLearningTest(unittest.TestCase):
    def test_every_training_date_precedes_validation_date(self):
        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(30)]
        for train, validation in chronological_splits(dates, n_splits=3):
            self.assertLess(max(dates[index] for index in train), min(dates[index] for index in validation))

    def test_challenger_cannot_promote_before_four_weeks(self):
        challenger = {"shadow_days": 27, "sample_count": 250, "bet_count": 120, "brier_improvement": 0.03, "log_loss_improvement": 0.03, "brier_skill": 0.02, "clv": 0.01, "roi": 0.02, "max_drawdown": 80}
        self.assertFalse(promotion_decision(challenger, {"max_drawdown": 90}))

    def test_all_gates_allow_promotion(self):
        challenger = {"shadow_days": 28, "sample_count": 250, "bet_count": 120, "brier_improvement": 0.03, "log_loss_improvement": 0.03, "brier_skill": 0.02, "clv": 0.01, "roi": 0.02, "max_drawdown": 80}
        self.assertTrue(promotion_decision(challenger, {"max_drawdown": 90}))

    def test_missing_champion_returns_existing_blended_probability(self):
        self.assertEqual(0.31, predict_draw_probability({"base_draw_probability": 0.31}, root=self.temp_root))

    def test_rollback_when_recent_brier_or_log_loss_worsens_two_percent(self):
        self.assertTrue(rollback_decision({"brier": 0.204, "log_loss": 0.60}, {"brier": 0.20, "log_loss": 0.60}))

    def test_only_underperforming_league_is_paused(self):
        rows = self.league_rows("L1", count=30, negative_roi=True, worsening=True) + self.league_rows("L2", count=30, negative_roi=False, worsening=False)
        states = league_pause_states(rows)
        self.assertTrue(states["L1"]["paused"])
        self.assertFalse(states["L2"]["paused"])
```

Also add a generation regression proving that a registry entry with `per_league.<stage>.paused=true` forces that league's otherwise promoted alert to `additional_stake=0` and `settlement_mode="observation"`, while the alert remains visible and is still written for learning.

- [ ] **Step 2: Verify the tests fail**

Run: `python -m unittest tests.test_draw_model_learning -v`

Expected: FAIL because `draw_model_learning` does not exist.

- [ ] **Step 3: Pin learning dependencies**

Create `requirements.txt`:

```text
joblib==1.5.2
numpy==2.4.2
scikit-learn==1.8.0
```

Keep Pillow installed separately in image-producing workflows to avoid changing its current platform package behavior.

- [ ] **Step 4: Implement chronological learning and registry state**

Use these feature columns in this order:

```python
FEATURES = [
    "base_draw_probability", "market_draw_probability", "favorite_probability", "win_probability_gap",
    "xg_total", "favorite_movement", "regional_gap", "source_count", "is_knockout", "is_balanced",
]
```

For fewer than 200 settled all-match samples, train a sigmoid calibrator with only `base_draw_probability` and `market_draw_probability`. At 200 or more samples, train a `Pipeline([StandardScaler(), LogisticRegression(C=0.5, max_iter=1000, random_state=42)])` on all features. Use `TimeSeriesSplit` without shuffled data, save fold Brier/LogLoss and market baselines, and generate only a challenger until the four-week shadow and metric gates pass. Promotion must atomically replace the champion and retain `previous_champion` in the registry for rollback. If training fails, retain the current champion and write the error to `last_training_error`.

At alert-generation time, write immutable timestamped pre-match feature snapshots under `data/draw_feature_snapshots/`. Each row contains all ten serving/training features, exact date and teams, match ID, stage, domestic draw odds, `captured_at`, and `kickoff_at`; accept it only when both timestamps parse and `captured_at <= kickoff_at`. Build `data/draw_training_samples.csv` only from these snapshots joined to `data/bet_results.csv` by exact date and teams. Use `outcome=1` only for equal 90-minute goals. Never train on unresolved matches, mutable post-match copies, or rows without proven pre-match provenance.

Use immutable versioned model artifacts strictly beneath `data/models/`. Validate a new artifact before activation, then atomically replace only `output/draw_model_registry.json` to switch champion/challenger/previous-champion pointers. Never overwrite bytes referenced by the current registry. Reject absolute paths, parent traversal, paths outside `data/models`, schema/version mismatches, unexpected feature order or model kind, estimator dimension mismatches, and corrupt joblib files.

Apply exponentially decaying sample weights while retaining every historical row. Evaluate the fixed challenger on post-creation immutable samples: its own probabilities determine qualifying simulated bets and therefore its own `bet_count`, fixed-stake ROI, CLV, and maximum drawdown. Compare challenger Brier and LogLoss against the current champion on the same rows, using base probability only when no champion exists; both relative improvements must be at least 2%. Day 28 is a minimum shadow age: keep an under-sampled challenger active until it reaches both 200 probability samples and 100 qualifying simulated bets, then promote only when every quality gate passes or reject it so a fresh challenger can start.

After promotion, compare the new and previous champions on the latest 50 settled all-match samples; roll back when either Brier or LogLoss is worse by at least 2% relative to the previous champion. Sort per-league rows chronologically, and after 30 samples mark only that league paused when ROI is negative and recent-ten Brier is worse than the preceding ten. A paused league can still be scored, displayed, and logged but cannot publish a paid alert; `generate_draw_alert.py` must read `output/draw_model_registry.json` and force that league's subtype metrics to unpromoted before stake attachment.

`predict_draw_probability()` must return `features["base_draw_probability"]` when no valid champion exists or any feature/artifact validation fails; otherwise load the registry-matched champion and clamp its output to `[0.03, 0.70]`. It accepts an optional keyword-only `root` for isolated tests. Alert generation passes all ten features with exactly the same definitions written to the immutable snapshot.

Implement the tested gate helpers exactly:

```python
def chronological_splits(dates: list[date], n_splits: int):
    indices = list(range(len(dates)))
    for train, validation in TimeSeriesSplit(n_splits=n_splits).split(indices):
        yield list(train), list(validation)


def promotion_decision(challenger: dict, champion: dict) -> bool:
    return all((
        challenger.get("shadow_days", 0) >= 28,
        challenger.get("sample_count", 0) >= 200,
        challenger.get("bet_count", 0) >= 100,
        challenger.get("brier_improvement", 0) >= 0.02,
        challenger.get("log_loss_improvement", 0) >= 0.02,
        challenger.get("brier_skill", 0) > 0,
        challenger.get("clv", 0) > 0,
        challenger.get("roi", 0) > 0,
        challenger.get("max_drawdown", float("inf")) <= champion.get("max_drawdown", float("inf")),
    ))
```

- [ ] **Step 5: Run synthetic learning tests**

Run: `python -m unittest tests.test_draw_model_learning -v`

Expected: all tests PASS and no model files are written outside the test temporary directory.

- [ ] **Step 6: Commit learning**

```bash
git add draw_model_learning.py requirements.txt data/models/.gitkeep data/draw_feature_snapshots/.gitkeep generate_draw_alert.py tests/test_draw_model_learning.py tests/test_generate_draw_alert.py
git commit -m "feat: add guarded draw model learning"
```

### Task 6: Website and Email-Image Reporting

**Files:**
- Modify: `build_site.py`
- Modify: `build_daily_image.py`
- Create: `tests/test_draw_alert_reporting.py`

**Interfaces:**
- Consumes: the daily alert CSV, alert ledger, alert metrics, and model registry.
- Produces: a consistent “平局预警” section in `web/index.html` and `web/daily-report.png`.

- [ ] **Step 1: Write failing rendering tests**

```python
import unittest
from build_site import render_draw_alert


class DrawAlertReportingTest(unittest.TestCase):
    def test_linked_alert_copy_does_not_claim_extra_stake(self):
        html = render_draw_alert([{"rank": "1", "subtype": "cold_draw", "match": "挪威 vs 英格兰", "settlement_mode": "linked", "linked_main_stake": "100", "model_draw_probability": "0.32", "market_draw_probability": "0.27", "domestic_draw_odds": "3.60", "expected_value": "1.15", "captured_at": "2026-07-12T13:30:00+08:00"}])
        self.assertIn("冷门平局", html)
        self.assertIn("复用主方案金额", html)
        self.assertNotIn("额外投入 100", html)

    def test_empty_alert_has_neutral_copy(self):
        self.assertIn("今日无符合门槛", render_draw_alert([]))
```

- [ ] **Step 2: Verify the rendering tests fail**

Run: `python -m unittest tests.test_draw_alert_reporting -v`

Expected: FAIL because `render_draw_alert` is not defined.

- [ ] **Step 3: Add website rendering**

Add `read_draw_alert(display_date)`, `read_draw_alert_metrics()`, `read_draw_model_registry()`, and `render_draw_alert(alerts)` to `build_site.py`. Place the un-nested full-width section after the main plan and before observations. Show zero to four ranked alerts with subtype, match, domestic draw odds, model/market probabilities, edge, expected value, evidence, quality, capture time, linked/observation/standalone state, and each subtype’s `count/30` promotion progress. Escape all external evidence text with `html.escape`.

The rendering state labels are fixed as follows:

```python
SUBTYPE_LABELS = {"cold_draw": "冷门平局", "balanced_draw": "均势平局"}
SETTLEMENT_LABELS = {
    "linked": "复用主方案金额，不重复投入",
    "observation": "零金额观察",
    "standalone": "独立小额模拟",
    "budget_capped_observation": "达到当日预警预算上限，零新增金额观察",
}


def render_draw_alert(alerts: list[dict]) -> str:
    if not alerts:
        return '<section class="draw-alert"><h2>平局预警</h2><p>今日无符合门槛的平局预警</p></section>'
    rows = []
    for alert in alerts:
        subtype = SUBTYPE_LABELS[alert["subtype"]]
        state = SETTLEMENT_LABELS[alert["settlement_mode"]]
        match = html.escape(alert["match"])
        rows.append(f'<article><span>第{alert["rank"]}场 · {subtype}</span><strong>{match}</strong><p>{state}</p></article>')
    return f'<section class="draw-alert"><h2>平局预警</h2>{"".join(rows)}</section>'
```

- [ ] **Step 4: Add daily-image rendering with stable dimensions**

Extend the precomputed image height by `100 + 170 * alert_count` pixels. Draw zero to four ranked rows with the same fields as the website, wrap each evidence summary to two lines, and use the existing 7-8 pixel corner radius and restrained green/gold/red palette. Ensure the alert block never changes the width of the plan or ledger columns.

- [ ] **Step 5: Run rendering and image smoke tests**

Run: `python -m unittest tests.test_draw_alert_reporting -v`

Run: `python build_site.py`

Run: `python build_daily_image.py`

Expected: tests PASS; `web/index.html` and `web/daily-report.png` are regenerated without exceptions.

- [ ] **Step 6: Commit reporting**

```bash
git add build_site.py build_daily_image.py tests/test_draw_alert_reporting.py
git commit -m "feat: show draw alerts in daily reports"
```

### Task 7: Cloud Scheduling and Failure Isolation

**Files:**
- Modify: `.github/workflows/daily-forecast.yml`
- Create: `.github/workflows/draw-alert-refresh.yml`
- Modify: `.github/workflows/noon-settlement.yml`
- Modify: `.github/workflows/email-report.yml`
- Modify: `tests/test_workflow_schedule.py`

**Interfaces:**
- Consumes: the scripts from Tasks 1-6.
- Produces: committed daily market evidence, alert, ledger, metrics, model files, rebuilt site/image, and a 14:00 email using the latest committed image.

- [ ] **Step 1: Write failing schedule tests**

```python
import unittest
from pathlib import Path


class WorkflowScheduleTest(unittest.TestCase):
    def test_beijing_schedule_crons(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        self.assertIn('cron: "15 4 * * *"', (root / "daily-forecast.yml").read_text(encoding="utf-8"))
        self.assertIn('cron: "30 5 * * *"', (root / "draw-alert-refresh.yml").read_text(encoding="utf-8"))
        self.assertIn('cron: "45 5 * * *"', (root / "noon-settlement.yml").read_text(encoding="utf-8"))
        self.assertIn('cron: "0 6 * * *"', (root / "email-report.yml").read_text(encoding="utf-8"))

    def test_refresh_failure_does_not_block_report_rebuild(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "draw-alert-refresh.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("continue-on-error: true"), 3)
        self.assertIn("python build_daily_image.py", text)
```

- [ ] **Step 2: Verify the schedule tests fail**

Run: `python -m unittest tests.test_workflow_schedule -v`

Expected: FAIL because `draw-alert-refresh.yml` does not exist.

- [ ] **Step 3: Extend the 12:15 workflow**

After the existing prediction and betting-plan commands, run:

```yaml
          python collect_market_heat.py
          python generate_draw_alert.py
          python draw_alert_ledger.py
```

Install `requirements.txt`, and include `data/market_heat_*.json`, `data/models/*`, `output/draw_alert*.csv`, `output/draw_alert*.json`, and `output/draw_model_registry.json` in the commit pattern.

- [ ] **Step 4: Create the 13:30 refresh workflow**

Schedule `30 5 * * *`, checkout the latest main branch, install dependencies/fonts, rerun `import_sporttery.py`, `predict_today.py`, `collect_market_heat.py`, `generate_draw_alert.py`, `build_site.py`, and `build_daily_image.py`. Put import, prediction refresh, and optional market collection in separate steps with `continue-on-error: true`; alert generation then uses the newest complete timestamped inputs or reuses the committed 12:15 alert and capture time. Commit outputs and deploy Pages with the existing actions.

- [ ] **Step 5: Extend settlement and email workflows**

At 13:45, run `draw_alert_ledger.py --settle`, `draw_model_learning.py --train`, then rebuild the site/image before commit/deploy. Keep the 14:05 result retry. At 14:00, install no learning dependencies and send the latest checked-out `web/daily-report.png`; keep Gmail credentials only in GitHub secrets/environment.

- [ ] **Step 6: Run workflow tests**

Run: `python -m unittest tests.test_workflow_schedule -v`

Expected: 2 tests PASS.

- [ ] **Step 7: Commit workflows**

```bash
git add .github/workflows/daily-forecast.yml .github/workflows/draw-alert-refresh.yml .github/workflows/noon-settlement.yml .github/workflows/email-report.yml tests/test_workflow_schedule.py
git commit -m "feat: automate draw alert refresh and learning"
```

### Task 8: End-to-End Verification and Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLOUD_SETUP.md`

**Interfaces:**
- Consumes: the complete feature.
- Produces: user-facing operating notes and verified deployable source.

- [ ] **Step 1: Document the new daily flow and safety states**

Add concise sections explaining the 12:15 base forecast, 13:30 alert refresh, 13:45 settlement/training, 14:00 email, zero-stake observation, linked main-plan state, independent 30-sample gates, and the fact that no alert is a valid daily result.

- [ ] **Step 2: Run the full test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all existing and new tests PASS.

- [ ] **Step 3: Run a local deterministic pipeline smoke test**

Run against the committed sample date without network writes:

```bash
python predict_today.py --date 2026-07-12
python generate_betting_plan.py --date 2026-07-12
python collect_market_heat.py --date 2026-07-12 --offline
python generate_draw_alert.py --date 2026-07-12
python draw_alert_ledger.py --date 2026-07-12
python build_site.py
python build_daily_image.py
```

Expected: every command exits 0; there are zero to four alert rows, at most two per league; total additional alert stake is at most 80 and all daily stakes are at most 500; HTML and PNG are rebuilt.

- [ ] **Step 4: Inspect generated report at desktop and mobile widths**

Start the local server, inspect `web/index.html` at 1440x900 and 390x844, and verify the alert text does not overlap the main plan, ledger, or model metrics. Also inspect `web/daily-report.png` at original resolution for clipped evidence or stake copy.

- [ ] **Step 5: Confirm the git diff contains no generated secrets or unrelated user files**

Run: `git status --short` and `git diff --check`.

Expected: only planned source/docs/tests/workflows and intentionally regenerated report artifacts appear; no Gmail password, token, temporary model cache, or unrelated local output is staged.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md CLOUD_SETUP.md
git commit -m "docs: explain draw alert automation"
```

- [ ] **Step 7: Final verification before publication**

Run: `python -m unittest discover -s tests -v`

Run: `git log --oneline --max-count=10`

Expected: all tests PASS and each task is represented by an intentional commit.

## Reference Documentation

- Polymarket Gamma API introduction: https://docs.polymarket.com/api-reference/introduction
- Polymarket public search: https://docs.polymarket.com/api-reference/search/search-markets-events-and-profiles
- scikit-learn time-series validation: https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html
- scikit-learn probability calibration: https://scikit-learn.org/stable/modules/calibration.html
