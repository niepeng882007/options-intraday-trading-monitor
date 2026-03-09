"""Tests for the HK Predict module."""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timezone, timedelta

from src.hk import (
    RegimeType, VolumeProfileResult, GammaWallResult,
    RegimeResult, FilterResult, Playbook,
)
from src.hk.volume_profile import calculate_volume_profile
from src.hk.indicators import (
    calculate_vwap, calculate_vwap_series, calculate_rvol,
    get_today_bars, get_history_bars, is_trading_time,
)
from src.hk.regime import classify_regime
from src.hk.playbook import generate_playbook, format_playbook_message
from src.hk.filter import check_filters
from src.hk.orderbook import analyze_order_book, format_order_book_summary, format_alerts_message
from src.hk.gamma_wall import calculate_gamma_wall, format_gamma_wall_message

HKT = timezone(timedelta(hours=8))


# ── Helpers ──

def _make_bars(prices: list[tuple], tz: str = "Asia/Hong_Kong") -> pd.DataFrame:
    """Create a DataFrame of 1m bars.

    prices: list of (datetime_str, open, high, low, close, volume)
    """
    rows = []
    for ts, o, h, l, c, v in prices:
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
    idx = pd.DatetimeIndex(
        [pd.Timestamp(p[0], tz=tz) for p in prices], name="Datetime"
    )
    return pd.DataFrame(rows, index=idx)


# ── Volume Profile Tests ──

class TestVolumeProfile:
    def test_empty_bars(self):
        result = calculate_volume_profile(pd.DataFrame())
        assert result.poc == 0
        assert result.vah == 0
        assert result.val == 0

    def test_single_bar(self):
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 102, 99, 101, 10000),
        ])
        result = calculate_volume_profile(bars, tick_size=1.0)
        assert result.poc > 0
        assert result.total_volume > 0

    def test_poc_at_highest_volume(self):
        """POC should be at the price level with the most volume."""
        bars = _make_bars([
            # Most volume concentrated around 500
            ("2026-03-09 09:30:00", 500, 501, 499, 500, 100000),
            ("2026-03-09 09:31:00", 500, 501, 499, 500, 100000),
            ("2026-03-09 09:32:00", 500, 501, 499, 500, 100000),
            # Less volume at 510
            ("2026-03-09 09:33:00", 510, 511, 509, 510, 1000),
        ])
        result = calculate_volume_profile(bars, tick_size=1.0)
        assert 499 <= result.poc <= 501

    def test_value_area(self):
        """VAH >= POC >= VAL."""
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 110, 90, 100, 10000),
            ("2026-03-09 09:31:00", 100, 105, 95, 102, 20000),
            ("2026-03-09 09:32:00", 98, 103, 97, 100, 15000),
        ])
        result = calculate_volume_profile(bars, tick_size=1.0)
        assert result.vah >= result.poc >= result.val

    def test_auto_tick_size(self):
        """Tick size auto-detection based on price."""
        # HSI-level prices should get tick_size=50
        bars = _make_bars([
            ("2026-03-09 09:30:00", 25000, 25100, 24900, 25050, 1000),
        ])
        result = calculate_volume_profile(bars)
        # Should have bins at 50-point intervals
        assert all(p % 50 == 0 for p in result.volume_by_price.keys())


# ── VWAP Tests ──

class TestVWAP:
    def test_empty(self):
        assert calculate_vwap(pd.DataFrame()) == 0.0

    def test_single_bar(self):
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 102, 98, 100, 10000),
        ])
        vwap = calculate_vwap(bars)
        # VWAP = (H+L+C)/3 for single bar
        assert abs(vwap - (102 + 98 + 100) / 3) < 0.01

    def test_volume_weighted(self):
        """Higher volume bar should pull VWAP toward its price."""
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 100, 100, 100, 1),
            ("2026-03-09 09:31:00", 200, 200, 200, 200, 1000000),
        ])
        vwap = calculate_vwap(bars)
        assert vwap > 190  # Should be close to 200

    def test_series_length(self):
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 102, 98, 100, 10000),
            ("2026-03-09 09:31:00", 101, 103, 99, 101, 10000),
        ])
        series = calculate_vwap_series(bars)
        assert len(series) == 2


