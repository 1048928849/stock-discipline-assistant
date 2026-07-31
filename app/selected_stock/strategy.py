from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from typing import Any

from app.domain.hashing import canonical_hash
from app.selected_stock.contracts import (
    ContextStatus,
    CycleState,
    DataStatus,
    GateResult,
    HoldingPlan,
    PlanStatus,
    PositionPlan,
    PricePlan,
    ScoreCard,
    ScoreComponent,
    SelectedStockAnalysisRequest,
    SelectedStockAnalysisResult,
    SourceLineage,
    StockRole,
    StrategyComparison,
    TradeMode,
)
from app.strategies.base import StrategySnapshot, TradingStrategy
from app.strategies.contracts import StrategyManifest, StrategySignal
from app.strategies.hashing import implementation_hash, parameter_hash


ZERO = Decimal("0")
ONE = Decimal("1")
Q4 = Decimal("0.0001")


DEFAULT_PARAMETERS = {
    "minimum_history_rows": 120,
    "recommended_history_rows": 250,
    "minimum_reward_risk": 2,
    "maximum_stop_distance_pct": 8,
    "risk_per_trade_pct": 0.5,
    "initial_position_pct": 10,
    "max_position_pct": 30,
    "daily_loss_limit_pct": 2,
    "single_stock_loss_limit_pct": 1,
    "max_new_position_pct": 10,
    "max_add_position_pct": 10,
}


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


def _floor_lot(value: Decimal) -> int:
    return max(
        0,
        int(value.to_integral_value(rounding=ROUND_FLOOR)) // 100 * 100,
    )


def _score(
    value: Decimal,
    maximum: Decimal,
    *reasons: str,
    evidence: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    ceiling: Decimal | None = None,
    confidence: Decimal = Decimal("0.8"),
) -> ScoreComponent:
    cap = ceiling if ceiling is not None else maximum
    return ScoreComponent(
        score=min(max(value, ZERO), cap),
        maximum=maximum,
        reason_codes=tuple(reasons),
        evidence=evidence,
        missing_evidence=missing,
        score_ceiling=cap,
        confidence=confidence,
    )


def classify_cycle_state(
    indicators: dict[str, Any],
    *,
    market_status: ContextStatus,
    industry_status: ContextStatus,
) -> CycleState:
    relative = indicators.get("relative_strength") or {}
    bearish = bool(indicators.get("bearish_alignment"))
    bullish = bool(indicators.get("bullish_alignment"))
    rsi = Decimal(str(indicators.get("rsi14") or 50))
    atr_pct = Decimal(str(indicators.get("atr_pct") or 0))
    stock_vs_market = relative.get("stock_vs_csi300_20")
    stock_vs_industry = relative.get("stock_vs_industry_20")
    industry_vs_market = relative.get("industry_vs_csi300_20")
    market_state = (indicators.get("market_context") or {}).get("state")
    if bearish or bool(indicators.get("large_bearish_streak")):
        return CycleState.DECLINE
    if rsi >= Decimal("78") and atr_pct >= Decimal("0.04"):
        return CycleState.CLIMAX
    if (
        bullish
        and stock_vs_market is not None
        and Decimal(str(stock_vs_market)) < 0
    ):
        return CycleState.DIVERGENCE
    if (
        market_state == "DIVERGENCE"
        and industry_status == ContextStatus.AVAILABLE
        and stock_vs_industry is not None
        and Decimal(str(stock_vs_industry)) > 0
    ):
        return CycleState.CONCENTRATION
    if bullish and bool(indicators.get("breakout_on_volume")):
        return CycleState.START_CONFIRMED
    if (
        bullish
        and stock_vs_market is not None
        and Decimal(str(stock_vs_market)) > 0
        and (
            industry_vs_market is None
            or Decimal(str(industry_vs_market)) >= 0
        )
    ):
        return CycleState.MARKUP
    if bool(indicators.get("pullback_on_low_volume")):
        return CycleState.PREPARATION
    if bool(indicators.get("ma_entanglement")):
        return CycleState.PROBE
    if market_status == ContextStatus.BREADTH_UNAVAILABLE:
        return CycleState.UNKNOWN
    return CycleState.TRANSITION


