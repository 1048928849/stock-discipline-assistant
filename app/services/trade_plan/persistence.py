"""Backward-compatible persistence facade for the finalized application boundary."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.preview import PreviewSnapshot
from app.errors import AppError
from app.models import RuleVersion
from app.schemas_workflow import TradePlanSaveRequest
from app.services.persistence import PreviewSnapshotRepository, TradePlanRepository
from app.services.rule_version_manager import RuleVersionManager
from app.services.trade_plan.confirm_application import confirm_trade_plan
from app.services.transaction import transaction_scope


def ensure_generator_rule_version(db: Session) -> RuleVersion:
    with transaction_scope(db):
        version = RuleVersionManager(db).ensure_active_version()
    return version


def get_preview_snapshot(
    db: Session, account_id: int, symbol: str, preview_hash: str
) -> PreviewSnapshot | None:
    return PreviewSnapshotRepository(db).load(account_id, symbol, preview_hash)


def save_preview_snapshot(db: Session, account_id: int, preview: dict) -> PreviewSnapshot:
    return PreviewSnapshotRepository(db).save(account_id, preview)


def save_plan(db: Session, request: TradePlanSaveRequest) -> dict:
    return confirm_trade_plan(db, request)


def get_history(db: Session, account_id: int, symbol: str) -> list[dict]:
    repository = TradePlanRepository(db)
    return [
        {
            "id": item.id,
            "plan_version": item.plan_version,
            "status": item.status,
            "execution_status": item.execution_status,
            "buy_zone": [float(item.buy_zone_low), float(item.buy_zone_high)],
            "stop": float(item.initial_stop),
            "rule_version": repository.rule_version(item.rule_version_id).version,
            "data_date": item.data_date.isoformat(),
            "created_at": item.created_at.isoformat(),
            "preview_hash": item.preview_hash,
        }
        for item in repository.history(account_id, symbol)
    ]


def compare_versions(db: Session, first_id: int, second_id: int) -> dict:
    repository = TradePlanRepository(db)
    first, second = repository.get(first_id), repository.get(second_id)
    if not first or not second:
        raise AppError(404, "TRADE_PLAN_NOT_FOUND", "比较的交易计划不存在")
    if first.symbol != second.symbol or first.account_id != second.account_id:
        raise AppError(422, "PLAN_COMPARE_SCOPE", "只能比较同一账户、同一股票的计划")
    keys = {
        "status": (first.status, second.status),
        "buy_zone": (
            [float(first.buy_zone_low), float(first.buy_zone_high)],
            [float(second.buy_zone_low), float(second.buy_zone_high)],
        ),
        "hard_stop": (float(first.initial_stop), float(second.initial_stop)),
        "quantity": (first.planned_quantity, second.planned_quantity),
        "risk_pct": (float(first.risk_pct), float(second.risk_pct)),
        "rule_version": (
            repository.rule_version(first.rule_version_id).version,
            repository.rule_version(second.rule_version_id).version,
        ),
    }
    return {
        "first": {"id": first.id, "plan_version": first.plan_version},
        "second": {"id": second.id, "plan_version": second.plan_version},
        "differences": [
            {"field": key, "before": before, "after": after}
            for key, (before, after) in keys.items()
            if before != after
        ],
    }
