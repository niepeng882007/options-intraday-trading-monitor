"""HK Predict orchestrator — daily playbook generation with scheduled pushes.

Usage:
    python -m src.hk          # Run as standalone
    # Or integrated into main OptionsMonitor via shared APScheduler
"""

from __future__ import annotations

import asyncio
import html
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.hk import FilterResult, GammaWallResult, Playbook
from src.hk.collector import HKCollector
from src.hk.filter import check_filters
from src.hk.gamma_wall import calculate_gamma_wall, format_gamma_wall_message
from src.hk.indicators import (
    calculate_rvol,
    calculate_vwap,
    get_history_bars,
    get_today_bars,
)
from src.hk.orderbook import (
    analyze_order_book,
    format_alerts_message,
    format_order_book_summary,
)
from src.hk.playbook import format_playbook_message, generate_playbook
from src.hk.regime import classify_regime
from src.hk.volume_profile import calculate_volume_profile
from src.utils.logger import setup_logger

logger = setup_logger("hk_predictor")

HKT = timezone(timedelta(hours=8))
_executor = ThreadPoolExecutor(max_workers=1)
_esc = html.escape

DEFAULT_CONFIG_PATH = "config/hk_settings.yaml"


def _load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class HKPredictor:
    """Orchestrates HK market prediction pipeline.

    Data flow per cycle:
        1. Fetch 1m K-lines (5 days) → Volume Profile (POC/VAH/VAL)
        2. Fetch today's K-lines → VWAP
        3. Compute RVOL (session-aware)
        4. Fetch option chain OI → Gamma Wall (indices only)
        5. Fetch quote → Filter checks
        6. Classify Regime → Generate Playbook
        7. Push to Telegram
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self._cfg = _load_config(config_path)
        futu_cfg = self._cfg.get("futu", {})
        self._collector = HKCollector(
            host=futu_cfg.get("host", "127.0.0.1"),
            port=futu_cfg.get("port", 11111),
        )
        self._send_fn: asyncio.coroutines | None = None  # TG send callback
        self._last_playbooks: dict[str, Playbook] = {}
        self._connected = False

    # ── Lifecycle ──

    async def connect(self) -> None:
        await self._run_sync(self._collector.connect)
        self._connected = True
        logger.info("HKPredictor connected")

    async def close(self) -> None:
        await self._run_sync(self._collector.close)
        self._connected = False

    def set_send_fn(self, fn) -> None:
        """Set async callback for Telegram message sending: fn(text, parse_mode)."""
        self._send_fn = fn

    async def _send_tg(self, text: str) -> None:
        if self._send_fn:
            try:
                await self._send_fn(text, parse_mode="HTML")
            except Exception as e:
                logger.error("TG send failed: %s", e)

    # ── Sync → Async bridge ──

    async def _run_sync(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, fn, *args)

    # ── Core pipeline ──

    async def run_playbook_cycle(
        self,
        symbol: str | None = None,
        update_type: str = "morning",
    ) -> None:
        """Run full playbook pipeline for all configured symbols (or a specific one)."""
        symbols = self._get_symbols(symbol)
        index_symbols = [s["symbol"] for s in self._cfg.get("gamma_wall", {}).get("index_symbols", [])] \
            if isinstance(self._cfg.get("gamma_wall", {}).get("index_symbols", [None])[0], dict) \
            else self._cfg.get("gamma_wall", {}).get("index_symbols", [])

        for sym_info in symbols:
            sym = sym_info if isinstance(sym_info, str) else sym_info.get("symbol", "")
            name = sym_info if isinstance(sym_info, str) else sym_info.get("name", sym)
            try:
                msg = await self._run_single_symbol(sym, update_type, index_symbols)
                self._last_playbooks[sym] = msg  # store Playbook object before formatting
                formatted = format_playbook_message(msg, symbol=name, update_type=update_type)
                await self._send_tg(formatted)
                logger.info("Playbook sent for %s (%s)", sym, update_type)
            except Exception:
                logger.exception("Playbook cycle failed for %s", sym)

    async def _run_single_symbol(
        self,
        symbol: str,
        update_type: str,
        index_symbols: list[str],
    ) -> Playbook:
        """Run pipeline for a single symbol, return Playbook."""
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        regime_cfg = cfg.get("regime", {})
        filter_cfg = cfg.get("filters", {})
        calendar_path = cfg.get("calendar_file", "config/hk_calendar.yaml")

        # 1. Fetch K-lines
        lookback = vp_cfg.get("lookback_days", 5)
        bars = await self._run_sync(self._collector.get_history_kline, symbol, lookback)

        # 2. Volume Profile (on historical bars only)
        hist_bars = get_history_bars(bars)
        vp = calculate_volume_profile(
            hist_bars,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
        )

        # 3. VWAP (today's bars)
        today_bars = get_today_bars(bars)
        vwap = calculate_vwap(today_bars)

        # 4. RVOL
        rvol = calculate_rvol(
            today_bars, hist_bars,
            lookback_days=cfg.get("rvol", {}).get("lookback_days", 10),
        )

        # 5. Quote for current price + filter data
        quote = await self._run_sync(self._collector.get_quote, symbol)
        price = quote["last_price"]
        turnover = quote["turnover"]

        # 6. Gamma Wall (indices only)
        gamma_wall: GammaWallResult | None = None
        if symbol in index_symbols:
            try:
                chain = await self._run_sync(
                    self._collector.get_option_chain_with_oi, symbol,
                )
                if not chain.empty:
                    gamma_wall = calculate_gamma_wall(chain, price)
            except Exception:
                logger.warning("Gamma wall fetch failed for %s", symbol, exc_info=True)

        # 7. Filters
        # Get previous day's high/low for Inside Day check
        prev_high, prev_low = 0.0, 0.0
        if not hist_bars.empty:
            last_day = hist_bars.index[-1].date()
            last_day_bars = hist_bars[hist_bars.index.date == last_day]
            if not last_day_bars.empty:
                prev_high = float(last_day_bars["High"].max())
                prev_low = float(last_day_bars["Low"].min())

        filters = check_filters(
            symbol=symbol,
            turnover=turnover,
            prev_high=prev_high,
            prev_low=prev_low,
            current_high=quote["high_price"],
            current_low=quote["low_price"],
            rvol=rvol,
            calendar_path=calendar_path,
            min_turnover=filter_cfg.get("min_turnover_hkd", 1e8),
        )

        # 8. Regime classification
        regime = classify_regime(
            price=price,
            rvol=rvol,
            vp=vp,
            gamma_wall=gamma_wall,
            breakout_rvol=regime_cfg.get("breakout_rvol", 1.2),
            range_rvol=regime_cfg.get("range_rvol", 0.8),
            iv_spike_ratio=regime_cfg.get("iv_spike_ratio", 1.3),
        )

        # 9. Generate Playbook
        return generate_playbook(
            regime=regime,
            vp=vp,
            vwap=vwap,
            gamma_wall=gamma_wall,
            filters=filters,
            symbol=symbol,
            update_type=update_type,
        )

    # ── Order book monitoring ──

    async def check_orderbook_alerts(self, symbol: str | None = None) -> None:
        """Check order book for anomalies and push alerts."""
        symbols = self._get_stock_symbols(symbol)
        ob_cfg = self._cfg.get("order_book", {})
        ratio = ob_cfg.get("large_order_ratio", 3.0)
        depth = ob_cfg.get("monitor_depth", 10)

        for sym in symbols:
            try:
                book = await self._run_sync(self._collector.get_order_book, sym, depth)
                alerts = analyze_order_book(book, large_order_ratio=ratio)
                if alerts:
                    name = self.get_name(sym)
                    for a in alerts:
                        a.symbol = f"{name}({a.symbol})"
                    msg = format_alerts_message(alerts)
                    await self._send_tg(msg)
            except Exception:
                logger.debug("Order book check failed for %s", sym, exc_info=True)

    # ── Bot command helpers (called from telegram.py handlers) ──

    async def get_status_text(self) -> str:
        """Generate current HK market status text."""
        lines = ["<b>HK Market Status</b>", ""]
        try:
            state = await self._run_sync(self._collector.get_global_state)
            market = state.get("market_hk", "N/A")
            lines.append(f"Market: {market}")
        except Exception as e:
            lines.append(f"Connection: ERROR ({e})")

        # Show latest quotes for watchlist
        symbols = self._get_stock_symbols()[:3]  # top 3
        for sym in symbols:
            try:
                q = await self._run_sync(self._collector.get_quote, sym)
                chg = ((q["last_price"] - q["prev_close"]) / q["prev_close"] * 100) if q["prev_close"] else 0
                arrow = "+" if chg >= 0 else ""
                name = self.get_name(sym)
                lines.append(f"  {name}({sym}): {q['last_price']:,.2f} ({arrow}{chg:.2f}%)")
            except Exception:
                lines.append(f"  {sym}: N/A")

        lines.append(f"\n{datetime.now(HKT).strftime('%H:%M:%S')} HKT")
        return "\n".join(lines)

    async def generate_and_format_playbook(
        self,
        symbol: str | None = None,
        update_type: str = "manual",
    ) -> str:
        """Generate playbook and return formatted text (for /hk_playbook command)."""
        sym = symbol or self._get_primary_index()
        index_symbols = self._cfg.get("gamma_wall", {}).get("index_symbols", [])
        playbook = await self._run_single_symbol(sym, update_type, index_symbols)
        name = self.get_name(sym)
        display = f"{name} ({sym})"
        return format_playbook_message(playbook, symbol=display, update_type=update_type)

    async def get_orderbook_text(self, symbol: str) -> str:
        """Get formatted order book snapshot."""
        depth = self._cfg.get("order_book", {}).get("monitor_depth", 10)
        book = await self._run_sync(self._collector.get_order_book, symbol, depth)
        name = self.get_name(symbol)
        text = format_order_book_summary(book)
        # Replace symbol-only title with name+symbol
        return text.replace(
            f"<b>盘口快照 {symbol}</b>",
            f"<b>盘口快照 {_esc(name)} ({_esc(symbol)})</b>",
        )

    async def get_gamma_wall_text(self, symbol: str) -> str:
        """Get formatted gamma wall info."""
        quote = await self._run_sync(self._collector.get_quote, symbol)
        chain = await self._run_sync(self._collector.get_option_chain_with_oi, symbol)
        gw = calculate_gamma_wall(chain, quote["last_price"])
        name = self.get_name(symbol)
        display = f"{name} ({symbol})"
        return format_gamma_wall_message(gw, display)

    # ── New bot command helpers ──

    async def get_levels_text(self, symbol: str | None = None) -> str:
        """Generate key levels text for /hk_levels command."""
        sym = symbol or self._get_primary_index()
        vp_cfg = self._cfg.get("volume_profile", {})
        lookback = vp_cfg.get("lookback_days", 5)

        bars = await self._run_sync(self._collector.get_history_kline, sym, lookback)
        hist_bars = get_history_bars(bars)
        today_bars = get_today_bars(bars)

        vp = calculate_volume_profile(
            hist_bars,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
        )
        vwap = calculate_vwap(today_bars)
        quote = await self._run_sync(self._collector.get_quote, sym)
        price = quote["last_price"]

        # Price position relative to VA
        if price > vp.vah:
            position = "above VAH \u2191"
        elif price < vp.val:
            position = "below VAL \u2193"
        elif abs(price - vp.poc) / vp.poc < 0.002:
            position = "at POC \u2194"
        else:
            position = "in Value Area"

        # Distances
        vah_dist = (price - vp.vah) / vp.vah * 100 if vp.vah else 0
        val_dist = (price - vp.val) / vp.val * 100 if vp.val else 0
        vwap_dist = (price - vwap) / vwap * 100 if vwap else 0

        # Today's bar stats
        today_high = float(today_bars["High"].max()) if not today_bars.empty else 0
        today_low = float(today_bars["Low"].min()) if not today_bars.empty else 0
        today_range = (today_high - today_low) / today_low * 100 if today_low else 0

        name = self.get_name(sym)
        lines = [
            f"\U0001f4cd <b>\u5173\u952e\u70b9\u4f4d | {_esc(name)} ({_esc(sym)})</b>",
            "\u2501" * 20,
            "",
            f"<b>Volume Profile</b> ({lookback}D lookback)",
            f"  VAH: {vp.vah:,.2f}  ({vah_dist:+.2f}%)",
            f"  POC: {vp.poc:,.2f}",
            f"  VAL: {vp.val:,.2f}  ({val_dist:+.2f}%)",
            "",
            f"<b>VWAP</b>: {vwap:,.2f}  ({vwap_dist:+.2f}%)",
            "",
            f"<b>\u5f53\u524d\u4ef7</b>: {price:,.2f}  \u2014 {position}",
        ]

        if today_high > 0:
            lines.append(f"\u4eca\u65e5\u533a\u95f4: {today_low:,.2f} ~ {today_high:,.2f} ({today_range:.2f}%)")

        lines.append(f"\n\u23f1 {datetime.now(HKT).strftime('%H:%M:%S')} HKT")
        return "\n".join(lines)

    async def get_regime_text(self, symbol: str | None = None) -> str:
        """Generate regime classification text for /hk_regime command."""
        sym = symbol or self._get_primary_index()
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        regime_cfg = cfg.get("regime", {})

        bars = await self._run_sync(self._collector.get_history_kline, sym, vp_cfg.get("lookback_days", 5))
        hist_bars = get_history_bars(bars)
        today_bars = get_today_bars(bars)

        vp = calculate_volume_profile(hist_bars, value_area_pct=vp_cfg.get("value_area_pct", 0.70))
        rvol = calculate_rvol(
            today_bars, hist_bars,
            lookback_days=cfg.get("rvol", {}).get("lookback_days", 10),
        )

        quote = await self._run_sync(self._collector.get_quote, sym)
        price = quote["last_price"]

        # Gamma wall for indices
        gamma_wall = None
        index_syms = cfg.get("gamma_wall", {}).get("index_symbols", [])
        if sym in index_syms:
            try:
                chain = await self._run_sync(self._collector.get_option_chain_with_oi, sym)
                if not chain.empty:
                    from src.hk.gamma_wall import calculate_gamma_wall
                    gamma_wall = calculate_gamma_wall(chain, price)
            except Exception:
                pass

        regime = classify_regime(
            price=price, rvol=rvol, vp=vp,
            gamma_wall=gamma_wall,
            breakout_rvol=regime_cfg.get("breakout_rvol", 1.05),
            range_rvol=regime_cfg.get("range_rvol", 0.95),
            iv_spike_ratio=regime_cfg.get("iv_spike_ratio", 1.3),
        )

        from src.hk.playbook import REGIME_NAME_CN, REGIME_EMOJI as PB_EMOJI
        emoji = PB_EMOJI.get(regime.regime, "\u2753")
        name_cn = REGIME_NAME_CN.get(regime.regime, "\u672a\u77e5")

        # Confidence bar
        filled = int(regime.confidence * 10)
        bar = "\u2588" * filled + "\u2591" * (10 - filled)

        # Trading advice based on regime
        advice_map = {
            "breakout": "\u987a\u52bf\u64cd\u4f5c\uff0c\u4ee5 VWAP \u4e3a\u9632\u5b88\u7ebf",
            "range": "\u9ad8\u629b\u4f4e\u5438\uff0c\u5728 VAH/VAL \u9644\u8fd1\u53cd\u8f6c",
            "whipsaw": "\u964d\u4f4e\u4ed3\u4f4d\uff0c\u7b49\u5f85\u5e26\u91cf\u786e\u8ba4",
            "unclear": "\u89c2\u671b\u4e3a\u4e3b\uff0c\u7b49\u5f85 Regime \u66f4\u65b0",
        }
        advice = advice_map.get(regime.regime.value, "")

        name = self.get_name(sym)
        lines = [
            f"{emoji} <b>Regime \u5206\u7c7b | {_esc(name)} ({_esc(sym)})</b>",
            "\u2501" * 20,
            "",
            f"\u98ce\u683c: {emoji} <b>{name_cn}</b>",
            f"\u4fe1\u5fc3: {bar} {regime.confidence:.0%}",
            f"RVOL: {regime.rvol:.2f}",
            "",
            f"<b>\u5206\u7c7b\u8be6\u60c5</b>",
            f"  {_esc(regime.details)}",
            "",
            f"<b>\u4ef7\u683c\u4f4d\u7f6e</b>",
            f"  \u5f53\u524d: {regime.price:,.2f}",
            f"  VAH: {regime.vah:,.2f}  |  VAL: {regime.val:,.2f}  |  POC: {regime.poc:,.2f}",
            "",
            f"<b>\u4ea4\u6613\u5efa\u8bae</b>: {advice}",
            "",
            f"<b>\u9608\u503c\u53c2\u8003</b>",
            f"  BREAKOUT: RVOL \u2265 {regime_cfg.get('breakout_rvol', 1.05)} + \u4ef7\u683c\u5728 VA \u5916",
            f"  RANGE: RVOL \u2264 {regime_cfg.get('range_rvol', 0.95)} + \u4ef7\u683c\u5728 VA \u5185",
            "",
            f"\u23f1 {datetime.now(HKT).strftime('%H:%M:%S')} HKT",
        ]
        return "\n".join(lines)

    async def get_quote_text(self, symbol: str) -> str:
        """Generate detailed quote text for /hk_quote command."""
        import html as _html
        _esc_fn = _html.escape

        quote = await self._run_sync(self._collector.get_quote, symbol)
        price = quote["last_price"]
        prev_close = quote["prev_close"]
        change = price - prev_close if prev_close else 0
        change_pct = change / prev_close * 100 if prev_close else 0
        arrow = "\U0001f7e2" if change >= 0 else "\U0001f534"
        vol = quote["volume"]
        turnover = quote["turnover"]

        # Format volume
        def _fv(v):
            if v >= 1e9:
                return f"{v / 1e9:.2f}B"
            if v >= 1e6:
                return f"{v / 1e6:.2f}M"
            if v >= 1e3:
                return f"{v / 1e3:.0f}K"
            return str(v)

        name = self.get_name(symbol)
        lines = [
            f"{arrow} <b>{_esc_fn(name)} ({_esc_fn(symbol)})</b>",
            "\u2501" * 20,
            "",
            f"<b>\u6700\u65b0\u4ef7</b>: {price:,.2f}  ({change:+.2f}, {change_pct:+.2f}%)",
            "",
            f"<b>OHLC</b>",
            f"  Open:  {quote['open_price']:,.2f}",
            f"  High:  {quote['high_price']:,.2f}",
            f"  Low:   {quote['low_price']:,.2f}",
            "",
            f"<b>\u4ea4\u6613\u6570\u636e</b>",
            f"  \u6210\u4ea4\u91cf: {_fv(vol)}",
            f"  \u6210\u4ea4\u989d: {_fv(turnover)} HKD",
        ]

        if quote.get("turnover_rate"):
            lines.append(f"  \u6362\u624b\u7387: {quote['turnover_rate']:.2f}%")
        if quote.get("amplitude"):
            lines.append(f"  \u632f\u5e45: {quote['amplitude']:.2f}%")

        lines.append("")
        lines.append(f"<b>\u76d8\u53e3</b>")
        bid = quote.get("bid_price", 0)
        ask = quote.get("ask_price", 0)
        spread = ask - bid if ask and bid else 0
        spread_pct = spread / price * 100 if price else 0
        lines.append(f"  Bid: {bid:,.2f}  |  Ask: {ask:,.2f}")
        lines.append(f"  Spread: {spread:.2f} ({spread_pct:.3f}%)")

        lines.append(f"\n\u23f1 {datetime.now(HKT).strftime('%H:%M:%S')} HKT")
        return "\n".join(lines)

    async def get_filters_text(self, symbol: str | None = None) -> str:
        """Generate filter status text for /hk_filters command."""
        sym = symbol or self._get_primary_index()
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        filter_cfg = cfg.get("filters", {})
        calendar_path = cfg.get("calendar_file", "config/hk_calendar.yaml")

        # Fetch data needed for filters
        bars = await self._run_sync(self._collector.get_history_kline, sym, vp_cfg.get("lookback_days", 5))
        hist_bars = get_history_bars(bars)
        today_bars = get_today_bars(bars)
        quote = await self._run_sync(self._collector.get_quote, sym)

        rvol = calculate_rvol(
            today_bars, hist_bars,
            lookback_days=cfg.get("rvol", {}).get("lookback_days", 10),
        )

        # Previous day high/low
        prev_high, prev_low = 0.0, 0.0
        if not hist_bars.empty:
            last_day = hist_bars.index[-1].date()
            last_day_bars = hist_bars[hist_bars.index.date == last_day]
            if not last_day_bars.empty:
                prev_high = float(last_day_bars["High"].max())
                prev_low = float(last_day_bars["Low"].min())

        filters = check_filters(
            symbol=sym,
            turnover=quote["turnover"],
            prev_high=prev_high,
            prev_low=prev_low,
            current_high=quote["high_price"],
            current_low=quote["low_price"],
            rvol=rvol,
            calendar_path=calendar_path,
            min_turnover=filter_cfg.get("min_turnover_hkd", 1e8),
        )

        # Risk level display
        risk_icons = {
            "normal": "\U0001f7e2 \u6b63\u5e38",
            "elevated": "\U0001f7e1 \u504f\u9ad8",
            "high": "\U0001f534 \u9ad8\u98ce\u9669",
            "blocked": "\u26d4 \u7981\u6b62\u4ea4\u6613",
        }
        risk_display = risk_icons.get(filters.risk_level, filters.risk_level)
        trade_ok = "\u2705 \u53ef\u4ea4\u6613" if filters.tradeable else "\u274c \u4e0d\u5b9c\u4ea4\u6613"

        name = self.get_name(sym)
        lines = [
            f"\U0001f6e1 <b>\u4ea4\u6613\u8fc7\u6ee4 | {_esc(name)} ({_esc(sym)})</b>",
            "\u2501" * 20,
            "",
            f"\u4ea4\u6613\u72b6\u6001: {trade_ok}",
            f"\u98ce\u9669\u7b49\u7ea7: {risk_display}",
            "",
        ]

        # Filter checklist
        lines.append("<b>\u8fc7\u6ee4\u5668\u68c0\u67e5</b>")

        # Turnover check
        min_tv = filter_cfg.get("min_turnover_hkd", 1e8)
        tv = quote["turnover"]
        tv_ok = tv >= min_tv
        tv_icon = "\u2705" if tv_ok else "\u274c"
        lines.append(f"  {tv_icon} \u6210\u4ea4\u989d: {tv / 1e8:.2f}\u4ebf / \u9608\u503c {min_tv / 1e8:.0f}\u4ebf")

        # RVOL
        lines.append(f"  \U0001f4ca RVOL: {rvol:.2f}")

        # Inside Day
        if prev_high > 0 and prev_low > 0:
            is_inside = quote["high_price"] <= prev_high and quote["low_price"] >= prev_low
            id_icon = "\u26a0\ufe0f" if is_inside else "\u2705"
            id_text = "Inside Day" if is_inside else "\u6b63\u5e38"
            lines.append(f"  {id_icon} K\u7ebf\u5f62\u6001: {id_text}")

        lines.append("")

        if filters.warnings:
            lines.append("<b>\u544a\u8b66\u4fe1\u606f</b>")
            for w in filters.warnings:
                lines.append(f"  \u26a0\ufe0f {_esc(w)}")
        else:
            lines.append("\u2705 \u65e0\u544a\u8b66\u4fe1\u606f")

        lines.append(f"\n\u23f1 {datetime.now(HKT).strftime('%H:%M:%S')} HKT")
        return "\n".join(lines)

    async def get_watchlist_text(self) -> str:
        """Generate watchlist overview text for /hk_watchlist command."""
        import html as _html
        _esc_fn = _html.escape

        watchlist = self._cfg.get("watchlist", {})
        all_symbols = []
        for group_name, group_key in [("\U0001f4c8 \u6307\u6570", "indices"), ("\U0001f4b9 \u80a1\u7968", "stocks")]:
            items = watchlist.get(group_key, [])
            for item in items:
                sym = item["symbol"] if isinstance(item, dict) else item
                name = item.get("name", sym) if isinstance(item, dict) else sym
                all_symbols.append((group_name, sym, name))

        lines = [
            "\U0001f4cb <b>HK \u76d1\u63a7\u5217\u8868</b>",
            "\u2501" * 20,
            "",
        ]

        current_group = ""
        for group_name, sym, name in all_symbols:
            if group_name != current_group:
                if current_group:
                    lines.append("")
                lines.append(f"<b>{group_name}</b>")
                current_group = group_name

            try:
                q = await self._run_sync(self._collector.get_quote, sym)
                price = q["last_price"]
                prev = q["prev_close"]
                chg_pct = (price - prev) / prev * 100 if prev else 0
                arrow = "\U0001f7e2" if chg_pct >= 0 else "\U0001f534"
                tv = q["turnover"]
                tv_str = f"{tv / 1e8:.1f}\u4ebf" if tv >= 1e8 else f"{tv / 1e6:.1f}M"
                lines.append(
                    f"  {arrow} <b>{_esc_fn(name)}</b> ({_esc_fn(sym)})"
                )
                lines.append(
                    f"     {price:,.2f} ({chg_pct:+.2f}%)  Vol {tv_str}"
                )
            except Exception:
                lines.append(f"  \u274c <b>{_esc_fn(name)}</b> ({_esc_fn(sym)}) \u2014 \u67e5\u8be2\u5931\u8d25")

        lines.append(f"\n\u23f1 {datetime.now(HKT).strftime('%H:%M:%S')} HKT")
        return "\n".join(lines)

    # ── Helpers ──

    def _get_symbols(self, symbol: str | None = None) -> list[dict | str]:
        """Get symbols to process. If symbol given, return just that one."""
        if symbol:
            return [symbol]
        watchlist = self._cfg.get("watchlist", {})
        symbols = []
        for idx in watchlist.get("indices", []):
            symbols.append(idx)
        for stk in watchlist.get("stocks", []):
            symbols.append(stk)
        return symbols if symbols else ["HK.800000"]

    def _get_stock_symbols(self, symbol: str | None = None) -> list[str]:
        """Get stock symbols (non-index) for order book monitoring."""
        if symbol:
            return [symbol]
        watchlist = self._cfg.get("watchlist", {})
        return [s["symbol"] for s in watchlist.get("stocks", [])]

    def _get_primary_index(self) -> str:
        indices = self._cfg.get("watchlist", {}).get("indices", [])
        if indices:
            return indices[0]["symbol"] if isinstance(indices[0], dict) else indices[0]
        return "HK.800000"

    def get_name(self, symbol: str) -> str:
        """Lookup display name for a symbol from watchlist config."""
        watchlist = self._cfg.get("watchlist", {})
        for group in ("indices", "stocks"):
            for item in watchlist.get(group, []):
                if isinstance(item, dict) and item.get("symbol") == symbol:
                    return item.get("name", symbol)
        return symbol


# ── Standalone entry point ──

async def _main() -> None:
    """Run HKPredictor as standalone service with APScheduler."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    predictor = HKPredictor()

    # Setup Telegram
    cfg = predictor._cfg.get("telegram", {})
    bot_token = cfg.get("bot_token", "")
    chat_id = cfg.get("chat_id", "")

    # Resolve env vars
    import os
    if bot_token.startswith("${"):
        bot_token = os.environ.get(bot_token.strip("${}"), "")
    if str(chat_id).startswith("${"):
        chat_id = os.environ.get(str(chat_id).strip("${}"), "")

    await predictor.connect()

    # Schedule playbook pushes (HKT = UTC+8)
    scheduler = AsyncIOScheduler(timezone="Asia/Hong_Kong")

    # 09:35 Morning playbook
    scheduler.add_job(
        predictor.run_playbook_cycle,
        CronTrigger(hour=9, minute=35, day_of_week="mon-fri", timezone="Asia/Hong_Kong"),
        kwargs={"update_type": "morning"},
        id="hk_morning_playbook",
    )

    # 10:05 Confirmation update
    scheduler.add_job(
        predictor.run_playbook_cycle,
        CronTrigger(hour=10, minute=5, day_of_week="mon-fri", timezone="Asia/Hong_Kong"),
        kwargs={"update_type": "confirm"},
        id="hk_confirm_playbook",
    )

    # 13:05 Afternoon playbook
    scheduler.add_job(
        predictor.run_playbook_cycle,
        CronTrigger(hour=13, minute=5, day_of_week="mon-fri", timezone="Asia/Hong_Kong"),
        kwargs={"update_type": "afternoon"},
        id="hk_afternoon_playbook",
    )

    # Order book monitoring every 60s during trading hours
    scheduler.add_job(
        predictor.check_orderbook_alerts,
        CronTrigger(
            second=0, day_of_week="mon-fri", timezone="Asia/Hong_Kong",
            hour="9-11,13-15", minute="*",
        ),
        id="hk_orderbook_monitor",
    )

    scheduler.start()
    logger.info("HKPredictor scheduler started (3 playbook pushes + order book monitor)")

    if bot_token and chat_id:
        from telegram.ext import Application
        from src.hk.telegram import register_hk_commands

        app = Application.builder().token(bot_token).build()
        register_hk_commands(app, predictor)

        # Set send function using the Application's bot
        async def send_tg(text: str, parse_mode: str = "HTML") -> None:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)

        predictor.set_send_fn(send_tg)

        logger.info("Starting Telegram bot polling...")
        async with app:
            await app.start()
            # Register command menu for Telegram's "/" autocomplete
            from telegram import BotCommand
            await app.bot.set_my_commands([
                BotCommand("hk", "市场状态快照"),
                BotCommand("hk_playbook", "生成 Playbook [symbol]"),
                BotCommand("hk_levels", "关键点位 [symbol]"),
                BotCommand("hk_regime", "Regime 分类 [symbol]"),
                BotCommand("hk_quote", "详细报价 <symbol>"),
                BotCommand("hk_filters", "交易过滤状态 [symbol]"),
                BotCommand("hk_watchlist", "监控列表总览"),
                BotCommand("hk_orderbook", "LV2 盘口 [symbol]"),
                BotCommand("hk_gamma", "Gamma Wall [symbol]"),
                BotCommand("hk_help", "指令列表与别名"),
            ])
            logger.info("Telegram command menu registered")
            await app.updater.start_polling(drop_pending_updates=True)
            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await app.updater.stop()
                await app.stop()
    else:
        logger.warning("Telegram not configured, messages will be logged only")

        async def send_tg(text: str, **kwargs) -> None:
            logger.info("TG (dry): %s", text[:200])

        predictor.set_send_fn(send_tg)

        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    scheduler.shutdown()
    await predictor.close()
    logger.info("HKPredictor shutdown")


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
