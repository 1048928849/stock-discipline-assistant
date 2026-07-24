from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import (
    Account,
    DisciplineEvent,
    Holding,
    MarketQuote,
    MarketSourceLog,
    PlanAnalysisRun,
    PositionSnapshot,
    RuleSet,
    RuleVersion,
    Trade,
    TradePlan,
    TradePlanCheck,
)
from app.schemas_workflow import PositionAssessment, RuleVersionUpdate, TradePlanCreate
from app.services.discipline import check_discipline
from app.services.portfolio import holding_metrics


DEFAULT_RULE_PARAMETERS = {
    "single_trade_risk_pct": 1.0,
    "single_trade_risk_range_pct": [0.5, 1.0],
    "beginner_holdings_range": [2, 4],
    "max_single_position_pct": 25.0,
    "recommended_single_position_range_pct": [20.0, 30.0],
    "max_account_drawdown_pct": 8.0,
    "account_drawdown_trigger_range_pct": [6.0, 10.0],
    "a_share_lot_size": 100,
}

DEFAULT_RULES = {
    "principle": "先写计划、再计算风险；硬止损优先，不能在亏损后扩大止损或临时改成长线。",
    "default_pattern": "日线趋势波段：平台放量突破—缩量回踩—再次转强",
    "position_stages": ["观察", "试错", "确认", "趋势", "转弱"],
    "disclaimer": "默认参数是学习手册中的保守纪律模板，可修改，不构成收益保证。",
}

GATES = (
    ("market", "市场环境"),
    ("mode", "交易模式和级别"),
    ("direction", "月线/周线大周期方向"),
    ("logic", "行业与公司逻辑"),
    ("technical", "技术位置和量价结构"),
    ("risk", "止损、仓位和账户风险"),
    ("execution", "买入、加仓、减仓和退出计划"),
)


def ensure_default_rule_version(db: Session) -> RuleVersion:
    rule_set = db.scalar(select(RuleSet).where(RuleSet.code == "mi_trend_conservative"))
    if rule_set is None:
        rule_set = RuleSet(
            code="mi_trend_conservative",
            name="趋势交易保守纪律模板",
            description="将趋势交易学习手册中的仓位、止损和计划要求规则化。",
            source_name="《Mi姐趋势交易体系学习手册》与《14天学习导航》",
            enabled=True,
        )
        db.add(rule_set)
        db.flush()
    version = db.scalar(
        select(RuleVersion)
        .where(RuleVersion.rule_set_id == rule_set.id, RuleVersion.active.is_(True))
        .order_by(RuleVersion.id.desc())
    )
    if version is None:
        version = RuleVersion(
            rule_set_id=rule_set.id,
            version="1.0.0",
            parameters=DEFAULT_RULE_PARAMETERS,
            rules=DEFAULT_RULES,
            change_note="首次规则化：采用手册保守模板，所有参数允许后续版本调整。",
            effective_from=date.today(),
            active=True,
        )
        db.add(version)
        db.commit()
        db.refresh(version)
    return version


