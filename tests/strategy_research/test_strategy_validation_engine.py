from datetime import date, timedelta

import pytest

from app.domain.strategy_validation import ValidationPolicy, ValidationTrade, ValidationVerdict
from app.services.strategy_validation_engine import (
    build_walk_forward_folds,
    evaluate_validation,
)


def _trade(index: int, gross_r: float, *, costs: float = 0.1, regime: str = "UP"):
    signal = date(2024, 1, 1) + timedelta(days=index * 3)
    return ValidationTrade(
        symbol=f"{index:06d}",
        signal_date=signal,
        entry_date=signal + timedelta(days=1),
        exit_date=signal + timedelta(days=2),
        gross_r=gross_r,
        fee_r=costs / 2,
        slippage_r=costs / 2,
        regime=regime,
    )


def test_walk_forward_folds_never_overlap_train_and_test():
    dates = [date(2024, 1, 1) + timedelta(days=index) for index in range(20)]

    folds = build_walk_forward_folds(dates, train_size=10, test_size=5)

    assert len(folds) == 2
    assert all(fold.train_end < fold.test_start for fold in folds)


def test_costs_can_turn_apparent_edge_into_failure():
    trades = [_trade(index, 0.05, costs=0.1) for index in range(40)]

    report = evaluate_validation(trades)

    assert report.verdict is ValidationVerdict.FAILED
    assert report.metrics.expectancy_r < 0
    assert "扣除成本后的期望值未通过" in report.failures


def test_positive_out_of_sample_distribution_passes_policy():
    trades = [
        _trade(index, 1.2 if index % 2 == 0 else -0.5, regime="UP" if index < 20 else "RANGE")
        for index in range(40)
    ]

    report = evaluate_validation(
        trades,
        ValidationPolicy(maximum_drawdown_r=5, maximum_tail_loss_r=1),
    )

    assert report.verdict is ValidationVerdict.PASSED
    assert report.metrics.expectancy_r > 0
    assert report.metrics.total_cost_r == 4


def test_small_sample_cannot_be_promoted_even_when_profitable():
    report = evaluate_validation([_trade(index, 2) for index in range(5)])

    assert report.verdict is ValidationVerdict.INSUFFICIENT_DATA


def test_future_leakage_order_is_rejected():
    invalid = ValidationTrade("000001", date(2024, 1, 3), date(2024, 1, 2), date(2024, 1, 4), 1)

    with pytest.raises(ValueError, match="禁止使用未来数据"):
        evaluate_validation([invalid])
