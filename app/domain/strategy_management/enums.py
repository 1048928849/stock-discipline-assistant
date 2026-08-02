from enum import Enum


class StrategyLifecycle(str, Enum):
    DRAFT = "DRAFT"
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"
