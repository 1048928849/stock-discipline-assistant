from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.repository import (
    CompanyProfileRecord,
    MarketBarRecord,
    MarketQuoteRecord,
)
from app.models import CompanyProfile, MarketDailyBar, MarketQuote
from app.services.technical_snapshots import load_qfq_frame


class SqlAlchemyMarketRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_company_profile(self, symbol: str) -> CompanyProfileRecord | None:
        item = self.db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == symbol))
        if item is None:
            return None
        return CompanyProfileRecord(
            symbol=item.symbol,
            name=item.name,
            industry=item.industry,
            source=item.source,
            fetched_at=item.fetched_at,
        )

    def get_latest_bar(self, symbol: str) -> MarketBarRecord | None:
        item = self.db.scalar(
            select(MarketDailyBar)
            .where(MarketDailyBar.symbol == symbol)
            .order_by(MarketDailyBar.trade_date.desc(), MarketDailyBar.fetched_at.desc())
        )
        if item is None:
            return None
        return MarketBarRecord(
            symbol=item.symbol,
            trade_date=item.trade_date,
            close=item.close,
            source=item.source,
            fetched_at=item.fetched_at,
        )

    def get_quote(self, symbol: str) -> MarketQuoteRecord | None:
        item = self.db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
        if item is None:
            return None
        return MarketQuoteRecord(symbol=item.symbol, name=item.name, price=item.price)

    def load_qfq_frame(self, symbol: str):
        return load_qfq_frame(self.db, symbol)
