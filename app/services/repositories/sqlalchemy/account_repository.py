from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.repository import AccountRecord, HoldingRecord
from app.models import Account, Holding


def _holding_record(item: Holding) -> HoldingRecord:
    return HoldingRecord(
        symbol=item.symbol,
        quantity=item.quantity,
        cost_price=item.cost_price,
        current_price=item.current_price,
        stop_loss_price=item.stop_loss_price,
        target_price=item.target_price,
        sector=item.sector,
    )


class SqlAlchemyAccountRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_account(self, account_id: int) -> AccountRecord | None:
        item = self.db.get(Account, account_id)
        if item is None:
            return None
        return AccountRecord(
            id=item.id,
            total_assets=item.total_assets,
            available_cash=item.available_cash,
        )

    def get_holding(self, account_id: int, symbol: str) -> HoldingRecord | None:
        item = self.db.scalar(
            select(Holding).where(
                Holding.account_id == account_id,
                Holding.symbol == symbol,
            )
        )
        return _holding_record(item) if item else None

    def get_holdings(self, account_id: int) -> tuple[HoldingRecord, ...]:
        items = self.db.scalars(select(Holding).where(Holding.account_id == account_id)).all()
        return tuple(_holding_record(item) for item in items)
