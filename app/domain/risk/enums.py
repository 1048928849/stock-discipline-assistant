from enum import Enum


class RiskStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
