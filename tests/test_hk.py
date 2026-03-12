"""Tests for the HK Playbook module."""

import json
import os
import tempfile
import time

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timezone, timedelta

from src.hk import (
    RegimeType, VolumeProfileResult, GammaWallResult,
    RegimeResult, FilterResult, Playbook,
    OptionRecommendation, OptionLeg, ChaseRiskResult,
    OptionMarketSnapshot, QuoteSnapshot, SpreadMetrics,
    ScanSignal, ScanAlertRecord,
)
from src.hk.volume_profile import calculate_volume_profile
from src.hk.indicators import (
    calculate_vwap, calculate_vwap_series, calculate_rvol,
    get_today_bars, get_history_bars, is_trading_time,
)
from src.hk.regime import classify_regime, _intraday_trend
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
    assess_chase_risk,
    _is_positive_ev,
    _decide_direction,
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

    def test_recency_decay_narrows_va(self):
        """Recency decay should reduce influence of old days, narrowing VA after trend breaks."""
        # Day 1 (old): traded at 135-140
        # Day 2-3 (recent): traded at 125-131
        bars = _make_bars([
            ("2026-03-05 09:30:00", 137, 140, 135, 138, 100000),
            ("2026-03-05 10:00:00", 138, 140, 136, 139, 100000),
            ("2026-03-07 09:30:00", 128, 131, 125, 129, 100000),
            ("2026-03-07 10:00:00", 127, 130, 125, 128, 100000),
            ("2026-03-08 09:30:00", 127, 130, 126, 129, 100000),
            ("2026-03-08 10:00:00", 126, 129, 125, 127, 100000),
        ])
        vp_no_decay = calculate_volume_profile(bars, tick_size=1.0, recency_decay=0)
        vp_decay = calculate_volume_profile(bars, tick_size=1.0, recency_decay=0.3)
        # Without decay: VA spans both clusters → wide
        # With decay: old cluster (135-140) is down-weighted → VA narrows to recent range
        assert vp_decay.vah - vp_decay.val < vp_no_decay.vah - vp_no_decay.val

    def test_recency_decay_zero_is_noop(self):
        """recency_decay=0 should produce identical results to default."""
        bars = _make_bars([
            ("2026-03-07 09:30:00", 100, 102, 98, 101, 10000),
            ("2026-03-08 09:30:00", 101, 103, 99, 102, 10000),
        ])
        vp_default = calculate_volume_profile(bars, tick_size=1.0)
        vp_zero = calculate_volume_profile(bars, tick_size=1.0, recency_decay=0)
        assert vp_default.poc == vp_zero.poc
        assert vp_default.vah == vp_zero.vah
        assert vp_default.val == vp_zero.val


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

    # ── Issue 0: WHIPSAW overrides BREAKOUT ──

    def test_whipsaw_overrides_breakout(self):
        """WHIPSAW should take priority even when BREAKOUT conditions are also met."""
        gw = GammaWallResult(call_wall_strike=516, put_wall_strike=490, max_pain=500)
        # price=515 > VAH=510 (outside VA), rvol=1.5 > breakout_rvol
        # BUT IV spiking + near call wall (516) → should be WHIPSAW, not BREAKOUT
        result = classify_regime(
            price=515, rvol=1.5, vp=self._vp(),
            gamma_wall=gw, atm_iv=50, avg_iv=30, iv_spike_ratio=1.3,
        )
        assert result.regime == RegimeType.WHIPSAW

    def test_breakout_when_no_iv_spike(self):
        """Without IV spike, BREAKOUT should still work even near gamma wall."""
        gw = GammaWallResult(call_wall_strike=516, put_wall_strike=490, max_pain=500)
        result = classify_regime(
            price=515, rvol=1.5, vp=self._vp(),
            gamma_wall=gw, atm_iv=30, avg_iv=30, iv_spike_ratio=1.3,
        )
        assert result.regime == RegimeType.BREAKOUT

    # ── Issue 3: Multi-factor confidence ──

    def test_breakout_confidence_reduced_near_gamma_wall(self):
        """BREAKOUT near gamma wall should have lower confidence."""
        gw = GammaWallResult(call_wall_strike=516, put_wall_strike=490, max_pain=500)
        result_near = classify_regime(
            price=515, rvol=1.5, vp=self._vp(), gamma_wall=gw,
        )
        result_far = classify_regime(
            price=515, rvol=1.5, vp=self._vp(),
        )
        assert result_near.regime == RegimeType.BREAKOUT
        assert result_far.regime == RegimeType.BREAKOUT
        assert result_near.confidence < result_far.confidence

    def test_range_confidence_boosted_near_gamma_wall(self):
        """RANGE near gamma wall should have higher confidence (pinning effect)."""
        gw = GammaWallResult(call_wall_strike=505, put_wall_strike=490, max_pain=500)
        result_near = classify_regime(
            price=504, rvol=0.6, vp=self._vp(), gamma_wall=gw,
        )
        result_far = classify_regime(
            price=500, rvol=0.6, vp=self._vp(),
        )
        assert result_near.regime == RegimeType.RANGE
        assert result_far.regime == RegimeType.RANGE
        assert result_near.confidence > result_far.confidence

    def test_breakout_deep_has_higher_confidence(self):
        """Deeper breakout (farther from VA) should have slightly higher confidence."""
        result_shallow = classify_regime(price=511, rvol=1.5, vp=self._vp())
        result_deep = classify_regime(price=530, rvol=1.5, vp=self._vp())
        assert result_deep.confidence >= result_shallow.confidence

    # ── Issue 4: Put Wall detection ──

    def test_near_put_wall_only(self):
        """WHIPSAW should trigger when only put_wall_strike > 0 and price is near it."""
        gw = GammaWallResult(call_wall_strike=0, put_wall_strike=501, max_pain=500)
        result = classify_regime(
            price=500, rvol=1.0, vp=self._vp(),
            gamma_wall=gw, atm_iv=50, avg_iv=30, iv_spike_ratio=1.3,
        )
        assert result.regime == RegimeType.WHIPSAW

    def test_no_gamma_wall_no_whipsaw(self):
        """Without gamma wall, IV spike alone should not trigger WHIPSAW."""
        result = classify_regime(
            price=500, rvol=1.0, vp=self._vp(),
            atm_iv=50, avg_iv=30, iv_spike_ratio=1.3,
        )
        assert result.regime != RegimeType.WHIPSAW

    # ── RANGE confidence discount for wide intraday range ──

    def test_range_confidence_discounted_by_wide_intraday_range(self):
        """Wide intraday range (>30% of VA) should reduce RANGE confidence."""
        vp = self._vp()  # vah=510, val=490, VA range=20
        result_narrow = classify_regime(
            price=500, rvol=0.6, vp=vp, intraday_range=4.0,  # 20% of VA
        )
        result_wide = classify_regime(
            price=500, rvol=0.6, vp=vp, intraday_range=8.0,  # 40% of VA
        )
        assert result_narrow.regime == RegimeType.RANGE
        assert result_wide.regime == RegimeType.RANGE
        assert result_wide.confidence < result_narrow.confidence
        assert "振幅" in result_wide.details

    def test_range_no_discount_narrow_range(self):
        """Narrow intraday range (<=30% of VA) should not discount."""
        vp = self._vp()  # VA range=20
        result_no_range = classify_regime(price=500, rvol=0.6, vp=vp)
        result_narrow = classify_regime(
            price=500, rvol=0.6, vp=vp, intraday_range=5.0,  # 25% of VA
        )
        assert result_no_range.confidence == result_narrow.confidence

    def test_range_heavy_discount_very_wide(self):
        """Very wide intraday range should get capped discount."""
        vp = self._vp()  # VA range=20
        result = classify_regime(
            price=500, rvol=0.6, vp=vp, intraday_range=20.0,  # 100% of VA
        )
        assert result.regime == RegimeType.RANGE
        result_base = classify_regime(price=500, rvol=0.6, vp=vp)
        assert result_base.confidence - result.confidence >= 0.25

    # ── Momentum Breakout (Style A2) ──

    def test_momentum_breakout_above_vah(self):
        """Price >1% above VAH + low RVOL → Momentum BREAKOUT."""
        # price=520, vah=510 → dist = (520-510)/520*100 = 1.92%
        result = classify_regime(price=520, rvol=0.9, vp=self._vp(), momentum_min_dist_pct=1.0)
        assert result.regime == RegimeType.BREAKOUT
        assert "Momentum" in result.details
        assert 0.40 <= result.confidence <= 0.65

    def test_momentum_breakout_below_val(self):
        """Price >1% below VAL + low RVOL → Momentum BREAKOUT."""
        # price=480, val=490 → dist = (490-480)/480*100 = 2.08%
        result = classify_regime(price=480, rvol=0.9, vp=self._vp(), momentum_min_dist_pct=1.0)
        assert result.regime == RegimeType.BREAKOUT
        assert "Momentum" in result.details
        assert "below VAL" in result.details

    def test_momentum_breakout_distance_too_small(self):
        """Price outside VA but <1% → should NOT trigger momentum breakout."""
        # price=511, vah=510 → dist = (511-510)/511*100 = 0.196% < 1%
        result = classify_regime(price=511, rvol=0.9, vp=self._vp(), momentum_min_dist_pct=1.0)
        assert result.regime != RegimeType.BREAKOUT or "Momentum" not in result.details

    def test_momentum_breakout_volume_surge_boost(self):
        """Volume surge should add +0.10 to confidence."""
        vp = self._vp()
        result_no_surge = classify_regime(
            price=520, rvol=0.9, vp=vp, has_volume_surge=False, momentum_min_dist_pct=1.0,
        )
        result_surge = classify_regime(
            price=520, rvol=0.9, vp=vp, has_volume_surge=True, momentum_min_dist_pct=1.0,
        )
        assert result_surge.confidence > result_no_surge.confidence
        assert result_surge.confidence - result_no_surge.confidence == pytest.approx(0.10, abs=0.01)
        assert "volume surge" in result_surge.details

    def test_momentum_breakout_lower_than_traditional(self):
        """Momentum BREAKOUT confidence should be lower than traditional BREAKOUT."""
        vp = self._vp()
        # Traditional: high RVOL + outside VA
        trad = classify_regime(price=520, rvol=1.5, vp=vp)
        # Momentum: low RVOL + outside VA
        momentum = classify_regime(price=520, rvol=0.9, vp=vp, momentum_min_dist_pct=1.0)
        assert trad.regime == RegimeType.BREAKOUT
        assert momentum.regime == RegimeType.BREAKOUT
        assert trad.confidence > momentum.confidence

    def test_whipsaw_still_overrides_momentum(self):
        """WHIPSAW (IV spike + gamma wall) should still take priority over momentum."""
        gw = GammaWallResult(call_wall_strike=521, put_wall_strike=490, max_pain=500)
        result = classify_regime(
            price=520, rvol=0.9, vp=self._vp(),
            gamma_wall=gw, atm_iv=50, avg_iv=30, iv_spike_ratio=1.3,
            momentum_min_dist_pct=1.0,
        )
        assert result.regime == RegimeType.WHIPSAW

    def test_momentum_breakout_gamma_wall_penalty(self):
        """Near gamma wall should reduce momentum breakout confidence by 0.05."""
        vp = self._vp()
        gw = GammaWallResult(call_wall_strike=521, put_wall_strike=490, max_pain=500)
        result_no_gw = classify_regime(
            price=520, rvol=0.9, vp=vp, momentum_min_dist_pct=1.0,
        )
        # Not IV spiking, so no WHIPSAW, but near gamma wall
        result_gw = classify_regime(
            price=520, rvol=0.9, vp=vp, gamma_wall=gw, momentum_min_dist_pct=1.0,
        )
        assert result_no_gw.regime == RegimeType.BREAKOUT
        assert result_gw.regime == RegimeType.BREAKOUT
        assert result_no_gw.confidence > result_gw.confidence

    def test_momentum_breakout_rvol_near_threshold_boost(self):
        """RVOL closer to breakout threshold should get small boost."""
        vp = self._vp()
        # rvol=0.9 (near range_rvol=0.8) vs rvol=1.0 (closer to breakout_rvol=1.2)
        result_low = classify_regime(
            price=520, rvol=0.86, vp=vp, momentum_min_dist_pct=1.0,
            breakout_rvol=1.2, range_rvol=0.8,
        )
        result_high = classify_regime(
            price=520, rvol=1.15, vp=vp, momentum_min_dist_pct=1.0,
            breakout_rvol=1.2, range_rvol=0.8,
        )
        assert result_high.confidence >= result_low.confidence

    def test_existing_unclear_low_rvol_outside_va_unchanged(self):
        """Price outside VA + very low RVOL + small distance → still UNCLEAR (not momentum)."""
        # price=511, dist=0.196% < 1.0% threshold
        result = classify_regime(price=511, rvol=0.5, vp=self._vp())
        assert result.regime == RegimeType.UNCLEAR


