from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import AppError
from app.models import RuleSet, TradePlan, TradePlanAIAnalysis
from app.schemas_workflow import (
    PositionAssessment,
    OneClickPlanRequest,
    PlanExecutionEvaluate,
    PlanExecutionFillCreate,
    RuleVersionUpdate,
    TradePlanAIRequest,
    TradePlanCreate,
    TradePlanPreviewRequest,
    TradePlanSaveRequest,
)
from app.services.one_click_pipeline import (
    confirm_one_click_plan,
    get_analysis_run,
    run_one_click_analysis,
)
from app.services.data_sources import UnifiedDataService
from app.services.plan_execution import (
    add_manual_fill,
    evaluate_plan_execution,
    execution_detail,
)
from app.services.trade_plan_ai import run_ai_analysis, serialize_ai_analysis
from app.services.trade_plan_generator import (
    compare_plans,
    ensure_generator_rule_version,
    generate_trade_plan_preview,
    plan_history,
    save_generated_plan,
)
from app.services.workflow import (
    assess_positions,
    create_rule_version,
    create_trade_plan,
    dashboard_summary,
    ensure_default_rule_version,
    save_position_assessment,
    serialize_trade_plan,
)


router = APIRouter(prefix="/api/v1")


@router.post("/trade-plan-generator/analyze")
def analyze_and_generate_plan(payload: OneClickPlanRequest, db: Session = Depends(get_db)):
    """普通用户入口：自动取数、规则计算、AI降级解释，一次返回完整计划。"""
    return run_one_click_analysis(db, payload)


@router.get("/trade-plan-generator/analyze/{run_id}")
def get_one_click_analysis(run_id: int, db: Session = Depends(get_db)):
    return get_analysis_run(db, run_id)


@router.post("/trade-plan-generator/analyze/{run_id}/confirm", status_code=status.HTTP_201_CREATED)
def confirm_analyzed_plan(run_id: int, db: Session = Depends(get_db)):
    return confirm_one_click_plan(db, run_id)


@router.post("/trade-plan-generator/preview")
def preview_trade_plan(payload: TradePlanPreviewRequest, db: Session = Depends(get_db)):
    return generate_trade_plan_preview(db, payload)


@router.post("/trade-plan-generator/save", status_code=status.HTTP_201_CREATED)
def save_trade_plan(payload: TradePlanSaveRequest, db: Session = Depends(get_db)):
    return save_generated_plan(db, payload)


@router.post("/trade-plan-generator/ai")
def analyze_trade_plan(payload: TradePlanAIRequest, db: Session = Depends(get_db)):
    return run_ai_analysis(db, payload)


@router.get("/trade-plan-generator/ai/{analysis_id}")
def get_trade_plan_ai(analysis_id: int, db: Session = Depends(get_db)):
    item = db.get(TradePlanAIAnalysis, analysis_id)
    if item is None:
        raise AppError(404, "AI_ANALYSIS_NOT_FOUND", "AI辅助分析不存在")
    return serialize_ai_analysis(item)


@router.get("/trade-plan-generator/history")
def get_trade_plan_history(account_id: int, symbol: str, db: Session = Depends(get_db)):
    return plan_history(db, account_id, symbol)


@router.get("/trade-plan-generator/compare")
def compare_trade_plan_versions(first_id: int, second_id: int, db: Session = Depends(get_db)):
    return compare_plans(db, first_id, second_id)


@router.post("/trade-plan-generator/holding-check")
def check_holding_conditions(payload: TradePlanPreviewRequest, db: Session = Depends(get_db)):
    preview = generate_trade_plan_preview(db, payload)
    return {
        "symbol": preview["symbol"],
        "status": preview["status"],
        "existing_position": preview["existing_position"],
        "confirmation_add": preview["confirmation_add"],
        "exit_plan": preview["exit_plan"],
        "gates": preview["gates"],
    }


@router.get("/trade-plan-generator/rules")
def get_trade_plan_generator_rules(db: Session = Depends(get_db)):
    version = ensure_generator_rule_version(db)
    return {
        "version": version.version,
        "parameters": version.parameters,
        "rules": version.rules,
        "effective_from": version.effective_from.isoformat(),
        "updated_at": version.updated_at.isoformat(),
    }


