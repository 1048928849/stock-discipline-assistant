from decimal import Decimal

from app.models import Account, Holding
from app.services.portfolio import holding_metrics


def test_portfolio_profit_and_position_math():
    account = Account(
        name="测试",
        total_assets=Decimal("200000"),
        cash=Decimal("150000"),
        available_cash=Decimal("150000"),
    )
    holding = Holding(
        account_id=1,
        symbol="000001",
        name="平安银行",
        quantity=1000,
        cost_price=Decimal("10"),
        current_price=Decimal("12"),
        price_source="manual",
    )
    metrics = holding_metrics(holding, account)
    assert metrics["market_value"] == Decimal("12000.0000")
    assert metrics["unrealized_pnl"] == Decimal("2000.0000")
    assert metrics["return_pct"] == Decimal("20.0000")
    assert metrics["position_pct"] == Decimal("6.0000")
