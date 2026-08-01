from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.preview.models import PreviewSnapshot, thaw
from app.domain.trade_plan import TradePlanSnapshot
from app.errors import AppError
from app.models import (
    PreviewSnapshotRecord,
    RuleVersion,
    TradePlan,
    TradePlanAIAnalysis,
    TradePlanCheck,
)
from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest
from app.services.preview_snapshot import (
    create_snapshot,
    load_snapshot,
    snapshot_payload as preview_snapshot_payload,
    verify_hash,
)
from app.services.rule_version_manager import RuleVersionManager
from app.services.trade_plan.assembler import assemble_preview
from app.services.trade_plan.lifecycle import confirm_preview, next_version, snapshot_payload


def ensure_generator_rule_version(db: Session) -> RuleVersion:
    return RuleVersionManager(db).ensure_active_version()


def _snapshot_record_payload(record: PreviewSnapshotRecord) -> dict:
    return {
        "snapshot_id": record.id,
        "symbol": record.symbol,
        "preview_hash": record.preview_hash,
        "strategy_snapshot": record.strategy_snapshot,
        "feature_snapshot": record.feature_snapshot,
        "risk_snapshot": record.risk_snapshot,
        "decision_snapshot": record.decision_snapshot,
        "price_snapshot": record.price_snapshot,
        "rule_version_snapshot": record.rule_version_snapshot,
        "account_snapshot": record.account_snapshot,
        "market_snapshot": record.market_snapshot,
        "preview_payload": record.preview_payload,
        "created_at": record.created_at,
        "hash": record.snapshot_hash,
    }


def get_preview_snapshot(
    db: Session, account_id: int, symbol: str, preview_hash: str
) -> PreviewSnapshot | None:
    record = db.scalar(
        select(PreviewSnapshotRecord).where(
            PreviewSnapshotRecord.account_id == account_id,
            PreviewSnapshotRecord.symbol == symbol,
            PreviewSnapshotRecord.preview_hash == preview_hash,
        )
    )
    return load_snapshot(_snapshot_record_payload(record)) if record else None


