from enum import Enum


class TradePlanLifecycle(str, Enum):
    DRAFT = "draft"
    PREVIEW = "preview"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    HOLDING = "holding"
    CLOSED = "closed"
