from __future__ import annotations

from sqlalchemy.orm import Session

from app.errors import AppError
from app.schemas_workflow import TradePlanSaveRequest
from app.services.persistence import (
    ExecutionInitializer,
    PreviewSnapshotRepository,
    TradePlanMapper,
    TradePlanRepository,
)
from app.services.preview_snapshot import snapshot_payload, verify_hash
from app.services.rule_version_manager import RuleVersionManager
from app.services.trade_plan.legacy_confirm import recalculate_legacy_preview
from app.services.trade_plan.lifecycle import next_version
from app.services.transaction import transaction_scope


def confirm_trade_plan(db: Session, request: TradePlanSaveRequest) -> dict:
    with transaction_scope(db):
        snapshots = PreviewSnapshotRepository(db)
        plans = TradePlanRepository(db)
        frozen = snapshots.load(request.account_id, request.symbol, request.preview_hash)
        if frozen is None:
            confirm_mode = "LEGACY_RECALCULATE"
            preview = recalculate_legacy_preview(db, request)
            frozen = snapshots.save(request.account_id, preview)
        else:
            confirm_mode = "SNAPSHOT_CONFIRM"
            if not verify_hash(frozen):
                raise AppError(
                    409,
                    "PREVIEW_SNAPSHOT_INVALID",
                    "预览快照完整性校验失败，请重新生成预览",
                )
            preview = snapshot_payload(frozen)

        buy_zone = preview["buy_plan"]["buy_zone"]
        stop = preview["buy_plan"]["hard_stop"]
        if not buy_zone[0] or not stop:
            raise AppError(422, "PLAN_NOT_SAVABLE", "缺少可靠买入区或硬止损，不能保存正式计划")

        rule = RuleVersionManager(db).ensure_active_version()
        version = next_version(plans.latest(request.account_id, request.symbol))
        plan = TradePlanMapper().to_orm(
            request=request,
            preview=preview,
            frozen=frozen,
            rule=rule,
            version=version,
            confirm_mode=confirm_mode,
        )
        plans.save(plan)
        plans.attach_ai_analysis(request.ai_analysis_id, plan, preview)
        plans.add_checks(plan, preview, rule)
        ExecutionInitializer(db).initialize(
            plan,
            request.position_mode or ("持仓" if preview["existing_position"]["exists"] else "空仓"),
        )

    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "symbol": plan.symbol,
        "plan_version": version.version,
        "status": plan.status,
        "execution_status": plan.execution_status,
        "preview": preview,
    }