def save_preview_snapshot(db: Session, account_id: int, preview: dict) -> PreviewSnapshot:
    existing = get_preview_snapshot(db, account_id, preview["symbol"], preview["preview_hash"])
    if existing is not None:
        return existing
    snapshot = create_snapshot(preview)
    record = PreviewSnapshotRecord(
        account_id=account_id,
        symbol=snapshot.symbol,
        preview_hash=snapshot.preview_hash,
        snapshot_hash=snapshot.hash,
        strategy_snapshot=thaw(snapshot.strategy_snapshot),
        feature_snapshot=thaw(snapshot.feature_snapshot),
        risk_snapshot=thaw(snapshot.risk_snapshot),
        decision_snapshot=thaw(snapshot.decision_snapshot),
        price_snapshot=thaw(snapshot.price_snapshot),
        rule_version_snapshot=thaw(snapshot.rule_version_snapshot),
        account_snapshot=thaw(snapshot.account_snapshot),
        market_snapshot=thaw(snapshot.market_snapshot),
        preview_payload=thaw(snapshot.preview_payload),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return load_snapshot(_snapshot_record_payload(record))


def save_preview(preview: dict) -> TradePlanSnapshot:
    model = assemble_preview(preview["symbol"], preview)
    return confirm_preview(model, preview["preview_hash"])


def save_plan(db: Session, request: TradePlanSaveRequest) -> dict:
    frozen = get_preview_snapshot(db, request.account_id, request.symbol, request.preview_hash)
    legacy_recalculate_confirm = frozen is None
    if frozen is not None:
        if not verify_hash(frozen):
            raise AppError(409, "PREVIEW_SNAPSHOT_INVALID", "预览快照完整性校验失败，请重新生成预览")
        preview = preview_snapshot_payload(frozen)
    else:
        from app.services.trade_plan.application import generate_trade_plan

        preview_request = TradePlanPreviewRequest(
            **request.model_dump(exclude={"preview_hash", "ai_analysis_id"})
        )
        preview = generate_trade_plan(db, preview_request)
        if preview["preview_hash"] != request.preview_hash:
            raise AppError(409, "PREVIEW_CHANGED", "数据或规则已变化，请重新生成预览后再确认保存")
        frozen = save_preview_snapshot(db, request.account_id, preview)
    latest = db.scalar(
        select(TradePlan)
        .where(TradePlan.account_id == request.account_id, TradePlan.symbol == request.symbol)
        .order_by(TradePlan.plan_version.desc(), TradePlan.id.desc())
    )
    plan_version = next_version(latest)
    buy_zone = preview["buy_plan"]["buy_zone"]
    stop = preview["buy_plan"]["hard_stop"]
    if not buy_zone[0] or not stop:
        raise AppError(422, "PLAN_NOT_SAVABLE", "缺少可靠买入区或硬止损，不能保存正式计划")
    rule = ensure_generator_rule_version(db)
    quantity = preview["position_calculation"].get("final_allowed_quantity", 0)
    entry = buy_zone[1]
    snapshot = save_preview(preview)
    plan = TradePlan(
        account_id=request.account_id,
        rule_version_id=rule.id,
        symbol=request.symbol,
        name=preview["company_name"],
        status=preview["status"],
        trade_mode=request.trade_mode,
        decision_level="日线",
        market_state=request.market_state,
        sector_state=request.sector_state,
        large_cycle_direction=preview["multi_timeframe"]["weekly"]["state"],
        industry_logic=None,
        company_logic=None,
        technical_structure=rule.rules["name"],
        buy_zone_low=Decimal(str(buy_zone[0])),
        buy_zone_high=Decimal(str(buy_zone[1])),
        initial_stop=Decimal(str(stop)),
        invalidation_condition=preview["buy_plan"]["structure_invalidation"],
        target_plan=preview["exit_plan"]["first_reduction"],
        account_equity=Decimal(str(preview["account"]["equity"])),
        risk_pct=request.risk_pct,
        max_position_pct=request.max_position_pct,
        planned_quantity=quantity,
        planned_position_value=Decimal(str(round(quantity * entry, 4))),
        planned_risk_amount=Decimal(
            str(round(quantity * preview["position_calculation"].get("per_share_risk", 0), 2))
        ),
        add_condition="；".join(preview["confirmation_add"]["requirements"]),
        reduce_condition=preview["exit_plan"]["first_reduction"],
        exit_condition="；".join(preview["exit_plan"]["final_exit"]),
        no_trade_condition="；".join(preview["buy_plan"]["abandon_conditions"]),
        next_action="；".join(preview["next_observations"]),
        data_status=preview["data_status"],
        data_date=date.fromisoformat(preview["data_date"]),
        source="确定性交易计划生成器",
        plan_version=plan_version.version,
        parent_plan_id=plan_version.parent_plan_id,
        preview_hash=snapshot.preview_hash,
        engine_snapshot={
            **dict(snapshot_payload(snapshot)),
            "_confirmation": {
                "legacy_recalculate_confirm": legacy_recalculate_confirm,
                "preview_snapshot_id": frozen.snapshot_id,
            },
        },
        market_snapshot={
            "market_state": request.market_state,
            "sector_state": request.sector_state,
        },
        account_snapshot=preview["account"],
        source_snapshot=preview["sources"],
    )
    db.add(plan)
    db.flush()
    if request.ai_analysis_id is not None:
        analysis = db.get(TradePlanAIAnalysis, request.ai_analysis_id)
        if (
            analysis is None
            or analysis.symbol != request.symbol
            or analysis.evidence_package.get("preview_hash") != preview["preview_hash"]
        ):
            raise AppError(422, "AI_ANALYSIS_MISMATCH", "AI分析与当前股票或证据版本不匹配")
        analysis.trade_plan_id = plan.id
    for gate in preview["gates"]:
        db.add(
            TradePlanCheck(
                trade_plan_id=plan.id,
                gate_code=gate["code"],
                gate_name=gate["name"],
                status=gate["status"],
                basis=gate["evidence"],
                missing_data=gate["missing_conditions"],
                rule_version=rule.version,
                checked_at=datetime.now(),
            )
        )
    from app.services.plan_execution import initialize_plan_execution

    initialize_plan_execution(
        db,
        plan,
        request.position_mode or ("持仓" if preview["existing_position"]["exists"] else "空仓"),
    )
    db.commit()
    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "symbol": plan.symbol,
        "plan_version": plan_version.version,
        "status": plan.status,
        "execution_status": plan.execution_status,
        "preview": preview,
    }


def get_history(db: Session, account_id: int, symbol: str) -> list[dict]:
    items = db.scalars(
        select(TradePlan)
        .where(TradePlan.account_id == account_id, TradePlan.symbol == symbol)
        .order_by(TradePlan.plan_version.desc(), TradePlan.id.desc())
    ).all()
    return [
        {
            "id": item.id,
            "plan_version": item.plan_version,
            "status": item.status,
            "execution_status": item.execution_status,
            "buy_zone": [float(item.buy_zone_low), float(item.buy_zone_high)],
            "stop": float(item.initial_stop),
            "rule_version": db.get(RuleVersion, item.rule_version_id).version,
            "data_date": item.data_date.isoformat(),
            "created_at": item.created_at.isoformat(),
            "preview_hash": item.preview_hash,
        }
        for item in items
    ]


def compare_versions(db: Session, first_id: int, second_id: int) -> dict:
    first, second = db.get(TradePlan, first_id), db.get(TradePlan, second_id)
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
            db.get(RuleVersion, first.rule_version_id).version,
            db.get(RuleVersion, second.rule_version_id).version,
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
