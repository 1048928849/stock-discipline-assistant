from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.hashing import canonical_hash


class HistoryRequirementPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_date: date
    benchmark_symbols: tuple[str, ...]
    selected_industries: tuple[str, ...]
    required_stock_symbols: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    start_date: date
    end_date: date
    minimum_rows: int = Field(ge=1)
    config_hash: str = Field(min_length=64, max_length=64)
    plan_hash: str = Field(min_length=64, max_length=64)

    @field_validator(
        "benchmark_symbols",
        "selected_industries",
        "required_stock_symbols",
        "required_capabilities",
        mode="before",
    )
    @classmethod
    def stable_unique(cls, value):
        return tuple(sorted(set(value)))

    @classmethod
    def create(
        cls,
        *,
        trade_date: date,
        benchmark_symbols: tuple[str, ...],
        selected_industries: tuple[str, ...],
        required_stock_symbols: tuple[str, ...],
        required_capabilities: tuple[str, ...],
        start_date: date,
        end_date: date,
        minimum_rows: int,
        config: dict[str, Any],
    ) -> "HistoryRequirementPlan":
        config_hash = canonical_hash(config)
        values = {
            "trade_date": trade_date,
            "benchmark_symbols": tuple(sorted(set(benchmark_symbols))),
            "selected_industries": tuple(sorted(set(selected_industries))),
            "required_stock_symbols": tuple(sorted(set(required_stock_symbols))),
            "required_capabilities": tuple(sorted(set(required_capabilities))),
            "start_date": start_date,
            "end_date": end_date,
            "minimum_rows": minimum_rows,
            "config_hash": config_hash,
        }
        return cls(**values, plan_hash=canonical_hash(values))


__all__ = ["HistoryRequirementPlan"]
