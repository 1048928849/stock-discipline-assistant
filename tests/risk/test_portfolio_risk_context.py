from dataclasses import dataclass
from decimal import Decimal

from app.services.portfolio_risk_context import calculate_portfolio_risk


@dataclass
class Holding:
    quantity: int
    current_price: Decimal
    stop_loss_price: Decimal | None


def test_sums_open_risk_from_frozen_stops():
    result = calculate_portfolio_risk(
        [
            Holding(1000, Decimal(10), Decimal(9)),
            Holding(500, Decimal(20), Decimal(18)),
        ],
        equity=Decimal(100000),
    )

    assert result.open_risk_amount == Decimal(2000)
    assert result.open_risk_pct == 2
    assert result.complete is True


def test_missing_stop_is_explicit_instead_of_invented():
    result = calculate_portfolio_risk([Holding(1000, Decimal(10), None)], equity=Decimal(100000))

    assert result.open_risk_amount == 0
    assert result.complete is False
    assert result.holdings_without_stop == 1
    assert "风险被低估" in result.warnings[0]
