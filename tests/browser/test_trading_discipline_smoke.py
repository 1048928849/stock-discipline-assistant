from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page


BASE_URL = os.environ.get("BROWSER_SMOKE_BASE_URL", "http://127.0.0.1:8765")
ARTIFACTS = Path(os.environ.get("BROWSER_SMOKE_ARTIFACTS", "browser-smoke-artifacts"))


@pytest.mark.browser_smoke
@pytest.mark.parametrize("viewport", [(1440, 900), (390, 844)])
def test_trading_discipline_browser_smoke(page: Page, viewport: tuple[int, int]):
    width, height = viewport
    page.set_viewport_size({"width": width, "height": height})
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    dashboard = {
        "program": {"id": 1, "status": "ACTIVE", "valid_samples": 4, "target_samples": 20},
        "progress": {"valid": 4, "target": 20},
        "average_discipline_score": 80,
        "planned_trade_ratio": 0.75,
        "unplanned_trade_ratio": 0.25,
        "chase_risk_trade_ratio": 0.25,
        "stop_compliance_ratio": 1,
        "position_limit_breaches": 0,
        "thesis_change_count": 0,
        "post_position_new_reason_count": 0,
        "losing_position_add_attempts": 1,
        "information_tier_distribution": {"FACT": 2, "ANALYSIS": 1, "SENTIMENT": 1},
        "recurring_mistakes": [{"code": "CHASE_ENTRY", "count": 1}],
        "recent_trades": [
            {
                "compliance_result": "PROFITABLE_UNDISCIPLINED",
                "pnl_pct": "20",
                "error_codes": ["CHASE_ENTRY"],
            },
            {"compliance_result": "LOSING_COMPLIANT", "pnl_pct": "-5", "error_codes": []},
        ],
        "playbook_quality_conclusion": None,
    }
    pretrade = {
        "id": 1,
        "status": "BLOCK",
        "action": "BUY",
        "score": 80,
        "category_scores": {
            "PREDEFINED_PLAYBOOK": 20,
            "PREEXISTING_ENTRY_CONDITION": 20,
            "PREDEFINED_INVALIDATION": 20,
            "PREDEFINED_POSITION": 20,
            "RULE_STABILITY": 0,
        },
        "reason_codes": ["ENTRY_AFTER_CLIMAX"],
        "executable": False,
        "formal_authority": "DISCIPLINE_ONLY",
        "snapshot_hash": "a" * 64,
        "decision_meaning": "DISCIPLINE_CONSTRAINT_ACTIVE",
        "rules": [
            {
                "rule_code": "CHASE_RISK",
                "status": "BLOCK",
                "reason_code": "ENTRY_AFTER_CLIMAX",
                "effect_on_action": "BLOCK_EXPANSION",
            }
        ],
    }
    page.route(
        "**/api/v1/trading-discipline/dashboard?**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(dashboard)
        ),
    )
    page.route(
        "**/api/v1/trading-discipline/pretrade-check",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(pretrade)
        ),
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        response = page.goto(f"{BASE_URL}/trading-discipline", wait_until="networkidle")
        assert response is not None and response.status == 200
        page.get_by_text("4 / 20", exact=True).wait_for()
        assert page.get_by_text("PROFITABLE_UNDISCIPLINED", exact=True).is_visible()
        assert page.get_by_text("LOSING_COMPLIANT", exact=True).is_visible()
        form = page.locator("#discipline-pretrade-form")
        form.locator("input[name=symbol]").fill("300308")
        form.locator("input[name=current_price]").fill("12")
        form.locator("input[name=entry_low]").fill("10.42")
        form.locator("input[name=entry_high]").fill("10.54")
        form.locator("input[name=hard_stop]").fill("9.70")
        form.locator("input[name=proposed_quantity]").fill("100")
        form.locator("input[name=max_quantity]").fill("600")
        form.locator("button").click()
        page.get_by_text("BLOCKED_BY_DISCIPLINE / NOT_EVALUATED", exact=True).wait_for()
        assert (
            page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth"
            )
            is False
        )
        assert not console_errors
        assert not page_errors
    except Exception:
        stem = f"trading-discipline-{width}x{height}"
        page.screenshot(path=str(ARTIFACTS / f"{stem}.png"), full_page=True)
        (ARTIFACTS / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        raise
