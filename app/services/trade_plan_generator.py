from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.data_hub.trading_calendar import get_trading_calendar
from app.models import (
    Account,
    CompanyProfile,
    Holding,
    MarketDailyBar,
    MarketQuote,
    RuleVersion,
    TradePlan,
    TradePlanAIAnalysis,
    TradePlanCheck,
)
from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest
from app.services.technical import prepare_indicators
from app.services.technical_snapshots import load_qfq_frame
from app.services.workflow import ensure_default_rule_version


GENERATOR_PARAMETERS = {
    "default_account_equity": 300000,
    "default_risk_pct": 0.5,
    "max_single_position_pct": 30,
    "max_total_position_pct": 80,
    "max_industry_position_pct": 40,
    "position_tranches": 3,
    "market_high_risk_total_cap_pct": 30,
    "market_neutral_total_cap_pct": 60,
    "platform_min_days": 20,
    "breakout_pct": 1.0,
    "breakout_volume_multiple": 1.5,
    "pullback_tolerance_pct": 3.0,
    "pullback_volume_ratio": 0.8,
    "ma_periods": [5, 20, 60, 250],
    "atr_buffer_multiple": 0.5,
    "minimum_reward_risk": 2.0,
    "maximum_stop_distance_pct": 8.0,
    "freshness_days": 5,
    "trial_position_ratio": 0.3333,
    "pullback_confirmed_ratio": 0.7,
}

GENERATOR_RULES = {
    "name": "日线趋势波段：平台放量突破—缩量回踩—再次转强",
    "hard_prohibitions": [
        "市场明显下降",
        "周线明显下降",
        "下降趋势中只有一根放量阳线",
        "未形成有效平台",
        "突破后放量跌回平台",
        "远离计划买入区或连续上涨后追高",
        "板块明显转弱",
        "止损距离或风险收益不合格",
        "数据不足或过期",
    ],
    "confirmation_add": "只在首仓浮盈、结构有效、再次放量转强且总风险未超限时允许确认加仓。",
}


