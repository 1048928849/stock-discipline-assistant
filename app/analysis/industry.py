from __future__ import annotations

from decimal import Decimal

from app.analysis.contracts import (
    IndustryAnalysisInput,
    IndustryAssessment,
    IndustryContext,
)


def _sum(values, count: int) -> Decimal | None:
    rows = [item for item in values[-count:] if item is not None]
    return sum(rows, Decimal("0")) if rows else None


def _relative(changes, benchmark, count: int) -> Decimal | None:
    own = _sum(changes, count)
    base = _sum(benchmark, count)
    return own - base if own is not None and base is not None else None


def _persistence(changes) -> int:
    count = 0
    for value in reversed(changes):
        if value is None or value <= 0:
            break
        count += 1
    return count


def _max_drawdown(changes) -> Decimal | None:
    values = [item for item in changes if item is not None]
    if not values:
        return None
    level = Decimal("100")
    peak = level
    maximum = Decimal("0")
    for change in values:
        level *= Decimal("1") + change / Decimal("100")
        peak = max(peak, level)
        maximum = max(maximum, (peak - level) / peak)
    return maximum


def _assessment(series, benchmark) -> IndustryAssessment:
    observations = sorted(series.observations, key=lambda item: item.trade_date)
    changes = [item.change_pct for item in observations]
    latest = observations[-1] if observations else None
    rs5 = _relative(changes, benchmark, 5)
    rs10 = _relative(changes, benchmark, 10)
    rs20 = _relative(changes, benchmark, 20)
    persistence = _persistence(changes)
    width = latest.advance_ratio if latest else None
    leader = latest.leader_strength if latest else None
    crowding = bool(
        latest
        and (
            (latest.amount_share is not None and latest.amount_share >= Decimal("0.12"))
            or (
                leader is not None
                and leader >= Decimal("9.5")
                and width is not None
                and width < Decimal("0.45")
            )
        )
    )
    supporting = []
    conflicting = []
    if rs20 is not None and rs20 >= Decimal("5"):
        supporting.append("20-day relative strength")
    if persistence >= 5:
        supporting.append("persistent positive sessions")
    if width is not None and width >= Decimal("0.55"):
        supporting.append("broad constituent participation")
    if crowding:
        conflicting.append("crowding or narrow leader risk")
    if rs5 is not None and rs5 < 0:
        classification, phase = "FADING", "FADING"
    elif crowding and width is not None and width < Decimal("0.45"):
        classification, phase = "DIVERGENCE", "HIGH_DIVERGENCE"
    elif (
        rs20 is not None
        and rs20 >= Decimal("5")
        and persistence >= 5
        and width is not None
        and width >= Decimal("0.55")
    ):
        classification = "MAINLINE"
        phase = "ACCELERATION" if rs5 is not None and rs5 >= Decimal("5") else "PERSISTENT"
    elif rs10 is not None and rs10 >= Decimal("2") and persistence >= 3:
        classification, phase = "SECONDARY", "ROTATION"
    elif latest and latest.change_pct is not None and latest.change_pct >= Decimal("2"):
        classification, phase = "ROTATION", "PULSE"
    else:
        classification, phase = "NONE", "NONE"
    return IndustryAssessment(
        name=series.name,
        classification=classification,
        phase=phase,
        daily_change=latest.change_pct if latest else None,
        relative_strength_5d=rs5,
        relative_strength_10d=rs10,
        relative_strength_20d=rs20,
        amount=latest.amount if latest else None,
        amount_share=latest.amount_share if latest else None,
        advance_ratio=width,
        limit_up_count=latest.limit_up_count if latest else None,
        leader_strength=leader,
        persistence_days=persistence,
        max_drawdown=_max_drawdown(changes),
        new_high_ratio=latest.new_high_ratio if latest else None,
        crowding_risk=crowding,
        supporting_indicators=tuple(supporting),
        conflicting_indicators=tuple(conflicting),
    )


def analyze_industry_mainlines(value: IndustryAnalysisInput) -> IndustryContext:
    assessments = tuple(
        sorted(
            (_assessment(item, value.benchmark_changes) for item in value.industries),
            key=lambda item: item.name,
        )
    )
    return IndustryContext(
        industries=assessments,
        mainlines=tuple(item.name for item in assessments if item.classification == "MAINLINE"),
        secondary=tuple(
            item.name for item in assessments if item.classification == "SECONDARY"
        ),
        fading_industries=tuple(
            item.name for item in assessments if item.classification == "FADING"
        ),
    )


__all__ = ["analyze_industry_mainlines"]
