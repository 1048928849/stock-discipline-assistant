from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class HoldingRiskView(Protocol):
    quantity: int
    current_price: Decimal
    stop_loss_price: Decimal | None


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    open_risk_amount: Decimal
    open_risk_pct: float
    holdings_with_stop: int
    holdings_without_stop: int
    complete: bool
    warnings: tuple[str, ...]


def calculate_portfolio_risk(
    holdings: list[HoldingRiskView] | tuple[HoldingRiskView, ...],
    *,
    equity: Decimal,
) -> PortfolioRiskSnapshot:
    open_risk = Decimal(0)
    with_stop = 0
    without_stop = 0
    for holding in holdings:
        if holding.quantity <= 0:
            continue
        if holding.stop_loss_price is None:
            without_stop += 1
            continue
        with_stop += 1
        per_share_risk = max(Decimal(0), holding.current_price - holding.stop_loss_price)
        open_risk += Decimal(holding.quantity) * per_share_risk
    warnings = (f"{without_stop}个持仓缺少硬止损，组合开放风险被低估",) if without_stop else ()
    return PortfolioRiskSnapshot(
        open_risk_amount=open_risk,
        open_risk_pct=round(float(open_risk / equity * 100), 4) if equity > 0 else 0,
        holdings_with_stop=with_stop,
        holdings_without_stop=without_stop,
        complete=without_stop == 0,
        warnings=warnings,
    )
