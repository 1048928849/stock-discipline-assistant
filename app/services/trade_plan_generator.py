"""Backward-compatible facade for the extracted trade plan application layer."""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest
from app.services.risk_engine import floor_lot
from app.services.trade_plan.application import generate_trade_plan
from app.services.trade_plan.compatibility import GENERATOR_PARAMETERS, GENERATOR_RULES
from app.services.trade_plan.persistence import (
    compare_versions,
    ensure_generator_rule_version,
    get_history,
    save_plan,
)


def _floor_lot(value: float | Decimal) -> int:
    return floor_lot(value)


def generate_trade_plan_preview(db: Session, request: TradePlanPreviewRequest) -> dict:
    return generate_trade_plan(db, request)


def save_generated_plan(db: Session, request: TradePlanSaveRequest) -> dict:
    return save_plan(db, request)


def plan_history(db: Session, account_id: int, symbol: str) -> list[dict]:
    return get_history(db, account_id, symbol)


def compare_plans(db: Session, first_id: int, second_id: int) -> dict:
    return compare_versions(db, first_id, second_id)


__all__ = [
    "GENERATOR_PARAMETERS",
    "GENERATOR_RULES",
    "_floor_lot",
    "compare_plans",
    "ensure_generator_rule_version",
    "generate_trade_plan_preview",
    "plan_history",
    "save_generated_plan",
]
