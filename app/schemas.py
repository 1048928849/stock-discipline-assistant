from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccountBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    total_assets: Decimal = Field(gt=0, max_digits=20, decimal_places=4)
    cash: Decimal = Field(ge=0, max_digits=20, decimal_places=4)
    available_cash: Decimal = Field(ge=0, max_digits=20, decimal_places=4)

    @model_validator(mode="after")
    def validate_cash(self):
        if self.cash > self.total_assets:
            raise ValueError("现金不能大于总资产")
        if self.available_cash > self.cash:
            raise ValueError("可用资金不能大于现金")
        return self


class AccountCreate(AccountBase):
    pass


class AccountUpdate(AccountBase):
    pass


class AccountRead(AccountBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class HoldingBase(BaseModel):
    account_id: int
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0)
    cost_price: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    current_price: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    sector: str | None = Field(default=None, max_length=100)
    buy_reason: str | None = Field(default=None, max_length=2000)
    invalidation_condition: str | None = Field(default=None, max_length=2000)
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    target_price: Decimal | None = Field(default=None, gt=0)
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=100)
    price_source: str = Field(default="manual", max_length=30)


class HoldingCreate(HoldingBase):
    pass


class HoldingUpdate(HoldingBase):
    pass


class HoldingRead(HoldingBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    price_updated_at: datetime | None
    created_at: datetime
    updated_at: datetime
    market_value: Decimal
    unrealized_pnl: Decimal
    return_pct: Decimal
    position_pct: Decimal


class HealthRead(BaseModel):
    status: str
    database: str
    version: str
