"""数据采集器 — Futu API + yfinance 宏观数据 + 校验。

订阅策略（节省 v1 额度）：
- QQQ/SPY/IWM：订阅实时报价 + 5分钟K线，extended_time=True
- Mag7 七只：订阅实时报价，extended_time=True
- VIX/UUP/TNX：不订阅，用 get_market_snapshot 快照轮询（每30秒）

所有 Futu SDK 调用通过 asyncio.to_thread() 包装（SDK 是同步的）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from models import (
    CalendarEvent,
    CollectionResult,
    DataStatus,
    IndexData,
    MacroData,
    Mag7Data,
)

logger = logging.getLogger("collector")

_ET = ZoneInfo("America/New_York")


class DataCollector:
    """纯数据采集管道 — Futu snapshot + yfinance 宏观。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config
        self._futu_host = config.get("futu", {}).get("host", "127.0.0.1")
        self._futu_port = config.get("futu", {}).get("port", 11111)

        # Futu 连接（延迟初始化）
        self._quote_ctx = None

        # 符号列表
        sym_cfg = config.get("symbols", {})
        self._index_symbols: list[str] = sym_cfg.get("indexes", [])
        self._mag7_symbols: list[str] = sym_cfg.get("mag7", [])
        macro_cfg = sym_cfg.get("macro", {})
        self._vix_ticker: str = macro_cfg.get("vix", "^VIX")
        self._tnx_ticker: str = macro_cfg.get("tnx", "^TNX")
        self._uup_ticker: str = macro_cfg.get("uup", "UUP")

        # 采集参数
        col_cfg = config.get("collector", {})
        self._macro_cache_ttl = col_cfg.get("macro_cache_ttl", 120)
        self._vix_ma_period = col_cfg.get("vix_ma_period", 10)
        self._vix_ma_cache_ttl = col_cfg.get("vix_ma_cache_ttl", 86400)
        self._mag7_avg_days = col_cfg.get("mag7_volume_avg_days", 5)
        self._weekly_lookback = col_cfg.get("weekly_lookback_days", 5)

        vp_cfg = col_cfg.get("volume_profile", {})
        self._vp_lookback = vp_cfg.get("lookback_trading_days", 5)
        self._vp_value_area = vp_cfg.get("value_area_pct", 0.70)
        self._gamma_enabled = col_cfg.get("gamma_wall", {}).get("enabled", True)

        # 校验阈值
        val_cfg = config.get("validation", {})
        self._vix_min = val_cfg.get("vix_min_valid", 1.0)
        self._tnx_min = val_cfg.get("tnx_min_valid", 0.01)
        self._uup_min = val_cfg.get("uup_min_valid", 1.0)
        self._stale_hour = val_cfg.get("stale_data_cutoff_hour_et", 4)

        # 存档路径
        self._archive_dir = Path(
            config.get("archive_dir", "data/raw")
        )
        self._archive_dir.mkdir(parents=True, exist_ok=True)

        # TTL 缓存: (timestamp, value)
        self._vix_cache: tuple[float, dict | None] = (0.0, None)
        self._tnx_cache: tuple[float, dict | None] = (0.0, None)
        self._uup_cache: tuple[float, dict | None] = (0.0, None)
        self._vix_ma_cache: tuple[float, float] = (0.0, 0.0)

        # 每日缓存（start() 时加载）
        self._daily_levels: dict[str, dict] = {}  # symbol → {pdh, pdl, poc, vah, val}
        self._weekly_hl: dict[str, tuple[float, float]] = {}  # symbol → (high, low)
        self._gamma_walls: dict[str, tuple[float, float]] = {}  # symbol → (call, put)
        self._mag7_avg_vol: dict[str, float] = {}  # symbol → 5日均量

        # 订阅追踪
        self._subscribed_count = 0

        # 实时数据回调存储（供 monitor 使用）
        self._latest_quotes: dict[str, dict] = {}
        self._latest_klines: dict[str, dict] = {}

    # ── 连接与订阅 ──

    async def start(self) -> None:
        """初始化：连接 Futu → 订阅 → 加载每日缓存。"""
        await self._connect_futu()
        await self._setup_subscriptions()
        await self._load_daily_caches()
        logger.info("DataCollector started")

    async def _connect_futu(self) -> None:
        """连接 FutuOpenD。"""
        from futu import OpenQuoteContext

        def _connect():
            ctx = OpenQuoteContext(host=self._futu_host, port=self._futu_port)
            return ctx

        self._quote_ctx = await asyncio.to_thread(_connect)
        logger.info("Futu connected: %s:%d", self._futu_host, self._futu_port)

    async def _setup_subscriptions(self) -> int:
        """订阅 Futu 实时数据，返回使用的配额数。

        QQQ/SPY/IWM: QUOTE + K_5M = 6 slots
        Mag7 (7 stocks): QUOTE = 7 slots
        总计 = 13 slots
        """
        from futu import SubType

        ctx = self._quote_ctx
        if ctx is None:
            return 0

        count = 0

        # 指数：QUOTE + K_5M
        if self._index_symbols:
            def _sub_idx():
                ret1, _ = ctx.subscribe(
                    self._index_symbols,
                    [SubType.QUOTE],
                    is_first_push=False,
                    subscribe_push=True,
                    extended_time=True,
                )
                ret2, _ = ctx.subscribe(
                    self._index_symbols,
                    [SubType.K_5M],
                    is_first_push=False,
                    subscribe_push=True,
                    extended_time=True,
                )
                return ret1, ret2

            r1, r2 = await asyncio.to_thread(_sub_idx)
            idx_count = len(self._index_symbols) * 2
            count += idx_count
            logger.info(
                "Subscribed indexes (%d symbols × 2): ret=%s/%s",
                len(self._index_symbols), r1, r2,
            )

        # Mag7：仅 QUOTE
        if self._mag7_symbols:
            def _sub_mag7():
                ret, _ = ctx.subscribe(
                    self._mag7_symbols,
                    [SubType.QUOTE],
                    is_first_push=False,
                    subscribe_push=True,
                    extended_time=True,
                )
                return ret

            r = await asyncio.to_thread(_sub_mag7)
            mag7_count = len(self._mag7_symbols)
            count += mag7_count
            logger.info(
                "Subscribed Mag7 (%d symbols × 1): ret=%s",
                len(self._mag7_symbols), r,
            )

        self._subscribed_count = count
        logger.info("Total subscription slots: %d", count)
        return count

    def get_subscription_count(self) -> int:
        """返回当前订阅配额使用数。"""
        return self._subscribed_count

    async def _load_daily_caches(self) -> None:
        """加载每日缓存：VP、PDH/PDL、Weekly HL、Gamma Wall、Mag7 均量。"""
        # 指数的 VP + PDH/PDL + Weekly HL + Gamma Wall
        for futu_sym in self._index_symbols:
            short = _short_symbol(futu_sym)
            try:
                bars = await self._get_history_bars(futu_sym, days=self._vp_lookback * 2 + 2)
                if bars is not None and not bars.empty:
                    pdh, pdl, pdc = _extract_prev_day_levels(bars)
                    poc, vah, val = _calculate_volume_profile(
                        bars, self._vp_value_area,
                    )
                    self._daily_levels[short] = {
                        "pdh": pdh, "pdl": pdl, "pdc": pdc,
                        "poc": poc, "vah": vah, "val": val,
                    }
                    logger.info(
                        "%s levels: PDH=%.2f PDL=%.2f POC=%.2f VAH=%.2f VAL=%.2f",
                        short, pdh, pdl, poc, vah, val,
                    )
            except Exception:
                logger.warning("Failed to load daily bars for %s", short, exc_info=True)

            # Weekly HL
            try:
                wk_bars = await self._get_history_bars(futu_sym, days=self._weekly_lookback + 5, interval="1d")
                if wk_bars is not None and not wk_bars.empty:
                    recent = wk_bars.tail(self._weekly_lookback)
                    self._weekly_hl[short] = (
                        float(recent["High"].max()),
                        float(recent["Low"].min()),
                    )
            except Exception:
                logger.debug("Weekly HL load failed for %s", short)

            # Gamma wall
            try:
                cw, pw = await self._get_gamma_wall(futu_sym)
                self._gamma_walls[short] = (cw, pw)
            except Exception:
                logger.debug("Gamma wall load failed for %s", short)

        # Mag7 均量
        for futu_sym in self._mag7_symbols:
            short = _short_symbol(futu_sym)
            try:
                bars = await self._get_history_bars(futu_sym, days=self._mag7_avg_days + 5, interval="1d")
                if bars is not None and not bars.empty:
                    recent = bars.tail(self._mag7_avg_days)
                    avg_vol = float(recent["Volume"].mean())
                    self._mag7_avg_vol[short] = avg_vol
                    logger.debug("%s avg 5d volume: %.0f", short, avg_vol)
            except Exception:
                logger.debug("Mag7 avg volume failed for %s", short)

    # ── 数据可用性检查 ──

    async def check_availability(self) -> list[DataStatus]:
        """启动时逐一验证每个数据源。"""
        statuses: list[DataStatus] = []

        # Futu 连接
        futu_ok = self._quote_ctx is not None
        statuses.append(DataStatus(
            source="futu",
            ok=futu_ok,
            detail="" if futu_ok else "FutuOpenD 连接失败",
        ))

        # yfinance VIX
        vix = await self._fetch_yf_ticker(self._vix_ticker)
        vix_ok = vix is not None and vix.get("last_price", 0) >= self._vix_min
        statuses.append(DataStatus(
            source="yfinance_vix",
            ok=vix_ok,
            detail="" if vix_ok else f"VIX 数据异常: {vix}",
        ))

        # yfinance TNX
        tnx = await self._fetch_yf_ticker(self._tnx_ticker)
        tnx_ok = tnx is not None and tnx.get("last_price", 0) >= self._tnx_min
        statuses.append(DataStatus(
            source="yfinance_tnx",
            ok=tnx_ok,
            detail="" if tnx_ok else f"TNX 数据异常: {tnx}",
        ))

        # yfinance UUP
        uup = await self._fetch_yf_ticker(self._uup_ticker)
        uup_ok = uup is not None and uup.get("last_price", 0) >= self._uup_min
        statuses.append(DataStatus(
            source="yfinance_uup",
            ok=uup_ok,
            detail="" if uup_ok else f"UUP 数据异常: {uup}",
        ))

        # Futu 指数 snapshot
        if futu_ok:
            try:
                snaps = await self._get_snapshots(self._index_symbols)
                idx_ok = len(snaps) > 0
            except Exception:
                idx_ok = False
            statuses.append(DataStatus(
                source="futu_indexes",
                ok=idx_ok,
                detail="" if idx_ok else "指数快照获取失败",
            ))

        return statuses

    # ── 完整采集 ──

    async def collect_full(self, calendar_events: list[CalendarEvent] | None = None) -> CollectionResult:
        """完整采集一次所有数据源 → CollectionResult。"""
        now = time.time()
        et_now = datetime.now(_ET)

        # 并行采集宏观 + 指数 + Mag7
        macro_task = self._collect_macro()
        indices_task = self._collect_indices()
        mag7_task = self._collect_mag7()
        macro, indices, mag7 = await asyncio.gather(
            macro_task, indices_task, mag7_task,
        )

        # 数据验证
        statuses = self._validate(macro, indices, mag7)

        result = CollectionResult(
            timestamp=now,
            date_str=et_now.strftime("%Y-%m-%d"),
            time_str=et_now.strftime("%H:%M") + " ET",
            macro=macro,
            indices=indices,
            mag7=mag7,
            calendar=calendar_events or [],
            statuses=statuses,
            is_premarket=self._is_premarket(),
        )

        # 归档原始数据
        self.archive_raw(result)

        return result

    # ── 宏观采集 ──

    async def _collect_macro(self) -> MacroData:
        """采集 VIX + TNX + UUP 宏观数据。"""
        vix_data, tnx_data, uup_data = await asyncio.gather(
            self._fetch_yf_cached(self._vix_ticker, "_vix_cache"),
            self._fetch_yf_cached(self._tnx_ticker, "_tnx_cache"),
            self._fetch_yf_cached(self._uup_ticker, "_uup_cache"),
        )

        # VIX
        vix_current = vix_data.get("last_price") if vix_data else None
        vix_prev = vix_data.get("prev_close") if vix_data else None

        # VIX MA10
        vix_ma10 = await self._fetch_vix_ma()

        # VIX 偏离
        vix_deviation = None
        if vix_current is not None and vix_ma10 is not None and vix_ma10 > 0:
            vix_deviation = (vix_current - vix_ma10) / vix_ma10

        # TNX
        tnx_current = tnx_data.get("last_price") if tnx_data else None
        tnx_prev = tnx_data.get("prev_close") if tnx_data else None
        tnx_bps = None
        if tnx_current is not None and tnx_prev is not None and tnx_prev > 0:
            tnx_bps = (tnx_current - tnx_prev) * 100

        # UUP
        uup_current = uup_data.get("last_price") if uup_data else None
        uup_prev = uup_data.get("prev_close") if uup_data else None
        uup_pct = None
        if uup_current is not None and uup_prev is not None and uup_prev > 0:
            uup_pct = (uup_current - uup_prev) / uup_prev * 100

        return MacroData(
            vix_current=vix_current,
            vix_prev_close=vix_prev,
            vix_ma10=vix_ma10,
            vix_deviation_pct=vix_deviation,
            tnx_current=tnx_current,
            tnx_prev_close=tnx_prev,
            tnx_change_bps=tnx_bps,
            uup_current=uup_current,
            uup_prev_close=uup_prev,
            uup_change_pct=uup_pct,
            timestamp=time.time(),
        )

    async def _fetch_yf_cached(self, ticker_symbol: str, cache_attr: str) -> dict | None:
        """yfinance 获取 + TTL 缓存。"""
        now = time.time()
        cache: tuple[float, dict | None] = getattr(self, cache_attr)
        if cache[1] is not None and now - cache[0] < self._macro_cache_ttl:
            return cache[1]

        data = await self._fetch_yf_ticker(ticker_symbol)
        if data is not None and data.get("last_price", 0) > 0:
            setattr(self, cache_attr, (now, data))
            return data

        # 获取失败，返回旧缓存
        return cache[1]

    async def _fetch_yf_ticker(self, ticker_symbol: str) -> dict | None:
        """yfinance fast_info 获取。"""
        try:
            import yfinance as yf

            def _fetch():
                ticker = yf.Ticker(ticker_symbol)
                info = ticker.fast_info
                return {
                    "last_price": float(getattr(info, "last_price", 0) or 0),
                    "prev_close": float(getattr(info, "previous_close", 0) or 0),
                }

            return await asyncio.to_thread(_fetch)
        except Exception:
            logger.debug("yfinance %s fetch failed", ticker_symbol, exc_info=True)
            return None

    async def _fetch_vix_ma(self) -> float | None:
        """获取 VIX N 日均值（每日缓存）。"""
        now = time.time()
        if self._vix_ma_cache[1] > 0 and now - self._vix_ma_cache[0] < self._vix_ma_cache_ttl:
            return self._vix_ma_cache[1]

        try:
            import yfinance as yf

            def _fetch():
                ticker = yf.Ticker("^VIX")
                hist = ticker.history(period=f"{self._vix_ma_period + 5}d")
                if hist.empty:
                    return None
                closes = hist["Close"].tail(self._vix_ma_period).tolist()
                return sum(closes) / len(closes) if closes else None

            result = await asyncio.to_thread(_fetch)
            if result is not None:
                self._vix_ma_cache = (now, result)
            return result
        except Exception:
            logger.debug("VIX MA fetch failed")
            return self._vix_ma_cache[1] if self._vix_ma_cache[1] > 0 else None

    # ── 指数采集 ──

    async def _collect_indices(self) -> list[IndexData]:
        """采集 QQQ/SPY/IWM 指数数据。"""
        if not self._index_symbols:
            return []

        try:
            snapshots = await self._get_snapshots(self._index_symbols)
        except Exception:
            logger.warning("Index snapshot fetch failed", exc_info=True)
            return [
                IndexData(symbol=_short_symbol(s), status="不可用")
                for s in self._index_symbols
            ]

        premarket = self._is_premarket()
        result = []

        for futu_sym in self._index_symbols:
            short = _short_symbol(futu_sym)
            snap = snapshots.get(futu_sym, {})

            if not snap:
                result.append(IndexData(symbol=short, status="不可用"))
                continue

            prev_close = snap.get("prev_close_price", 0.0)
            pre_price = snap.get("pre_price", 0.0)

            # 盘前优先 pre_price
            if premarket and pre_price > 0:
                price = pre_price
            else:
                price = snap.get("last_price", 0.0)

            # 自算涨跌幅
            change_pct = None
            if price > 0 and prev_close > 0:
                change_pct = round((price - prev_close) / prev_close * 100, 2)

            # 盘前无成交检测
            status = "ok"
            if price > 0 and prev_close > 0 and price == prev_close:
                status = "盘前无成交"

            # 每日缓存的点位
            cached = self._daily_levels.get(short, {})
            wk = self._weekly_hl.get(short, (None, None))
            gw = self._gamma_walls.get(short, (None, None))

            result.append(IndexData(
                symbol=short,
                price=round(price, 2) if price > 0 else None,
                prev_close=round(prev_close, 2) if prev_close > 0 else None,
                change_pct=change_pct,
                volume=snap.get("volume") or None,
                gap_pct=change_pct,  # 盘前 gap ≈ change
                pdc=round(prev_close, 2) if prev_close > 0 else cached.get("pdc"),
                pdh=cached.get("pdh"),
                pdl=cached.get("pdl"),
                pmh=round(snap.get("pre_high_price", 0), 2) or None,
                pml=round(snap.get("pre_low_price", 0), 2) or None,
                weekly_high=wk[0],
                weekly_low=wk[1],
                poc=cached.get("poc"),
                vah=cached.get("vah"),
                val=cached.get("val"),
                gamma_call_wall=gw[0],
                gamma_put_wall=gw[1],
                status=status,
            ))

        return result

    # ── Mag7 采集 ──

    async def _collect_mag7(self) -> list[Mag7Data]:
        """采集 Mag7 七只股票数据。"""
        if not self._mag7_symbols:
            return []

        try:
            snapshots = await self._get_snapshots(self._mag7_symbols)
        except Exception:
            logger.warning("Mag7 snapshot fetch failed", exc_info=True)
            return [
                Mag7Data(symbol=_short_symbol(s), status="不可用")
                for s in self._mag7_symbols
            ]

        premarket = self._is_premarket()
        result = []

        for futu_sym in self._mag7_symbols:
            short = _short_symbol(futu_sym)
            snap = snapshots.get(futu_sym, {})

            if not snap:
                result.append(Mag7Data(symbol=short, status="不可用"))
                continue

            prev_close = snap.get("prev_close_price", 0.0)
            pre_price = snap.get("pre_price", 0.0)

            if premarket and pre_price > 0:
                price = pre_price
            else:
                price = snap.get("last_price", 0.0)

            # 自算涨跌幅
            change_pct = None
            if price > 0 and prev_close > 0:
                change_pct = round((price - prev_close) / prev_close * 100, 2)

            # 自算量比
            volume = snap.get("volume") or 0
            avg_vol = self._mag7_avg_vol.get(short, 0)
            volume_ratio = None
            if volume > 0 and avg_vol > 0:
                volume_ratio = round(volume / avg_vol, 2)

            status = "ok"
            if price > 0 and prev_close > 0 and price == prev_close:
                status = "盘前无成交"

            result.append(Mag7Data(
                symbol=short,
                change_pct=change_pct,
                volume=volume if volume > 0 else None,
                volume_ratio=volume_ratio,
                status=status,
            ))

        return result

    # ── 数据验证 ──

    def _validate(
        self,
        macro: MacroData,
        indices: list[IndexData],
        mag7: list[Mag7Data],
    ) -> list[DataStatus]:
        """数据校验 → 异常字段置 None，返回状态列表。"""
        statuses: list[DataStatus] = []

        # VIX
        if macro.vix_current is not None and macro.vix_current < self._vix_min:
            macro.vix_current = None
            macro.vix_prev_close = None
            macro.vix_deviation_pct = None
            statuses.append(DataStatus("vix", False, f"VIX < {self._vix_min}"))

        # TNX
        if macro.tnx_current is not None and macro.tnx_current < self._tnx_min:
            macro.tnx_current = None
            macro.tnx_prev_close = None
            macro.tnx_change_bps = None
            statuses.append(DataStatus("tnx", False, f"TNX < {self._tnx_min}"))

        # UUP
        if macro.uup_current is not None and macro.uup_current < self._uup_min:
            macro.uup_current = None
            macro.uup_prev_close = None
            macro.uup_change_pct = None
            statuses.append(DataStatus("uup", False, f"UUP < {self._uup_min}"))

        # 时间戳检查
        et_now = datetime.now(_ET)
        stale_cutoff = et_now.replace(
            hour=self._stale_hour, minute=0, second=0, microsecond=0,
        ).timestamp()
        if macro.timestamp > 0 and macro.timestamp < stale_cutoff:
            statuses.append(DataStatus("macro_timestamp", False, "宏观数据时间戳过时"))

        # 指数状态
        for idx in indices:
            if idx.status != "ok":
                statuses.append(DataStatus(
                    f"index_{idx.symbol}", False, idx.status,
                ))

        # Mag7 状态
        for m in mag7:
            if m.status != "ok":
                statuses.append(DataStatus(
                    f"mag7_{m.symbol}", False, m.status,
                ))

        return statuses

    # ── 原始数据归档 ──

    def archive_raw(self, result: CollectionResult) -> None:
        """追加到 data/raw/{date}.json。"""
        filepath = self._archive_dir / f"{result.date_str}.json"

        record = {
            "timestamp": result.timestamp,
            "time_et": result.time_str,
            "macro": _dataclass_to_dict(result.macro),
            "indices": [_dataclass_to_dict(i) for i in result.indices],
            "mag7": [_dataclass_to_dict(m) for m in result.mag7],
            "statuses": [_dataclass_to_dict(s) for s in result.statuses],
        }

        # 读取已有记录，追加
        existing: list = []
        if filepath.exists():
            try:
                with open(filepath, encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, Exception):
                existing = []

        existing.append(record)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        logger.debug("Raw data archived: %s", filepath)

    # ── Futu API 封装 ──

    async def _get_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """批量获取 Futu snapshot。"""
        ctx = self._quote_ctx
        if ctx is None:
            return {}

        def _fetch():
            ret, data = ctx.get_market_snapshot(symbols)
            if ret != 0 or data is None or data.empty:
                return {}
            result = {}
            for _, row in data.iterrows():
                code = row.get("code", "")
                result[code] = {
                    "last_price": float(row.get("last_price", 0) or 0),
                    "prev_close_price": float(row.get("prev_close_price", 0) or 0),
                    "open_price": float(row.get("open_price", 0) or 0),
                    "volume": int(row.get("volume", 0) or 0),
                    "pre_price": float(row.get("pre_price", 0) or 0),
                    "pre_high_price": float(row.get("pre_high_price", 0) or 0),
                    "pre_low_price": float(row.get("pre_low_price", 0) or 0),
                }
            return result

        return await asyncio.to_thread(_fetch)

    async def _get_history_bars(
        self, symbol: str, days: int = 10, interval: str = "1m",
    ) -> pd.DataFrame | None:
        """获取历史 K 线。"""
        from futu import KLType

        kl_map = {
            "1m": KLType.K_1M,
            "5m": KLType.K_5M,
            "15m": KLType.K_15M,
            "1d": KLType.K_DAY,
        }
        kl_type = kl_map.get(interval)
        if kl_type is None:
            raise ValueError(f"Unknown interval: {interval}")

        ctx = self._quote_ctx
        if ctx is None:
            return None

        if interval == "1d":
            max_count = days + 10
        else:
            max_count = (days + 3) * 400

        def _fetch():
            ret, data, _ = ctx.request_history_kline(
                symbol,
                ktype=kl_type,
                max_count=max_count,
            )
            if ret != 0 or data is None or data.empty:
                return None
            return data

        raw = await asyncio.to_thread(_fetch)
        if raw is None:
            return None

        return _normalize_kline(raw)

    async def _get_gamma_wall(self, futu_symbol: str) -> tuple[float | None, float | None]:
        """获取 gamma wall（OI），失败回退整数关口。"""
        if not self._gamma_enabled:
            short = _short_symbol(futu_symbol)
            cached = self._daily_levels.get(short, {})
            price = cached.get("poc", 0)
            if price > 0:
                return _integer_round_levels(price)
            return None, None

        ctx = self._quote_ctx
        if ctx is None:
            return None, None

        try:
            def _fetch():
                ret, data = ctx.get_option_chain(futu_symbol)
                if ret != 0 or data is None or data.empty:
                    return None
                return data

            chain = await asyncio.to_thread(_fetch)
            if chain is not None:
                cw, pw = _gamma_from_chain(chain)
                if cw > 0 and pw > 0:
                    return cw, pw
        except Exception:
            logger.debug("Gamma wall fetch failed for %s", futu_symbol)

        # 回退整数关口
        short = _short_symbol(futu_symbol)
        cached = self._daily_levels.get(short, {})
        price = cached.get("poc", 0)
        if price > 0:
            return _integer_round_levels(price)
        return None, None

    # ── 工具方法 ──

    @staticmethod
    def _is_premarket() -> bool:
        """判断当前是否处于美股盘前时段（ET 04:00-09:30）。"""
        et_now = datetime.now(_ET).time()
        return dt_time(4, 0) <= et_now < dt_time(9, 30)

    def get_latest_quotes(self) -> dict[str, dict]:
        """返回最新报价快照（供 monitor 使用）。"""
        return dict(self._latest_quotes)

    def close(self) -> None:
        """关闭 Futu 连接。"""
        if self._quote_ctx is not None:
            try:
                self._quote_ctx.close()
            except Exception:
                pass
            self._quote_ctx = None
        logger.info("DataCollector closed")


