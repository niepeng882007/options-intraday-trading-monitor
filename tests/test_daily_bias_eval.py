"""Tests for Daily Bias Signal Validation (Phase 0).

Tests sub-signal computation, label computation, aggregation logic,
and Go/No-Go threshold checking using synthetic data.
"""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from src.us_playbook.backtest.daily_bias_eval import (
    # Signal functions
    compute_daily_structure,
    compute_yesterday_candle,
    compute_yesterday_volume,
    compute_hourly_ema,
    compute_gap_direction,
    # Label functions
    compute_label_a,
    compute_time_segments,
    # Helpers
    resample_daily,
    resample_hourly,
    _compute_atr,
    _binomial_p_value,
    _dir_to_num,
    _vix_bucket,
    # Evaluator
    DailyBiasEvaluator,
    DailyBiasReport,
    DayResult,
    SignalPrediction,
    ParamWinRate,
    GoNoGoVerdict,
    # Constants
    G1_RAW_DIR_MIN_WR,
    G3_MAX_CORRELATION,
    G5_CANDLE_FLOOR,
    WEIGHT_SCHEMES,
    SIGNAL_TYPES,
    # Report
    format_report,
    format_json,
)

ET = ZoneInfo("America/New_York")


# ── Helpers ──

def _make_bars(prices: list[tuple], tz: str = "America/New_York") -> pd.DataFrame:
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


def _make_daily_bars(data: list[tuple], tz: str = "America/New_York") -> pd.DataFrame:
    """Create daily bar DataFrame.

    data: list of (date_str, open, high, low, close, volume)
    """
    rows = []
    for ds, o, h, l, c, v in data:
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d[0], tz=tz) for d in data], name="Datetime"
    )
    return pd.DataFrame(rows, index=idx)


def _make_uptrend_daily(n_days: int = 20, start_price: float = 100.0, base_date: str = "2026-02-01") -> pd.DataFrame:
    """Create daily bars with clear uptrend (HH/HL)."""
    data = []
    price = start_price
    dt = pd.Timestamp(base_date, tz=ET)
    for i in range(n_days):
        day = dt + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        o = price
        h = price + 1.5
        l = price - 0.3
        c = price + 1.0
        data.append((str(day.date()), o, h, l, c, 1_000_000 + i * 10000))
        price = c + 0.2  # Gap up slightly
    return _make_daily_bars(data)


def _make_downtrend_daily(n_days: int = 20, start_price: float = 200.0, base_date: str = "2026-02-01") -> pd.DataFrame:
    """Create daily bars with clear downtrend (LH/LL)."""
    data = []
    price = start_price
    dt = pd.Timestamp(base_date, tz=ET)
    for i in range(n_days):
        day = dt + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        o = price
        h = price + 0.3
        l = price - 1.5
        c = price - 1.0
        data.append((str(day.date()), o, h, l, c, 1_000_000))
        price = c - 0.2
    return _make_daily_bars(data)


def _make_1m_day(
    day_str: str,
    open_price: float = 100.0,
    close_price: float = 101.0,
    n_bars: int = 390,
) -> pd.DataFrame:
    """Create 1m bars for a single trading day with linear price path."""
    prices = np.linspace(open_price, close_price, n_bars)
    rows = []
    base = pd.Timestamp(f"{day_str} 09:30:00", tz=ET)
    for i in range(n_bars):
        p = prices[i]
        rows.append((
            str(base + timedelta(minutes=i)),
            p - 0.1, p + 0.2, p - 0.2, p, 10000,
        ))
    return _make_bars(rows)


def _make_hourly_bars(n_hours: int = 60, start_price: float = 100.0, trend: str = "up") -> pd.DataFrame:
    """Create hourly bars for EMA testing."""
    data = []
    price = start_price
    base = pd.Timestamp("2026-02-01 09:30:00", tz=ET)
    for i in range(n_hours):
        t = base + timedelta(hours=i)
        if trend == "up":
            o = price
            h = price + 1.0
            l = price - 0.3
            c = price + 0.5
            price = c
        elif trend == "down":
            o = price
            h = price + 0.3
            l = price - 1.0
            c = price - 0.5
            price = c
        else:
            o = price
            h = price + 0.5
            l = price - 0.5
            c = price + np.random.uniform(-0.2, 0.2)
            price = c
        data.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 50000})
    idx = pd.DatetimeIndex([base + timedelta(hours=i) for i in range(n_hours)], name="Datetime")
    return pd.DataFrame(data, index=idx)


