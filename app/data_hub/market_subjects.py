from __future__ import annotations

import hashlib
import unicodedata

from app.domain.quality_subject import SubjectRef


def _required_component(value: str, *, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _price_unit(value: str) -> str:
    return _required_component(value, name="price_unit").upper()


def _volume_unit(value: str) -> str:
    return _required_component(value, name="volume_unit").lower()


def _adjustment(value: str) -> str:
    return _required_component(value, name="adjustment").lower()


def stock_quote_subject(
    symbol: str,
    quote_type: str,
    price_unit: str,
) -> SubjectRef:
    semantic_key = (
        f"{_required_component(quote_type, name='quote_type').lower()}/"
        f"{_price_unit(price_unit)}"
    )
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key=semantic_key,
    )


def stock_daily_subject(
    symbol: str,
    adjustment: str,
    price_unit: str,
    volume_unit: str,
) -> SubjectRef:
    semantic_key = (
        f"{_adjustment(adjustment)}/{_price_unit(price_unit)}/"
        f"{_volume_unit(volume_unit)}"
    )
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key=semantic_key,
    )


def stock_intraday_subject(symbol: str) -> SubjectRef:
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key="60m/qfq/CNY/share",
    )


def stock_turnover_subject(symbol: str) -> SubjectRef:
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key="daily/ratio",
    )


def market_breadth_subject() -> SubjectRef:
    return SubjectRef(
        subject_type="market",
        subject_id="CN-A",
        semantic_key="daily/all-a",
    )


def market_amount_subject() -> SubjectRef:
    return SubjectRef(
        subject_type="market",
        subject_id="CN-A",
        semantic_key="daily/CNY",
    )


def industry_constituents_subject(industry: str) -> SubjectRef:
    base = sector_daily_subject(industry, "unadjusted", "CNY", "share")
    return SubjectRef(
        subject_type=base.subject_type,
        subject_id=base.subject_id,
        semantic_key="constituents/current",
    )


def company_concepts_subject(symbol: str) -> SubjectRef:
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key="concepts/current",
    )


def company_industry_chain_subject(symbol: str) -> SubjectRef:
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key="industry-chain/current",
    )


def index_daily_subject(
    index_id: str,
    adjustment: str,
    price_unit: str,
    volume_unit: str,
) -> SubjectRef:
    normalized_id = _required_component(index_id, name="index_id").upper()
    if normalized_id in {"000300", "000300.SH", "000300.SS"}:
        normalized_id = "CSI000300"
    semantic_key = (
        f"{_adjustment(adjustment)}/{_price_unit(price_unit)}/"
        f"{_volume_unit(volume_unit)}"
    )
    return SubjectRef(
        subject_type="index",
        subject_id=normalized_id,
        semantic_key=semantic_key,
    )


def sector_daily_subject(
    board_name: str,
    adjustment: str,
    price_unit: str,
    volume_unit: str,
) -> SubjectRef:
    normalized_name = unicodedata.normalize(
        "NFKC",
        _required_component(board_name, name="board_name"),
    ).strip()
    if not normalized_name:
        raise ValueError("board_name must not be empty")
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()
    semantic_key = (
        f"{_adjustment(adjustment)}/{_price_unit(price_unit)}/"
        f"{_volume_unit(volume_unit)}"
    )
    return SubjectRef(
        subject_type="sector",
        subject_id=f"S{digest[:11].upper()}",
        semantic_key=semantic_key,
    )


__all__ = [
    "company_concepts_subject",
    "company_industry_chain_subject",
    "index_daily_subject",
    "industry_constituents_subject",
    "market_amount_subject",
    "market_breadth_subject",
    "sector_daily_subject",
    "stock_daily_subject",
    "stock_intraday_subject",
    "stock_quote_subject",
    "stock_turnover_subject",
]
