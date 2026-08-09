from datetime import datetime, timedelta

from app.models import Account, Trade, TradingPlaybook


def seed(session):
    account = Account(name="纪律训练账户", total_assets=100000, cash=80000, available_cash=80000)
    playbook = TradingPlaybook(
        code="CORE_STATE_CHANGE_V1",
        version="1.0.0",
        name="核心状态变化训练法",
        description="状态变化→二次确认→第一次有效分歧",
        active=True,
        rules={},
    )
    session.add_all([account, playbook])
    session.commit()
    return account, playbook


def context(account_id, **updates):
    now = datetime(2026, 8, 9, 9, 30)
    payload = {
        "account_id": account_id,
        "symbol": "300308",
        "action": "BUY",
        "decision_at": now.isoformat(),
        "playbook_code": "CORE_STATE_CHANGE_V1",
        "playbook_selected_at": (now - timedelta(days=1)).isoformat(),
        "entry_evidence_at": (now - timedelta(hours=1)).isoformat(),
        "invalidation_defined_at": (now - timedelta(days=1)).isoformat(),
        "hard_stop": "9.7023",
        "intended_quantity": 100,
        "proposed_quantity": 100,
        "current_quantity": 0,
        "current_price": "10.45",
        "planned_entry_low": "10.4209",
        "planned_entry_high": "10.5391",
        "planned_max_quantity": 600,
        "market_stage": "SECOND_CONFIRMATION",
        "chase_risk": False,
        "product_v1_status": "READY",
        "csv_v2_executable": False,
        "survival_blocks": [],
        "source_refs": ["product-v1:test"],
    }
    payload.update(updates)
    return payload


def test_pretrade_api_is_idempotent_and_non_executable(client, session):
    account, _ = seed(session)
    first = client.post("/api/v1/trading-discipline/pretrade-check", json=context(account.id))
    second = client.post("/api/v1/trading-discipline/pretrade-check", json=context(account.id))
    assert first.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["score"] == 100
    assert first.json()["executable"] is False


def test_future_evidence_is_excluded_from_historical_query(client, session):
    seed(session)
    before = {
        "symbol": "300308",
        "title": "entry fact",
        "content_summary": "official",
        "tier": "FACT",
        "source_type": "announcement",
        "observed_at": "2026-08-09T09:00:00",
        "published_at": "2026-08-09T08:00:00",
        "added_by": "user",
        "before_or_after_entry": "BEFORE",
        "verified": True,
        "quality": "VERIFIED",
    }
    after = before | {
        "title": "future news",
        "observed_at": "2026-08-10T09:00:00",
        "published_at": "2026-08-10T08:00:00",
        "before_or_after_entry": "AFTER",
    }
    assert client.post("/api/v1/trading-discipline/evidence", json=before).status_code == 201
    assert client.post("/api/v1/trading-discipline/evidence", json=after).status_code == 201
    rows = client.get(
        "/api/v1/trading-discipline/evidence",
        params={"symbol": "300308", "decision_at": "2026-08-09T09:30:00"},
    ).json()
    assert [item["title"] for item in rows] == ["entry fact"]


def test_sentiment_has_no_formal_plan_authority(client, session):
    seed(session)
    payload = {
        "symbol": "300308",
        "title": "听说",
        "content_summary": "论坛观点",
        "tier": "SENTIMENT",
        "source_type": "forum",
        "observed_at": "2026-08-09T09:00:00",
        "added_by": "user",
        "before_or_after_entry": "AFTER",
        "verified": False,
        "quality": "UNVERIFIED",
    }
    result = client.post("/api/v1/trading-discipline/evidence", json=payload).json()
    assert result["formal_execution_authority"] is False
    assert result["may_modify_plan"] is False


def test_original_thesis_is_immutable_and_revision_links_parent(client, session):
    account, playbook = seed(session)
    original = {
        "thesis_id": "thesis-1",
        "symbol": "300308",
        "account_id": account.id,
        "playbook_id": playbook.id,
        "entry_reasons": ["state change"],
        "invalidation_conditions": ["breakout failed"],
        "expected_behavior": ["hold support"],
        "hard_stop": "9.7023",
        "initial_position": 100,
        "max_position": 600,
        "created_at": "2026-08-09T09:00:00",
    }
    first = client.post("/api/v1/trading-discipline/theses", json=original)
    revision = client.post(
        "/api/v1/trading-discipline/theses",
        json=original
        | {"entry_reasons": ["new verified fact"], "parent_snapshot_id": first.json()["id"]},
    )
    assert first.status_code == revision.status_code == 201
    assert first.json()["revision"] == 1
    assert revision.json()["revision"] == 2
    rows = client.get("/api/v1/trading-discipline/theses", params={"thesis_id": "thesis-1"}).json()
    assert rows[0]["entry_reasons"] == ["state change"]
    assert rows[1]["parent_snapshot_id"] == rows[0]["id"]


def test_training_review_keeps_profit_and_discipline_independent(client, session):
    account, playbook = seed(session)
    program = client.post(
        "/api/v1/trading-discipline/training-programs",
        json={"account_id": account.id, "playbook_id": playbook.id, "status": "ACTIVE"},
    ).json()
    trade = Trade(
        account_id=account.id,
        symbol="300308",
        side="BUY",
        quantity=100,
        price=10,
        traded_at=datetime(2026, 8, 9, 10),
        is_planned=False,
    )
    session.add(trade)
    session.commit()
    review = client.post(
        "/api/v1/trading-discipline/reviews",
        json={
            "trade_id": trade.id,
            "training_program_id": program["id"],
            "pnl_pct": "20",
            "error_codes": ["UNPLANNED_TRADE"],
            "stage_at_decision": "ACCELERATION",
        },
    )
    assert review.status_code == 201
    assert review.json()["compliance_result"] == "PROFITABLE_UNDISCIPLINED"
    dashboard = client.get(
        "/api/v1/trading-discipline/dashboard", params={"account_id": account.id}
    ).json()
    assert dashboard["progress"] == {"valid": 1, "target": 20}
    assert dashboard["playbook_quality_conclusion"] is None
