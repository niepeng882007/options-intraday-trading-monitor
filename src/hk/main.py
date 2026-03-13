"""HK Playbook orchestrator — on-demand playbook + auto-scan alert system.

Usage:
    python -m src.hk          # Run as standalone (dev/debug)
    # Or integrated into main OptionsMonitor via shared Telegram Application
"""

from __future__ import annotations

import asyncio
import html
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yaml

from src.common.chart import ChartData, generate_chart_async
from src.common.types import PlaybookResponse
from src.hk import (
    FilterResult, GammaWallResult, HKKeyLevels, OptionMarketSnapshot, OptionRecommendation,
    Playbook, QuoteSnapshot,
    RegimeResult, RegimeType, ScanAlertRecord, ScanSignal,
    VolumeProfileResult,
)
from src.hk.collector import HKCollector
from src.hk.filter import check_filters
from src.hk.gamma_wall import calculate_gamma_wall
from src.hk.indicators import (
    build_hk_key_levels,
    calculate_avg_daily_range,
    calculate_initial_balance,
    calculate_peak_session_rvol,
    calculate_rvol,
    calculate_vwap,
    detect_volume_pulse,
    get_history_bars,
    get_today_bars,
    minutes_to_close_hk,
)
from src.hk.option_recommend import recommend, select_expiry
from src.hk.playbook import format_playbook_message, generate_playbook
from src.hk.regime import classify_regime, _intraday_trend
from src.hk.volume_profile import calculate_volume_profile
from src.hk.watchlist import HKWatchlist
from src.utils.logger import setup_logger

logger = setup_logger("hk_predictor")

HKT = timezone(timedelta(hours=8))
_executor = ThreadPoolExecutor(max_workers=1)
_esc = html.escape

DEFAULT_CONFIG_PATH = "config/hk_settings.yaml"

# Cache TTL for historical K-line + VP (today bars always fetched fresh)
_VP_CACHE_TTL = 120  # 2 min for multi-day VP data


def _detect_volume_surges(
    today_bars: pd.DataFrame,
    threshold: float = 3.0,
    recent_n: int = 5,
) -> list[str]:
    """Detect volume surges in recent bars (>threshold x median volume).

    Returns warning strings for risk section.
    """
    if today_bars.empty or len(today_bars) < 10:
        return []

    median_vol = float(today_bars["Volume"].median())
    if median_vol <= 0:
        return []

    recent = today_bars.iloc[-recent_n:]
    surges = recent[recent["Volume"] > median_vol * threshold]

    if surges.empty:
        return []

    max_ratio = float(surges["Volume"].max() / median_vol)
    surge_count = len(surges)

    t_start = surges.index[0].strftime("%H:%M")
    warnings: list[str] = []

    if surge_count == 1:
        warnings.append(f"量能突变: {t_start} 出现 {max_ratio:.1f}x 放量, 注意 Regime 转换风险")
    else:
        t_end = surges.index[-1].strftime("%H:%M")
        price_start = float(surges.iloc[0]["Open"])
        price_end = float(surges.iloc[-1]["Close"])
        move_pct = (price_end - price_start) / price_start * 100
        direction = "↑" if move_pct > 0 else "↓"
        warnings.append(
            f"量能突变: {t_start}-{t_end} 连续 {surge_count} 根 bar 放量"
            f" (最高 {max_ratio:.1f}x), 价格 {direction}{abs(move_pct):.2f}%"
        )

    return warnings


def _has_volume_surge(
    today_bars: pd.DataFrame,
    threshold: float = 2.0,
    recent_n: int = 5,
) -> bool:
    """Check if there's a volume surge in recent bars (for L1 screening)."""
    if today_bars.empty or len(today_bars) < 10:
        return False
    median_vol = float(today_bars["Volume"].median())
    if median_vol <= 0:
        return False
    recent = today_bars.iloc[-recent_n:]
    return bool((recent["Volume"] > median_vol * threshold).any())


