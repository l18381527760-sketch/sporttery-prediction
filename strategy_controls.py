"""Conservative calibration and simulation-account controls."""

from __future__ import annotations

import math
from datetime import date, datetime


SETTLED_STATUSES = {"命中", "未中", "未命中"}


def fit_league_draw_calibrations(
    rows: list[dict],
    *,
    min_samples: int = 30,
    prior_samples: int = 60,
    max_adjustment: float = 0.05,
    validation_fraction: float = 0.25,
) -> dict[str, dict]:
    """Fit a time-ordered, validation-gated draw intercept for each league."""
    min_samples = max(30, int(min_samples))
    prior_samples = max(1, int(prior_samples))
    max_adjustment = min(0.10, max(0.0, float(max_adjustment)))
    validation_fraction = min(0.40, max(0.20, float(validation_fraction)))
    grouped: dict[str, list[tuple[date, str, float, float]]] = {}
    for row in rows:
        stage = str(row.get("stage") or "").strip()
        probability = _probability(row.get("base_draw_probability"))
        outcome = _outcome(row.get("outcome"))
        match_date = _date(row.get("date"))
        if not stage or probability is None or outcome is None or match_date is None:
            continue
        grouped.setdefault(stage, []).append(
            (match_date, str(row.get("match_id") or ""), probability, outcome)
        )

    table = {}
    for stage, items in grouped.items():
        items.sort(key=lambda item: (item[0], item[1]))
        probabilities = [item[2] for item in items]
        outcomes = [item[3] for item in items]
        sample_count = len(items)
        state = {
            "enabled": False,
            "sample_count": sample_count,
            "adjustment": 0.0,
            "proposed_adjustment": 0.0,
            "average_model_probability": _mean(probabilities),
            "observed_draw_rate": _mean(outcomes),
            "validation_count": 0,
            "validation_brier_before": None,
            "validation_brier_after": None,
            "reason": "insufficient_samples",
        }
        if sample_count < min_samples:
            table[stage] = state
            continue

        validation_count = max(10, int(math.ceil(sample_count * validation_fraction)))
        validation_count = min(validation_count, sample_count - 10)
        split = sample_count - validation_count
        train_probabilities = probabilities[:split]
        train_outcomes = outcomes[:split]
        validation_probabilities = probabilities[split:]
        validation_outcomes = outcomes[split:]
        train_bias = _mean(train_outcomes) - _mean(train_probabilities)
        train_shrinkage = split / (split + prior_samples)
        proposed = _clip(
            train_bias * train_shrinkage, -max_adjustment, max_adjustment
        )
        before = _brier(validation_probabilities, validation_outcomes)
        after = _brier(
            [_clip(value + proposed, 0.03, 0.70) for value in validation_probabilities],
            validation_outcomes,
        )
        state.update(
            validation_count=validation_count,
            validation_brier_before=before,
            validation_brier_after=after,
            proposed_adjustment=proposed,
            reason="validation_not_improved",
        )
        if after is not None and before is not None and after < before - 1e-12:
            full_bias = _mean(outcomes) - _mean(probabilities)
            full_shrinkage = sample_count / (sample_count + prior_samples)
            adjustment = _clip(
                full_bias * full_shrinkage, -max_adjustment, max_adjustment
            )
            state.update(
                enabled=abs(adjustment) > 1e-12,
                adjustment=adjustment,
                reason="validated" if abs(adjustment) > 1e-12 else "no_bias",
            )
        table[stage] = state
    return table


def apply_league_draw_calibration(
    probability: float, stage: str, table: dict[str, dict]
) -> tuple[float, dict]:
    base = _probability(probability)
    if base is None:
        raise ValueError("draw probability must be between zero and one")
    state = table.get(str(stage or "").strip())
    if not isinstance(state, dict):
        state = {
            "enabled": False,
            "sample_count": 0,
            "adjustment": 0.0,
            "reason": "no_league_history",
        }
    if state.get("enabled") is not True:
        return base, dict(state)
    adjustment = _number(state.get("adjustment")) or 0.0
    return _clip(base + adjustment, 0.03, 0.70), dict(state)