# ── Volume Surge Detection Tests ──

class TestVolumeSurgeDetection:
    def test_no_surge_below_threshold(self):
        from src.hk.main import _detect_volume_surges
        bars = _make_bars([
            (f"2026-03-09 {9+i//60:02d}:{30+i%60:02d}:00", 100, 101, 99, 100, 1000)
            for i in range(15)
        ])
        assert _detect_volume_surges(bars) == []

    def test_surge_detected(self):
        from src.hk.main import _detect_volume_surges
        base = [
            (f"2026-03-09 {9+i//60:02d}:{30+i%60:02d}:00", 100, 101, 99, 100, 1000)
            for i in range(12)
        ]
        # Add 3 surge bars at the end
        base.append(("2026-03-09 09:42:00", 100, 102, 100, 101, 5000))
        base.append(("2026-03-09 09:43:00", 101, 103, 101, 102, 8000))
        base.append(("2026-03-09 09:44:00", 102, 104, 102, 103, 6000))
        bars = _make_bars(base)
        warnings = _detect_volume_surges(bars, threshold=3.0, recent_n=5)
        assert len(warnings) >= 1
        assert "量能突变" in warnings[0]

    def test_surge_not_detected_if_too_few_bars(self):
        from src.hk.main import _detect_volume_surges
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 10000),
        ])
        assert _detect_volume_surges(bars) == []


# ── VA Boundary Distance in Playbook Tests ──

class TestPlaybookVADistance:
    def test_va_distance_shown_in_risk_section(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=132.6, vah=135.0, val=130.0, poc=132.5,
        )
        vp = VolumeProfileResult(poc=132.5, vah=135.0, val=130.0)
        pb = generate_playbook(regime, vp, vwap=132.0)
        msg = format_playbook_message(pb, symbol="Test")
        assert "距关键位" in msg
        assert "VAH" in msg
        assert "VAL" in msg

    def test_va_distance_shows_percentage(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=132.6, vah=135.0, val=130.0, poc=132.5,
        )
        vp = VolumeProfileResult(poc=132.5, vah=135.0, val=130.0)
        pb = generate_playbook(regime, vp, vwap=132.0)
        msg = format_playbook_message(pb, symbol="Test")
        # VAH 135 is ~1.8% above 132.6
        assert "1.8%" in msg or "1.9%" in msg

    def test_va_distance_with_gamma_wall(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=500.0, vah=510.0, val=490.0, poc=500.0,
        )
        vp = VolumeProfileResult(poc=500, vah=510, val=490)
        gw = GammaWallResult(call_wall_strike=520, put_wall_strike=480, max_pain=500)
        pb = generate_playbook(regime, vp, vwap=500, gamma_wall=gw)
        msg = format_playbook_message(pb, symbol="Test")
        assert "Call Wall" in msg
        assert "Put Wall" in msg


# ── Extract IV Tests ──

class TestExtractIV:
    def test_median_baseline(self):
        """avg_iv should be median of all strikes, not atm_iv * 0.8."""
        from src.hk.main import HKPredictor
        chain = pd.DataFrame({
            "strike_price": [95, 97, 100, 103, 105, 110],
            "implied_volatility": [40, 35, 30, 28, 25, 20],
        })
        atm_iv, avg_iv = HKPredictor._extract_iv(chain, price=100)
        # Median of [40, 35, 30, 28, 25, 20] = 29.0
        assert abs(avg_iv - 29.0) < 1.0
        # ATM (4 nearest: 100=30, 97=35, 103=28, 95=40) mean = 33.25
        assert abs(atm_iv - 33.25) < 1.0

    def test_iv_spike_detectable(self):
        """When ATM IV is much higher than chain median, spike should be detectable."""
        from src.hk.main import HKPredictor
        chain = pd.DataFrame({
            "strike_price": [90, 95, 100, 105, 110, 115, 120],
            "implied_volatility": [20, 22, 60, 58, 21, 19, 18],
        })
        atm_iv, avg_iv = HKPredictor._extract_iv(chain, price=100)
        # ATM IV ~59 (high), median ~21 (low) → atm_iv > avg_iv * 1.3
        assert atm_iv > avg_iv * 1.3

    def test_empty_chain(self):
        from src.hk.main import HKPredictor
        atm_iv, avg_iv = HKPredictor._extract_iv(pd.DataFrame(), price=100)
        assert atm_iv == 0.0
        assert avg_iv == 0.0


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
            details="RVOL 0.60 < 0.80, price in value area",
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
        assert "区间震荡日" in msg
        assert "RVOL" in msg
        assert "POC" in msg
        assert "Call" in msg
        assert "505" in msg
        assert "2026-03-18" in msg
        assert "判断依据" in msg
        assert "失效条件" in msg

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

    def test_format_message_shows_full_realtime_snapshot(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.74,
            rvol=0.81, price=96.5, vah=97.0, val=92.5, poc=94.0,
            details="RVOL 0.81 < 0.95, price in value area",
        )
        vp = VolumeProfileResult(poc=94.0, vah=97.0, val=92.5)
        quote = QuoteSnapshot(
            symbol="HK.01211",
            last_price=96.5,
            open_price=96.2,
            high_price=97.1,
            low_price=95.8,
            prev_close=95.4,
            volume=1234567,
            turnover=234000000.0,
            bid_price=96.45,
            ask_price=96.5,
            amplitude=1.36,
            turnover_rate=0.42,
        )
        option_market = OptionMarketSnapshot(
            expiry="2026-03-13",
            contract_count=24,
            call_contract_count=12,
            put_contract_count=12,
            atm_iv=31.2,
            avg_iv=28.4,
            iv_ratio=1.10,
        )
        pb = generate_playbook(
            regime, vp, vwap=96.85, quote=quote, option_market=option_market,
        )
        msg = format_playbook_message(pb, symbol="BYD (HK.01211)")
        assert "96.50" in msg
        assert "买一" in msg
        assert "成交量" in msg
        assert "到期日 2026-03-13" in msg
        assert "ATM IV 31.20" in msg
        assert "判断依据" in msg
        assert "加分项" in msg

    def test_format_spread_recommendation_is_beginner_friendly(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.75,
            rvol=0.81, price=96.5, vah=97.0, val=92.5, poc=94.0,
            details="RVOL 0.81 < 0.95, price in value area",
        )
        vp = VolumeProfileResult(poc=94.0, vah=97.0, val=92.5)
        rec = OptionRecommendation(
            action="bear_call_spread",
            direction="bearish",
            expiry="2026-03-13",
            legs=[
                OptionLeg(
                    side="sell", option_type="call", strike=98.0,
                    pct_from_price=1.55, moneyness="OTM 1.6%",
                    delta=0.31, open_interest=320, last_price=1.25,
                    implied_volatility=30.2, volume=88,
                ),
                OptionLeg(
                    side="buy", option_type="call", strike=100.0,
                    pct_from_price=3.63, moneyness="OTM 3.6%",
                    delta=0.18, open_interest=210, last_price=0.66,
                    implied_volatility=29.7, volume=54,
                ),
            ],
            rationale="Regime: 区间震荡; 价格 96.50 靠近 VAH 97.00, 高抛机会; 震荡市适合使用价差策略, 利用时间价值衰减",
            risk_note="止损: 突破 VAH 97.00; 失效条件: 带量突破 VA 边界转为 BREAKOUT",
        )
        pb = generate_playbook(regime, vp, vwap=96.85, option_rec=rec)
        msg = format_playbook_message(pb, symbol="BYD (HK.01211)")
        assert "白话解释:" in msg
        assert "执行: 组合单一次提交" in msg
        assert "卖 CALL 98" in msg
        assert "Δ +0.31" in msg
        assert "触发后怎么做: 直接把整组 Bear Call Spread 一次性平仓" in msg


# ── SpreadMetrics Tests ──

