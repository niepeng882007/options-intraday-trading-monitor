"""IndexDataCollector — 纯轮询封装 FutuCollector snapshot + yfinance 宏观数据。

M1+M2 阶段不使用 Futu 订阅，全部 snapshot + yfinance 轮询。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pandas as pd

from src.common.levels import extract_previous_day_hl
from src.common.volume_profile import calculate_volume_profile
from src.index_trader import (
    IndexQuote,
    LevelMap,
    MacroSnapshot,
    Mag7Stock,
    VIXRegime,
)
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.collector.futu import FutuCollector

logger = setup_logger("index_collector")


class IndexDataCollector:
    """数据采集层 — 封装 Futu snapshot + yfinance，带 TTL 缓存。"""

    def __init__(self, futu_collector: FutuCollector, config: dict) -> None:
        self._futu = futu_collector
        self._cfg = config

        # TTL 缓存: (timestamp, value)
        self._vix_cache: tuple[float, dict | None] = (0.0, None)
        self._vix_history_cache: tuple[float, list | None] = (0.0, None)
        self._tnx_cache: tuple[float, dict | None] = (0.0, None)
        self._uup_cache: tuple[float, dict | None] = (0.0, None)

        # 每日缓存（盘前加载一次）
        self._daily_bars: dict[str, pd.DataFrame] = {}
        self._daily_levels: dict[str, dict] = {}

    # ── 初始化 ──

    async def start(self) -> None:
        """加载每日缓存（VP 点位、PDH/PDL）。"""
        index_symbols = [i["symbol"] for i in self._cfg.get("indices", [])]
        vp_cfg = self._cfg.get("volume_profile", {})
        lookback = vp_cfg.get("lookback_trading_days", 5)
        # 日历天数：交易日 * 2 + 2 覆盖周末+假日
        fetch_days = lookback * 2 + 2

        for symbol in index_symbols:
            try:
                bars = await self._futu.get_history_bars(symbol, days=fetch_days)
                if bars is not None and not bars.empty:
                    self._daily_bars[symbol] = bars
                    pdh, pdl = extract_previous_day_hl(bars)
                    # VP
                    history = bars[bars.index.date != bars.index[-1].date()] if not bars.empty else bars
                    vp = calculate_volume_profile(
                        history,
                        value_area_pct=vp_cfg.get("value_area_pct", 0.70),
                        recency_decay=vp_cfg.get("recency_decay", 0.15),
                    )
                    self._daily_levels[symbol] = {
                        "pdh": pdh, "pdl": pdl,
                        "poc": vp.poc, "vah": vp.vah, "val": vp.val,
                    }
                    logger.info(
                        "%s daily levels loaded: PDH=%.2f PDL=%.2f POC=%.2f",
                        symbol, pdh, pdl, vp.poc,
                    )
            except Exception:
                logger.warning("Failed to load daily bars for %s", symbol, exc_info=True)

    # ── 宏观数据 ──

    async def fetch_macro(self) -> MacroSnapshot:
        """获取 VIX + TNX + UUP 宏观数据。"""
        vix_data, tnx_data, uup_data = await asyncio.gather(
            self._fetch_yf_ticker("^VIX", self._vix_cache, self._cfg.get("macro", {}).get("vix", {}).get("cache_ttl", 120)),
            self._fetch_yf_ticker("^TNX", self._tnx_cache, self._cfg.get("macro", {}).get("tnx", {}).get("cache_ttl", 120)),
            self._fetch_yf_ticker("UUP", self._uup_cache, self._cfg.get("macro", {}).get("uup", {}).get("cache_ttl", 120)),
        )

        # VIX
        vix_current = vix_data.get("last_price", 0.0) if vix_data else 0.0
        vix_prev = vix_data.get("prev_close", 0.0) if vix_data else 0.0

        # VIX MA10
        vix_ma10 = await self._fetch_vix_ma10()

        # VIX regime
        macro_cfg = self._cfg.get("macro", {}).get("vix", {})
        vix_deviation = (vix_current - vix_ma10) / vix_ma10 if vix_ma10 > 0 else 0.0
        if vix_deviation >= macro_cfg.get("extreme_deviation", 0.40):
            vix_regime = VIXRegime.EXTREME
        elif vix_deviation >= macro_cfg.get("high_deviation", 0.20):
            vix_regime = VIXRegime.HIGH
        elif vix_deviation <= macro_cfg.get("low_deviation", -0.05):
            vix_regime = VIXRegime.LOW
        else:
            vix_regime = VIXRegime.NORMAL

        # TNX
        tnx_current = tnx_data.get("last_price", 0.0) if tnx_data else 0.0
        tnx_prev = tnx_data.get("prev_close", 0.0) if tnx_data else 0.0
        tnx_change_bps = (tnx_current - tnx_prev) * 100 if tnx_prev > 0 else 0.0

        # UUP
        uup_current = uup_data.get("last_price", 0.0) if uup_data else 0.0
        uup_prev = uup_data.get("prev_close", 0.0) if uup_data else 0.0
        uup_change_pct = ((uup_current - uup_prev) / uup_prev * 100) if uup_prev > 0 else 0.0

        uup_cfg = self._cfg.get("macro", {}).get("uup", {})
        uup_threshold = uup_cfg.get("strong_threshold_pct", 0.5)
        if uup_change_pct >= uup_threshold:
            dxy_direction = "strong"
        elif uup_change_pct <= -uup_threshold:
            dxy_direction = "weak"
        else:
            dxy_direction = "flat"

        return MacroSnapshot(
            vix_current=vix_current,
            vix_prev_close=vix_prev,
            vix_ma10=vix_ma10,
            vix_deviation_pct=vix_deviation,
            vix_regime=vix_regime,
            tnx_current=tnx_current,
            tnx_prev_close=tnx_prev,
            tnx_change_bps=tnx_change_bps,
            uup_current=uup_current,
            uup_prev_close=uup_prev,
            uup_change_pct=uup_change_pct,
            dxy_direction=dxy_direction,
            timestamp=time.time(),
        )

    async def _fetch_yf_ticker(
        self,
        ticker_symbol: str,
        cache: tuple[float, dict | None],
        ttl: float,
    ) -> dict | None:
        """yfinance fast_info 获取，带 TTL 缓存。"""
        now = time.time()
        if cache[1] and now - cache[0] < ttl:
            return cache[1]

        try:
            import yfinance as yf
            ticker = yf.Ticker(ticker_symbol)
            info = ticker.fast_info
            data = {
                "last_price": float(getattr(info, "last_price", 0) or 0),
                "prev_close": float(getattr(info, "previous_close", 0) or 0),
            }
            if data["last_price"] <= 0:
                return cache[1]

            # 更新缓存 — 通过属性名映射到实例缓存
            new_cache = (now, data)
            if ticker_symbol == "^VIX":
                self._vix_cache = new_cache
            elif ticker_symbol == "^TNX":
                self._tnx_cache = new_cache
            elif ticker_symbol == "UUP":
                self._uup_cache = new_cache
            return data
        except Exception:
            logger.debug("yfinance %s fetch failed, using cached", ticker_symbol)
            return cache[1]

    async def _fetch_vix_ma10(self) -> float:
        """获取 VIX 10 日均值（每日缓存）。"""
        now = time.time()
        if self._vix_history_cache[1] and now - self._vix_history_cache[0] < 86400:
            closes = self._vix_history_cache[1]
            if closes:
                return sum(closes) / len(closes)
            return 0.0

        try:
            import yfinance as yf
            ma_period = self._cfg.get("macro", {}).get("vix", {}).get("ma_period", 10)
            ticker = yf.Ticker("^VIX")
            hist = ticker.history(period=f"{ma_period + 5}d")
            if hist.empty:
                return 0.0
            closes = hist["Close"].tail(ma_period).tolist()
            self._vix_history_cache = (now, closes)
            return sum(closes) / len(closes) if closes else 0.0
        except Exception:
            logger.debug("VIX history fetch failed")
            if self._vix_history_cache[1]:
                closes = self._vix_history_cache[1]
                return sum(closes) / len(closes)
            return 0.0

    # ── 指数报价 ──

    async def fetch_indices(self) -> list[IndexQuote]:
        """批量获取 QQQ/SPY/IWM 报价快照。"""
        index_symbols = [i["symbol"] for i in self._cfg.get("indices", [])]
        if not index_symbols:
            return []

        try:
            snapshots = await self._futu.get_snapshots(index_symbols)
        except Exception:
            logger.warning("Index snapshot fetch failed", exc_info=True)
            return []

        result = []
        for cfg_item in self._cfg.get("indices", []):
            sym = cfg_item["symbol"]
            snap = snapshots.get(sym, {})
            if not snap:
                continue
            price = snap.get("last_price", 0.0)
            prev_close = snap.get("prev_close_price", 0.0)
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            open_price = snap.get("open_price", 0.0)
            gap_pct = ((open_price - prev_close) / prev_close * 100) if prev_close > 0 and open_price > 0 else 0.0
            result.append(IndexQuote(
                symbol=sym,
                price=price,
                prev_close=prev_close,
                change_pct=change_pct,
                volume=snap.get("volume", 0),
                premarket_high=snap.get("pre_high_price", 0.0) if "pre_high_price" in snap else 0.0,
                premarket_low=snap.get("pre_low_price", 0.0) if "pre_low_price" in snap else 0.0,
                gap_pct=gap_pct,
            ))
        return result

    # ── Mag7 报价 ──

    async def fetch_mag7(self) -> list[Mag7Stock]:
        """批量获取 Mag7 报价快照。"""
        symbols = self._cfg.get("mag7", {}).get("symbols", [])
        if not symbols:
            return []

        try:
            snapshots = await self._futu.get_snapshots(symbols)
        except Exception:
            logger.warning("Mag7 snapshot fetch failed", exc_info=True)
            return []

        result = []
        for sym in symbols:
            snap = snapshots.get(sym, {})
            if not snap:
                continue
            price = snap.get("last_price", 0.0)
            prev_close = snap.get("prev_close_price", 0.0)
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            result.append(Mag7Stock(
                code=sym,
                price=price,
                change_pct=change_pct,
                volume=snap.get("volume", 0),
                volume_ratio=snap.get("volume_ratio", 0.0),
                is_anomaly=snap.get("volume_ratio", 0.0) >= self._cfg.get("mag7", {}).get("volume_anomaly_ratio", 2.0),
            ))
        return result

    # ── 点位 ──

    async def fetch_levels(self, symbol: str) -> LevelMap:
        """获取单个标的完整点位集。"""
        # 实时报价
        try:
            snap = await self._futu.get_snapshot(symbol)
        except Exception:
            snap = {}

        price = snap.get("last_price", 0.0)
        prev_close = snap.get("prev_close_price", 0.0)
        pmh = snap.get("pre_high_price", 0.0) if "pre_high_price" in snap else 0.0
        pml = snap.get("pre_low_price", 0.0) if "pre_low_price" in snap else 0.0

        # 每日缓存的 VP + PDH/PDL
        cached = self._daily_levels.get(symbol, {})

        # 周线高低
        weekly_high, weekly_low = await self._fetch_weekly_hl(symbol)

        # Gamma wall
        gamma_call, gamma_put = await self._fetch_gamma_wall(symbol, price)

        # VWAP（from today's bars if available）
        vwap = 0.0
        if symbol in self._daily_bars:
            from src.common.indicators import calculate_vwap
            bars = self._daily_bars[symbol]
            today = bars.index[-1].date() if not bars.empty else None
            if today:
                today_bars = bars[bars.index.date == today]
                if not today_bars.empty:
                    vwap = calculate_vwap(today_bars)

        return LevelMap(
            symbol=symbol,
            current_price=price,
            pdc=prev_close,
            pdh=cached.get("pdh", 0.0),
            pdl=cached.get("pdl", 0.0),
            pmh=pmh,
            pml=pml,
            weekly_high=weekly_high,
            weekly_low=weekly_low,
            poc=cached.get("poc", 0.0),
            vah=cached.get("vah", 0.0),
            val=cached.get("val", 0.0),
            gamma_call_wall=gamma_call,
            gamma_put_wall=gamma_put,
            vwap=vwap,
        )

    async def _fetch_weekly_hl(self, symbol: str) -> tuple[float, float]:
        """获取周线高低点。"""
        try:
            bars = await self._futu.get_history_bars(symbol, days=10, interval="1d")
            if bars is not None and not bars.empty:
                recent_5 = bars.tail(5)
                return float(recent_5["High"].max()), float(recent_5["Low"].min())
        except Exception:
            logger.debug("Weekly HL fetch failed for %s", symbol)
        return 0.0, 0.0

    async def _fetch_gamma_wall(self, symbol: str, current_price: float) -> tuple[float, float]:
        """获取 gamma wall（OI 数据），失败回退整数关口。"""
        if not self._cfg.get("gamma_wall", {}).get("enabled", True):
            return self._integer_round_levels(current_price)

        try:
            from src.common.gamma_wall import calculate_gamma_wall
            chain = await self._futu.get_option_chain(symbol)
            if chain is not None and not chain.empty:
                result = calculate_gamma_wall(chain, current_price)
                if result.call_wall_strike > 0 and result.put_wall_strike > 0:
                    return result.call_wall_strike, result.put_wall_strike
        except Exception:
            logger.debug("Gamma wall fetch failed for %s, using integer levels", symbol)

        return self._integer_round_levels(current_price)

    @staticmethod
    def _integer_round_levels(price: float) -> tuple[float, float]:
        """整数关口近似（gamma wall 不可用时的回退）。"""
        if price <= 0:
            return 0.0, 0.0
        step = 10 if price > 100 else 5
        upper = ((price // step) + 1) * step
        lower = (price // step) * step
        return float(upper), float(lower)
