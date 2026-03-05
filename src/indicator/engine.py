from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import AverageTrueRange

from src.utils.logger import setup_logger

logger = setup_logger("indicator_engine")

MIN_BARS_RSI = 15
MIN_BARS_MACD = 35
MIN_BARS_EMA = 22
MIN_BARS_ATR = 15
MIN_BARS_VWAP = 2


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
    vwap: float | None = None
    atr: float | None = None
    close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    previous_close: float | None = None
    range_percentile: float | None = None
    vwap_distance_pct: float | None = None
    volume_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class SymbolData:
    bars_1m: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    bars_5m: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    last_indicators_1m: IndicatorResult | None = None
    last_indicators_5m: IndicatorResult | None = None


class IndicatorEngine:
    """Calculates technical indicators from OHLCV data.

    Maintains per-symbol sliding-window DataFrames for 1m and 5m timeframes.
    The 5m bars are aggregated from 1m bars.
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

        sym.bars_5m = self._resample_to_5m(sym.bars_1m)

    @staticmethod
    def _resample_to_5m(bars_1m: pd.DataFrame) -> pd.DataFrame:
        if bars_1m.empty:
            return pd.DataFrame()
        resampled = bars_1m.resample("5min").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        )
        return resampled.dropna(subset=["Open"])

    # ── Indicator calculation ──

    def calculate(self, symbol: str, timeframe: str = "1m") -> IndicatorResult | None:
        sym = self._ensure_symbol(symbol)
        bars = sym.bars_1m if timeframe == "1m" else sym.bars_5m

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
        self._calc_vwap(bars, result)
        self._calc_price_metrics(bars, result)

        if timeframe == "1m":
            sym.last_indicators_1m = result
        else:
            sym.last_indicators_5m = result

        logger.debug(
            "Indicators %s [%s]: RSI=%.1f MACD_H=%.4f EMA9=%.2f EMA21=%.2f VWAP=%s ATR=%s",
            symbol,
            timeframe,
            result.rsi or 0,
            result.macd_histogram or 0,
            result.ema_9 or 0,
            result.ema_21 or 0,
            f"{result.vwap:.2f}" if result.vwap else "N/A",
            f"{result.atr:.4f}" if result.atr else "N/A",
        )
        return result

    def calculate_all(self, symbol: str) -> dict[str, IndicatorResult | None]:
        return {
            "1m": self.calculate(symbol, "1m"),
            "5m": self.calculate(symbol, "5m"),
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
            return {"1m": None, "5m": None}

        last_idx = sym.bars_1m.index[-1]
        sym.bars_1m.at[last_idx, "Close"] = price
        sym.bars_1m.at[last_idx, "High"] = max(sym.bars_1m.at[last_idx, "High"], price)
        sym.bars_1m.at[last_idx, "Low"] = min(sym.bars_1m.at[last_idx, "Low"], price)

        sym.bars_5m = self._resample_to_5m(sym.bars_1m)
        return self.calculate_all(symbol)

    def get_last(self, symbol: str, timeframe: str) -> IndicatorResult | None:
        sym = self._data.get(symbol)
        if sym is None:
            return None
        return sym.last_indicators_1m if timeframe == "1m" else sym.last_indicators_5m

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
        if len(bars) < MIN_BARS_EMA:
            return
        try:
            ema9 = EMAIndicator(close=bars["Close"], window=9).ema_indicator()
            ema21 = EMAIndicator(close=bars["Close"], window=21).ema_indicator()
            if pd.notna(ema9.iloc[-1]):
                result.ema_9 = float(ema9.iloc[-1])
            if pd.notna(ema21.iloc[-1]):
                result.ema_21 = float(ema21.iloc[-1])
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
    def _calc_vwap(bars: pd.DataFrame, result: IndicatorResult) -> None:
        if len(bars) < MIN_BARS_VWAP:
            return
        if "Volume" not in bars.columns:
            return
        try:
            typical_price = (bars["High"] + bars["Low"] + bars["Close"]) / 3
            cumulative_tp_vol = (typical_price * bars["Volume"]).cumsum()
            cumulative_vol = bars["Volume"].cumsum()
            vwap_series = cumulative_tp_vol / cumulative_vol
            last_val = vwap_series.iloc[-1]
            if pd.notna(last_val):
                result.vwap = float(last_val)
        except Exception:
            pass

    @staticmethod
    def _calc_price_metrics(bars: pd.DataFrame, result: IndicatorResult) -> None:
        if bars.empty:
            return
        try:
            result.close = float(bars["Close"].iloc[-1])
            result.day_high = float(bars["High"].max())
            result.day_low = float(bars["Low"].min())

            day_range = result.day_high - result.day_low
            if day_range > 1e-9:
                result.range_percentile = (
                    (result.close - result.day_low) / day_range * 100
                )

            if result.vwap and result.vwap > 1e-9 and result.close:
                result.vwap_distance_pct = (
                    (result.close - result.vwap) / result.vwap * 100
                )

            if len(bars) >= 2 and "Volume" in bars.columns:
                lookback = min(20, len(bars) - 1)
                avg_vol = bars["Volume"].iloc[-lookback - 1 : -1].mean()
                current_vol = float(bars["Volume"].iloc[-1])
                if avg_vol > 0:
                    result.volume_ratio = current_vol / avg_vol
        except Exception:
            pass