class TestSpreadMetrics:
    def test_calculate_spread_metrics_bear_call(self):
        from src.hk.option_recommend import _calculate_spread_metrics
        legs = [
            OptionLeg(
                side="sell", option_type="call", strike=98.0,
                pct_from_price=1.0, moneyness="OTM 1.0%",
                delta=0.43, last_price=1.390,
            ),
            OptionLeg(
                side="buy", option_type="call", strike=100.0,
                pct_from_price=3.6, moneyness="OTM 3.6%",
                delta=0.24, last_price=0.660,
            ),
        ]
        sm = _calculate_spread_metrics(legs, "bear_call_spread")
        assert sm is not None
        assert abs(sm.net_credit - 0.730) < 0.001
        assert abs(sm.max_loss - 1.270) < 0.001
        assert abs(sm.breakeven - 98.730) < 0.001
        assert sm.risk_reward_ratio > 0
        assert sm.win_probability > 0

    def test_calculate_spread_metrics_bull_put(self):
        from src.hk.option_recommend import _calculate_spread_metrics
        legs = [
            OptionLeg(
                side="sell", option_type="put", strike=95.0,
                pct_from_price=2.0, moneyness="OTM 2.0%",
                delta=-0.35, last_price=1.200,
            ),
            OptionLeg(
                side="buy", option_type="put", strike=93.0,
                pct_from_price=4.0, moneyness="OTM 4.0%",
                delta=-0.20, last_price=0.500,
            ),
        ]
        sm = _calculate_spread_metrics(legs, "bull_put_spread")
        assert sm is not None
        assert abs(sm.net_credit - 0.700) < 0.001
        assert abs(sm.breakeven - 94.300) < 0.001

    def test_calculate_spread_metrics_no_price(self):
        from src.hk.option_recommend import _calculate_spread_metrics
        legs = [
            OptionLeg(side="sell", option_type="call", strike=98.0,
                      pct_from_price=1.0, moneyness="OTM", last_price=0),
            OptionLeg(side="buy", option_type="call", strike=100.0,
                      pct_from_price=3.0, moneyness="OTM", last_price=0),
        ]
        assert _calculate_spread_metrics(legs, "bear_call_spread") is None


class TestPlaybookSpreadPnL:
    def test_spread_pnl_shown_in_message(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.75,
            rvol=0.81, price=96.5, vah=97.0, val=92.5, poc=94.0,
        )
        vp = VolumeProfileResult(poc=94.0, vah=97.0, val=92.5)
        sm = SpreadMetrics(
            net_credit=0.730, max_profit=0.730, max_loss=1.270,
            breakeven=98.730, risk_reward_ratio=0.575, win_probability=0.57,
        )
        rec = OptionRecommendation(
            action="bear_call_spread", direction="bearish",
            expiry="2026-03-13", dte=3,
            legs=[
                OptionLeg(side="sell", option_type="call", strike=98.0,
                          pct_from_price=1.55, moneyness="OTM 1.6%",
                          delta=0.43, open_interest=491, last_price=1.390,
                          implied_volatility=51.7, volume=332),
                OptionLeg(side="buy", option_type="call", strike=100.0,
                          pct_from_price=3.63, moneyness="OTM 3.6%",
                          delta=0.24, open_interest=289, last_price=0.660,
                          implied_volatility=52.7, volume=236),
            ],
            spread_metrics=sm,
            rationale="Regime 区间震荡",
            risk_note="止损: 突破 VAH 97.00",
        )
        pb = generate_playbook(regime, vp, vwap=96.80, option_rec=rec)
        msg = format_playbook_message(pb, symbol="BYD (HK.01211)")
        assert "Spread 损益" in msg
        assert "净收入 0.730" in msg
        assert "最大亏损 1.270" in msg
        assert "盈亏平衡 98.730" in msg
        assert "R:R" in msg

    def test_position_size_shown(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.62,
            rvol=0.76, price=96.55, vah=97.0, val=92.5, poc=94.0,
        )
        vp = VolumeProfileResult(poc=94.0, vah=97.0, val=92.5)
        rec = OptionRecommendation(
            action="call", direction="bullish",
            expiry="2026-03-18",
            legs=[OptionLeg(side="buy", option_type="call", strike=97,
                            pct_from_price=0.5, moneyness="OTM 0.5%")],
            rationale="test",
        )
        pb = generate_playbook(regime, vp, vwap=96.8, option_rec=rec)
        msg = format_playbook_message(pb, symbol="Test")
        assert "仓位参考:" in msg
        assert "50%" in msg

    def test_dte_gamma_warning_in_risk(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=96.5, vah=97.0, val=92.5, poc=94.0,
        )
        vp = VolumeProfileResult(poc=94.0, vah=97.0, val=92.5)
        rec = OptionRecommendation(
            action="call", direction="bullish",
            expiry="2026-03-13", dte=3,
            legs=[OptionLeg(side="buy", option_type="call", strike=97,
                            pct_from_price=0.5, moneyness="OTM 0.5%")],
            rationale="test",
        )
        pb = generate_playbook(regime, vp, vwap=96.8, option_rec=rec)
        msg = format_playbook_message(pb, symbol="Test")
        assert "3 DTE" in msg
        assert "Gamma" in msg

    def test_stop_loss_uses_breakeven_for_spread(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.75,
            rvol=0.81, price=96.5, vah=97.0, val=92.5, poc=94.0,
        )
        vp = VolumeProfileResult(poc=94.0, vah=97.0, val=92.5)
        sm = SpreadMetrics(
            net_credit=0.730, max_profit=0.730, max_loss=1.270,
            breakeven=98.730, risk_reward_ratio=0.575, win_probability=0.57,
        )
        rec = OptionRecommendation(
            action="bear_call_spread", direction="bearish",
            expiry="2026-03-13", dte=3,
            legs=[
                OptionLeg(side="sell", option_type="call", strike=98.0,
                          pct_from_price=1.55, moneyness="OTM 1.6%",
                          delta=0.43, last_price=1.390),
                OptionLeg(side="buy", option_type="call", strike=100.0,
                          pct_from_price=3.63, moneyness="OTM 3.6%",
                          delta=0.24, last_price=0.660),
            ],
            spread_metrics=sm,
            rationale="test",
            risk_note="test risk",
        )
        pb = generate_playbook(regime, vp, vwap=96.80, option_rec=rec)
        msg = format_playbook_message(pb, symbol="Test")
        # Stop loss references breakeven, not VAH
        assert "盈亏平衡 98.73" in msg
        assert "最大亏损 1.270" in msg


