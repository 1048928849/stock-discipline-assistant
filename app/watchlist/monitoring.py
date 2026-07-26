from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.composition.data_hub import build_data_hub
from app.config import Settings, get_settings
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    TradingCalendar,
    TradingPhase,
    get_trading_calendar,
    market_storage_naive_to_aware,
    to_utc_storage_naive,
)
from app.domain.quality import DataQualityStatus
from app.models import WatchlistItem
from app.services.market_cache import persist_market_quote, resolve_cached_quote
from app.watchlist.contracts import (
    EventSeverity,
    MonitoringHealth,
    MonitoringRuleType,
    PriceStateInput,
    WatchlistScanSummary,
    WatchlistStatus,
)
from app.watchlist.events import create_event_once, create_reanalysis_request_once
from app.watchlist.lease import acquire_monitor_lease, release_monitor_lease
from app.watchlist.reanalysis import execute_reanalysis
from app.watchlist.service import transition_item
from app.watchlist.state_machine import determine_price_transition


_QUALITY_HEALTH = {
    DataQualityStatus.STALE: MonitoringHealth.STALE,
    DataQualityStatus.CONFLICTED: MonitoringHealth.CONFLICTED,
    DataQualityStatus.MISSING: MonitoringHealth.DATA_BLOCKED,
}