def simulation_account_state(
    betting_rows: list[dict],
    observation_rows: list[dict],
    target_date: date,
    policy: dict,
    metrics: dict | None = None,
) -> dict:
    """Return hard budget controls and the manual-review simulation progress."""
    required_days = max(30, int(_number(policy.get("required_settled_days")) or 30))
    configured_budget = _number(policy.get("monthly_budget_cap"))
    configured_stop_loss = _number(policy.get("monthly_stop_loss"))
    monthly_budget_cap = max(
        0.0, 3000.0 if configured_budget is None else configured_budget
    )
    monthly_stop_loss = max(
        0.0, 500.0 if configured_stop_loss is None else configured_stop_loss
    )
    month_prefix = target_date.strftime("%Y-%m-")
    target_text = target_date.isoformat()
    month_rows = [
        row
        for row in betting_rows
        if str(row.get("date") or "").startswith(month_prefix)
        and str(row.get("date") or "") != target_text
    ]
    monthly_stake = sum(
        value
        for row in month_rows
        if (value := _nonnegative(row.get("stake"))) is not None
    )
    monthly_profit = sum(
        _number(row.get("profit")) or 0.0
        for row in month_rows
        if row.get("status") in SETTLED_STATUSES
    )
    pause_reasons = []
    if monthly_budget_cap <= 0 or monthly_stake >= monthly_budget_cap - 1e-9:
        pause_reasons.append("monthly_budget_cap")
    if monthly_stop_loss <= 0 or monthly_profit <= -monthly_stop_loss + 1e-9:
        pause_reasons.append("monthly_stop_loss")

    settled_dates = {
        str(row.get("date") or "")
        for row in observation_rows
        if row.get("status") in SETTLED_STATUSES and _date(row.get("date")) is not None
    }
    metrics = metrics if isinstance(metrics, dict) else {}
    active = metrics.get("active_betting_strategy")
    if not isinstance(active, dict):
        active = metrics.get("active_strategy") if isinstance(metrics.get("active_strategy"), dict) else {}
    clv = metrics.get("clv") if isinstance(metrics.get("clv"), dict) else {}
    roi = _number(active.get("roi"))
    average_clv = _number(clv.get("average_clv"))
    completed_days = len(settled_dates)
    review_ready = (
        completed_days >= required_days
        and roi is not None
        and roi > 0
        and average_clv is not None
        and average_clv > 0
    )
    return {
        "mode": "simulation",
        "required_settled_days": required_days,
        "completed_days": completed_days,
        "review_ready": review_ready,
        "real_money_automation": False,
        "monthly_budget_cap": monthly_budget_cap,
        "monthly_stop_loss": monthly_stop_loss,
        "monthly_stake": round(monthly_stake, 2),
        "monthly_profit": round(monthly_profit, 2),
        "remaining_monthly_budget": round(
            max(0.0, monthly_budget_cap - monthly_stake), 2
        ),
        "paused": bool(pause_reasons),
        "pause_reasons": pause_reasons,
    }


def build_daily_decision(
    plan: list[dict],
    observations: list[dict],
    target_date: date,
    prediction_count: int,
    account_state: dict,
    learning_policy: dict | None = None,
) -> dict:
    stakes = [_nonnegative(row.get("stake")) for row in plan]
    valid_stakes = [value for value in stakes if value is not None]
    simulated_stake = sum(valid_stakes) if len(valid_stakes) == len(stakes) else 0.0
    pause_reasons = account_state.get("pause_reasons", [])
    if pause_reasons:
        labels = {
            "monthly_budget_cap": "本月模拟投入已达到上限",
            "monthly_stop_loss": "本月模拟止损线已触发",
        }
        reason = "；".join(labels.get(item, item) for item in pause_reasons) + "，今日仅保留零金额观察。"
        status = "risk_paused"
    elif plan:
        reason = "仅保留同时通过官方赔率、概率优势、正期望值和风险门槛的方案。"
        status = "bet"
    else:
        reason = "没有比赛同时通过官方赔率、概率优势、正期望值和风险门槛，今日主方案观望。"
        status = "no_bet"
    learning_policy = learning_policy if isinstance(learning_policy, dict) else {}
    return {
        "date": target_date.isoformat(),
        "status": status,
        "reason": reason,
        "matches_reviewed": max(0, int(prediction_count)),
        "qualified_bets": len(plan),
        "observation_count": len(observations),
        "simulated_stake": round(simulated_stake, 2),
        "case_study_policy": str(learning_policy.get("case_study_policy") or "regression_only"),
        "minimum_rule_samples": max(30, int(_number(learning_policy.get("minimum_rule_samples")) or 30)),
        "account": dict(account_state),
    }


def combo_leg_limit(strategy: dict, completed_days: int) -> int:
    configured = int(_number(strategy.get("combo_max_legs")) or 3)
    configured = min(3, max(2, configured))
    required_days = max(
        30, int(_number(strategy.get("three_leg_min_settled_days")) or 30)
    )
    return min(configured, 2) if completed_days < required_days else configured


def _brier(probabilities: list[float], outcomes: list[float]) -> float | None:
    if not probabilities or len(probabilities) != len(outcomes):
        return None
    return sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) / len(probabilities)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _clip(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _number(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative(value) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _probability(value) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and 0 <= parsed <= 1 else None


def _outcome(value) -> float | None:
    parsed = _number(value)
    return parsed if parsed in {0.0, 1.0} else None


def _date(value) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