def classify_stock_role(
    indicators: dict[str, Any],
    *,
    industry_status: ContextStatus,
) -> StockRole:
    if industry_status != ContextStatus.AVAILABLE:
        return StockRole.UNKNOWN
    evidence = indicators.get("role_evidence") or {}
    relative = indicators.get("relative_strength") or {}
    stock_vs_industry = relative.get("stock_vs_industry_20")
    relative_value = (
        Decimal(str(stock_vs_industry)) if stock_vs_industry is not None else None
    )
    if evidence.get("event_driven") is True:
        return StockRole.EVENT_DRIVEN
    if evidence.get("constituent_rank") == 1 and relative_value is not None and relative_value >= Decimal("0.10"):
        return StockRole.LEADER
    if (
        Decimal(str(evidence.get("amount_rank_percentile", 1))) <= Decimal("0.10")
        and Decimal(str(evidence.get("industry_weight", 0))) >= Decimal("0.05")
    ):
        return StockRole.CAPACITY_CORE
    if evidence.get("branch_rank") == 1:
        return StockRole.BRANCH_CORE
    if evidence.get("early_rotation") is True:
        return StockRole.ROTATION_FRONT
    if relative_value is not None and relative_value >= Decimal("0.05"):
        return StockRole.TREND_CORE
    if relative_value is not None and relative_value <= Decimal("-0.05"):
        return StockRole.FOLLOWER
    return StockRole.UNKNOWN