# ── Tests: Helpers ──

class TestHelpers:
    def test_dir_to_num(self):
        assert _dir_to_num("bullish") == 1
        assert _dir_to_num("bearish") == -1
        assert _dir_to_num("neutral") == 0
        assert _dir_to_num("unknown") == 0

    def test_binomial_p_value_strong_signal(self):
        """65% win rate with 200 samples → very significant."""
        p = _binomial_p_value(130, 200)
        assert p < 0.001

    def test_binomial_p_value_random(self):
        """50% win rate → not significant."""
        p = _binomial_p_value(100, 200)
        assert p > 0.4

    def test_binomial_p_value_small_sample(self):
        """Very small sample → returns 1.0."""
        p = _binomial_p_value(3, 4)
        assert p == 1.0

    def test_vix_bucket(self):
        assert _vix_bucket(12.0) == "low"
        assert _vix_bucket(20.0) == "mid"
        assert _vix_bucket(30.0) == "high"
        assert _vix_bucket(16.0) == "mid"  # boundary: >= 16 is mid
        assert _vix_bucket(24.0) == "mid"  # boundary: <= 24 is mid


class TestResample:
    def test_resample_daily(self):
        bars = _make_1m_day("2026-03-09", 100, 102, 390)
        daily = resample_daily(bars)
        assert len(daily) == 1
        assert daily.iloc[0]["Open"] == pytest.approx(99.9, abs=0.2)
        assert daily.iloc[0]["Close"] == pytest.approx(102, abs=0.2)

    def test_resample_hourly(self):
        bars = _make_1m_day("2026-03-09", 100, 102, 390)
        hourly = resample_hourly(bars)
        assert len(hourly) >= 6  # ~6.5 hours of trading

    def test_resample_empty(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        assert resample_daily(empty).empty
        assert resample_hourly(empty).empty

    def test_compute_atr(self):
        daily = _make_uptrend_daily(20)
        atr = _compute_atr(daily)
        assert atr > 0


# ── Tests: Signal Computation ──

class TestDailyStructure:
    def test_uptrend_detected(self):
        daily = _make_uptrend_daily(20)
        # Test at last index — should detect bullish structure from preceding bars
        result = compute_daily_structure(daily, len(daily) - 1, window=5)
        assert result.direction == "bullish"
        assert result.strength > 0

    def test_downtrend_detected(self):
        daily = _make_downtrend_daily(20)
        result = compute_daily_structure(daily, len(daily) - 1, window=5)
        assert result.direction == "bearish"
        assert result.strength > 0

    def test_insufficient_history(self):
        daily = _make_uptrend_daily(3)
        result = compute_daily_structure(daily, 2, window=5)
        assert result.direction == "neutral"

    def test_different_windows(self):
        daily = _make_uptrend_daily(20)
        for w in [5, 8, 10]:
            result = compute_daily_structure(daily, len(daily) - 1, w)
            assert result.param_key == f"window_{w}"


class TestYesterdayCandle:
    def test_bullish_candle(self):
        daily = _make_daily_bars([
            ("2026-03-08", 100, 105, 99, 104, 1000000),  # Bullish: c > o, body_ratio high
            ("2026-03-09", 105, 108, 104, 107, 1000000),
        ])
        result = compute_yesterday_candle(daily, 1, body_ratio_threshold=0.3)
        assert result.direction == "bullish"

    def test_bearish_candle(self):
        daily = _make_daily_bars([
            ("2026-03-08", 104, 105, 99, 100, 1000000),  # Bearish: c < o
            ("2026-03-09", 100, 103, 99, 102, 1000000),
        ])
        result = compute_yesterday_candle(daily, 1, body_ratio_threshold=0.3)
        assert result.direction == "bearish"

    def test_doji_filtered(self):
        """Small body candle should be filtered out at high threshold."""
        daily = _make_daily_bars([
            ("2026-03-08", 100, 105, 95, 100.5, 1000000),  # Doji: body_ratio ~0.05
            ("2026-03-09", 100, 103, 99, 102, 1000000),
        ])
        result = compute_yesterday_candle(daily, 1, body_ratio_threshold=0.5)
        assert result.direction == "neutral"

    def test_no_history(self):
        daily = _make_daily_bars([("2026-03-09", 100, 103, 99, 102, 1000000)])
        result = compute_yesterday_candle(daily, 0, body_ratio_threshold=0.3)
        assert result.direction == "neutral"


class TestYesterdayVolume:
    def test_high_volume_confirms(self):
        """High volume + bullish candle → bullish."""
        daily = _make_daily_bars([
            ("2026-03-05", 100, 102, 99, 101, 500000),
            ("2026-03-06", 101, 103, 100, 102, 600000),
            ("2026-03-07", 102, 104, 101, 103, 550000),
            ("2026-03-08", 103, 108, 102, 107, 2000000),  # High vol + bullish
            ("2026-03-09", 107, 110, 106, 109, 1000000),
        ])
        result = compute_yesterday_volume(daily, 4, "bullish", vol_lookback=3)
        assert result.direction == "bullish"
        assert result.strength >= 1.2  # vol_ratio

    def test_low_volume_neutral(self):
        """Low volume → neutral."""
        daily = _make_daily_bars([
            ("2026-03-05", 100, 102, 99, 101, 1000000),
            ("2026-03-06", 101, 103, 100, 102, 1100000),
            ("2026-03-07", 102, 104, 101, 103, 900000),
            ("2026-03-08", 103, 105, 102, 104, 300000),  # Low vol
            ("2026-03-09", 104, 107, 103, 106, 800000),
        ])
        result = compute_yesterday_volume(daily, 4, "bullish", vol_lookback=3)
        assert result.direction == "neutral"

    def test_neutral_candle_neutral_volume(self):
        daily = _make_daily_bars([
            ("2026-03-08", 100, 102, 99, 101, 1000000),
            ("2026-03-09", 101, 103, 100, 102, 1000000),
        ])
        result = compute_yesterday_volume(daily, 1, "neutral")
        assert result.direction == "neutral"


class TestHourlyEMA:
    def test_bullish_ema(self):
        """Uptrending hourly bars → fast EMA above slow EMA → bullish."""
        hourly = _make_hourly_bars(60, 100.0, "up")
        # Target date: a date after the hourly data
        target = hourly.index[-1].date() + timedelta(days=1)
        result = compute_hourly_ema(hourly, target, fast_period=8, slow_period=21)
        assert result.direction == "bullish"

    def test_bearish_ema(self):
        hourly = _make_hourly_bars(60, 200.0, "down")
        target = hourly.index[-1].date() + timedelta(days=1)
        result = compute_hourly_ema(hourly, target, fast_period=8, slow_period=21)
        assert result.direction == "bearish"

    def test_insufficient_data(self):
        hourly = _make_hourly_bars(5, 100.0, "up")
        target = hourly.index[-1].date() + timedelta(days=1)
        result = compute_hourly_ema(hourly, target, fast_period=8, slow_period=21)
        assert result.direction == "neutral"


class TestGapDirection:
    def test_gap_up(self):
        daily = _make_daily_bars([
            ("2026-03-05", 95, 97, 94, 96, 1000000),
            ("2026-03-06", 96, 98, 95, 97, 1000000),
            ("2026-03-07", 97, 99, 96, 98, 1000000),
            # Big gap up: open 103 vs prev close 98 → gap = 5, ATR ~3 → normalized ~1.7
            ("2026-03-08", 103, 106, 102, 105, 1500000),
        ])
        result = compute_gap_direction(daily, 3, atr_multiplier=0.3)
        assert result.direction == "bullish"

    def test_gap_down(self):
        daily = _make_daily_bars([
            ("2026-03-05", 100, 102, 99, 101, 1000000),
            ("2026-03-06", 101, 103, 100, 102, 1000000),
            ("2026-03-07", 102, 104, 101, 103, 1000000),
            # Big gap down: open 97 vs prev close 103
            ("2026-03-08", 97, 98, 95, 96, 1500000),
        ])
        result = compute_gap_direction(daily, 3, atr_multiplier=0.3)
        assert result.direction == "bearish"

    def test_small_gap_neutral(self):
        daily = _make_daily_bars([
            ("2026-03-05", 100, 105, 95, 101, 1000000),  # Large range → large ATR
            ("2026-03-06", 101, 106, 96, 102, 1000000),
            ("2026-03-07", 102, 107, 97, 103, 1000000),
            ("2026-03-08", 103.1, 108, 98, 104, 1000000),  # Tiny gap (0.1)
        ])
        result = compute_gap_direction(daily, 3, atr_multiplier=0.5)
        assert result.direction == "neutral"

    def test_no_history(self):
        daily = _make_daily_bars([("2026-03-08", 100, 102, 99, 101, 1000000)])
        result = compute_gap_direction(daily, 0, atr_multiplier=0.3)
        assert result.direction == "neutral"


# ── Tests: Label Computation ──

class TestLabelA:
    def test_bullish_close_vs_open(self):
        bars = _make_1m_day("2026-03-09", 100, 105)
        cvo, cvv = compute_label_a(bars)
        assert cvo == "bullish"

    def test_bearish_close_vs_open(self):
        bars = _make_1m_day("2026-03-09", 105, 100)
        cvo, cvv = compute_label_a(bars)
        assert cvo == "bearish"

    def test_empty_bars(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        cvo, cvv = compute_label_a(empty)
        assert cvo == "neutral"
        assert cvv == "neutral"


class TestTimeSegments:
    def test_segments(self):
        # Create bars covering full day with uptrend
        bars = _make_1m_day("2026-03-09", 100, 110, 390)
        segments = compute_time_segments(bars)
        assert segments["AM1"] == "bullish"
        assert segments["PM"] == "bullish"

    def test_empty_bars(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        segments = compute_time_segments(empty)
        assert segments["AM1"] == "neutral"


# ── Tests: Evaluator Integration ──

class TestEvaluatorSmoke:
    """Smoke tests for DailyBiasEvaluator with synthetic data."""

    def _make_multi_day_1m(self, n_days: int = 25) -> dict[str, pd.DataFrame]:
        """Create 1m bars for SPY spanning multiple trading days."""
        all_bars = []
        base = datetime(2026, 2, 1, tzinfo=ET)
        price = 500.0
        for d in range(n_days * 2):  # extra for weekends
            day = base + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            if len(all_bars) >= n_days:
                break
            day_str = day.strftime("%Y-%m-%d")
            # Alternate up/down days
            if d % 3 == 0:
                close = price + 2
            else:
                close = price - 1
            day_bars = _make_1m_day(day_str, price, close, 390)
            all_bars.append(day_bars)
            price = close + 0.1

        if not all_bars:
            return {}
        combined = pd.concat(all_bars)
        return {"SPY": combined}

    def test_evaluate_produces_report(self):
        """Evaluator should produce a report with non-zero data."""
        bars = self._make_multi_day_1m(25)
        evaluator = DailyBiasEvaluator()
        report = evaluator.evaluate(bars)
        assert isinstance(report, DailyBiasReport)
        assert report.trading_days > 0
        assert report.total_samples > 0

    def test_evaluate_empty_data(self):
        """Empty input should return empty report."""
        evaluator = DailyBiasEvaluator()
        report = evaluator.evaluate({})
        assert report.total_samples == 0

    def test_signal_results_present(self):
        """Report should contain signal results for each type."""
        bars = self._make_multi_day_1m(25)
        evaluator = DailyBiasEvaluator()
        report = evaluator.evaluate(bars)
        signal_types = {r.signal_type for r in report.signal_results}
        # At least some signals should be present
        assert len(signal_types) > 0

    def test_verdicts_present(self):
        """Report should contain Go/No-Go verdicts."""
        bars = self._make_multi_day_1m(25)
        evaluator = DailyBiasEvaluator()
        report = evaluator.evaluate(bars)
        assert len(report.verdicts) == 6  # G1-G6
        assert report.overall_verdict in ("PASS", "PARTIAL", "FAIL")


# ── Tests: Report Formatting ──

class TestReportFormatting:
    def _make_sample_report(self) -> DailyBiasReport:
        return DailyBiasReport(
            symbols=["SPY", "AAPL"],
            period_days=180,
            trading_days=120,
            total_samples=960,
            effective_n=250,
            signal_results=[
                ParamWinRate("structure", "window_10", 0.57, 0.55, 0.53, 800, 600, 0.02),
                ParamWinRate("candle", "body_ratio_50", 0.52, 0.51, 0.49, 700, 550, 0.15),
                ParamWinRate("ema", "ema_8_21", 0.54, 0.53, 0.51, 850, 640, 0.04),
            ],
            verdicts=[
                GoNoGoVerdict("G1", "Sub-signal raw dir WR", "> 55%", "57.0%", "PASS", "MUST"),
                GoNoGoVerdict("G2", "Aggregated WR >= best single", ">= 57.0%", "58.2%", "PASS", "SHOULD"),
                GoNoGoVerdict("G3", "Structure vs EMA corr", "< 0.7", "0.35", "PASS", "SHOULD"),
                GoNoGoVerdict("G4", "Confidence impact", ">= 10%", "15.0%", "PASS", "MUST"),
                GoNoGoVerdict("G5", "Candle WR", "> 50%", "52.0%", "PASS", "OPTIONAL"),
                GoNoGoVerdict("G6", "PM segment WR", "> 50%", "51.5%", "PASS", "EXPLORATORY"),
            ],
            overall_verdict="PASS",
            recommendation="全部信号集成",
        )

    def test_format_report_text(self):
        report = self._make_sample_report()
        text = format_report(report)
        assert "Daily Bias Signal Validation Report" in text
        assert "SPY" in text
        assert "AAPL" in text
        assert "PASS" in text
        assert "Go/No-Go" in text

    def test_format_report_json(self):
        report = self._make_sample_report()
        text = format_json(report)
        data = __import__("json").loads(text)
        assert data["overall"] == "PASS"
        assert len(data["signal_results"]) == 3
        assert len(data["verdicts"]) == 6


# ── Tests: Go/No-Go Logic ──

class TestGoNoGo:
    def test_g1_pass(self):
        """Best signal WR > 55% → G1 PASS."""
        evaluator = DailyBiasEvaluator()
        signal_results = [
            ParamWinRate("structure", "window_10", 0.58, 0.56, 0.54, 300, 200, 0.01),
            ParamWinRate("candle", "body_ratio_50", 0.48, 0.47, 0.46, 300, 200, 0.70),
        ]
        verdicts, overall, rec = evaluator._evaluate_go_nogo(
            signal_results, [], [], [], [],
        )
        g1 = next(v for v in verdicts if v.code == "G1")
        assert g1.verdict == "PASS"

    def test_g1_inconclusive(self):
        """Best WR between 52% and 55% → G1 INCONCLUSIVE."""
        evaluator = DailyBiasEvaluator()
        signal_results = [
            ParamWinRate("structure", "window_10", 0.53, 0.52, 0.50, 300, 200, 0.10),
            ParamWinRate("candle", "body_ratio_50", 0.49, 0.48, 0.47, 300, 200, 0.70),
        ]
        verdicts, overall, rec = evaluator._evaluate_go_nogo(
            signal_results, [], [], [], [],
        )
        g1 = next(v for v in verdicts if v.code == "G1")
        assert g1.verdict == "INCONCLUSIVE"

    def test_g1_fail(self):
        """Best WR < 52% → G1 FAIL."""
        evaluator = DailyBiasEvaluator()
        signal_results = [
            ParamWinRate("structure", "window_10", 0.49, 0.48, 0.47, 300, 200, 0.70),
            ParamWinRate("candle", "body_ratio_50", 0.51, 0.50, 0.49, 300, 200, 0.50),
        ]
        verdicts, overall, rec = evaluator._evaluate_go_nogo(
            signal_results, [], [], [], [],
        )
        g1 = next(v for v in verdicts if v.code == "G1")
        assert g1.verdict == "FAIL"

    def test_g3_structure_ema_high_corr(self):
        """Structure vs EMA correlation >= 0.7 → G3 FAIL."""
        from src.us_playbook.backtest.daily_bias_eval import CorrelationPair
        evaluator = DailyBiasEvaluator()
        corrs = [CorrelationPair("structure", "ema", 0.85, 0.80)]
        verdicts, _, _ = evaluator._evaluate_go_nogo(
            [ParamWinRate("structure", "w10", 0.56, 0.55, 0.53, 300, 200, 0.02)],
            corrs, [], [], [],
        )
        g3 = next(v for v in verdicts if v.code == "G3")
        assert g3.verdict == "FAIL"

    def test_overall_fail_on_g1_fail(self):
        """G1 MUST FAIL → overall FAIL."""
        evaluator = DailyBiasEvaluator()
        signal_results = [
            ParamWinRate("structure", "w10", 0.45, 0.44, 0.43, 300, 200, 0.95),
        ]
        verdicts, overall, rec = evaluator._evaluate_go_nogo(
            signal_results, [], [], [], [],
        )
        assert overall == "FAIL"
        assert "放弃" in rec

    def test_overall_pass(self):
        """All criteria pass → overall PASS."""
        from src.us_playbook.backtest.daily_bias_eval import (
            CorrelationPair, AggregateResult, ConfidenceSensitivity, TimeSegmentResult,
        )
        evaluator = DailyBiasEvaluator()
        signal_results = [
            ParamWinRate("structure", "w10", 0.58, 0.56, 0.54, 300, 200, 0.01),
            ParamWinRate("candle", "br50", 0.53, 0.52, 0.50, 300, 200, 0.10),
            ParamWinRate("ema", "ema_8_21", 0.56, 0.55, 0.52, 300, 200, 0.02),
        ]
        corrs = [CorrelationPair("structure", "ema", 0.30, 0.28)]
        aggs = [AggregateResult("equal", 0.59, 0.55, 300, 200)]
        conf = [ConfidenceSensitivity(0.10, 45, 15.0)]
        time_segs = [
            TimeSegmentResult("structure", "w10", "PM", 0.55, 100),
            TimeSegmentResult("ema", "ema_8_21", "PM", 0.54, 100),
        ]
        verdicts, overall, rec = evaluator._evaluate_go_nogo(
            signal_results, corrs, aggs, conf, time_segs,
        )
        assert overall == "PASS"


# ── Tests: Aggregation ──

class TestAggregation:
    def test_weight_schemes_defined(self):
        """All weight schemes should have all signal types."""
        for name, weights in WEIGHT_SCHEMES.items():
            for sig in SIGNAL_TYPES:
                assert sig in weights, f"{name} missing {sig}"

    def test_weights_sum_to_one(self):
        """Each weight scheme should sum to ~1.0."""
        for name, weights in WEIGHT_SCHEMES.items():
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.01, f"{name} weights sum to {total}"


# ── Tests: Confidence Sensitivity ──

class TestConfidenceSensitivity:
    def test_modifier_changes_decisions(self):
        """A modifier should change some scan trigger decisions."""
        evaluator = DailyBiasEvaluator()
        # Create regime days with confidence near threshold (0.70)
        from src.us_playbook.backtest import RegimeEvalDay
        from src.us_playbook import USRegimeType
        days = []
        for i in range(10):
            days.append(RegimeEvalDay(
                date=date(2026, 3, i + 1),
                symbol="SPY",
                predicted=USRegimeType.GAP_GO,
                confidence=0.68,  # Just below scan threshold
                rvol=1.8,
                vah=505, val=495, poc=500,
            ))
        result = evaluator._compute_confidence_sensitivity(days)
        # +0.05 modifier should push 0.68 → 0.73 → trigger scan
        pos_mod = next(r for r in result if r.modifier == 0.05)
        assert pos_mod.decisions_changed == 10  # All should flip

    def test_no_tradeable_days(self):
        """All UNCLEAR → empty result."""
        evaluator = DailyBiasEvaluator()
        from src.us_playbook.backtest import RegimeEvalDay
        from src.us_playbook import USRegimeType
        days = [RegimeEvalDay(
            date=date(2026, 3, 1), symbol="SPY",
            predicted=USRegimeType.UNCLEAR, confidence=0.30,
            rvol=0.8, vah=505, val=495, poc=500,
        )]
        result = evaluator._compute_confidence_sensitivity(days)
        assert result == []
