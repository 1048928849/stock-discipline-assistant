from decimal import Decimal


def account_payload():
    return {
        "name": "主账户",
        "total_assets": "100000.0000",
        "cash": "60000.0000",
        "available_cash": "55000.0000",
    }


def holding_payload(account_id: int):
    return {
        "account_id": account_id,
        "symbol": "600519",
        "name": "贵州茅台",
        "quantity": 10,
        "cost_price": "1500.0000",
        "current_price": "1600.0000",
        "sector": "白酒",
        "buy_reason": "仅用于测试记录",
        "invalidation_condition": "基本面假设失效",
        "stop_loss_price": "1400.0000",
        "max_position_pct": "20.0000",
        "price_source": "manual",
    }


def test_health_and_home(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_account_and_holding_crud_uses_total_assets_as_denominator(client):
    account = client.post("/api/v1/accounts", json=account_payload()).json()
    response = client.post("/api/v1/holdings", json=holding_payload(account["id"]))
    assert response.status_code == 201
    holding = response.json()
    assert Decimal(holding["market_value"]) == Decimal("16000.0000")
    assert Decimal(holding["unrealized_pnl"]) == Decimal("1000.0000")
    assert Decimal(holding["return_pct"]) == Decimal("6.6667")
    # 16000 / 总资产100000，而非 16000 / 持仓市值16000。
    assert Decimal(holding["position_pct"]) == Decimal("16.0000")
    assert client.get("/api/v1/holdings").json()[0]["symbol"] == "600519"
    assert client.delete(f"/api/v1/holdings/{holding['id']}").status_code == 204


def test_duplicate_holding_returns_stable_error(client):
    account = client.post("/api/v1/accounts", json=account_payload()).json()
    payload = holding_payload(account["id"])
    assert client.post("/api/v1/holdings", json=payload).status_code == 201
    response = client.post("/api/v1/holdings", json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DUPLICATE_RESOURCE"


def test_validation_and_not_found_have_stable_errors(client):
    invalid = account_payload()
    invalid["cash"] = "120000"
    response = client.post("/api/v1/accounts", json=invalid)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    response = client.get("/api/v1/accounts/999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"
    response = client.get("/api/v1/not-a-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
