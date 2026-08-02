from sqlalchemy.orm import Session

from app.domain.repository import RuleVersionRecord
from app.services.repositories.sqlalchemy.account_repository import (
    SqlAlchemyAccountRepository,
)
from app.services.repositories.sqlalchemy.market_repository import SqlAlchemyMarketRepository
from app.services.rule_version_manager import RuleVersionManager


class SqlAlchemyTradePlanRepository:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.accounts = SqlAlchemyAccountRepository(db)
        self.market = SqlAlchemyMarketRepository(db)

    def get_account(self, account_id: int):
        return self.accounts.get_account(account_id)

    def get_holding(self, account_id: int, symbol: str):
        return self.accounts.get_holding(account_id, symbol)

    def get_holdings(self, account_id: int):
        return self.accounts.get_holdings(account_id)

    def get_company_profile(self, symbol: str):
        return self.market.get_company_profile(symbol)

    def get_latest_bar(self, symbol: str):
        return self.market.get_latest_bar(symbol)

    def get_quote(self, symbol: str):
        return self.market.get_quote(symbol)

    def load_qfq_frame(self, symbol: str):
        return self.market.load_qfq_frame(symbol)

    def ensure_rule_version(self) -> RuleVersionRecord:
        item = RuleVersionManager(self.db).ensure_active_version()
        return RuleVersionRecord(
            version=item.version,
            parameters=item.parameters,
            rules=item.rules,
        )
