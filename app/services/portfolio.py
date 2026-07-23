from decimal import Decimal, ROUND_HALF_UP

from app.models import Account, Holding


FOUR_PLACES = Decimal("0.0001")


def holding_metrics(holding: Holding, account: Account) -> dict[str, Decimal]:
    market_value = Decimal(holding.quantity) * holding.current_price
    cost_value = Decimal(holding.quantity) * holding.cost_price
    unrealized_pnl = market_value - cost_value
    return_pct = unrealized_pnl / cost_value * 100 if cost_value else Decimal(0)
    # 总资产包含现金，仓位分母禁止改为持仓市值合计。
    position_pct = market_value / account.total_assets * 100 if account.total_assets else Decimal(0)
    return {
        "market_value": market_value.quantize(FOUR_PLACES),
        "unrealized_pnl": unrealized_pnl.quantize(FOUR_PLACES),
        "return_pct": return_pct.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP),
        "position_pct": position_pct.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP),
    }
