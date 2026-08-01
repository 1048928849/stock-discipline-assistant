from app.domain.strategy_management import StrategyLifecycle

TRANSITIONS: dict[StrategyLifecycle, frozenset[StrategyLifecycle]] = {
    StrategyLifecycle.DRAFT: frozenset({StrategyLifecycle.RESEARCH}),
    StrategyLifecycle.RESEARCH: frozenset({StrategyLifecycle.SHADOW}),
    StrategyLifecycle.SHADOW: frozenset(
        {StrategyLifecycle.ACTIVE, StrategyLifecycle.RETIRED}
    ),
    StrategyLifecycle.ACTIVE: frozenset(
        {StrategyLifecycle.SUSPENDED, StrategyLifecycle.RETIRED}
    ),
    StrategyLifecycle.SUSPENDED: frozenset(
        {StrategyLifecycle.ACTIVE, StrategyLifecycle.RETIRED}
    ),
    StrategyLifecycle.RETIRED: frozenset(),
}


def can_transition(current: StrategyLifecycle, target: StrategyLifecycle) -> bool:
    return target in TRANSITIONS[current]


def require_transition(current: StrategyLifecycle, target: StrategyLifecycle) -> None:
    if not can_transition(current, target):
        raise ValueError(f"非法策略生命周期转换：{current.value} -> {target.value}")
