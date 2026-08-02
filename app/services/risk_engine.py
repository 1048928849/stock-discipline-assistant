from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Any

from app.domain.risk import RiskConstraint, RiskContext, RiskEvaluationResult, RiskStatus

POSITION_FORMULA = (
    "最终数量=min(风险预算、可用资金、单股仓位、总仓位、行业集中度允许数量)，再向下取100股整手"
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
    current_drawdown_pct = _decimal(account.get("current_drawdown_pct", 0))
    max_drawdown_pct = account.get("max_account_drawdown_pct")
    drawdown_blocked = max_drawdown_pct is not None and current_drawdown_pct >= _decimal(
        max_drawdown_pct
    )
    consecutive_losses = max(0, int(account.get("consecutive_losses", 0)))
    default_multiplier = 0.5 if consecutive_losses >= 3 else 1
    loss_streak_multiplier = max(
        Decimal(0),
        min(
            Decimal(1),
            _decimal(account.get("loss_streak_risk_multiplier", default_multiplier)),
        ),
    )
    effective_risk_budget = risk_budget * loss_streak_multiplier
    risk_quantity = floor_lot(effective_risk_budget / per_share_risk)
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
        equity * _decimal(account["max_industry_position_pct"]) / Decimal(100) - sector_value,
    )
    single_quantity = floor_lot(single_remaining / entry_price)
    total_quantity = floor_lot(total_remaining / entry_price)
    sector_quantity = floor_lot(sector_remaining / entry_price)
    constraints_list = [
        RiskConstraint("risk_budget", risk_quantity),
        RiskConstraint("cash", cash_quantity),
        RiskConstraint("single_position", single_quantity),
        RiskConstraint("total_position", total_quantity),
        RiskConstraint("sector_exposure", sector_quantity),
    ]
    max_portfolio_risk_pct = account.get("max_portfolio_risk_pct")
    if max_portfolio_risk_pct is not None:
        open_risk = _decimal(position.get("open_risk_amount", 0))
        portfolio_remaining = max(
            Decimal(0),
            equity * _decimal(max_portfolio_risk_pct) / Decimal(100) - open_risk,
        )
        constraints_list.append(
            RiskConstraint("portfolio_open_risk", floor_lot(portfolio_remaining / per_share_risk))
        )
    portfolio_risk_incomplete = not bool(position.get("portfolio_risk_complete", True))
    if portfolio_risk_incomplete:
        constraints_list.append(RiskConstraint("portfolio_stop_completeness", 0))
    if drawdown_blocked:
        constraints_list.append(RiskConstraint("account_drawdown_circuit_breaker", 0))
    constraints = tuple(constraints_list)
    allowed_quantity = min(item.limit for item in constraints)
    binding_constraint = next(item.name for item in constraints if item.limit == allowed_quantity)
    trial_quantity = floor_lot(allowed_quantity * float(account.get("trial_position_ratio", 0)))
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
    blocking_reasons_list: list[str] = []
    if drawdown_blocked:
        blocking_reasons_list.append("账户回撤达到熔断阈值，禁止新增风险")
    if portfolio_risk_incomplete:
        missing_stops = int(position.get("holdings_without_stop", 0))
        blocking_reasons_list.append(
            f"{missing_stops}个现有持仓缺少硬止损，无法可靠计算组合开放风险"
        )
    if allowed_quantity < 100:
        blocking_reasons_list.append("最终允许数量不足100股")
    blocking_reasons = tuple(blocking_reasons_list)
    stress_losses: dict[str, float] = {}
    for gap_pct in account.get("stress_gap_pcts", (3, 5, 10)):
        gap = _decimal(gap_pct)
        gap_exit = entry_price * (Decimal(1) - gap / Decimal(100))
        stressed_exit = min(stop_price, gap_exit)
        stress_losses[f"gap_down_{float(gap):g}_pct"] = round(
            allowed_quantity * float(entry_price - stressed_exit), 2
        )
    details = {
        "risk_budget": round(float(risk_budget), 2),
        "effective_risk_budget": round(float(effective_risk_budget), 2),
        "current_drawdown_pct": float(current_drawdown_pct),
        "drawdown_circuit_breaker": drawdown_blocked,
        "consecutive_losses": consecutive_losses,
        "loss_streak_risk_multiplier": float(loss_streak_multiplier),
        "per_share_risk": round(float(per_share_risk), 4),
        "risk_allowed_quantity": risk_quantity,
        "cash_allowed_quantity": cash_quantity,
        "single_position_allowed_quantity": single_quantity,
        "total_position_allowed_quantity": total_quantity,
        "industry_concentration_allowed_quantity": sector_quantity,
        "final_allowed_quantity": allowed_quantity,
        "trial_quantity": trial_quantity,
        "trial_amount": round(trial_quantity * float(entry_price), 2),
        "trial_account_pct": round(trial_quantity * float(entry_price) / float(equity) * 100, 2),
        "maximum_loss": round(trial_quantity * float(per_share_risk), 2),
        "formula": POSITION_FORMULA,
        "stop_distance_pct": stop_distance_pct,
        "stop_status": stop_status,
        "reward_risk_status": reward_risk_status,
        "stress_losses": stress_losses,
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
    hidden = {
        "stop_distance_pct",
        "stop_status",
        "reward_risk_status",
        # V2账户风险诊断先保留在领域结果中，旧API不静默增加字段。
        "effective_risk_budget",
        "current_drawdown_pct",
        "drawdown_circuit_breaker",
        "consecutive_losses",
        "loss_streak_risk_multiplier",
        "stress_losses",
    }
    return {key: value for key, value in result.calculation_details.items() if key not in hidden}
