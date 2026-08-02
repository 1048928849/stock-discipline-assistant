from __future__ import annotations

from sqlalchemy.orm import Session

from app.errors import AppError
from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest


def recalculate_legacy_preview(db: Session, request: TradePlanSaveRequest) -> dict:
    """Compatibility path for previews created before server-side snapshots existed."""
    from app.services.trade_plan.application import generate_trade_plan

    preview_request = TradePlanPreviewRequest(
        **request.model_dump(exclude={"preview_hash", "ai_analysis_id"})
    )
    preview = generate_trade_plan(db, preview_request)
    if preview["preview_hash"] != request.preview_hash:
        raise AppError(409, "PREVIEW_CHANGED", "数据或规则已变化，请重新生成预览后再确认保存")
    return preview