class TestIVInterpretation:
    def test_high_iv_ratio_seller_strategy(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=500, vah=510, val=490, poc=500,
        )
        vp = VolumeProfileResult(poc=500, vah=510, val=490)
        option_market = OptionMarketSnapshot(
            expiry="2026-03-13", contract_count=24,
            call_contract_count=12, put_contract_count=12,
            atm_iv=60.0, avg_iv=48.0, iv_ratio=1.25,
        )
        pb = generate_playbook(regime, vp, vwap=502, option_market=option_market)
        msg = format_playbook_message(pb, symbol="Test")
        assert "卖方策略" in msg

    def test_low_iv_ratio_cheap_options(self):
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=500, vah=510, val=490, poc=500,
        )
        vp = VolumeProfileResult(poc=500, vah=510, val=490)
        option_market = OptionMarketSnapshot(
            expiry="2026-03-13", contract_count=24,
            call_contract_count=12, put_contract_count=12,
            atm_iv=30.0, avg_iv=38.0, iv_ratio=0.79,
        )
        pb = generate_playbook(regime, vp, vwap=502, option_market=option_market)
        msg = format_playbook_message(pb, symbol="Test")
        assert "期权定价相对便宜" in msg


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
        assert "最优卖价" in text
        assert "盘口结论" in text

    def test_format_alerts_empty(self):
        assert format_alerts_message([]) == ""

    def test_format_alerts_has_interpretation(self):
        alerts = analyze_order_book(self._book(), large_order_ratio=3.0)
        text = format_alerts_message(alerts)
        assert "盘口异常检测" in text
        assert "解读" in text


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
        msg = format_gamma_wall_message(gw, "HSI", current_price=25100)
        assert "25,200" in msg
        assert "24,800" in msg
        assert "HSI" in msg
        assert "上方阻力" in msg
        assert "解读" in msg


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

    def test_get_history_bars_max_trading_days(self):
        """max_trading_days should truncate to most recent N days."""
        bars = _make_bars([
            ("2026-03-05 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-06 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-07 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-08 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 1000),  # today
        ])
        # Without cap: 4 history days
        hist_all = get_history_bars(bars)
        assert len(set(hist_all.index.date)) == 4
        # With cap: 2 most recent history days (03-07, 03-08)
        hist_2 = get_history_bars(bars, max_trading_days=2)
        dates = sorted(set(hist_2.index.date))
        assert len(dates) == 2
        assert dates[0] == pd.Timestamp("2026-03-07").date()

    def test_get_history_bars_max_no_cap(self):
        """max_trading_days=0 should return all history days."""
        bars = _make_bars([
            ("2026-03-06 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-07 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 1000),
        ])
        hist = get_history_bars(bars, max_trading_days=0)
        assert len(set(hist.index.date)) == 2


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


# ── Chase Risk Assessment Tests ──

class TestAssessChaseRisk:
    def _vp(self, poc=500, vah=510, val=490):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_inside_va_near_vwap_none(self):
        """Price inside VA, near VWAP → no chase risk."""
        result = assess_chase_risk(
            price=505, vwap=503, vp=self._vp(), direction="bullish",
        )
        assert result.level == "none"

    def test_va_dist_moderate(self):
        """Price 2.5%+ above VAH with bullish direction → moderate."""
        # VAH=510, price=523.5 → dist = 2.65%
        result = assess_chase_risk(
            price=523.5, vwap=520, vp=self._vp(), direction="bullish",
        )
        assert result.level == "moderate"
        assert result.va_dist_pct > 2.5

    def test_va_dist_high(self):
        """Price 4%+ above VAH → high (00700 case: price=545, VAH=520)."""
        vp = self._vp(poc=515, vah=520, val=510)
        result = assess_chase_risk(
            price=545, vwap=536, vp=vp, direction="bullish",
        )
        assert result.level == "high"
        assert result.va_dist_pct >= 4.0

    def test_vwap_dev_high(self):
        """VWAP deviation 3.5%+ → high."""
        result = assess_chase_risk(
            price=520, vwap=500, vp=self._vp(), direction="bullish",
        )
        assert result.level == "high"
        assert result.vwap_dev_pct >= 3.5

    def test_direction_not_aligned_no_risk(self):
        """Bearish direction + price above VAH → no risk (not chasing)."""
        result = assess_chase_risk(
            price=525, vwap=520, vp=self._vp(), direction="bearish",
        )
        assert result.level == "none"

    def test_vwap_zero_skip(self):
        """VWAP=0 → skip assessment, return none."""
        result = assess_chase_risk(
            price=525, vwap=0, vp=self._vp(), direction="bullish",
        )
        assert result.level == "none"

    def test_afternoon_tighten(self):
        """Afternoon should tighten thresholds by 0.5%."""
        vp = self._vp(poc=500, vah=510, val=490)
        # Price 520 → va_dist from VAH = 1.96%, normally below 2.5 → none
        # With afternoon tighten (2.5 - 0.5 = 2.0), 1.96% is still < 2.0 → none
        # Price 522 → va_dist = 2.35%, < 2.5 normally → none, but >= 2.0 afternoon → moderate
        result = assess_chase_risk(
            price=522, vwap=520, vp=vp, direction="bullish",
            is_afternoon=True,
        )
        assert result.level == "moderate"
        # Same price without afternoon → none
        result_morning = assess_chase_risk(
            price=522, vwap=520, vp=vp, direction="bullish",
            is_afternoon=False,
        )
        assert result_morning.level == "none"

    def test_pullback_target_is_vwap(self):
        """pullback_target should equal VWAP."""
        result = assess_chase_risk(
            price=545, vwap=536, vp=self._vp(poc=515, vah=520, val=510),
            direction="bullish",
        )
        assert result.pullback_target == 536

    def test_bearish_below_val(self):
        """Bearish + price below VAL → chase risk on short side."""
        vp = self._vp(poc=500, vah=510, val=490)
        # val=490, price=469 → dist = (490-469)/490 = 4.29% → high
        result = assess_chase_risk(
            price=469, vwap=480, vp=vp, direction="bearish",
        )
        assert result.level == "high"
        assert result.va_dist_pct >= 4.0


# ── Recommend with Chase Risk Integration Tests ──

class TestRecommendChaseRisk:
    def _vp(self):
        return VolumeProfileResult(poc=515, vah=520, val=510)

    def _filters(self):
        return FilterResult(tradeable=True)

    def _chain(self, expiry="2026-03-17"):
        return pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 540,
             "strike_time": expiry, "open_interest": 200, "delta": 0.5},
            {"code": "C2", "option_type": "CALL", "strike_price": 545,
             "strike_time": expiry, "open_interest": 150, "delta": 0.4},
            {"code": "C3", "option_type": "CALL", "strike_price": 550,
             "strike_time": expiry, "open_interest": 100, "delta": 0.3},
        ])

    def test_high_chase_returns_wait(self):
        """High chase risk → action=wait, direction preserved."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=545, vah=520, val=510, poc=515,
        )
        dates = [{"strike_time": "2026-03-17"}]
        rec = recommend(
            regime, self._vp(), self._filters(),
            chain_df=self._chain(), expiry_dates=dates,
            vwap=536, chase_risk_cfg={"va_high_pct": 4.0},
        )
        assert rec.action == "wait"
        assert rec.direction == "bullish"
        assert "追高" in rec.rationale or "延伸" in rec.rationale

    def test_moderate_chase_prefers_atm(self):
        """Moderate chase risk → call action, risk_note contains chase warning."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=525, vah=520, val=510, poc=515,
        )
        # va_dist = (525-520)/520 = 0.96% → below moderate threshold
        # But use low thresholds to trigger moderate
        dates = [{"strike_time": "2026-03-17"}]
        chain = pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 524,
             "strike_time": "2026-03-17", "open_interest": 200, "delta": 0.5},
            {"code": "C2", "option_type": "CALL", "strike_price": 526,
             "strike_time": "2026-03-17", "open_interest": 150, "delta": 0.4},
        ])
        rec = recommend(
            regime, self._vp(), self._filters(),
            chain_df=chain, expiry_dates=dates,
            vwap=520,
            chase_risk_cfg={"vwap_moderate_pct": 0.5, "va_moderate_pct": 0.5,
                            "vwap_high_pct": 10.0, "va_high_pct": 10.0},
        )
        assert rec.action == "call"
        assert "追高" in rec.risk_note

    def test_no_chase_inside_va(self):
        """Inside VA, no chase risk → normal behavior."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=516, vah=520, val=510, poc=515,
        )
        chain = pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 515,
             "strike_time": "2026-03-17", "open_interest": 200, "delta": 0.5},
        ])
        dates = [{"strike_time": "2026-03-17"}]
        rec = recommend(
            regime, self._vp(), self._filters(),
            chain_df=chain, expiry_dates=dates,
            vwap=514,
        )
        assert rec.action == "call"
        assert "追高" not in rec.risk_note


# ── Auto-Scan Tests ──

class TestScanWindow:
    """Test _get_scan_window static method."""

    def test_morning_window(self):
        from src.hk.main import HKPredictor
        cfg = {
            "morning_window": ["09:35", "12:00"],
            "afternoon_window": ["13:05", "15:45"],
        }
        # 10:00 HKT → morning
        t = datetime(2026, 3, 10, 10, 0, tzinfo=HKT)  # Tuesday
        in_w, session = HKPredictor._get_scan_window(cfg, t)
        assert in_w is True
        assert session == "morning"

    def test_afternoon_window(self):
        from src.hk.main import HKPredictor
        cfg = {
            "morning_window": ["09:35", "12:00"],
            "afternoon_window": ["13:05", "15:45"],
        }
        t = datetime(2026, 3, 10, 14, 0, tzinfo=HKT)  # Tuesday
        in_w, session = HKPredictor._get_scan_window(cfg, t)
        assert in_w is True
        assert session == "afternoon"

    def test_lunch_break_not_in_window(self):
        from src.hk.main import HKPredictor
        cfg = {
            "morning_window": ["09:35", "12:00"],
            "afternoon_window": ["13:05", "15:45"],
        }
        t = datetime(2026, 3, 10, 12, 30, tzinfo=HKT)
        in_w, _ = HKPredictor._get_scan_window(cfg, t)
        assert in_w is False

    def test_weekend_not_in_window(self):
        from src.hk.main import HKPredictor
        cfg = {
            "morning_window": ["09:35", "12:00"],
            "afternoon_window": ["13:05", "15:45"],
        }
        # Saturday
        t = datetime(2026, 3, 14, 10, 0, tzinfo=HKT)
        in_w, _ = HKPredictor._get_scan_window(cfg, t)
        assert in_w is False

    def test_before_open_not_in_window(self):
        from src.hk.main import HKPredictor
        cfg = {
            "morning_window": ["09:35", "12:00"],
            "afternoon_window": ["13:05", "15:45"],
        }
        t = datetime(2026, 3, 10, 9, 0, tzinfo=HKT)
        in_w, _ = HKPredictor._get_scan_window(cfg, t)
        assert in_w is False


class TestL1Screen:
    """Test L1 lightweight screening logic."""

    def _make_predictor(self, scan_cfg=None):
        from unittest.mock import MagicMock, patch
        from src.hk.main import HKPredictor

        with patch.object(HKPredictor, '__init__', lambda self, *a, **kw: None):
            p = HKPredictor.__new__(HKPredictor)
            p._cfg = {
                "auto_scan": scan_cfg or {
                    "breakout": {
                        "min_confidence": 0.72,
                        "min_rvol": 1.35,
                        "min_magnitude_pct": 0.15,
                        "volume_surge_threshold": 2.0,
                        "volume_surge_bars": 5,
                    },
                    "range": {
                        "min_confidence": 0.72,
                        "rvol_min": 0.55,
                        "rvol_max": 0.90,
                        "va_proximity_pct": 0.30,
                    },
                },
                "volume_profile": {"value_area_pct": 0.70, "recency_decay": 0.15},
                "rvol": {"lookback_days": 10},
                "regime": {"breakout_rvol": 1.05, "range_rvol": 0.95},
            }
            p.watchlist = MagicMock()
            p._vp_cache = {}
            p._scan_history = {}
            p._scan_history_date = ""

            async def run_sync(fn, *args):
                return fn(*args)
            p._run_sync = run_sync
            p._collector = MagicMock()

            return p

    @pytest.mark.asyncio
    async def test_breakout_l1_pass(self):
        """High RVOL + price above VAH + magnitude → BREAKOUT L1 pass."""
        p = self._make_predictor()

        # Price 525, clearly above VAH ~521
        p._collector.get_quote = lambda sym: {
            "last_price": 525, "high_price": 530, "low_price": 520, "turnover": 5e9,
        }

        # Bars: today 3x volume → RVOL ≈ 3.0 (well above 1.35)
        # Generate enough bars for volume surge detection (needs >=10)
        today_bars = _make_bars([
            (f"2026-03-10 09:{30+i}:00", 520+i*0.5, 521+i*0.5, 519+i*0.5, 520.5+i*0.5, 3000)
            for i in range(12)
        ])
        hist_bars = _make_bars([
            (f"2026-03-09 09:{30+i}:00", 515+i*0.2, 516+i*0.2, 514+i*0.2, 515.5+i*0.2, 1000)
            for i in range(12)
        ])

        async def mock_bars(sym, cfg):
            return hist_bars, today_bars
        p._get_bars_cached = mock_bars

        result = await p._l1_screen("HK.00700", "morning", p._cfg["auto_scan"])
        assert result is not None
        assert result["signal_type"] == "BREAKOUT"
        assert result["direction"] == "bullish"
        assert len(result["trigger_reasons"]) > 0

    @pytest.mark.asyncio
    async def test_low_rvol_l1_reject(self):
        """Low RVOL → L1 rejects."""
        p = self._make_predictor()
        p._collector.get_quote = lambda sym: {
            "last_price": 515, "high_price": 520, "low_price": 510, "turnover": 5e9,
        }

        # Equal volume → RVOL ≈ 1.0
        today_bars = _make_bars([
            (f"2026-03-10 09:{30+i}:00", 515, 516, 514, 515, 1000)
            for i in range(12)
        ])
        hist_bars = _make_bars([
            (f"2026-03-09 09:{30+i}:00", 515, 516, 514, 515, 1000)
            for i in range(12)
        ])

        async def mock_bars(sym, cfg):
            return hist_bars, today_bars
        p._get_bars_cached = mock_bars

        result = await p._l1_screen("HK.00700", "morning", p._cfg["auto_scan"])
        assert result is None

    @pytest.mark.asyncio
    async def test_range_blocked_in_afternoon(self):
        """RANGE signal in afternoon → L1 rejects."""
        p = self._make_predictor()
        # Price inside VA, near VAL → would trigger RANGE in morning
        p._collector.get_quote = lambda sym: {
            "last_price": 515.5, "high_price": 516, "low_price": 515, "turnover": 5e9,
        }

        # Low volume → RVOL low → RANGE regime
        today_bars = _make_bars([
            (f"2026-03-10 09:{30+i}:00", 515, 516, 514, 515.5, 500)
            for i in range(12)
        ])
        hist_bars = _make_bars([
            (f"2026-03-09 09:{30+i}:00", 515, 521, 514, 518, 1000)
            for i in range(12)
        ])

        async def mock_bars(sym, cfg):
            return hist_bars, today_bars
        p._get_bars_cached = mock_bars

        result = await p._l1_screen("HK.00700", "afternoon", p._cfg["auto_scan"])
        assert result is None

    @pytest.mark.asyncio
    async def test_breakout_needs_enhanced_condition(self):
        """BREAKOUT without magnitude or surge → L1 rejects."""
        p = self._make_predictor()

        # hist_bars: 515-521 range → VP computes VAH ~519
        # Price 519.5: barely above VAH, magnitude = (519.5-519)/519.5 ≈ 0.096% < 0.15%
        p._collector.get_quote = lambda sym: {
            "last_price": 519.5, "high_price": 520, "low_price": 519, "turnover": 5e9,
        }

        # High volume but uniform (no surge) — enough bars for detection
        today_bars = _make_bars([
            (f"2026-03-10 09:{30+i}:00", 519, 520, 518, 519.5, 3000)
            for i in range(12)
        ])
        hist_bars = _make_bars([
            (f"2026-03-09 09:{30+i}:00", 515, 521, 514, 518, 1000)
            for i in range(12)
        ])

        async def mock_bars(sym, cfg):
            return hist_bars, today_bars
        p._get_bars_cached = mock_bars

        result = await p._l1_screen("HK.00700", "morning", p._cfg["auto_scan"])
        # Should reject because magnitude < 0.15% and no volume surge
        assert result is None


class TestFrequencyControl:
    """Test 3-layer frequency control with override exceptions."""

    def _make_predictor(self):
        from unittest.mock import MagicMock, patch
        from src.hk.main import HKPredictor

        with patch.object(HKPredictor, '__init__', lambda self, *a, **kw: None):
            p = HKPredictor.__new__(HKPredictor)
            p._scan_history = {}
            p._scan_history_date = "2026-03-10"
            return p

    def _make_signal(self, signal_type="BREAKOUT", direction="bullish",
                     confidence=0.80, price=525.0):
        from src.hk import ScanSignal
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT if signal_type == "BREAKOUT" else RegimeType.RANGE,
            confidence=confidence, rvol=1.5, price=price,
            vah=521, val=514, poc=518,
        )
        return ScanSignal(
            signal_type=signal_type,
            direction=direction,
            symbol="HK.00700",
            regime=regime,
            price=price,
            trigger_reasons=["test"],
            timestamp=time.time(),
        )

    def _default_scan_cfg(self):
        return {
            "cooldown": {
                "same_signal_minutes": 30,
                "max_per_session": 2,
                "max_per_day": 3,
            },
            "override": {
                "confidence_increase": 0.10,
                "price_extension_pct": 0.50,
                "regime_upgrade": True,
            },
        }

    def test_first_signal_allowed(self):
        p = self._make_predictor()
        signal = self._make_signal()
        allowed, override = p._check_frequency("HK.00700", signal, "morning", self._default_scan_cfg())
        assert allowed is True
        assert override is None

    def test_same_signal_within_cooldown_blocked(self):
        from src.hk import ScanAlertRecord
        p = self._make_predictor()
        # Record a recent alert
        p._scan_history["HK.00700"] = [
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                confidence=0.80, price=525, timestamp=time.time(), session="morning",
            )
        ]
        signal = self._make_signal(confidence=0.80, price=525)
        allowed, _ = p._check_frequency("HK.00700", signal, "morning", self._default_scan_cfg())
        assert allowed is False

    def test_max_per_session_blocked(self):
        from src.hk import ScanAlertRecord
        p = self._make_predictor()
        now = time.time()
        # 2 alerts already in morning session (different directions, so layer 1 doesn't block)
        p._scan_history["HK.00700"] = [
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                confidence=0.80, price=525, timestamp=now - 2000, session="morning",
            ),
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bearish",
                confidence=0.75, price=510, timestamp=now - 1000, session="morning",
            ),
        ]
        # New signal: different direction from last, so layer 1 doesn't block
        # But session limit (2) is reached → blocked (no regime upgrade since both BREAKOUT)
        signal = self._make_signal(signal_type="BREAKOUT", direction="bullish", price=528)
        allowed, _ = p._check_frequency("HK.00700", signal, "morning", self._default_scan_cfg())
        assert allowed is False

    def test_max_per_day_blocked(self):
        from src.hk import ScanAlertRecord
        p = self._make_predictor()
        now = time.time()
        # 3 alerts already today
        p._scan_history["HK.00700"] = [
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                confidence=0.80, price=525, timestamp=now - 10000, session="morning",
            ),
            ScanAlertRecord(
                symbol="HK.00700", signal_type="RANGE", direction="bearish",
                confidence=0.75, price=520, timestamp=now - 8000, session="morning",
            ),
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                confidence=0.85, price=530, timestamp=now - 3000, session="afternoon",
            ),
        ]
        signal = self._make_signal(direction="bearish", price=510)
        allowed, _ = p._check_frequency("HK.00700", signal, "afternoon", self._default_scan_cfg())
        assert allowed is False

    def test_override_confidence_increase(self):
        from src.hk import ScanAlertRecord
        p = self._make_predictor()
        p._scan_history["HK.00700"] = [
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                confidence=0.72, price=525, timestamp=time.time(), session="morning",
            )
        ]
        # Confidence jumped from 0.72 to 0.85 (delta = 0.13 >= 0.10)
        signal = self._make_signal(confidence=0.85, price=525)
        allowed, override = p._check_frequency("HK.00700", signal, "morning", self._default_scan_cfg())
        assert allowed is True
        assert override is not None
        assert "置信度" in override

    def test_override_price_extension(self):
        from src.hk import ScanAlertRecord
        p = self._make_predictor()
        p._scan_history["HK.00700"] = [
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                confidence=0.80, price=525, timestamp=time.time(), session="morning",
            )
        ]
        # Price extended 0.6% from 525 to 528.15 (>= 0.50%)
        signal = self._make_signal(confidence=0.80, price=528.15)
        allowed, override = p._check_frequency("HK.00700", signal, "morning", self._default_scan_cfg())
        assert allowed is True
        assert override is not None
        assert "扩展" in override

    def test_override_regime_upgrade(self):
        """Session limit reached with last signal being RANGE → BREAKOUT upgrade overrides."""
        from src.hk import ScanAlertRecord
        p = self._make_predictor()
        now = time.time()
        # 2 alerts in morning session (session limit reached), last one is RANGE
        p._scan_history["HK.00700"] = [
            ScanAlertRecord(
                symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                confidence=0.80, price=525, timestamp=now - 2000, session="morning",
            ),
            ScanAlertRecord(
                symbol="HK.00700", signal_type="RANGE", direction="bearish",
                confidence=0.75, price=515, timestamp=now - 1000, session="morning",
            ),
        ]
        # New BREAKOUT signal → regime upgrade overrides session limit
        signal = self._make_signal(signal_type="BREAKOUT", direction="bullish", price=530)
        allowed, override = p._check_frequency("HK.00700", signal, "morning", self._default_scan_cfg())
        assert allowed is True
        assert override is not None
        assert "BREAKOUT" in override

    def test_daily_reset(self):
        from src.hk import ScanAlertRecord
        from unittest.mock import patch
        from src.hk.main import HKPredictor

        with patch.object(HKPredictor, '__init__', lambda self, *a, **kw: None):
            p = HKPredictor.__new__(HKPredictor)
            p._scan_history = {"HK.00700": [
                ScanAlertRecord(
                    symbol="HK.00700", signal_type="BREAKOUT", direction="bullish",
                    confidence=0.80, price=525, timestamp=time.time(), session="morning",
                )
            ]}
            p._scan_history_date = "2026-03-09"  # yesterday

            p._reset_scan_history_if_new_day()
            assert len(p._scan_history) == 0
            assert p._scan_history_date == datetime.now(HKT).strftime("%Y-%m-%d")


class TestScanHeader:
    """Test scan alert header formatting."""

    def test_breakout_header(self):
        from src.hk.main import HKPredictor
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.82, rvol=1.52,
            price=525, vah=521, val=514, poc=518,
        )
        signal = ScanSignal(
            signal_type="BREAKOUT", direction="bullish", symbol="HK.00700",
            regime=regime, price=525,
            trigger_reasons=["突破 VAH 0.32%", "最近 5 根 bar 量能突变"],
        )
        header = HKPredictor._format_scan_header(signal, "normal", None, 30)
        assert "BREAKOUT 强信号" in header
        assert "看多" in header
        assert "突破 VAH" in header
        assert "30 分钟" in header
        assert "当前状态" in header
        assert "是否还能追" in header

    def test_elevated_risk_marker(self):
        from src.hk.main import HKPredictor
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.75, rvol=0.70,
            price=515, vah=521, val=514, poc=518,
        )
        signal = ScanSignal(
            signal_type="RANGE", direction="bullish", symbol="HK.00700",
            regime=regime, price=515, trigger_reasons=["接近 VAL"],
        )
        header = HKPredictor._format_scan_header(signal, "elevated", None, 30)
        assert "风险偏高" in header
        assert "触发原因" in header

    def test_override_reason_shown(self):
        from src.hk.main import HKPredictor
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.90, rvol=1.6,
            price=530, vah=521, val=514, poc=518,
        )
        signal = ScanSignal(
            signal_type="BREAKOUT", direction="bullish", symbol="HK.00700",
            regime=regime, price=530, trigger_reasons=["突破 VAH 0.50%"],
        )
        header = HKPredictor._format_scan_header(signal, "normal", "置信度提升 13%", 30)
        assert "冷却期覆盖" in header
        assert "置信度提升" in header


class TestAutoScanIntegration:
    """Integration-level tests for run_auto_scan."""

    @pytest.mark.asyncio
    async def test_disabled_scan_does_nothing(self):
        """auto_scan.enabled=false → no scan."""
        from unittest.mock import MagicMock, patch
        from src.hk.main import HKPredictor

        sent = []
        async def mock_send(msg):
            sent.append(msg)

        with patch.object(HKPredictor, '__init__', lambda self, *a, **kw: None):
            p = HKPredictor.__new__(HKPredictor)
            p._cfg = {"auto_scan": {"enabled": False}}
            p._scan_history = {}
            p._scan_history_date = ""
            await p.run_auto_scan(mock_send)

        assert len(sent) == 0

    @pytest.mark.asyncio
    async def test_outside_window_does_nothing(self):
        """Outside trading window → no scan."""
        from unittest.mock import MagicMock, patch
        from src.hk.main import HKPredictor

        sent = []
        async def mock_send(msg):
            sent.append(msg)

        with patch.object(HKPredictor, '__init__', lambda self, *a, **kw: None):
            p = HKPredictor.__new__(HKPredictor)
            p._cfg = {
                "auto_scan": {
                    "enabled": True,
                    "morning_window": ["09:35", "12:00"],
                    "afternoon_window": ["13:05", "15:45"],
                },
            }
            p._connected = True
            p._scan_history = {}
            p._scan_history_date = ""

            # Mock time to be outside window (lunch break)
            with patch("src.hk.main.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 10, 12, 30, tzinfo=HKT)
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                await p.run_auto_scan(mock_send)

        assert len(sent) == 0

    @pytest.mark.asyncio
    async def test_full_scan_breakout_triggers(self):
        """Full L1→L2→frequency→send pipeline for BREAKOUT."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.hk.main import HKPredictor
        from src.hk import (
            Playbook, OptionRecommendation, OptionLeg,
        )

        sent = []
        async def mock_send(msg):
            sent.append(msg)

        with patch.object(HKPredictor, '__init__', lambda self, *a, **kw: None):
            p = HKPredictor.__new__(HKPredictor)
            p._cfg = {
                "auto_scan": {
                    "enabled": True,
                    "morning_window": ["09:35", "12:00"],
                    "afternoon_window": ["13:05", "15:45"],
                    "breakout": {
                        "min_confidence": 0.72,
                        "min_rvol": 1.35,
                        "min_magnitude_pct": 0.15,
                        "volume_surge_threshold": 2.0,
                        "volume_surge_bars": 5,
                    },
                    "range": {
                        "min_confidence": 0.72,
                        "rvol_min": 0.55,
                        "rvol_max": 0.90,
                        "va_proximity_pct": 0.30,
                    },
                    "cooldown": {
                        "same_signal_minutes": 30,
                        "max_per_session": 2,
                        "max_per_day": 3,
                    },
                    "override": {
                        "confidence_increase": 0.10,
                        "price_extension_pct": 0.50,
                        "regime_upgrade": True,
                    },
                },
                "volume_profile": {"value_area_pct": 0.70, "recency_decay": 0.15},
                "rvol": {"lookback_days": 10},
                "regime": {"breakout_rvol": 1.05, "range_rvol": 0.95},
            }
            p.watchlist = MagicMock()
            p.watchlist.symbols.return_value = ["HK.00700"]
            p.watchlist.get_name.return_value = "Tencent"
            p._connected = True
            p._scan_history = {}
            p._scan_history_date = ""
            p._vp_cache = {}

            async def run_sync(fn, *args):
                return fn(*args)
            p._run_sync = run_sync

            # Quote: price above VAH with good magnitude
            p._collector = MagicMock()
            p._collector.get_quote = lambda sym: {
                "last_price": 525, "high_price": 530, "low_price": 520, "turnover": 5e9,
            }

            # Bars with high volume (RVOL ~3.0)
            today_bars = _make_bars([
                (f"2026-03-10 09:{30+i}:00", 520+i*0.5, 521+i*0.5, 519+i*0.5, 520.5+i*0.5, 3000)
                for i in range(12)
            ])
            hist_bars = _make_bars([
                (f"2026-03-09 09:{30+i}:00", 515+i*0.2, 516+i*0.2, 514+i*0.2, 515.5+i*0.2, 1000)
                for i in range(12)
            ])

            async def mock_bars(sym, cfg):
                return hist_bars, today_bars
            p._get_bars_cached = mock_bars

            # Mock L2 pipeline to return valid results
            regime = RegimeResult(
                regime=RegimeType.BREAKOUT, confidence=0.82, rvol=1.52,
                price=525, vah=521, val=514, poc=518,
            )
            option_rec = OptionRecommendation(
                action="call", direction="bullish", expiry="2026-03-18",
                legs=[OptionLeg(side="buy", option_type="call", strike=525,
                               pct_from_price=0.0, moneyness="ATM")],
            )
            filters = FilterResult(tradeable=True, risk_level="normal")
            vp = VolumeProfileResult(poc=518, vah=521, val=514)
            playbook = Playbook(
                regime=regime, volume_profile=vp, gamma_wall=None,
                filters=filters, vwap=520.0, option_rec=option_rec,
            )

            async def mock_pipeline(symbol):
                return regime, vp, 520.0, filters, option_rec, None, playbook, today_bars
            p._run_analysis_pipeline = mock_pipeline

            # Mock time to be in morning window
            with patch("src.hk.main.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 10, 10, 0, tzinfo=HKT)
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                mock_dt.strptime = datetime.strptime
                await p.run_auto_scan(mock_send)

        assert len(sent) == 1
        assert "BREAKOUT 强信号" in sent[0]
        assert "看多" in sent[0]
        # Verify alert was recorded
        assert "HK.00700" in p._scan_history
        assert len(p._scan_history["HK.00700"]) == 1


# ── P1: Negative EV Spread Filter Tests ──

class TestNegativeEVFilter:
    """P1: Spread with R:R < 0.10 or negative EV should be rejected."""

    def test_positive_ev_passes(self):
        # EV = 0.75 * 0.700 - 0.25 * 1.300 = 0.525 - 0.325 = +0.200
        sm = SpreadMetrics(
            net_credit=0.700, max_profit=0.700, max_loss=1.300,
            breakeven=98.7, risk_reward_ratio=0.538, win_probability=0.75,
        )
        assert _is_positive_ev(sm)

    def test_negative_ev_rejected(self):
        """HK.00941 case: R:R=0.04, win_prob=0.81, EV=-0.385 → reject."""
        sm = SpreadMetrics(
            net_credit=0.090, max_profit=0.090, max_loss=2.410,
            breakeven=80.09, risk_reward_ratio=0.037, win_probability=0.81,
        )
        assert not _is_positive_ev(sm)

    def test_low_rr_rejected(self):
        sm = SpreadMetrics(
            net_credit=0.050, max_profit=0.050, max_loss=1.950,
            breakeven=100.05, risk_reward_ratio=0.026, win_probability=0.90,
        )
        assert not _is_positive_ev(sm)

    def test_borderline_rr_passes(self):
        sm = SpreadMetrics(
            net_credit=0.200, max_profit=0.200, max_loss=1.800,
            breakeven=100.2, risk_reward_ratio=0.111, win_probability=0.80,
        )
        # EV = 0.80*0.200 - 0.20*1.800 = 0.160 - 0.360 = -0.200 → still negative
        assert not _is_positive_ev(sm)

    def test_recommend_rejects_negative_ev_spread(self):
        """Full recommend() should fall through to single leg when spread has negative EV."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=79.0, vah=79.5, val=78.0, poc=78.5,
        )
        vp = VolumeProfileResult(poc=78.5, vah=79.5, val=78.0)
        filters = FilterResult(tradeable=True)
        # Build chain where spread would have very poor R:R
        chain = pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 80.0,
             "strike_time": "2026-03-20", "open_interest": 200,
             "delta": 0.19, "last_price": 0.100},
            {"code": "C2", "option_type": "CALL", "strike_price": 82.50,
             "strike_time": "2026-03-20", "open_interest": 150,
             "delta": 0.08, "last_price": 0.010},
        ])
        dates = [{"strike_time": "2026-03-20"}]
        rec = recommend(regime, vp, filters, chain_df=chain, expiry_dates=dates)
        # Should NOT be bear_call_spread (negative EV)
        assert rec.action != "bear_call_spread"


# ── P2: Strike Display Precision Tests ──

class TestStrikeDisplayPrecision:
    """P2: Fractional strikes should display with 1 decimal, not truncated."""

    def test_fractional_strike_in_leg(self):
        from src.hk.playbook import _format_leg_line
        leg = OptionLeg(
            side="buy", option_type="call", strike=82.50,
            pct_from_price=4.5, moneyness="OTM 4.5%",
            delta=0.08, open_interest=150, last_price=0.010,
        )
        lines = _format_leg_line(leg)
        assert "82.5" in lines[0]
        assert "82 " not in lines[0]  # Should not truncate to "82"

    def test_integer_strike_no_decimal(self):
        from src.hk.playbook import _format_leg_line
        leg = OptionLeg(
            side="sell", option_type="call", strike=80.0,
            pct_from_price=1.3, moneyness="OTM 1.3%",
            delta=0.19, open_interest=200, last_price=0.100,
        )
        lines = _format_leg_line(leg)
        assert "80 " in lines[0] or "80 (" in lines[0]

    def test_format_strike_in_playbook_message(self):
        """Full playbook message should show 82.5 not 82."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=79.0, vah=79.5, val=78.0, poc=78.5,
        )
        vp = VolumeProfileResult(poc=78.5, vah=79.5, val=78.0)
        rec = OptionRecommendation(
            action="bear_call_spread", direction="bearish",
            expiry="2026-03-13", dte=3,
            legs=[
                OptionLeg(side="sell", option_type="call", strike=80.0,
                          pct_from_price=1.3, moneyness="OTM 1.3%",
                          delta=0.19, open_interest=200, last_price=0.100),
                OptionLeg(side="buy", option_type="call", strike=82.50,
                          pct_from_price=4.5, moneyness="OTM 4.5%",
                          delta=0.08, open_interest=150, last_price=0.010),
            ],
            spread_metrics=SpreadMetrics(
                net_credit=0.090, max_profit=0.090, max_loss=2.410,
                breakeven=80.09, risk_reward_ratio=0.037, win_probability=0.81,
            ),
        )
        pb = generate_playbook(regime, vp, vwap=78.55, option_rec=rec)
        msg = format_playbook_message(pb, symbol="HK.00941")
        assert "82.5" in msg


