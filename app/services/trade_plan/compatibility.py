from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.trade_plan import TradePlanPreview

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


def legacy_gate(
    code: str,
    name: str,
    status: str,
    evidence: str,
    source: str,
    data_time: str,
    missing=None,
) -> dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "status": status,
        "evidence": evidence,
        "missing_conditions": missing or [],
        "source": source,
        "data_time": data_time,
    }


def preview_digest(preview: dict[str, Any]) -> str:
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


def legacy_preview(preview: TradePlanPreview) -> dict[str, Any]:
    payload = dict(preview.payload)
    payload["preview_hash"] = preview_digest(payload)
    return payload
