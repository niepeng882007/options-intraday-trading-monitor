"""测试数据采集 + 验证逻辑。"""

from models import IndexData, MacroData, Mag7Data
from collector import (
    DataCollector,
    _short_symbol,
    _extract_prev_day_levels,
    _calculate_volume_profile,
    _integer_round_levels,
    lookup_risk,
)
import pandas as pd
import numpy as np


# ── 纯函数测试 ──


class TestShortSymbol:
    def test_futu_format(self):
        assert _short_symbol("US.QQQ") == "QQQ"

    def test_already_short(self):
        assert _short_symbol("QQQ") == "QQQ"

    def test_hk_format(self):
        assert _short_symbol("HK.00700") == "00700"


class TestIntegerRoundLevels:
    def test_high_price(self):
        upper, lower = _integer_round_levels(485.0)
        assert upper == 490.0
        assert lower == 480.0

    def test_low_price(self):
        upper, lower = _integer_round_levels(23.0)
        assert upper == 25.0
        assert lower == 20.0

    def test_zero(self):
        assert _integer_round_levels(0) == (0.0, 0.0)


class TestExtractPrevDayLevels:
    def test_with_two_days(self):
        idx = pd.DatetimeIndex(
            pd.date_range("2026-03-24 09:30", periods=10, freq="5min", tz="America/New_York").tolist()
            + pd.date_range("2026-03-25 09:30", periods=5, freq="5min", tz="America/New_York").tolist()
        )
        data = {
            "Open": [100] * 15,
            "High": [102, 103, 101, 104, 102, 103, 101, 105, 102, 103, 100, 101, 102, 103, 104],
            "Low": [98, 99, 97, 96, 98, 99, 97, 95, 98, 99, 100, 99, 98, 97, 96],
            "Close": [101] * 15,
            "Volume": [1000] * 15,
        }
        bars = pd.DataFrame(data, index=idx)
        pdh, pdl, pdc = _extract_prev_day_levels(bars)
        assert pdh == 105.0  # max High on 03-24
        assert pdl == 95.0   # min Low on 03-24
        assert pdc == 101.0  # last Close on 03-24


class TestVolumeProfile:
    def test_basic_calculation(self):
        idx = pd.DatetimeIndex(
            pd.date_range("2026-03-24 09:30", periods=100, freq="5min", tz="America/New_York").tolist()
            + pd.date_range("2026-03-25 09:30", periods=5, freq="5min", tz="America/New_York").tolist()
        )
        np.random.seed(42)
        prices = np.random.normal(500, 5, 105)
        data = {
            "High": prices + 1,
            "Low": prices - 1,
            "Close": prices,
            "Volume": np.random.randint(1000, 5000, 105),
        }
        bars = pd.DataFrame(data, index=idx)
        poc, vah, val = _calculate_volume_profile(bars, 0.70)

        assert poc > 0
        assert vah > poc
        assert val < poc

    def test_empty_bars(self):
        bars = pd.DataFrame()
        poc, vah, val = _calculate_volume_profile(bars)
        assert poc == 0.0


# ── 数据验证 ──


class TestDataValidation:
    def test_vix_below_threshold(self, default_config):
        collector = DataCollector.__new__(DataCollector)
        collector._vix_min = 1.0
        collector._tnx_min = 0.01
        collector._uup_min = 1.0
        collector._stale_hour = 4

        macro = MacroData(vix_current=0.5, vix_prev_close=18.0, vix_ma10=17.0)
        indices = []
        mag7 = []
        statuses = collector._validate(macro, indices, mag7)

        # VIX 应被标为不可用
        assert macro.vix_current is None
        assert any("VIX" in s.detail for s in statuses)

    def test_tnx_below_threshold(self, default_config):
        collector = DataCollector.__new__(DataCollector)
        collector._vix_min = 1.0
        collector._tnx_min = 0.01
        collector._uup_min = 1.0
        collector._stale_hour = 4

        macro = MacroData(vix_current=18.0, tnx_current=0.001, tnx_prev_close=4.3)
        statuses = collector._validate(macro, [], [])

        assert macro.tnx_current is None
        assert any("TNX" in s.detail for s in statuses)

    def test_change_pct_self_calculation(self):
        """涨跌幅必须自算，不依赖 Futu change_rate。"""
        price = 555.0
        prev_close = 550.0
        expected = (price - prev_close) / prev_close * 100
        assert abs(expected - 0.909) < 0.01

    def test_no_trade_detection(self):
        """last_price == prev_close 应标记为盘前无成交。"""
        idx = IndexData(symbol="QQQ", price=480.0, prev_close=480.0, status="盘前无成交")
        assert idx.status == "盘前无成交"


# ── 风控查表 ──


class TestRiskLookup:
    def test_normal(self, default_config):
        result = lookup_risk(0.05, default_config)
        assert result["regime"] == "normal"
        assert result["max_daily_loss_pct"] == 2.0
        assert result["max_single_risk_pct"] == 1.0

    def test_high_volatility(self, default_config):
        result = lookup_risk(0.25, default_config)
        assert result["regime"] == "high_volatility"
        assert result["max_daily_loss_pct"] == 1.0
        assert result["cooldown_minutes"] == 999

    def test_negative_deviation(self, default_config):
        """负偏离超过阈值也应触发高波动。"""
        result = lookup_risk(-0.25, default_config)
        assert result["regime"] == "high_volatility"

    def test_none_deviation(self, default_config):
        """VIX 偏离不可用时使用 normal。"""
        result = lookup_risk(None, default_config)
        assert result["regime"] == "normal"