# ── P3: IV Interpretation Strategy-Aware Tests ──

class TestIVInterpretationStrategyAware:
    """P3: Low IV should be negative for seller strategies, positive for buyer strategies."""

    def test_low_iv_seller_strategy_warning(self):
        """Low IV with bear_call_spread should show warning, not support."""
        from src.hk.playbook import _regime_reason_lines
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=79.0, vah=79.5, val=78.0, poc=78.5,
        )
        vp = VolumeProfileResult(poc=78.5, vah=79.5, val=78.0)
        option_market = OptionMarketSnapshot(
            atm_iv=18.25, avg_iv=22.0, iv_ratio=0.83,
        )
        rec = OptionRecommendation(
            action="bear_call_spread", direction="bearish",
        )
        reasons, supports, uncertainties, invalidations = _regime_reason_lines(
            regime, vp, 78.55, None, option_market, None, option_rec=rec,
        )
        # Should be in uncertainties (warning), not supports
        assert any("premium 收入偏少" in u or "卖方" in u for u in uncertainties)
        assert not any("期权定价相对便宜" in s for s in supports)

    def test_low_iv_buyer_strategy_support(self):
        """Low IV with single leg call should show as support."""
        from src.hk.playbook import _regime_reason_lines
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=79.0, vah=79.5, val=78.0, poc=78.5,
        )
        vp = VolumeProfileResult(poc=78.5, vah=79.5, val=78.0)
        option_market = OptionMarketSnapshot(
            atm_iv=18.25, avg_iv=22.0, iv_ratio=0.83,
        )
        rec = OptionRecommendation(action="call", direction="bullish")
        reasons, supports, uncertainties, invalidations = _regime_reason_lines(
            regime, vp, 78.55, None, option_market, None, option_rec=rec,
        )
        assert any("期权定价相对便宜" in s for s in supports)
        assert not any("卖方" in u for u in uncertainties)


