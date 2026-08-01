from sqlalchemy.orm import Session

from app.models import TradePlan
from app.services.plan_execution import initialize_plan_execution


class ExecutionInitializer:
    def __init__(self, db: Session) -> None:
        self.db = db

    def initialize(self, plan: TradePlan, position_mode: str) -> None:
        initialize_plan_execution(self.db, plan, position_mode)
