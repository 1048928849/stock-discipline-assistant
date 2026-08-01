from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.domain.preview import RuleVersionSnapshot
from app.models import RuleVersion
from app.services.trade_plan.compatibility import GENERATOR_PARAMETERS, GENERATOR_RULES
from app.services.workflow import ensure_default_rule_version


class RuleVersionManager:
    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_active_version(self) -> RuleVersion:
        current = ensure_default_rule_version(self.db, commit=False)
        if all(key in current.parameters for key in GENERATOR_PARAMETERS):
            return current
        current.active = False
        version = RuleVersion(
            rule_set_id=current.rule_set_id,
            version="1.2.0" if "platform_min_days" in current.parameters else "1.1.0",
            parameters={**current.parameters, **GENERATOR_PARAMETERS},
            rules={**current.rules, **GENERATOR_RULES},
            change_note="集中一键计划的账户、风险、分批仓位和市场降风险参数；旧计划保持原规则版本。",
            effective_from=date.today(),  # noqa: DTZ011 - preserves existing rule-version behavior
            active=True,
        )
        self.db.add(version)
        self.db.flush()
        return version

    def create_snapshot_version(self, version: RuleVersion) -> RuleVersionSnapshot:
        return RuleVersionSnapshot(
            id=version.id,
            version=version.version,
            effective_from=version.effective_from,
            active=version.active,
            parameters=version.parameters,
            rules=version.rules,
        )
