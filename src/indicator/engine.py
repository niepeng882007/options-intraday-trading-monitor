from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator, ADXIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from src.utils.logger import setup_logger

logger = setup_logger("indicator_engine")

MIN_BARS_RSI = 15
MIN_BARS_MACD = 35
MIN_BARS_EMA_SHORT = 22
MIN_BARS_EMA_50 = 51
MIN_BARS_EMA_200 = 201
MIN_BARS_ATR = 15
MIN_BARS_VWAP = 2
MIN_BARS_BOLLINGER = 21
MIN_BARS_ADX = 28
MIN_BARS_STOCHASTIC = 15
MIN_BARS_WARMUP = 260  # 51 × 5 + buffer, enough for EMA50 on 5m


@dataclass
class IndicatorResult:
    symbol: str
    timeframe: str
    timestamp: float
    rsi: float | None = None
    macd_line: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    ema_9: float | None = None
    ema_21: float | None = None
    ema_50: float | None = None
    ema_200: float | None = None
    vwap: float | None = None
    atr: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_width_pct: float | None = None
    close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    previous_close: float | None = None
    day_open: float | None = None
    day_change_pct: float | None = None
    range_percentile: float | None = None
    vwap_distance_pct: float | None = None
    abs_vwap_distance_pct: float | None = None
    volume_ratio: float | None = None
    candle_body_pct: float | None = None
    candle_range_pct: float | None = None
    prev_bar_high: float | None = None
    adx: float | None = None
    volume_spike: float | None = None          # current_vol / avg(last 3 bars vol)
    bb_width_percentile: float | None = None   # intraday BBW percentile (0-100)
    bb_middle: float | None = None             # BB middle band (SMA20)
    bb_pct_b: float | None = None              # %B = (close - lower) / (upper - lower)
    bb_width_expansion: float | None = None    # current BBW / recent 10-period BBW avg
    stoch_k: float | None = None               # Stochastic %K
    stoch_d: float | None = None               # Stochastic %D
    upper_shadow_pct: float | None = None      # (high - max(O,C)) / close * 100
    lower_shadow_pct: float | None = None      # (min(O,C) - low) / close * 100
    prev_bar_close: float | None = None        # previous bar close
    prev_bar_low: float | None = None          # previous bar low

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class SymbolData:
    bars_1m: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    bars_5m: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    bars_15m: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    last_indicators_1m: IndicatorResult | None = None
    last_indicators_5m: IndicatorResult | None = None
    last_indicators_15m: IndicatorResult | None = None
    bbw_history: list[float] = field(default_factory=list)  # intraday BBW values


