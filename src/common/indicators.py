"""Shared indicator functions — used by both HK and US modules."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.types import RelativeStrength


def calculate_atr_5min(
    today_bars: pd.DataFrame,
    period: int = 14,
) -> float:
    """Calculate 5-minute ATR from 1-minute bars.

    Resamples 1-min OHLCV to 5-min bars, computes True Range per bar,
    returns average of last *period* True Ranges.
    Returns absolute price value (not percentage).
    """
    if today_bars is None or today_bars.empty:
        return 0.0

    # Resample 1-min → 5-min
    ohlcv = today_bars[["Open", "High", "Low", "Close"]].copy()
    bars_5m = ohlcv.resample("5min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()

    if len(bars_5m) < 2:
        return 0.0

    # True Range = max(H-L, |H-prev_C|, |L-prev_C|)
    prev_close = bars_5m["Close"].shift(1)
    tr = pd.concat([
        bars_5m["High"] - bars_5m["Low"],
        (bars_5m["High"] - prev_close).abs(),
        (bars_5m["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    tr = tr.dropna()
    if len(tr) < 1:
        return 0.0

    tail = tr.iloc[-period:] if len(tr) >= period else tr
    return float(tail.mean())


def calculate_vwap(bars: pd.DataFrame) -> float:
    """Calculate VWAP for today's bars.

    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    Continuous across breaks (lunch/halt) since bars simply don't exist during gaps.
    """
    if bars.empty:
        return 0.0

    typical_price = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    cum_vol = bars["Volume"].cumsum()
    cum_tp_vol = (typical_price * bars["Volume"]).cumsum()

    if cum_vol.iloc[-1] == 0:
        return 0.0

    return float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1])


def calculate_vwap_series(bars: pd.DataFrame) -> pd.Series:
    """Return cumulative VWAP as a time series (same index as *bars*)."""
    if bars.empty:
        return pd.Series(dtype=float)

    typical_price = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    cum_vol = bars["Volume"].cumsum()
    cum_tp_vol = (typical_price * bars["Volume"]).cumsum()

    vwap_s = cum_tp_vol / cum_vol.replace(0, np.nan)
    return vwap_s


def calculate_vwap_slope(bars: pd.DataFrame, lookback: int = 15) -> float:
    """VWAP slope over the last *lookback* bars (%/bar via linear regression).

    Returns 0.0 when insufficient data or VWAP is zero.
    """
    vwap_s = calculate_vwap_series(bars)
    if vwap_s.empty or len(vwap_s) < 2:
        return 0.0

    tail = vwap_s.dropna().iloc[-lookback:]
    if len(tail) < 2:
        return 0.0

    y = tail.values
    x = np.arange(len(y), dtype=float)
    slope = float(np.polyfit(x, y, 1)[0])  # absolute slope per bar

    mean_vwap = float(np.mean(y))
    if mean_vwap == 0:
        return 0.0

    return slope / mean_vwap  # normalise to %/bar


def calculate_vwap_hold_duration(
    bars: pd.DataFrame,
    vwap_series: pd.Series | None = None,
) -> tuple[int, str]:
    """Count consecutive bars on the same side of VWAP from the tail.

    Returns ``(consecutive_bars, side)`` where side is "bullish" / "bearish" / "neutral".
    Reuses *vwap_series* if provided; otherwise computes it.
    """
    if bars is None or bars.empty:
        return 0, "neutral"

    if vwap_series is None:
        vwap_series = calculate_vwap_series(bars)
    if vwap_series.empty or vwap_series.isna().all():
        return 0, "neutral"

    closes = bars["Close"].values
    vwaps = vwap_series.values
    n = len(closes)
    if n == 0:
        return 0, "neutral"

    # Determine side of last bar
    last_close = closes[-1]
    last_vwap = vwaps[-1]
    if np.isnan(last_vwap) or last_close == last_vwap:
        return 0, "neutral"

    side = "bullish" if last_close > last_vwap else "bearish"
    count = 0
    for i in range(n - 1, -1, -1):
        v = vwaps[i]
        if np.isnan(v):
            break
        if side == "bullish" and closes[i] > v:
            count += 1
        elif side == "bearish" and closes[i] < v:
            count += 1
        else:
            break

    return count, side


def compute_relative_strength(
    stock_bars: pd.DataFrame,
    spy_bars: pd.DataFrame,
    *,
    correlation_window: int = 30,
    decouple_threshold: float = 0.40,
) -> RelativeStrength:
    """Compute intraday relative strength of stock vs SPY.

    Parameters
    ----------
    stock_bars, spy_bars : DataFrame with "Close" column (today's 1-min bars).
    correlation_window : rolling window for correlation calc.
    decouple_threshold : |correlation| below this → decoupled.

    Returns
    -------
    RelativeStrength dataclass.
    """
    if stock_bars.empty or spy_bars.empty:
        return RelativeStrength(label="数据不足")

    stock_open = float(stock_bars["Close"].iloc[0])
    spy_open = float(spy_bars["Close"].iloc[0])
    if stock_open <= 0 or spy_open <= 0:
        return RelativeStrength(label="数据不足")

    stock_close = float(stock_bars["Close"].iloc[-1])
    spy_close = float(spy_bars["Close"].iloc[-1])

    stock_ret = (stock_close - stock_open) / stock_open * 100
    spy_ret = (spy_close - spy_open) / spy_open * 100

    # RS ratio: avoid div-by-zero
    if abs(spy_ret) < 0.01:
        rs_ratio = 1.0 + stock_ret / 100  # SPY flat → ratio ≈ 1 + stock move
    else:
        rs_ratio = stock_ret / spy_ret if spy_ret != 0 else 1.0

    # Rolling correlation on returns
    correlation = 0.0
    min_len = min(len(stock_bars), len(spy_bars))
    if min_len >= max(correlation_window, 10):
        stock_returns = stock_bars["Close"].pct_change().dropna().iloc[-min_len + 1:]
        spy_returns = spy_bars["Close"].pct_change().dropna().iloc[-min_len + 1:]
        # Align lengths
        align_len = min(len(stock_returns), len(spy_returns))
        if align_len >= correlation_window:
            s = stock_returns.iloc[-align_len:].values
            b = spy_returns.iloc[-align_len:].values
            # Use last correlation_window bars
            s_win = s[-correlation_window:]
            b_win = b[-correlation_window:]
            if np.std(s_win) > 0 and np.std(b_win) > 0:
                correlation = float(np.corrcoef(s_win, b_win)[0, 1])

    decoupled = abs(correlation) < decouple_threshold

    # Label
    if decoupled:
        label = "脱钩"
    elif stock_ret > spy_ret + 0.1:
        label = "强势"
    elif stock_ret < spy_ret - 0.1:
        label = "弱势"
    else:
        label = "同步"

    return RelativeStrength(
        rs_ratio=round(rs_ratio, 3),
        stock_return_pct=round(stock_ret, 2),
        spy_return_pct=round(spy_ret, 2),
        correlation=round(correlation, 3),
        decoupled=decoupled,
        label=label,
    )
