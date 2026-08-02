from __future__ import annotations

from collections import Counter
from datetime import date
from math import inf
from statistics import mean

from app.domain.strategy_validation import (
    ValidationMetrics,
    ValidationPolicy,
    ValidationReport,
    ValidationTrade,
    ValidationVerdict,
    WalkForwardFold,
)


def build_walk_forward_folds(
    dates: list[date] | tuple[date, ...],
    *,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
) -> tuple[WalkForwardFold, ...]:
    ordered = sorted(set(dates))
    if train_size <= 0 or test_size <= 0:
        raise ValueError("训练窗口和测试窗口必须大于0")
    step = step_size or test_size
    if step <= 0:
        raise ValueError("滚动步长必须大于0")
    folds: list[WalkForwardFold] = []
    start = 0
    while start + train_size + test_size <= len(ordered):
        train = ordered[start : start + train_size]
        test = ordered[start + train_size : start + train_size + test_size]
        if train[-1] >= test[0]:
            raise ValueError("训练集和样本外测试集发生时间重叠")
        folds.append(WalkForwardFold(train[0], train[-1], test[0], test[-1]))
        start += step
    return tuple(folds)


def _validate_trade_order(trades: tuple[ValidationTrade, ...]) -> None:
    for trade in trades:
        if not trade.signal_date <= trade.entry_date < trade.exit_date:
            raise ValueError("交易日期必须满足 signal <= entry < exit，禁止使用未来数据")
        if trade.fee_r < 0 or trade.slippage_r < 0:
            raise ValueError("手续费和滑点不能为负数")


def _maximum_drawdown(values: list[float]) -> float:
    equity = peak = 0.0
    maximum = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def evaluate_validation(
    trades: list[ValidationTrade] | tuple[ValidationTrade, ...],
    policy: ValidationPolicy | None = None,
) -> ValidationReport:
    policy = policy or ValidationPolicy()
    ordered = tuple(sorted(trades, key=lambda item: (item.exit_date, item.symbol)))
    _validate_trade_order(ordered)
    net = [item.net_r for item in ordered]
    gains = sum(value for value in net if value > 0)
    losses = abs(sum(value for value in net if value < 0))
    profit_factor = gains / losses if losses else inf if gains else None
    tail_count = max(1, int(len(net) * 0.05)) if net else 0
    tail_loss = abs(mean(sorted(net)[:tail_count])) if tail_count else 0
    regimes = Counter(item.regime for item in ordered)
    metrics = ValidationMetrics(
        sample_size=len(ordered),
        expectancy_r=round(mean(net), 4) if net else 0,
        win_rate=round(sum(value > 0 for value in net) / len(net) * 100, 2) if net else 0,
        profit_factor=round(profit_factor, 4)
        if profit_factor not in (None, inf)
        else profit_factor,
        maximum_drawdown_r=round(_maximum_drawdown(net), 4),
        tail_loss_r=round(tail_loss, 4),
        total_cost_r=round(sum(item.fee_r + item.slippage_r for item in ordered), 4),
        regime_samples=tuple(sorted(regimes.items())),
    )
    failures: list[str] = []
    warnings: list[str] = []
    if metrics.sample_size < policy.minimum_sample_size:
        failures.append("样本量不足")
    if metrics.expectancy_r <= policy.minimum_expectancy_r:
        failures.append("扣除成本后的期望值未通过")
    if metrics.profit_factor is None or metrics.profit_factor < policy.minimum_profit_factor:
        failures.append("利润因子未通过")
    if metrics.maximum_drawdown_r > policy.maximum_drawdown_r:
        failures.append("最大回撤超过门槛")
    if metrics.tail_loss_r > policy.maximum_tail_loss_r:
        failures.append("尾部损失超过门槛")
    thin_regimes = sorted(
        regime for regime, count in regimes.items() if count < policy.minimum_regime_sample_size
    )
    if thin_regimes:
        warnings.append(f"市场环境样本不足：{', '.join(thin_regimes)}")
    verdict = (
        ValidationVerdict.INSUFFICIENT_DATA
        if metrics.sample_size < policy.minimum_sample_size
        else ValidationVerdict.FAILED
        if failures
        else ValidationVerdict.PASSED
    )
    return ValidationReport(verdict, metrics, tuple(failures), tuple(warnings))