# ── P4: Volume Surge RANGE Downgrade Tests ──

class TestVolumeSurgeRangeDowngrade:
    """P4: Volume surge during RANGE near VA edge should reduce confidence."""

    def test_range_with_surge_near_vah_reduces_confidence(self):
        vp = VolumeProfileResult(poc=78.5, vah=79.0, val=78.0)
        # Price near VAH (78.95 vs VAH 79.0)
        result_no_surge = classify_regime(
            price=78.95, rvol=0.6, vp=vp,
            has_volume_surge=False, intraday_range=0.5,
        )
        result_with_surge = classify_regime(
            price=78.95, rvol=0.6, vp=vp,
            has_volume_surge=True, intraday_range=0.5,
        )
        # Both should be RANGE (or UNCLEAR if downgraded)
        if result_with_surge.regime == RegimeType.RANGE:
            assert result_with_surge.confidence < result_no_surge.confidence
        else:
            # Downgraded to UNCLEAR
            assert result_with_surge.regime == RegimeType.UNCLEAR

    def test_range_with_surge_at_center_no_downgrade(self):
        """Volume surge near center of VA should not cause downgrade."""
        vp = VolumeProfileResult(poc=78.5, vah=79.0, val=78.0)
        result_no_surge = classify_regime(
            price=78.5, rvol=0.6, vp=vp,
            has_volume_surge=False, intraday_range=0.3,
        )
        result_with_surge = classify_regime(
            price=78.5, rvol=0.6, vp=vp,
            has_volume_surge=True, intraday_range=0.3,
        )
        # At center (0.5 of range), both should be RANGE with same confidence
        assert result_with_surge.regime == RegimeType.RANGE
        assert result_with_surge.confidence == result_no_surge.confidence


