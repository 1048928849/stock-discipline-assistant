from __future__ import annotations

from decimal import Decimal

from app.discovery.contracts import DiscoveryConfig, IndustryDiscoveryInput


_CLASSIFICATION_SCORE = {
    "MAINLINE": Decimal("1"),
    "SECONDARY": Decimal("0.8"),
    "ROTATION": Decimal("0.55"),
    "DIVERGENCE": Decimal("0.3"),
    "FADING": Decimal("0.1"),
    "NONE": Decimal("0"),
}


def _clamp(
    value: Decimal,
    low: Decimal = Decimal("0"),
    high: Decimal = Decimal("1"),
) -> Decimal:
    return max(low, min(high, value))


def _ratio(value: Decimal | None, scale: Decimal) -> Decimal:
    return Decimal("0") if value is None else _clamp(value / scale)


def _signed_ratio(value: Decimal | None, scale: Decimal) -> Decimal:
    if value is None:
        return Decimal("0.5")
    return _clamp((value / scale + Decimal("1")) / Decimal("2"))


class IndustryDiscoveryScorer:
    """Single owner of the accepted candidate-discovery industry weights."""

    def __init__(self, config: DiscoveryConfig) -> None:
        self.config = config

    def score(self, item: IndustryDiscoveryInput) -> Decimal:
        config = self.config
        rs_values = [
            value
            for value in (
                item.relative_strength_5d,
                item.relative_strength_10d,
                item.relative_strength_20d,
            )
            if value is not None
        ]
        relative = (
            sum(
                (_signed_ratio(value, Decimal("20")) for value in rs_values),
                Decimal("0"),
            )
            / Decimal(len(rs_values))
            if rs_values
            else Decimal("0")
        )
        flow_values = [
            value
            for value in (
                item.net_inflow_1d,
                item.net_inflow_5d,
                item.net_inflow_10d,
            )
            if value is not None
        ]
        capital_flow = (
            sum(
                (
                    _signed_ratio(value, Decimal("1000000000"))
                    for value in flow_values
                ),
                Decimal("0"),
            )
            / Decimal(len(flow_values))
            if flow_values
            else Decimal("0")
        )
        score = (
            config.weight_classification
            * _CLASSIFICATION_SCORE[item.classification]
            + config.weight_relative_strength * relative
            + config.weight_amount_share * _ratio(item.amount_share, Decimal("0.15"))
            + config.weight_breadth * _ratio(item.advance_ratio, Decimal("1"))
            + config.weight_limit_up
            * _ratio(Decimal(item.limit_up_count or 0), Decimal("10"))
            + config.weight_leader_strength
            * _ratio(item.leader_strength, Decimal("10"))
            + config.weight_new_highs * _ratio(item.new_high_ratio, Decimal("1"))
            + config.weight_capital_flow * capital_flow
            + config.weight_broken_limit
            * (Decimal("1") - _ratio(item.broken_limit_rate, Decimal("1")))
        )
        return score.quantize(Decimal("0.000001"))

    def rank(
        self, industries: tuple[IndustryDiscoveryInput, ...]
    ) -> tuple[tuple[IndustryDiscoveryInput, Decimal], ...]:
        rows = [
            (item, self.score(item))
            for item in industries
            if item.quality_status in {"VERIFIED", "SINGLE_SOURCE"}
        ]
        rows.sort(key=lambda row: (-row[1], row[0].industry_key))
        return tuple(rows)


__all__ = ["IndustryDiscoveryScorer"]
