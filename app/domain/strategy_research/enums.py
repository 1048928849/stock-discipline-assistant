from enum import Enum


class EvidenceType(str, Enum):
    MANUAL = "MANUAL"
    MARKET_DATA = "MARKET_DATA"
    FINANCIAL_REPORT = "FINANCIAL_REPORT"
    NEWS = "NEWS"
    CASE_STUDY = "CASE_STUDY"


class ValidationStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
