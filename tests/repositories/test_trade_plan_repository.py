from datetime import date, datetime
from decimal import Decimal

from app.models import Account, Holding, MarketDailyBar, MarketQuote
from app.services.repositories.sqlalchemy.trade_plan_repository import (
    SqlAlchemyTradePlanRepository,
)


def test_repository_reads_account_holding_and_market_records(session):
    account = Account(
        name="测试账户",
        total_assets=Decimal(100000),
        cash=Decimal(80000),
        available_cash=Decimal(80000),
    )
    session.add(account)
    session.flush()
    session.add(
        Holding(
            account_id=account.id,
            symbol="300502",
            name="测试公司",
            quantity=100,
            cost_price=Decimal(10),
            current_price=Decimal(11),
            sector="测试行业",
            price_source="manual",
        )
    )
    session.add(
        MarketQuote(
            symbol="300502",
            name="测试公司",
            price=Decimal(11),
            source="fixture",
            source_api="fixture",
            fetched_at=datetime.now(),
        )
    )
    session.add(
        MarketDailyBar(
            symbol="300502",
            trade_date=date.today(),
            open=Decimal(10),
            high=Decimal("11.2"),
            low=Decimal("9.8"),
            close=Decimal(11),
            volume=Decimal(1000),
            source="akshare_tencent_qfq",
            fetched_at=datetime.now(),
        )
    )
    session.commit()
    repository = SqlAlchemyTradePlanRepository(session)

    account_record = repository.get_account(account.id)
    holding_record = repository.get_holding(account.id, "300502")
    quote_record = repository.get_quote("300502")
    latest_bar = repository.get_latest_bar("300502")

    assert account_record.total_assets == Decimal(100000)
    assert holding_record.quantity == 100
    assert quote_record.price == Decimal(11)
    assert latest_bar.trade_date == date.today()
    assert not hasattr(account_record, "status")


def test_repository_empty_data_is_explicit(session):
    repository = SqlAlchemyTradePlanRepository(session)

    assert repository.get_account(999) is None
    assert repository.get_holding(999, "300502") is None
    assert repository.get_holdings(999) == ()
    assert repository.get_company_profile("300502") is None
    assert repository.get_latest_bar("300502") is None
    assert repository.get_quote("300502") is None


def test_repository_missing_market_frame_raises_original_error(session):
    repository = SqlAlchemyTradePlanRepository(session)

    try:
        repository.load_qfq_frame("300502")
    except ValueError as exc:
        assert str(exc) == "没有前复权历史数据，请先执行 AKShare 行情同步"
    else:
        raise AssertionError("缺失行情应显式失败")