class CycleStructureValidationStrategyV2(TradingStrategy):
    strategy_id = "cycle_structure_validation_v2"
    strategy_version = "2.0.0"

    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id=self.strategy_id,
            version=self.strategy_version,
            name="Cycle Structure Validation Strategy V2",
            description="Deterministic cycle, structure, trigger and risk advisory strategy.",
            time_horizon="daily-swing-with-intraday-confirmation",
            required_capabilities=("market.daily.qfq", "market.index_daily"),
            parameter_schema={
                "type": "object",
                "properties": {
                    "minimum_history_rows": {"type": "integer", "minimum": 120},
                    "recommended_history_rows": {"type": "integer", "minimum": 120},
                    "minimum_reward_risk": {"type": "number", "minimum": 1},
                    "maximum_stop_distance_pct": {"type": "number", "minimum": 1},
                    "risk_per_trade_pct": {"type": "number", "minimum": 0.1},
                    "initial_position_pct": {"type": "number", "minimum": 0},
                    "max_position_pct": {"type": "number", "minimum": 0},
                    "daily_loss_limit_pct": {"type": "number", "minimum": 0},
                    "single_stock_loss_limit_pct": {"type": "number", "minimum": 0},
                    "max_new_position_pct": {"type": "number", "minimum": 0},
                    "max_add_position_pct": {"type": "number", "minimum": 0},
                },
                "required": sorted(DEFAULT_PARAMETERS),
                "additionalProperties": False,
            },
            default_parameters=DEFAULT_PARAMETERS,
            applicable_market_regimes=(
                "PREPARATION",
                "PROBE",
                "START_CONFIRMED",
                "MARKUP",
                "DIVERGENCE",
                "CONCENTRATION",
                "CLIMAX",
                "DECLINE",
                "TRANSITION",
                "UNKNOWN",
            ),
            source_evidence=(
                "Product V1 deterministic risk parameters",
                "Huiyang cycle evolution and execution manual",
                "Huiyang full process trading discipline textbook",
            ),
            implementation_hash=implementation_hash(type(self)),
        )

    def is_applicable(self, snapshot: StrategySnapshot, parameters: dict) -> bool:
        self.validate_parameters(parameters)
        return all(snapshot.has_capability(item) for item in self.required_capabilities())

    def evaluate(self, snapshot: StrategySnapshot, parameters: dict) -> StrategySignal:
        validated = self.validate_parameters(parameters)
        missing = tuple(
            item for item in self.required_capabilities() if not snapshot.has_capability(item)
        )
        evidence = tuple(
            sorted(
                {
                    reference
                    for capability in self.required_capabilities()
                    for reference in snapshot.evidence_refs_for(capability)
                }
            )
        )
        return StrategySignal(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            applicable=not missing,
            signal_type="ADVISORY_CONTEXT_READY" if not missing else "BLOCKED",
            signal_strength=Decimal("0.5") if not missing else ZERO,
            entry_assessment={"snapshot_hash": snapshot.snapshot_hash},
            invalidation="bound data, trigger, or risk condition changes",
            suggested_risk_level="PRODUCT_V1_EXECUTION_AUTHORITY_REQUIRED",
            holding_horizon="daily-swing",
            evidence_refs=evidence,
            blocked_reasons=tuple(f"missing {item}" for item in missing),
            parameter_hash=parameter_hash(validated),
        )

    def build_plan(
        self,
        *,
        request: SelectedStockAnalysisRequest,
        generated_at,
        analysis_date,
        data_status: DataStatus,
        market_status: ContextStatus,
        industry_status: ContextStatus,
        industry_name: str | None,
        stock_row_count: int,
        indicators: dict[str, Any],
        quality_bindings,
        source_lineage: tuple[SourceLineage, ...],
        snapshot_hash: str,
        product_v1_status: str,
        parameters: dict[str, Any] | None = None,
    ) -> SelectedStockAnalysisResult:
        params = self.validate_parameters(parameters or {})
        latest = Decimal(str(indicators["latest_close"]))
        current = request.current_price or latest
        support_low, support_high = (
            Decimal(str(value)) for value in indicators["support_zone"]
        )
        resistance_low, resistance_high = (
            Decimal(str(value)) for value in indicators["resistance_zone"]
        )
        atr = Decimal(str(indicators.get("atr14") or 0))
        stop = max(Decimal("0.01"), support_low - atr * Decimal("0.5"))
        entry_low, entry_high = support_low, support_high
        risk = max(entry_high - stop, ZERO)
        target = max(resistance_low, entry_high)
        reward_risk = (target - entry_high) / risk if risk > 0 else None
        stop_distance_pct = risk / entry_high * Decimal("100") if entry_high > 0 else None

        relative = indicators["relative_strength"]
        bearish = bool(indicators["bearish_alignment"])
        bullish = bool(indicators["bullish_alignment"])
        breakout = bool(indicators["breakout_on_volume"])
        low_volume_pullback = bool(indicators["pullback_on_low_volume"])

        cycle = classify_cycle_state(
            indicators,
            market_status=market_status,
            industry_status=industry_status,
        )
        role = classify_stock_role(indicators, industry_status=industry_status)

        has_position = bool(request.current_position_quantity or request.current_position_pct)
        if has_position and cycle in {CycleState.CLIMAX, CycleState.DECLINE}:
            trade_mode = TradeMode.HIGH_LEVEL_DEFENSE
        elif breakout:
            trade_mode = TradeMode.EARLY_BREAKOUT_CONFIRMATION
        else:
            trade_mode = TradeMode.CORE_TREND_PULLBACK

        gate_values = {
            "DATA_INSUFFICIENT": stock_row_count >= int(params["minimum_history_rows"]),
            "DATA_STALE": data_status in {DataStatus.FRESH},
            "DATA_CONFLICTED": data_status != DataStatus.CONFLICTED_DATA,
            "NO_CLEAR_INVALIDATION": stop > 0 and stop < entry_low,
            "STOP_TOO_FAR": bool(
                stop_distance_pct is not None
                and stop_distance_pct <= Decimal(str(params["maximum_stop_distance_pct"]))
            ),
            "RISK_REWARD_INSUFFICIENT": bool(
                reward_risk is not None
                and reward_risk >= Decimal(str(params["minimum_reward_risk"]))
            ),
            "DOWNTREND_STRUCTURE": not bearish,
            "DECLINE_CYCLE": cycle != CycleState.DECLINE,
            "CLIMAX_NEW_ENTRY_BLOCKED": not (
                cycle == CycleState.CLIMAX and not has_position
            ),
            "FOLLOWER_WITHOUT_LEADER_CONFIRMATION": role != StockRole.FOLLOWER,
            "INDUSTRY_CONTEXT_REQUIRED_BUT_MISSING": (
                industry_status == ContextStatus.AVAILABLE
            ),
            "LOSS_POSITION_ADD_BLOCKED": not (
                has_position and request.average_cost and current < request.average_cost
            ),
            "T1_NEW_POSITION_RISK_EXCEEDED": (
                has_position or Decimal(str(params["max_new_position_pct"])) > 0
            ),
            "MAX_POSITION_EXCEEDED": (
                request.current_position_pct is None
                or request.current_position_pct
                <= (request.max_position_pct or Decimal(str(params["max_position_pct"])))
            ),
            "DAILY_LOSS_LIMIT_REACHED": True,
            "CONFIRM_FREEZE_FAILED": True,
        }
        gates = tuple(
            GateResult(
                code=code,
                passed=passed,
                reason_code=f"{code}_{'PASS' if passed else 'BLOCKED'}",
                evidence=(
                    "CSV_V2 is advisory; Product V1 remains confirm/freeze authority"
                    if code == "CONFIRM_FREEZE_FAILED"
                    else f"deterministic evaluation={passed}"
                ,),
            )
            for code, passed in gate_values.items()
        )
        failed_gates = tuple(item.code for item in gates if not item.passed)

        market_component = _score(
            Decimal("0") if market_status != ContextStatus.AVAILABLE else Decimal("9"),
            Decimal("15"),
            "MARKET_BREADTH_MISSING" if market_status != ContextStatus.AVAILABLE else "MARKET_CONTEXT_AVAILABLE",
            missing=("market breadth",) if market_status != ContextStatus.AVAILABLE else (),
            ceiling=Decimal("5") if market_status != ContextStatus.AVAILABLE else Decimal("15"),
            confidence=Decimal("0.35") if market_status != ContextStatus.AVAILABLE else Decimal("0.8"),
        )
        industry_component = _score(
            Decimal("0")
            if industry_status != ContextStatus.AVAILABLE
            else Decimal("12")
            if Decimal(str(relative.get("industry_vs_csi300_20") or 0)) > 0
            else Decimal("7"),
            Decimal("15"),
            "INDUSTRY_CONTEXT_MISSING"
            if industry_status != ContextStatus.AVAILABLE
            else "INDUSTRY_RELATIVE_STRENGTH_EVALUATED",
            missing=("industry mapping and history",)
            if industry_status != ContextStatus.AVAILABLE
            else (),
            ceiling=Decimal("0")
            if industry_status != ContextStatus.AVAILABLE
            else Decimal("15"),
            confidence=Decimal("0.2")
            if industry_status != ContextStatus.AVAILABLE
            else Decimal("0.8"),
        )
        role_component = _score(
            Decimal("16") if role == StockRole.TREND_CORE else Decimal("8") if role != StockRole.UNKNOWN else ZERO,
            Decimal("20"),
            f"STOCK_ROLE_{role.value}",
            missing=("constituent rank",) if role == StockRole.UNKNOWN else (),
            ceiling=Decimal("10") if role == StockRole.UNKNOWN else Decimal("20"),
        )
        trend_component = _score(
            Decimal("18") if bullish and (breakout or low_volume_pullback) else Decimal("12") if bullish else Decimal("4"),
            Decimal("20"),
            "TREND_VOLUME_CONFIRMED" if bullish else "TREND_NOT_CONFIRMED",
        )
        location_component = _score(
            Decimal("12") if support_low <= current <= support_high else Decimal("7") if current <= resistance_high else Decimal("2"),
            Decimal("15"),
            "NEAR_SUPPORT" if support_low <= current <= support_high else "AWAY_FROM_SUPPORT",
        )
        risk_component = _score(
            Decimal("13") if gate_values["STOP_TOO_FAR"] and gate_values["RISK_REWARD_INSUFFICIENT"] else Decimal("4"),
            Decimal("15"),
            "RISK_INVALIDATION_VALID" if gate_values["NO_CLEAR_INVALIDATION"] else "INVALIDATION_MISSING",
        )
        components = (
            market_component,
            industry_component,
            role_component,
            trend_component,
            location_component,
            risk_component,
        )
        total = sum((item.score for item in components), ZERO)
        grade = "A" if total >= 85 else "B" if total >= 75 else "C" if total >= 60 else "D"
        scores = ScoreCard(
            market_cycle=market_component,
            industry_continuity=industry_component,
            stock_role_relative_strength=role_component,
            trend_volume=trend_component,
            location_trigger=location_component,
            risk_invalidation=risk_component,
            total=total,
            grade=grade,
        )

        pending = []
        intraday_confirmed = indicators.get("intraday_confirmation") is True
        if not breakout and not low_volume_pullback:
            pending.append("PENDING_DAILY_TRIGGER")
        if not intraday_confirmed:
            pending.append("PENDING_INTRADAY_CONFIRMATION")
        if market_status != ContextStatus.AVAILABLE:
            pending.append("BREADTH_UNAVAILABLE")
        if industry_status != ContextStatus.AVAILABLE:
            pending.append("INDUSTRY_CONTEXT_UNAVAILABLE")

        if data_status in {DataStatus.INSUFFICIENT_DATA, DataStatus.STALE, DataStatus.CONFLICTED_DATA}:
            plan_status = PlanStatus.INSUFFICIENT_DATA
        elif cycle == CycleState.DECLINE or not gate_values["NO_CLEAR_INVALIDATION"]:
            plan_status = PlanStatus.EXIT if has_position else PlanStatus.NO_TRADE
        elif has_position and cycle == CycleState.CLIMAX:
            plan_status = PlanStatus.REDUCE
        elif has_position:
            plan_status = PlanStatus.HOLD
        elif not failed_gates and intraday_confirmed and (breakout or low_volume_pullback):
            plan_status = PlanStatus.ENTRY_ALLOWED
        else:
            plan_status = PlanStatus.WAIT_FOR_TRIGGER

        max_position_pct = request.max_position_pct or Decimal(str(params["max_position_pct"]))
        initial_pct = min(Decimal(str(params["initial_position_pct"])), max_position_pct)
        quantity = max_quantity = None
        risk_amount = None
        if request.account_size is not None:
            cash = min(request.available_cash or request.account_size, request.account_size)
            max_quantity = _floor_lot(
                min(
                    request.account_size * max_position_pct / Decimal("100"),
                    cash,
                )
                / current
            )
            risk_budget = request.risk_budget or (
                request.account_size
                * Decimal(str(params["risk_per_trade_pct"]))
                / Decimal("100")
            )
            by_value = request.account_size * initial_pct / Decimal("100") / current
            by_risk = risk_budget / max(current - stop, Decimal("0.01"))
            quantity = min(_floor_lot(by_value), _floor_lot(by_risk), max_quantity)
            risk_amount = Decimal(quantity) * max(current - stop, ZERO)

        holding_plan = None
        if has_position:
            quantity_held = Decimal(request.current_position_quantity or 0)
            unrealized = (
                (current - request.average_cost) * quantity_held
                if request.average_cost is not None and quantity_held
                else None
            )
            held_risk = quantity_held * max(current - stop, ZERO) if quantity_held else None
            holding_plan = HoldingPlan(
                unrealized_pnl=_q(unrealized),
                risk_amount=_q(held_risk),
                risk_pct=_q(held_risk / request.account_size * Decimal("100"))
                if held_risk is not None and request.account_size
                else None,
                allow_hold=plan_status in {PlanStatus.HOLD, PlanStatus.REDUCE},
                allow_add=bool(
                    request.average_cost
                    and current > request.average_cost
                    and industry_status == ContextStatus.AVAILABLE
                    and role not in {StockRole.FOLLOWER, StockRole.UNKNOWN}
                    and current > stop
                    and not failed_gates
                ),
                stop_distance_pct=_q((current - stop) / current * Decimal("100")),
                maximum_risk_after_add=_q(
                    held_risk + (risk_amount or ZERO) if held_risk is not None else None
                ),
                plan_invalidated=current <= stop,
                next_session_plan=(
                    "Do not add to a losing position",
                    "Exit or reduce if the invalidation zone is not recovered",
                    "Reconfirm industry and trigger before any add",
                ),
            )

        differences = (
            "CSV_V2 remains advisory and cannot confirm/freeze",
            "CSV_V2 requires cycle, relative strength, trigger and invalidation evidence",
            "Product V1 fixed trade numbers are unchanged",
        )
        comparison_status = (
            "DATA_CONFLICT"
            if data_status in {DataStatus.CONFLICTED_DATA, DataStatus.INSUFFICIENT_DATA}
            or product_v1_status.startswith("BLOCKED")
            else "MORE_CONSERVATIVE"
            if plan_status in {PlanStatus.NO_TRADE, PlanStatus.WAIT_FOR_TRIGGER, PlanStatus.INSUFFICIENT_DATA}
            else "ALIGNED"
        )
        payload = {
            "analysis_run_id": None,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_mode": request.strategy_mode,
            "stock_code": request.stock_code,
            "analysis_date": analysis_date,
            "generated_at": generated_at,
            "data_status": data_status,
            "market_context_status": market_status,
            "industry_context_status": industry_status,
            "industry_name": industry_name,
            "cycle_state": cycle,
            "stock_role": role,
            "trade_mode": trade_mode,
            "plan_status": plan_status,
            "executable": False,
            "confidence": min(
                (sum((item.confidence for item in components), ZERO) / Decimal(6)),
                ONE,
            ),
            "hard_gates": gates,
            "scores": scores,
            "technical_evidence": indicators,
            "passed_conditions": tuple(item.reason_code for item in gates if item.passed),
            "failed_conditions": tuple(item.reason_code for item in gates if not item.passed),
            "pending_conditions": tuple(sorted(set(pending))),
            "price_plan": PricePlan(
                support_zone_low=_q(support_low),
                support_zone_high=_q(support_high),
                entry_zone_low=_q(entry_low) if not failed_gates else None,
                entry_zone_high=_q(entry_high) if not failed_gates else None,
                trigger_price=_q(resistance_low) if breakout else None,
                stop_loss=_q(stop),
                first_take_profit=_q(resistance_low),
                second_take_profit=_q(resistance_high),
                invalidation_price=_q(stop),
                risk_reward_ratio=_q(reward_risk),
            ),
            "position_plan": PositionPlan(
                initial_position_pct=initial_pct if not failed_gates else ZERO,
                max_position_pct=max_position_pct if not failed_gates else ZERO,
                quantity=quantity if not failed_gates else None,
                max_quantity=max_quantity if not failed_gates else None,
                risk_amount=_q(risk_amount) if not failed_gates else None,
                add_conditions=(
                    "Existing position is profitable",
                    "Industry and stock role have not weakened",
                    "Trigger is reconfirmed and total risk remains within budget",
                ),
                reduce_conditions=(
                    "Break support or intraday average and fail to recover",
                    "Industry weakens while relative strength deteriorates",
                    "High-level acceleration loses acceptance",
                ),
            ),
            "holding_plan": holding_plan,
            "invalidation_conditions": (
                f"PRICE_AT_OR_BELOW_{_q(stop)}",
                "INDUSTRY_CONTEXT_DETERIORATES",
                "RELATIVE_STRENGTH_AND_TREND_STRUCTURE_FAIL",
            ),
            "next_check_condition": tuple(sorted(set(pending))),
            "execution_blockers": tuple(sorted(set(failed_gates + ("CSV_V2_ADVISORY_ONLY",)))),
            "quality_bindings": quality_bindings,
            "source_lineage": source_lineage,
            "product_v1_comparison": StrategyComparison(
                conflict_status=comparison_status,
                product_v1_status=product_v1_status,
                csv_v2_status=plan_status.value,
                differences=differences,
            ),
            "explanation": {
                "summary": "Deterministic advisory result; natural-language AI is optional.",
                "risk_notice": "Research only. No automatic order or execution permission.",
                "user_focus": request.user_focus,
                "catalyst": request.catalyst_context.model_dump(mode="json")
                if request.catalyst_context
                else None,
            },
            "snapshot_hash": snapshot_hash,
        }
        payload["result_digest"] = canonical_hash(payload)
        return SelectedStockAnalysisResult.model_validate(payload)


__all__ = [
    "CycleStructureValidationStrategyV2",
    "DEFAULT_PARAMETERS",
    "classify_cycle_state",
    "classify_stock_role",
]
