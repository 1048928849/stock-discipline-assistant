from enum import Enum


class StrategyRuleStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WAIT = "WAIT"
    UNKNOWN = "UNKNOWN"


class StrategyRuleCategory(str, Enum):
    CONTEXT = "CONTEXT"
    HARD_REJECT = "HARD_REJECT"
    REQUIRED = "REQUIRED"
    TRIGGER = "TRIGGER"
    HOLD = "HOLD"
    ADD = "ADD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    INFORMATIONAL = "INFORMATIONAL"
