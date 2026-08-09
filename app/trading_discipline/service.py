from __future__ import annotations

from decimal import Decimal

from app.domain.hashing import canonical_hash
from app.trading_discipline.contracts import (
    DetectionStatus,
    EvidenceTier,
    GateStatus,
    MarketStage,
    PositionStressInput,
    PositionStressResult,
    PreTradeContext,
    PreTradeResult,
    ProposedAction,
    RuleEvaluation,
    StageAssessment,
    StageEvidence,
    StressStatus,
)


PLAYBOOK_CODE = "CORE_STATE_CHANGE_V1"
RULE_VERSION = "1.0.0"
SCORE_CATEGORIES = (
    "PREDEFINED_PLAYBOOK",
    "PREEXISTING_ENTRY_CONDITION",
    "PREDEFINED_INVALIDATION",
    "PREDEFINED_POSITION",
    "RULE_STABILITY",
)


class TradingDisciplineService:
    """Deterministic constraint layer; never creates formal execution authority."""

    def assess_market_stage(self, item: StageEvidence) -> StageAssessment:
        required = (
            item.prior_base,
            item.above_ma_cluster,
            item.breakout,
            item.volume_ratio,
            item.close_position,
        )
        if sum(value is not None for value in required) < 4:
            return StageAssessment(
                stage=MarketStage.UNKNOWN,
                result=DetectionStatus.NOT_EVALUATED,
                evidence_groups=[],
                reason_codes=["DAILY_EVIDENCE_INSUFFICIENT"],
                as_of=item.as_of,
            )
        groups: list[str] = []
        if item.prior_base:
            groups.append("PRIOR_STRUCTURE")
        if item.above_ma_cluster and item.breakout:
            groups.append("PRICE_STRUCTURE")
        if item.volume_ratio is not None and item.volume_ratio >= Decimal("1.5"):
            groups.append("VOLUME")
        if item.close_position is not None and item.close_position >= Decimal("0.75"):
            groups.append("CANDLE_CLOSE")
        if item.relative_strength_change is not None and item.relative_strength_change > 0:
            groups.append("RELATIVE_STRENGTH")
        if item.industry_participation:
            groups.append("INDUSTRY")
        if item.sessions_since_state_change is not None and item.sessions_since_state_change > 0:
            if item.held_breakout and item.higher_low:
                return StageAssessment(
                    stage=MarketStage.SECOND_CONFIRMATION,
                    result=DetectionStatus.CONFIRMED,
                    evidence_groups=groups + ["SUBSEQUENT_CONFIRMATION"],
                    reason_codes=["SECOND_CONFIRMATION_HELD"],
                    as_of=item.as_of,
                )
            if (
                item.sessions_since_state_change >= 5
                and item.distance_from_structure_pct is not None
                and item.distance_from_structure_pct >= Decimal("12")
            ):
                stage = (
                    MarketStage.CLIMAX
                    if item.distance_from_structure_pct >= Decimal("20")
                    else MarketStage.ACCELERATION
                )
                return StageAssessment(
                    stage=stage,
                    result=DetectionStatus.CONFIRMED,
                    evidence_groups=groups,
                    reason_codes=[f"{stage.value}_DISTANCE"],
                    as_of=item.as_of,
                )
        if len(set(groups)) >= 3 and "PRICE_STRUCTURE" in groups:
            return StageAssessment(
                stage=MarketStage.STATE_CHANGE,
                result=DetectionStatus.CANDIDATE,
                evidence_groups=groups,
                reason_codes=["STATE_CHANGE_MULTI_GROUP"],
                as_of=item.as_of,
            )
        return StageAssessment(
            stage=MarketStage.DORMANT,
            result=DetectionStatus.NOT_PRESENT,
            evidence_groups=groups,
            reason_codes=["STATE_CHANGE_NOT_PRESENT"],
            as_of=item.as_of,
        )

    def pretrade_check(self, ctx: PreTradeContext) -> PreTradeResult:
        rules: list[RuleEvaluation] = []

        def add(
            code: str,
            status: GateStatus,
            reason: str,
            effect: str,
            required: list[str],
            evaluated: list[str],
            missing: list[str] | None = None,
            actual=None,
            threshold=None,
        ):
            rules.append(
                RuleEvaluation(
                    rule_code=code,
                    status=status,
                    required_inputs=required,
                    evaluated_inputs=evaluated,
                    missing_inputs=missing or [],
                    evidence={},
                    threshold=threshold,
                    actual_value=actual,
                    reason_code=reason,
                    effect_on_action=effect,
                    source_refs=ctx.source_refs,
                )
            )

        known_playbook = ctx.playbook_code == PLAYBOOK_CODE
        add(
            "KNOWN_PLAYBOOK",
            GateStatus.PASS if known_playbook else GateStatus.BLOCK,
            "PLAYBOOK_VERIFIED" if known_playbook else "OUT_OF_PLAYBOOK",
            "NONE" if known_playbook else "BLOCK_EXPANSION",
            ["playbook_code"],
            ["playbook_code"],
        )
        playbook_preexisting = (
            ctx.playbook_selected_at is not None and ctx.playbook_selected_at <= ctx.decision_at
        )
        add(
            "PLAYBOOK_SELECTED_BEFORE_DECISION",
            GateStatus.PASS if playbook_preexisting else GateStatus.BLOCK,
            "PLAYBOOK_PREEXISTED" if playbook_preexisting else "PLAYBOOK_CREATED_AFTER_DECISION",
            "NONE" if playbook_preexisting else "BLOCK_EXPANSION",
            ["playbook_selected_at", "decision_at"],
            ["decision_at"] + (["playbook_selected_at"] if ctx.playbook_selected_at else []),
            [] if ctx.playbook_selected_at else ["playbook_selected_at"],
        )
        entry_preexisting = (
            ctx.entry_evidence_at is not None and ctx.entry_evidence_at <= ctx.decision_at
        )
        add(
            "PREEXISTING_ENTRY_EVIDENCE",
            GateStatus.PASS if entry_preexisting else GateStatus.BLOCK,
            "ENTRY_EVIDENCE_PREEXISTED" if entry_preexisting else "UNPLANNED_TRADE",
            "NONE" if entry_preexisting else "BLOCK_EXPANSION",
            ["entry_evidence_at", "decision_at"],
            ["decision_at"] + (["entry_evidence_at"] if ctx.entry_evidence_at else []),
            [] if ctx.entry_evidence_at else ["entry_evidence_at"],
        )
        invalidation_ok = (
            ctx.invalidation_defined_at is not None
            and ctx.invalidation_defined_at <= ctx.decision_at
        )
        add(
            "PREDEFINED_INVALIDATION",
            GateStatus.PASS if invalidation_ok else GateStatus.BLOCK,
            "INVALIDATION_PREDEFINED" if invalidation_ok else "MISSING_INVALIDATION",
            "NONE" if invalidation_ok else "BLOCK_EXPANSION",
            ["invalidation_defined_at"],
            ["invalidation_defined_at"] if ctx.invalidation_defined_at else [],
            [] if ctx.invalidation_defined_at else ["invalidation_defined_at"],
        )
        add(
            "PREDEFINED_HARD_STOP",
            GateStatus.PASS if ctx.hard_stop is not None else GateStatus.BLOCK,
            "HARD_STOP_PREDEFINED" if ctx.hard_stop is not None else "MISSING_STOP",
            "NONE" if ctx.hard_stop is not None else "BLOCK_EXPANSION",
            ["hard_stop"],
            ["hard_stop"] if ctx.hard_stop is not None else [],
            [] if ctx.hard_stop is not None else ["hard_stop"],
        )
        size_ok = (
            ctx.intended_quantity is not None
            and ctx.planned_max_quantity is not None
            and ctx.proposed_quantity <= ctx.planned_max_quantity
        )
        add(
            "PREDEFINED_POSITION",
            GateStatus.PASS if size_ok else GateStatus.BLOCK,
            "POSITION_WITHIN_PLAN" if size_ok else "POSITION_ABOVE_PLAN",
            "NONE" if size_ok else "BLOCK_EXPANSION",
            ["intended_quantity", "planned_max_quantity"],
            [
                name
                for name, value in (
                    ("intended_quantity", ctx.intended_quantity),
                    ("planned_max_quantity", ctx.planned_max_quantity),
                )
                if value is not None
            ],
            [
                name
                for name, value in (
                    ("intended_quantity", ctx.intended_quantity),
                    ("planned_max_quantity", ctx.planned_max_quantity),
                )
                if value is None
            ],
            ctx.proposed_quantity,
            ctx.planned_max_quantity,
        )
        in_structure = (
            ctx.planned_entry_low is not None
            and ctx.planned_entry_high is not None
            and ctx.planned_entry_low
            <= ctx.current_price
            <= ctx.planned_entry_high * Decimal("1.03")
        )
        add(
            "ENTRY_STRUCTURE",
            GateStatus.PASS if in_structure else GateStatus.BLOCK,
            "PRICE_IN_STRUCTURE" if in_structure else "ENTRY_TOO_FAR_FROM_PLAN",
            "NONE" if in_structure else "BLOCK_EXPANSION",
            ["current_price", "planned_entry_low", "planned_entry_high"],
            ["current_price"]
            + [
                n
                for n, v in (
                    ("planned_entry_low", ctx.planned_entry_low),
                    ("planned_entry_high", ctx.planned_entry_high),
                )
                if v is not None
            ],
            [
                n
                for n, v in (
                    ("planned_entry_low", ctx.planned_entry_low),
                    ("planned_entry_high", ctx.planned_entry_high),
                )
                if v is None
            ],
        )
        chase = ctx.chase_risk is True or ctx.market_stage in {
            MarketStage.ACCELERATION,
            MarketStage.CLIMAX,
        }
        add(
            "CHASE_RISK",
            GateStatus.BLOCK
            if chase
            else (
                GateStatus.NOT_EVALUATED
                if ctx.chase_risk is None and ctx.market_stage is MarketStage.UNKNOWN
                else GateStatus.PASS
            ),
            "ENTRY_AFTER_CLIMAX"
            if ctx.market_stage is MarketStage.CLIMAX
            else ("LATE_CONFIRMATION_RISK" if chase else "CHASE_NOT_ACTIVE"),
            "BLOCK_EXPANSION" if chase else "NONE",
            ["chase_risk", "market_stage"],
            ["market_stage"] + (["chase_risk"] if ctx.chase_risk is not None else []),
            []
            if ctx.chase_risk is not None or ctx.market_stage is not MarketStage.UNKNOWN
            else ["chase_risk"],
        )
        losing_add = (
            ctx.action is ProposedAction.ADD
            and ctx.cost_price is not None
            and ctx.current_price < ctx.cost_price
        )
        add(
            "LOSS_AVERAGING",
            GateStatus.BLOCK if losing_add else GateStatus.PASS,
            "LOSS_AVERAGING" if losing_add else "NO_LOSS_AVERAGING",
            "BLOCK_ADD" if losing_add else "NONE",
            ["action", "cost_price", "current_price"],
            ["action", "current_price"] + (["cost_price"] if ctx.cost_price is not None else []),
        )
        low_authority = ctx.new_primary_evidence_tier in {
            EvidenceTier.ANALYSIS,
            EvidenceTier.SENTIMENT,
        }
        mutation = ctx.thesis_changed_after_entry or low_authority
        add(
            "RULE_STABILITY",
            GateStatus.BLOCK
            if mutation and ctx.action in {ProposedAction.BUY, ProposedAction.ADD}
            else GateStatus.PASS,
            "POST_POSITION_NEW_REASON" if mutation else "RULE_STABLE",
            "REQUIRE_REANALYSIS" if mutation else "NONE",
            ["thesis_changed_after_entry", "new_primary_evidence_tier"],
            ["thesis_changed_after_entry"]
            + (["new_primary_evidence_tier"] if ctx.new_primary_evidence_tier else []),
        )
        upstream = (
            ctx.product_v1_status == "NO_TRADE"
            or bool(ctx.survival_blocks)
            or ctx.csv_v2_executable
        )
        add(
            "UPSTREAM_AUTHORITY",
            GateStatus.BLOCK if upstream else GateStatus.PASS,
            "PRODUCT_V1_NO_TRADE"
            if ctx.product_v1_status == "NO_TRADE"
            else (
                ctx.survival_blocks[0]
                if ctx.survival_blocks
                else ("CSV_V2_AUTHORITY_VIOLATION" if ctx.csv_v2_executable else "UPSTREAM_CLEAR")
            ),
            "BLOCK_EXPANSION" if upstream else "NONE",
            ["product_v1_status", "csv_v2_executable", "survival_blocks"],
            ["product_v1_status", "csv_v2_executable", "survival_blocks"],
        )

        scores = {
            "PREDEFINED_PLAYBOOK": 20 if known_playbook and playbook_preexisting else 0,
            "PREEXISTING_ENTRY_CONDITION": 20 if entry_preexisting else 0,
            "PREDEFINED_INVALIDATION": 20 if invalidation_ok and ctx.hard_stop is not None else 0,
            "PREDEFINED_POSITION": 20 if size_ok else 0,
            "RULE_STABILITY": 20 if not mutation else 0,
        }
        status = (
            GateStatus.BLOCK
            if any(r.status is GateStatus.BLOCK for r in rules)
            else (
                GateStatus.NOT_EVALUATED
                if any(r.status is GateStatus.NOT_EVALUATED for r in rules)
                else (
                    GateStatus.WARN
                    if any(r.status is GateStatus.WARN for r in rules)
                    else GateStatus.PASS
                )
            )
        )
        payload = {
            "context": ctx.model_dump(mode="json"),
            "rules": [r.model_dump(mode="json") for r in rules],
            "scores": scores,
            "rule_version": RULE_VERSION,
        }
        return PreTradeResult(
            status=status,
            action=ctx.action,
            rules=rules,
            score=sum(scores.values()),
            category_scores=scores,
            reason_codes=[r.reason_code for r in rules if r.status is not GateStatus.PASS],
            executable=False,
            snapshot_hash=canonical_hash(payload),
        )

    def position_stress(self, item: PositionStressInput) -> PositionStressResult:
        proposed_value = (
            Decimal(item.current_quantity + item.proposed_quantity) * item.current_price
        )
        position_pct = proposed_value / item.total_assets * 100
        planned = item.planned_max_position_pct or item.single_name_limit_pct
        sector_pct = (
            (item.sector_value + Decimal(item.proposed_quantity) * item.current_price)
            / item.total_assets
            * 100
        )
        total_pct = (
            (item.total_exposure_value + Decimal(item.proposed_quantity) * item.current_price)
            / item.total_assets
            * 100
        )
        impacts = {
            f"adverse_{pct}_pct": proposed_value
            * Decimal(pct)
            / Decimal("100")
            / item.total_assets
            * 100
            for pct in (3, 5, 10)
        }
        stop_impact = (
            Decimal("0")
            if item.hard_stop is None
            else max(Decimal("0"), item.current_price - item.hard_stop)
            * Decimal(item.current_quantity + item.proposed_quantity)
            / item.total_assets
            * 100
        )
        t1 = proposed_value * item.t1_gap_risk_pct / Decimal("100") / item.total_assets * 100
        reasons: list[str] = []
        if position_pct > planned:
            reasons.append("POSITION_ABOVE_PLAN")
        if position_pct > item.single_name_limit_pct:
            reasons.append("POSITION_ABOVE_SINGLE_NAME_LIMIT")
        if impacts["adverse_10_pct"] >= Decimal("2"):
            reasons.append("ADVERSE_MOVE_ACCOUNT_IMPACT_HIGH")
        if t1 >= Decimal("1"):
            reasons.append("T1_RISK_HIGH")
        if sector_pct > item.sector_limit_pct:
            reasons.append("SECTOR_CONCENTRATION_HIGH")
        if total_pct > item.total_limit_pct:
            reasons.append("TOTAL_EXPOSURE_HIGH")
        critical = any(
            code in reasons
            for code in (
                "POSITION_ABOVE_SINGLE_NAME_LIMIT",
                "ADVERSE_MOVE_ACCOUNT_IMPACT_HIGH",
                "T1_RISK_HIGH",
                "TOTAL_EXPOSURE_HIGH",
            )
        )
        status = (
            StressStatus.CRITICAL
            if critical
            else (
                StressStatus.HIGH
                if reasons
                else (
                    StressStatus.ELEVATED
                    if position_pct >= planned * Decimal("0.8")
                    else StressStatus.NORMAL
                )
            )
        )
        metrics = {
            "position_pct": position_pct,
            "planned_max_position_pct": planned,
            "excess_position_pct": max(Decimal("0"), position_pct - planned),
            "single_name_open_risk_pct": stop_impact,
            "sector_concentration_pct": sector_pct,
            "total_exposure_pct": total_pct,
            "available_cash": item.available_cash,
            "hard_stop_account_impact_pct": stop_impact,
            "t1_gap_risk_impact_pct": t1,
            **impacts,
        }
        return PositionStressResult(
            status=status,
            metrics=metrics,
            reason_codes=reasons,
            add_blocked=critical,
            reanalysis_required=critical,
        )


__all__ = ["PLAYBOOK_CODE", "RULE_VERSION", "SCORE_CATEGORIES", "TradingDisciplineService"]
