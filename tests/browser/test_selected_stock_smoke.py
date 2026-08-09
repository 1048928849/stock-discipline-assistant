from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page


BASE_URL = os.environ.get("BROWSER_SMOKE_BASE_URL", "http://127.0.0.1:8765")
ARTIFACTS = Path(os.environ.get("BROWSER_SMOKE_ARTIFACTS", "browser-smoke-artifacts"))


def _analysis(*, available: bool) -> dict:
    industry_status = "AVAILABLE" if available else "INDUSTRY_CONTEXT_UNAVAILABLE"
    return {
        "analysis_run_id": 1,
        "strategy_id": "cycle_structure_validation_v2",
        "strategy_version": "2.0.0",
        "strategy_mode": "CSV_V2_ADVISORY",
        "stock_code": "300308",
        "analysis_date": "2026-07-31",
        "generated_at": "2026-07-31T16:00:00+08:00",
        "data_status": "FRESH",
        "market_context_status": "AVAILABLE",
        "industry_context_status": industry_status,
        "industry_name": "通信设备" if available else None,
        "industry_context": {
            "status": industry_status,
            "mapping": {"industry_name": "通信设备", "provider": "akshare"}
            if available
            else None,
            "history_row_count": 250 if available else 0,
            "constituent_count": 20 if available else 0,
            "valid_member_count": 18 if available else 0,
            "coverage_ratio": 0.9 if available else 0,
            "role": "TREND_CORE" if available else "UNKNOWN",
            "role_evidence": {
                "reason_code": "ROLE_EVIDENCE_COMPLETE"
                if available
                else "EXTERNAL_INDUSTRY_DATA_BLOCKED",
                "return_rank_20": 2 if available else None,
                "return_rank_60": 3 if available else None,
                "amount_rank": 4 if available else None,
                "amount_percentile": 0.85 if available else None,
                "excess_return_20": 0.08 if available else None,
                "excess_return_60": 0.12 if available else None,
                "consecutive_leading_days": 4 if available else 0,
            },
        },
        "cycle_state": "START_CONFIRMED",
        "stock_role": "TREND_CORE" if available else "UNKNOWN",
        "trade_mode": "CORE_TREND_PULLBACK",
        "plan_status": "NO_TRADE",
        "scores": {"total": 62, "grade": "C"},
        "price_plan": {
            "support_zone_low": 10.1,
            "support_zone_high": 10.3,
            "entry_zone_low": 10.4209,
            "entry_zone_high": 10.5391,
            "stop_loss": 9.7023,
            "invalidation_price": 9.7023,
            "first_take_profit": 11.2,
            "second_take_profit": 12.0,
            "risk_reward_ratio": 2.1,
        },
        "position_plan": {
            "initial_position_pct": 10,
            "max_position_pct": 20,
            "quantity": 100,
            "max_quantity": 600,
            "risk_amount": 77.77,
            "maximum_loss_after_trade": 77.77,
            "post_trade_position_pct": 10,
            "t1_overnight_gap_risk_pct": 5,
            "t1_risk_amount": 500,
        },
        "passed_conditions": ["READY"],
        "failed_conditions": ["PRICE_CONFLICT"],
        "pending_conditions": ["WAIT_FOR_INDUSTRY"],
        "execution_blockers": ["SURVIVAL_BLOCK"],
        "price_observation": {
            "source": "USER_OBSERVATION",
            "trust_status": "USER_PRICE_CONFLICTED",
            "observed_at": "2026-07-31T15:00:00+08:00",
            "executable_for_entry": False,
            "executable_for_position": False,
            "reason_code": "USER_PRICE_CONFLICTED",
        },
        "account_context": {
            "source": "ACCOUNT_SNAPSHOT",
            "trust_status": "SERVER_ACCOUNT_AUTHORITY",
            "account_id": 1,
            "daily_loss_amount": 100,
            "daily_loss_pct": 1,
            "observed_at": "2026-07-31T15:00:00+08:00",
            "conflict_fields": [],
        },
        "survival_discipline": [
            {
                "rule_code": "T1_OVERNIGHT_RISK",
                "status": "BLOCK",
                "reason_code": "T1_RISK_BLOCKED",
                "action": "BLOCK_NEW_ACTION",
                "evidence": {"gap_risk_pct": 5},
            }
        ],
        "hard_gates": [
            {"code": "ready", "status": "PASS", "reason_code": "READY", "evidence": []},
            {
                "code": "survival",
                "status": "BLOCKED",
                "reason_code": "SURVIVAL_BLOCK",
                "evidence": [],
            },
            {
                "code": "industry",
                "status": "NOT_EVALUATED",
                "reason_code": "EXTERNAL_INDUSTRY_DATA_BLOCKED",
                "evidence": [],
            },
        ],
        "quality_bindings": [],
        "source_lineage": [],
        "product_v1_comparison": {
            "conflict_status": "MORE_CONSERVATIVE",
            "formal_execution_owner": "PRODUCT_V1",
            "product_v1_status": "READY",
            "csv_v2_status": "NO_TRADE",
        },
    }


