from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Any

from app.domain.risk import RiskConstraint, RiskContext, RiskEvaluationResult, RiskStatus

POSITION_FORMULA = (
    "最终数量=min(风险预算、可用资金、单股仓位、总仓位、行业集中度允许数量)，"
    "再向下取100股整手"
)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def floor_lot(value: float | Decimal) -> int:
    return max(0, int(_decimal(value).to_integral_value(rounding=ROUND_FLOOR)) // 100 * 100)


def evaluate_risk(context: RiskContext) -> RiskEvaluationResult:
    account = context.account_context
    position = context.position_context
    entry = context.entry_context.get("entry_price")
    stop = context.stop_context.get("stop_price")
    risk_pct = account.get("risk_pct")
    if entry is None or stop is None or _decimal(entry) <= _decimal(stop):
        missing = []
        if entry is None:
            missing.append("有效买入价")
        if stop is None:
            missing.append("硬止损")
        if entry is not None and stop is not None and _decimal(entry) <= _decimal(stop):
            missing.append("买入价必须高于硬止损")
        return RiskEvaluationResult(
            status=RiskStatus.INSUFFICIENT_DATA,
            allowed_quantity=0,
            risk_amount=None,
            risk_ratio=float(risk_pct) if risk_pct is not None else None,
            constraints=(),
            blocking_reasons=tuple(missing),
            calculation_details={},
        )

    equity = _decimal(account["equity"])
    available_cash = _decimal(account["available_cash"])
    entry_price = _decimal(entry)
    stop_price = _decimal(stop)
    risk_ratio = _decimal(risk_pct)
    per_share_risk = entry_price - stop_price
    risk_budget = equity * risk_ratio / Decimal(100)
    risk_quantity = floor_lot(risk_budget / per_share_risk)
    cash_quantity = floor_lot(available_cash / entry_price)

    existing_value = _decimal(position.get("existing_symbol_value", 0))
    total_value = _decimal(position.get("total_position_value", 0))
    sector_value = _decimal(context.sector_context.get("sector_position_value", 0))
    single_remaining = max(
        Decimal(0),
        equity * _decimal(account["max_position_pct"]) / Decimal(100) - existing_value,
    )
    total_remaining = max(
        Decimal(0),
        equity * _decimal(account["max_total_position_pct"]) / Decimal(100) - total_value,
    )
    sector_remaining = max(
        Decimal(0),
        equity * _decimal(account["max_industry_position_pct"]) / Decimal(100)
        - sector_value,
    )
    single_quantity = floor_lot(single_remaining / entry_price)
    total_quantity = floor_lot(total_remaining / entry_price)
    sector_quantity = floor_lot(sector_remaining / entry_price)
    constraints = (
        RiskConstraint("risk_budget", risk_quantity),
        RiskConstraint("cash", cash_quantity),
        RiskConstraint("single_position", single_quantity),
        RiskConstraint("total_position", total_quantity),
        RiskConstraint("sector_exposure", sector_quantity),
    )
    allowed_quantity = min(item.limit for item in constraints)
    binding_constraint = next(
        item.name for item in constraints if item.limit == allowed_quantity
    )
    trial_quantity = floor_lot(
        allowed_quantity * float(account.get("trial_position_ratio", 0))
    )
    stop_distance_pct = float(per_share_risk / entry_price * Decimal(100))
    maximum_stop_distance = float(context.stop_context["maximum_stop_distance_pct"])
    reward_risk = context.stop_context.get("reward_risk")
    minimum_reward_risk = float(context.stop_context["minimum_reward_risk"])
    stop_status = "不通过" if stop_distance_pct > maximum_stop_distance else "通过"
    reward_risk_status = (
        "无法判断"
        if reward_risk is None
        else "通过"
        if float(reward_risk) >= minimum_reward_risk
        else "不通过"
    )
    status = RiskStatus.PASS if allowed_quantity >= 100 else RiskStatus.BLOCKED
    blocking_reasons = (
        () if status is RiskStatus.PASS else ("最终允许数量不足100股",)
    )
    details = {
        "risk_budget": round(float(risk_budget), 2),
        "per_share_risk": round(float(per_share_risk), 4),
        "risk_allowed_quantity": risk_quantity,
        "cash_allowed_quantity": cash_quantity,
        "single_position_allowed_quantity": single_quantity,
        "total_position_allowed_quantity": total_quantity,
        "industry_concentration_allowed_quantity": sector_quantity,
        "final_allowed_quantity": allowed_quantity,
        "trial_quantity": trial_quantity,
        "trial_amount": round(trial_quantity * float(entry_price), 2),
        "trial_account_pct": round(
            trial_quantity * float(entry_price) / float(equity) * 100, 2
        ),
        "maximum_loss": round(trial_quantity * float(per_share_risk), 2),
        "formula": POSITION_FORMULA,
        "stop_distance_pct": stop_distance_pct,
        "stop_status": stop_status,
        "reward_risk_status": reward_risk_status,
    }
    return RiskEvaluationResult(
        status=status,
        allowed_quantity=allowed_quantity,
        risk_amount=round(allowed_quantity * float(per_share_risk), 2),
        risk_ratio=float(risk_ratio),
        constraints=constraints,
        blocking_reasons=blocking_reasons,
        calculation_details=details,
        binding_constraint=binding_constraint,
    )


def legacy_position_calculation(result: RiskEvaluationResult) -> dict[str, Any]:
    if result.status is RiskStatus.INSUFFICIENT_DATA:
        return {}
    hidden = {"stop_distance_pct", "stop_status", "reward_risk_status"}
    return {
        key: value
        for key, value in result.calculation_details.items()
        if key not in hidden
    }