@router.get("/data-sources/status")
def data_source_status(probe: bool = False, db: Session = Depends(get_db)):
    return UnifiedDataService(db).registry.public_status(probe=probe)


@router.get("/trade-plans/{plan_id}/execution")
def get_plan_execution(plan_id: int, db: Session = Depends(get_db)):
    return execution_detail(db, plan_id)


@router.post("/trade-plans/{plan_id}/execution/fills", status_code=status.HTTP_201_CREATED)
def add_plan_execution_fill(
    plan_id: int, payload: PlanExecutionFillCreate, db: Session = Depends(get_db)
):
    return add_manual_fill(db, plan_id, payload)


@router.post("/trade-plans/{plan_id}/execution/evaluate")
def evaluate_execution(
    plan_id: int, payload: PlanExecutionEvaluate, db: Session = Depends(get_db)
):
    return evaluate_plan_execution(db, plan_id, payload)


@router.get("/dashboard/summary")
def get_dashboard_summary(db: Session = Depends(get_db)):
    return dashboard_summary(db)


@router.get("/rule-versions/current")
def get_current_rule_version(db: Session = Depends(get_db)):
    version = ensure_default_rule_version(db)
    rule_set = db.get(RuleSet, version.rule_set_id)
    return {
        "rule_set": rule_set.name,
        "version": version.version,
        "parameters": version.parameters,
        "rules": version.rules,
        "source": rule_set.source_name,
        "effective_from": version.effective_from.isoformat(),
        "updated_at": version.updated_at.isoformat(),
        "status": "success",
    }


@router.post("/rule-versions", status_code=status.HTTP_201_CREATED)
def add_rule_version(payload: RuleVersionUpdate, db: Session = Depends(get_db)):
    version = create_rule_version(db, payload)
    return {
        "id": version.id,
        "version": version.version,
        "parameters": version.parameters,
        "effective_from": version.effective_from.isoformat(),
        "status": "success",
    }


@router.get("/trade-plans")
def list_trade_plans(
    account_id: int | None = None,
    plan_status: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
):
    query = select(TradePlan).order_by(TradePlan.updated_at.desc())
    if account_id is not None:
        query = query.where(TradePlan.account_id == account_id)
    if plan_status:
        query = query.where(TradePlan.status == plan_status)
    return [serialize_trade_plan(db, item) for item in db.scalars(query).all()]


@router.post("/trade-plans", status_code=status.HTTP_201_CREATED)
def add_trade_plan(payload: TradePlanCreate, db: Session = Depends(get_db)):
    return serialize_trade_plan(db, create_trade_plan(db, payload))


@router.get("/trade-plans/{plan_id}")
def get_trade_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(TradePlan, plan_id)
    if plan is None:
        raise AppError(404, "TRADE_PLAN_NOT_FOUND", "交易计划不存在")
    return serialize_trade_plan(db, plan)


@router.get("/positions/management")
def position_management(account_id: int | None = None, db: Session = Depends(get_db)):
    return assess_positions(db, account_id=account_id)


@router.post("/positions/snapshots")
def snapshot_positions(account_id: int | None = None, db: Session = Depends(get_db)):
    return assess_positions(db, account_id=account_id, persist=True)


@router.post("/positions/{holding_id}/assessment", status_code=status.HTTP_201_CREATED)
def assess_position(holding_id: int, payload: PositionAssessment, db: Session = Depends(get_db)):
    item = save_position_assessment(db, holding_id, payload)
    return {
        "id": item.id,
        "holding_id": item.holding_id,
        "stage": item.stage,
        "logic_status": item.logic_status,
        "hard_stop_triggered": item.hard_stop_triggered,
        "invalidation_triggered": item.invalidation_triggered,
        "allow_add": item.allow_add,
        "next_action": item.next_action,
        "source": item.source,
        "data_date": item.data_date.isoformat(),
        "updated_at": item.fetched_at.isoformat(),
        "status": "success",
    }
