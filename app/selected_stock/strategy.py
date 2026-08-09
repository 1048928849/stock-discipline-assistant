from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from app.data_hub.trading_calendar import to_shanghai_aware
from app.domain.hashing import canonical_hash
from app.selected_stock.contracts import (
    AccountContext,
    ContextStatus,
    CycleState,
    DataStatus,
    GateResult,
    GateStatus,
    HoldingPlan,
    IndustryContextEvidence,
    PlanStatus,
    PositionPlan,
    PriceObservation,
    PricePlan,
    ScoreCard,
    ScoreComponent,
    SelectedStockAnalysisRequest,
    SelectedStockAnalysisResult,
    SourceLineage,
    StockRole,
    StockRoleEvidence,
    StrategyComparison,
    SurvivalRuleStatus,
    TradeMode,
    UserPriceStatus,
)
from app.strategies.base import StrategySnapshot, TradingStrategy
from app.strategies.contracts import StrategyManifest, StrategySignal
from app.strategies.hashing import implementation_hash, parameter_hash
from app.selected_stock.survival import evaluate_survival_discipline


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
    "user_price_max_age_seconds": 900,
    "position_input_tolerance_pct": 0.5,
    "user_price_conflict_tolerance_pct": 5,
    "t1_atr_multiplier": 1.5,
    "t1_recent_gap_cap_pct": 15,
}


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


def _floor_lot(value: Decimal) -> int:
    return max(
        0,
        int(value.to_integral_value(rounding=ROUND_FLOOR)) // 100 * 100,
    )


def _gate(
    code: str,
    status: GateStatus,
    reason_code: str,
    *evidence: str,
    required_inputs: tuple[str, ...] = (),
    evaluated_inputs: dict[str, Any] | None = None,
    threshold: Any | None = None,
    actual_value: Any | None = None,
) -> GateResult:
    missing_inputs = tuple(
        item.split("=", 1)[1]
        for item in evidence
        if item.startswith("missing=")
    )
    return GateResult(
        code=code,
        status=status,
        reason_code=reason_code,
        required_inputs=required_inputs,
        evaluated_inputs=evaluated_inputs or {},
        threshold=threshold,
        actual_value=actual_value,
        evidence=tuple(evidence),
        missing_inputs=missing_inputs,
        effect_on_plan="ALLOW" if status == GateStatus.PASS else "BLOCK_NEW_ACTION",
        effect_on_score=(
            "ELIGIBLE" if status == GateStatus.PASS else "NO_POSITIVE_SCORE"
        ),
        effect_on_position=(
            "ELIGIBLE" if status == GateStatus.PASS else "ZERO_NEW_POSITION"
        ),
    )


def _resolve_price_context(
    *,
    request: SelectedStockAnalysisRequest,
    generated_at,
    analysis_date,
    market_price: Decimal,
    market_price_observed_at,
    max_age_seconds: int,
    conflict_tolerance_pct: Decimal,
) -> PriceObservation:
    generated = to_shanghai_aware(generated_at)
    market_observed = to_shanghai_aware(market_price_observed_at)
    if request.current_price is None:
        return PriceObservation(
            price=market_price,
            observed_at=market_observed,
            source="MARKET_DAILY_CLOSE",
            trust_status=UserPriceStatus.LATEST_CLOSE_ONLY,
            matched_analysis_date=market_observed.date() == analysis_date,
            executable_for_entry=False,
            executable_for_position=True,
            reason_code="LATEST_CLOSE_NOT_REALTIME",
            market_close=market_price,
            market_close_observed_at=market_observed,
        )

    observed = to_shanghai_aware(request.current_price_observed_at)
    if observed > generated:
        status = UserPriceStatus.USER_PRICE_FUTURE
    elif observed.date() != analysis_date:
        status = UserPriceStatus.USER_PRICE_DATE_MISMATCH
    elif generated - observed > timedelta(seconds=max_age_seconds):
        status = UserPriceStatus.USER_PRICE_STALE
    elif abs(request.current_price / market_price - ONE) * Decimal("100") > conflict_tolerance_pct:
        status = UserPriceStatus.USER_PRICE_CONFLICTED
    else:
        status = UserPriceStatus.USER_PRICE_FRESH
    fresh = status == UserPriceStatus.USER_PRICE_FRESH
    age_seconds = max(0, int((generated - observed).total_seconds()))
    return PriceObservation(
        price=request.current_price if fresh else market_price,
        observed_at=observed if fresh else market_observed,
        source="USER_OBSERVATION" if fresh else "MARKET_DAILY_CLOSE",
        trust_status=status,
        age_seconds=age_seconds,
        matched_analysis_date=observed.date() == analysis_date,
        executable_for_entry=fresh,
        executable_for_position=fresh,
        reason_code=status.value,
        market_close=market_price,
        market_close_observed_at=market_observed,
        user_price=request.current_price,
        user_price_observed_at=observed,
    )


