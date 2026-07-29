from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


FAKE_AKSHARE = r'''
import os
print("akshare import diagnostic")

class Frame:
    columns = ["序号", "代码", "涨跌幅"]
    empty = False

    def __init__(self, rows):
        self.rows = rows

    def notna(self):
        return self

    def where(self, condition, other):
        return self

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows

def stock_zt_pool_em(date):
    print("limit-up sdk diagnostic")
    code = "X" * 513 if os.environ.get("FAKE_LONG_FIELD") == "1" else "600001"
    return Frame([{"序号": 1, "代码": code, "涨跌幅": 10}])

def stock_zt_pool_dtgc_em(date):
    print("limit-down sdk diagnostic")
    return Frame([{"序号": 1, "代码": "000001", "涨跌幅": -10}])
'''


def _run(tmp_path: Path, *, long_field: bool = False):
    (tmp_path / "akshare.py").write_text(FAKE_AKSHARE, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), str(Path(__file__).resolve().parents[1])]
    )
    if long_field:
        env["FAKE_LONG_FIELD"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "app.providers.market_breadth_worker"],
        input=json.dumps(
            {"operation": "limit_pools", "trade_date": "2026-07-28"}
        ).encode(),
        capture_output=True,
        timeout=10,
        env=env,
        shell=False,
    )


def test_worker_isolates_import_and_sdk_stdout(tmp_path):
    completed = _run(tmp_path)
    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["limit_up_symbols"] == ["600001"]
    assert response["limit_down_symbols"] == ["000001"]
    assert completed.stdout.count(b"{") == 1
    assert b"diagnostic" in completed.stderr


def test_worker_rejects_long_field_with_bounded_error_json(tmp_path):
    completed = _run(tmp_path, long_field=True)
    assert completed.returncode == 1
    response = json.loads(completed.stdout)
    assert response["ok"] is False
    assert response["error_type"] == "QUERY"
    assert len(completed.stdout) < 4096


def test_worker_rejects_arbitrary_operation_with_one_error_json():
    completed = subprocess.run(
        [sys.executable, "-m", "app.providers.market_breadth_worker"],
        input=b'{"operation":"anything","trade_date":"2026-07-28"}',
        capture_output=True,
        timeout=10,
        shell=False,
    )
    assert completed.returncode == 1
    response = json.loads(completed.stdout)
    assert response["error_type"] == "REQUEST"
    assert completed.stdout.count(b"{") == 1