def create_rule_version(db: Session, payload: RuleVersionUpdate) -> RuleVersion:
    current = ensure_default_rule_version(db)
    current.active = False
    major, minor, patch = (int(value) for value in current.version.split("."))
    parameters = dict(current.parameters)
    parameters.update(
        {
            "single_trade_risk_pct": float(payload.single_trade_risk_pct),
            "max_single_position_pct": float(payload.max_single_position_pct),
            "max_account_drawdown_pct": float(payload.max_account_drawdown_pct),
            "beginner_holdings_range": [
                payload.beginner_min_holdings,
                payload.beginner_max_holdings,
            ],
        }
    )
    version = RuleVersion(
        rule_set_id=current.rule_set_id,
        version=f"{major}.{minor}.{patch + 1}",
        parameters=parameters,
        rules=current.rules,
        change_note="用户调整纪律参数；旧计划继续保留原规则版本。",
        effective_from=date.today(),
        active=True,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _gate_values(payload: TradePlanCreate, quantity: int, rule_version: str) -> list[dict]:
    values: dict[str, tuple[str, str, list[str]]] = {}
    if not payload.market_state or payload.market_state == "无法判断":
        values["market"] = ("无法判断", "没有可靠市场状态，不能据此扩大风险。", ["市场状态"])
    elif payload.market_state == "下降":
        values["market"] = ("不通过", "市场处于下降状态，默认只观察。", [])
    elif payload.market_state == "震荡":
        values["market"] = ("警告", "震荡环境降低仓位与预期，避免在区间中部追涨。", [])
    else:
        values["market"] = ("通过", "市场状态填写为上升。", [])
    values["mode"] = (
        "通过" if payload.trade_mode and payload.decision_level else "无法判断",
        f"{payload.trade_mode}，以{payload.decision_level}决策。",
        [] if payload.trade_mode and payload.decision_level else ["交易模式或级别"],
    )
    if not payload.large_cycle_direction or payload.large_cycle_direction == "无法判断":
        values["direction"] = ("无法判断", "缺少月线/周线方向判断。", ["大周期方向"])
    elif payload.large_cycle_direction == "向下":
        values["direction"] = ("不通过", "大周期向下，不能用小周期信号覆盖风险。", [])
    elif payload.large_cycle_direction == "震荡":
        values["direction"] = ("警告", "大周期方向未完全形成，只允许保守试错。", [])
    else:
        values["direction"] = ("通过", "月线/周线方向填写为向上。", [])
    missing_logic = [
        label
        for value, label in (
            (payload.industry_logic, "产业逻辑"),
            (payload.company_logic, "公司逻辑"),
        )
        if not value
    ]
    values["logic"] = (
        "通过" if not missing_logic else "无法判断",
        "产业与公司逻辑均已填写。" if not missing_logic else "逻辑证据不完整。",
        missing_logic,
    )
    values["technical"] = (
        "通过" if payload.technical_structure else "无法判断",
        payload.technical_structure or "缺少技术结构和量价依据。",
        [] if payload.technical_structure else ["技术结构与量价证据"],
    )
    risk_warnings = []
    if payload.risk_pct > Decimal("1"):
        risk_warnings.append("单笔风险高于保守模板上限 1%")
    if payload.max_position_pct > Decimal("30"):
        risk_warnings.append("单只仓位高于保守模板上限 30%")
    values["risk"] = (
        "警告" if risk_warnings else "通过" if quantity > 0 else "不通过",
        "；".join(risk_warnings)
        or ("止损距离、账户风险与整手数量已计算。" if quantity > 0 else "风险预算不足一手。"),
        [],
    )
    missing_execution = [
        label
        for value, label in (
            (payload.add_condition, "加仓条件"),
            (payload.reduce_condition, "减仓条件"),
            (payload.exit_condition, "清仓条件"),
            (payload.no_trade_condition, "不交易条件"),
            (payload.target_plan, "目标或移动止盈"),
        )
        if not value
    ]
    values["execution"] = (
        "通过" if not missing_execution else "无法判断",
        "买入后的加、减、退与放弃条件完整。" if not missing_execution else "执行计划不完整。",
        missing_execution,
    )
    return [
        {
            "gate_code": code,
            "gate_name": name,
            "status": values[code][0],
            "basis": values[code][1],
            "missing_data": values[code][2],
            "rule_version": rule_version,
            "checked_at": datetime.now(),
        }
        for code, name in GATES
    ]


def create_trade_plan(db: Session, payload: TradePlanCreate) -> TradePlan:
    account = db.get(Account, payload.account_id)
    if account is None:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "账户不存在")
    rule = ensure_default_rule_version(db)
    entry = payload.buy_zone_high
    per_share_risk = entry - payload.initial_stop
    risk_budget = account.total_assets * payload.risk_pct / Decimal("100")
    risk_quantity = (risk_budget / per_share_risk).to_integral_value(rounding=ROUND_FLOOR)
    position_cap = account.total_assets * payload.max_position_pct / Decimal("100")
    position_quantity = (position_cap / entry).to_integral_value(rounding=ROUND_FLOOR)
    cash_quantity = (account.available_cash / entry).to_integral_value(rounding=ROUND_FLOOR)
    raw_quantity = min(risk_quantity, position_quantity, cash_quantity)
    quantity = int(raw_quantity // 100 * 100)
    checks = _gate_values(payload, quantity, rule.version)
    statuses = {item["status"] for item in checks}
    if "不通过" in statuses:
        status = "BLOCKED"
        next_action = "存在不通过的交易闸门：不要执行买入，先处理对应风险。"
    elif "无法判断" in statuses:
        status = "DRAFT"
        next_action = "数据或计划尚未补齐：完善无法判断项后再评估。"
    else:
        status = "READY"
        next_action = "计划条件完整；仅在买入触发条件出现时按计划执行，不代表建议买入。"
    status = "DRAFT"
    next_action = (
        "Manual draft only; 不要执行买入，需先运行受管分析并冻结正式计划。"
    )
    plan = TradePlan(
        **payload.model_dump(),
        rule_version_id=rule.id,
        status=status,
        account_equity=account.total_assets,
        planned_quantity=quantity,
        planned_position_value=entry * quantity,
        planned_risk_amount=per_share_risk * quantity,
        next_action=next_action,
        data_status="manual_draft",
        source="用户事前计划 + 规则化仓位计算",
    )
    db.add(plan)
    db.flush()
    for item in checks:
        db.add(TradePlanCheck(trade_plan_id=plan.id, **item))
    db.commit()
    db.refresh(plan)
    return plan


def serialize_trade_plan(db: Session, plan: TradePlan) -> dict:
    checks = db.scalars(
        select(TradePlanCheck)
        .where(TradePlanCheck.trade_plan_id == plan.id)
        .order_by(TradePlanCheck.id)
    ).all()
    rule = db.get(RuleVersion, plan.rule_version_id)
    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "symbol": plan.symbol,
        "name": plan.name,
        "status": plan.status,
        "execution_status": plan.execution_status,
        "execution_summary": plan.execution_summary,
        "trade_mode": plan.trade_mode,
        "decision_level": plan.decision_level,
        "market_state": plan.market_state,
        "sector_state": plan.sector_state,
        "large_cycle_direction": plan.large_cycle_direction,
        "industry_logic": plan.industry_logic,
        "company_logic": plan.company_logic,
        "technical_structure": plan.technical_structure,
        "buy_zone": [float(plan.buy_zone_low), float(plan.buy_zone_high)],
        "initial_stop": float(plan.initial_stop),
        "invalidation_condition": plan.invalidation_condition,
        "target_plan": plan.target_plan,
        "account_equity": float(plan.account_equity),
        "risk_pct": float(plan.risk_pct),
        "max_position_pct": float(plan.max_position_pct),
        "planned_quantity": plan.planned_quantity,
        "planned_position_value": float(plan.planned_position_value),
        "planned_risk_amount": float(plan.planned_risk_amount),
        "add_condition": plan.add_condition,
        "reduce_condition": plan.reduce_condition,
        "exit_condition": plan.exit_condition,
        "no_trade_condition": plan.no_trade_condition,
        "next_action": plan.next_action,
        "checks": [
            {
                "name": item.gate_name,
                "status": item.status,
                "basis": item.basis,
                "missing_data": item.missing_data,
                "rule_version": item.rule_version,
            }
            for item in checks
        ],
        "rule_version": rule.version if rule else "数据不足，无法判断",
        "source": plan.source,
        "data_date": plan.data_date.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
        "data_status": plan.data_status,
    }