# ── 纯函数工具 ──


def _short_symbol(futu_symbol: str) -> str:
    """US.QQQ → QQQ"""
    return futu_symbol.split(".")[-1] if "." in futu_symbol else futu_symbol


def _normalize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """标准化 Futu K 线 DataFrame。"""
    rename_map = {
        "time_key": "Datetime",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    if "Datetime" in df.columns:
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        df = df.set_index("Datetime")
        if df.index.tz is None:
            df.index = df.index.tz_localize(_ET)
    return df


def _extract_prev_day_levels(bars: pd.DataFrame) -> tuple[float, float, float]:
    """从 K 线提取昨日 PDH/PDL/PDC。"""
    if bars.empty:
        return 0.0, 0.0, 0.0

    dates = sorted(bars.index.date)
    unique_dates = sorted(set(dates))
    if len(unique_dates) < 2:
        return 0.0, 0.0, 0.0

    prev_date = unique_dates[-2]
    prev_bars = bars[bars.index.date == prev_date]
    if prev_bars.empty:
        return 0.0, 0.0, 0.0

    return (
        float(prev_bars["High"].max()),
        float(prev_bars["Low"].min()),
        float(prev_bars["Close"].iloc[-1]),
    )


def _calculate_volume_profile(
    bars: pd.DataFrame,
    value_area_pct: float = 0.70,
) -> tuple[float, float, float]:
    """简化版 Volume Profile → (POC, VAH, VAL)。"""
    if bars.empty or "Volume" not in bars.columns:
        return 0.0, 0.0, 0.0

    # 排除当天数据
    dates = sorted(set(bars.index.date))
    if len(dates) < 2:
        return 0.0, 0.0, 0.0

    last_date = dates[-1]
    hist = bars[bars.index.date != last_date]
    if hist.empty:
        return 0.0, 0.0, 0.0

    # 价格分 bin
    prices = (hist["High"] + hist["Low"] + hist["Close"]) / 3
    volumes = hist["Volume"]

    price_min, price_max = float(prices.min()), float(prices.max())
    if price_max <= price_min:
        return 0.0, 0.0, 0.0

    num_bins = 50
    bin_size = (price_max - price_min) / num_bins
    if bin_size <= 0:
        return 0.0, 0.0, 0.0

    vol_by_bin: dict[int, float] = {}
    for p, v in zip(prices, volumes):
        b = int((float(p) - price_min) / bin_size)
        b = min(b, num_bins - 1)
        vol_by_bin[b] = vol_by_bin.get(b, 0) + float(v)

    if not vol_by_bin:
        return 0.0, 0.0, 0.0

    # POC = 最大成交量 bin 的中点
    poc_bin = max(vol_by_bin, key=vol_by_bin.get)  # type: ignore[arg-type]
    poc = price_min + (poc_bin + 0.5) * bin_size

    # Value Area: 从 POC 向两侧扩展到包含 70% 总成交量
    total_vol = sum(vol_by_bin.values())
    target_vol = total_vol * value_area_pct

    included = {poc_bin}
    included_vol = vol_by_bin.get(poc_bin, 0)
    lo, hi = poc_bin, poc_bin

    while included_vol < target_vol:
        up_vol = vol_by_bin.get(hi + 1, 0) if hi + 1 < num_bins else 0
        dn_vol = vol_by_bin.get(lo - 1, 0) if lo - 1 >= 0 else 0

        if up_vol == 0 and dn_vol == 0:
            break

        if up_vol >= dn_vol:
            hi += 1
            included.add(hi)
            included_vol += up_vol
        else:
            lo -= 1
            included.add(lo)
            included_vol += dn_vol

    vah = price_min + (hi + 1) * bin_size
    val = price_min + lo * bin_size

    return round(poc, 2), round(vah, 2), round(val, 2)


def _integer_round_levels(price: float) -> tuple[float, float]:
    """整数关口近似（gamma wall 不可用时）。"""
    if price <= 0:
        return 0.0, 0.0
    step = 10 if price > 100 else 5
    upper = ((price // step) + 1) * step
    lower = (price // step) * step
    return float(upper), float(lower)


def _gamma_from_chain(chain: pd.DataFrame) -> tuple[float, float]:
    """从期权链数据提取 gamma wall（最大 OI 的 call/put 行权价）。"""
    if chain.empty:
        return 0.0, 0.0

    # 尝试按 option_type 分组
    call_col = "option_type"
    oi_col = "open_interest"
    strike_col = "strike_price"

    # 检查列是否存在
    for col in [call_col, oi_col, strike_col]:
        if col not in chain.columns:
            return 0.0, 0.0

    calls = chain[chain[call_col] == "CALL"]
    puts = chain[chain[call_col] == "PUT"]

    call_wall = 0.0
    if not calls.empty and oi_col in calls.columns:
        max_idx = calls[oi_col].idxmax()
        call_wall = float(calls.loc[max_idx, strike_col])

    put_wall = 0.0
    if not puts.empty and oi_col in puts.columns:
        max_idx = puts[oi_col].idxmax()
        put_wall = float(puts.loc[max_idx, strike_col])

    return call_wall, put_wall


def _dataclass_to_dict(obj: Any) -> dict:
    """dataclass → dict（用于 JSON 序列化）。"""
    from dataclasses import asdict
    return asdict(obj)


def lookup_risk(vix_deviation_pct: float | None, config: dict) -> dict:
    """VIX 偏离查表 → 风控参数。

    纯查找表：VIX MA10 偏离 > threshold → high_volatility 参数，否则 normal。
    """
    risk_cfg = config.get("risk", {})
    threshold = risk_cfg.get("vix_high_deviation_threshold", 0.20)

    if vix_deviation_pct is not None and abs(vix_deviation_pct) >= threshold:
        params = risk_cfg.get("high_volatility", {})
        regime = "high_volatility"
    else:
        params = risk_cfg.get("normal", {})
        regime = "normal"

    return {
        "vix_deviation_pct": vix_deviation_pct,
        "regime": regime,
        "max_single_risk_pct": params.get("max_single_risk_pct", 1.0),
        "max_daily_loss_pct": params.get("max_daily_loss_pct", 2.0),
        "circuit_breaker_count": params.get("circuit_breaker_count", 3),
        "cooldown_minutes": params.get("cooldown_minutes", 30),
    }
