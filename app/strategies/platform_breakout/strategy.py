from app.domain.strategy import StrategyContext, StrategyEvaluationResult, StrategyRuleStatus
from app.strategies.platform_breakout.rules import (
    breakout_volume_confirmation,
    large_cycle_direction,
    platform_data_sufficiency,
    platform_structure,
    pullback_structure,
    turn_stronger_confirmation,
)


class PlatformBreakoutPullbackStrategy:
    strategy_id = "platform_breakout_pullback"
    strategy_version = "1.0.0"

    def evaluate(self, context: StrategyContext) -> StrategyEvaluationResult:
        rules = (
            platform_data_sufficiency(context),
            large_cycle_direction(context),
            platform_structure(context),
            breakout_volume_confirmation(context),
            pullback_structure(context),
            turn_stronger_confirmation(context),
        )
        if rules[0].status is StrategyRuleStatus.UNKNOWN:
            overall = StrategyRuleStatus.UNKNOWN
        elif any(rule.status is StrategyRuleStatus.FAIL for rule in rules):
            overall = StrategyRuleStatus.FAIL
        elif all(rule.status is StrategyRuleStatus.PASS for rule in rules):
            overall = StrategyRuleStatus.PASS
        else:
            overall = StrategyRuleStatus.WAIT
        return StrategyEvaluationResult(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            overall_status=overall,
            rules=rules,
            reasons=tuple(
                rule.reason
                for rule in rules
                if rule.status in {StrategyRuleStatus.FAIL, StrategyRuleStatus.UNKNOWN}
            ),
            next_observations=tuple(
                rule.reason
                for rule in rules
                if rule.status in {StrategyRuleStatus.WAIT, StrategyRuleStatus.UNKNOWN}
            ),
            missing_data=tuple(
                sorted({item for rule in rules for item in rule.missing_data})
            ),
        )
