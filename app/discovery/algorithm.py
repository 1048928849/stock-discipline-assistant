from __future__ import annotations

from decimal import Decimal

from app.domain.hashing import canonical_hash
from app.domain.quality import DataQualityStatus, worst_quality
from app.discovery.contracts import (
    CandidateIndustryResult,
    CandidateResult,
    CandidateStockInput,
    DiscoveryConfig,
    DiscoveryResult,
    DiscoverySnapshot,
    IndustryDiscoveryInput,
)


_EXECUTABLE_QUALITY = {"VERIFIED", "SINGLE_SOURCE"}
_CLASSIFICATION_SCORE = {
    "MAINLINE": Decimal("1"),
    "SECONDARY": Decimal("0.8"),
    "ROTATION": Decimal("0.55"),
    "DIVERGENCE": Decimal("0.3"),
    "FADING": Decimal("0.1"),
    "NONE": Decimal("0"),
}


def _worst_quality(*statuses: str) -> str:
    return worst_quality([DataQualityStatus(item) for item in statuses]).value


def _clamp(value: Decimal, low: Decimal = Decimal("0"), high: Decimal = Decimal("1")):
    return max(low, min(high, value))


def _ratio(value: Decimal | None, scale: Decimal) -> Decimal:
    return Decimal("0") if value is None else _clamp(value / scale)


def _signed_ratio(value: Decimal | None, scale: Decimal) -> Decimal:
    if value is None:
        return Decimal("0.5")
    return _clamp((value / scale + Decimal("1")) / Decimal("2"))


def _industry_score(item: IndustryDiscoveryInput, config: DiscoveryConfig) -> Decimal:
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
        sum((_signed_ratio(value, Decimal("20")) for value in rs_values), Decimal("0"))
        / Decimal(len(rs_values))
        if rs_values
        else Decimal("0")
    )
    flow_values = [
        value
        for value in (item.net_inflow_1d, item.net_inflow_5d, item.net_inflow_10d)
        if value is not None
    ]
    capital_flow = (
        sum((_signed_ratio(value, Decimal("1000000000")) for value in flow_values), Decimal("0"))
        / Decimal(len(flow_values))
        if flow_values
        else Decimal("0")
    )
    score = (
        config.weight_classification * _CLASSIFICATION_SCORE[item.classification]
        + config.weight_relative_strength * relative
        + config.weight_amount_share * _ratio(item.amount_share, Decimal("0.15"))
        + config.weight_breadth * _ratio(item.advance_ratio, Decimal("1"))
        + config.weight_limit_up
        * _ratio(Decimal(item.limit_up_count or 0), Decimal("10"))
        + config.weight_leader_strength * _ratio(item.leader_strength, Decimal("10"))
        + config.weight_new_highs * _ratio(item.new_high_ratio, Decimal("1"))
        + config.weight_capital_flow * capital_flow
        + config.weight_broken_limit
        * (Decimal("1") - _ratio(item.broken_limit_rate, Decimal("1")))
    )
    return score.quantize(Decimal("0.000001"))


def _return(prices: list[Decimal], sessions: int) -> Decimal:
    if len(prices) <= sessions:
        return Decimal("0")
    return ((prices[-1] / prices[-sessions - 1]) - Decimal("1")) * Decimal("100")


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _drawdown(prices: list[Decimal]) -> Decimal:
    peak = prices[0]
    maximum = Decimal("0")
    for price in prices:
        peak = max(peak, price)
        maximum = max(maximum, (peak - price) / peak * Decimal("100"))
    return maximum


def _candidate_metrics(item: CandidateStockInput) -> dict[str, Decimal]:
    ordered = sorted(item.prices, key=lambda row: row.trade_date)
    closes = [row.close for row in ordered]
    ma20 = _mean(closes[-20:])
    ma50 = _mean(closes[-50:])
    return {
        "return_5d_pct": _return(closes, 5).quantize(Decimal("0.0001")),
        "return_20d_pct": _return(closes, 20).quantize(Decimal("0.0001")),
        "distance_ma20_pct": ((closes[-1] / ma20 - 1) * 100).quantize(Decimal("0.0001")),
        "distance_ma50_pct": ((closes[-1] / ma50 - 1) * 100).quantize(Decimal("0.0001")),
        "max_drawdown_20d_pct": _drawdown(closes[-20:]).quantize(Decimal("0.0001")),
        "average_amount_20d": _mean([row.amount for row in ordered[-20:]]).quantize(
            Decimal("0.0001")
        ),
        "turnover_rate": ordered[-1].turnover_rate.quantize(Decimal("0.0001")),
        "current_price": closes[-1],
    }


