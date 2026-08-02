from __future__ import annotations

from datetime import date

import pandas as pd

from app.domain.features import Feature, FeatureQuality, FeatureSnapshot, FeatureValue
from app.features.platform import calculate_platform_facts
from app.features.technical import calculate_technical_facts
from app.features.timeframes import calculate_timeframe_facts


class FeaturePipeline:
    """Build a typed, decision-free fact snapshot from normalized market data."""

    def build(
        self,
        *,
        symbol: str,
        as_of: date | str,
        market_data: pd.DataFrame | None,
        parameters: dict,
        source_ids: tuple[str, ...] = (),
        data_time: str | None = None,
        quality: FeatureQuality = FeatureQuality.GOOD,
        missing_reason: str | None = None,
    ) -> FeatureSnapshot:
        as_of_text = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        if market_data is None or market_data.empty:
            missing = FeatureValue(
                value={"missing_reason": missing_reason or "market_data_missing"},
                source_ids=source_ids,
                data_time=data_time,
                quality=FeatureQuality.MISSING,
            )
            return FeatureSnapshot(
                symbol=symbol,
                as_of=as_of_text,
                features=(
                    Feature("platform_structure", missing),
                    Feature("technical_indicators", missing),
                    Feature("multi_timeframe", missing),
                ),
            )
        return FeatureSnapshot(
            symbol=symbol,
            as_of=as_of_text,
            features=(
                Feature(
                    "platform_structure",
                    FeatureValue(
                        value=calculate_platform_facts(market_data, parameters),
                        source_ids=source_ids,
                        data_time=data_time,
                        quality=quality,
                    ),
                ),
                Feature(
                    "technical_indicators",
                    FeatureValue(
                        value=calculate_technical_facts(market_data),
                        source_ids=source_ids,
                        data_time=data_time,
                        quality=quality,
                    ),
                ),
                Feature(
                    "multi_timeframe",
                    FeatureValue(
                        value=calculate_timeframe_facts(market_data),
                        source_ids=source_ids,
                        data_time=data_time,
                        quality=quality,
                    ),
                ),
            ),
        )
