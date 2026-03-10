"""Tests for the HK Predict module."""

import json
import os
import tempfile

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timezone, timedelta

from src.hk import (
    RegimeType, VolumeProfileResult, GammaWallResult,
    RegimeResult, FilterResult, Playbook,
    OptionRecommendation, OptionLeg,
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
from src.hk.watchlist import HKWatchlist, normalize_symbol
from src.hk.option_recommend import (
    select_expiry,
    classify_moneyness,
    recommend_single_leg,
    recommend_spread,
    should_wait,
    recommend,
)

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


# ── normalize_symbol Tests ──

class TestNormalizeSymbol:
    def test_plain_code(self):
        assert normalize_symbol("09988") == "HK.09988"

    def test_hk_prefix(self):
        assert normalize_symbol("HK09988") == "HK.09988"

    def test_hk_dot_prefix(self):
        assert normalize_symbol("HK.09988") == "HK.09988"

    def test_lowercase(self):
        assert normalize_symbol("hk09988") == "HK.09988"

    def test_six_digit(self):
        assert normalize_symbol("800000") == "HK.800000"

    def test_short_code_padded(self):
        assert normalize_symbol("0700") == "HK.00700"

    def test_invalid_text(self):
        assert normalize_symbol("AAPL") is None

    def test_empty(self):
        assert normalize_symbol("") is None

    def test_slash_prefix(self):
        assert normalize_symbol("/hk_help") is None


# ── Watchlist Tests ──

class TestWatchlist:
    def _make_wl(self, tmpdir, initial=None):
        path = os.path.join(tmpdir, "wl.json")
        return HKWatchlist(path=path, initial_config=initial)

    def test_add_and_contains(self, tmp_path):
        wl = self._make_wl(tmp_path)
        assert wl.add("HK.09988", "Alibaba")
        assert wl.contains("HK.09988")

    def test_add_duplicate(self, tmp_path):
        wl = self._make_wl(tmp_path)
        wl.add("HK.09988", "Alibaba")
        assert not wl.add("HK.09988", "Alibaba")

    def test_remove(self, tmp_path):
        wl = self._make_wl(tmp_path)
        wl.add("HK.09988", "Alibaba")
        assert wl.remove("HK.09988")
        assert not wl.contains("HK.09988")

    def test_remove_nonexistent(self, tmp_path):
        wl = self._make_wl(tmp_path)
        assert not wl.remove("HK.99999")

    def test_list_all(self, tmp_path):
        wl = self._make_wl(tmp_path)
        wl.add("HK.09988", "Alibaba")
        wl.add("HK.00700", "Tencent")
        items = wl.list_all()
        assert len(items) == 2
        assert items[0]["symbol"] == "HK.09988"

    def test_get_name(self, tmp_path):
        wl = self._make_wl(tmp_path)
        wl.add("HK.09988", "Alibaba")
        assert wl.get_name("HK.09988") == "Alibaba"
        assert wl.get_name("HK.99999") == "HK.99999"

    def test_persistence(self, tmp_path):
        path = os.path.join(tmp_path, "wl.json")
        wl1 = HKWatchlist(path=path)
        wl1.add("HK.09988", "Alibaba")
        # Create new instance from same file
        wl2 = HKWatchlist(path=path)
        assert wl2.contains("HK.09988")
        assert wl2.get_name("HK.09988") == "Alibaba"

    def test_init_from_config(self, tmp_path):
        cfg = {
            "watchlist": {
                "indices": [{"symbol": "HK.800000", "name": "HSI"}],
                "stocks": [{"symbol": "HK.00700", "name": "Tencent"}],
            }
        }
        wl = self._make_wl(tmp_path, initial=cfg)
        assert wl.contains("HK.800000")
        assert wl.contains("HK.00700")
        assert len(wl.list_all()) == 2


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
        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 501, 499, 500, 100000),
            ("2026-03-09 09:31:00", 500, 501, 499, 500, 100000),
            ("2026-03-09 09:32:00", 500, 501, 499, 500, 100000),
            ("2026-03-09 09:33:00", 510, 511, 509, 510, 1000),
        ])
        result = calculate_volume_profile(bars, tick_size=1.0)
        assert 499 <= result.poc <= 501

    def test_value_area(self):
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 110, 90, 100, 10000),
            ("2026-03-09 09:31:00", 100, 105, 95, 102, 20000),
            ("2026-03-09 09:32:00", 98, 103, 97, 100, 15000),
        ])
        result = calculate_volume_profile(bars, tick_size=1.0)
        assert result.vah >= result.poc >= result.val

    def test_auto_tick_size(self):
        bars = _make_bars([
            ("2026-03-09 09:30:00", 25000, 25100, 24900, 25050, 1000),
        ])
        result = calculate_volume_profile(bars)
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
        assert abs(vwap - (102 + 98 + 100) / 3) < 0.01

    def test_volume_weighted(self):
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 100, 100, 100, 1),
            ("2026-03-09 09:31:00", 200, 200, 200, 200, 1000000),
        ])
        vwap = calculate_vwap(bars)
        assert vwap > 190

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
        today = _make_bars([
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 10000),
        ])
        hist = _make_bars([
            ("2026-03-08 09:30:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_rvol(today, hist)
        assert abs(rvol - 1.0) < 0.1

    def test_high_volume(self):
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
        result = classify_regime(price=515, rvol=1.5, vp=self._vp())
        assert result.regime == RegimeType.BREAKOUT

    def test_range(self):
        result = classify_regime(price=500, rvol=0.6, vp=self._vp())
        assert result.regime == RegimeType.RANGE

    def test_whipsaw(self):
        gw = GammaWallResult(call_wall_strike=502, put_wall_strike=498, max_pain=500)
        result = classify_regime(
            price=501, rvol=1.0, vp=self._vp(),
            gamma_wall=gw, atm_iv=40, avg_iv=20, iv_spike_ratio=1.3,
        )
        assert result.regime == RegimeType.WHIPSAW

    def test_unclear_neutral_rvol(self):
        result = classify_regime(price=500, rvol=1.0, vp=self._vp())
        assert result.regime == RegimeType.UNCLEAR

    def test_unclear_no_data(self):
        vp = VolumeProfileResult(poc=0, vah=0, val=0)
        result = classify_regime(price=100, rvol=1.0, vp=vp)
        assert result.regime == RegimeType.UNCLEAR
        assert result.confidence == 0.0

    def test_breakout_below_val(self):
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

    def test_format_message_5_sections(self):
        """Formatted message should contain all 5 sections."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=500, vah=510, val=490, poc=500,
        )
        vp = VolumeProfileResult(poc=500, vah=510, val=490)
        rec = OptionRecommendation(
            action="call", direction="bullish",
            expiry="2026-03-18",
            legs=[OptionLeg(side="buy", option_type="call", strike=505, pct_from_price=1.0, moneyness="OTM 1.0%")],
            moneyness="OTM 1.0%",
            rationale="test rationale",
            risk_note="test risk note",
        )
        pb = generate_playbook(regime, vp, vwap=502, option_rec=rec)
        msg = format_playbook_message(pb, symbol="Tencent (HK.00700)")
        # Section checks
        assert "Regime" in msg
        assert "RVOL" in msg
        assert "POC" in msg
        assert "Call" in msg
        assert "505" in msg
        assert "2026-03-18" in msg

    def test_format_wait_recommendation(self):
        regime = RegimeResult(
            regime=RegimeType.UNCLEAR, confidence=0.3,
            rvol=0.4, price=500, vah=510, val=490, poc=500,
        )
        vp = VolumeProfileResult(poc=500, vah=510, val=490)
        rec = OptionRecommendation(
            action="wait", direction="neutral",
            rationale="观望",
            risk_note="Regime UNCLEAR",
            wait_conditions=["等待突破 VAH"],
        )
        pb = generate_playbook(regime, vp, vwap=502, option_rec=rec)
        msg = format_playbook_message(pb, symbol="Test")
        assert "观望" in msg
        assert "VAH" in msg


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
                (513.0, 30000, 2, {}),
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
        assert gw.call_wall_strike == 25200

    def test_put_wall(self):
        gw = calculate_gamma_wall(self._chain(), current_price=25100)
        assert gw.put_wall_strike == 24800

    def test_max_pain(self):
        gw = calculate_gamma_wall(self._chain(), current_price=25100)
        assert gw.max_pain > 0

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


# ── Option Recommendation Tests ──

class TestSelectExpiry:
    def test_filters_dte_zero(self):
        today = date(2026, 3, 10)
        dates = [
            {"strike_time": "2026-03-10"},  # DTE=0
            {"strike_time": "2026-03-17"},  # DTE=7
        ]
        result = select_expiry(dates, today)
        assert result == "2026-03-17"

    def test_prefers_nearest(self):
        today = date(2026, 3, 10)
        dates = [
            {"strike_time": "2026-03-14"},  # DTE=4
            {"strike_time": "2026-03-21"},  # DTE=11
        ]
        result = select_expiry(dates, today)
        assert result == "2026-03-14"

    def test_no_valid_expiry(self):
        today = date(2026, 3, 10)
        dates = [{"strike_time": "2026-03-10"}]  # only DTE=0
        assert select_expiry(dates, today) is None

    def test_empty_list(self):
        assert select_expiry([]) is None


class TestClassifyMoneyness:
    def test_atm(self):
        assert classify_moneyness(100.2, 100.0, "call") == "ATM"

    def test_call_otm(self):
        result = classify_moneyness(105, 100, "call")
        assert result.startswith("OTM")

    def test_call_itm(self):
        result = classify_moneyness(95, 100, "call")
        assert result.startswith("ITM")

    def test_put_otm(self):
        result = classify_moneyness(95, 100, "put")
        assert result.startswith("OTM")

    def test_put_itm(self):
        result = classify_moneyness(105, 100, "put")
        assert result.startswith("ITM")


class TestRecommendSingleLeg:
    def _chain(self, expiry="2026-03-17"):
        return pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 98, "strike_time": expiry,
             "open_interest": 100, "delta": 0.6, "implied_volatility": 30},
            {"code": "C2", "option_type": "CALL", "strike_price": 100, "strike_time": expiry,
             "open_interest": 200, "delta": 0.5, "implied_volatility": 28},
            {"code": "C3", "option_type": "CALL", "strike_price": 102, "strike_time": expiry,
             "open_interest": 150, "delta": 0.35, "implied_volatility": 26},
            {"code": "P1", "option_type": "PUT", "strike_price": 98, "strike_time": expiry,
             "open_interest": 180, "delta": -0.4, "implied_volatility": 30},
            {"code": "P2", "option_type": "PUT", "strike_price": 100, "strike_time": expiry,
             "open_interest": 250, "delta": -0.5, "implied_volatility": 28},
            {"code": "P3", "option_type": "PUT", "strike_price": 102, "strike_time": expiry,
             "open_interest": 120, "delta": -0.6, "implied_volatility": 26},
        ])

    def test_bullish_picks_call(self):
        leg = recommend_single_leg("bullish", self._chain(), price=100, expiry="2026-03-17")
        assert leg is not None
        assert leg.option_type == "call"
        assert leg.side == "buy"

    def test_bearish_picks_put(self):
        leg = recommend_single_leg("bearish", self._chain(), price=100, expiry="2026-03-17")
        assert leg is not None
        assert leg.option_type == "put"
        assert leg.side == "buy"

    def test_empty_chain(self):
        leg = recommend_single_leg("bullish", pd.DataFrame(), price=100, expiry="2026-03-17")
        assert leg is None


class TestRecommendSpread:
    def _chain(self, expiry="2026-03-17"):
        return pd.DataFrame([
            {"code": f"C{i}", "option_type": "CALL", "strike_price": 95 + i * 2,
             "strike_time": expiry, "open_interest": 100 + i * 50, "delta": 0.5 - i * 0.1}
            for i in range(5)
        ] + [
            {"code": f"P{i}", "option_type": "PUT", "strike_price": 95 + i * 2,
             "strike_time": expiry, "open_interest": 100 + i * 50, "delta": -0.5 + i * 0.1}
            for i in range(5)
        ])

    def test_bullish_spread(self):
        legs = recommend_spread("bullish", self._chain(), price=101, expiry="2026-03-17")
        assert legs is not None
        assert len(legs) == 2
        assert any(l.side == "sell" and l.option_type == "put" for l in legs)
        assert any(l.side == "buy" and l.option_type == "put" for l in legs)

    def test_bearish_spread(self):
        legs = recommend_spread("bearish", self._chain(), price=101, expiry="2026-03-17")
        assert legs is not None
        assert len(legs) == 2
        assert any(l.side == "sell" and l.option_type == "call" for l in legs)

    def test_empty_chain(self):
        assert recommend_spread("bullish", pd.DataFrame(), price=100, expiry="2026-03-17") is None


class TestShouldWait:
    def _regime(self, regime_type=RegimeType.BREAKOUT, confidence=0.8, rvol=1.5):
        return RegimeResult(
            regime=regime_type, confidence=confidence,
            rvol=rvol, price=100, vah=105, val=95, poc=100,
        )

    def _vp(self):
        return VolumeProfileResult(poc=100, vah=105, val=95)

    def _filters(self, tradeable=True):
        return FilterResult(tradeable=tradeable)

    def test_no_wait_breakout(self):
        wait, reasons, _ = should_wait(
            self._regime(), self._filters(), self._vp(),
            chain_available=True, expiry_available=True,
        )
        assert not wait

    def test_wait_filter_blocked(self):
        wait, reasons, _ = should_wait(
            self._regime(), self._filters(tradeable=False), self._vp(),
            chain_available=True, expiry_available=True,
        )
        assert wait

    def test_wait_unclear_low_confidence(self):
        wait, reasons, _ = should_wait(
            self._regime(RegimeType.UNCLEAR, confidence=0.3),
            self._filters(), self._vp(),
            chain_available=True, expiry_available=True,
        )
        assert wait

    def test_wait_whipsaw(self):
        wait, reasons, _ = should_wait(
            self._regime(RegimeType.WHIPSAW),
            self._filters(), self._vp(),
            chain_available=True, expiry_available=True,
        )
        assert wait

    def test_wait_low_rvol(self):
        wait, reasons, _ = should_wait(
            self._regime(rvol=0.3),
            self._filters(), self._vp(),
            chain_available=True, expiry_available=True,
        )
        assert wait

    def test_wait_no_chain(self):
        wait, reasons, _ = should_wait(
            self._regime(), self._filters(), self._vp(),
            chain_available=False, expiry_available=False,
        )
        assert wait


class TestRecommend:
    def _vp(self):
        return VolumeProfileResult(poc=100, vah=105, val=95)

    def _filters(self):
        return FilterResult(tradeable=True)

    def test_wait_when_unclear(self):
        regime = RegimeResult(
            regime=RegimeType.UNCLEAR, confidence=0.3,
            rvol=0.8, price=100, vah=105, val=95, poc=100,
        )
        rec = recommend(regime, self._vp(), self._filters())
        assert rec.action == "wait"

    def test_bullish_call_breakout(self):
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=108, vah=105, val=95, poc=100,
        )
        chain = pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 108,
             "strike_time": "2026-03-17", "open_interest": 200, "delta": 0.5},
            {"code": "C2", "option_type": "CALL", "strike_price": 110,
             "strike_time": "2026-03-17", "open_interest": 150, "delta": 0.35},
        ])
        dates = [{"strike_time": "2026-03-17"}]
        rec = recommend(regime, self._vp(), self._filters(),
                        chain_df=chain, expiry_dates=dates)
        assert rec.action == "call"
        assert rec.direction == "bullish"

    def test_no_chain_waits(self):
        """No chain data → must return wait (not degraded call/put)."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=108, vah=105, val=95, poc=100,
        )
        dates = [{"strike_time": "2026-03-17"}]
        rec = recommend(regime, self._vp(), self._filters(), expiry_dates=dates)
        assert rec.action == "wait"
        assert rec.direction == "bullish"  # direction hint preserved
        assert rec.liquidity_warning is not None

    def test_no_expiry_waits(self):
        """No valid expiry → must return wait."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=108, vah=105, val=95, poc=100,
        )
        chain = pd.DataFrame({
            "strike_price": [105, 110],
            "option_type": ["CALL", "CALL"],
            "strike_time": ["2026-03-17", "2026-03-17"],
            "open_interest": [100, 200],
        })
        rec = recommend(regime, self._vp(), self._filters(), chain_df=chain, expiry_dates=[])
        assert rec.action == "wait"

    def test_no_suitable_strike_waits(self):
        """Chain exists but no strike meets OI threshold → must return wait."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=108, vah=105, val=95, poc=100,
        )
        # All OI below threshold (10)
        chain = pd.DataFrame({
            "strike_price": [105, 110],
            "option_type": ["CALL", "CALL"],
            "strike_time": ["2026-03-17", "2026-03-17"],
            "open_interest": [1, 2],
        })
        dates = [{"strike_time": "2026-03-17"}]
        rec = recommend(regime, self._vp(), self._filters(), chain_df=chain, expiry_dates=dates)
        assert rec.action == "wait"
        assert rec.liquidity_warning is not None


# ── Telegram Handler Regex Tests ──

class TestTelegramRegex:
    """Test regex patterns for handler routing."""

    def test_query_plain_code(self):
        from src.hk.telegram import _RE_QUERY
        assert _RE_QUERY.match("09988")
        assert _RE_QUERY.match("00700")
        assert _RE_QUERY.match("800000")

    def test_query_hk_prefix(self):
        from src.hk.telegram import _RE_QUERY
        assert _RE_QUERY.match("HK09988")
        assert _RE_QUERY.match("HK.09988")
        assert _RE_QUERY.match("hk09988")

    def test_query_no_match_commands(self):
        from src.hk.telegram import _RE_QUERY
        assert not _RE_QUERY.match("/hk_help")
        assert not _RE_QUERY.match("AAPL")
        assert not _RE_QUERY.match("+09988")

    def test_add_pattern(self):
        from src.hk.telegram import _RE_ADD
        m = _RE_ADD.match("+09988 Alibaba")
        assert m
        assert m.group(1) == "09988"
        assert m.group(2) == "Alibaba"

    def test_add_no_name(self):
        from src.hk.telegram import _RE_ADD
        m = _RE_ADD.match("+09988")
        assert m
        assert m.group(2) == ""

    def test_remove_pattern(self):
        from src.hk.telegram import _RE_REMOVE
        m = _RE_REMOVE.match("-09988")
        assert m
        assert m.group(1) == "09988"

    def test_watchlist_pattern(self):
        from src.hk.telegram import _RE_WATCHLIST
        assert _RE_WATCHLIST.match("wl")
        assert _RE_WATCHLIST.match("WL")
        assert _RE_WATCHLIST.match("watchlist")
        assert _RE_WATCHLIST.match("Watchlist")
        assert not _RE_WATCHLIST.match("wl ")  # trailing space
