from __future__ import annotations

from typing import Protocol

from app.domain.preview.models import PreviewSnapshot


class PreviewSnapshotRepository(Protocol):
    def save_preview_snapshot(self, account_id: int, preview: dict) -> PreviewSnapshot: ...

    def get_preview_snapshot(
        self, account_id: int, symbol: str, preview_hash: str
    ) -> PreviewSnapshot | None: ...