# ── P5: DTE <= 3 Spread Downgrade Tests ──

class TestDTESpreadDowngrade:
    """P5: DTE <= 3 should skip spread and fall through to single leg."""

    def test_dte_3_no_spread(self):
        """With DTE=3, should get single leg instead of spread."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=102.0, vah=105.0, val=95.0, poc=100.0,
        )
        vp = VolumeProfileResult(poc=100.0, vah=105.0, val=95.0)
        filters = FilterResult(tradeable=True)
        chain = pd.DataFrame([
            {"code": f"C{i}", "option_type": "CALL", "strike_price": 100 + i * 2,
             "strike_time": "2026-03-13", "open_interest": 200, "delta": 0.5 - i * 0.1,
             "last_price": 2.0 - i * 0.4}
            for i in range(5)
        ])
        # Expiry is 3 days away
        today = date(2026, 3, 10)
        dates = [{"strike_time": "2026-03-13"}]
        rec = recommend(regime, vp, filters, chain_df=chain, expiry_dates=dates)
        # Should NOT be a spread (DTE too low)
        assert rec.action not in {"bear_call_spread", "bull_put_spread"}

    def test_dte_7_allows_spread(self):
        """With DTE=10, spread path is available (may still be rejected by EV check)."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=102.0, vah=105.0, val=95.0, poc=100.0,
        )
        vp = VolumeProfileResult(poc=100.0, vah=105.0, val=95.0)
        filters = FilterResult(tradeable=True)
        # Build chain with both CALL and PUT, and good R:R for spread
        chain = pd.DataFrame([
            {"code": "C0", "option_type": "CALL", "strike_price": 102,
             "strike_time": "2026-03-20", "open_interest": 200, "delta": 0.50,
             "last_price": 3.0},
            {"code": "C1", "option_type": "CALL", "strike_price": 104,
             "strike_time": "2026-03-20", "open_interest": 200, "delta": 0.35,
             "last_price": 1.5},
            {"code": "P0", "option_type": "PUT", "strike_price": 100,
             "strike_time": "2026-03-20", "open_interest": 200, "delta": -0.40,
             "last_price": 2.0},
            {"code": "P1", "option_type": "PUT", "strike_price": 98,
             "strike_time": "2026-03-20", "open_interest": 200, "delta": -0.25,
             "last_price": 0.5},
        ])
        dates = [{"strike_time": "2026-03-20"}]
        rec = recommend(regime, vp, filters, chain_df=chain, expiry_dates=dates)
        # DTE=10, spread path is open — should get bear_call_spread or fall through to put
        assert rec.action != "wait"


# ── P6: avg_iv ATM ±3 Strikes Tests ──

class TestExtractIVNarrowRange:
    """P6: avg_iv should use ATM ±3 strikes, not full chain median."""

    def test_narrow_range_avoids_deep_otm_skew(self):
        """Deep OTM strikes with high IV should not inflate avg_iv."""
        from src.hk.main import HKPredictor
        chain = pd.DataFrame({
            "strike_price": [60, 65, 70, 75, 78, 80, 82, 85, 90, 95, 100],
            "implied_volatility": [120, 100, 80, 40, 25, 20, 18, 22, 60, 90, 110],
        })
        atm_iv, avg_iv = HKPredictor._extract_iv(chain, price=80)
        # Near-ATM strikes (75,78,80,82,85,65,70) IVs: 40,25,20,18,22,100,80
        # The avg_iv should be much lower than full-chain median
        full_chain_median = chain["implied_volatility"].median()  # ~60
        assert avg_iv < full_chain_median  # Narrower range avoids deep OTM skew

    def test_atm_iv_unchanged(self):
        """atm_iv calculation should remain the same (4 nearest strikes mean)."""
        from src.hk.main import HKPredictor
        chain = pd.DataFrame({
            "strike_price": [95, 97, 100, 103, 105, 110],
            "implied_volatility": [40, 35, 30, 28, 25, 20],
        })
        atm_iv, avg_iv = HKPredictor._extract_iv(chain, price=100)
        # ATM (4 nearest: 100=30, 97=35, 103=28, 95=40) mean = 33.25
        assert abs(atm_iv - 33.25) < 1.0


# ── Intraday Trend + VWAP Contradiction Tests (2026-03-11) ──


def _make_falling_bars(n: int = 20) -> pd.DataFrame:
    """Create N bars with a clear downtrend (open=100, falling to ~90)."""
    prices = []
    base = 100.0
    for i in range(n):
        ts = f"2026-03-11 09:{30 + i}:00"
        o = base - i * 0.5
        c = o - 0.3
        h = o + 0.1
        l = c - 0.1
        prices.append((ts, o, h, l, c, 1000))
    return _make_bars(prices)


def _make_rising_bars(n: int = 20) -> pd.DataFrame:
    """Create N bars with a clear uptrend (open=100, rising to ~110)."""
    prices = []
    base = 100.0
    for i in range(n):
        ts = f"2026-03-11 09:{30 + i}:00"
        o = base + i * 0.5
        c = o + 0.3
        h = c + 0.1
        l = o - 0.1
        prices.append((ts, o, h, l, c, 1000))
    return _make_bars(prices)


def _make_flat_bars(n: int = 5) -> pd.DataFrame:
    """Create few flat bars (< min_bars threshold)."""
    prices = []
    for i in range(n):
        ts = f"2026-03-11 09:{30 + i}:00"
        prices.append((ts, 100.0, 100.1, 99.9, 100.0, 1000))
    return _make_bars(prices)


class TestIntradayTrend:
    def test_intraday_trend_falling(self):
        """Construct declining bars → ("falling", >=0.5)."""
        bars = _make_falling_bars(20)
        direction, strength = _intraday_trend(bars)
        assert direction == "falling"
        assert strength >= 0.5

    def test_intraday_trend_rising(self):
        """Construct rising bars → ("rising", >=0.5)."""
        bars = _make_rising_bars(20)
        direction, strength = _intraday_trend(bars)
        assert direction == "rising"
        assert strength >= 0.5

    def test_intraday_trend_insufficient_bars(self):
        """< 10 bars → ("flat", 0.0)."""
        bars = _make_flat_bars(5)
        direction, strength = _intraday_trend(bars)
        assert direction == "flat"
        assert strength == 0.0

    def test_intraday_trend_none_bars(self):
        """None bars → ("flat", 0.0)."""
        direction, strength = _intraday_trend(None)
        assert direction == "flat"
        assert strength == 0.0


class TestBreakoutVwapContradiction:
    """Tests for VWAP/trend contradiction discounts in classify_regime."""

    _base_vp = VolumeProfileResult(poc=550.0, vah=556.0, val=540.0)

    def test_breakout_vwap_contradiction(self):
        """price > VAH but < VWAP → confidence reduced by 0.20."""
        result = classify_regime(
            price=567.0, rvol=1.5, vp=self._base_vp,
            breakout_rvol=1.05,
            vwap=574.0,  # price below VWAP
        )
        # Without VWAP contradiction, base confidence would be ~1.0
        assert result.regime == RegimeType.BREAKOUT
        assert result.confidence < 0.85  # 0.20 discount applied
        assert "VWAP contradiction" in result.details

    def test_breakout_no_vwap_backward_compat(self):
        """vwap=0 → no VWAP discount applied (backward compatible)."""
        result = classify_regime(
            price=567.0, rvol=1.5, vp=self._base_vp,
            breakout_rvol=1.05,
            vwap=0.0,
        )
        assert result.regime == RegimeType.BREAKOUT
        assert "VWAP contradiction" not in result.details

    def test_breakout_falling_trend(self):
        """Falling today_bars → confidence reduced."""
        bars = _make_falling_bars(20)
        result = classify_regime(
            price=567.0, rvol=1.5, vp=self._base_vp,
            breakout_rvol=1.05,
            today_bars=bars,
        )
        assert result.regime == RegimeType.BREAKOUT
        assert result.confidence < 0.85
        assert "trend contradiction" in result.details

    def test_breakout_combined_degrades_to_unclear(self):
        """VWAP contradiction + trend contradiction → UNCLEAR."""
        bars = _make_falling_bars(20)
        result = classify_regime(
            price=557.0, rvol=1.2, vp=self._base_vp,
            breakout_rvol=1.05,
            vwap=574.0,  # VWAP contradiction: -0.20
            today_bars=bars,  # trend contradiction: -0.20
            # shallow penetration (557 vs VAH 556 = 0.18%): -0.15
        )
        # Combined: -0.55 from ~0.65 base → <0.40 → UNCLEAR
        assert result.regime == RegimeType.UNCLEAR
        assert result.confidence < 0.40

    def test_breakout_shallow_penetration(self):
        """Price barely above VAH (0.1%) → shallow penetration discount."""
        result = classify_regime(
            price=556.5, rvol=1.5, vp=self._base_vp,
            breakout_rvol=1.05,
            va_penetration_min_pct=0.3,
        )
        assert result.regime == RegimeType.BREAKOUT
        assert result.confidence < 0.90  # 0.15 discount for shallow penetration
        assert "shallow penetration" in result.details

    def test_breakout_gap_fade(self):
        """Gap +5% but price < open → confidence reduced."""
        result = classify_regime(
            price=567.0, rvol=1.5, vp=self._base_vp,
            breakout_rvol=1.05,
            open_price=578.0,
            prev_close=550.0,  # gap = +5.1%
            gap_warning_pct=3.0,
        )
        assert result.regime == RegimeType.BREAKOUT
        assert "gap fade" in result.details

    def test_breakout_below_val_vwap_contradiction(self):
        """price < VAL but > VWAP → VWAP contradiction."""
        vp = VolumeProfileResult(poc=550.0, vah=560.0, val=540.0)
        result = classify_regime(
            price=535.0, rvol=1.5, vp=vp,
            breakout_rvol=1.05,
            vwap=530.0,  # price > VWAP → contradiction for bearish
        )
        assert result.regime == RegimeType.BREAKOUT
        assert "VWAP contradiction" in result.details


