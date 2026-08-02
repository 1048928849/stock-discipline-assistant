from datetime import date, timedelta
from decimal import Decimal

from app.models import Account
from app.services.account_equity import account_drawdown, record_account_equity


def test_account_equity_history_calculates_peak_drawdown(session):
    today = date(2026, 8, 1)
    account = Account(
        name="回撤账户",
        total_assets=Decimal(100000),
        cash=Decimal(100000),
        available_cash=Decimal(100000),
    )
    session.add(account)
    session.flush()
    record_account_equity(session, account, snapshot_date=today - timedelta(days=1))
    account.total_assets = Decimal(92000)
    record_account_equity(session, account, snapshot_date=today)

    result = account_drawdown(session, account.id, current_equity=account.total_assets)

    assert result.peak_equity == Decimal(100000)
    assert result.drawdown_pct == 8
    assert result.observation_count == 2
