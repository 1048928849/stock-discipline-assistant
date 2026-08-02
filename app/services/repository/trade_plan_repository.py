from sqlalchemy.orm import Session

from app.domain.repository import TradePlanReadRepository
from app.services.repositories.sqlalchemy.trade_plan_repository import (
    SqlAlchemyTradePlanRepository,
)


def build_trade_plan_repository(db: Session) -> TradePlanReadRepository:
    return SqlAlchemyTradePlanRepository(db)