def _load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class HKPredictor:
    """Orchestrates HK Playbook pipeline — on-demand + auto-scan.

    Data flow per query:
        1. Fetch 1m K-lines (5 days) → Volume Profile (POC/VAH/VAL)
        2. Fetch today's K-lines → VWAP
        3. Compute RVOL (session-aware)
        4. Fetch option chain OI → Gamma Wall (indices only) + IV
        5. Fetch quote → Filter checks
        6. Classify Regime (with IV)
        7. Generate option recommendation
        8. Format & return Playbook
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self._cfg = _load_config(config_path)
        futu_cfg = self._cfg.get("futu", {})
        self._collector = HKCollector(
            host=os.getenv("FUTU_HOST", futu_cfg.get("host", "127.0.0.1")),
            port=futu_cfg.get("port", 11111),
        )
        self._connected = False

        # Watchlist — dynamic, persisted to JSON
        self.watchlist = HKWatchlist(
            path="data/hk_watchlist.json",
            initial_config=self._cfg,
        )

        # Cache: only historical bars for VP (today bars always fetched fresh)
        self._vp_cache: dict[str, tuple[float, pd.DataFrame]] = {}  # symbol → (ts, hist_bars)

        # Auto-scan state: per-symbol alert history for frequency control
        self._scan_history: dict[str, list[ScanAlertRecord]] = {}
        self._scan_history_date: str = ""  # YYYY-MM-DD, reset daily

        # Market context cache: symbol → (RegimeResult, timestamp)
        self._market_context_cache: dict[str, tuple[RegimeResult, float]] = {}

    # ── Lifecycle ──

    async def connect(self) -> None:
        await self._run_sync(self._collector.connect)
        self._connected = True
        logger.info("HKPredictor connected")

    async def close(self) -> None:
        await self._run_sync(self._collector.close)
        self._connected = False

    async def _ensure_connected(self) -> None:
        """Ensure Futu connection is active; attempt reconnect if needed."""
        if self._connected:
            return
        logger.info("HKPredictor not connected, attempting reconnect...")
        try:
            await self._run_sync(self._collector.connect)
            self._connected = True
            logger.info("HKPredictor reconnected successfully")
        except Exception as e:
            raise ConnectionError(f"HK Futu 连接不可用，请检查 FutuOpenD: {e}") from e

    # ── Sync → Async bridge ──

    async def _run_sync(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, fn, *args)

    # ── Core: analysis pipeline (shared by on-demand and auto-scan) ──

    async def _run_analysis_pipeline(self, symbol: str) -> tuple[
        RegimeResult, VolumeProfileResult, float, FilterResult,
        OptionRecommendation, GammaWallResult | None, Playbook,
        pd.DataFrame,  # today_bars (for volume surge detection)
    ]:
        """Run the full analysis pipeline for a symbol. Returns all intermediate results."""
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        regime_cfg = cfg.get("regime", {})
        filter_cfg = cfg.get("filters", {})
        calendar_path = cfg.get("calendar_file", "config/hk_calendar.yaml")
        index_symbols = cfg.get("gamma_wall", {}).get("index_symbols", [])

        # 1. K-lines (with cache)
        hist_bars, today_bars = await self._get_bars_cached(symbol, vp_cfg)

        # 2. Volume Profile
        vp = calculate_volume_profile(
            hist_bars,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
            recency_decay=vp_cfg.get("recency_decay", 0.15),
        )

        # 3. VWAP
        vwap = calculate_vwap(today_bars)
        if not today_bars.empty:
            logger.info(
                "VWAP debug %s: vwap=%.2f, today_bars=%d rows, "
                "date_range=[%s ~ %s], price_range=[%.2f ~ %.2f]",
                symbol, vwap, len(today_bars),
                today_bars.index[0], today_bars.index[-1],
                float(today_bars["Close"].min()), float(today_bars["Close"].max()),
            )

        # 4. RVOL (prefer Futu volume_ratio when available)
        calc_rvol = calculate_rvol(
            today_bars, hist_bars,
            lookback_days=cfg.get("rvol", {}).get("lookback_days", 10),
        )

        # 5. Quote
        quote = await self._run_sync(self._collector.get_quote, symbol)
        quote_snapshot = QuoteSnapshot(**quote)
        price = quote_snapshot.last_price
        turnover = quote_snapshot.turnover

        # 5b. RVOL source selection: Futu volume_ratio > K-line calculated
        futu_rvol = quote_snapshot.volume_ratio
        rvol = futu_rvol if futu_rvol > 0 else calc_rvol
        logger.debug(
            "RVOL %s: futu=%.2f, calc=%.2f, used=%.2f (source=%s)",
            symbol, futu_rvol, calc_rvol, rvol,
            "futu" if futu_rvol > 0 else "calc",
        )

        # 6. Gamma Wall + Option chain
        gamma_wall: GammaWallResult | None = None
        chain_df: pd.DataFrame = pd.DataFrame()
        expiry_dates: list[dict] = []
        target_expiry: str | None = None
        atm_iv = 0.0
        avg_iv = 0.0

        idx_opt_type = "NORMAL" if symbol in index_symbols else None

        try:
            expiry_dates = await self._run_sync(
                self._collector.get_option_expiration_dates, symbol, idx_opt_type,
            )
            logger.debug("Expiry dates for %s: %d found", symbol, len(expiry_dates))

            target_expiry = select_expiry(expiry_dates)

            if not target_expiry:
                logger.warning("No valid expiry for %s (raw dates: %s)", symbol, expiry_dates)
            else:
                chain_df = await self._run_sync(
                    self._collector.get_option_chain_with_oi,
                    symbol, idx_opt_type, target_expiry,
                )
                logger.debug("Option chain for %s: %d rows", symbol, len(chain_df))

            if not chain_df.empty:
                if symbol in index_symbols:
                    gamma_wall = calculate_gamma_wall(chain_df, price)
                atm_iv, avg_iv = self._extract_iv(chain_df, price)

        except Exception:
            logger.warning("Option chain fetch failed for %s", symbol, exc_info=True)

        # 6b. IBH/IBL
        ib_window = regime_cfg.get("ib_window_minutes", 30)
        ibh, ibl = calculate_initial_balance(today_bars, window_minutes=ib_window)

        # 6c. PDC / Day Open / avg_daily_range
        pdc = quote_snapshot.prev_close
        day_open_price = quote_snapshot.open_price
        avg_daily_range_pct = calculate_avg_daily_range(hist_bars, lookback_days=10)

        # 7. Filters
        prev_high, prev_low = 0.0, 0.0
        prev_close = 0.0
        if not hist_bars.empty:
            last_day = hist_bars.index[-1].date()
            last_day_bars = hist_bars[hist_bars.index.date == last_day]
            if not last_day_bars.empty:
                prev_high = float(last_day_bars["High"].max())
                prev_low = float(last_day_bars["Low"].min())
                prev_close = float(last_day_bars["Close"].iloc[-1])

        iv_rank = 0.0
        if avg_iv > 0 and atm_iv > 0:
            iv_rank = min(100.0, (atm_iv / avg_iv) * 50.0)

        expiry_as_date: date | None = None
        if target_expiry:
            try:
                expiry_as_date = datetime.strptime(target_expiry[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass

        filters = check_filters(
            symbol=symbol,
            turnover=turnover,
            prev_high=prev_high,
            prev_low=prev_low,
            current_high=quote_snapshot.high_price,
            current_low=quote_snapshot.low_price,
            rvol=rvol,
            iv_rank=iv_rank,
            expiry_date=expiry_as_date,
            calendar_path=calendar_path,
            min_turnover=filter_cfg.get("min_turnover_hkd", 1e8),
        )

        # 8. Volume surge detection (before regime, feeds into classify_regime)
        has_surge = _has_volume_surge(today_bars)
        surge_warnings = _detect_volume_surges(today_bars)
        if surge_warnings:
            filters.warnings.extend(surge_warnings)

        # 8a. Pulse detection + peak session RVOL
        pulse = detect_volume_pulse(today_bars)
        p_peak_ratio = pulse.peak_ratio if pulse else 0.0
        p_displacement = pulse.displacement_pct if pulse else 0.0
        peak_session_rvol = calculate_peak_session_rvol(today_bars, hist_bars)

        # 8b. Regime classification
        intraday_range = quote_snapshot.high_price - quote_snapshot.low_price
        regime = classify_regime(
            price=price,
            rvol=rvol,
            vp=vp,
            gamma_wall=gamma_wall,
            atm_iv=atm_iv,
            avg_iv=avg_iv,
            breakout_rvol=regime_cfg.get("breakout_rvol", 1.2),
            range_rvol=regime_cfg.get("range_rvol", 0.8),
            iv_spike_ratio=regime_cfg.get("iv_spike_ratio", 1.3),
            intraday_range=intraday_range,
            has_volume_surge=has_surge,
            momentum_min_dist_pct=regime_cfg.get("momentum_min_dist_pct", 1.0),
            vwap=vwap,
            open_price=quote_snapshot.open_price,
            prev_close=quote_snapshot.prev_close,
            today_bars=today_bars,
            gap_warning_pct=regime_cfg.get("gap_warning_pct", 3.0),
            va_penetration_min_pct=regime_cfg.get("va_penetration_min_pct", 0.3),
            failed_breakout_pct=regime_cfg.get("failed_breakout_pct", 0.5),
            # New 5-class params
            ibh=ibh,
            ibl=ibl,
            pdc=pdc,
            day_open=day_open_price,
            gap_and_go_gap_pct=regime_cfg.get("gap_and_go_gap_pct", 1.0),
            gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.2),
            trend_day_rvol=regime_cfg.get("trend_day_rvol", 0.0),
            fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 0.0),
            unclear_atr_pct=regime_cfg.get("unclear_atr_pct", 0.5),
            unclear_vwap_proximity_pct=regime_cfg.get("unclear_vwap_proximity_pct", 0.5),
            # Pulse + peak RVOL params
            pulse_peak_ratio=p_peak_ratio,
            pulse_displacement_pct=p_displacement,
            peak_rvol=peak_session_rvol,
            directional_trap_pct=regime_cfg.get("directional_trap_pct", 1.5),
            pulse_min_ratio=regime_cfg.get("pulse_min_ratio", 2.5),
            pulse_min_displacement_pct=regime_cfg.get("pulse_min_displacement_pct", 1.0),
        )

        # 8c. Build HKKeyLevels
        hk_kl = build_hk_key_levels(
            vp, vwap, prev_high, prev_low, pdc, day_open_price, ibh, ibl, gamma_wall,
        )

        # 9. Option recommendation
        chase_risk_cfg = cfg.get("chase_risk", {})
        opt_rec_cfg = cfg.get("option_recommend", {})
        option_rec = recommend(
            regime=regime,
            vp=vp,
            filters=filters,
            chain_df=chain_df if not chain_df.empty else None,
            expiry_dates=expiry_dates,
            gamma_wall=gamma_wall,
            vwap=vwap,
            chase_risk_cfg=chase_risk_cfg,
            range_min_dte=opt_rec_cfg.get("range_min_dte", 2),
        )

        option_market = OptionMarketSnapshot(
            expiry=target_expiry,
            contract_count=len(chain_df),
            call_contract_count=int(
                (chain_df["option_type"].astype(str).str.upper() == "CALL").sum()
            ) if not chain_df.empty and "option_type" in chain_df.columns else 0,
            put_contract_count=int(
                (chain_df["option_type"].astype(str).str.upper() == "PUT").sum()
            ) if not chain_df.empty and "option_type" in chain_df.columns else 0,
            atm_iv=atm_iv,
            avg_iv=avg_iv,
            iv_ratio=(atm_iv / avg_iv) if avg_iv > 0 else 0.0,
        )

        # 10. Generate playbook
        playbook = generate_playbook(
            regime=regime,
            vp=vp,
            vwap=vwap,
            gamma_wall=gamma_wall,
            filters=filters,
            symbol=symbol,
            option_rec=option_rec,
            quote=quote_snapshot,
            option_market=option_market,
            key_levels_obj=hk_kl,
            avg_daily_range_pct=avg_daily_range_pct,
        )

        return regime, vp, vwap, filters, option_rec, gamma_wall, playbook, today_bars

    # ── Market context: HSI/HSTECH regime (simplified pipeline) ──

    async def _get_market_context_regime(self, symbol: str) -> RegimeResult | None:
        """Simplified pipeline for HSI/HSTECH — bars, VP, VWAP, RVOL, IBH/IBL, regime.

        No option chain, gamma wall, or filters.  Cached with configurable TTL.
        """
        ctx_cfg = self._cfg.get("market_context", {})
        ttl = ctx_cfg.get("context_ttl_seconds", 300)

        cached = self._market_context_cache.get(symbol)
        if cached:
            result, ts = cached
            if time.time() - ts < ttl:
                return result

        try:
            vp_cfg = self._cfg.get("volume_profile", {})
            regime_cfg = self._cfg.get("regime", {})

            hist_bars, today_bars = await self._get_bars_cached(symbol, vp_cfg)
            if hist_bars.empty:
                return None

            vp = calculate_volume_profile(
                hist_bars,
                value_area_pct=vp_cfg.get("value_area_pct", 0.70),
                recency_decay=vp_cfg.get("recency_decay", 0.15),
            )
            vwap = calculate_vwap(today_bars) if not today_bars.empty else 0.0
            rvol = calculate_rvol(
                today_bars, hist_bars,
                lookback_days=self._cfg.get("rvol", {}).get("lookback_days", 10),
            )

            quote = await self._run_sync(self._collector.get_quote, symbol)
            price = quote.get("last_price", 0.0)
            open_price = quote.get("open_price", 0.0)
            prev_close = quote.get("prev_close", 0.0)

            ib_window = regime_cfg.get("ib_window_minutes", 30)
            ibh, ibl = calculate_initial_balance(today_bars, window_minutes=ib_window)

            has_surge = _has_volume_surge(today_bars)
            intraday_range = float(today_bars["High"].max() - today_bars["Low"].min()) if not today_bars.empty else 0.0

            pulse = detect_volume_pulse(today_bars)
            p_peak = pulse.peak_ratio if pulse else 0.0
            p_disp = pulse.displacement_pct if pulse else 0.0
            peak_rvol = calculate_peak_session_rvol(today_bars, hist_bars)

            result = classify_regime(
                price=price,
                rvol=rvol,
                vp=vp,
                breakout_rvol=regime_cfg.get("breakout_rvol", 1.2),
                range_rvol=regime_cfg.get("range_rvol", 0.8),
                has_volume_surge=has_surge,
                momentum_min_dist_pct=regime_cfg.get("momentum_min_dist_pct", 1.0),
                vwap=vwap,
                open_price=open_price,
                prev_close=prev_close,
                today_bars=today_bars,
                gap_warning_pct=regime_cfg.get("gap_warning_pct", 3.0),
                va_penetration_min_pct=regime_cfg.get("va_penetration_min_pct", 0.3),
                failed_breakout_pct=regime_cfg.get("failed_breakout_pct", 0.5),
                intraday_range=intraday_range,
                ibh=ibh,
                ibl=ibl,
                pdc=prev_close,
                day_open=open_price,
                gap_and_go_gap_pct=regime_cfg.get("gap_and_go_gap_pct", 1.0),
                gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.2),
                trend_day_rvol=regime_cfg.get("trend_day_rvol", 0.0),
                fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 0.0),
                unclear_atr_pct=regime_cfg.get("unclear_atr_pct", 0.5),
                unclear_vwap_proximity_pct=regime_cfg.get("unclear_vwap_proximity_pct", 0.5),
                pulse_peak_ratio=p_peak,
                pulse_displacement_pct=p_disp,
                peak_rvol=peak_rvol,
                directional_trap_pct=regime_cfg.get("directional_trap_pct", 1.5),
                pulse_min_ratio=regime_cfg.get("pulse_min_ratio", 2.5),
                pulse_min_displacement_pct=regime_cfg.get("pulse_min_displacement_pct", 1.0),
            )

            self._market_context_cache[symbol] = (result, time.time())
            return result

        except Exception:
            logger.warning("Market context fetch failed for %s", symbol, exc_info=True)
            return None

    # ── Core: on-demand playbook for a single symbol ──

    async def generate_playbook_for_symbol(self, symbol: str) -> PlaybookResponse:
        """Generate aggregated playbook for a single symbol. Returns PlaybookResponse with HTML + chart."""
        self._reset_scan_history_if_new_day()
        await self._ensure_connected()
        regime, vp, vwap, _filters, _opt_rec, gw, playbook, today_bars = (
            await self._run_analysis_pipeline(symbol)
        )
        name = self.watchlist.get_name(symbol)
        display = f"{name} ({symbol})" if name != symbol else symbol

        # Fetch market context (HSI / HSTECH)
        ctx_cfg = self._cfg.get("market_context", {})
        hsi_regime = await self._get_market_context_regime(ctx_cfg.get("hsi_symbol", "HK.800000"))
        hstech_regime = await self._get_market_context_regime(ctx_cfg.get("hstech_symbol", "HK.800700"))

        html_text = format_playbook_message(
            playbook, symbol=display,
            hsi_regime=hsi_regime, hstech_regime=hstech_regime,
        )

        # Generate chart (best-effort — failure degrades to text-only)
        chart_bytes: bytes | None = None
        try:
            chart_data = ChartData(
                symbol=display,
                today_bars=today_bars,
                volume_profile=vp,
                vwap=vwap,
                last_price=regime.price,
                prev_close=playbook.quote.prev_close if playbook.quote else 0.0,
                regime_label=f"{regime.regime.value.upper()} {regime.confidence:.0%}",
                key_levels=playbook.key_levels,
                gamma_wall=gw,
            )
            buf = await generate_chart_async(chart_data)
            if buf is not None:
                chart_bytes = buf.getvalue()
        except Exception:
            logger.warning("Chart generation failed for %s", symbol, exc_info=True)

        return PlaybookResponse(html=html_text, chart=chart_bytes)

    # ── Auto-scan: window check ──

    @staticmethod
    def _get_scan_window(
        scan_cfg: dict,
        now_hkt: datetime | None = None,
    ) -> tuple[bool, str]:
        """Check if current time is within a scan window.

        Returns (in_window, session_name). session_name is "morning" or "afternoon".
        """
        if now_hkt is None:
            now_hkt = datetime.now(HKT)

        # Weekday check (Mon=0 to Fri=4)
        if now_hkt.weekday() > 4:
            return False, ""

        t = now_hkt.hour * 60 + now_hkt.minute

        for session_name, key in [("morning", "morning_window"), ("afternoon", "afternoon_window")]:
            window = scan_cfg.get(key)
            if not window or len(window) < 2:
                continue
            s_h, s_m = map(int, window[0].split(":"))
            e_h, e_m = map(int, window[1].split(":"))
            if s_h * 60 + s_m <= t <= e_h * 60 + e_m:
                return True, session_name

        return False, ""

    # ── Auto-scan: L1 lightweight screen ──

    async def _l1_screen(
        self,
        symbol: str,
        session: str,
        scan_cfg: dict,
    ) -> dict | None:
        """Lightweight L1 screen (~200ms). Returns screening data if passed, None otherwise."""
        vp_cfg = self._cfg.get("volume_profile", {})
        breakout_cfg = scan_cfg.get("breakout", {})
        range_cfg = scan_cfg.get("range", {})

        # Quote
        quote = await self._run_sync(self._collector.get_quote, symbol)
        price = quote["last_price"]
        if price <= 0:
            return None

        # VP (from cache or fetch)
        hist_bars, today_bars = await self._get_bars_cached(symbol, vp_cfg)
        vp = calculate_volume_profile(
            hist_bars,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
            recency_decay=vp_cfg.get("recency_decay", 0.15),
        )
        if vp.vah <= 0 or vp.val <= 0:
            return None

        # RVOL (prefer Futu volume_ratio when available)
        calc_rvol = calculate_rvol(
            today_bars, hist_bars,
            lookback_days=self._cfg.get("rvol", {}).get("lookback_days", 10),
        )
        futu_rvol = float(quote.get("volume_ratio", 0) or 0)
        rvol = futu_rvol if futu_rvol > 0 else calc_rvol
        logger.debug(
            "L1 RVOL %s: futu=%.2f, calc=%.2f, used=%.2f (source=%s)",
            symbol, futu_rvol, calc_rvol, rvol,
            "futu" if futu_rvol > 0 else "calc",
        )

        # Volume surge (before regime, feeds into classify_regime)
        has_surge = _has_volume_surge(today_bars)

        # L1 VWAP (lightweight, ~1ms)
        l1_vwap = calculate_vwap(today_bars) if not today_bars.empty else 0.0

        # Pulse detection + peak session RVOL
        l1_pulse = detect_volume_pulse(today_bars)
        l1_p_peak = l1_pulse.peak_ratio if l1_pulse else 0.0
        l1_p_disp = l1_pulse.displacement_pct if l1_pulse else 0.0
        l1_peak_rvol = calculate_peak_session_rvol(today_bars, hist_bars)

        # Preliminary regime (no option chain / gamma wall — lightweight)
        regime_cfg = self._cfg.get("regime", {})
        ib_window = regime_cfg.get("ib_window_minutes", 30)
        l1_ibh, l1_ibl = calculate_initial_balance(today_bars, window_minutes=ib_window)
        l1_open = quote.get("open_price", 0.0)
        l1_prev_close = quote.get("prev_close", 0.0)
        regime = classify_regime(
            price=price,
            rvol=rvol,
            vp=vp,
            breakout_rvol=regime_cfg.get("breakout_rvol", 1.2),
            range_rvol=regime_cfg.get("range_rvol", 0.8),
            has_volume_surge=has_surge,
            momentum_min_dist_pct=regime_cfg.get("momentum_min_dist_pct", 1.0),
            vwap=l1_vwap,
            open_price=l1_open,
            prev_close=l1_prev_close,
            today_bars=today_bars,
            gap_warning_pct=regime_cfg.get("gap_warning_pct", 3.0),
            va_penetration_min_pct=regime_cfg.get("va_penetration_min_pct", 0.3),
            failed_breakout_pct=regime_cfg.get("failed_breakout_pct", 0.5),
            ibh=l1_ibh,
            ibl=l1_ibl,
            pdc=l1_prev_close,
            day_open=l1_open,
            gap_and_go_gap_pct=regime_cfg.get("gap_and_go_gap_pct", 1.0),
            gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.2),
            trend_day_rvol=regime_cfg.get("trend_day_rvol", 0.0),
            fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 0.0),
            unclear_atr_pct=regime_cfg.get("unclear_atr_pct", 0.5),
            unclear_vwap_proximity_pct=regime_cfg.get("unclear_vwap_proximity_pct", 0.5),
            pulse_peak_ratio=l1_p_peak,
            pulse_displacement_pct=l1_p_disp,
            peak_rvol=l1_peak_rvol,
            directional_trap_pct=regime_cfg.get("directional_trap_pct", 1.5),
            pulse_min_ratio=regime_cfg.get("pulse_min_ratio", 2.5),
            pulse_min_displacement_pct=regime_cfg.get("pulse_min_displacement_pct", 1.0),
        )

        # ── TREND / GAP_AND_GO L1 check (both sessions) ──
        bo_min_conf = breakout_cfg.get("min_confidence", 0.72)
        bo_min_rvol = breakout_cfg.get("min_rvol", 1.35)
        bo_min_mag = breakout_cfg.get("min_magnitude_pct", 0.15)
        bo_surge_threshold = breakout_cfg.get("volume_surge_threshold", 2.0)
        bo_surge_bars = breakout_cfg.get("volume_surge_bars", 5)

        trend_regimes = {RegimeType.GAP_AND_GO, RegimeType.TREND_DAY, RegimeType.BREAKOUT}
        if (
            regime.regime in trend_regimes
            and regime.confidence >= bo_min_conf
            and rvol >= bo_min_rvol
        ):
            # Enhanced condition: at least 1 of 2
            magnitude = 0.0
            if price > vp.vah and vp.vah > 0:
                magnitude = (price - vp.vah) / price * 100
            elif price < vp.val and vp.val > 0:
                magnitude = (vp.val - price) / price * 100

            has_surge = _has_volume_surge(today_bars, bo_surge_threshold, bo_surge_bars)

            if magnitude >= bo_min_mag or has_surge:
                direction = "bullish" if price > vp.vah else "bearish"

                # Step 6: Explicit trend + VWAP filter (double safeguard)
                trend_dir, trend_strength = _intraday_trend(today_bars)
                if trend_strength >= 0.5:
                    if direction == "bullish" and trend_dir == "falling":
                        logger.info("L1 reject %s: %s bullish but intraday falling", symbol, regime.regime.value)
                        return None
                    if direction == "bearish" and trend_dir == "rising":
                        logger.info("L1 reject %s: %s bearish but intraday rising", symbol, regime.regime.value)
                        return None

                if l1_vwap > 0:
                    if direction == "bullish" and price < l1_vwap:
                        logger.info("L1 reject %s: %s bullish but below VWAP", symbol, regime.regime.value)
                        return None
                    if direction == "bearish" and price > l1_vwap:
                        logger.info("L1 reject %s: %s bearish but above VWAP", symbol, regime.regime.value)
                        return None

                triggers = []
                if magnitude >= bo_min_mag:
                    boundary = "VAH" if price > vp.vah else "VAL"
                    triggers.append(f"突破 {boundary} {magnitude:.2f}%")
                if has_surge:
                    triggers.append("最近 5 根 bar 量能突变")

                signal_type = regime.regime.value.upper()
                return {
                    "signal_type": signal_type,
                    "direction": direction,
                    "regime": regime,
                    "vp": vp,
                    "rvol": rvol,
                    "price": price,
                    "today_bars": today_bars,
                    "trigger_reasons": triggers,
                }

        # ── FADE_CHOP L1 check (morning only) ──
        if session != "morning":
            return None

        rng_min_conf = range_cfg.get("min_confidence", 0.72)
        rng_rvol_min = range_cfg.get("rvol_min", 0.55)
        rng_rvol_max = range_cfg.get("rvol_max", 0.90)
        rng_prox = range_cfg.get("va_proximity_pct", 0.30)

        fade_regimes = {RegimeType.FADE_CHOP, RegimeType.RANGE}
        if (
            regime.regime in fade_regimes
            and regime.confidence >= rng_min_conf
            and rng_rvol_min <= rvol <= rng_rvol_max
        ):
            # L1 breach rejection: if today already breached VA boundary significantly,
            # the level has been tested — FADE_CHOP mean-reversion thesis is weakened
            fb_pct = regime_cfg.get("failed_breakout_pct", 0.5)
            if not today_bars.empty and vp.vah > 0 and vp.val > 0:
                t_high = float(today_bars["High"].max())
                t_low = float(today_bars["Low"].min())
                if t_high > vp.vah and (t_high - vp.vah) / vp.vah * 100 >= fb_pct:
                    logger.info("L1 reject %s: FADE_CHOP but today breached VAH (%.2f > %.2f)", symbol, t_high, vp.vah)
                    return None
                if t_low < vp.val and (vp.val - t_low) / vp.val * 100 >= fb_pct:
                    logger.info("L1 reject %s: FADE_CHOP but today breached VAL (%.2f < %.2f)", symbol, t_low, vp.val)
                    return None

            # Price within proximity of VAH or VAL
            dist_vah = abs(price - vp.vah) / price * 100 if price > 0 else 999
            dist_val = abs(price - vp.val) / price * 100 if price > 0 else 999
            near_boundary = min(dist_vah, dist_val)

            if near_boundary <= rng_prox:
                direction = "bearish" if dist_vah < dist_val else "bullish"
                boundary = "VAH" if dist_vah < dist_val else "VAL"

                # VWAP structural veto: skip FADE_CHOP signal if VWAP contradicts direction
                if l1_vwap > 0:
                    if direction == "bearish" and l1_vwap > vp.vah:
                        logger.info("L1 reject %s: FADE_CHOP bearish but VWAP %.2f > VAH %.2f", symbol, l1_vwap, vp.vah)
                        return None
                    if direction == "bullish" and l1_vwap < vp.val:
                        logger.info("L1 reject %s: FADE_CHOP bullish but VWAP %.2f < VAL %.2f", symbol, l1_vwap, vp.val)
                        return None

                triggers = [f"接近 {boundary} (距离 {near_boundary:.2f}%)"]

                signal_type = regime.regime.value.upper()
                return {
                    "signal_type": signal_type,
                    "direction": direction,
                    "regime": regime,
                    "vp": vp,
                    "rvol": rvol,
                    "price": price,
                    "today_bars": today_bars,
                    "trigger_reasons": triggers,
                }

        return None

    # ── Auto-scan: L2 full verification ──

    async def _l2_verify(
        self,
        symbol: str,
        l1_data: dict,
    ) -> tuple[ScanSignal, str, OptionRecommendation, FilterResult] | None:
        """Full pipeline verification (~2-3s). Returns (signal, playbook_html, option_rec, filters) or None."""
        regime, vp, vwap, filters, option_rec, gamma_wall, playbook, today_bars = (
            await self._run_analysis_pipeline(symbol)
        )

        # Base conditions
        if not filters.tradeable:
            logger.debug("L2 reject %s: not tradeable", symbol)
            return None
        if filters.risk_level == "high":
            logger.debug("L2 reject %s: risk_level=high", symbol)
            return None
        if option_rec.action == "wait":
            logger.debug("L2 reject %s: option_rec=wait", symbol)
            return None

        signal_type = l1_data["signal_type"]
        direction = l1_data["direction"]

        # Trend regimes: re-verify with full regime (now includes option chain / gamma wall)
        trend_types = {"GAP_AND_GO", "TREND_DAY", "BREAKOUT"}
        fade_types = {"FADE_CHOP", "RANGE"}
        if signal_type in trend_types:
            if regime.regime.value.upper() not in trend_types:
                logger.debug("L2 reject %s: full regime not trend (%s)", symbol, regime.regime)
                return None

        # Fade regimes: verify action is actionable
        if signal_type in fade_types:
            if regime.regime.value.upper() not in fade_types:
                logger.debug("L2 reject %s: full regime not fade (%s)", symbol, regime.regime)
                return None
            allowed_actions = {"bull_put_spread", "bear_call_spread", "call", "put"}
            if option_rec.action not in allowed_actions:
                logger.debug("L2 reject %s: FADE_CHOP action=%s not actionable", symbol, option_rec.action)
                return None

        # Build signal
        signal = ScanSignal(
            signal_type=signal_type,
            direction=direction,
            symbol=symbol,
            regime=regime,
            price=regime.price,
            trigger_reasons=l1_data["trigger_reasons"],
            timestamp=time.time(),
        )

        # Format playbook HTML
        name = self.watchlist.get_name(symbol)
        display = f"{name} ({symbol})" if name != symbol else symbol
        playbook_html = format_playbook_message(playbook, symbol=display)

        return signal, playbook_html, option_rec, filters

    # ── Auto-scan: frequency control ──

    def _reset_scan_history_if_new_day(self) -> None:
        """Clear scan history on day change."""
        today = datetime.now(HKT).strftime("%Y-%m-%d")
        if self._scan_history_date != today:
            self._scan_history.clear()
            self._scan_history_date = today

    def _check_frequency(
        self,
        symbol: str,
        signal: ScanSignal,
        session: str,
        scan_cfg: dict,
    ) -> tuple[bool, str | None]:
        """3-layer frequency control with override exceptions.

        Returns (allowed, override_reason). override_reason is set if cooldown was bypassed.
        """
        cooldown_cfg = scan_cfg.get("cooldown", {})
        override_cfg = scan_cfg.get("override", {})
        same_signal_mins = cooldown_cfg.get("same_signal_minutes", 30)
        max_per_session = cooldown_cfg.get("max_per_session", 2)
        max_per_day = cooldown_cfg.get("max_per_day", 3)

        records = self._scan_history.get(symbol, [])
        now = time.time()

        weak_regimes = {"FADE_CHOP", "RANGE", "UNCLEAR"}
        strong_regimes = {"GAP_AND_GO", "TREND_DAY", "BREAKOUT"}

        # Layer 3: max per day
        if len(records) >= max_per_day:
            # Check override: regime upgrade (weak → strong)
            if override_cfg.get("regime_upgrade", True):
                last = records[-1]
                if last.signal_type in weak_regimes and signal.signal_type in strong_regimes:
                    return True, f"Regime 从 {last.signal_type} 升级为 {signal.signal_type}"
            logger.debug("Frequency block %s: daily max %d reached", symbol, max_per_day)
            return False, None

        # Layer 2: max per session
        session_records = [r for r in records if r.session == session]
        if len(session_records) >= max_per_session:
            # Check override: regime upgrade (weak → strong)
            if override_cfg.get("regime_upgrade", True):
                last = session_records[-1]
                if last.signal_type in weak_regimes and signal.signal_type in strong_regimes:
                    return True, f"Regime 从 {last.signal_type} 升级为 {signal.signal_type}"
            logger.debug("Frequency block %s: session max %d reached", symbol, max_per_session)
            return False, None

        # Layer 1: same signal_type + direction within cooldown period
        same_signals = [
            r for r in records
            if r.signal_type == signal.signal_type
            and r.direction == signal.direction
            and now - r.timestamp < same_signal_mins * 60
        ]
        if same_signals:
            last = same_signals[-1]

            # Override 1: confidence increase
            conf_threshold = override_cfg.get("confidence_increase", 0.10)
            if signal.regime.confidence - last.confidence >= conf_threshold:
                return True, f"置信度提升 {signal.regime.confidence - last.confidence:.0%}"

            # Override 2: price extension in signal direction
            ext_threshold = override_cfg.get("price_extension_pct", 0.50)
            if last.price > 0:
                if signal.direction == "bullish":
                    price_ext = (signal.price - last.price) / last.price * 100
                else:
                    price_ext = (last.price - signal.price) / last.price * 100
                if price_ext >= ext_threshold:
                    return True, f"价格继续扩展 {price_ext:.2f}%"

            # Override 3: regime upgrade (weak → strong)
            if override_cfg.get("regime_upgrade", True):
                if last.signal_type in weak_regimes and signal.signal_type in strong_regimes:
                    return True, f"Regime 从 {last.signal_type} 升级为 {signal.signal_type}"

            logger.debug(
                "Frequency block %s: same signal within %dmin cooldown",
                symbol, same_signal_mins,
            )
            return False, None

        return True, None

    def _record_alert(self, symbol: str, signal: ScanSignal, session: str) -> None:
        """Record a sent alert for frequency tracking."""
        record = ScanAlertRecord(
            symbol=symbol,
            signal_type=signal.signal_type,
            direction=signal.direction,
            confidence=signal.regime.confidence,
            price=signal.price,
            timestamp=signal.timestamp,
            session=session,
        )
        self._scan_history.setdefault(symbol, []).append(record)

    # ── Auto-scan: alert message formatting ──

    @staticmethod
    def _format_scan_header(
        signal: ScanSignal,
        risk_level: str,
        override_reason: str | None,
        cooldown_mins: int = 30,
    ) -> str:
        """Format the alert header prepended to the playbook."""
        trend_types = {"GAP_AND_GO", "TREND_DAY", "BREAKOUT"}
        if signal.signal_type in trend_types:
            emoji = "\U0001f680" if signal.direction == "bullish" else "\U0001f4a5"
        else:
            emoji = "\U0001f4e6"
        dir_cn = "看多" if signal.direction == "bullish" else "看空"
        dir_arrow = "\u2191" if signal.direction == "bullish" else "\u2193"

        display_type = signal.signal_type.replace("_", " ")
        lines = [
            f"\U0001f514 <b>{display_type} 强信号</b> {emoji} {dir_arrow} {dir_cn}",
        ]
        if signal.signal_type in trend_types:
            lines.append("  当前状态: 高置信度趋势突破")
        else:
            lines.append("  当前状态: 高置信度区间边界信号")

        lines.append("  触发原因:")
        for reason in signal.trigger_reasons:
            lines.append(f"  • {_esc(reason)}")

        lines.append(
            f"  • Confidence {signal.regime.confidence:.0%} | RVOL {signal.regime.rvol:.2f}"
        )

        lines.append("  是否还能追:")
        if signal.signal_type in trend_types:
            lines.append("  • 更适合等回踩确认，不适合已经大幅延伸后的追价。")
        else:
            lines.append("  • 只适合靠近边界时轻仓试单，不适合在区间中间位置追单。")

        lines.append("  风险提示:")
        if risk_level == "elevated":
            lines.append("  • 风险偏高，仓位和出手频率都要收缩。")
        else:
            lines.append("  • 若价格重新回到无优势区域，信号强度会明显下降。")

        if override_reason:
            lines.append(f"  • 冷却期覆盖: {_esc(override_reason)}")

        lines.append(f"  • {cooldown_mins} 分钟内不再重复 (除非信号显著增强)")
        lines.append("\u2501" * 20)

        return "\n".join(lines)

    # ── Auto-scan: main entry point ──

    async def run_auto_scan(self, send_fn) -> None:
        """Scan watchlist for strong signals and push alerts.

        L1 lightweight check per symbol → L2 full pipeline for candidates → frequency control → send.
        """
        scan_cfg = self._cfg.get("auto_scan", {})
        if not scan_cfg.get("enabled", False):
            return
        if not self._connected:
            return

        # Window check
        now_hkt = datetime.now(HKT)
        in_window, session = self._get_scan_window(scan_cfg, now_hkt)
        if not in_window:
            return

        # Reset history on day change
        self._reset_scan_history_if_new_day()

        symbols = self.watchlist.symbols()
        cooldown_mins = scan_cfg.get("cooldown", {}).get("same_signal_minutes", 30)

        for symbol in symbols:
            try:
                # L1: lightweight screen
                l1_data = await self._l1_screen(symbol, session, scan_cfg)
                if l1_data is None:
                    continue

                logger.info(
                    "L1 pass %s: type=%s dir=%s rvol=%.2f price=%.2f",
                    symbol, l1_data["signal_type"], l1_data["direction"],
                    l1_data["rvol"], l1_data["price"],
                )

                # L2: full pipeline verification
                l2_result = await self._l2_verify(symbol, l1_data)
                if l2_result is None:
                    continue

                signal, playbook_html, option_rec, l2_filters = l2_result

                # Frequency control
                allowed, override_reason = self._check_frequency(
                    symbol, signal, session, scan_cfg,
                )
                if not allowed:
                    continue

                risk_level = l2_filters.risk_level

                # Format and send
                header = self._format_scan_header(
                    signal, risk_level, override_reason, cooldown_mins,
                )
                await send_fn(header + "\n" + playbook_html)

                # Record alert
                self._record_alert(symbol, signal, session)

                logger.info(
                    "Auto-scan alert sent: %s %s %s conf=%.2f%s",
                    symbol, signal.signal_type, signal.direction,
                    signal.regime.confidence,
                    f" (override: {override_reason})" if override_reason else "",
                )

            except Exception:
                logger.warning("Auto-scan error for %s", symbol, exc_info=True)

    # ── Helpers ──

    async def _get_bars_cached(
        self, symbol: str, vp_cfg: dict,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Get hist_bars (cached 2min) and today_bars (always fresh)."""
        now = time.time()
        cached = self._vp_cache.get(symbol)

        if cached and now - cached[0] < _VP_CACHE_TTL:
            hist_bars = cached[1]
            # Always fetch fresh today bars for up-to-date RVOL/VWAP
            fresh = await self._run_sync(self._collector.get_history_kline, symbol, 1)
            today_bars = get_today_bars(fresh)
            return hist_bars, today_bars

        lookback = vp_cfg.get("lookback_days", 5)
        bars = await self._run_sync(self._collector.get_history_kline, symbol, lookback)
        hist_bars = get_history_bars(bars, max_trading_days=lookback)
        today_bars = get_today_bars(bars)

        self._vp_cache[symbol] = (now, hist_bars)
        return hist_bars, today_bars

    @staticmethod
    def _extract_iv(chain_df: pd.DataFrame, price: float) -> tuple[float, float]:
        """Extract ATM implied volatility from option chain.

        Returns (atm_iv, avg_iv) where:
        - atm_iv = mean IV of 4 nearest strikes to current price
        - avg_iv = median IV of strikes within ATM ±3 strikes (not full chain,
          to avoid deep OTM skew distortion)
        """
        if chain_df.empty or "implied_volatility" not in chain_df.columns:
            return 0.0, 0.0

        chain_copy = chain_df.copy()
        chain_copy["_dist"] = (chain_copy["strike_price"] - price).abs()

        # Get unique strikes sorted by distance, take nearest 7 (ATM ±3)
        nearest_strikes = (
            chain_copy.drop_duplicates(subset=["strike_price"])
            .nsmallest(7, "_dist")["strike_price"]
            .tolist()
        )
        near_atm = chain_copy[chain_copy["strike_price"].isin(nearest_strikes)]
        near_atm_iv = near_atm["implied_volatility"].dropna()
        near_atm_iv = near_atm_iv[near_atm_iv > 0]
        if near_atm_iv.empty:
            # Fallback to full chain
            all_iv = chain_df["implied_volatility"].dropna()
            all_iv = all_iv[all_iv > 0]
            avg_iv = float(all_iv.median()) if not all_iv.empty else 0.0
        else:
            avg_iv = float(near_atm_iv.median())

        # atm_iv = mean IV of 4 nearest strikes
        atm = chain_copy.nsmallest(4, "_dist")
        atm_iv_vals = atm["implied_volatility"].dropna()
        atm_iv_vals = atm_iv_vals[atm_iv_vals > 0]
        if atm_iv_vals.empty:
            return 0.0, avg_iv

        return float(atm_iv_vals.mean()), avg_iv