# ── RVOL Tests ──

class TestRVOL:
    def test_empty_returns_neutral(self):
        assert calculate_rvol(pd.DataFrame(), pd.DataFrame()) == 1.0

    def test_same_volume(self):
        """Same volume as history → RVOL ≈ 1.0."""
        today = _make_bars([
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 10000),
        ])
        hist = _make_bars([
            ("2026-03-08 09:30:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_rvol(today, hist)
        assert abs(rvol - 1.0) < 0.1

    def test_high_volume(self):
        """2x volume → RVOL ≈ 2.0."""
        today = _make_bars([
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 20000),
        ])
        hist = _make_bars([
            ("2026-03-08 09:30:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_rvol(today, hist)
        assert abs(rvol - 2.0) < 0.1


# ── Regime Tests ──

class TestRegime:
    def _vp(self, poc=500, vah=510, val=490):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_breakout(self):
        """High RVOL + price above VAH = BREAKOUT."""
        result = classify_regime(price=515, rvol=1.5, vp=self._vp())
        assert result.regime == RegimeType.BREAKOUT

    def test_range(self):
        """Low RVOL + price in value area = RANGE."""
        result = classify_regime(price=500, rvol=0.6, vp=self._vp())
        assert result.regime == RegimeType.RANGE

    def test_whipsaw(self):
        """IV spike + near gamma wall = WHIPSAW."""
        gw = GammaWallResult(call_wall_strike=502, put_wall_strike=498, max_pain=500)
        result = classify_regime(
            price=501, rvol=1.0, vp=self._vp(),
            gamma_wall=gw, atm_iv=40, avg_iv=20, iv_spike_ratio=1.3,
        )
        assert result.regime == RegimeType.WHIPSAW

    def test_unclear_neutral_rvol(self):
        """RVOL in neutral zone → UNCLEAR."""
        result = classify_regime(price=500, rvol=1.0, vp=self._vp())
        assert result.regime == RegimeType.UNCLEAR

    def test_unclear_no_data(self):
        vp = VolumeProfileResult(poc=0, vah=0, val=0)
        result = classify_regime(price=100, rvol=1.0, vp=vp)
        assert result.regime == RegimeType.UNCLEAR
        assert result.confidence == 0.0

    def test_breakout_below_val(self):
        """Price below VAL with high RVOL = BREAKOUT."""
        result = classify_regime(price=480, rvol=1.5, vp=self._vp())
        assert result.regime == RegimeType.BREAKOUT
        assert "below VAL" in result.details


# ── Playbook Tests ──

class TestPlaybook:
    def test_generate(self):
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=25100, vah=25000, val=24800, poc=24900,
        )
        vp = VolumeProfileResult(poc=24900, vah=25000, val=24800)
        pb = generate_playbook(regime, vp, vwap=24950)
        assert pb.regime.regime == RegimeType.BREAKOUT
        assert "VWAP" in pb.key_levels
        assert pb.strategy_text

    def test_format_message(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=500, vah=510, val=490, poc=500,
        )
        vp = VolumeProfileResult(poc=500, vah=510, val=490)
        pb = generate_playbook(regime, vp, vwap=502)
        msg = format_playbook_message(pb, symbol="HK.00700", update_type="morning")
        assert "Playbook" in msg
        assert "HK.00700" in msg
        assert "RVOL" in msg


# ── Filter Tests ──

class TestFilter:
    def test_normal_day(self):
        result = check_filters(
            symbol="HK.00700", turnover=5e9,
            prev_high=520, prev_low=510, current_high=525, current_low=505,
            calendar_path="nonexistent.yaml",
        )
        assert result.tradeable
        assert result.risk_level == "normal"

    def test_inside_day_with_atr_shrink(self):
        result = check_filters(
            symbol="HK.00700", turnover=5e9,
            prev_high=520, prev_low=510, current_high=518, current_low=512,
            atr_current=2.0, atr_prev=10.0,
            calendar_path="nonexistent.yaml",
        )
        assert any("Inside Day" in w for w in result.warnings)
        assert result.risk_level in ("elevated", "high")

    def test_high_iv_low_rvol(self):
        result = check_filters(
            symbol="HK.00700", turnover=5e9,
            prev_high=0, prev_low=0, current_high=0, current_low=0,
            iv_rank=85, rvol=0.7,
            calendar_path="nonexistent.yaml",
        )
        assert not result.tradeable
        assert result.risk_level == "high"

    def test_low_turnover_warning(self):
        result = check_filters(
            symbol="HK.00700", turnover=5e7,
            prev_high=0, prev_low=0, current_high=0, current_low=0,
            min_turnover=1e8,
            calendar_path="nonexistent.yaml",
        )
        assert any("HKD" in w for w in result.warnings)

    def test_expiry_day_risk(self):
        today = date(2026, 3, 13)
        result = check_filters(
            symbol="HK.00700", turnover=5e9,
            prev_high=0, prev_low=0, current_high=0, current_low=0,
            expiry_date=today, today=today,
            calendar_path="nonexistent.yaml",
        )
        assert result.risk_level == "high"
        assert any("Theta" in w for w in result.warnings)


# ── Order Book Tests ──

class TestOrderBook:
    def _book(self):
        return {
            "code": "HK.00700",
            "Ask": [
                (511.0, 1000, 5, {}),
                (511.5, 800, 3, {}),
                (512.0, 1200, 4, {}),
                (512.5, 900, 3, {}),
                (513.0, 30000, 2, {}),  # Large order: 30000 / avg(~1180) ≈ 6.8x
            ],
            "Bid": [
                (510.5, 900, 4, {}),
                (510.0, 1100, 6, {}),
            ],
        }

    def test_detect_large_order(self):
        alerts = analyze_order_book(self._book(), large_order_ratio=3.0)
        assert len(alerts) >= 1
        assert any(a.price == 513.0 for a in alerts)

    def test_no_alerts_low_threshold(self):
        alerts = analyze_order_book(self._book(), large_order_ratio=100.0)
        assert len(alerts) == 0

    def test_format_summary(self):
        text = format_order_book_summary(self._book())
        assert "HK.00700" in text
        assert "512.00" in text

    def test_format_alerts_empty(self):
        assert format_alerts_message([]) == ""


# ── Gamma Wall Tests ──

class TestGammaWall:
    def _chain(self):
        return pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 25000, "open_interest": 100},
            {"code": "C2", "option_type": "CALL", "strike_price": 25200, "open_interest": 500},
            {"code": "C3", "option_type": "CALL", "strike_price": 25400, "open_interest": 200},
            {"code": "P1", "option_type": "PUT", "strike_price": 24600, "open_interest": 300},
            {"code": "P2", "option_type": "PUT", "strike_price": 24800, "open_interest": 800},
            {"code": "P3", "option_type": "PUT", "strike_price": 25000, "open_interest": 150},
        ])

    def test_call_wall(self):
        gw = calculate_gamma_wall(self._chain(), current_price=25100)
        assert gw.call_wall_strike == 25200  # Max call OI above price

    def test_put_wall(self):
        gw = calculate_gamma_wall(self._chain(), current_price=25100)
        assert gw.put_wall_strike == 24800  # Max put OI below price

    def test_max_pain(self):
        gw = calculate_gamma_wall(self._chain(), current_price=25100)
        assert gw.max_pain > 0  # Should find a valid max pain

    def test_empty_chain(self):
        gw = calculate_gamma_wall(pd.DataFrame(), current_price=25000)
        assert gw.call_wall_strike == 0
        assert gw.put_wall_strike == 0

    def test_format_message(self):
        gw = GammaWallResult(
            call_wall_strike=25200, put_wall_strike=24800, max_pain=25000,
            call_oi_by_strike={25200: 500}, put_oi_by_strike={24800: 800},
        )
        msg = format_gamma_wall_message(gw, "HSI")
        assert "25,200" in msg
        assert "24,800" in msg
        assert "HSI" in msg


# ── Bar Splitting Tests ──

class TestBarSplitting:
    def test_get_today_bars(self):
        bars = _make_bars([
            ("2026-03-07 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-08 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-09 10:00:00", 101, 102, 100, 101, 1000),
        ])
        today = get_today_bars(bars)
        assert len(today) == 2

    def test_get_history_bars(self):
        bars = _make_bars([
            ("2026-03-07 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-08 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 1000),
        ])
        hist = get_history_bars(bars)
        assert len(hist) == 2


# ── Trading Time Tests ──

class TestTradingTime:
    def test_morning(self):
        from datetime import time as dt_time
        assert is_trading_time(dt_time(9, 30))
        assert is_trading_time(dt_time(11, 59))

    def test_lunch_break(self):
        from datetime import time as dt_time
        assert not is_trading_time(dt_time(12, 30))

    def test_afternoon(self):
        from datetime import time as dt_time
        assert is_trading_time(dt_time(13, 0))
        assert is_trading_time(dt_time(15, 59))


# ── Order Book Dedup Tests ──

class TestOrderBookDedup:
    """Test order book alert dedup logic in HKPredictor."""

    def _make_predictor(self) -> "HKPredictor":
        """Create a minimal HKPredictor with mocked collector."""
        from unittest.mock import MagicMock, AsyncMock
        from src.hk.main import HKPredictor, _SeenOrder

        p = object.__new__(HKPredictor)
        p._cfg = {
            "watchlist": {"stocks": [{"symbol": "HK.00700", "name": "Tencent"}]},
            "order_book": {
                "large_order_ratio": 3.0,
                "monitor_depth": 10,
                "alert_cooldown_minutes": 15,
                "volume_change_threshold": 0.5,
                "seen_order_expiry_minutes": 60,
            },
        }
        p._collector = MagicMock()
        p._send_fn = AsyncMock()
        p._seen_orders = {}
        p._last_playbooks = {}
        p._connected = True
        return p

    def _make_book(self, ask_price: float = 513.0, ask_vol: int = 30000) -> dict:
        return {
            "code": "HK.00700",
            "Ask": [
                (511.0, 1000, 5, {}),
                (511.5, 800, 3, {}),
                (512.0, 1200, 4, {}),
                (512.5, 900, 3, {}),
                (ask_price, ask_vol, 2, {}),
            ],
            "Bid": [(510.5, 900, 4, {})],
        }

    @pytest.mark.asyncio
    async def test_first_alert_passes(self):
        """First time seeing a large order → alert with 🆕 tag."""
        p = self._make_predictor()
        p._collector.get_order_book = lambda *a, **k: self._make_book()

        sent_messages = []
        async def capture(text, **kw):
            sent_messages.append(text)
        p._send_fn = capture

        # Patch _run_sync to call function directly
        async def _run_sync(fn, *args):
            return fn(*args)
        p._run_sync = _run_sync

        await p.check_orderbook_alerts()
        assert len(sent_messages) == 1
        assert "🆕" in sent_messages[0]
        assert len(p._seen_orders) == 1

    @pytest.mark.asyncio
    async def test_duplicate_suppressed(self):
        """Same order within cooldown → no alert."""
        p = self._make_predictor()
        p._collector.get_order_book = lambda *a, **k: self._make_book()

        sent_messages = []
        async def capture(text, **kw):
            sent_messages.append(text)
        p._send_fn = capture

        async def _run_sync(fn, *args):
            return fn(*args)
        p._run_sync = _run_sync

        await p.check_orderbook_alerts()
        assert len(sent_messages) == 1  # first alert

        await p.check_orderbook_alerts()
        assert len(sent_messages) == 1  # no second alert

    @pytest.mark.asyncio
    async def test_volume_increase_realerts(self):
        """Volume increase ≥50% → re-alert with 📈 tag."""
        from src.hk.main import _SeenOrder

        p = self._make_predictor()
        now = datetime.now(timezone(timedelta(hours=8)))

        # Pre-seed a seen order with volume=20000
        key = ("HK.00700", "ask", 513.0)
        p._seen_orders[key] = _SeenOrder(volume=20000, alerted_at=now)

        # New book has 30000 → +50% increase
        p._collector.get_order_book = lambda *a, **k: self._make_book(ask_vol=30000)

        sent_messages = []
        async def capture(text, **kw):
            sent_messages.append(text)
        p._send_fn = capture

        async def _run_sync(fn, *args):
            return fn(*args)
        p._run_sync = _run_sync

        await p.check_orderbook_alerts()
        assert len(sent_messages) == 1
        assert "📈" in sent_messages[0]

    @pytest.mark.asyncio
    async def test_cooldown_expiry_realerts(self):
        """After cooldown expires, still-present order → re-alert with ⏰ tag."""
        from src.hk.main import _SeenOrder

        p = self._make_predictor()
        now = datetime.now(timezone(timedelta(hours=8)))

        # Pre-seed order alerted 20 minutes ago (cooldown=15min)
        key = ("HK.00700", "ask", 513.0)
        p._seen_orders[key] = _SeenOrder(
            volume=30000,
            alerted_at=now - timedelta(minutes=20),
        )

        p._collector.get_order_book = lambda *a, **k: self._make_book(ask_vol=30000)

        sent_messages = []
        async def capture(text, **kw):
            sent_messages.append(text)
        p._send_fn = capture

        async def _run_sync(fn, *args):
            return fn(*args)
        p._run_sync = _run_sync

        await p.check_orderbook_alerts()
        assert len(sent_messages) == 1
        assert "⏰" in sent_messages[0]

    def test_cleanup_expired(self):
        """Expired entries are removed from _seen_orders."""
        from src.hk.main import _SeenOrder

        p = self._make_predictor()
        now = datetime.now(timezone(timedelta(hours=8)))

        # Add an entry that's 90 minutes old (expiry=60min)
        key_old = ("HK.00700", "ask", 513.0)
        p._seen_orders[key_old] = _SeenOrder(
            volume=30000,
            alerted_at=now - timedelta(minutes=90),
        )

        # Add a recent entry
        key_new = ("HK.00700", "bid", 510.0)
        p._seen_orders[key_new] = _SeenOrder(
            volume=5000,
            alerted_at=now - timedelta(minutes=5),
        )

        p._cleanup_seen_orders(now, expiry_minutes=60)
        assert key_old not in p._seen_orders
        assert key_new in p._seen_orders

    @pytest.mark.asyncio
    async def test_different_prices_independent(self):
        """Different price levels are tracked independently."""
        from src.hk.main import _SeenOrder

        p = self._make_predictor()
        now = datetime.now(timezone(timedelta(hours=8)))

        # Pre-seed order at 513.0
        key = ("HK.00700", "ask", 513.0)
        p._seen_orders[key] = _SeenOrder(volume=30000, alerted_at=now)

        # New book has large order at DIFFERENT price 514.0
        book = {
            "code": "HK.00700",
            "Ask": [
                (511.0, 1000, 5, {}),
                (511.5, 800, 3, {}),
                (512.0, 1200, 4, {}),
                (512.5, 900, 3, {}),
                (514.0, 25000, 2, {}),  # Different price
            ],
            "Bid": [(510.5, 900, 4, {})],
        }
        p._collector.get_order_book = lambda *a, **k: book

        sent_messages = []
        async def capture(text, **kw):
            sent_messages.append(text)
        p._send_fn = capture

        async def _run_sync(fn, *args):
            return fn(*args)
        p._run_sync = _run_sync

        await p.check_orderbook_alerts()
        assert len(sent_messages) == 1
        assert "🆕" in sent_messages[0]
        # Both keys should exist
        assert ("HK.00700", "ask", 513.0) in p._seen_orders
        assert ("HK.00700", "ask", 514.0) in p._seen_orders
