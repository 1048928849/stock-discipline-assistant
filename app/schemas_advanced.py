from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TradeCreate(BaseModel):
    account_id: int
    symbol: str = Field(pattern=r"^\d{6}$")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(default=0, ge=0)
    traded_at: datetime
    reason: str | None = Field(default=None, max_length=2000)
    is_planned: bool = True
    emotion: str | None = Field(default=None, max_length=50)
    expectation: str | None = Field(default=None, max_length=2000)
    invalidation_condition: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)


class TradeRead(TradeCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class ReviewCreate(BaseModel):
    trade_id: int | None = None
    period_type: Literal["trade", "daily", "weekly"]
    period_start: date
    conclusion: str = Field(min_length=1, max_length=5000)


class ReviewRead(ReviewCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    metrics: dict | None = None
    created_at: datetime
    updated_at: datetime


class DisciplineAlert(BaseModel):
    rule: str
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    message: str
    trigger_data: dict


class DisciplineRuleCreate(BaseModel):
    code: Literal["single_position_pct", "sector_pct", "total_pct", "daily_trades"]
    name: str = Field(min_length=1, max_length=100)
    severity: Literal["INFO", "WARNING", "CRITICAL"] = "WARNING"
    enabled: bool = True
    config: dict


class WatchAccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_]+$")
    enabled: bool = True


class WatchQueryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expression: str = Field(min_length=1, max_length=500)
    enabled: bool = True


class AIResult(BaseModel):
    translation_zh: str
    summary: str
    category: Literal[
        "光模块/CPO", "PCB", "存储", "先进封装", "消费电子", "国产算力", "宏观", "其他"
    ]
    companies: list[str]
    industry_chain: list[str]
    information_type: Literal["事实", "公司表态", "媒体报道", "个人观点", "未经证实传闻"]
    potential_positive: list[str]
    potential_negative: list[str]
    verification_items: list[str]


class BacktestParameters(BaseModel):
    stop_pct: float = Field(default=0.08, ge=0.01, le=0.5)
    fast: int = Field(default=5, ge=2, le=120)
    slow: int = Field(default=20, ge=3, le=250)
    max_position: float = Field(default=0.2, gt=0, lt=1)
    batch_drop: float = Field(default=0.03, ge=0.005, le=0.3)
    take_profit: float = Field(default=0.1, ge=0.01, le=1)

    @model_validator(mode="after")
    def validate_ma_periods(self):
        if self.fast >= self.slow:
            raise ValueError("快均线周期必须小于慢均线周期")
        return self


class BacktestCreate(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    strategy: Literal["fixed_stop", "moving_average", "batch_buy", "trend_filter"]
    date_from: date
    date_to: date
    cash: Decimal = Field(default=100000, gt=0)
    commission: float = Field(default=0.0003, ge=0, le=0.02)
    slippage: float = Field(default=0.001, ge=0, le=0.05)
    parameters: BacktestParameters = Field(default_factory=BacktestParameters)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.date_to <= self.date_from:
            raise ValueError("结束日期必须晚于开始日期")
        return self
