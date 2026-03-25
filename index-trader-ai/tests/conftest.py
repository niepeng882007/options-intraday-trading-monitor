"""共享 fixtures。"""

import sys
from pathlib import Path

import pytest

# 将项目根目录加入 sys.path，使 import models/collector/... 正常工作
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def default_config():
    """最小化测试配置。"""
    return {
        "futu": {"host": "127.0.0.1", "port": 11111},
        "symbols": {
            "indexes": ["US.QQQ", "US.SPY", "US.IWM"],
            "mag7": ["US.AAPL", "US.MSFT", "US.NVDA", "US.AMZN", "US.GOOGL", "US.META", "US.TSLA"],
            "macro": {"vix": "^VIX", "tnx": "^TNX", "uup": "UUP"},
        },
        "collector": {
            "macro_cache_ttl": 120,
            "vix_ma_period": 10,
            "vix_ma_cache_ttl": 86400,
            "mag7_volume_avg_days": 5,
            "weekly_lookback_days": 5,
            "volume_profile": {"lookback_trading_days": 5, "value_area_pct": 0.70},
            "gamma_wall": {"enabled": False},
        },
        "validation": {
            "vix_min_valid": 1.0,
            "tnx_min_valid": 0.01,
            "uup_min_valid": 1.0,
            "stale_data_cutoff_hour_et": 4,
        },
        "levels": {"proximity_pct": 0.001},
        "risk": {
            "vix_high_deviation_threshold": 0.20,
            "normal": {
                "max_single_risk_pct": 1.0,
                "max_daily_loss_pct": 2.0,
                "circuit_breaker_count": 3,
                "cooldown_minutes": 30,
            },
            "high_volatility": {
                "max_single_risk_pct": 0.5,
                "max_daily_loss_pct": 1.0,
                "circuit_breaker_count": 2,
                "cooldown_minutes": 999,
            },
        },
        "update_thresholds": {"price_change_pct": 0.05, "pct_change_abs": 0.10},
        "monitor": {
            "enabled": True,
            "volume_anomaly_ratio": 3.0,
            "data_alert_minutes_before": 5,
        },
        "archive_dir": "/tmp/index_trader_test_archive",
    }