class WatchlistMonitoringService:
    def __init__(
        self,
        db: Session,
        *,
        router: DataHubRouter | None = None,
        calendar: TradingCalendar | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.calendar = calendar or get_trading_calendar()
        self.router = router

    def scan(
        self,
        *,
        now: datetime,
        item_ids: list[int] | None = None,
    ) -> WatchlistScanSummary:
        if now.tzinfo is None:
            raise ValueError("watchlist scan now must be timezone-aware")
        owner = uuid4().hex
        if not acquire_monitor_lease(
            self.db,
            owner_token=owner,
            lease_seconds=self.settings.watchlist_monitor_lease_seconds,
            now=now,
        ):
            return WatchlistScanSummary(lease_acquired=False)
        summary = WatchlistScanSummary()
        try:
            query = select(WatchlistItem).where(
                WatchlistItem.monitoring_enabled.is_(True),
                WatchlistItem.status.notin_(
                    [WatchlistStatus.ARCHIVED.value, WatchlistStatus.INVALIDATED.value]
                ),
            )
            if item_ids is not None:
                query = query.where(WatchlistItem.id.in_(item_ids))
            items = self.db.scalars(
                query.order_by(WatchlistItem.id).limit(
                    self.settings.watchlist_monitor_batch_size
                )
            ).all()
            if self.calendar.market_phase(now) not in {
                TradingPhase.MORNING_SESSION,
                TradingPhase.AFTERNOON_SESSION,
            }:
                summary.scanned = len(items)
                summary.unchanged = len(items)
                return summary
            if self.router is None:
                self.router = build_data_hub(
                    self.db,
                    calendar=self.calendar,
                    now_fn=lambda: now,
                )
            for item in items:
                summary.scanned += 1
                try:
                    self._scan_item(item, now=now, summary=summary)
                except Exception as exc:
                    self.db.rollback()
                    summary.failures.append(
                        {
                            "item_id": item.id,
                            "symbol": item.symbol,
                            "error": f"{type(exc).__name__}: {exc}"[:1000],
                        }
                    )
            return summary
        finally:
            release_monitor_lease(self.db, owner_token=owner)

    def _scan_item(
        self,
        item: WatchlistItem,
        *,
        now: datetime,
        summary: WatchlistScanSummary,
    ) -> None:
        assert self.router is not None
        try:
            result = self.router.get_quote(item.symbol)
            self.db.commit()
            try:
                result.require_trusted_value()
            except ProviderUnavailableError:
                pass
            else:
                persist_market_quote(self.db, self.router, result)
                self.db.commit()
            selection = resolve_cached_quote(
                self.db,
                symbol=item.symbol,
                capability="market.quote.realtime",
                evaluated_at=now,
            )
            quality = selection.effective_quality.effective_quality
            if (
                not selection.executable
                and result.quality_status
                in {
                    DataQualityStatus.CONFLICTED,
                    DataQualityStatus.STALE,
                    DataQualityStatus.MISSING,
                }
            ):
                quality = result.quality_status
            health = (
                MonitoringHealth.HEALTHY
                if selection.executable and selection.value is not None
                else _QUALITY_HEALTH.get(quality, MonitoringHealth.DATA_BLOCKED)
            )
        except ProviderUnavailableError as exc:
            self.db.rollback()
            self._record_blocked(
                item,
                now=now,
                health=MonitoringHealth.PROVIDER_UNAVAILABLE,
                quality=None,
                reason=str(exc),
                summary=summary,
            )
            return

        if health != MonitoringHealth.HEALTHY or selection.value is None:
            self._record_blocked(
                item,
                now=now,
                health=health,
                quality=quality,
                reason=selection.blocking_reason or quality.value,
                summary=summary,
            )
            return

        quote = selection.value
        quote_observed_at = market_storage_naive_to_aware(quote.observed_at)
        item.monitoring_health = MonitoringHealth.HEALTHY.value
        item.data_quality = quality.value
        item.current_price = quote.price
        item.current_price_observed_at = quote.observed_at
        item.last_scanned_at = to_utc_storage_naive(now)
        item.next_scan_at = to_utc_storage_naive(
            now + timedelta(seconds=self.settings.watchlist_monitor_interval_seconds)
        )
        outcome = determine_price_transition(
            PriceStateInput(
                current_status=WatchlistStatus(item.status),
                current_price=quote.price,
                entry_low=item.entry_low,
                entry_high=item.entry_high,
                hard_stop=item.hard_stop,
                near_entry_distance_pct=self.settings.watchlist_near_entry_distance_pct,
                observed_at=quote_observed_at,
                monitoring_health=MonitoringHealth.HEALTHY,
            )
        )
        transitioned = outcome.to_status != outcome.from_status
        evidence = (
            [f"quality_record:{selection.quality_record_id}"]
            if selection.quality_record_id
            else []
        )
        if transitioned:
            transition_item(
                self.db,
                item,
                outcome.to_status,
                reason_codes=list(outcome.reason_codes),
                evidence_references=evidence,
                observed_at=quote_observed_at,
            )
            summary.transitioned += 1
        else:
            summary.unchanged += 1
        request = None
        if outcome.rule_type and transitioned:
            _, created = create_event_once(
                self.db,
                item,
                event_type=outcome.rule_type,
                severity=outcome.severity,
                title=self._event_title(outcome.rule_type, item.symbol),
                reason_codes=list(outcome.reason_codes),
                observed_at=quote_observed_at,
                payload={
                    "price": str(quote.price),
                    "quality_record_id": selection.quality_record_id,
                    "from_status": outcome.from_status.value,
                    "to_status": outcome.to_status.value,
                },
                reanalysis_required=outcome.trigger_reanalysis,
                cooldown_seconds=self.settings.watchlist_monitor_interval_seconds,
            )
            summary.events_created += int(created)
        if outcome.trigger_reanalysis:
            request, created = create_reanalysis_request_once(
                self.db,
                item,
                reason_codes=list(outcome.reason_codes),
                observed_at=quote_observed_at,
                cooldown_seconds=self.settings.watchlist_monitor_interval_seconds,
            )
            summary.reanalysis_requested += int(created)
        self.db.commit()
        if request is not None and request.status == "PENDING":
            reanalysis = execute_reanalysis(self.db, request.id)
            if reanalysis.status == "BLOCKED":
                summary.blocked += 1
            elif reanalysis.status == "FAILED":
                summary.failures.append(
                    {
                        "item_id": item.id,
                        "symbol": item.symbol,
                        "error": (
                            f"reanalysis failed: {reanalysis.error_code}: "
                            f"{reanalysis.error_message}"
                        )[:1000],
                    }
                )

    def _record_blocked(
        self,
        item: WatchlistItem,
        *,
        now: datetime,
        health: MonitoringHealth,
        quality: DataQualityStatus | None,
        reason: str,
        summary: WatchlistScanSummary,
    ) -> None:
        item.monitoring_health = health.value
        item.data_quality = quality.value if quality else None
        item.last_scanned_at = to_utc_storage_naive(now)
        item.next_scan_at = to_utc_storage_naive(
            now + timedelta(seconds=self.settings.watchlist_monitor_interval_seconds)
        )
        _, created = create_event_once(
            self.db,
            item,
            event_type=MonitoringRuleType.DATA_QUALITY_DEGRADED,
            severity=EventSeverity.ATTENTION,
            title=f"{item.symbol} 监控数据不可执行",
            reason_codes=[health.value],
            observed_at=now,
            payload={"quality_status": quality.value if quality else None, "reason": reason},
            reanalysis_required=False,
            cooldown_seconds=self.settings.watchlist_monitor_interval_seconds,
        )
        summary.events_created += int(created)
        summary.blocked += 1
        summary.unchanged += 1
        self.db.commit()

    @staticmethod
    def _event_title(rule: MonitoringRuleType, symbol: str) -> str:
        return {
            MonitoringRuleType.PRICE_NEAR_ENTRY_ZONE: f"{symbol} 接近买入区",
            MonitoringRuleType.PRICE_ENTER_ENTRY_ZONE: f"{symbol} 进入买入区",
            MonitoringRuleType.PRICE_BREAK_HARD_STOP: f"{symbol} 跌破硬止损",
            MonitoringRuleType.PLAN_INVALIDATION_TRIGGERED: f"{symbol} 观察逻辑失效",
        }.get(rule, f"{symbol} 监控条件变化")


__all__ = ["WatchlistMonitoringService"]
