from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.preview import PreviewSnapshot
from app.domain.trade_plan import TradePlanVersion
from app.models import RuleVersion, StrategyVersionRecord, TradePlan
from app.schemas_workflow import TradePlanSaveRequest


class TradePlanMapper:
    def to_orm(
        self,
        *,
        request: TradePlanSaveRequest,
        preview: dict,
        frozen: PreviewSnapshot,
        rule: RuleVersion,
        strategy_version: StrategyVersionRecord,
        version: TradePlanVersion,
        confirm_mode: str,
    ) -> TradePlan:
        buy_zone = preview["buy_plan"]["buy_zone"]
        stop = preview["buy_plan"]["hard_stop"]
        quantity = preview["position_calculation"].get("final_allowed_quantity", 0)
        entry = buy_zone[1]
        engine_snapshot = {
            **preview,
            "_confirmation": {
                "confirm_mode": confirm_mode,
                "legacy_recalculate_confirm": confirm_mode == "LEGACY_RECALCULATE",
                "preview_snapshot_id": frozen.snapshot_id,
            },
        }
        return TradePlan(
            account_id=request.account_id,
            rule_version_id=rule.id,
            strategy_id=strategy_version.strategy_id,
            strategy_version_id=strategy_version.id,
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
            plan_version=version.version,
            parent_plan_id=version.parent_plan_id,
            preview_hash=frozen.preview_hash,
            engine_snapshot=engine_snapshot,
            market_snapshot={
                "market_state": request.market_state,
                "sector_state": request.sector_state,
            },
            account_snapshot=preview["account"],
            source_snapshot=preview["sources"],
        )