def assess_positions(
    db: Session, account_id: int | None = None, persist: bool = False
) -> list[dict]:
    query = select(Holding).order_by(Holding.id)
    if account_id is not None:
        query = query.where(Holding.account_id == account_id)
    holdings = db.scalars(query).all()
    result = []
    for holding in holdings:
        account = db.get(Account, holding.account_id)
        plan = db.scalar(
            select(TradePlan)
            .where(
                TradePlan.account_id == holding.account_id,
                TradePlan.symbol == holding.symbol,
                TradePlan.status.in_(("READY", "DRAFT", "BLOCKED")),
            )
            .order_by(TradePlan.id.desc())
        )
        hard_stop = bool(
            holding.stop_loss_price is not None and holding.current_price <= holding.stop_loss_price
        )
        if hard_stop:
            stage, logic_status = "转弱", "不成立"
            next_action = "已触及硬止损：按原计划处理，不得放宽止损或临时改成长线。"
        elif plan is None:
            stage, logic_status = "观察", "无法判断"
            next_action = "补齐原始交易计划、规则版本、逻辑失效点与下一步动作。"
        else:
            return_pct = (holding.current_price / holding.cost_price - 1) * 100
            stage = "趋势" if return_pct >= 10 else "确认" if return_pct >= 0 else "试错"
            logic_status = "无法判断"
            next_action = plan.next_action
        allow_add = bool(
            plan and plan.status == "READY" and not hard_stop and stage in {"确认", "趋势"}
        )
        metrics = holding_metrics(holding, account)
        risk_amount = (
            max(Decimal("0"), holding.current_price - holding.stop_loss_price) * holding.quantity
            if holding.stop_loss_price is not None
            else None
        )
        item = {
            "holding_id": holding.id,
            "account_id": holding.account_id,
            "symbol": holding.symbol,
            "name": holding.name,
            "current_price": float(holding.current_price),
            "price_source": holding.price_source,
            "price_updated_at": holding.price_updated_at.isoformat()
            if holding.price_updated_at
            else None,
            "position_pct": float(metrics["position_pct"]),
            "stage": stage,
            "logic_status": logic_status,
            "hard_stop_triggered": hard_stop,
            "invalidation_triggered": None,
            "allow_add": allow_add,
            "risk_amount": float(risk_amount) if risk_amount is not None else None,
            "risk_exposure_pct": float(risk_amount / account.total_assets * 100)
            if risk_amount is not None and account.total_assets
            else None,
            "supporting_evidence": [],
            "opposing_evidence": ["已触及硬止损"] if hard_stop else [],
            "next_action": next_action,
            "trade_plan_id": plan.id if plan else None,
            "rule_version": db.get(RuleVersion, plan.rule_version_id).version if plan else None,
            "source": "持仓记录 + 最新关联交易计划",
            "data_date": date.today().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "data_status": "success" if holding.price_updated_at else "stale_or_manual",
        }
        result.append(item)
        if persist:
            db.add(
                PositionSnapshot(
                    holding_id=holding.id,
                    trade_plan_id=plan.id if plan else None,
                    snapshot_date=date.today(),
                    stage=stage,
                    logic_status=logic_status,
                    hard_stop_triggered=hard_stop,
                    invalidation_triggered=None,
                    allow_add=allow_add,
                    risk_amount=risk_amount,
                    risk_exposure_pct=Decimal(str(item["risk_exposure_pct"]))
                    if item["risk_exposure_pct"] is not None
                    else None,
                    supporting_evidence=item["supporting_evidence"],
                    opposing_evidence=item["opposing_evidence"],
                    next_action=next_action,
                    source=item["source"],
                    data_date=date.today(),
                    fetched_at=datetime.now(),
                )
            )
    if persist:
        db.commit()
    return result