def _candidate(
    item: CandidateStockInput,
    industry: IndustryDiscoveryInput,
    industry_score: Decimal,
    config: DiscoveryConfig,
    *,
    high_risk_market: bool,
) -> tuple[Decimal, dict, tuple[str, ...], tuple[str, ...], str] | None:
    if item.quality_status not in _EXECUTABLE_QUALITY:
        return None
    if item.is_st or item.delisting or item.suspended:
        return None
    if len(item.prices) < config.minimum_history_rows:
        return None
    metrics = _candidate_metrics(item)
    if (
        metrics["average_amount_20d"] < config.minimum_average_amount
        or metrics["turnover_rate"] < config.minimum_turnover_rate
    ):
        return None
    overextended = any(
        (
            metrics["return_5d_pct"] > config.max_return_5d_pct,
            metrics["return_20d_pct"] > config.max_return_20d_pct,
            metrics["distance_ma20_pct"] > config.max_distance_ma20_pct,
            metrics["distance_ma50_pct"] > config.max_distance_ma50_pct,
            metrics["turnover_rate"] > config.maximum_turnover_rate,
        )
    )
    trend = _clamp(
        (metrics["return_20d_pct"] + Decimal("10")) / Decimal("35")
    )
    relative = _clamp(
        (metrics["return_20d_pct"] - (industry.relative_strength_20d or 0) + 10)
        / Decimal("30")
    )
    liquidity = _clamp(
        metrics["average_amount_20d"] / (config.minimum_average_amount * Decimal("5"))
    )
    anti_chasing = Decimal("0.1") if overextended else Decimal("1")
    score = (
        industry_score * Decimal("0.35")
        + Decimal("0.65")
        * (
            config.candidate_weight_trend * trend
            + config.candidate_weight_relative_strength * relative
            + config.candidate_weight_liquidity * liquidity
            + config.candidate_weight_anti_chasing * anti_chasing
        )
    ).quantize(Decimal("0.000001"))
    if overextended:
        score = (score * Decimal("0.55")).quantize(Decimal("0.000001"))
    reason_codes = ["STRONG_INDUSTRY", "TREND_STRUCTURE_INTACT"]
    risk_flags = []
    if overextended:
        risk_flags.append("OVEREXTENDED")
        reason_codes.append("ANTI_CHASING_PENALTY")
    if high_risk_market:
        candidate_type = "RESEARCH_ONLY"
        reason_codes.append("MARKET_RISK_RESEARCH_ONLY")
        if item.limit_up:
            reason_codes.append("LIMIT_UP_LEADER_REFERENCE")
    elif item.limit_up:
        candidate_type = "LEADER_REFERENCE"
        reason_codes.append("LIMIT_UP_LEADER_REFERENCE")
    else:
        candidate_type = "WATCH_CANDIDATE"
        reason_codes.append("EARLY_WATCH_CANDIDATE")
    return score, metrics, tuple(sorted(reason_codes)), tuple(sorted(risk_flags)), candidate_type