def _manual_account_context(
    request: SelectedStockAnalysisRequest,
    *,
    observed_at,
) -> AccountContext:
    supplied = any(
        value is not None
        for value in (
            request.account_size,
            request.available_cash,
            request.current_position_quantity,
            request.current_position_pct,
            request.average_cost,
            request.daily_realized_pnl,
        )
    )
    daily_total = (
        request.daily_realized_pnl + request.daily_unrealized_pnl
        if request.daily_realized_pnl is not None
        and request.daily_unrealized_pnl is not None
        else None
    )
    daily_loss = max(-daily_total, ZERO) if daily_total is not None else None
    daily_loss_pct = (
        daily_loss / request.account_size * Decimal("100")
        if daily_loss is not None and request.account_size
        else None
    )
    return AccountContext(
        source="MANUAL_ACCOUNT_CONTEXT" if supplied else "NO_ACCOUNT_CONTEXT",
        trust_status="MANUAL_ACCOUNT_CONTEXT" if supplied else "UNAVAILABLE",
        account_size=request.account_size,
        available_cash=request.available_cash,
        current_position_quantity=request.current_position_quantity,
        current_position_pct=request.current_position_pct,
        average_cost=request.average_cost,
        daily_realized_pnl=request.daily_realized_pnl,
        daily_unrealized_pnl=request.daily_unrealized_pnl,
        daily_loss_amount=_q(daily_loss),
        daily_loss_pct=_q(daily_loss_pct),
        observed_at=request.daily_pnl_observed_at or observed_at,
        confidence=Decimal("0.6") if supplied else ZERO,
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


def _fallback_industry_context(
    industry_status: ContextStatus,
    industry_name: str | None,
    stock_row_count: int,
) -> IndustryContextEvidence:
    reason = (
        "LEGACY_INDUSTRY_CONTEXT_AVAILABLE"
        if industry_status == ContextStatus.AVAILABLE
        else "INDUSTRY_CONTEXT_UNAVAILABLE"
    )
    role_evidence = StockRoleEvidence(
        member_count=0,
        valid_member_count=0,
        coverage_ratio=ZERO,
        evidence_complete=False,
        reason_code="ROLE_EVIDENCE_INSUFFICIENT",
    )
    return IndustryContextEvidence(
        status=industry_status,
        history_row_count=stock_row_count if industry_name else 0,
        constituent_count=0,
        valid_member_count=0,
        coverage_ratio=ZERO,
        quality_status="MISSING",
        role=StockRole.UNKNOWN,
        role_evidence=role_evidence,
        reason_codes=(reason,),
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
                    "user_price_max_age_seconds": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "position_input_tolerance_pct": {
                        "type": "number",
                        "minimum": 0,
                    },
                    "user_price_conflict_tolerance_pct": {
                        "type": "number",
                        "minimum": 0,
                    },
                    "t1_atr_multiplier": {"type": "number", "minimum": 1},
                    "t1_recent_gap_cap_pct": {
                        "type": "number",
                        "minimum": 1,
                    },
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
        market_price_observed_at,
        account_context: AccountContext | None = None,
        industry_context: IndustryContextEvidence | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> SelectedStockAnalysisResult:
        params = self.validate_parameters(parameters or {})
        latest = Decimal(str(indicators["latest_close"]))
        price_observation = _resolve_price_context(
            request=request,
            generated_at=generated_at,
            analysis_date=analysis_date,
            market_price=latest,
            market_price_observed_at=market_price_observed_at,
            max_age_seconds=int(params["user_price_max_age_seconds"]),
            conflict_tolerance_pct=Decimal(
                str(params["user_price_conflict_tolerance_pct"])
            ),
        )
        current = price_observation.price
        account = account_context or _manual_account_context(
            request,
            observed_at=generated_at,
        )
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
        effective_industry_context = industry_context or _fallback_industry_context(
            industry_status,
            industry_name,
            stock_row_count,
        )
        if industry_context is not None:
            indicators["role_evidence"] = industry_context.role_evidence.model_dump(
                mode="python"
            )
        role = (
            industry_context.role
            if industry_context is not None
            else classify_stock_role(indicators, industry_status=industry_status)
        )

        account_size = account.account_size
        current_quantity = int(account.current_position_quantity or 0)
        supplied_position_pct = account.current_position_pct
        has_position = bool(current_quantity or supplied_position_pct)
        implied_position_pct = (
            Decimal(current_quantity) * current / account_size * Decimal("100")
            if current_quantity and account_size
            else ZERO
            if not has_position
            else None
        )
        position_input_conflict = bool(
            current_quantity is not None
            and supplied_position_pct is not None
            and implied_position_pct is not None
            and abs(implied_position_pct - supplied_position_pct)
            > Decimal(str(params["position_input_tolerance_pct"]))
        ) or account.trust_status == "ACCOUNT_INPUT_CONFLICT"
        current_position_pct = (
            supplied_position_pct
            if supplied_position_pct is not None
            else implied_position_pct
        )
        if has_position and cycle in {CycleState.CLIMAX, CycleState.DECLINE}:
            trade_mode = TradeMode.HIGH_LEVEL_DEFENSE
        elif breakout:
            trade_mode = TradeMode.EARLY_BREAKOUT_CONFIRMATION
        else:
            trade_mode = TradeMode.CORE_TREND_PULLBACK

        max_position_pct = request.max_position_pct or Decimal(
            str(params["max_position_pct"])
        )
        proposed_trade_pct = Decimal(str(params["initial_position_pct"]))
        post_trade_position_pct = (
            current_position_pct + proposed_trade_pct
            if current_position_pct is not None
            else None
        )
        cash = (
            min(account.available_cash or account_size, account_size)
            if account_size is not None
            else None
        )
        risk_budget = (
            request.risk_budget
            or account_size * Decimal(str(params["risk_per_trade_pct"])) / Decimal("100")
            if account_size is not None
            else None
        )
        quantity = max_quantity = None
        proposed_risk = None
        if account_size is not None and price_observation.executable_for_position:
            by_value = account_size * proposed_trade_pct / Decimal("100") / current
            by_risk = (
                risk_budget / max(current - stop, Decimal("0.01"))
                if risk_budget is not None
                else ZERO
            )
            by_cash = cash / current if cash is not None else ZERO
            quantity = min(
                _floor_lot(by_value),
                _floor_lot(by_risk),
                _floor_lot(by_cash),
            )
            max_quantity = _floor_lot(
                account_size * max_position_pct / Decimal("100") / current
            )
            proposed_risk = Decimal(quantity) * max(current - stop, ZERO)

        held_quantity_for_risk = Decimal(current_quantity)
        if not current_quantity and current_position_pct and account_size:
            held_quantity_for_risk = (
                account_size * current_position_pct / Decimal("100") / current
            )
        held_risk = (
            held_quantity_for_risk * max(current - stop, ZERO)
            if has_position and held_quantity_for_risk
            else ZERO
        )
        maximum_loss_after_trade = (
            held_risk + proposed_risk if proposed_risk is not None else None
        )
        gap = indicators.get("overnight_gap") or {}
        atr_pct = Decimal(str(indicators.get("atr_pct") or 0))
        gap_95 = Decimal(str(gap.get("negative_gap_95") or 0))
        recent_gap = Decimal(str(gap.get("recent_max_negative_gap") or 0))
        t1_gap_cap = Decimal(str(params["t1_recent_gap_cap_pct"])) / Decimal("100")
        overnight_gap_risk_pct = max(
            atr_pct * Decimal(str(params["t1_atr_multiplier"])),
            gap_95,
            min(recent_gap, t1_gap_cap),
        )
        planned_value = Decimal(quantity) * current if quantity is not None else None
        t1_risk_amount = (
            planned_value * overnight_gap_risk_pct
            if planned_value is not None
            else None
        )
        t1_risk_pct = (
            t1_risk_amount / account_size * Decimal("100")
            if t1_risk_amount is not None and account_size
            else None
        )

        gates: list[GateResult] = []
        enough_rows = stock_row_count >= int(params["minimum_history_rows"])
        gates.append(
            _gate(
                "DATA_INSUFFICIENT",
                GateStatus.PASS if enough_rows else GateStatus.BLOCKED,
                "HISTORY_ROWS_SUFFICIENT" if enough_rows else "DATA_INSUFFICIENT",
                f"rows={stock_row_count}",
                f"minimum={params['minimum_history_rows']}",
            )
        )
        fresh_data = data_status == DataStatus.FRESH
        gates.append(
            _gate(
                "DATA_STALE",
                GateStatus.PASS if fresh_data else GateStatus.BLOCKED,
                "DATA_FRESH" if fresh_data else "DATA_STALE",
                f"data_status={data_status.value}",
            )
        )
        conflict_free = data_status != DataStatus.CONFLICTED_DATA
        gates.append(
            _gate(
                "DATA_CONFLICTED",
                GateStatus.PASS if conflict_free else GateStatus.BLOCKED,
                "DATA_CONFLICT_FREE" if conflict_free else "DATA_CONFLICTED",
                f"data_status={data_status.value}",
            )
        )
        invalidation_clear = stop > 0 and stop < entry_low
        gates.append(
            _gate(
                "NO_CLEAR_INVALIDATION",
                GateStatus.PASS if invalidation_clear else GateStatus.BLOCKED,
                "INVALIDATION_PRICE_VALID"
                if invalidation_clear
                else "NO_CLEAR_INVALIDATION",
                f"stop={_q(stop)}",
                f"entry_low={_q(entry_low)}",
            )
        )
        if stop_distance_pct is None or not invalidation_clear:
            gates.append(
                _gate(
                    "STOP_TOO_FAR",
                    GateStatus.NOT_EVALUATED,
                    "STOP_DISTANCE_UNAVAILABLE",
                    "missing=valid stop distance",
                )
            )
        else:
            stop_ok = stop_distance_pct <= Decimal(
                str(params["maximum_stop_distance_pct"])
            )
            gates.append(
                _gate(
                    "STOP_TOO_FAR",
                    GateStatus.PASS if stop_ok else GateStatus.BLOCKED,
                    "STOP_DISTANCE_WITHIN_LIMIT" if stop_ok else "STOP_TOO_FAR",
                    f"actual_pct={_q(stop_distance_pct)}",
                    f"maximum_pct={params['maximum_stop_distance_pct']}",
                )
            )
        if reward_risk is None:
            gates.append(
                _gate(
                    "RISK_REWARD_INSUFFICIENT",
                    GateStatus.NOT_EVALUATED,
                    "RISK_REWARD_UNAVAILABLE",
                    "missing=valid reward and risk",
                )
            )
        else:
            reward_ok = reward_risk >= Decimal(str(params["minimum_reward_risk"]))
            gates.append(
                _gate(
                    "RISK_REWARD_INSUFFICIENT",
                    GateStatus.PASS if reward_ok else GateStatus.BLOCKED,
                    "RISK_REWARD_SUFFICIENT"
                    if reward_ok
                    else "RISK_REWARD_INSUFFICIENT",
                    f"actual={_q(reward_risk)}",
                    f"minimum={params['minimum_reward_risk']}",
                )
            )
        gates.extend(
            (
                _gate(
                    "DOWNTREND_STRUCTURE",
                    GateStatus.BLOCKED if bearish else GateStatus.PASS,
                    "DOWNTREND_STRUCTURE" if bearish else "DOWNTREND_NOT_PRESENT",
                    f"bearish_alignment={bearish}",
                ),
                _gate(
                    "DECLINE_CYCLE",
                    GateStatus.BLOCKED
                    if cycle == CycleState.DECLINE
                    else GateStatus.PASS,
                    "DECLINE_CYCLE"
                    if cycle == CycleState.DECLINE
                    else "CYCLE_NOT_DECLINE",
                    f"cycle={cycle.value}",
                ),
                _gate(
                    "CLIMAX_NEW_ENTRY_BLOCKED",
                    GateStatus.BLOCKED
                    if cycle == CycleState.CLIMAX and not has_position
                    else GateStatus.PASS,
                    "CLIMAX_NEW_ENTRY_BLOCKED"
                    if cycle == CycleState.CLIMAX and not has_position
                    else "CLIMAX_ENTRY_RULE_CLEAR",
                    f"cycle={cycle.value}",
                    f"has_position={has_position}",
                ),
            )
        )
        if role == StockRole.UNKNOWN:
            gates.append(
                _gate(
                    "FOLLOWER_WITHOUT_LEADER_CONFIRMATION",
                    GateStatus.NOT_EVALUATED,
                    "ROLE_EVIDENCE_INSUFFICIENT",
                    "missing=industry constituent role evidence",
                )
            )
        else:
            role_ok = role != StockRole.FOLLOWER
            gates.append(
                _gate(
                    "FOLLOWER_WITHOUT_LEADER_CONFIRMATION",
                    GateStatus.PASS if role_ok else GateStatus.BLOCKED,
                    "ROLE_CONFIRMED" if role_ok else "FOLLOWER_WITHOUT_LEADER_CONFIRMATION",
                    f"role={role.value}",
                )
            )
        industry_available = industry_status == ContextStatus.AVAILABLE
        gates.append(
            _gate(
                "INDUSTRY_CONTEXT_REQUIRED_BUT_MISSING",
                GateStatus.PASS if industry_available else GateStatus.NOT_EVALUATED,
                "INDUSTRY_CONTEXT_AVAILABLE"
                if industry_available
                else "INDUSTRY_CONTEXT_UNAVAILABLE",
                f"industry_status={industry_status.value}",
            )
        )
        if not has_position:
            gates.append(
                _gate(
                    "LOSS_POSITION_ADD_BLOCKED",
                    GateStatus.PASS,
                    "NO_EXISTING_POSITION_TO_ADD",
                    "has_position=False",
                )
            )
        elif account.average_cost is None:
            gates.append(
                _gate(
                    "LOSS_POSITION_ADD_BLOCKED",
                    GateStatus.NOT_EVALUATED,
                    "AVERAGE_COST_UNAVAILABLE",
                    "missing=average_cost",
                )
            )
        else:
            profitable = current >= account.average_cost
            gates.append(
                _gate(
                    "LOSS_POSITION_ADD_BLOCKED",
                    GateStatus.PASS if profitable else GateStatus.BLOCKED,
                    "POSITION_NOT_LOSING" if profitable else "LOSS_POSITION_ADD_BLOCKED",
                    f"trusted_price={_q(current)}",
                    f"average_cost={_q(account.average_cost)}",
                )
            )
        if account_size is None or quantity is None or t1_risk_pct is None:
            gates.append(
                _gate(
                    "T1_NEW_POSITION_RISK_EXCEEDED",
                    GateStatus.NOT_EVALUATED,
                    "T1_RISK_CONTEXT_UNAVAILABLE",
                    "missing=account equity or proposed lot quantity",
                )
            )
        else:
            allocation_limit = Decimal(
                str(
                    params[
                        "max_add_position_pct"
                        if has_position
                        else "max_new_position_pct"
                    ]
                )
            )
            t1_ok = (
                proposed_trade_pct <= allocation_limit
                and t1_risk_pct <= Decimal(str(params["risk_per_trade_pct"]))
                and quantity >= 100
            )
            gates.append(
                _gate(
                    "T1_NEW_POSITION_RISK_EXCEEDED",
                    GateStatus.PASS if t1_ok else GateStatus.BLOCKED,
                    "T1_RISK_WITHIN_BUDGET"
                    if t1_ok
                    else "T1_NEW_POSITION_RISK_EXCEEDED",
                    f"proposed_pct={_q(proposed_trade_pct)}",
                    f"allocation_limit_pct={_q(allocation_limit)}",
                    f"overnight_gap_risk_pct={_q(overnight_gap_risk_pct * Decimal('100'))}",
                    f"t1_risk_pct={_q(t1_risk_pct)}",
                    f"risk_budget_pct={params['risk_per_trade_pct']}",
                    f"quantity={quantity}",
                )
            )
        if post_trade_position_pct is None:
            max_position_status = GateStatus.NOT_EVALUATED
            max_position_reason = "POST_TRADE_POSITION_UNAVAILABLE"
        else:
            cash_ok = cash is None or cash >= current * Decimal("100")
            max_position_ok = (
                post_trade_position_pct <= max_position_pct and cash_ok
            )
            max_position_status = (
                GateStatus.PASS if max_position_ok else GateStatus.BLOCKED
            )
            max_position_reason = (
                "POST_TRADE_POSITION_WITHIN_LIMIT"
                if max_position_ok
                else "MAX_POSITION_EXCEEDED"
                if post_trade_position_pct > max_position_pct
                else "AVAILABLE_CASH_INSUFFICIENT"
            )
        gates.append(
            _gate(
                "MAX_POSITION_EXCEEDED",
                max_position_status,
                max_position_reason,
                f"current_pct={_q(current_position_pct)}",
                f"proposed_pct={_q(proposed_trade_pct)}",
                f"post_trade_pct={_q(post_trade_position_pct)}",
                f"maximum_pct={_q(max_position_pct)}",
                f"available_cash={_q(cash)}",
            )
        )
        daily_pnl_time = account.observed_at
        daily_context_valid = bool(
            account_size is not None
            and account.daily_loss_pct is not None
            and daily_pnl_time is not None
            and to_shanghai_aware(daily_pnl_time) <= to_shanghai_aware(generated_at)
            and to_shanghai_aware(daily_pnl_time).date() == analysis_date
        )
        if not daily_context_valid:
            gates.append(
                _gate(
                    "DAILY_LOSS_LIMIT_REACHED",
                    GateStatus.NOT_EVALUATED,
                    "DAILY_LOSS_CONTEXT_UNAVAILABLE",
                    "missing=dated realized/unrealized PnL or account equity",
                )
            )
        else:
            daily_ok = account.daily_loss_pct < Decimal(
                str(params["daily_loss_limit_pct"])
            )
            gates.append(
                _gate(
                    "DAILY_LOSS_LIMIT_REACHED",
                    GateStatus.PASS if daily_ok else GateStatus.BLOCKED,
                    "DAILY_LOSS_WITHIN_LIMIT"
                    if daily_ok
                    else "DAILY_LOSS_LIMIT_REACHED",
                    f"daily_loss_amount={_q(account.daily_loss_amount)}",
                    f"daily_loss_pct={_q(account.daily_loss_pct)}",
                    f"limit_pct={params['daily_loss_limit_pct']}",
                )
            )
        if account_size is None or maximum_loss_after_trade is None:
            gates.append(
                _gate(
                    "SINGLE_STOCK_LOSS_LIMIT",
                    GateStatus.NOT_EVALUATED,
                    "SINGLE_STOCK_RISK_CONTEXT_UNAVAILABLE",
                    "missing=account equity or proposed position risk",
                )
            )
        else:
            loss_limit = (
                account_size
                * Decimal(str(params["single_stock_loss_limit_pct"]))
                / Decimal("100")
            )
            single_loss_ok = maximum_loss_after_trade <= loss_limit
            gates.append(
                _gate(
                    "SINGLE_STOCK_LOSS_LIMIT",
                    GateStatus.PASS if single_loss_ok else GateStatus.BLOCKED,
                    "SINGLE_STOCK_LOSS_WITHIN_LIMIT"
                    if single_loss_ok
                    else "SINGLE_STOCK_LOSS_LIMIT_EXCEEDED",
                    f"maximum_loss_after_trade={_q(maximum_loss_after_trade)}",
                    f"limit_amount={_q(loss_limit)}",
                    f"limit_pct={params['single_stock_loss_limit_pct']}",
                )
            )
        gates = tuple(gates)
        gate_by_code = {item.code: item for item in gates}
        survival_discipline = evaluate_survival_discipline(
            indicators=indicators,
            current_price=current,
            entry_high=entry_high,
            hard_stop=stop,
            proposed_position_pct=proposed_trade_pct,
            has_position=has_position,
            market_status=market_status,
            industry_status=industry_status,
            stock_role=role,
        )
        survival_blockers = tuple(
            item.reason_code
            for item in survival_discipline
            if item.status == SurvivalRuleStatus.BLOCK
        )
        planning_gate_blockers = tuple(
            item.reason_code for item in gates if item.status != GateStatus.PASS
        ) + survival_blockers
        context_blockers = []
        if not price_observation.executable_for_position and request.current_price is not None:
            context_blockers.append(price_observation.reason_code)
        if position_input_conflict:
            context_blockers.append("POSITION_INPUT_CONFLICT")
        if account.trust_status == "ACCOUNT_INPUT_CONFLICT":
            context_blockers.append("ACCOUNT_INPUT_CONFLICT")
        planning_blockers = tuple(
            sorted(set(planning_gate_blockers + tuple(context_blockers)))
        )

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
        risk_gate_codes = (
            "NO_CLEAR_INVALIDATION",
            "STOP_TOO_FAR",
            "RISK_REWARD_INSUFFICIENT",
            "LOSS_POSITION_ADD_BLOCKED",
            "T1_NEW_POSITION_RISK_EXCEEDED",
            "MAX_POSITION_EXCEEDED",
            "DAILY_LOSS_LIMIT_REACHED",
            "SINGLE_STOCK_LOSS_LIMIT",
        )
        risk_fully_passed = all(
            gate_by_code[code].status == GateStatus.PASS for code in risk_gate_codes
        ) and not context_blockers
        risk_component = _score(
            Decimal("13") if risk_fully_passed else ZERO,
            Decimal("15"),
            "RISK_CONTEXT_FULLY_EVALUATED"
            if risk_fully_passed
            else "RISK_CONTEXT_BLOCKED_OR_NOT_EVALUATED",
            missing=tuple(
                gate_by_code[code].reason_code
                for code in risk_gate_codes
                if gate_by_code[code].status != GateStatus.PASS
            ),
            ceiling=Decimal("15") if risk_fully_passed else ZERO,
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

        hard_stop_triggered = any(
            item.reason_code == "HARD_STOP_TRIGGERED"
            and item.status == SurvivalRuleStatus.BLOCK
            for item in survival_discipline
        )
        if hard_stop_triggered:
            plan_status = PlanStatus.EXIT if has_position else PlanStatus.NO_TRADE
        elif data_status in {DataStatus.INSUFFICIENT_DATA, DataStatus.STALE, DataStatus.CONFLICTED_DATA}:
            plan_status = PlanStatus.INSUFFICIENT_DATA
        elif cycle == CycleState.DECLINE or gate_by_code[
            "NO_CLEAR_INVALIDATION"
        ].status != GateStatus.PASS:
            plan_status = PlanStatus.EXIT if has_position else PlanStatus.NO_TRADE
        elif has_position and cycle == CycleState.CLIMAX:
            plan_status = PlanStatus.REDUCE
        elif has_position:
            plan_status = PlanStatus.HOLD
        elif (
            not planning_blockers
            and price_observation.executable_for_entry
            and intraday_confirmed
            and (breakout or low_volume_pullback)
        ):
            plan_status = PlanStatus.ENTRY_ALLOWED
        else:
            plan_status = PlanStatus.WAIT_FOR_TRIGGER

        holding_plan = None
        if has_position:
            quantity_held = Decimal(current_quantity)
            unrealized = (
                (current - account.average_cost) * quantity_held
                if account.average_cost is not None and quantity_held
                else None
            )
            holding_plan = HoldingPlan(
                unrealized_pnl=_q(unrealized),
                risk_amount=_q(held_risk) if held_risk else None,
                risk_pct=_q(held_risk / account_size * Decimal("100"))
                if held_risk and account_size
                else None,
                allow_hold=plan_status in {PlanStatus.HOLD, PlanStatus.REDUCE},
                allow_add=bool(
                    account.average_cost
                    and current > account.average_cost
                    and industry_status == ContextStatus.AVAILABLE
                    and role not in {StockRole.FOLLOWER, StockRole.UNKNOWN}
                    and current > stop
                    and not planning_blockers
                    and price_observation.executable_for_position
                ),
                stop_distance_pct=_q((current - stop) / current * Decimal("100")),
                maximum_risk_after_add=_q(
                    maximum_loss_after_trade
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
        audit_lineage = source_lineage + (
            SourceLineage(
                capability="selected_stock.price_observation",
                provider_id=(
                    "user-observation"
                    if request.current_price is not None
                    else "selected-stock-market-close"
                ),
                source=price_observation.source,
                price_unit="CNY",
                row_count=1,
                observed_at=price_observation.observed_at,
                fetched_at=to_shanghai_aware(generated_at),
                details={
                    "trust_status": price_observation.trust_status.value,
                    "reason_code": price_observation.reason_code,
                    "executable_for_entry": price_observation.executable_for_entry,
                    "executable_for_position": price_observation.executable_for_position,
                },
            ),
            SourceLineage(
                capability="selected_stock.account_context",
                provider_id=(
                    "account-database"
                    if account.source == "SERVER_ACCOUNT"
                    else "user-account-observation"
                    if account.source == "MANUAL_ACCOUNT_CONTEXT"
                    else "none"
                ),
                source=account.source,
                row_count=1 if account.source != "NO_ACCOUNT_CONTEXT" else 0,
                observed_at=account.observed_at,
                fetched_at=to_shanghai_aware(generated_at),
                details={
                    "account_id": account.account_id,
                    "trust_status": account.trust_status,
                    "conflict_fields": list(account.conflict_fields),
                },
            ),
        )
        selected_snapshot_hash = canonical_hash(
            {
                "data_snapshot_hash": snapshot_hash,
                "price_observation": price_observation.model_dump(mode="json"),
                "account_context": account.model_dump(mode="json"),
            }
        )
        output_blocked = bool(planning_blockers)
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
            "industry_context": effective_industry_context,
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
            "passed_conditions": tuple(
                item.reason_code for item in gates if item.status == GateStatus.PASS
            ),
            "failed_conditions": tuple(
                item.reason_code for item in gates if item.status == GateStatus.BLOCKED
            ),
            "pending_conditions": tuple(
                sorted(
                    set(
                        pending
                        + [
                            item.reason_code
                            for item in gates
                            if item.status == GateStatus.NOT_EVALUATED
                        ]
                    )
                )
            ),
            "price_plan": PricePlan(
                support_zone_low=_q(support_low),
                support_zone_high=_q(support_high),
                entry_zone_low=_q(entry_low) if not output_blocked else None,
                entry_zone_high=_q(entry_high) if not output_blocked else None,
                trigger_price=_q(resistance_low) if breakout else None,
                stop_loss=_q(stop),
                first_take_profit=_q(resistance_low),
                second_take_profit=_q(resistance_high),
                invalidation_price=_q(stop),
                risk_reward_ratio=_q(reward_risk),
            ),
            "position_plan": PositionPlan(
                initial_position_pct=proposed_trade_pct if not output_blocked else ZERO,
                max_position_pct=max_position_pct if not output_blocked else ZERO,
                proposed_trade_pct=proposed_trade_pct if not output_blocked else ZERO,
                post_trade_position_pct=_q(post_trade_position_pct),
                quantity=quantity if not output_blocked else None,
                max_quantity=max_quantity if not output_blocked else None,
                risk_amount=_q(proposed_risk) if not output_blocked else None,
                maximum_loss_after_trade=_q(maximum_loss_after_trade),
                t1_overnight_gap_risk_pct=_q(
                    overnight_gap_risk_pct * Decimal("100")
                ),
                t1_risk_amount=_q(t1_risk_amount),
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
            "execution_blockers": tuple(
                sorted(
                    set(
                        planning_blockers
                        + (
                            "CSV_V2_FORMAL_EXECUTION_UNAVAILABLE",
                        )
                    )
                )
            ),
            "price_observation": price_observation,
            "account_context": account,
            "survival_discipline": survival_discipline,
            "quality_bindings": quality_bindings,
            "source_lineage": audit_lineage,
            "product_v1_comparison": StrategyComparison(
                conflict_status=comparison_status,
                product_v1_status=product_v1_status,
                csv_v2_status=plan_status.value,
                differences=differences,
            ),
            "explanation": {
                "summary": "Deterministic advisory result; natural-language AI is optional.",
                "risk_notice": "Research only. No automatic order or execution permission.",
                "price_reason_code": price_observation.reason_code,
                "account_trust_status": account.trust_status,
                "user_focus": request.user_focus,
                "catalyst": request.catalyst_context.model_dump(mode="json")
                if request.catalyst_context
                else None,
            },
            "snapshot_hash": selected_snapshot_hash,
        }
        payload["result_digest"] = canonical_hash(payload)
        return SelectedStockAnalysisResult.model_validate(payload)


__all__ = [
    "CycleStructureValidationStrategyV2",
    "DEFAULT_PARAMETERS",
    "classify_cycle_state",
    "classify_stock_role",
]
