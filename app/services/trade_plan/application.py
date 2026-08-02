from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

import pandas as pd

from app.domain.features import FeatureQuality
from app.domain.repository import HoldingRecord, TradePlanReadRepository
from app.domain.risk import RiskContext, RiskStatus
from app.errors import AppError
from app.schemas_workflow import TradePlanPreviewRequest
from app.services.account_equity import account_drawdown
from app.services.decision_engine import compatibility_context, evaluate_decision
from app.services.features import FeaturePipeline
from app.services.event_anchor_data import load_event_anchor_inputs
from app.services.portfolio_risk_context import calculate_portfolio_risk
from app.services.price_planner import plan_prices
from app.services.repository import build_trade_plan_repository
from app.services.risk_engine import (
    evaluate_risk,
    legacy_position_calculation,
)
from app.services.strategy_evaluation import evaluate_platform_breakout, strategy_gates
from app.services.trade_plan.assembler import assemble_preview
from app.services.trade_plan.compatibility import (
    GENERATOR_PARAMETERS,
    GENERATOR_RULES,
    legacy_gate,
    legacy_preview,
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def generate_trade_plan(
    db: Any,
    request: TradePlanPreviewRequest,
    repository: TradePlanReadRepository | None = None,
) -> dict:
    repository = repository or build_trade_plan_repository(db)
    account = repository.get_account(request.account_id)
    if account is None:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "账户不存在")
    rule = repository.ensure_rule_version()
    parameters = {**GENERATOR_PARAMETERS, **rule.parameters}
    profile = repository.get_company_profile(request.symbol)
    stored_holding = repository.get_holding(request.account_id, request.symbol)
    latest_bar = repository.get_latest_bar(request.symbol)
    quote = repository.get_quote(request.symbol)
    holding = stored_holding
    if request.position_mode == "空仓":
        holding = None
    elif (
        request.position_mode == "持仓" and request.holding_quantity and request.holding_cost_price
    ):
        reference_price = (
            quote.price if quote else latest_bar.close if latest_bar else request.holding_cost_price
        )
        holding = HoldingRecord(
            symbol=request.symbol,
            quantity=request.holding_quantity,
            cost_price=request.holding_cost_price,
            current_price=reference_price,
            stop_loss_price=stored_holding.stop_loss_price if stored_holding else None,
            target_price=stored_holding.target_price if stored_holding else None,
            sector=stored_holding.sector
            if stored_holding
            else (profile.industry if profile else None),
        )
    missing = []
    try:
        frame = repository.load_qfq_frame(request.symbol)
    except ValueError as exc:
        frame = None
        missing.append(str(exc))
    now = datetime.now()
    data_time = latest_bar.fetched_at.isoformat() if latest_bar else "数据不足"
    data_date = latest_bar.trade_date.isoformat() if latest_bar else None
    stale = not latest_bar or latest_bar.trade_date < date.today() - timedelta(
        days=int(parameters["freshness_days"])
    )
    source = latest_bar.source if latest_bar else "数据不足"
    feature_snapshot = FeaturePipeline().build(
        symbol=request.symbol,
        as_of=data_date or date.today(),
        market_data=frame,
        parameters=parameters,
        source_ids=(source,) if latest_bar else (),
        data_time=data_time,
        quality=FeatureQuality.STALE if stale else FeatureQuality.GOOD,
        missing_reason=missing[0] if missing else None,
    )
    if frame is not None:
        pattern = feature_snapshot.value("platform_structure")
        timeframe_facts = feature_snapshot.value("multi_timeframe")
    else:
        pattern = None
        timeframe_facts = None
    gates = []
    market_status = (
        "不通过"
        if request.market_state == "下降"
        else "无法判断"
        if request.market_state == "无法判断"
        else "警告"
        if request.market_state == "震荡"
        else "通过"
    )
    gates.append(
        legacy_gate(
            "market",
            "市场环境",
            market_status,
            request.market_evidence or f"兼容旧入口的用户选择：{request.market_state}。",
            request.market_source or "用户判断（旧入口）",
            request.market_data_time or now.isoformat(),
            ["可靠宽基指数状态"] if request.market_state == "无法判断" else [],
        )
    )
    sector_status = (
        "不通过"
        if request.sector_state == "弱"
        else "无法判断"
        if request.sector_state == "无法判断"
        else "警告"
        if request.sector_state == "中性"
        else "通过"
    )
    gates.append(
        legacy_gate(
            "sector",
            "行业/板块强弱",
            sector_status,
            request.sector_evidence
            or f"行业：{profile.industry if profile else '数据不足'}；兼容旧入口的用户选择：{request.sector_state}。",
            request.sector_source or "公司概况 + 用户判断（旧入口）",
            request.sector_data_time
            or (profile.fetched_at.isoformat() if profile else now.isoformat()),
            ["行业指数相对强弱"] if request.sector_state == "无法判断" else [],
        )
    )
    gates.append(
        legacy_gate(
            "mode", "交易模式和周期", "通过", request.trade_mode, "用户选择", now.isoformat()
        )
    )
    if timeframe_facts is not None:
        weekly = timeframe_facts["weekly_state"]
        monthly = timeframe_facts["monthly_state"]
        daily = timeframe_facts["daily_state"]
    else:
        weekly = monthly = daily = {"state": "无法判断", "evidence": "日线数据不足"}
    strategy_result = evaluate_platform_breakout(
        symbol=request.symbol,
        feature_snapshot=feature_snapshot,
        parameters=parameters,
        position_mode=request.position_mode,
        position_context={"has_position": holding is not None},
        market_context={"state": request.market_state, "missing_data": tuple(missing)},
        sector_context={"state": request.sector_state},
    )
    gates.extend(strategy_gates(strategy_result))
    current_price = float(quote.price) if quote else pattern["latest_close"] if pattern else None
    minute_sessions, institutional_evidence = (
        load_event_anchor_inputs(
            db,
            request.symbol,
            as_of=pd.Timestamp(data_date).date() if data_date else date.today(),
        )
        if db is not None
        else ({}, ())
    )
    price_plan = plan_prices(
        strategy_result=strategy_result,
        feature_snapshot=feature_snapshot,
        market_data=frame,
        parameters=parameters,
        minute_sessions=minute_sessions,
        institutional_evidence=institutional_evidence,
    )
    stop = price_plan.stop_price
    entry_reference = price_plan.entry_reference
    reward_risk = price_plan.reward_risk
    first_target = price_plan.first_target
    second_target = price_plan.second_target
    holdings = list(repository.get_holdings(account.id))
    if request.position_mode == "空仓":
        holdings = [item for item in holdings if item.symbol != request.symbol]
    elif request.position_mode == "持仓" and holding is not None:
        holdings = [item for item in holdings if item.symbol != request.symbol] + [holding]
    total_value = sum(Decimal(item.quantity) * item.current_price for item in holdings)
    industry = profile.industry if profile else None
    industry_value = sum(
        Decimal(item.quantity) * item.current_price
        for item in holdings
        if industry and item.sector == industry
    )
    existing_value = Decimal(holding.quantity) * holding.current_price if holding else Decimal(0)
    risk_holdings = [
        HoldingRecord(
            symbol=item.symbol,
            quantity=item.quantity,
            cost_price=item.cost_price,
            current_price=item.current_price,
            stop_loss_price=Decimal(str(stop)),
            target_price=item.target_price,
            sector=item.sector,
        )
        if item.symbol == request.symbol and item.stop_loss_price is None and stop is not None
        else item
        for item in holdings
    ]
    portfolio_risk = calculate_portfolio_risk(
        risk_holdings,
        equity=account.total_assets,
    )
    drawdown = (
        account_drawdown(db, account.id, current_equity=account.total_assets)
        if db is not None
        else None
    )
    risk_result = evaluate_risk(
        RiskContext(
            account_context={
                "equity": account.total_assets,
                "available_cash": account.available_cash,
                "risk_pct": request.risk_pct,
                "max_position_pct": request.max_position_pct,
                "max_total_position_pct": request.max_total_position_pct,
                "max_industry_position_pct": request.max_industry_position_pct,
                "trial_position_ratio": parameters["trial_position_ratio"],
                "max_portfolio_risk_pct": parameters.get("max_portfolio_risk_pct", 5),
                "current_drawdown_pct": drawdown.drawdown_pct if drawdown else 0,
                "max_account_drawdown_pct": parameters.get("max_account_drawdown_pct", 8),
            },
            position_context={
                "existing_symbol_value": existing_value,
                "total_position_value": total_value,
                "open_risk_amount": portfolio_risk.open_risk_amount,
                "portfolio_risk_complete": portfolio_risk.complete,
                "holdings_without_stop": portfolio_risk.holdings_without_stop,
            },
            entry_context={"entry_price": entry_reference},
            stop_context={
                "stop_price": stop,
                "maximum_stop_distance_pct": parameters["maximum_stop_distance_pct"],
                "reward_risk": reward_risk,
                "minimum_reward_risk": parameters["minimum_reward_risk"],
            },
            market_context={"state": request.market_state},
            sector_context={
                "state": request.sector_state,
                "sector_position_value": industry_value,
            },
        )
    )
    calculations = legacy_position_calculation(risk_result)
    trial_quantity = calculations.get("trial_quantity", 0)
    stop_distance_pct = risk_result.calculation_details.get("stop_distance_pct")
    stop_status = risk_result.calculation_details.get("stop_status", "无法判断")
    gates.append(
        legacy_gate(
            "stop",
            "硬止损是否明确",
            stop_status,
            f"硬止损 {stop if stop is not None else '无法计算'}；距参考买入价 {stop_distance_pct:.2f}%"
            if stop_distance_pct is not None
            else "平台或ATR数据不足，无法可靠计算硬止损。",
            source,
            data_time,
            [] if stop is not None else ["平台下沿、有效低点或ATR"],
        )
    )
    rr_status = risk_result.calculation_details.get("reward_risk_status", "无法判断")
    gates.append(
        legacy_gate(
            "reward_risk",
            "风险收益是否合格",
            rr_status,
            f"平台高度目标 {first_target}，预期盈亏比 {reward_risk:.2f}:1；最低要求 {parameters['minimum_reward_risk']}:1。"
            if reward_risk is not None
            else "无法计算风险收益比。",
            "规则引擎",
            data_time,
        )
    )
    position_status = (
        "无法判断"
        if risk_result.status is RiskStatus.INSUFFICIENT_DATA
        else "不通过"
        if risk_result.status is RiskStatus.BLOCKED
        else "通过"
    )
    gates.append(
        legacy_gate(
            "position",
            "仓位是否超过账户限制",
            position_status,
            calculations.get("formula", "缺少有效买入价或止损价，无法计算仓位。"),
            "账户、持仓和用户风险参数",
            now.isoformat(),
            [] if calculations else ["有效买入价与硬止损"],
        )
    )
    data_status = "无法判断" if missing or stale else "通过"
    gates.append(
        legacy_gate(
            "data",
            "数据完整性和新鲜度",
            data_status,
            f"最近K线 {data_date or '缺失'}；{'数据已过期' if stale else '数据在允许时效内'}；60分钟和换手率尚未接入。",
            source,
            data_time,
            [*missing, "60分钟K线", "换手率"] if missing or stale else ["60分钟K线", "换手率"],
        )
    )
    statuses = {item["code"]: item["status"] for item in gates}
    decision_result = evaluate_decision(
        compatibility_context(
            strategy_result=strategy_result,
            gate_statuses=statuses,
            entry_capacity_allowed=trial_quantity >= 100,
            position_context={
                "has_position": holding is not None,
                "current_price": current_price,
                "cost_price": holding.cost_price if holding else None,
                "stop_loss_price": holding.stop_loss_price if holding else None,
                "target_price": holding.target_price if holding else None,
                "platform_broken": pattern["platform_broken"] if pattern else False,
            },
        )
    )
    final_status = decision_result.legacy_plan_status
    floating_profit = decision_result.position_evidence["floating_profit"]
    hard_stop_triggered = decision_result.position_evidence["hard_stop_triggered"]
    first_reduction_triggered = decision_result.position_evidence["first_reduction_triggered"]
    confirmation_add_allowed = decision_result.position_evidence["confirmation_add_allowed"]
    current_allowed = final_status == "READY" and trial_quantity >= 100
    buy_low = price_plan.entry_zone.low
    buy_high = price_plan.entry_zone.high
    reasons = [item["evidence"] for item in gates if item["status"] in {"不通过", "无法判断"}]
    next_items = [item["evidence"] for item in gates if item["status"] in {"警告", "无法判断"}][:5]
    sources = [
        {
            "source_id": "market_bars",
            "name": source,
            "data_date": data_date,
            "fetched_at": data_time,
            "stale": stale,
        },
        {
            "source_id": "account",
            "name": "本地账户设置",
            "data_date": date.today().isoformat(),
            "fetched_at": now.isoformat(),
            "stale": False,
        },
        {
            "source_id": "company_profile",
            "name": profile.source if profile else "数据不足",
            "data_date": None,
            "fetched_at": profile.fetched_at.isoformat() if profile else None,
            "stale": profile is None,
        },
        {
            "source_id": "market_sector_context",
            "name": "自动市场/行业规则" if request.market_evidence else "用户判断（旧入口）",
            "data_date": date.today().isoformat(),
            "fetched_at": now.isoformat(),
            "stale": False,
        },
    ]
    preview = {
        "symbol": request.symbol,
        "company_name": profile.name if profile else quote.name if quote else request.symbol,
        "status": final_status,
        "status_reason": reasons or ["全部关键闸门通过，只有触发条件实际出现时才允许按计划试错。"],
        "missing_conditions": sorted(
            {missing for item in gates for missing in item["missing_conditions"]}
        ),
        "next_observations": next_items or ["持续检查板块、公司逻辑和结构是否变化。"],
        "current_buy_allowed": current_allowed,
        "rule": {"version": rule.version, "name": rule.rules["name"], "parameters": parameters},
        "account": {
            "id": account.id,
            "equity": float(account.total_assets),
            "available_cash": float(account.available_cash),
            "current_total_position_pct": round(float(total_value / account.total_assets * 100), 2)
            if account.total_assets
            else 0,
            "risk_pct": float(request.risk_pct),
            "max_position_pct": float(request.max_position_pct),
            "max_total_position_pct": float(request.max_total_position_pct),
            "max_industry_position_pct": float(request.max_industry_position_pct),
        },
        "existing_position": {
            "exists": bool(holding),
            "quantity": holding.quantity if holding else 0,
            "cost_price": float(holding.cost_price) if holding else None,
            "current_price": current_price,
            "position_pct": round(
                float(
                    Decimal(holding.quantity) * holding.current_price / account.total_assets * 100
                ),
                2,
            )
            if holding and account.total_assets
            else 0,
            "floating_profit": floating_profit,
            "confirmation_add_allowed": confirmation_add_allowed,
            "hard_stop_triggered": hard_stop_triggered,
            "first_reduction_triggered": first_reduction_triggered,
            "note": "已有持仓不生成重复首次开仓；只检查确认加仓与退出条件。"
            if holding
            else "当前账户未持有该股票。",
        },
        "account_risk": {
            "portfolio_open_risk": float(portfolio_risk.open_risk_amount),
            "portfolio_open_risk_pct": portfolio_risk.open_risk_pct,
            "risk_complete": portfolio_risk.complete,
            "holdings_without_stop": portfolio_risk.holdings_without_stop,
            "warnings": list(portfolio_risk.warnings),
            "peak_equity": float(drawdown.peak_equity) if drawdown else None,
            "current_drawdown_pct": drawdown.drawdown_pct if drawdown else None,
            "drawdown_observations": drawdown.observation_count if drawdown else 0,
            "drawdown_circuit_breaker": risk_result.calculation_details.get(
                "drawdown_circuit_breaker", False
            ),
            "stress_losses": risk_result.calculation_details.get("stress_losses", {}),
        },
        "multi_timeframe": {
            "monthly": monthly,
            "weekly": weekly,
            "daily": daily,
            "60min": {"state": "无法判断", "evidence": "尚未接入可靠60分钟数据"},
        },
        "pattern": pattern,
        "market_structure": _json_value(asdict(price_plan.market_structure))
        if price_plan.market_structure
        else None,
        "chart": [
            {
                "date": pd.Timestamp(index).date().isoformat(),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
                "volume": round(float(row["Volume"]), 2),
            }
            for index, row in frame.tail(120).iterrows()
        ]
        if frame is not None
        else [],
        "gates": gates,
        "buy_plan": {
            "allowed": current_allowed,
            "buy_zone": [buy_low, buy_high],
            "turn_trigger_price": pattern["turn_trigger_price"] if pattern else None,
            "trigger_condition": "收盘越过转强触发价、超过前一日高点，且成交量不低于20日均量。",
            "hard_stop": stop,
            "stop_cannot_move_down": True,
            "structure_invalidation": price_plan.structure_invalidation,
            "logic_invalidation": request.logic_invalidation
            or "行业/公司核心假设、业绩订单需求或治理风险恶化时退出。",
            "abandon_conditions": GENERATOR_RULES["hard_prohibitions"],
        },
        "position_calculation": calculations,
        "confirmation_add": {
            "allowed": confirmation_add_allowed,
            "requirements": [
                "首仓浮盈",
                "突破有效",
                "回踩未破位",
                "再次放量转强",
                "板块未转弱",
                "产业和公司逻辑未恶化",
                "总风险未超限",
                "浮盈覆盖部分新增风险",
            ],
            "prohibited": [
                "股价下跌后降低成本",
                "触发硬止损后加仓",
                "平台跌破后加仓",
                "为了快速回本加仓",
            ],
            "stages": {
                "首次试错": "计划仓位25%—40%",
                "回踩确认": "计划仓位60%—80%",
                "趋势确认": "不超过计划仓位上限",
            },
        },
        "exit_plan": {
            "first_reduction": f"到达平台高度目标 {first_target} 或达到 {parameters['minimum_reward_risk']}R 后评估减仓。"
            if first_target
            else "数据不足，无法判断",
            "second_reduction": f"价格达到 {second_target}（约3R）后，结合周线压力与量价状态再次分批减仓。"
            if second_target
            else "数据不足，无法判断",
            "trailing_stop": "按最近有效回踩低点、5/20日均线、趋势线或ATR移动止盈；不得向下放宽硬止损。",
            "final_exit": [
                "放量跌破关键结构",
                "跌破关键均线后无法收回",
                "板块和行业同步转弱",
                "高位放量滞涨",
                "公司或产业逻辑失效",
                "估值上涨显著快于盈利兑现",
            ],
            "types": ["减仓", "清仓", "硬止损", "移动止盈", "逻辑退出"],
        },
        "sources": sources,
        "data_status": "stale" if stale else "success" if frame is not None else "insufficient",
        "data_date": data_date,
        "generated_at": now.isoformat(),
        "disclaimer": "这是条件式研究计划，不预测涨跌、不连接券商、不自动下单。",
    }
    return legacy_preview(
        assemble_preview(
            request.symbol,
            preview,
            strategy_result=strategy_result,
            risk_result=risk_result,
            decision_result=decision_result,
        )
    )