class TestDirectionVwapContradiction:
    """Tests for _decide_direction with VWAP contradiction."""

    _vp = VolumeProfileResult(poc=550.0, vah=556.0, val=540.0)

    def test_direction_vwap_contradiction_neutral(self):
        """price > VAH but < VWAP → neutral."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=567.0, vah=556.0, val=540.0, poc=550.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=574.0)
        assert direction == "neutral"

    def test_direction_no_vwap_compat(self):
        """vwap=0 → normal bullish (backward compatible)."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=567.0, vah=556.0, val=540.0, poc=550.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=0.0)
        assert direction == "bullish"

    def test_direction_bearish_vwap_contradiction(self):
        """price < VAL but > VWAP → neutral."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=535.0, vah=556.0, val=540.0, poc=550.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=530.0)
        assert direction == "neutral"

    def test_direction_aligned_vwap(self):
        """price > VAH and > VWAP → bullish (no contradiction)."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=567.0, vah=556.0, val=540.0, poc=550.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=560.0)
        assert direction == "bullish"


# ── P0-1: VWAP Structural Veto for RANGE ──

class TestRangeVwapStructuralVeto:
    """P0-1: RANGE direction should be vetoed when VWAP contradicts structural thesis."""

    _vp = VolumeProfileResult(poc=109.0, vah=109.0, val=107.0)

    def test_range_bearish_vetoed_when_vwap_above_vah(self):
        """RANGE bearish near VAH should be neutral when VWAP > VAH (uptrend, not range)."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=108.80, vah=109.0, val=107.0, poc=108.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=109.54)
        assert direction == "neutral"

    def test_range_bullish_vetoed_when_vwap_below_val(self):
        """RANGE bullish near VAL should be neutral when VWAP < VAL (downtrend, not range)."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=107.20, vah=109.0, val=107.0, poc=108.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=106.50)
        assert direction == "neutral"

    def test_range_bearish_allowed_when_vwap_inside_va(self):
        """RANGE bearish near VAH is valid when VWAP is inside VA (normal range)."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=108.80, vah=109.0, val=107.0, poc=108.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=108.50)
        assert direction == "bearish"

    def test_range_direction_no_vwap_backward_compat(self):
        """Without VWAP, RANGE direction works normally."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=108.80, vah=109.0, val=107.0, poc=108.0,
        )
        direction = _decide_direction(regime, self._vp, vwap=0.0)
        assert direction == "bearish"  # price > mid → bearish

    def test_jd_case_vwap_above_vah_blocks_bearish_put(self):
        """JD 2026-03-12 case: price=108.80, VWAP=109.54, VAH=109.00 → no bearish Put."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.72,
            rvol=0.6, price=108.80, vah=109.0, val=107.0, poc=108.0,
        )
        vp = VolumeProfileResult(poc=108.0, vah=109.0, val=107.0)
        filters = FilterResult(tradeable=True)
        chain = pd.DataFrame([
            {"code": "P1", "option_type": "PUT", "strike_price": 108.0,
             "strike_time": "2026-03-14", "open_interest": 200,
             "delta": -0.45, "last_price": 1.50},
        ])
        dates = [{"strike_time": "2026-03-14"}]
        rec = recommend(regime, vp, filters, chain_df=chain, expiry_dates=dates, vwap=109.54)
        # Should be wait (neutral direction from VWAP veto)
        assert rec.action == "wait"
        assert rec.direction == "neutral"


# ── P0-2: Failed Breakout Detection ──

class TestFailedBreakoutDetection:
    """P0-2: Price that breached VA boundary and retreated should discount RANGE confidence."""

    _vp = VolumeProfileResult(poc=109.0, vah=110.0, val=108.0)

    def test_failed_breakout_above_vah_discounts_confidence(self):
        """Today's high breached VAH by >= 0.5% then retreated → confidence reduced."""
        bars = _make_bars([
            ("2026-03-12 10:00", 110.3, 110.6, 110.0, 110.2, 100),  # high=110.6, VAH=110 → 0.55%
            ("2026-03-12 10:01", 110.2, 110.3, 109.5, 109.5, 100),
        ])
        result = classify_regime(
            price=109.5, rvol=0.6, vp=self._vp,
            today_bars=bars, failed_breakout_pct=0.5,
        )
        assert result.regime == RegimeType.RANGE
        assert "failed breakout above VAH" in result.details

        # Compare with no breach
        result_no_breach = classify_regime(price=109.5, rvol=0.6, vp=self._vp)
        assert result.confidence < result_no_breach.confidence

    def test_no_breach_no_discount(self):
        """Today's high below VAH → no failed breakout discount."""
        bars = _make_bars([
            ("2026-03-12 10:00", 109.0, 109.8, 108.5, 109.5, 100),
        ])
        result = classify_regime(
            price=109.5, rvol=0.6, vp=self._vp,
            today_bars=bars, failed_breakout_pct=0.5,
        )
        assert result.regime == RegimeType.RANGE
        assert "failed breakout" not in result.details

    def test_shallow_breach_no_discount(self):
        """Breach < 0.5% → no discount (under threshold)."""
        bars = _make_bars([
            ("2026-03-12 10:00", 110.0, 110.3, 109.5, 109.5, 100),  # 110.3 vs VAH 110 → 0.27%
        ])
        result = classify_regime(
            price=109.5, rvol=0.6, vp=self._vp,
            today_bars=bars, failed_breakout_pct=0.5,
        )
        assert "failed breakout" not in result.details


# ── P1-2: RANGE DTE Guard ──

class TestRangeDTEGuard:
    """P1-2: RANGE regime with DTE < range_min_dte should wait."""

    def test_range_1dte_blocked(self):
        """RANGE with DTE=1 should return wait."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=109.8, vah=110.0, val=108.0, poc=109.0,
        )
        vp = VolumeProfileResult(poc=109.0, vah=110.0, val=108.0)
        filters = FilterResult(tradeable=True)
        chain = pd.DataFrame([
            {"code": "P1", "option_type": "PUT", "strike_price": 109.0,
             "strike_time": "2026-03-13", "open_interest": 200,
             "delta": -0.45, "last_price": 1.50},
        ])
        # DTE=1
        dates = [{"strike_time": "2026-03-13"}]
        rec = recommend(
            regime, vp, filters, chain_df=chain, expiry_dates=dates,
            vwap=109.5, range_min_dte=2,
        )
        assert rec.action == "wait"
        assert "DTE" in rec.rationale

    def test_range_3dte_allowed(self):
        """RANGE with DTE=3 (>= range_min_dte=2) should NOT be blocked by DTE guard."""
        regime = RegimeResult(
            regime=RegimeType.RANGE, confidence=0.7,
            rvol=0.6, price=109.8, vah=110.0, val=108.0, poc=109.0,
        )
        vp = VolumeProfileResult(poc=109.0, vah=110.0, val=108.0)
        filters = FilterResult(tradeable=True)
        chain = pd.DataFrame([
            {"code": "P1", "option_type": "PUT", "strike_price": 109.0,
             "strike_time": "2026-03-15", "open_interest": 200,
             "delta": -0.45, "last_price": 1.50},
        ])
        # DTE=3
        dates = [{"strike_time": "2026-03-15"}]
        rec = recommend(
            regime, vp, filters, chain_df=chain, expiry_dates=dates,
            vwap=109.5, range_min_dte=2,
        )
        # Should NOT be blocked by DTE guard (may still be wait for other reasons)
        if rec.action == "wait":
            assert "DTE" not in rec.rationale

    def test_breakout_1dte_not_blocked(self):
        """BREAKOUT with DTE=1 should NOT be blocked by DTE guard (only affects RANGE)."""
        regime = RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=0.8,
            rvol=1.5, price=112.0, vah=110.0, val=108.0, poc=109.0,
        )
        vp = VolumeProfileResult(poc=109.0, vah=110.0, val=108.0)
        filters = FilterResult(tradeable=True)
        chain = pd.DataFrame([
            {"code": "C1", "option_type": "CALL", "strike_price": 112.0,
             "strike_time": "2026-03-13", "open_interest": 200,
             "delta": 0.50, "last_price": 2.00},
        ])
        dates = [{"strike_time": "2026-03-13"}]
        rec = recommend(
            regime, vp, filters, chain_df=chain, expiry_dates=dates,
            vwap=112.0, range_min_dte=2,
        )
        # BREAKOUT should not be affected by range_min_dte
        assert "DTE" not in (rec.rationale or "")


# ── P2-2: Spike-and-Fade Marker ──

class TestSpikeAndFadeMarker:
    """P2-2: Informational spike-and-fade marker in RANGE details."""

    _vp = VolumeProfileResult(poc=109.0, vah=110.0, val=108.0)

    def test_spike_and_fade_above_vah(self):
        """Today touched VAH but retreated → spike-and-fade marker."""
        bars = _make_bars([
            ("2026-03-12 10:00", 109.5, 110.3, 109.0, 109.2, 100),
        ])
        result = classify_regime(
            price=109.2, rvol=0.6, vp=self._vp,
            today_bars=bars, failed_breakout_pct=5.0,  # High threshold to avoid fb discount
        )
        assert result.regime == RegimeType.RANGE
        assert "spike-and-fade above VAH" in result.details

    def test_no_spike_no_marker(self):
        """Today didn't touch VAH → no marker."""
        bars = _make_bars([
            ("2026-03-12 10:00", 109.0, 109.5, 108.5, 109.2, 100),
        ])
        result = classify_regime(
            price=109.2, rvol=0.6, vp=self._vp,
            today_bars=bars,
        )
        assert "spike-and-fade" not in result.details