def discover_candidates(
    snapshot: DiscoverySnapshot,
    config: DiscoveryConfig,
) -> DiscoveryResult:
    snapshot_hash = snapshot.snapshot_hash()
    total_constituents = sum(
        item.total_constituents for item in snapshot.industries
    )
    historical_data_ready = sum(
        item.historical_data_ready for item in snapshot.industries
    )
    historical_data_missing = sum(
        item.historical_data_missing for item in snapshot.industries
    )
    coverage_ratio = (
        (
            Decimal(historical_data_ready) / Decimal(total_constituents)
        ).quantize(Decimal("0.000001"))
        if total_constituents
        else Decimal("0")
    )
    coverage_fields = {
        "total_constituents": total_constituents,
        "historical_data_ready": historical_data_ready,
        "historical_data_missing": historical_data_missing,
        "coverage_ratio": coverage_ratio,
    }
    if snapshot.market.quality_status not in _EXECUTABLE_QUALITY:
        return DiscoveryResult(
            status="BLOCKED",
            algorithm_id=config.algorithm_id,
            algorithm_version=config.algorithm_version,
            config_hash=config.config_hash(),
            input_snapshot_hash=snapshot_hash,
            market_state=snapshot.market.state,
            quality_status=snapshot.market.quality_status,
            blocked_reasons=snapshot.blocked_reasons
            or (f"MARKET_QUALITY_{snapshot.market.quality_status}",),
            **coverage_fields,
            industries=(),
            candidates=(),
        )

    scored_industries = []
    for item in snapshot.industries:
        if item.quality_status not in _EXECUTABLE_QUALITY:
            scored_industries.append((item, None))
        else:
            scored_industries.append((item, _industry_score(item, config)))
    scored_industries.sort(
        key=lambda row: (
            row[1] is None,
            -(row[1] or Decimal("0")),
            row[0].industry_key,
        )
    )
    selected_keys = {
        item.industry_key
        for item, score in scored_industries[: config.max_industries]
        if score is not None
    }
    industry_results = []
    executable_rank = 0
    for item, score in scored_industries:
        rank = None
        if score is not None:
            executable_rank += 1
            rank = executable_rank
        reasons = (
            ("QUALITY_BLOCKED",)
            if score is None
            else ("INDUSTRY_SELECTED",)
            if item.industry_key in selected_keys
            else ("INDUSTRY_BELOW_LIMIT",)
        )
        industry_results.append(
            CandidateIndustryResult(
                industry_key=item.industry_key,
                industry_name=item.industry_name,
                classification=item.classification,
                score=score,
                rank=rank,
                metrics={
                    "relative_strength_5d": item.relative_strength_5d,
                    "relative_strength_10d": item.relative_strength_10d,
                    "relative_strength_20d": item.relative_strength_20d,
                    "amount_share": item.amount_share,
                    "advance_ratio": item.advance_ratio,
                    "limit_up_count": item.limit_up_count,
                    "leader_strength": item.leader_strength,
                    "new_high_ratio": item.new_high_ratio,
                    "net_inflow_1d": item.net_inflow_1d,
                    "net_inflow_5d": item.net_inflow_5d,
                    "net_inflow_10d": item.net_inflow_10d,
                    "broken_limit_rate": item.broken_limit_rate,
                    "total_constituents": item.total_constituents,
                    "historical_data_ready": item.historical_data_ready,
                    "historical_data_missing": item.historical_data_missing,
                    "coverage_ratio": item.coverage_ratio,
                },
                reason_codes=reasons,
                evidence_references=item.evidence_references,
                quality_status=item.quality_status,
            )
        )

    industry_quality = _worst_quality(
        snapshot.market.quality_status,
        *(item.quality_status for item in snapshot.industries),
    )
    if selected_keys and any(
        item.quality_status not in _EXECUTABLE_QUALITY for item in snapshot.industries
    ):
        return DiscoveryResult(
            status="BLOCKED",
            algorithm_id=config.algorithm_id,
            algorithm_version=config.algorithm_version,
            config_hash=config.config_hash(),
            input_snapshot_hash=snapshot_hash,
            market_state=snapshot.market.state,
            quality_status=industry_quality,
            blocked_reasons=("INCOMPLETE_INDUSTRY_UNIVERSE",),
            **coverage_fields,
            industries=tuple(industry_results),
            candidates=(),
        )

    if not selected_keys:
        return DiscoveryResult(
            status="BLOCKED",
            algorithm_id=config.algorithm_id,
            algorithm_version=config.algorithm_version,
            config_hash=config.config_hash(),
            input_snapshot_hash=snapshot_hash,
            market_state=snapshot.market.state,
            quality_status=industry_quality,
            blocked_reasons=("NO_EXECUTABLE_INDUSTRY_DATA",),
            **coverage_fields,
            industries=tuple(industry_results),
            candidates=(),
        )

    selected_industries = [
        industry
        for industry, score in scored_industries
        if score is not None and industry.industry_key in selected_keys
    ]
    if any(
        industry.coverage_ratio < config.candidate_min_history_coverage_ratio
        for industry in selected_industries
    ):
        return DiscoveryResult(
            status="BLOCKED",
            algorithm_id=config.algorithm_id,
            algorithm_version=config.algorithm_version,
            config_hash=config.config_hash(),
            input_snapshot_hash=snapshot_hash,
            market_state=snapshot.market.state,
            quality_status=_worst_quality(industry_quality, "MISSING"),
            blocked_reasons=("HISTORICAL_UNIVERSE_COVERAGE_INSUFFICIENT",),
            **coverage_fields,
            industries=tuple(industry_results),
            candidates=(),
        )

    trusted_stock_inputs = [
        stock
        for industry, score in scored_industries
        if score is not None and industry.industry_key in selected_keys
        for stock in industry.constituents
        if stock.quality_status in _EXECUTABLE_QUALITY
        and len(stock.prices) >= config.minimum_history_rows
    ]
    if not trusted_stock_inputs:
        stock_quality = _worst_quality(
            industry_quality,
            *(
                stock.quality_status
                for industry, score in scored_industries
                if score is not None and industry.industry_key in selected_keys
                for stock in industry.constituents
            ),
        )
        return DiscoveryResult(
            status="BLOCKED",
            algorithm_id=config.algorithm_id,
            algorithm_version=config.algorithm_version,
            config_hash=config.config_hash(),
            input_snapshot_hash=snapshot_hash,
            market_state=snapshot.market.state,
            quality_status=stock_quality,
            blocked_reasons=("NO_EXECUTABLE_STOCK_DATA",),
            **coverage_fields,
            industries=tuple(industry_results),
            candidates=(),
        )

    high_risk = snapshot.market.state in {"CONTRACTION", "PANIC"}
    candidates = []
    score_by_industry = {
        item.industry_key: score
        for item, score in scored_industries
        if score is not None and item.industry_key in selected_keys
    }
    for industry, score in scored_industries:
        if industry.industry_key not in score_by_industry:
            continue
        industry_candidates = []
        for stock in sorted(industry.constituents, key=lambda row: row.symbol):
            evaluated = _candidate(
                stock,
                industry,
                score,
                config,
                high_risk_market=high_risk,
            )
            if evaluated is None:
                continue
            stock_score, metrics, reasons, risks, candidate_type = evaluated
            industry_candidates.append(
                (stock_score, stock, metrics, reasons, risks, candidate_type)
            )
        industry_candidates.sort(key=lambda row: (-row[0], row[1].symbol))
        candidates.extend(industry_candidates[: config.max_candidates_per_industry])
    candidates.sort(key=lambda row: (-row[0], row[1].symbol))
    candidate_results = []
    for rank, (score, stock, metrics, reasons, risks, candidate_type) in enumerate(
        candidates[: config.max_candidates], start=1
    ):
        candidate_snapshot_hash = canonical_hash(
            {
                "input_snapshot_hash": snapshot_hash,
                "config_hash": config.config_hash(),
                "stock": stock,
                "score": score,
                "reason_codes": reasons,
            }
        )
        candidate_results.append(
            CandidateResult(
                symbol=stock.symbol,
                name=stock.name,
                industry_key=stock.industry_key,
                industry_name=stock.industry_name,
                candidate_type=candidate_type,
                score=score,
                rank=rank,
                current_price=metrics["current_price"],
                technical_metrics={
                    key: value for key, value in metrics.items() if key != "current_price"
                },
                reason_codes=reasons,
                risk_flags=risks,
                evidence_references=tuple(
                    sorted(
                        set(
                            stock.evidence_references
                            + industry.evidence_references
                            + snapshot.market.evidence_references
                        )
                    )
                ),
                quality_status=_worst_quality(
                    snapshot.market.quality_status,
                    industry.quality_status,
                    stock.quality_status,
                ),
                snapshot_hash=candidate_snapshot_hash,
            )
        )
    return DiscoveryResult(
        status="COMPLETED",
        algorithm_id=config.algorithm_id,
        algorithm_version=config.algorithm_version,
        config_hash=config.config_hash(),
        input_snapshot_hash=snapshot_hash,
        market_state=snapshot.market.state,
        quality_status=industry_quality,
        blocked_reasons=(),
        **coverage_fields,
        industries=tuple(industry_results),
        candidates=tuple(candidate_results),
    )


__all__ = ["discover_candidates"]