class IndicatorEngine:
    """Calculates technical indicators from OHLCV data.

    Maintains per-symbol sliding-window DataFrames for 1m, 5m, and 15m timeframes.
    The 5m and 15m bars are aggregated from 1m bars.
    """

    def __init__(self) -> None:
        self._data: dict[str, SymbolData] = {}

    def _ensure_symbol(self, symbol: str) -> SymbolData:
        if symbol not in self._data:
            self._data[symbol] = SymbolData()
        return self._data[symbol]

    # ── Bar ingestion ──

    def update_bars(self, symbol: str, bars_1m: pd.DataFrame) -> None:
        sym = self._ensure_symbol(symbol)

        if bars_1m.empty:
            return

        if sym.bars_1m.empty:
            sym.bars_1m = bars_1m.copy()
        else:
            sym.bars_1m = pd.concat([sym.bars_1m, bars_1m])
            sym.bars_1m = sym.bars_1m[~sym.bars_1m.index.duplicated(keep="last")]
            sym.bars_1m.sort_index(inplace=True)

        sym.bars_5m = self._resample(sym.bars_1m, "5min")
        sym.bars_15m = self._resample(sym.bars_1m, "15min")

    @staticmethod
    def _resample(bars_1m: pd.DataFrame, freq: str) -> pd.DataFrame:
        if bars_1m.empty:
            return pd.DataFrame()
        resampled = bars_1m.resample(freq).agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        )
        return resampled.dropna(subset=["Open"])

    # ── Indicator calculation ──

    def calculate(self, symbol: str, timeframe: str = "1m") -> IndicatorResult | None:
        sym = self._ensure_symbol(symbol)
        if timeframe == "15m":
            bars = sym.bars_15m
        elif timeframe == "5m":
            bars = sym.bars_5m
        else:
            bars = sym.bars_1m

        if bars.empty or len(bars) < 2:
            return None

        now_ts = bars.index[-1].timestamp() if hasattr(bars.index[-1], "timestamp") else 0.0

        result = IndicatorResult(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=now_ts,
        )

        self._calc_rsi(bars, result)
        self._calc_macd(bars, result)
        self._calc_ema(bars, result)
        self._calc_atr(bars, result)
        self._calc_adx(bars, result)
        self._calc_stochastic(bars, result)
        self._calc_vwap(bars, result)
        self._calc_bollinger(bars, result, sym=sym)
        self._calc_candle_metrics(bars, result)
        self._calc_price_metrics(bars, result)

        if timeframe == "15m":
            sym.last_indicators_15m = result
        elif timeframe == "5m":
            sym.last_indicators_5m = result
        else:
            sym.last_indicators_1m = result

        logger.debug(
            "Indicators %s [%s]: RSI=%.1f MACD_H=%.4f EMA9=%.2f EMA50=%s EMA200=%s VWAP=%s BBW=%s",
            symbol,
            timeframe,
            result.rsi or 0,
            result.macd_histogram or 0,
            result.ema_9 or 0,
            f"{result.ema_50:.2f}" if result.ema_50 else "N/A",
            f"{result.ema_200:.2f}" if result.ema_200 else "N/A",
            f"{result.vwap:.2f}" if result.vwap else "N/A",
            f"{result.bb_width_pct:.4f}" if result.bb_width_pct else "N/A",
        )
        return result

    def calculate_all(self, symbol: str) -> dict[str, IndicatorResult | None]:
        return {
            "1m": self.calculate(symbol, "1m"),
            "5m": self.calculate(symbol, "5m"),
            "15m": self.calculate(symbol, "15m"),
        }

    def update_live_price(
        self, symbol: str, price: float, timestamp: float
    ) -> dict[str, IndicatorResult | None]:
        """Update the current 1m bar's close with a live quote price and recalculate.

        This allows entry evaluation on every quote poll (~10s) instead of
        waiting for the next full history poll (~60s), significantly reducing
        signal latency.
        """
        sym = self._ensure_symbol(symbol)
        if sym.bars_1m.empty:
            return {"1m": None, "5m": None, "15m": None}

        last_idx = sym.bars_1m.index[-1]
        sym.bars_1m.at[last_idx, "Close"] = price
        sym.bars_1m.at[last_idx, "High"] = max(sym.bars_1m.at[last_idx, "High"], price)
        sym.bars_1m.at[last_idx, "Low"] = min(sym.bars_1m.at[last_idx, "Low"], price)

        sym.bars_5m = self._resample(sym.bars_1m, "5min")
        sym.bars_15m = self._resample(sym.bars_1m, "15min")
        return self.calculate_all(symbol)

    def needs_warmup(self, symbol: str, min_bars: int = MIN_BARS_WARMUP) -> bool:
        sym = self._data.get(symbol)
        return sym is None or len(sym.bars_1m) < min_bars

    def get_last(self, symbol: str, timeframe: str) -> IndicatorResult | None:
        sym = self._data.get(symbol)
        if sym is None:
            return None
        if timeframe == "15m":
            return sym.last_indicators_15m
        if timeframe == "5m":
            return sym.last_indicators_5m
        return sym.last_indicators_1m

    # ── Individual indicator helpers (using `ta` library) ──

    @staticmethod
    def _calc_rsi(bars: pd.DataFrame, result: IndicatorResult, period: int = 14) -> None:
        if len(bars) < MIN_BARS_RSI:
            return
        try:
            indicator = RSIIndicator(close=bars["Close"], window=period)
            rsi_series = indicator.rsi()
            last_val = rsi_series.iloc[-1]
            if pd.notna(last_val):
                result.rsi = float(last_val)
        except Exception:
            pass

    @staticmethod
    def _calc_macd(
        bars: pd.DataFrame,
        result: IndicatorResult,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> None:
        if len(bars) < MIN_BARS_MACD:
            return
        try:
            indicator = MACD(
                close=bars["Close"],
                window_slow=slow,
                window_fast=fast,
                window_sign=signal,
            )
            macd_line = indicator.macd().iloc[-1]
            macd_signal_val = indicator.macd_signal().iloc[-1]
            macd_diff = indicator.macd_diff().iloc[-1]

            if pd.notna(macd_line):
                result.macd_line = float(macd_line)
            if pd.notna(macd_signal_val):
                result.macd_signal = float(macd_signal_val)
            if pd.notna(macd_diff):
                result.macd_histogram = float(macd_diff)
        except Exception:
            pass

    @staticmethod
    def _calc_ema(bars: pd.DataFrame, result: IndicatorResult) -> None:
        bar_count = len(bars)
        try:
            if bar_count >= MIN_BARS_EMA_SHORT:
                ema9 = EMAIndicator(close=bars["Close"], window=9).ema_indicator()
                ema21 = EMAIndicator(close=bars["Close"], window=21).ema_indicator()
                if pd.notna(ema9.iloc[-1]):
                    result.ema_9 = float(ema9.iloc[-1])
                if pd.notna(ema21.iloc[-1]):
                    result.ema_21 = float(ema21.iloc[-1])

            if bar_count >= MIN_BARS_EMA_50:
                ema50 = EMAIndicator(close=bars["Close"], window=50).ema_indicator()
                if pd.notna(ema50.iloc[-1]):
                    result.ema_50 = float(ema50.iloc[-1])

            if bar_count >= MIN_BARS_EMA_200:
                ema200 = EMAIndicator(close=bars["Close"], window=200).ema_indicator()
                if pd.notna(ema200.iloc[-1]):
                    result.ema_200 = float(ema200.iloc[-1])
        except Exception:
            pass

    @staticmethod
    def _calc_atr(bars: pd.DataFrame, result: IndicatorResult, period: int = 14) -> None:
        if len(bars) < MIN_BARS_ATR:
            return
        try:
            indicator = AverageTrueRange(
                high=bars["High"], low=bars["Low"], close=bars["Close"], window=period
            )
            atr_val = indicator.average_true_range().iloc[-1]
            if pd.notna(atr_val):
                result.atr = float(atr_val)
        except Exception:
            pass

    @staticmethod
    def _calc_adx(bars: pd.DataFrame, result: IndicatorResult, period: int = 14) -> None:
        if len(bars) < MIN_BARS_ADX:
            return
        try:
            indicator = ADXIndicator(
                high=bars["High"], low=bars["Low"], close=bars["Close"], window=period
            )
            adx_val = indicator.adx().iloc[-1]
            if pd.notna(adx_val):
                result.adx = float(adx_val)
        except Exception:
            pass

    @staticmethod
    def _calc_stochastic(
        bars: pd.DataFrame,
        result: IndicatorResult,
        window: int = 9,
        smooth_window: int = 3,
    ) -> None:
        if len(bars) < MIN_BARS_STOCHASTIC:
            return
        try:
            indicator = StochasticOscillator(
                high=bars["High"], low=bars["Low"], close=bars["Close"],
                window=window, smooth_window=smooth_window,
            )
            k_val = indicator.stoch().iloc[-1]
            d_val = indicator.stoch_signal().iloc[-1]
            if pd.notna(k_val):
                result.stoch_k = float(k_val)
            if pd.notna(d_val):
                result.stoch_d = float(d_val)
        except Exception:
            pass

    @staticmethod
    def _calc_vwap(bars: pd.DataFrame, result: IndicatorResult) -> None:
        if len(bars) < MIN_BARS_VWAP:
            return
        if "Volume" not in bars.columns:
            return
        try:
            today = bars.index[-1].date()
            today_bars = bars[bars.index.date == today]
            if len(today_bars) < MIN_BARS_VWAP:
                return

            typical_price = (today_bars["High"] + today_bars["Low"] + today_bars["Close"]) / 3
            cumulative_tp_vol = (typical_price * today_bars["Volume"]).cumsum()
            cumulative_vol = today_bars["Volume"].cumsum()
            vwap_series = cumulative_tp_vol / cumulative_vol
            last_val = vwap_series.iloc[-1]
            if pd.notna(last_val):
                result.vwap = float(last_val)
        except Exception:
            pass

    @staticmethod
    def _calc_bollinger(
        bars: pd.DataFrame,
        result: IndicatorResult,
        period: int = 20,
        std_dev: int = 2,
        sym: SymbolData | None = None,
    ) -> None:
        if len(bars) < MIN_BARS_BOLLINGER:
            return
        try:
            bb = BollingerBands(close=bars["Close"], window=period, window_dev=std_dev)
            upper = bb.bollinger_hband().iloc[-1]
            lower = bb.bollinger_lband().iloc[-1]
            middle = bb.bollinger_mavg().iloc[-1]

            if pd.notna(upper) and pd.notna(lower):
                result.bb_upper = float(upper)
                result.bb_lower = float(lower)
                if pd.notna(middle) and float(middle) > 0:
                    result.bb_middle = float(middle)
                    bbw = (float(upper) - float(lower)) / float(middle) * 100
                    result.bb_width_pct = bbw

                    # %B = (close - lower) / (upper - lower)
                    close_val = float(bars["Close"].iloc[-1])
                    band_width = float(upper) - float(lower)
                    if band_width > 1e-9:
                        result.bb_pct_b = (close_val - float(lower)) / band_width

                    # BB width expansion rate: current BBW / recent 10-period BBW avg
                    if sym is not None and len(sym.bbw_history) >= 10:
                        recent_avg = sum(sym.bbw_history[-10:]) / 10
                        if recent_avg > 1e-9:
                            result.bb_width_expansion = bbw / recent_avg

                    # Track intraday BBW and compute percentile
                    if sym is not None:
                        sym.bbw_history.append(bbw)
                        # Keep max 500 values (one trading day ~390 1m bars)
                        if len(sym.bbw_history) > 500:
                            sym.bbw_history = sym.bbw_history[-500:]
                        if len(sym.bbw_history) >= 5:
                            count_below = sum(1 for v in sym.bbw_history if v <= bbw)
                            result.bb_width_percentile = (
                                count_below / len(sym.bbw_history) * 100
                            )
        except Exception:
            pass

    @staticmethod
    def _calc_candle_metrics(bars: pd.DataFrame, result: IndicatorResult) -> None:
        """Calculate candle body size and previous bar high for cross-bar confirmation."""
        if bars.empty:
            return
        try:
            last = bars.iloc[-1]
            close_val = float(last["Close"])
            open_val = float(last["Open"])
            high_val = float(last["High"])
            low_val = float(last["Low"])

            result.open = open_val
            result.high = high_val
            result.low = low_val

            if close_val > 0:
                result.candle_body_pct = abs(close_val - open_val) / close_val * 100
                result.candle_range_pct = (high_val - low_val) / close_val * 100
                max_oc = max(close_val, open_val)
                min_oc = min(close_val, open_val)
                result.upper_shadow_pct = (high_val - max_oc) / close_val * 100
                result.lower_shadow_pct = (min_oc - low_val) / close_val * 100

            if len(bars) >= 2:
                prev = bars.iloc[-2]
                result.prev_bar_high = float(prev["High"])
                result.prev_bar_close = float(prev["Close"])
                result.prev_bar_low = float(prev["Low"])
        except Exception:
            pass

    @staticmethod
    def _calc_price_metrics(bars: pd.DataFrame, result: IndicatorResult) -> None:
        if bars.empty:
            return
        try:
            result.close = float(bars["Close"].iloc[-1])

            today = bars.index[-1].date()
            today_bars = bars[bars.index.date == today]
            if today_bars.empty:
                today_bars = bars

            result.day_high = float(today_bars["High"].max())
            result.day_low = float(today_bars["Low"].min())

            day_open = float(today_bars["Open"].iloc[0])
            result.day_open = day_open
            if day_open > 1e-9:
                result.day_change_pct = (result.close - day_open) / day_open * 100

            day_range = result.day_high - result.day_low
            if day_range > 1e-9:
                result.range_percentile = (
                    (result.close - result.day_low) / day_range * 100
                )

            if result.vwap and result.vwap > 1e-9 and result.close:
                result.vwap_distance_pct = (
                    (result.close - result.vwap) / result.vwap * 100
                )
                result.abs_vwap_distance_pct = abs(result.vwap_distance_pct)

            if len(bars) >= 2 and "Volume" in bars.columns:
                lookback = min(20, len(bars) - 1)
                avg_vol = bars["Volume"].iloc[-lookback - 1 : -1].mean()
                current_vol = float(bars["Volume"].iloc[-1])
                if avg_vol > 0:
                    result.volume_ratio = current_vol / avg_vol

                # volume_spike: current vol / avg of last 3 bars
                if len(bars) >= 4:
                    avg_3 = bars["Volume"].iloc[-4:-1].mean()
                    if avg_3 > 0:
                        result.volume_spike = current_vol / avg_3
        except Exception:
            pass