def ensure_generator_rule_version(db: Session) -> RuleVersion:
    current = ensure_default_rule_version(db)
    if all(key in current.parameters for key in GENERATOR_PARAMETERS):
        return current
    current.active = False
    parameters = {**current.parameters, **GENERATOR_PARAMETERS}
    version = RuleVersion(
        rule_set_id=current.rule_set_id,
        version="1.2.0" if "platform_min_days" in current.parameters else "1.1.0",
        parameters=parameters,
        rules={**current.rules, **GENERATOR_RULES},
        change_note="集中一键计划的账户、风险、分批仓位和市场降风险参数；旧计划保持原规则版本。",
        effective_from=date.today(),
        active=True,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _direction(close: pd.Series, fast: int, slow: int) -> dict:
    if len(close) < slow:
        return {"state": "无法判断", "evidence": f"需要至少 {slow} 根，当前 {len(close)} 根"}
    fast_ma = float(close.rolling(fast).mean().iloc[-1])
    slow_ma = float(close.rolling(slow).mean().iloc[-1])
    current = float(close.iloc[-1])
    state = (
        "向上" if current > fast_ma > slow_ma else "向下" if current < fast_ma < slow_ma else "震荡"
    )
    return {
        "state": state,
        "close": round(current, 4),
        f"ma{fast}": round(fast_ma, 4),
        f"ma{slow}": round(slow_ma, 4),
        "evidence": f"收盘 {current:.2f}，MA{fast} {fast_ma:.2f}，MA{slow} {slow_ma:.2f}",
    }


def _gate(
    code: str, name: str, status: str, evidence: str, source: str, data_time: str, missing=None
):
    return {
        "code": code,
        "name": name,
        "status": status,
        "evidence": evidence,
        "missing_conditions": missing or [],
        "source": source,
        "data_time": data_time,
    }


def _floor_lot(value: float | Decimal) -> int:
    return max(0, int(Decimal(str(value)).to_integral_value(rounding=ROUND_FLOOR)) // 100 * 100)


def _platform_pattern(frame: pd.DataFrame, parameters: dict) -> dict:
    prepared = prepare_indicators(frame)
    window = int(parameters["platform_min_days"])
    pct = float(parameters["breakout_pct"]) / 100
    volume_multiple = float(parameters["breakout_volume_multiple"])
    recent_start = max(window, len(prepared) - 45)
    candidates = []
    for index in range(recent_start, len(prepared)):
        prior = prepared.iloc[index - window : index]
        upper = float(prior["High"].max())
        lower = float(prior["Low"].min())
        volume_base = float(prior["Volume"].mean())
        row = prepared.iloc[index]
        if float(row["Close"]) >= upper * (1 + pct):
            candidates.append(
                {
                    "index": index,
                    "upper": upper,
                    "lower": lower,
                    "price": float(row["Close"]),
                    "date": pd.Timestamp(prepared.index[index]).date().isoformat(),
                    "volume_ratio": float(row["Volume"]) / volume_base if volume_base else 0,
                    "volume_confirmed": bool(
                        volume_base and float(row["Volume"]) >= volume_base * volume_multiple
                    ),
                }
            )
    confirmed_candidates = [item for item in candidates if item["volume_confirmed"]]
    if candidates:
        # 保留本轮结构中最早的有效突破，避免把“再次转强”误识别成新的首次突破。
        breakout = (confirmed_candidates or candidates)[0]
        upper, lower = breakout["upper"], breakout["lower"]
    else:
        prior = prepared.tail(window)
        upper, lower = float(prior["High"].max()), float(prior["Low"].min())
        breakout = None
    platform_range_pct = (upper / lower - 1) * 100 if lower else 999
    valid_platform = platform_range_pct <= 25
    post = prepared.iloc[breakout["index"] + 1 :] if breakout else prepared.iloc[0:0]
    pullback_sample = post.iloc[:-1] if len(post) > 1 else post
    tolerance = float(parameters["pullback_tolerance_pct"]) / 100
    pullback_rows = pullback_sample[
        (pullback_sample["Low"] <= upper * (1 + tolerance))
        & (pullback_sample["Close"] <= upper * (1 + tolerance))
    ]
    pullback_seen = not pullback_rows.empty
    platform_broken = bool(
        not post.empty and ((post["Close"] < lower) | (post["Low"] < lower * (1 - tolerance))).any()
    )
    pullback_volume_ratio = None
    pullback_shrinking = False
    pullback_low = None
    if pullback_seen:
        pullback_volume_ratio = float(
            pullback_rows["Volume"].mean() / prepared["VOL_MA20"].iloc[-1]
        )
        pullback_shrinking = pullback_volume_ratio <= float(parameters["pullback_volume_ratio"])
        pullback_low = float(pullback_rows["Low"].min())
    last, previous = prepared.iloc[-1], prepared.iloc[-2]
    turn_trigger = max(upper, float(post["High"].iloc[:-1].max()) if len(post) > 1 else upper)
    turned_stronger = bool(
        breakout
        and pullback_seen
        and float(last["Close"]) > turn_trigger
        and float(last["Close"]) > float(previous["High"])
        and float(last["Volume"]) >= float(last["VOL_MA20"])
    )
    return {
        "platform_days": window,
        "platform_start": pd.Timestamp(prepared.index[-window]).date().isoformat(),
        "platform_end": pd.Timestamp(prepared.index[-1]).date().isoformat(),
        "platform_upper": round(upper, 4),
        "platform_lower": round(lower, 4),
        "platform_range_pct": round(platform_range_pct, 2),
        "valid_platform": valid_platform,
        "breakout": breakout,
        "pullback_seen": pullback_seen,
        "pullback_range": [round(pullback_low, 4), round(upper * (1 + tolerance), 4)]
        if pullback_low
        else None,
        "pullback_volume_ratio": round(pullback_volume_ratio, 2)
        if pullback_volume_ratio is not None
        else None,
        "pullback_shrinking": pullback_shrinking,
        "platform_broken": platform_broken,
        "turn_trigger_price": round(turn_trigger, 4),
        "turned_stronger": turned_stronger,
        "latest_close": round(float(last["Close"]), 4),
        "latest_volume_ratio": round(
            float(last["Volume"] / last["VOL_MA20"]) if last["VOL_MA20"] else 0, 2
        ),
        "atr14": round(float(last["ATRr_14"]), 4),
        "ma": {
            "ma5": round(float(last["SMA_5"]), 4),
            "ma20": round(float(last["SMA_20"]), 4),
            "ma60": round(float(last["SMA_60"]), 4),
            "ma250": round(float(prepared["Close"].rolling(250).mean().iloc[-1]), 4)
            if len(prepared) >= 250
            else None,
        },
        "macd": round(float(last["MACD_12_26_9"]), 4),
        "rsi14": round(float(last["RSI_14"]), 2),
    }


def _preview_digest(preview: dict) -> str:
    frozen = {
        "symbol": preview["symbol"],
        "status": preview["status"],
        "rule": preview["rule"],
        "account": preview["account"],
        "existing_position": preview["existing_position"],
        "multi_timeframe": preview["multi_timeframe"],
        "pattern": preview["pattern"],
        "buy_plan": preview["buy_plan"],
        "position_calculation": preview["position_calculation"],
        "gate_results": [
            {
                "code": item["code"],
                "status": item["status"],
                "evidence": item["evidence"],
                "source": item["source"],
            }
            for item in preview["gates"]
        ],
        "data_date": preview["data_date"],
    }
    return hashlib.sha256(
        json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def generate_trade_plan_preview(db: Session, request: TradePlanPreviewRequest) -> dict:
    account = db.get(Account, request.account_id)
    if account is None:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "账户不存在")
    rule = ensure_generator_rule_version(db)
    parameters = {**GENERATOR_PARAMETERS, **rule.parameters}
    profile = db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == request.symbol))
    stored_holding = db.scalar(
        select(Holding).where(
            Holding.account_id == request.account_id, Holding.symbol == request.symbol
        )
    )
    latest_bar = db.scalar(
        select(MarketDailyBar)
        .where(MarketDailyBar.symbol == request.symbol)
        .order_by(MarketDailyBar.trade_date.desc(), MarketDailyBar.fetched_at.desc())
    )
    quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == request.symbol))
    holding = stored_holding
    if request.position_mode == "空仓":
        holding = None
    elif request.position_mode == "持仓" and request.holding_quantity and request.holding_cost_price:
        reference_price = (
            quote.price
            if quote
            else latest_bar.close
            if latest_bar
            else request.holding_cost_price
        )
        holding = SimpleNamespace(
            quantity=request.holding_quantity,
            cost_price=request.holding_cost_price,
            current_price=reference_price,
            stop_loss_price=stored_holding.stop_loss_price if stored_holding else None,
            target_price=stored_holding.target_price if stored_holding else None,
            sector=stored_holding.sector if stored_holding else (profile.industry if profile else None),
        )
    missing = []
    try:
        frame = load_qfq_frame(db, request.symbol)
        pattern = _platform_pattern(frame, parameters)
    except ValueError as exc:
        frame, pattern = None, None
        missing.append(str(exc))
    now = datetime.now()
    data_time = latest_bar.fetched_at.isoformat() if latest_bar else "数据不足"
    data_date = latest_bar.trade_date.isoformat() if latest_bar else None
    stale = not latest_bar or get_trading_calendar().session_lag(
        latest_bar.trade_date
    ) > int(parameters["freshness_days"])
    source = latest_bar.source if latest_bar else "数据不足"
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
        _gate(
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
        _gate(
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
        _gate("mode", "交易模式和周期", "通过", request.trade_mode, "用户选择", now.isoformat())
    )
    if frame is not None:
        weekly = _direction(frame["Close"].resample("W-FRI").last().dropna(), 10, 30)
        monthly = _direction(frame["Close"].resample("ME").last().dropna(), 3, 6)
        daily = _direction(frame["Close"], 20, min(60, len(frame)))
        large_state = (
            "向下"
            if "向下" in {weekly["state"], monthly["state"]}
            else "向上"
            if weekly["state"] == monthly["state"] == "向上"
            else "震荡"
        )
        gates.append(
            _gate(
                "large_cycle",
                "月线、周线大周期方向",
                "不通过"
                if weekly["state"] == "向下"
                else "通过"
                if large_state == "向上"
                else "警告",
                f"月线 {monthly['state']}（{monthly['evidence']}）；周线 {weekly['state']}（{weekly['evidence']}）。",
                source,
                data_time,
            )
        )
        gates.append(
            _gate(
                "platform",
                "日线平台和趋势结构",
                "通过" if pattern["valid_platform"] else "不通过",
                f"观察 {pattern['platform_days']} 日，上沿 {pattern['platform_upper']}，下沿 {pattern['platform_lower']}，区间振幅 {pattern['platform_range_pct']}%。",
                source,
                data_time,
            )
        )
        breakout = pattern["breakout"]
        gates.append(
            _gate(
                "breakout",
                "突破成交量",
                "通过"
                if breakout and breakout["volume_confirmed"]
                else "警告"
                if breakout
                else "无法判断",
                (
                    f"{breakout['date']} 收盘 {breakout['price']} 突破，成交量为平台均量 {breakout['volume_ratio']:.2f} 倍。"
                    if breakout
                    else f"尚未收盘突破平台上沿 {pattern['platform_upper']}。"
                ),
                source,
                data_time,
                [] if breakout else ["有效突破"],
            )
        )
        pull_status = (
            "不通过"
            if pattern["platform_broken"]
            else "通过"
            if pattern["pullback_seen"] and pattern["pullback_shrinking"]
            else "警告"
            if pattern["pullback_seen"]
            else "无法判断"
        )
        gates.append(
            _gate(
                "pullback",
                "回踩缩量及结构",
                pull_status,
                f"回踩区 {pattern['pullback_range'] or '尚未出现'}；量能比 {pattern['pullback_volume_ratio']}; 是否破位：{'是' if pattern['platform_broken'] else '否'}。",
                source,
                data_time,
                [] if pattern["pullback_seen"] else ["突破后的回踩样本"],
            )
        )
        gates.append(
            _gate(
                "turn_stronger",
                "再次转强条件",
                "通过" if pattern["turned_stronger"] else "无法判断",
                f"触发价 {pattern['turn_trigger_price']}；要求收盘越过触发价、超过前一日高点且成交量不低于20日均量。",
                source,
                data_time,
                [] if pattern["turned_stronger"] else ["再次放量转强"],
            )
        )
    else:
        weekly = monthly = daily = {"state": "无法判断", "evidence": "日线数据不足"}
        large_state = "无法判断"
        for code, name in (
            ("large_cycle", "月线、周线大周期方向"),
            ("platform", "日线平台和趋势结构"),
            ("breakout", "突破成交量"),
            ("pullback", "回踩缩量及结构"),
            ("turn_stronger", "再次转强条件"),
        ):
            gates.append(
                _gate(code, name, "无法判断", "历史行情数据不足。", source, data_time, missing)
            )
    current_price = float(quote.price) if quote else pattern["latest_close"] if pattern else None
    stop = None
    stop_distance_pct = None
    entry_reference = None
    reward_risk = None
    first_target = None
    second_target = None
    if pattern and pattern["valid_platform"]:
        atr_buffer = pattern["atr14"] * float(parameters["atr_buffer_multiple"])
        recent_low = float(frame["Low"].tail(10).min())
        stop = round(max(pattern["platform_lower"], recent_low) - atr_buffer, 4)
        entry_reference = round(pattern["turn_trigger_price"], 4)
        if stop < entry_reference:
            stop_distance_pct = (entry_reference - stop) / entry_reference * 100
            platform_target = pattern["platform_upper"] + (
                pattern["platform_upper"] - pattern["platform_lower"]
            )
            minimum_r_target = entry_reference + float(parameters["minimum_reward_risk"]) * (
                entry_reference - stop
            )
            raw_first_target = max(platform_target, minimum_r_target)
            first_target = round(raw_first_target, 4)
            second_target = round(entry_reference + 3 * (entry_reference - stop), 4)
            reward_risk = (raw_first_target - entry_reference) / (entry_reference - stop)
    stop_status = (
        "无法判断"
        if stop is None
        else "不通过"
        if stop_distance_pct > float(parameters["maximum_stop_distance_pct"])
        else "通过"
    )
    gates.append(
        _gate(
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
    rr_status = (
        "无法判断"
        if reward_risk is None
        else "通过"
        if reward_risk >= float(parameters["minimum_reward_risk"])
        else "不通过"
    )
    gates.append(
        _gate(
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
    holdings = db.scalars(select(Holding).where(Holding.account_id == account.id)).all()
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
    calculations = {}
    final_quantity = trial_quantity = 0
    if entry_reference and stop and entry_reference > stop:
        equity = account.total_assets
        risk_budget = equity * request.risk_pct / Decimal("100")
        per_share_risk = Decimal(str(entry_reference - stop))
        risk_qty = _floor_lot(risk_budget / per_share_risk)
        cash_qty = _floor_lot(account.available_cash / Decimal(str(entry_reference)))
        existing_value = (
            Decimal(holding.quantity) * holding.current_price if holding else Decimal(0)
        )
        single_remaining = max(
            Decimal(0), equity * request.max_position_pct / Decimal("100") - existing_value
        )
        single_qty = _floor_lot(single_remaining / Decimal(str(entry_reference)))
        total_remaining = max(
            Decimal(0), equity * request.max_total_position_pct / Decimal("100") - total_value
        )
        total_qty = _floor_lot(total_remaining / Decimal(str(entry_reference)))
        industry_remaining = max(
            Decimal(0),
            equity * request.max_industry_position_pct / Decimal("100") - industry_value,
        )
        industry_qty = _floor_lot(industry_remaining / Decimal(str(entry_reference)))
        final_quantity = min(risk_qty, cash_qty, single_qty, total_qty, industry_qty)
        trial_quantity = _floor_lot(final_quantity * float(parameters["trial_position_ratio"]))
        calculations = {
            "risk_budget": round(float(risk_budget), 2),
            "per_share_risk": round(float(per_share_risk), 4),
            "risk_allowed_quantity": risk_qty,
            "cash_allowed_quantity": cash_qty,
            "single_position_allowed_quantity": single_qty,
            "total_position_allowed_quantity": total_qty,
            "industry_concentration_allowed_quantity": industry_qty,
            "final_allowed_quantity": final_quantity,
            "trial_quantity": trial_quantity,
            "trial_amount": round(trial_quantity * entry_reference, 2),
            "trial_account_pct": round(trial_quantity * entry_reference / float(equity) * 100, 2),
            "maximum_loss": round(trial_quantity * float(per_share_risk), 2),
            "formula": "最终数量=min(风险预算、可用资金、单股仓位、总仓位、行业集中度允许数量)，再向下取100股整手",
        }
    position_status = (
        "无法判断" if not calculations else "不通过" if final_quantity < 100 else "通过"
    )
    gates.append(
        _gate(
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
        _gate(
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
    critical_unknown = any(
        statuses[code] == "无法判断"
        for code in ("market", "sector", "large_cycle", "platform", "stop", "reward_risk", "data")
    )
    hard_fail = any(
        statuses[code] == "不通过"
        for code in (
            "market",
            "sector",
            "large_cycle",
            "platform",
            "pullback",
            "stop",
            "reward_risk",
            "position",
        )
    )
    if hard_fail:
        final_status = "NO_TRADE"
    elif critical_unknown:
        final_status = "INSUFFICIENT_DATA"
    elif (
        statuses["breakout"] != "通过"
        or statuses["pullback"] != "通过"
        or not pattern["turned_stronger"]
    ):
        final_status = "WAIT"
    else:
        final_status = "READY"
    if holding:
        floating_profit = current_price is not None and current_price > float(holding.cost_price)
        hard_stop_triggered = bool(
            current_price is not None
            and holding.stop_loss_price is not None
            and current_price <= float(holding.stop_loss_price)
        )
        first_reduction_triggered = bool(
            current_price is not None
            and holding.target_price is not None
            and current_price >= float(holding.target_price)
        )
        confirmation_add_allowed = bool(
            floating_profit
            and final_status == "READY"
            and current_price > float(holding.stop_loss_price or 0)
            and not hard_stop_triggered
        )
        if not confirmation_add_allowed and final_status == "READY":
            final_status = "WAIT"
    else:
        floating_profit = None
        hard_stop_triggered = False
        first_reduction_triggered = False
        confirmation_add_allowed = False
    current_allowed = final_status == "READY" and trial_quantity >= 100
    buy_low = (
        round(entry_reference - pattern["atr14"] * 0.2, 4) if entry_reference and pattern else None
    )
    buy_high = (
        round(entry_reference + pattern["atr14"] * 0.2, 4) if entry_reference and pattern else None
    )
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
        "multi_timeframe": {
            "monthly": monthly,
            "weekly": weekly,
            "daily": daily,
            "60min": {"state": "无法判断", "evidence": "尚未接入可靠60分钟数据"},
        },
        "pattern": pattern,
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
            "structure_invalidation": f"收盘跌破平台下沿 {pattern['platform_lower']} 或放量跌回平台。"
            if pattern
            else "数据不足，无法判断",
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
    preview["preview_hash"] = _preview_digest(preview)
    return preview


def _persist_generated_plan(
    db: Session,
    request: TradePlanSaveRequest,
    *,
    analysis_run_id: int | None = None,
) -> dict:
    preview_request = TradePlanPreviewRequest(
        **request.model_dump(
            exclude={"preview_hash", "ai_analysis_id", "decision_package"}
        )
    )
    preview = generate_trade_plan_preview(db, preview_request)
    if preview["preview_hash"] != request.preview_hash:
        raise AppError(409, "PREVIEW_CHANGED", "数据或规则已变化，请重新生成预览后再确认保存")
    latest = db.scalar(
        select(TradePlan)
        .where(TradePlan.account_id == request.account_id, TradePlan.symbol == request.symbol)
        .order_by(TradePlan.plan_version.desc(), TradePlan.id.desc())
        .with_for_update()
    )
    version = (latest.plan_version or 1) + 1 if latest else 1
    buy_zone = preview["buy_plan"]["buy_zone"]
    stop = preview["buy_plan"]["hard_stop"]
    if not buy_zone[0] or not stop:
        raise AppError(422, "PLAN_NOT_SAVABLE", "缺少可靠买入区或硬止损，不能保存正式计划")
    rule = ensure_generator_rule_version(db)
    quantity = preview["position_calculation"].get("final_allowed_quantity", 0)
    entry = buy_zone[1]
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
            str(
                round(
                    quantity * preview["position_calculation"].get("per_share_risk", 0),
                    2,
                )
            )
        ),
        add_condition="；".join(preview["confirmation_add"]["requirements"]),
        reduce_condition=preview["exit_plan"]["first_reduction"],
        exit_condition="；".join(preview["exit_plan"]["final_exit"]),
        no_trade_condition="；".join(preview["buy_plan"]["abandon_conditions"]),
        next_action="；".join(preview["next_observations"]),
        data_status=preview["data_status"],
        data_date=date.fromisoformat(preview["data_date"]),
        source="确定性交易计划生成器",
        plan_version=version,
        analysis_run_id=analysis_run_id,
        parent_plan_id=latest.id if latest else None,
        preview_hash=preview["preview_hash"],
        engine_snapshot=preview,
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
    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "symbol": plan.symbol,
        "plan_version": version,
        "status": plan.status,
        "execution_status": plan.execution_status,
        "preview": preview,
    }


def save_generated_plan(db: Session, request: TradePlanSaveRequest) -> dict:
    raise AppError(
        422,
        "DECISION_PACKAGE_REQUIRED",
        "Formal plans can only be frozen through the governed analysis confirmation endpoint.",
    )


def plan_history(db: Session, account_id: int, symbol: str) -> list[dict]:
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


def compare_plans(db: Session, first_id: int, second_id: int) -> dict:
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
