import os
from datetime import datetime

import pytest

from app.config import Settings
from app.data_hub.trading_calendar import SHANGHAI_TZ, get_trading_calendar
from app.providers.freestockdb import FreeStockDBHttpClient, FreeStockDBProvider


@pytest.mark.freestockdb_integration
def test_real_local_freestockdb_health_and_minimum_history_contract():
    settings = Settings(
        freestockdb_enabled=True,
        freestockdb_base_url=os.environ.get(
            "FREESTOCKDB_BASE_URL", "http://127.0.0.1:7899"
        ),
        freestockdb_csi300_symbol=os.environ["FREESTOCKDB_CSI300_SYMBOL"],
    )
    provider = FreeStockDBProvider(settings, client=FreeStockDBHttpClient(settings))
    assert provider.health_check(probe=True)["status"] == "READY"
    now = datetime.now(SHANGHAI_TZ)
    end = get_trading_calendar().latest_completed_session(now)
    start = end.replace(year=end.year - 1)
    assert len(provider.get_index_history("CSI000300", start, end)["rows"]) >= 80
    assert len(provider.get_history("600519", start, end)) >= 80
    assert len(provider.get_turnover_daily("600519", start, end)) >= 80
