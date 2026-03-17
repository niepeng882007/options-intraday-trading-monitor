"""Daily Bias Signal Validation (Phase 0).

Evaluates 5 sub-signals for top-down daily bias:
1. Daily Structure (HH/HL vs LH/LL on daily bars)
2. Yesterday Candle (body direction + strength)
3. Yesterday Volume (volume confirmation modifier)
4. Hourly EMA crossover
5. Gap Direction (ATR-normalized)

Dual-label system:
- Label A (Raw Direction): close vs open, close vs VWAP
- Label B (Regime-Aligned P&L): regime classifier + trade simulator

Usage:
    python -m src.us_playbook.backtest.daily_bias_eval -d 180 --all-watchlist -v
    python -m src.us_playbook.backtest.daily_bias_eval -d 60 -y SPY,AAPL -v
    python -m src.us_playbook.backtest.daily_bias_eval -d 180 --all-watchlist -o json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.common.indicators import calculate_vwap
from src.us_playbook import USRegimeType
from src.us_playbook.backtest import RegimeEvalDay, SimTrade
from src.us_playbook.backtest.evaluators import evaluate_regimes
from src.us_playbook.backtest.simulator import USTradeSimulator
from src.utils.logger import setup_logger

logger = setup_logger("daily_bias_eval")

# ── Go/No-Go Constants (locked before evaluation) ──

G1_RAW_DIR_MIN_WR = 0.55       # At least 1 sub-signal raw direction WR > 55%
G3_MAX_CORRELATION = 0.70       # Structure vs EMA correlation < 0.7
G4_MIN_DECISION_IMPACT = 0.10   # >= 10% trades affected by confidence modifier
G5_CANDLE_FLOOR = 0.50          # Candle WR > 50% (not worse than random)
G6_PM_FLOOR = 0.50              # PM segment WR > 50%

# Exploratory thresholds (stricter than main)
EXPLORATORY_MIN_WR = 0.55

# Confidence sensitivity thresholds
CONFIDENCE_SCAN_THRESHOLD = 0.70   # auto-scan min confidence
CONFIDENCE_OBSERVE_THRESHOLD = 0.40  # below → observe mode

# Parameter scan variants
STRUCTURE_WINDOWS = [5, 8, 10, 15]
CANDLE_BODY_RATIOS = [0.3, 0.5, 0.6, 0.7]
EMA_PAIRS = [(8, 21), (13, 34), (20, 50)]
GAP_ATR_MULTIPLIERS = [0.2, 0.3, 0.5]
VOLUME_RATIO_THRESHOLD = 1.2

# Weight schemes for aggregation
WEIGHT_SCHEMES: dict[str, dict[str, float]] = {
    "equal": {"structure": 0.20, "candle": 0.20, "volume": 0.20, "ema": 0.20, "gap": 0.20},
    "structure_heavy": {"structure": 0.35, "candle": 0.15, "volume": 0.10, "ema": 0.25, "gap": 0.15},
    "ema_heavy": {"structure": 0.15, "candle": 0.15, "volume": 0.10, "ema": 0.35, "gap": 0.25},
    "no_candle": {"structure": 0.30, "candle": 0.00, "volume": 0.00, "ema": 0.40, "gap": 0.30},
    "original": {"structure": 0.25, "candle": 0.20, "volume": 0.10, "ema": 0.25, "gap": 0.20},
}

SIGNAL_TYPES = ["structure", "candle", "volume", "ema", "gap"]

# VIX buckets
VIX_LOW = 16.0
VIX_HIGH = 24.0


# ── Dataclasses ──

@dataclass
class SignalPrediction:
    """A single signal prediction for one day."""
    signal_type: str   # "structure", "candle", "volume", "ema", "gap"
    param_key: str     # "window_5", "body_ratio_50", "ema_8_21", etc.
    direction: str     # "bullish" / "bearish" / "neutral"
    strength: float = 0.0


@dataclass
class DayResult:
    """Evaluation result for a single day × symbol."""
    date: date
    symbol: str
    predictions: list[SignalPrediction]
    # Label A
    label_a_close_vs_open: str     # "bullish" / "bearish"
    label_a_close_vs_vwap: str     # "bullish" / "bearish"
    # Label B
    label_b_regime: USRegimeType | None = None
    label_b_trade_direction: str | None = None  # "long" / "short" / None
    label_b_pnl: float | None = None            # net_pnl_pct, None if no_trade
    label_b_actual_direction: str | None = None  # inferred from trade result
    # Context
    vix_level: float = 0.0
    vix_bucket: str = "mid"  # "low" / "mid" / "high"
    # Time segments
    am1_direction: str = "neutral"
    am2_direction: str = "neutral"
    pm_direction: str = "neutral"


@dataclass
class ParamWinRate:
    """Win rate for a specific signal × parameter combination."""
    signal_type: str
    param_key: str
    raw_dir_wr: float       # Label A (close_vs_open) win rate
    raw_dir_vwap_wr: float  # Label A (close_vs_vwap) win rate
    regime_pnl_wr: float    # Label B win rate
    sample_a: int           # Total directional predictions (excl. neutral)
    sample_b: int           # Total traded days with directional prediction
    p_value_a: float = 1.0  # Binomial p-value for raw_dir_wr vs 50%


@dataclass
class CorrelationPair:
    signal_a: str
    signal_b: str
    spearman: float
    pearson: float


@dataclass
class AggregateResult:
    """Win rate for an aggregation weight scheme."""
    scheme: str
    raw_dir_wr: float
    regime_pnl_wr: float
    sample_a: int
    sample_b: int


@dataclass
class ConfidenceSensitivity:
    """Impact of a confidence modifier on trading decisions."""
    modifier: float
    decisions_changed: int
    pct_changed: float


@dataclass
class TimeSegmentResult:
    """Win rate for a signal in a specific time segment."""
    signal_type: str
    param_key: str
    segment: str  # "AM1" / "AM2" / "PM"
    win_rate: float
    sample: int


@dataclass
class StratifiedResult:
    """Win rate for a signal in a VIX bucket."""
    signal_type: str
    vix_bucket: str
    win_rate: float
    sample: int


@dataclass
class GoNoGoVerdict:
    """Verdict for a single Go/No-Go criterion."""
    code: str           # "G1", "G2", etc.
    description: str
    threshold: str
    observed: str
    verdict: str        # "PASS" / "FAIL" / "INCONCLUSIVE"
    level: str          # "MUST" / "SHOULD" / "OPTIONAL" / "EXPLORATORY"


@dataclass
class DailyBiasReport:
    """Complete Phase 0 validation report."""
    symbols: list[str]
    period_days: int
    trading_days: int
    total_samples: int
    effective_n: int

    # Per-signal best param results
    signal_results: list[ParamWinRate] = field(default_factory=list)
    # All param variants (for detailed view)
    all_param_results: list[ParamWinRate] = field(default_factory=list)
    # Correlations
    correlations: list[CorrelationPair] = field(default_factory=list)
    # Aggregation
    aggregate_results: list[AggregateResult] = field(default_factory=list)
    # Confidence sensitivity
    confidence_sensitivity: list[ConfidenceSensitivity] = field(default_factory=list)
    # Time segments (exploratory)
    time_segments: list[TimeSegmentResult] = field(default_factory=list)
    # VIX stratification (exploratory)
    stratified: list[StratifiedResult] = field(default_factory=list)
    # Go/No-Go
    verdicts: list[GoNoGoVerdict] = field(default_factory=list)
    overall_verdict: str = ""
    recommendation: str = ""


# ── Helpers ──

def _dir_to_num(direction: str) -> int:
    return {"bullish": 1, "bearish": -1}.get(direction, 0)


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via error function."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _binomial_p_value(wins: int, total: int, null_p: float = 0.50) -> float:
    """One-sided binomial test: P(X >= wins | p = null_p), normal approx."""
    if total < 5:
        return 1.0
    se = (null_p * (1 - null_p) / total) ** 0.5
    if se == 0:
        return 1.0
    z = (wins / total - null_p) / se
    return 1 - _normal_cdf(z)


def resample_daily(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m bars to daily OHLCV."""
    if bars_1m.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    daily = bars_1m.resample("D").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna(subset=["Open"])
    return daily


