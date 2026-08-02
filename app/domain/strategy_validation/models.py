from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class ValidationVerdict(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class ValidationTrade:
    symbol: str
    signal_date: date
    entry_date: date
    exit_date: date
    gross_r: float
    fee_r: float = 0
    slippage_r: float = 0
    regime: str = "UNKNOWN"

    @property
    def net_r(self) -> float:
        return self.gross_r - self.fee_r - self.slippage_r


@dataclass(frozen=True)
class ValidationMetrics:
    sample_size: int
    expectancy_r: float
    win_rate: float
    profit_factor: float | None
    maximum_drawdown_r: float
    tail_loss_r: float
    total_cost_r: float
    regime_samples: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ValidationPolicy:
    minimum_sample_size: int = 30
    minimum_expectancy_r: float = 0
    minimum_profit_factor: float = 1
    maximum_drawdown_r: float = 10
    maximum_tail_loss_r: float = 2
    minimum_regime_sample_size: int = 5


@dataclass(frozen=True)
class ValidationReport:
    verdict: ValidationVerdict
    metrics: ValidationMetrics
    failures: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
