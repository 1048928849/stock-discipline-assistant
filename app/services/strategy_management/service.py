from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.domain.strategy_management import (
    StrategyDefinition,
    StrategyLifecycle,
    StrategyVersion,
)
from app.errors import AppError
from app.models import StrategyVersionRecord, TradePlan
from app.services.strategy_management.lifecycle import require_transition
from app.services.strategy_management.repository import SqlAlchemyStrategyRepository
from app.services.trade_plan.compatibility import GENERATOR_PARAMETERS
from app.services.transaction import transaction_scope

PLATFORM_BREAKOUT_STRATEGY_ID = "platform_breakout_pullback"
PLATFORM_BREAKOUT_VERSION = "1.0.0"
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

PLATFORM_BREAKOUT_RULE_SNAPSHOT = {
    "engine": "PlatformBreakoutPullbackStrategy",
    "required_rules": [
        "platform_data_sufficiency",
        "large_cycle_direction",
        "platform_structure",
        "breakout_volume_confirmation",
        "pullback_structure",
        "turn_stronger_confirmation",
    ],
}


class StrategyManagementService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = SqlAlchemyStrategyRepository(db)

    @staticmethod
    def _validate_version(version: str) -> None:
        if not _SEMVER.fullmatch(version):
            raise AppError(422, "STRATEGY_VERSION_INVALID", "策略版本必须使用三段式语义版本")

    def create_strategy(
        self,
        *,
        strategy_id: str,
        name: str,
        description: str,
        category: str,
        owner: str,
    ) -> StrategyDefinition:
        with transaction_scope(self.db):
            if self.repository.strategy_record(strategy_id):
                raise AppError(409, "STRATEGY_EXISTS", "策略已存在")
            record = self.repository.add_strategy(
                strategy_id=strategy_id,
                name=name,
                description=description,
                category=category,
                owner=owner,
            )
            self.repository.add_event(
                strategy_id=strategy_id,
                strategy_version_id=None,
                from_status=None,
                to_status=StrategyLifecycle.DRAFT,
                event_type="strategy_created",
            )
            result = self.repository._definition(record)
        return result

    def create_version(
        self,
        *,
        strategy_id: str,
        version: str,
        rule_snapshot: Mapping[str, Any],
        parameter_snapshot: Mapping[str, Any],
    ) -> StrategyVersion:
        self._validate_version(version)
        with transaction_scope(self.db):
            if self.repository.strategy_record(strategy_id) is None:
                raise AppError(404, "STRATEGY_NOT_FOUND", "策略不存在")
            if self.repository.version_record(strategy_id, version):
                raise AppError(409, "STRATEGY_VERSION_EXISTS", "策略版本已存在")
            record = self.repository.add_version(
                strategy_id=strategy_id,
                version=version,
                rule_snapshot=rule_snapshot,
                parameter_snapshot=parameter_snapshot,
            )
            self.repository.add_event(
                strategy_id=strategy_id,
                strategy_version_id=record.id,
                from_status=None,
                to_status=StrategyLifecycle.DRAFT,
                event_type="version_created",
            )
            result = self.repository._version(record)
        return result

    def create_new_version(
        self, strategy_id: str, source_version: str, new_version: str
    ) -> StrategyVersion:
        self._validate_version(new_version)
        with transaction_scope(self.db):
            source = self.repository.get_version(strategy_id, source_version)
            if source is None:
                raise AppError(404, "STRATEGY_VERSION_NOT_FOUND", "源策略版本不存在")
            if self.repository.version_record(strategy_id, new_version):
                raise AppError(409, "STRATEGY_VERSION_EXISTS", "策略版本已存在")
            record = self.repository.add_version(
                strategy_id=strategy_id,
                version=new_version,
                rule_snapshot=source.rule_snapshot,
                parameter_snapshot=source.parameter_snapshot,
            )
            self.repository.add_event(
                strategy_id=strategy_id,
                strategy_version_id=record.id,
                from_status=None,
                to_status=StrategyLifecycle.DRAFT,
                event_type="version_created_from_existing",
                reason=f"copied from {source_version}",
            )
            result = self.repository._version(record)
        return result

    def update_version_snapshots(
        self,
        strategy_id: str,
        version: str,
        *,
        rule_snapshot: Mapping[str, Any],
        parameter_snapshot: Mapping[str, Any],
    ) -> StrategyVersion:
        with transaction_scope(self.db):
            record = self._required_version_record(strategy_id, version)
            if record.activated_at is not None:
                raise AppError(
                    409,
                    "ACTIVE_STRATEGY_VERSION_IMMUTABLE",
                    "已激活过的策略版本不可修改，请创建新版本",
                )
            record.rule_snapshot = dict(rule_snapshot)
            record.parameter_snapshot = dict(parameter_snapshot)
            self.db.flush()
            result = self.repository._version(record)
        return result

    def transition(
        self,
        strategy_id: str,
        version: str,
        target: StrategyLifecycle,
        *,
        reason: str | None = None,
    ) -> StrategyVersion:
        with transaction_scope(self.db):
            record = self._required_version_record(strategy_id, version)
            current = StrategyLifecycle(record.status)
            try:
                require_transition(current, target)
            except ValueError as exc:
                raise AppError(409, "STRATEGY_LIFECYCLE_INVALID", str(exc)) from exc
            now = datetime.now()  # noqa: DTZ005 - database stores local naive time
            record.status = target.value
            if target is StrategyLifecycle.ACTIVE:
                record.activated_at = now
            if target is StrategyLifecycle.RETIRED:
                record.retired_at = now
            strategy = self.repository.strategy_record(strategy_id)
            if strategy is not None:
                strategy.status = target.value
            self.repository.add_event(
                strategy_id=strategy_id,
                strategy_version_id=record.id,
                from_status=current,
                to_status=target,
                event_type="lifecycle_transition",
                reason=reason,
            )
            result = self.repository._version(record)
        return result

    def activate(self, strategy_id: str, version: str) -> StrategyVersion:
        return self.transition(strategy_id, version, StrategyLifecycle.ACTIVE)

    def suspend(self, strategy_id: str, version: str) -> StrategyVersion:
        return self.transition(strategy_id, version, StrategyLifecycle.SUSPENDED)

    def retire(self, strategy_id: str, version: str) -> StrategyVersion:
        return self.transition(strategy_id, version, StrategyLifecycle.RETIRED)

    def list_versions(self, strategy_id: str) -> list[StrategyVersion]:
        return self.repository.list_versions(strategy_id)

    def ensure_platform_breakout(self) -> StrategyVersionRecord:
        with transaction_scope(self.db):
            strategy = self.repository.strategy_record(PLATFORM_BREAKOUT_STRATEGY_ID)
            if strategy is None:
                strategy = self.repository.add_strategy(
                    strategy_id=PLATFORM_BREAKOUT_STRATEGY_ID,
                    name="平台突破-回踩确认",
                    description="日线平台放量突破、缩量回踩并再次转强的纪律策略。",
                    category="趋势波段",
                    owner="system",
                    status=StrategyLifecycle.ACTIVE,
                )
            version = self.repository.version_record(
                PLATFORM_BREAKOUT_STRATEGY_ID, PLATFORM_BREAKOUT_VERSION
            )
            if version is None:
                version = self.repository.add_version(
                    strategy_id=PLATFORM_BREAKOUT_STRATEGY_ID,
                    version=PLATFORM_BREAKOUT_VERSION,
                    rule_snapshot=PLATFORM_BREAKOUT_RULE_SNAPSHOT,
                    parameter_snapshot=GENERATOR_PARAMETERS,
                    status=StrategyLifecycle.ACTIVE,
                )
                version.activated_at = datetime.now()  # noqa: DTZ005
                self.repository.add_event(
                    strategy_id=PLATFORM_BREAKOUT_STRATEGY_ID,
                    strategy_version_id=version.id,
                    from_status=None,
                    to_status=StrategyLifecycle.ACTIVE,
                    event_type="existing_strategy_registered",
                )
            self.db.execute(
                update(TradePlan)
                .where(TradePlan.strategy_id.is_(None))
                .values(
                    strategy_id=PLATFORM_BREAKOUT_STRATEGY_ID,
                    strategy_version_id=version.id,
                )
            )
            result = version
        return result

    def _required_version_record(
        self, strategy_id: str, version: str
    ) -> StrategyVersionRecord:
        record = self.repository.version_record(strategy_id, version)
        if record is None:
            raise AppError(404, "STRATEGY_VERSION_NOT_FOUND", "策略版本不存在")
        return record