@pytest.mark.browser_smoke
@pytest.mark.parametrize("viewport", [(1440, 900), (390, 844)])
def test_selected_stock_browser_smoke(page: Page, viewport: tuple[int, int]) -> None:
    width, height = viewport
    page.set_viewport_size({"width": width, "height": height})
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    response_count = 0

    def analysis_route(route):
        nonlocal response_count
        response_count += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_analysis(available=response_count == 1)),
        )

    page.route("**/api/v1/selected-stock-analysis", analysis_route)
    page.route(
        "**/api/v1/selected-stock-analysis/runs?**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                [
                    {
                        "run_id": 1,
                        "analysis_date": "2026-07-31",
                        "cycle_state": "START_CONFIRMED",
                        "stock_role": "TREND_CORE",
                        "plan_status": "NO_TRADE",
                        "data_status": "FRESH",
                        "industry_status": "AVAILABLE",
                    },
                    {
                        "run_id": 2,
                        "analysis_date": "2026-07-30",
                        "cycle_state": "PROBE",
                        "stock_role": "UNKNOWN",
                        "plan_status": "INSUFFICIENT_DATA",
                        "data_status": "INSUFFICIENT_DATA",
                        "industry_status": "INDUSTRY_CONTEXT_UNAVAILABLE",
                    },
                ]
            ),
        ),
    )
    page.route(
        "**/api/v1/selected-stock-analysis/runs/*/compare/*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"changes": {"plan_status": {"before": "NO_TRADE", "after": "WAIT_FOR_TRIGGER"}}}),
        ),
    )

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        response = page.goto(f"{BASE_URL}/selected-stock-analysis", wait_until="networkidle")
        assert response is not None and response.status == 200
        assert page.locator("#selected-stock-form").is_visible()
        page.locator("#selected-stock-form input[name=stock_code]").fill("300308")
        page.locator("#selected-stock-form button[type=submit]").click()
        page.locator("#selected-stock-result").get_by_text(
            "AVAILABLE", exact=True
        ).first.wait_for()
        content = page.locator("#selected-stock-result").inner_text()
        for expected in (
            "PASS",
            "BLOCKED",
            "NOT_EVALUATED",
            "USER_PRICE_CONFLICTED",
            "T1_RISK_BLOCKED",
            "SURVIVAL_BLOCK",
        ):
            assert expected in content
        assert page.locator("details.selected-raw").get_attribute("open") is None
        page.locator("#selected-stock-compare").click()
        page.get_by_text("结构化变化").wait_for()
        page.locator("#selected-stock-form button[type=submit]").click()
        page.locator("#selected-stock-result").get_by_text(
            "INDUSTRY_CONTEXT_UNAVAILABLE", exact=True
        ).first.wait_for()
        assert "正在获取并验证" not in page.locator("#selected-stock-result").inner_text()
        overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        assert overflow is False
        assert not page_errors
        assert not console_errors
    except Exception:
        stem = f"selected-stock-{width}x{height}"
        page.screenshot(path=str(ARTIFACTS / f"{stem}.png"), full_page=True)
        (ARTIFACTS / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        (ARTIFACTS / f"{stem}-console.log").write_text(
            "\n".join(console_errors + page_errors), encoding="utf-8"
        )
        raise