# ── Standalone entry point (dev/debug) ──

async def _main() -> None:
    """Run HKPredictor as standalone with Telegram bot polling (no scheduled pushes)."""
    from src.store import message_archive
    message_archive.init("data/monitor.db")

    predictor = HKPredictor()

    # Setup Telegram
    cfg = predictor._cfg.get("telegram", {})
    bot_token = cfg.get("bot_token", "")
    chat_id = cfg.get("chat_id", "")

    import os
    if bot_token.startswith("${"):
        bot_token = os.environ.get(bot_token.strip("${}"), "")
    if str(chat_id).startswith("${"):
        chat_id = os.environ.get(str(chat_id).strip("${}"), "")

    await predictor.connect()

    if bot_token and chat_id:
        from telegram.ext import Application
        from telegram.request import HTTPXRequest
        from src.hk.telegram import register_hk_predictor_handlers

        request = HTTPXRequest(read_timeout=30, write_timeout=30, connect_timeout=15)
        app = Application.builder().token(bot_token).request(request).build()
        register_hk_predictor_handlers(app, predictor)

        logger.info("Starting Telegram bot polling (on-demand mode, no scheduled pushes)...")
        async with app:
            await app.start()
            from telegram import BotCommand
            await app.bot.set_my_commands([
                BotCommand("hk_help", "HK 期权监控使用说明"),
            ])
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
        logger.warning("Telegram not configured — standalone mode requires TELEGRAM_BOT_TOKEN/CHAT_ID")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    await predictor.close()
    message_archive.close()
    logger.info("HKPredictor shutdown")


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