def resample_hourly(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m bars to 1H OHLCV (regular hours only)."""
    if bars_1m.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    hourly = bars_1m.resample("h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna(subset=["Open"])
    return hourly


def _compute_atr(daily_bars: pd.DataFrame, period: int = 14) -> float:
    """Average True Range from daily OHLCV."""
    if len(daily_bars) < 2:
        return 0.0
    high = daily_bars["High"].values
    low = daily_bars["Low"].values
    prev_close = np.roll(daily_bars["Close"].values, 1)
    prev_close[0] = daily_bars["Close"].values[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    if len(tr) < period:
        return float(np.mean(tr))
    return float(np.mean(tr[-period:]))


# ── Sub-Signal Computation ──

def compute_daily_structure(
    daily_bars: pd.DataFrame,
    target_idx: int,
    window: int,
) -> SignalPrediction:
    """Detect HH/HL (bullish) or LH/LL (bearish) in daily bars preceding target day."""
    param_key = f"window_{window}"
    if target_idx < window:
        return SignalPrediction("structure", param_key, "neutral")

    chunk = daily_bars.iloc[target_idx - window:target_idx]
    highs = chunk["High"].values
    lows = chunk["Low"].values
    n_pairs = len(highs) - 1
    if n_pairs < 2:
        return SignalPrediction("structure", param_key, "neutral")

    hh = sum(1 for i in range(n_pairs) if highs[i + 1] > highs[i])
    hl = sum(1 for i in range(n_pairs) if lows[i + 1] > lows[i])
    lh = sum(1 for i in range(n_pairs) if highs[i + 1] < highs[i])
    ll = sum(1 for i in range(n_pairs) if lows[i + 1] < lows[i])

    hh_r, hl_r = hh / n_pairs, hl / n_pairs
    lh_r, ll_r = lh / n_pairs, ll / n_pairs

    if hh_r >= 0.6 and hl_r >= 0.5:
        strength = (hh_r + hl_r) / 2
        return SignalPrediction("structure", param_key, "bullish", strength)
    if lh_r >= 0.6 and ll_r >= 0.5:
        strength = (lh_r + ll_r) / 2
        return SignalPrediction("structure", param_key, "bearish", strength)
    return SignalPrediction("structure", param_key, "neutral")


def compute_yesterday_candle(
    daily_bars: pd.DataFrame,
    target_idx: int,
    body_ratio_threshold: float,
) -> SignalPrediction:
    """Yesterday's candle body direction with body_ratio filter."""
    param_key = f"body_ratio_{int(body_ratio_threshold * 100)}"
    if target_idx < 1:
        return SignalPrediction("candle", param_key, "neutral")

    row = daily_bars.iloc[target_idx - 1]
    o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
    body = c - o
    range_ = h - l
    if range_ <= 0:
        return SignalPrediction("candle", param_key, "neutral")

    body_ratio = abs(body) / range_
    if body_ratio < body_ratio_threshold:
        return SignalPrediction("candle", param_key, "neutral")

    direction = "bullish" if body > 0 else "bearish"
    return SignalPrediction("candle", param_key, direction, body_ratio)


def compute_yesterday_volume(
    daily_bars: pd.DataFrame,
    target_idx: int,
    candle_direction: str,
    vol_lookback: int = 20,
) -> SignalPrediction:
    """Volume confirmation of yesterday's candle direction."""
    param_key = "vol_mod"
    if target_idx < 2 or candle_direction == "neutral":
        return SignalPrediction("volume", param_key, "neutral")

    yesterday_vol = daily_bars.iloc[target_idx - 1]["Volume"]
    start = max(0, target_idx - 1 - vol_lookback)
    hist_vol = daily_bars.iloc[start:target_idx - 1]["Volume"]
    if hist_vol.empty or hist_vol.mean() <= 0:
        return SignalPrediction("volume", param_key, "neutral")

    vol_ratio = yesterday_vol / hist_vol.mean()
    if vol_ratio >= VOLUME_RATIO_THRESHOLD:
        # High volume confirms candle direction
        return SignalPrediction("volume", param_key, candle_direction, vol_ratio)
    if vol_ratio < 0.8:
        # Low volume → weak (neutral)
        return SignalPrediction("volume", param_key, "neutral", vol_ratio)
    # Normal volume → follow candle direction with lower strength
    return SignalPrediction("volume", param_key, candle_direction, vol_ratio * 0.5)


def compute_hourly_ema(
    hourly_bars: pd.DataFrame,
    target_date: date,
    fast_period: int,
    slow_period: int,
) -> SignalPrediction:
    """EMA crossover on hourly bars up to end of previous day."""
    param_key = f"ema_{fast_period}_{slow_period}"
    # Filter hourly bars up to previous day end
    prev_end = pd.Timestamp(target_date, tz=hourly_bars.index.tz) if not hourly_bars.empty and hourly_bars.index.tz else pd.Timestamp(target_date)
    hist = hourly_bars[hourly_bars.index.date < target_date]
    if len(hist) < slow_period + 5:
        return SignalPrediction("ema", param_key, "neutral")

    closes = hist["Close"]
    fast_ema = closes.ewm(span=fast_period, adjust=False).mean()
    slow_ema = closes.ewm(span=slow_period, adjust=False).mean()

    last_fast = fast_ema.iloc[-1]
    last_slow = slow_ema.iloc[-1]

    if last_fast > last_slow:
        strength = (last_fast - last_slow) / last_slow if last_slow > 0 else 0
        return SignalPrediction("ema", param_key, "bullish", abs(strength))
    if last_fast < last_slow:
        strength = (last_slow - last_fast) / last_slow if last_slow > 0 else 0
        return SignalPrediction("ema", param_key, "bearish", abs(strength))
    return SignalPrediction("ema", param_key, "neutral")


def compute_gap_direction(
    daily_bars: pd.DataFrame,
    target_idx: int,
    atr_multiplier: float,
    atr_period: int = 14,
) -> SignalPrediction:
    """ATR-normalized gap direction signal."""
    param_key = f"atr_{int(atr_multiplier * 100)}"
    if target_idx < 2:
        return SignalPrediction("gap", param_key, "neutral")

    today_open = daily_bars.iloc[target_idx]["Open"]
    prev_close = daily_bars.iloc[target_idx - 1]["Close"]
    if prev_close <= 0:
        return SignalPrediction("gap", param_key, "neutral")

    gap = today_open - prev_close
    atr = _compute_atr(daily_bars.iloc[:target_idx], atr_period)
    if atr <= 0:
        return SignalPrediction("gap", param_key, "neutral")

    normalized_gap = gap / atr
    if abs(normalized_gap) < atr_multiplier:
        return SignalPrediction("gap", param_key, "neutral")

    direction = "bullish" if normalized_gap > 0 else "bearish"
    return SignalPrediction("gap", param_key, direction, abs(normalized_gap))


# ── Label Computation ──

def compute_label_a(day_bars_1m: pd.DataFrame) -> tuple[str, str]:
    """Compute Label A: raw direction labels for a single day's 1m bars.

    Returns (close_vs_open, close_vs_vwap).
    """
    if day_bars_1m.empty or len(day_bars_1m) < 5:
        return "neutral", "neutral"

    day_open = float(day_bars_1m.iloc[0]["Open"])
    day_close = float(day_bars_1m.iloc[-1]["Close"])
    vwap = calculate_vwap(day_bars_1m)

    close_vs_open = "bullish" if day_close > day_open else "bearish"
    close_vs_vwap = "bullish" if (vwap > 0 and day_close > vwap) else "bearish"

    return close_vs_open, close_vs_vwap


def compute_time_segments(day_bars_1m: pd.DataFrame) -> dict[str, str]:
    """Compute direction for AM1/AM2/PM segments."""
    segments = {"AM1": "neutral", "AM2": "neutral", "PM": "neutral"}
    if day_bars_1m.empty:
        return segments

    am1 = day_bars_1m[day_bars_1m.index.time < dt_time(11, 0)]
    if len(am1) >= 2:
        segments["AM1"] = "bullish" if float(am1.iloc[-1]["Close"]) > float(am1.iloc[0]["Open"]) else "bearish"

    am2 = day_bars_1m[(day_bars_1m.index.time >= dt_time(11, 0)) & (day_bars_1m.index.time < dt_time(13, 0))]
    if len(am2) >= 2:
        segments["AM2"] = "bullish" if float(am2.iloc[-1]["Close"]) > float(am2.iloc[0]["Open"]) else "bearish"

    pm = day_bars_1m[day_bars_1m.index.time >= dt_time(13, 0)]
    if len(pm) >= 2:
        segments["PM"] = "bullish" if float(pm.iloc[-1]["Close"]) > float(pm.iloc[0]["Open"]) else "bearish"

    return segments


# ── VIX History ──

def fetch_vix_history(days: int) -> pd.Series:
    """Fetch historical VIX close from yfinance. Returns Series indexed by date."""
    try:
        import yfinance as yf
        from zoneinfo import ZoneInfo
        end = datetime.now(ZoneInfo("America/New_York")).date()
        start = end - timedelta(days=int(days * 1.6))
        df = yf.download("^VIX", start=str(start), end=str(end), progress=False)
        if df.empty:
            return pd.Series(dtype=float)
        # Handle both single-level and multi-level columns
        if isinstance(df.columns, pd.MultiIndex):
            close = df[("Close", "^VIX")]
        else:
            close = df["Close"]
        return close.squeeze()
    except Exception as e:
        logger.warning("Failed to fetch VIX history: %s", e)
        return pd.Series(dtype=float)


def _vix_bucket(level: float) -> str:
    if level < VIX_LOW:
        return "low"
    if level > VIX_HIGH:
        return "high"
    return "mid"


# ── Evaluator ──

class DailyBiasEvaluator:
    """Orchestrates Phase 0 daily bias signal validation."""

    def __init__(
        self,
        config: dict | None = None,
        vp_lookback_days: int = 5,
        rvol_lookback_days: int = 10,
    ) -> None:
        cfg = config or {}
        vp_cfg = cfg.get("volume_profile", {})
        rvol_cfg = cfg.get("rvol", {})

        self.vp_lookback = vp_cfg.get("lookback_trading_days", vp_lookback_days)
        self.rvol_lookback = rvol_cfg.get("lookback_days", rvol_lookback_days)
        self.skip_open_minutes = rvol_cfg.get("skip_open_minutes", 3)
        self.value_area_pct = vp_cfg.get("value_area_pct", 0.70)
        self.recency_decay = vp_cfg.get("recency_decay", 0.15)
        self.regime_cfg = cfg.get("regime", {})
        self.sim_cfg = cfg.get("simulation", {})

    def evaluate(
        self,
        bars_by_symbol: dict[str, pd.DataFrame],
        vix_history: pd.Series | None = None,
    ) -> DailyBiasReport:
        """Run full daily bias evaluation.

        Args:
            bars_by_symbol: 1m bar DataFrames per symbol (from USDataLoader)
            vix_history: Optional VIX close Series indexed by date
        """
        if not bars_by_symbol:
            return DailyBiasReport([], 0, 0, 0, 0)

        # Step 1: Resample
        daily_by_symbol: dict[str, pd.DataFrame] = {}
        hourly_by_symbol: dict[str, pd.DataFrame] = {}
        for sym, bars in bars_by_symbol.items():
            daily_by_symbol[sym] = resample_daily(bars)
            hourly_by_symbol[sym] = resample_hourly(bars)

        # Step 2: Regime evaluation (reuse existing)
        regime_eval_cfg = {
            "gap_and_go_rvol": self.regime_cfg.get("gap_and_go_rvol", 1.5),
            "trend_day_rvol": self.regime_cfg.get("trend_day_rvol", 1.2),
            "fade_chop_rvol": self.regime_cfg.get("fade_chop_rvol", 1.0),
            "min_vp_trading_days": 3,
            "adaptive": self.regime_cfg.get("adaptive", {}),
        }
        regime_result = evaluate_regimes(
            bars_by_symbol,
            vp_lookback_days=self.vp_lookback,
            rvol_lookback_days=self.rvol_lookback,
            skip_open_minutes=self.skip_open_minutes,
            value_area_pct=self.value_area_pct,
            regime_cfg=regime_eval_cfg,
            recency_decay=self.recency_decay,
        )

        # Step 3: Trade simulation for Label B
        tp = self.sim_cfg.get("tp_pct", 0.5) / 100
        sl = self.sim_cfg.get("sl_pct", 0.25) / 100
        slippage = self.sim_cfg.get("slippage_per_leg", 0.03) / 100
        exit_mode = self.sim_cfg.get("exit_mode", "trailing")
        trail_act = self.sim_cfg.get("trailing_activation_pct", 0.4) / 100
        trail_pct = self.sim_cfg.get("trailing_trail_pct", 0.2) / 100

        simulator = USTradeSimulator(
            tp_pct=tp, sl_pct=sl, slippage_per_leg=slippage,
            exit_mode=exit_mode, trailing_activation_pct=trail_act,
            trailing_trail_pct=trail_pct,
        )
        sim_result = simulator.simulate_from_regimes(bars_by_symbol, regime_result.days)

        # Build trade map: (date, symbol) -> SimTrade
        trade_map: dict[tuple, SimTrade] = {}
        for trade in sim_result.trades:
            td = trade.entry_time.date() if hasattr(trade.entry_time, "date") else trade.entry_time
            trade_map[(td, trade.symbol)] = trade

        # Step 4: Compute sub-signals + labels for each day
        day_results: list[DayResult] = []

        for regime_day in regime_result.days:
            sym = regime_day.symbol
            target_date = regime_day.date

            if sym not in daily_by_symbol:
                continue

            daily = daily_by_symbol[sym]
            hourly = hourly_by_symbol[sym]
            bars_1m = bars_by_symbol[sym]

            # Find target index in daily bars
            daily_dates = [d.date() if hasattr(d, "date") else d for d in daily.index]
            try:
                target_idx = next(
                    i for i, d in enumerate(daily_dates)
                    if d == target_date
                )
            except StopIteration:
                continue

            # Compute all signal predictions
            predictions: list[SignalPrediction] = []

            # Structure signals
            for w in STRUCTURE_WINDOWS:
                predictions.append(compute_daily_structure(daily, target_idx, w))

            # Candle signals — pick best for volume modifier
            candle_dir = "neutral"
            for br in CANDLE_BODY_RATIOS:
                pred = compute_yesterday_candle(daily, target_idx, br)
                predictions.append(pred)
                if br == 0.3 and pred.direction != "neutral":
                    candle_dir = pred.direction

            # Volume modifier (uses candle direction from lowest threshold)
            predictions.append(compute_yesterday_volume(daily, target_idx, candle_dir))

            # EMA signals
            for fast, slow in EMA_PAIRS:
                predictions.append(compute_hourly_ema(hourly, target_date, fast, slow))

            # Gap signals
            for mult in GAP_ATR_MULTIPLIERS:
                predictions.append(compute_gap_direction(daily, target_idx, mult))

            # Label A
            day_bars = bars_1m[bars_1m.index.date == target_date]
            lbl_a_open, lbl_a_vwap = compute_label_a(day_bars)

            # Label B
            trade = trade_map.get((target_date, sym))
            label_b_pnl = None
            label_b_trade_dir = None
            label_b_actual = None
            if trade is not None:
                label_b_pnl = trade.net_pnl_pct
                label_b_trade_dir = "short" if "short" in trade.signal_type else "long"
                # Infer actual direction from trade result
                if trade.net_pnl_pct > 0:
                    label_b_actual = "bearish" if "short" in trade.signal_type else "bullish"
                else:
                    label_b_actual = "bullish" if "short" in trade.signal_type else "bearish"

            # VIX
            vix_level = 0.0
            if vix_history is not None and not vix_history.empty:
                # Try to match by date
                for vix_date in [target_date, pd.Timestamp(target_date)]:
                    if vix_date in vix_history.index:
                        vix_level = float(vix_history[vix_date])
                        break
                    # Try with tz-naive comparison
                    matching = vix_history[vix_history.index.date == target_date] if hasattr(vix_history.index, "date") else pd.Series(dtype=float)
                    if not matching.empty:
                        vix_level = float(matching.iloc[0])
                        break

            # Time segments
            segments = compute_time_segments(day_bars)

            day_results.append(DayResult(
                date=target_date,
                symbol=sym,
                predictions=predictions,
                label_a_close_vs_open=lbl_a_open,
                label_a_close_vs_vwap=lbl_a_vwap,
                label_b_regime=regime_day.predicted,
                label_b_trade_direction=label_b_trade_dir,
                label_b_pnl=label_b_pnl,
                label_b_actual_direction=label_b_actual,
                vix_level=vix_level,
                vix_bucket=_vix_bucket(vix_level) if vix_level > 0 else "mid",
                am1_direction=segments["AM1"],
                am2_direction=segments["AM2"],
                pm_direction=segments["PM"],
            ))

        # Step 5: Analysis
        return self._compile_report(
            day_results, regime_result.days, bars_by_symbol,
        )

    def _compile_report(
        self,
        day_results: list[DayResult],
        regime_days: list[RegimeEvalDay],
        bars_by_symbol: dict[str, pd.DataFrame],
    ) -> DailyBiasReport:
        """Compile all analysis into the final report."""
        if not day_results:
            return DailyBiasReport([], 0, 0, 0, 0)

        symbols = sorted(set(d.symbol for d in day_results))
        all_dates = set(d.date for d in day_results)
        trading_days = len(all_dates)
        total_samples = len(day_results)
        # Conservative effective N estimate
        effective_n = min(total_samples, int(trading_days * 2.1))

        # Period in calendar days
        if all_dates:
            sorted_dates = sorted(all_dates)
            period_days = (sorted_dates[-1] - sorted_dates[0]).days
        else:
            period_days = 0

        # A. Signal win rates per param
        all_param_results = self._compute_param_win_rates(day_results)

        # Best param per signal type
        signal_results = self._select_best_params(all_param_results)

        # B. Correlations (using best params)
        correlations = self._compute_correlations(day_results, signal_results)

        # C. Aggregation
        aggregate_results = self._compute_aggregation(day_results, signal_results)

        # D. Confidence sensitivity
        confidence_sensitivity = self._compute_confidence_sensitivity(regime_days)

        # E. Time segment analysis
        time_segments = self._compute_time_segments(day_results, signal_results)

        # F. VIX stratification
        stratified = self._compute_stratified(day_results, signal_results)

        # G. Go/No-Go verdicts
        verdicts, overall, recommendation = self._evaluate_go_nogo(
            signal_results, correlations, aggregate_results,
            confidence_sensitivity, time_segments,
        )

        return DailyBiasReport(
            symbols=symbols,
            period_days=period_days,
            trading_days=trading_days,
            total_samples=total_samples,
            effective_n=effective_n,
            signal_results=signal_results,
            all_param_results=all_param_results,
            correlations=correlations,
            aggregate_results=aggregate_results,
            confidence_sensitivity=confidence_sensitivity,
            time_segments=time_segments,
            stratified=stratified,
            verdicts=verdicts,
            overall_verdict=overall,
            recommendation=recommendation,
        )

    def _compute_param_win_rates(self, day_results: list[DayResult]) -> list[ParamWinRate]:
        """Compute win rates for every signal × param combination."""
        # Collect all unique (signal_type, param_key) pairs
        param_keys: set[tuple[str, str]] = set()
        for d in day_results:
            for p in d.predictions:
                param_keys.add((p.signal_type, p.param_key))

        results = []
        for sig_type, param_key in sorted(param_keys):
            # Label A: count directional predictions that match close_vs_open
            hits_a, total_a = 0, 0
            hits_a_vwap, total_a_vwap = 0, 0
            # Label B: count directional predictions that match trade result
            hits_b, total_b = 0, 0

            for d in day_results:
                pred = next(
                    (p for p in d.predictions if p.signal_type == sig_type and p.param_key == param_key),
                    None,
                )
                if pred is None or pred.direction == "neutral":
                    continue

                # Label A (close_vs_open)
                total_a += 1
                if pred.direction == d.label_a_close_vs_open:
                    hits_a += 1
                # Label A (close_vs_vwap)
                total_a_vwap += 1
                if pred.direction == d.label_a_close_vs_vwap:
                    hits_a_vwap += 1

                # Label B
                if d.label_b_actual_direction is not None:
                    total_b += 1
                    if pred.direction == d.label_b_actual_direction:
                        hits_b += 1

            wr_a = hits_a / total_a if total_a > 0 else 0.0
            wr_a_vwap = hits_a_vwap / total_a_vwap if total_a_vwap > 0 else 0.0
            wr_b = hits_b / total_b if total_b > 0 else 0.0
            p_val = _binomial_p_value(hits_a, total_a)

            results.append(ParamWinRate(
                signal_type=sig_type,
                param_key=param_key,
                raw_dir_wr=wr_a,
                raw_dir_vwap_wr=wr_a_vwap,
                regime_pnl_wr=wr_b,
                sample_a=total_a,
                sample_b=total_b,
                p_value_a=p_val,
            ))

        return results

    def _select_best_params(self, all_results: list[ParamWinRate]) -> list[ParamWinRate]:
        """Select the best parameter for each signal type (by raw direction WR)."""
        best: dict[str, ParamWinRate] = {}
        for r in all_results:
            if r.signal_type not in best or r.raw_dir_wr > best[r.signal_type].raw_dir_wr:
                best[r.signal_type] = r
        return [best[s] for s in SIGNAL_TYPES if s in best]

    def _compute_correlations(
        self,
        day_results: list[DayResult],
        best_params: list[ParamWinRate],
    ) -> list[CorrelationPair]:
        """Pearson + Spearman correlation between best-param signal directions."""
        # Build numeric series per signal
        param_map = {r.signal_type: r.param_key for r in best_params}
        series: dict[str, list[int]] = {s: [] for s in param_map}

        for d in day_results:
            for sig_type, param_key in param_map.items():
                pred = next(
                    (p for p in d.predictions if p.signal_type == sig_type and p.param_key == param_key),
                    None,
                )
                series[sig_type].append(_dir_to_num(pred.direction) if pred else 0)

        pairs = []
        sig_names = list(param_map.keys())
        for i in range(len(sig_names)):
            for j in range(i + 1, len(sig_names)):
                a, b = sig_names[i], sig_names[j]
                arr_a, arr_b = np.array(series[a], dtype=float), np.array(series[b], dtype=float)
                if np.std(arr_a) == 0 or np.std(arr_b) == 0:
                    pairs.append(CorrelationPair(a, b, 0.0, 0.0))
                    continue
                pearson = float(np.corrcoef(arr_a, arr_b)[0, 1])
                # Spearman via rank correlation
                rank_a = pd.Series(arr_a).rank().values
                rank_b = pd.Series(arr_b).rank().values
                spearman = float(np.corrcoef(rank_a, rank_b)[0, 1])
                pairs.append(CorrelationPair(a, b, round(spearman, 3), round(pearson, 3)))

        return pairs

    def _compute_aggregation(
        self,
        day_results: list[DayResult],
        best_params: list[ParamWinRate],
    ) -> list[AggregateResult]:
        """Test multiple weight schemes for aggregated bias prediction."""
        param_map = {r.signal_type: r.param_key for r in best_params}
        results = []

        for scheme_name, weights in WEIGHT_SCHEMES.items():
            hits_a, total_a = 0, 0
            hits_b, total_b = 0, 0

            for d in day_results:
                weighted_sum = 0.0
                total_weight = 0.0

                for sig_type, w in weights.items():
                    if w == 0 or sig_type not in param_map:
                        continue
                    pred = next(
                        (p for p in d.predictions
                         if p.signal_type == sig_type and p.param_key == param_map[sig_type]),
                        None,
                    )
                    if pred is None:
                        continue
                    weighted_sum += w * _dir_to_num(pred.direction)
                    total_weight += w

                if total_weight == 0:
                    continue

                agg_dir = "bullish" if weighted_sum > 0 else ("bearish" if weighted_sum < 0 else "neutral")
                if agg_dir == "neutral":
                    continue

                # Label A
                total_a += 1
                if agg_dir == d.label_a_close_vs_open:
                    hits_a += 1

                # Label B
                if d.label_b_actual_direction is not None:
                    total_b += 1
                    if agg_dir == d.label_b_actual_direction:
                        hits_b += 1

            results.append(AggregateResult(
                scheme=scheme_name,
                raw_dir_wr=hits_a / total_a if total_a else 0.0,
                regime_pnl_wr=hits_b / total_b if total_b else 0.0,
                sample_a=total_a,
                sample_b=total_b,
            ))

        return results

    def _compute_confidence_sensitivity(
        self,
        regime_days: list[RegimeEvalDay],
    ) -> list[ConfidenceSensitivity]:
        """How much does a confidence modifier change trading decisions?"""
        modifiers = [-0.15, -0.10, -0.05, 0.05, 0.10, 0.15]
        tradeable = [d for d in regime_days if d.predicted != USRegimeType.UNCLEAR]
        total = len(tradeable)
        if total == 0:
            return []

        results = []
        for mod in modifiers:
            changes = 0
            for d in tradeable:
                original = d.confidence
                modified = max(0, min(1, original + mod))
                # Check scan trigger change
                orig_scan = original >= CONFIDENCE_SCAN_THRESHOLD
                mod_scan = modified >= CONFIDENCE_SCAN_THRESHOLD
                if orig_scan != mod_scan:
                    changes += 1
                    continue
                # Check observe mode change
                orig_obs = original < CONFIDENCE_OBSERVE_THRESHOLD
                mod_obs = modified < CONFIDENCE_OBSERVE_THRESHOLD
                if orig_obs != mod_obs:
                    changes += 1

            results.append(ConfidenceSensitivity(
                modifier=mod,
                decisions_changed=changes,
                pct_changed=changes / total * 100,
            ))
        return results

    def _compute_time_segments(
        self,
        day_results: list[DayResult],
        best_params: list[ParamWinRate],
    ) -> list[TimeSegmentResult]:
        """Exploratory: signal win rates per time segment (AM1/AM2/PM)."""
        param_map = {r.signal_type: r.param_key for r in best_params}
        results = []

        for sig_type, param_key in param_map.items():
            for segment in ["AM1", "AM2", "PM"]:
                hits, total = 0, 0
                for d in day_results:
                    pred = next(
                        (p for p in d.predictions
                         if p.signal_type == sig_type and p.param_key == param_key),
                        None,
                    )
                    if pred is None or pred.direction == "neutral":
                        continue
                    seg_dir = getattr(d, f"{segment.lower()}_direction", "neutral")
                    if seg_dir == "neutral":
                        continue
                    total += 1
                    if pred.direction == seg_dir:
                        hits += 1

                results.append(TimeSegmentResult(
                    signal_type=sig_type,
                    param_key=param_key,
                    segment=segment,
                    win_rate=hits / total if total > 0 else 0.0,
                    sample=total,
                ))

        return results

    def _compute_stratified(
        self,
        day_results: list[DayResult],
        best_params: list[ParamWinRate],
    ) -> list[StratifiedResult]:
        """Exploratory: signal win rates stratified by VIX bucket."""
        param_map = {r.signal_type: r.param_key for r in best_params}
        results = []

        # Also compute aggregated bias per VIX bucket
        all_signal_types = list(param_map.keys()) + ["aggregated"]

        for sig_type in all_signal_types:
            for bucket in ["low", "mid", "high"]:
                hits, total = 0, 0
                for d in day_results:
                    if d.vix_bucket != bucket:
                        continue

                    if sig_type == "aggregated":
                        # Use equal weights for aggregated
                        wsum = 0.0
                        wt = 0.0
                        for st, pk in param_map.items():
                            pred = next(
                                (p for p in d.predictions if p.signal_type == st and p.param_key == pk),
                                None,
                            )
                            if pred:
                                wsum += _dir_to_num(pred.direction)
                                wt += 1
                        if wt == 0:
                            continue
                        direction = "bullish" if wsum > 0 else ("bearish" if wsum < 0 else "neutral")
                    else:
                        pred = next(
                            (p for p in d.predictions
                             if p.signal_type == sig_type and p.param_key == param_map[sig_type]),
                            None,
                        )
                        if pred is None:
                            continue
                        direction = pred.direction

                    if direction == "neutral":
                        continue
                    total += 1
                    if direction == d.label_a_close_vs_open:
                        hits += 1

                results.append(StratifiedResult(
                    signal_type=sig_type,
                    vix_bucket=bucket,
                    win_rate=hits / total if total > 0 else 0.0,
                    sample=total,
                ))

        return results

    def _evaluate_go_nogo(
        self,
        signal_results: list[ParamWinRate],
        correlations: list[CorrelationPair],
        aggregates: list[AggregateResult],
        conf_sensitivity: list[ConfidenceSensitivity],
        time_segments: list[TimeSegmentResult],
    ) -> tuple[list[GoNoGoVerdict], str, str]:
        """Evaluate all Go/No-Go criteria."""
        verdicts: list[GoNoGoVerdict] = []

        # G1: At least 1 sub-signal raw direction WR > 55%
        best_wr = max((r.raw_dir_wr for r in signal_results), default=0)
        best_sig = next((r for r in signal_results if r.raw_dir_wr == best_wr), None)
        best_sig_name = best_sig.signal_type if best_sig else "none"
        g1_pass = best_wr > G1_RAW_DIR_MIN_WR
        # Check MDE: with sample ~250, 55% is at MDE boundary
        g1_inconclusive = not g1_pass and best_wr >= 0.52
        verdicts.append(GoNoGoVerdict(
            "G1", f"Sub-signal raw dir WR (best: {best_sig_name})",
            f"> {G1_RAW_DIR_MIN_WR:.0%}", f"{best_wr:.1%}",
            "PASS" if g1_pass else ("INCONCLUSIVE" if g1_inconclusive else "FAIL"),
            "MUST",
        ))

        # G2: Aggregated >= best single signal (no degradation)
        best_single_wr = best_wr
        best_agg_wr = max((a.raw_dir_wr for a in aggregates), default=0)
        best_scheme = next((a.scheme for a in aggregates if a.raw_dir_wr == best_agg_wr), "none")
        g2_pass = best_agg_wr >= best_single_wr
        verdicts.append(GoNoGoVerdict(
            "G2", f"Aggregated WR >= best single (scheme: {best_scheme})",
            f">= {best_single_wr:.1%}", f"{best_agg_wr:.1%}",
            "PASS" if g2_pass else "FAIL",
            "SHOULD",
        ))

        # G3: Structure vs EMA correlation < 0.7
        struct_ema_corr = next(
            (c for c in correlations
             if ("structure" in {c.signal_a, c.signal_b} and "ema" in {c.signal_a, c.signal_b})),
            None,
        )
        corr_val = abs(struct_ema_corr.spearman) if struct_ema_corr else 0.0
        g3_pass = corr_val < G3_MAX_CORRELATION
        verdicts.append(GoNoGoVerdict(
            "G3", "Structure vs EMA correlation",
            f"< {G3_MAX_CORRELATION}", f"{corr_val:.3f}",
            "PASS" if g3_pass else "FAIL",
            "SHOULD",
        ))

        # G4: Confidence modifier affects >= 10% decisions
        max_impact = max((c.pct_changed for c in conf_sensitivity), default=0)
        g4_pass = max_impact >= G4_MIN_DECISION_IMPACT * 100
        verdicts.append(GoNoGoVerdict(
            "G4", "Confidence modifier decision impact",
            f">= {G4_MIN_DECISION_IMPACT:.0%}", f"{max_impact:.1f}%",
            "PASS" if g4_pass else "FAIL",
            "MUST",
        ))

        # G5: Candle raw direction WR > 50%
        candle_result = next((r for r in signal_results if r.signal_type == "candle"), None)
        candle_wr = candle_result.raw_dir_wr if candle_result else 0.0
        g5_pass = candle_wr > G5_CANDLE_FLOOR
        g5_inconclusive = not g5_pass and candle_wr >= G5_CANDLE_FLOOR  # exactly 50% is borderline
        verdicts.append(GoNoGoVerdict(
            "G5", "Candle raw dir WR",
            f"> {G5_CANDLE_FLOOR:.0%}", f"{candle_wr:.1%}",
            "PASS" if g5_pass else ("INCONCLUSIVE" if g5_inconclusive else "FAIL"),
            "OPTIONAL",
        ))

        # G6: PM segment WR > 50% (exploratory)
        pm_results = [t for t in time_segments if t.segment == "PM" and t.sample > 0]
        avg_pm_wr = np.mean([t.win_rate for t in pm_results]) if pm_results else 0.0
        g6_pass = avg_pm_wr > G6_PM_FLOOR
        g6_inconclusive = not g6_pass and avg_pm_wr >= 0.45
        verdicts.append(GoNoGoVerdict(
            "G6", "PM segment avg WR",
            f"> {G6_PM_FLOOR:.0%}", f"{avg_pm_wr:.1%}",
            "PASS" if g6_pass else ("INCONCLUSIVE" if g6_inconclusive else "FAIL"),
            "EXPLORATORY",
        ))

        # Overall verdict
        must_pass = all(v.verdict == "PASS" for v in verdicts if v.level == "MUST")
        must_fail = any(v.verdict == "FAIL" for v in verdicts if v.level == "MUST")
        n_pass = sum(1 for v in verdicts if v.verdict == "PASS")
        n_inconclusive = sum(1 for v in verdicts if v.verdict == "INCONCLUSIVE")

        if must_fail:
            overall = "FAIL"
            recommendation = "放弃 daily bias（G1 FAIL = 信号无预测力）" if not g1_pass else "改为方向过滤器"
        elif must_pass and n_pass >= 4:
            overall = "PASS"
            recommendation = "全部信号集成" if n_pass == 6 else "裁剪后信号集成"
        elif n_inconclusive > 0:
            overall = "PARTIAL"
            recommendation = f"Paper trading 验证 {n_inconclusive} 个 INCONCLUSIVE 信号"
        else:
            overall = "PARTIAL"
            recommendation = "裁剪后信号集成（部分条件通过）"

        return verdicts, overall, recommendation


# ── Report Formatting ──

def format_report(report: DailyBiasReport, verbose: bool = False) -> str:
    """Format the evaluation report as text."""
    lines: list[str] = []
    sep = "=" * 76

    lines.append(sep)
    lines.append("  Daily Bias Signal Validation Report")
    lines.append(f"  Symbols: {', '.join(report.symbols)}")
    lines.append(
        f"  Period: {report.period_days} cal days (~{report.trading_days} trading days), "
        f"pooled N={report.total_samples}"
    )
    lines.append(f"  Effective independent samples: ~{report.effective_n} (conservative beta-adjusted)")
    lines.append(f"  Total comparisons: ~36 (expect ~1.8 false positives at α=0.05)")
    lines.append(sep)

    # Section 1: Sub-signal win rates (dual label)
    lines.append("")
    lines.append("  ── 子信号独立预测力（双标签）──")
    lines.append(
        f"  {'Signal':<18} {'Raw Dir WR(A)':>13} {'VWAP WR(A)':>11} "
        f"{'Regime P&L(B)':>13} {'Sample':>7} {'Best Param':>12} {'p-value':>8} {'Verdict':>12}"
    )
    lines.append("  " + "-" * 100)
    for r in report.signal_results:
        verdict = _param_verdict(r)
        lines.append(
            f"  {r.signal_type:<18} {r.raw_dir_wr:>12.1%} {r.raw_dir_vwap_wr:>11.1%} "
            f"{r.regime_pnl_wr:>12.1%} {r.sample_a:>7} {r.param_key:>12} "
            f"{r.p_value_a:>8.4f} {verdict:>12}"
        )

    # Section 2: Correlation matrix
    if report.correlations:
        lines.append("")
        lines.append("  ── 信号相关性矩阵 ──")
        lines.append(f"  {'Pair':<25} {'Spearman':>10} {'Pearson':>10}")
        lines.append("  " + "-" * 47)
        for c in report.correlations:
            flag = " ⚠" if abs(c.spearman) >= G3_MAX_CORRELATION else ""
            lines.append(f"  {c.signal_a} × {c.signal_b:<14} {c.spearman:>10.3f} {c.pearson:>10.3f}{flag}")

    # Section 3: Aggregation
    if report.aggregate_results:
        lines.append("")
        lines.append("  ── 聚合 Bias 预测力（双标签）──")
        lines.append(f"  {'Weight Scheme':<20} {'Raw(A)':>8} {'P&L(B)':>8} {'Sample':>7}")
        lines.append("  " + "-" * 45)
        for a in report.aggregate_results:
            lines.append(f"  {a.scheme:<20} {a.raw_dir_wr:>7.1%} {a.regime_pnl_wr:>7.1%} {a.sample_a:>7}")

    # Section 4: Confidence sensitivity
    if report.confidence_sensitivity:
        lines.append("")
        lines.append("  ── Confidence 敏感度 ──")
        lines.append(f"  {'Modifier':>10} {'Decisions Changed':>18} {'% Changed':>10}")
        lines.append("  " + "-" * 40)
        for c in report.confidence_sensitivity:
            lines.append(f"  {c.modifier:>+10.2f} {c.decisions_changed:>18} {c.pct_changed:>9.1f}%")

    # Section 5: Time segments (exploratory)
    if report.time_segments:
        lines.append("")
        lines.append("  ── 信号时效性（探索性，阈值 55%）──")
        lines.append(f"  {'Signal':<18} {'AM1 WR':>8} {'AM2 WR':>8} {'PM WR':>8}")
        lines.append("  " + "-" * 44)
        sig_types_seen: set[str] = set()
        for t in report.time_segments:
            sig_types_seen.add(t.signal_type)
        for sig in sorted(sig_types_seen):
            am1 = next((t for t in report.time_segments if t.signal_type == sig and t.segment == "AM1"), None)
            am2 = next((t for t in report.time_segments if t.signal_type == sig and t.segment == "AM2"), None)
            pm = next((t for t in report.time_segments if t.signal_type == sig and t.segment == "PM"), None)
            lines.append(
                f"  {sig:<18} "
                f"{_fmt_wr(am1):>8} "
                f"{_fmt_wr(am2):>8} "
                f"{_fmt_wr(pm):>8}"
            )
        lines.append("  Note: exploratory — conclusions labeled as hypotheses for Phase 1 confirmation")

    # Section 6: VIX stratification (exploratory)
    if report.stratified:
        lines.append("")
        lines.append("  ── 宏观环境分层（探索性）──")
        lines.append(f"  {'Signal':<18} {'Low VIX(<16)':>13} {'Mid VIX(16-24)':>15} {'High VIX(>24)':>14}")
        lines.append("  " + "-" * 62)
        strat_sigs = sorted(set(s.signal_type for s in report.stratified))
        for sig in strat_sigs:
            low = next((s for s in report.stratified if s.signal_type == sig and s.vix_bucket == "low"), None)
            mid = next((s for s in report.stratified if s.signal_type == sig and s.vix_bucket == "mid"), None)
            high = next((s for s in report.stratified if s.signal_type == sig and s.vix_bucket == "high"), None)
            lines.append(
                f"  {sig:<18} "
                f"{_fmt_strat(low):>13} "
                f"{_fmt_strat(mid):>15} "
                f"{_fmt_strat(high):>14}"
            )
        lines.append("  Note: if signal effective only in specific regime, Phase 1 adds environment gating")

    # Section 7: Go/No-Go verdicts
    lines.append("")
    lines.append("  ── Go/No-Go 判定 ──")
    lines.append(f"  {'G#':<4} {'Criterion':<45} {'Threshold':>10} {'Observed':>10} {'Level':>12} {'Verdict':>12}")
    lines.append("  " + "-" * 96)
    for v in report.verdicts:
        lines.append(
            f"  {v.code:<4} {v.description:<45} {v.threshold:>10} {v.observed:>10} "
            f"{v.level:>12} {v.verdict:>12}"
        )

    lines.append("")
    lines.append(f"  Overall: {report.overall_verdict}")
    lines.append(f"  Recommendation: {report.recommendation}")
    lines.append(sep)

    return "\n".join(lines)


def _param_verdict(r: ParamWinRate) -> str:
    if r.sample_a < 30:
        return "LOW_SAMPLE"
    if r.raw_dir_wr > G1_RAW_DIR_MIN_WR and r.p_value_a < 0.05:
        return "PASS"
    if r.raw_dir_wr > G1_RAW_DIR_MIN_WR:
        return "WEAK_PASS"
    if r.raw_dir_wr >= 0.52:
        return "INCONCLUSIVE"
    if r.raw_dir_wr < G5_CANDLE_FLOOR:
        return "FAIL"
    return "NEUTRAL"


def _fmt_wr(t: TimeSegmentResult | None) -> str:
    if t is None or t.sample == 0:
        return "N/A"
    return f"{t.win_rate:.1%}"


def _fmt_strat(s: StratifiedResult | None) -> str:
    if s is None or s.sample == 0:
        return "N/A"
    return f"{s.win_rate:.1%}({s.sample})"


def format_json(report: DailyBiasReport) -> str:
    """Export report as JSON."""
    data: dict[str, Any] = {
        "meta": {
            "symbols": report.symbols,
            "period_days": report.period_days,
            "trading_days": report.trading_days,
            "total_samples": report.total_samples,
            "effective_n": report.effective_n,
        },
        "signal_results": [
            {
                "signal_type": r.signal_type,
                "param_key": r.param_key,
                "raw_dir_wr": round(r.raw_dir_wr, 4),
                "raw_dir_vwap_wr": round(r.raw_dir_vwap_wr, 4),
                "regime_pnl_wr": round(r.regime_pnl_wr, 4),
                "sample_a": r.sample_a,
                "sample_b": r.sample_b,
                "p_value_a": round(r.p_value_a, 6),
            }
            for r in report.signal_results
        ],
        "correlations": [
            {"pair": f"{c.signal_a}×{c.signal_b}", "spearman": c.spearman, "pearson": c.pearson}
            for c in report.correlations
        ],
        "aggregation": [
            {"scheme": a.scheme, "raw_dir_wr": round(a.raw_dir_wr, 4), "regime_pnl_wr": round(a.regime_pnl_wr, 4)}
            for a in report.aggregate_results
        ],
        "confidence_sensitivity": [
            {"modifier": c.modifier, "pct_changed": round(c.pct_changed, 1)}
            for c in report.confidence_sensitivity
        ],
        "verdicts": [
            {"code": v.code, "verdict": v.verdict, "level": v.level, "observed": v.observed}
            for v in report.verdicts
        ],
        "overall": report.overall_verdict,
        "recommendation": report.recommendation,
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


# ── CLI ──

def _load_settings(path: str = "config/us_playbook_settings.yaml") -> dict:
    import yaml
    p = Path(path)
    if not p.exists():
        logger.warning("Settings not found: %s, using defaults", path)
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def _get_default_symbols(settings: dict) -> list[str]:
    return [item["symbol"] for item in settings.get("watchlist", [])]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily Bias Signal Validation (Phase 0)"
    )
    parser.add_argument(
        "-y", "--symbol",
        help="Comma-separated symbols (default: all from config)",
    )
    parser.add_argument(
        "-d", "--days", type=int, default=180,
        help="Calendar days to evaluate (default: 180)",
    )
    parser.add_argument(
        "--all-watchlist", action="store_true",
        help="Use all symbols from us_playbook_settings.yaml watchlist",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "-o", "--output", choices=["table", "json"], default="table",
        help="Output format (default: table)",
    )
    parser.add_argument("--futu-host", default="127.0.0.1")
    parser.add_argument("--futu-port", type=int, default=11111)
    parser.add_argument(
        "--no-vix", action="store_true",
        help="Skip VIX history fetch (faster, no stratification)",
    )

    args = parser.parse_args()

    settings = _load_settings()

    # Resolve symbols
    if args.symbol:
        symbols = [s.strip() for s in args.symbol.split(",")]
    elif args.all_watchlist:
        symbols = _get_default_symbols(settings)
    else:
        symbols = _get_default_symbols(settings)

    if not symbols:
        print("No symbols. Use -y or --all-watchlist.")
        sys.exit(1)

    # Load data
    from src.us_playbook.backtest.data_loader import USDataLoader

    # Extra days for lookback (VP + RVOL + structure signal)
    vp_cfg = settings.get("volume_profile", {})
    rvol_cfg = settings.get("rvol", {})
    vp_lookback = vp_cfg.get("lookback_trading_days", 5)
    rvol_lookback = rvol_cfg.get("lookback_days", 10)
    extra = max(vp_lookback, rvol_lookback, max(STRUCTURE_WINDOWS))
    # Convert calendar days to approximate trading days
    trading_days_target = int(args.days * 5 / 7)
    load_days = trading_days_target + extra

    print(f"Symbols: {', '.join(symbols)}")
    print(f"Period: {args.days} calendar days (loading {load_days} trading days for lookback)")
    print()

    try:
        with USDataLoader(futu_host=args.futu_host, futu_port=args.futu_port) as loader:
            bars = loader.load_all(symbols, days=load_days)
    except ConnectionError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not bars:
        print("Failed to load data.")
        sys.exit(1)

    # VIX history
    vix_history = None
    if not args.no_vix:
        print("Fetching VIX history...")
        vix_history = fetch_vix_history(args.days)
        if vix_history.empty:
            print("  Warning: VIX history unavailable, skipping stratification")
        else:
            print(f"  VIX data: {len(vix_history)} days")
    print()

    # Run evaluation
    evaluator = DailyBiasEvaluator(config=settings)
    print("Running daily bias evaluation...")
    report = evaluator.evaluate(bars, vix_history=vix_history)

    # Output
    if args.output == "json":
        print(format_json(report))
    else:
        print(format_report(report, verbose=args.verbose))


if __name__ == "__main__":
    main()