def save_position_assessment(
    db: Session, holding_id: int, payload: PositionAssessment
) -> PositionSnapshot:
    holding = db.get(Holding, holding_id)
    if holding is None:
        raise AppError(404, "HOLDING_NOT_FOUND", "持仓不存在")
    base = next(item for item in assess_positions(db) if item["holding_id"] == holding_id)
    hard_stop = base["hard_stop_triggered"]
    allow_add = bool(
        payload.logic_status in {"成立", "部分成立"}
        and not hard_stop
        and payload.invalidation_triggered is not True
        and payload.stage in {"确认", "趋势"}
    )
    next_action = payload.next_action or base["next_action"]
    if hard_stop:
        next_action = "已触及硬止损：按原计划处理，不允许通过修改评估放宽止损。"
    snapshot = PositionSnapshot(
        holding_id=holding.id,
        trade_plan_id=base["trade_plan_id"],
        snapshot_date=date.today(),
        stage=payload.stage or base["stage"],
        logic_status=payload.logic_status,
        hard_stop_triggered=hard_stop,
        invalidation_triggered=payload.invalidation_triggered,
        allow_add=allow_add,
        risk_amount=Decimal(str(base["risk_amount"])) if base["risk_amount"] is not None else None,
        risk_exposure_pct=Decimal(str(base["risk_exposure_pct"]))
        if base["risk_exposure_pct"] is not None
        else None,
        supporting_evidence=payload.supporting_evidence,
        opposing_evidence=payload.opposing_evidence,
        next_action=next_action,
        source="用户持仓复核 + 系统硬止损检查",
        data_date=date.today(),
        fetched_at=datetime.now(),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def dashboard_summary(db: Session) -> dict:
    accounts = db.scalars(select(Account).order_by(Account.id)).all()
    holdings = assess_positions(db)
    alerts = []
    for account in accounts:
        alerts.extend(check_discipline(db, account.id, persist=False))
    critical = [item for item in alerts if item["severity"] == "CRITICAL"]
    risk_state = "防守" if critical else "中性" if holdings else "观察"
    latest_analysis = db.scalar(
        select(PlanAnalysisRun)
        .where(PlanAnalysisRun.status.in_(("success", "confirmed")))
        .order_by(PlanAnalysisRun.created_at.desc())
    )
    latest_market = (
        (latest_analysis.result_snapshot or {}).get("plan", {}).get("market_assessment")
        if latest_analysis
        else None
    )
    if latest_market:
        risk_state = {"高": "防守", "中等": "中性", "低": "进攻"}.get(
            latest_market.get("risk"), risk_state
        )
    plans = db.scalars(
        select(TradePlan).where(TradePlan.status.in_(("READY", "DRAFT", "BLOCKED")))
    ).all()
    near_plans = []
    for plan in plans:
        quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == plan.symbol))
        if quote and plan.buy_zone_low * Decimal(
            "0.98"
        ) <= quote.price <= plan.buy_zone_high * Decimal("1.02"):
            near_plans.append(
                {
                    "plan_id": plan.id,
                    "symbol": plan.symbol,
                    "name": plan.name,
                    "price": float(quote.price),
                    "buy_zone": [float(plan.buy_zone_low), float(plan.buy_zone_high)],
                    "status": plan.status,
                    "source": quote.source,
                    "data_date": quote.fetched_at.date().isoformat(),
                    "updated_at": quote.fetched_at.isoformat(),
                }
            )
    cutoff = datetime.now() - timedelta(hours=24)
    source_logs = db.scalars(
        select(MarketSourceLog).order_by(MarketSourceLog.fetched_at.desc()).limit(30)
    ).all()
    failed_sources = [
        {
            "source": item.source,
            "api": item.api_name,
            "status": item.status,
            "message": item.error or "数据获取失败",
            "updated_at": item.fetched_at.isoformat(),
        }
        for item in source_logs
        if item.status != "success"
    ]
    stale_quotes = db.scalars(select(MarketQuote).where(MarketQuote.fetched_at < cutoff)).all()
    data_issues = failed_sources + [
        {
            "source": item.source,
            "api": item.source_api,
            "status": "stale",
            "message": f"{item.symbol} 行情超过24小时未更新",
            "updated_at": item.fetched_at.isoformat(),
        }
        for item in stale_quotes
    ]
    today_start = datetime.combine(date.today(), datetime.min.time())
    violations = (
        db.scalar(
            select(func.count(DisciplineEvent.id)).where(DisciplineEvent.occurred_at >= today_start)
        )
        or 0
    )
    unplanned = (
        db.scalar(
            select(func.count(Trade.id)).where(
                Trade.traded_at >= today_start, Trade.is_planned.is_(False)
            )
        )
        or 0
    )
    priorities = []
    if critical:
        priorities.append("先处理已触及止损或超仓等严重持仓风险。")
    if any(plan.status != "READY" for plan in plans):
        priorities.append("补齐草稿或被阻止交易计划中的缺失条件。")
    if data_issues:
        priorities.append("刷新失败或过期的数据源，再做研究判断。")
    if not priorities:
        priorities.append("复核候选计划触发条件；没有条件出现时保持观察。")
    return {
        "market": {
            "state": latest_market.get("state", "无法判断") if latest_market else "无法判断",
            "risk_state": risk_state,
            "explanation": (
                f"最近一次一键分析使用沪深300规则模型：20日涨跌 {latest_market.get('return_20d')}%，市场风险 {latest_market.get('risk')}。"
                if latest_market
                else "尚未运行一键分析，暂无宽基市场判断。"
            ),
            "missing_data": ["市场成交额与多行业主线持续性"]
            if latest_market
            else ["沪深300规则状态", "市场成交额与主线持续性"],
            "source": "最近一次一键分析 / 沪深300确定性规则"
            if latest_market
            else "系统数据完整性检查",
            "data_date": latest_analysis.created_at.date().isoformat()
            if latest_analysis
            else date.today().isoformat(),
            "updated_at": latest_analysis.updated_at.isoformat()
            if latest_analysis
            else datetime.now().isoformat(),
            "status": "success" if latest_market else "insufficient",
        },
        "holding_risks": [
            item for item in holdings if item["hard_stop_triggered"] or not item["trade_plan_id"]
        ],
        "near_plans": near_plans,
        "discipline": {
            "today_violations": int(violations),
            "unplanned_trades": int(unplanned),
            "current_alerts": alerts,
        },
        "data_issues": data_issues,
        "priorities": priorities,
    }
