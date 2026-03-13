"""US Predictor orchestrator — on-demand playbook + auto-scan alert system.

Usage:
    python -m src.us_playbook      # Run standalone
    # Or integrated into OptionsMonitor via shared Telegram Application
"""

from __future__ import annotations

import asyncio
import html
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from src.common.chart import ChartData, generate_chart_async
from src.common.types import FilterResult, GammaWallResult, OptionMarketSnapshot, OptionRecommendation, PlaybookResponse, QuoteSnapshot, VolumeProfileResult
from src.common.gamma_wall import calculate_gamma_wall
from src.us_playbook import (
    KeyLevels,
    MarketTone,
    USPlaybookResult,
    USRegimeResult,
    USRegimeType,
    USScanAlertRecord,
    USScanSignal,
)
from src.us_playbook.market_tone import MarketToneEngine
from src.us_playbook.filter import check_us_filters
from src.us_playbook.indicators import calculate_rsi, calculate_us_rvol, calculate_vwap, compute_rvol_profile
from src.us_playbook.levels import (
    build_key_levels,
    calc_fetch_calendar_days,
    compute_volume_profile,
    extract_previous_day_hl,
    get_history_bars,
    get_today_bars,
)
from src.us_playbook.option_recommend import (
    compute_local_trend,
    option_quotes_to_df,
    recommend,
    select_expiry,
)
from src.us_playbook.playbook import format_us_playbook_message, get_regime_strategy
from src.us_playbook.regime import classify_us_regime, detect_regime_transition, regime_to_signal_type
from src.us_playbook.watchlist import USWatchlist
from src.utils.logger import setup_logger

logger = setup_logger("us_predictor")

ET = ZoneInfo("America/New_York")
_executor = ThreadPoolExecutor(max_workers=1)
_esc = html.escape


@dataclass
class _SymbolCache:
    """Cached data for a symbol — history bars, PDH/PDL, PMH/PML."""
    timestamp: float
    hist_bars: pd.DataFrame
    pdh: float
    pdl: float
    pmh: float
    pml: float
    pm_source: str


@dataclass
class _L1Result:
    """Intermediate data computed during L1 screening, reused by L2."""
    hist_bars: pd.DataFrame
    today_bars: pd.DataFrame
    cached_entry: _SymbolCache
    vp: VolumeProfileResult
    snap: dict
    price: float
    prev_close: float
    rvol: float
    rvol_profile: object  # RvolProfile | None
    regime: USRegimeResult


class USPredictor:
    """Orchestrates US Predictor pipeline — on-demand + auto-scan.

    Data flow per query:
        1. Fetch multi-day 1m bars → VP + PDH/PDL + today bars
        2. Fetch pre-market HL → PMH/PML
        3. Calculate VWAP + RVOL
        4. Fetch option chain → Gamma Wall + option recommendation
        5. Check filters
        6. Classify regime (with SPY context)
        7. Format & return Playbook
    """

    def __init__(self, config: dict, collector) -> None:
        self._cfg = config
        self._collector = collector

        # Watchlist — dynamic, persisted to JSON
        self.watchlist = USWatchlist(
            path="data/us_watchlist.json",
            initial_config=config,
        )

        # Cache: history bars + PDH/PDL + PMH/PML (today bars always fresh)
        _cache_ttl = config.get("hist_cache_ttl", 120)
        self._cache_ttl = _cache_ttl
        self._hist_cache: dict[str, _SymbolCache] = {}

        # SPY context: full pipeline result, long TTL
        self._SPY_CONTEXT_TTL = 300
        self._spy_context: tuple[float, USRegimeResult | None] = (0.0, None)

        # Last playbook results (for display)
        self._last_playbooks: dict[str, USPlaybookResult] = {}
        self._last_today_bars: dict[str, pd.DataFrame] = {}

        # Market Tone engine
        self._tone_engine = MarketToneEngine(config, collector)

        # Auto-scan state
        self._scan_history: dict[str, list[USScanAlertRecord]] = {}
        self._scan_history_date: str = ""
        # P1-4: regime transition tracking — {symbol: last_regime_result}
        self._last_scan_regimes: dict[str, USRegimeResult] = {}
        # Fade direction cooldown — {symbol: (direction, unix_ts)}
        self._last_fade_directions: dict[str, tuple[str, float]] = {}

    # ── Market Tone ──

    async def _ensure_market_tone(self) -> MarketTone | None:
        """Get or compute market tone. Reuses cached SPY today_bars."""
        if not self._cfg.get("market_tone", {}).get("enabled", False):
            return None
        spy_bars = self._last_today_bars.get("SPY")
        return await self._tone_engine.get_tone(spy_today_bars=spy_bars)

    @staticmethod
    def _apply_tone_modifier(regime: USRegimeResult, tone: MarketTone) -> None:
        """Apply market tone confidence modifier to a regime result."""
        if tone.confidence_modifier == 0:
            return
        old_conf = regime.confidence
        regime.confidence = max(0.0, min(1.0, regime.confidence + tone.confidence_modifier))
        if abs(regime.confidence - old_conf) > 0.001:
            tone_note = f"Tone {tone.grade} adj {tone.confidence_modifier:+.2f}"
            regime.details = f"{regime.details}; {tone_note}" if regime.details else tone_note

    # ── Core: analysis pipeline ──

    async def _run_analysis_pipeline(
        self,
        symbol: str,
        spy_regime: USRegimeType | None = None,
    ) -> USPlaybookResult | None:
        """Run full pipeline for a single symbol."""
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        rvol_cfg = cfg.get("rvol", {})
        regime_cfg = cfg.get("regime", {})
        filter_cfg = cfg.get("filters", {})
        option_cfg = cfg.get("option_recommend", {})

        # 1. Get bars (with cache)
        vp_target = vp_cfg.get("lookback_trading_days") or vp_cfg.get("lookback_days", 5)
        min_td = vp_cfg.get("min_trading_days", 3)
        rvol_td = rvol_cfg.get("lookback_days", 10)
        fetch_days = calc_fetch_calendar_days(vp_target, rvol_td)

        hist_bars, today, cached_entry = await self._get_bars_split(symbol, fetch_days, vp_target)

        # 2. Volume Profile
        vp_history = get_history_bars(hist_bars, max_trading_days=vp_target) if not hist_bars.empty else hist_bars
        vp = compute_volume_profile(
            vp_history,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
            recency_decay=vp_cfg.get("recency_decay", 0.15),
        )

        # 3. PDH/PDL and PMH/PML (from cache)
        pdh = cached_entry.pdh
        pdl = cached_entry.pdl
        pmh = cached_entry.pmh
        pml = cached_entry.pml
        pm_source = cached_entry.pm_source

        # 4. Current price (snapshot — no subscription needed)
        snap = await self._collector.get_snapshot(symbol)
        price = snap["last_price"]
        prev_close = snap["prev_close_price"] or 0.0

        # 4b. Build QuoteSnapshot
        quote_snapshot = QuoteSnapshot(
            symbol=symbol,
            last_price=price,
            open_price=snap.get("open_price", 0.0),
            high_price=snap.get("high_price", 0.0),
            low_price=snap.get("low_price", 0.0),
            prev_close=prev_close,
            volume=snap.get("volume", 0),
            turnover=snap.get("turnover", 0.0),
            bid_price=snap.get("bid_price", 0.0),
            ask_price=snap.get("ask_price", 0.0),
            turnover_rate=snap.get("turnover_rate", 0.0),
            amplitude=snap.get("amplitude", 0.0),
        )

        # 5. VWAP + RVOL
        vwap = calculate_vwap(today)
        rvol = calculate_us_rvol(
            today, hist_bars,
            skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
            lookback_days=rvol_cfg.get("lookback_days", 10),
        )

        # 6. Option chain fetch (shared by Gamma Wall + option recommendation)
        gamma_wall: GammaWallResult | None = None
        chain_df = pd.DataFrame()
        expiry_dates: list[str] = []
        target_expiry: str | None = None
        atm_iv = 0.0
        avg_iv = 0.0

        try:
            expiry_dates = await self._collector.get_option_expiration_dates(symbol)
            target_expiry = select_expiry(
                expiry_dates,
                dte_min=option_cfg.get("dte_min", 1),
                dte_preferred_max=option_cfg.get("dte_preferred_max", 7),
            )
            if target_expiry:
                try:
                    options = await asyncio.wait_for(
                        self._collector.get_option_chain(symbol, expiration=target_expiry),
                        timeout=10,
                    )
                    if options:
                        chain_df = option_quotes_to_df(options)
                except Exception:
                    logger.warning("Option chain fetch failed for %s, continuing without", symbol)

            if not chain_df.empty:
                # Gamma Wall (chain_df already has option_type/strike_price/open_interest)
                if cfg.get("gamma_wall", {}).get("enabled", True):
                    try:
                        gamma_wall = calculate_gamma_wall(chain_df, price)
                    except Exception:
                        logger.warning("Gamma wall calc failed for %s", symbol)

                atm_iv, avg_iv = self._extract_iv(chain_df, price)
        except Exception:
            logger.warning("Option data fetch failed for %s", symbol, exc_info=True)

        # 6b. Build OptionMarketSnapshot
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

        # 7. Filters
        filters = check_us_filters(
            rvol=rvol,
            prev_high=pdh,
            prev_low=pdl,
            current_high=snap["high_price"],
            current_low=snap["low_price"],
            calendar_path=filter_cfg.get("calendar_file", "config/us_calendar.yaml"),
            inside_day_rvol_threshold=filter_cfg.get("inside_day_rvol_threshold", 0.8),
            symbol=symbol,
        )

        # 8. Adaptive RVOL profile
        adaptive_cfg = regime_cfg.get("adaptive", {})
        rvol_profile = None
        if adaptive_cfg.get("enabled", True):
            rvol_profile = compute_rvol_profile(
                history_bars=hist_bars,
                today_rvol=rvol,
                skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
                gap_and_go_pctl=adaptive_cfg.get("gap_and_go_percentile", 85),
                trend_day_pctl=adaptive_cfg.get("trend_day_percentile", 60),
                fade_chop_pctl=adaptive_cfg.get("fade_chop_percentile", 30),
                fallback_gap_and_go=regime_cfg.get("gap_and_go_rvol", 1.5),
                fallback_trend_day=regime_cfg.get("trend_day_rvol", 1.2),
                fallback_fade_chop=regime_cfg.get("fade_chop_rvol", 1.0),
                min_sample_days=adaptive_cfg.get("min_sample_days", 5),
                min_trend_day_floor=adaptive_cfg.get("min_trend_day_floor", 1.0),
            )

        # 9. Regime classification
        regime = classify_us_regime(
            price=price,
            prev_close=prev_close,
            rvol=rvol,
            pmh=pmh,
            pml=pml,
            vp=vp,
            gamma_wall=gamma_wall,
            spy_regime=spy_regime,
            gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.5),
            trend_day_rvol=regime_cfg.get("trend_day_rvol", 1.2),
            fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 1.0),
            vp_trading_days=vp.trading_days,
            min_vp_trading_days=min_td,
            rvol_profile=rvol_profile,
            gap_significance_threshold=adaptive_cfg.get("gap_significance_threshold", 0.3),
            pm_source=pm_source,
            open_price=quote_snapshot.open_price,
            today_bars=today,
        )

        # 9a. P1-1: FADE_CHOP exec chain — independent from analysis chain
        exec_chain_df = chain_df  # default: same as analysis chain
        rr_option = option_cfg.get("range_reversal", {})
        if regime.regime == USRegimeType.FADE_CHOP and rr_option.get("dte_min"):
            rr_expiry = select_expiry(
                expiry_dates,
                dte_min=rr_option["dte_min"],
                dte_preferred_max=rr_option.get("dte_preferred_max", 7),
            )
            if rr_expiry and rr_expiry != target_expiry:
                try:
                    rr_options = await asyncio.wait_for(
                        self._collector.get_option_chain(symbol, expiration=rr_expiry),
                        timeout=10,
                    )
                    if rr_options:
                        exec_chain_df = option_quotes_to_df(rr_options)
                except Exception:
                    logger.debug("FADE_CHOP exec chain fetch failed for %s, using analysis chain", symbol)

        # 10. Option recommendation (exec chain for FADE_CHOP, analysis chain otherwise)
        chase_risk_cfg = self._cfg.get("chase_risk", {})
        option_rec: OptionRecommendation | None = None
        try:
            option_rec = recommend(
                regime=regime,
                vp=vp,
                filters=filters,
                chain_df=exec_chain_df if not exec_chain_df.empty else None,
                expiry_dates=expiry_dates,
                gamma_wall=gamma_wall,
                vwap=vwap,
                chase_risk_cfg=chase_risk_cfg,
                option_cfg=option_cfg,
                today_bars=today,
            )
        except Exception:
            logger.warning("Option recommendation failed for %s", symbol, exc_info=True)

        # 11. Build key levels
        key_levels = build_key_levels(vp, pdh, pdl, pmh, pml, vwap, gamma_wall, pm_source=pm_source)

        # 12. Strategy text (direction-aware)
        _dir = "bullish" if price > vp.vah or (vp.poc > 0 and price > vp.poc) else "bearish"
        if price < vp.val:
            _dir = "bearish"
        strategy_text = get_regime_strategy(regime.regime, _dir)

        name = self.watchlist.get_name(symbol)
        result = USPlaybookResult(
            symbol=symbol,
            name=name,
            regime=regime,
            key_levels=key_levels,
            volume_profile=vp,
            gamma_wall=gamma_wall,
            filters=filters,
            strategy_text=strategy_text,
            generated_at=datetime.now(ET),
            option_rec=option_rec,
            quote=quote_snapshot,
            option_market=option_market,
            market_tone=None,  # set by caller after pipeline
            avg_daily_range_pct=rvol_profile.avg_daily_range_pct if rvol_profile else 0.0,
        )
        self._last_playbooks[symbol] = result
        self._last_today_bars[symbol] = today
        return result

    @staticmethod
    def _extract_iv(chain_df: pd.DataFrame, price: float) -> tuple[float, float]:
        """Extract ATM implied volatility from option chain.

        Returns (atm_iv, avg_iv) where:
        - atm_iv = mean IV of 4 nearest strikes to current price
        - avg_iv = median IV of strikes within ATM ±3 strikes
        """
        iv_col = None
        for col in ("implied_volatility", "iv"):
            if col in chain_df.columns:
                iv_col = col
                break
        if chain_df.empty or iv_col is None:
            return 0.0, 0.0

        strike_col = "strike" if "strike" in chain_df.columns else "strike_price"
        if strike_col not in chain_df.columns:
            return 0.0, 0.0

        chain_copy = chain_df.copy()
        chain_copy["_dist"] = (chain_copy[strike_col] - price).abs()

        nearest_strikes = (
            chain_copy.drop_duplicates(subset=[strike_col])
            .nsmallest(7, "_dist")[strike_col]
            .tolist()
        )
        near_atm = chain_copy[chain_copy[strike_col].isin(nearest_strikes)]
        near_atm_iv = near_atm[iv_col].dropna()
        near_atm_iv = near_atm_iv[near_atm_iv > 0]
        if near_atm_iv.empty:
            all_iv = chain_df[iv_col].dropna()
            all_iv = all_iv[all_iv > 0]
            avg_iv = float(all_iv.median()) if not all_iv.empty else 0.0
        else:
            avg_iv = float(near_atm_iv.median())

        atm = chain_copy.nsmallest(4, "_dist")
        atm_iv_vals = atm[iv_col].dropna()
        atm_iv_vals = atm_iv_vals[atm_iv_vals > 0]
        if atm_iv_vals.empty:
            return 0.0, avg_iv

        return float(atm_iv_vals.mean()), avg_iv

    # ── SPY context ──

    async def _ensure_spy_context(self) -> USRegimeType | None:
        """SPY regime from full analysis pipeline. Cached with long TTL."""
        now = time.time()
        if self._spy_context[1] and now - self._spy_context[0] < self._SPY_CONTEXT_TTL:
            return self._spy_context[1].regime

        spy_result = await self._run_analysis_pipeline("SPY", spy_regime=None)
        if spy_result:
            self._spy_context = (now, spy_result.regime)
            return spy_result.regime.regime
        return self._spy_context[1].regime if self._spy_context[1] else None

    # ── On-demand playbook for a single symbol ──

    async def generate_playbook_for_symbol(self, symbol: str) -> PlaybookResponse:
        """Generate playbook for a symbol. Returns PlaybookResponse with HTML + chart."""
        # Get SPY context first (unless querying SPY itself)
        if symbol == "SPY":
            result = await self._run_analysis_pipeline(symbol, spy_regime=None)
            if result:
                # Update SPY context
                self._spy_context = (time.time(), result.regime)
        else:
            spy_regime = await self._ensure_spy_context()
            result = await self._run_analysis_pipeline(symbol, spy_regime=spy_regime)

        if not result:
            return PlaybookResponse(html=f"Failed to generate playbook for {symbol}")

        # Market Tone — compute after SPY pipeline so SPY bars are available
        tone = await self._ensure_market_tone()
        if tone and result.market_tone is None:
            result.market_tone = tone
            self._apply_tone_modifier(result.regime, tone)

        # Get SPY/QQQ results for market context display
        spy_result = self._last_playbooks.get("SPY")
        qqq_result = self._last_playbooks.get("QQQ")
        html_text = format_us_playbook_message(result, spy_result=spy_result, qqq_result=qqq_result)

        # Generate chart (best-effort — failure degrades to text-only)
        chart_bytes: bytes | None = None
        try:
            today_bars = self._last_today_bars.get(symbol)
            kl = result.key_levels
            key_levels_dict: dict[str, float] = {}
            for attr, label in [
                ("poc", "POC"), ("vah", "VAH"), ("val", "VAL"),
                ("vwap", "VWAP"), ("pdh", "PDH"), ("pdl", "PDL"),
                ("pmh", "PMH"), ("pml", "PML"),
                ("gamma_call_wall", "Gamma Call Wall"),
                ("gamma_put_wall", "Gamma Put Wall"),
            ]:
                v = getattr(kl, attr, 0.0)
                if v and v > 0:
                    key_levels_dict[label] = v

            display = f"{result.name} ({symbol})" if result.name != symbol else symbol
            chart_data = ChartData(
                symbol=display,
                today_bars=today_bars if today_bars is not None else pd.DataFrame(),
                volume_profile=result.volume_profile,
                vwap=kl.vwap,
                last_price=result.regime.price,
                prev_close=result.quote.prev_close if result.quote else 0.0,
                regime_label=f"{result.regime.regime.value.upper()} {result.regime.confidence:.0%}",
                key_levels=key_levels_dict,
                gamma_wall=result.gamma_wall,
            )
            buf = await generate_chart_async(chart_data)
            if buf is not None:
                chart_bytes = buf.getvalue()
        except Exception:
            logger.warning("Chart generation failed for %s", symbol, exc_info=True)

        return PlaybookResponse(html=html_text, chart=chart_bytes)

    # ── Auto-scan ──

    async def run_auto_scan(self, send_fn) -> None:
        """Scan watchlist for strong signals and push alerts."""
        scan_cfg = self._cfg.get("auto_scan", {})
        if not scan_cfg.get("enabled", False):
            return

        now_et = datetime.now(ET)
        in_window, session = self._get_scan_window(scan_cfg, now_et)
        if not in_window:
            return

        self._reset_scan_history_if_new_day()

        symbols = self.watchlist.symbols()
        context_symbols = self._cfg.get("regime", {}).get("market_context_symbols", ["SPY", "QQQ"])
        cooldown_mins = scan_cfg.get("cooldown", {}).get("same_signal_minutes", 30)

        # Phase 1: Scan SPY first for market context
        for ctx_sym in context_symbols:
            if ctx_sym in symbols:
                try:
                    result = await self._run_analysis_pipeline(ctx_sym, spy_regime=None)
                    if result and ctx_sym == "SPY":
                        self._spy_context = (time.time(), result.regime)
                except Exception:
                    logger.warning("Context symbol %s scan failed", ctx_sym, exc_info=True)

        spy_regime_type = self._spy_context[1].regime if self._spy_context[1] else None

        # Phase 1b: Market Tone gating
        tone = await self._ensure_market_tone()
        tone_cfg = self._cfg.get("market_tone", {}).get("grade", {})
        scan_gates = tone_cfg.get("auto_scan_gates", {})
        gate: str | None = None

        if tone:
            gate = scan_gates.get(tone.grade)
            if gate == "skip":
                logger.info("Auto-scan skipped: market tone grade=%s", tone.grade)
                return

            # Event day time restrictions
            event_day_cfg = tone_cfg.get("event_day", {})
            if tone.macro_signal == "data_reaction":
                data_start_str = event_day_cfg.get("data_scan_start_time", "10:00")
                data_h, data_m = map(int, data_start_str.split(":"))
                if now_et.hour < data_h or (now_et.hour == data_h and now_et.minute < data_m):
                    logger.info("Auto-scan deferred: data_reaction day before %s", data_start_str)
                    return

            # FOMC day: before unlock time → range_reversal_only
            if tone.macro_signal == "range_then_trend":
                unlock_str = event_day_cfg.get("fomc_trend_unlock_time", "14:00")
                unlock_h, unlock_m = map(int, unlock_str.split(":"))
                if now_et.hour < unlock_h or (now_et.hour == unlock_h and now_et.minute < unlock_m):
                    gate = "range_reversal_only"  # Override for FOMC pre-2PM

        # Phase 2: Batch snapshot pre-fetch for non-context symbols
        non_ctx_symbols = [s for s in symbols if s not in context_symbols]
        pre_fetched_snaps: dict[str, dict] = {}
        if non_ctx_symbols:
            try:
                pre_fetched_snaps = await self._collector.get_snapshots(non_ctx_symbols)
            except Exception:
                logger.warning("Batch snapshot fetch failed, will fallback to individual calls")

        # Phase 2: Scan remaining symbols
        for symbol in symbols:
            if symbol in context_symbols:
                continue
            try:
                # P0-2: frequency precheck — skip symbols that definitely hit limits
                if not self._quick_frequency_precheck(symbol, session, scan_cfg):
                    continue

                l1_data = await self._l1_screen(
                    symbol, session, scan_cfg, spy_regime_type,
                    pre_fetched_snap=pre_fetched_snaps.get(symbol),
                )

                # P1-4: Regime transition detection for previously scanned symbols
                if l1_data is None and symbol in self._last_scan_regimes:
                    l1_data = await self._check_regime_transition(
                        symbol, session, scan_cfg, spy_regime_type,
                    )

                if l1_data is None:
                    continue

                # Tone gate: C → range_reversal_only, D already skipped above
                if tone and gate == "range_reversal_only":
                    sig_type = l1_data["signal_type"]
                    if not sig_type.startswith("RANGE_REVERSAL"):
                        logger.debug(
                            "Tone gate rejects %s %s (grade=%s, only RR allowed)",
                            symbol, sig_type, tone.grade,
                        )
                        continue

                logger.info(
                    "L1 pass %s: type=%s dir=%s price=%.2f",
                    symbol, l1_data["signal_type"], l1_data["direction"], l1_data["price"],
                )

                # L2: full pipeline verification (structure + execution decoupled)
                l2_result = await self._l2_verify(symbol, l1_data, spy_regime_type)
                if l2_result is None:
                    continue

                signal, playbook_html, option_rec, l2_filters, l2_playbook_result = l2_result

                # Track regime for transition detection
                self._last_scan_regimes[symbol] = signal.regime

                # Fade direction cooldown — prevent contradictory FADE_CHOP pushes
                if (
                    signal.regime.regime == USRegimeType.FADE_CHOP
                    and signal.signal_type.startswith("RANGE_REVERSAL")
                ):
                    cooldown_min = self._cfg.get("chase_risk", {}).get(
                        "fade_direction_cooldown_minutes", 15,
                    )
                    last_fade = self._last_fade_directions.get(symbol)
                    if (
                        last_fade
                        and last_fade[0] != signal.direction
                        and signal.timestamp - last_fade[1] < cooldown_min * 60
                    ):
                        logger.info(
                            "Fade direction cooldown: %s changed %s→%s within %dmin, skip",
                            symbol, last_fade[0], signal.direction, cooldown_min,
                        )
                        continue
                    self._last_fade_directions[symbol] = (
                        signal.direction, signal.timestamp,
                    )

                # Frequency control
                allowed, override_reason = self._check_frequency(
                    symbol, signal, session, scan_cfg,
                )
                if not allowed:
                    continue

                # Generate chart (best-effort)
                chart_bytes: bytes | None = None
                try:
                    today_bars = self._last_today_bars.get(symbol)
                    kl = l2_playbook_result.key_levels
                    key_levels_dict: dict[str, float] = {}
                    for attr, label in [
                        ("poc", "POC"), ("vah", "VAH"), ("val", "VAL"),
                        ("vwap", "VWAP"), ("pdh", "PDH"), ("pdl", "PDL"),
                        ("pmh", "PMH"), ("pml", "PML"),
                        ("gamma_call_wall", "Gamma Call Wall"),
                        ("gamma_put_wall", "Gamma Put Wall"),
                    ]:
                        v = getattr(kl, attr, 0.0)
                        if v and v > 0:
                            key_levels_dict[label] = v

                    display = f"{l2_playbook_result.name} ({symbol})" if l2_playbook_result.name != symbol else symbol
                    chart_data = ChartData(
                        symbol=display,
                        today_bars=today_bars if today_bars is not None else pd.DataFrame(),
                        volume_profile=l2_playbook_result.volume_profile,
                        vwap=kl.vwap,
                        last_price=l2_playbook_result.regime.price,
                        prev_close=l2_playbook_result.quote.prev_close if l2_playbook_result.quote else 0.0,
                        regime_label=f"{l2_playbook_result.regime.regime.value.upper()} {l2_playbook_result.regime.confidence:.0%}",
                        key_levels=key_levels_dict,
                        gamma_wall=l2_playbook_result.gamma_wall,
                    )
                    buf = await generate_chart_async(chart_data)
                    if buf is not None:
                        chart_bytes = buf.getvalue()
                except Exception:
                    logger.warning("Auto-scan chart generation failed for %s", symbol, exc_info=True)

                # Format and send
                header = self._format_scan_header(
                    signal, l2_filters.risk_level, option_rec, override_reason, cooldown_mins,
                )
                await send_fn(header + "\n" + playbook_html, photo=chart_bytes)

                self._record_alert(symbol, signal, session)
                logger.info(
                    "Auto-scan alert sent: %s %s %s conf=%.2f%s",
                    symbol, signal.signal_type, signal.direction,
                    signal.regime.confidence,
                    f" (override: {override_reason})" if override_reason else "",
                )

            except Exception:
                logger.warning("Auto-scan error for %s", symbol, exc_info=True)

    # ── Auto-scan: window check ──

    @staticmethod
    def _get_scan_window(
        scan_cfg: dict,
        now_et: datetime | None = None,
    ) -> tuple[bool, str]:
        """Check if current time is within a scan window."""
        if now_et is None:
            now_et = datetime.now(ET)

        if now_et.weekday() > 4:
            return False, ""

        t = now_et.hour * 60 + now_et.minute

        for session_name, key in [("morning", "morning_window"), ("afternoon", "afternoon_window")]:
            window = scan_cfg.get(key)
            if not window or len(window) < 2:
                continue
            s_h, s_m = map(int, window[0].split(":"))
            e_h, e_m = map(int, window[1].split(":"))
            if s_h * 60 + s_m <= t <= e_h * 60 + e_m:
                return True, session_name

        return False, ""

    # ── Auto-scan: frequency precheck ──

    def _quick_frequency_precheck(
        self,
        symbol: str,
        session: str,
        scan_cfg: dict,
    ) -> bool:
        """Conservative pre-filter: skip symbols that definitely hit frequency limits.

        Returns False only when daily/session max is full AND no override is possible
        (i.e., last alert was already BREAKOUT, so regime_upgrade cannot apply).
        """
        cooldown_cfg = scan_cfg.get("cooldown", {})
        override_cfg = scan_cfg.get("override", {})
        max_per_session = cooldown_cfg.get("max_per_session", 2)
        max_per_day = cooldown_cfg.get("max_per_day", 3)

        records = self._scan_history.get(symbol, [])
        if not records:
            return True  # no history → always proceed

        # If regime_upgrade override is enabled and last alert was RANGE, upgrade is possible
        can_upgrade = override_cfg.get("regime_upgrade", True) and records[-1].signal_type.startswith("RANGE")

        # Daily max
        if len(records) >= max_per_day and not can_upgrade:
            logger.info("Precheck skip %s: daily max %d reached", symbol, max_per_day)
            return False

        # Session max
        session_records = [r for r in records if r.session == session]
        if len(session_records) >= max_per_session and not can_upgrade:
            logger.info("Precheck skip %s: session max %d reached", symbol, max_per_session)
            return False

        return True

    # ── Auto-scan: L1 lightweight screen ──

    async def _l1_screen(
        self,
        symbol: str,
        session: str,
        scan_cfg: dict,
        spy_regime: USRegimeType | None = None,
        pre_fetched_snap: dict | None = None,
    ) -> dict | None:
        """Lightweight L1 screen. Returns screening data if passed, None otherwise."""
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        rvol_cfg = cfg.get("rvol", {})
        regime_cfg = cfg.get("regime", {})
        breakout_cfg = scan_cfg.get("breakout", {})

        # Get bars (cached)
        vp_target = vp_cfg.get("lookback_trading_days") or vp_cfg.get("lookback_days", 5)
        rvol_td = rvol_cfg.get("lookback_days", 10)
        fetch_days = calc_fetch_calendar_days(vp_target, rvol_td)
        hist_bars, today, cached_entry = await self._get_bars_split(symbol, fetch_days, vp_target)

        # VP
        vp_history = get_history_bars(hist_bars, max_trading_days=vp_target) if not hist_bars.empty else hist_bars
        vp = compute_volume_profile(
            vp_history,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
            recency_decay=vp_cfg.get("recency_decay", 0.15),
        )
        if vp.vah <= 0 or vp.val <= 0:
            logger.info("L1 skip %s: VP invalid (VAH=%.2f, VAL=%.2f)", symbol, vp.vah, vp.val)
            return None

        # Snapshot for price (use pre-fetched if available)
        snap = pre_fetched_snap if pre_fetched_snap else await self._collector.get_snapshot(symbol)
        price = snap["last_price"]
        prev_close = snap["prev_close_price"] or 0.0
        if price <= 0:
            logger.info("L1 skip %s: price=0", symbol)
            return None

        # RVOL
        rvol = calculate_us_rvol(
            today, hist_bars,
            skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
            lookback_days=rvol_cfg.get("lookback_days", 10),
        )

        # Lightweight regime (no option chain / gamma wall)
        adaptive_cfg = regime_cfg.get("adaptive", {})
        rvol_profile = None
        if adaptive_cfg.get("enabled", True):
            rvol_profile = compute_rvol_profile(
                history_bars=hist_bars,
                today_rvol=rvol,
                skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
                gap_and_go_pctl=adaptive_cfg.get("gap_and_go_percentile", 85),
                trend_day_pctl=adaptive_cfg.get("trend_day_percentile", 60),
                fade_chop_pctl=adaptive_cfg.get("fade_chop_percentile", 30),
                fallback_gap_and_go=regime_cfg.get("gap_and_go_rvol", 1.5),
                fallback_trend_day=regime_cfg.get("trend_day_rvol", 1.2),
                fallback_fade_chop=regime_cfg.get("fade_chop_rvol", 1.0),
                min_sample_days=adaptive_cfg.get("min_sample_days", 5),
                min_trend_day_floor=adaptive_cfg.get("min_trend_day_floor", 1.0),
            )

        regime = classify_us_regime(
            price=price,
            prev_close=prev_close,
            rvol=rvol,
            pmh=cached_entry.pmh,
            pml=cached_entry.pml,
            vp=vp,
            spy_regime=spy_regime,
            gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.5),
            trend_day_rvol=regime_cfg.get("trend_day_rvol", 1.2),
            fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 1.0),
            vp_trading_days=vp.trading_days,
            min_vp_trading_days=vp_cfg.get("min_trading_days", 3),
            rvol_profile=rvol_profile,
            gap_significance_threshold=adaptive_cfg.get("gap_significance_threshold", 0.3),
            pm_source=cached_entry.pm_source,
            open_price=snap.get("open_price", 0.0),
            today_bars=today,
        )

        # Build L1 result for reuse in L2
        l1_result = _L1Result(
            hist_bars=hist_bars, today_bars=today, cached_entry=cached_entry,
            vp=vp, snap=snap, price=price, prev_close=prev_close,
            rvol=rvol, rvol_profile=rvol_profile, regime=regime,
        )

        # BREAKOUT check
        bo_min_conf = breakout_cfg.get("min_confidence", 0.70)
        bo_min_rvol = breakout_cfg.get("min_rvol", 1.30)
        bo_min_mag = breakout_cfg.get("min_magnitude_pct", 0.20)

        if (
            regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY)
            and regime.confidence >= bo_min_conf
            and rvol >= bo_min_rvol
        ):
            magnitude = 0.0
            if price > vp.vah and vp.vah > 0:
                magnitude = (price - vp.vah) / price * 100
            elif price < vp.val and vp.val > 0:
                magnitude = (vp.val - price) / price * 100

            if magnitude >= bo_min_mag:
                # P1-2: Bar-close confirmation — reject wick-only breakouts
                if not today.empty:
                    last_close = float(today.iloc[-1]["Close"])
                    if price > vp.vah and last_close < vp.vah:
                        logger.info("L1 skip %s: wick-only above VAH (close=%.2f < VAH=%.2f)", symbol, last_close, vp.vah)
                        return None  # wick above VAH, not confirmed
                    if price < vp.val and last_close > vp.val:
                        logger.info("L1 skip %s: wick-only below VAL (close=%.2f > VAL=%.2f)", symbol, last_close, vp.val)
                        return None  # wick below VAL, not confirmed

                # P1-1: Volume surge confirmation
                vol_surge_threshold = breakout_cfg.get("volume_surge_threshold", 2.0)
                vol_surge_bars = breakout_cfg.get("volume_surge_bars", 5)
                if not today.empty and len(today) >= 2:
                    # Skip opening rotation bars to avoid inflated baseline
                    from datetime import time as dt_time
                    _skip = rvol_cfg.get("skip_open_minutes", 3)
                    _cutoff = dt_time(9, 30 + _skip)
                    _filtered = today[today.index.time >= _cutoff] if hasattr(today.index, 'time') else today
                    avg_bar_vol = float(_filtered["Volume"].mean()) if not _filtered.empty else float(today["Volume"].mean())
                    recent = today.iloc[-vol_surge_bars:]
                    if avg_bar_vol > 0:
                        has_surge = (recent["Volume"] >= avg_bar_vol * vol_surge_threshold).any()
                        if not has_surge:
                            logger.info("L1 skip %s: no volume surge (avg=%.0f, threshold=%.1fx)", symbol, avg_bar_vol, vol_surge_threshold)
                            return None  # no volume surge to confirm breakout

                direction = "bullish" if price > vp.vah else "bearish"
                signal_type = regime_to_signal_type(regime.regime, direction) or "BREAKOUT_LONG"
                triggers = []
                boundary = "VAH" if price > vp.vah else "VAL"
                triggers.append(f"突破 {boundary} {magnitude:.2f}%")
                triggers.append(f"RVOL {rvol:.2f} | Conf {regime.confidence:.0%}")

                return {
                    "signal_type": signal_type,
                    "direction": direction,
                    "regime": regime,
                    "price": price,
                    "trigger_reasons": triggers,
                    "_l1_result": l1_result,
                }

        # RANGE_REVERSAL check (disabled by default in v1)
        range_cfg = scan_cfg.get("range_reversal", {})
        if range_cfg.get("enabled", False):
            rng_min_conf = range_cfg.get("min_confidence", 0.70)
            rng_rvol_min = range_cfg.get("rvol_min", 0.50)
            rng_rvol_max = range_cfg.get("rvol_max", 1.00)
            rng_prox = range_cfg.get("va_proximity_pct", 0.30)

            if (
                regime.regime == USRegimeType.FADE_CHOP
                and regime.confidence >= rng_min_conf
                and rng_rvol_min <= rvol <= rng_rvol_max
            ):
                dist_vah = abs(price - vp.vah) / price * 100 if price > 0 else 999
                dist_val = abs(price - vp.val) / price * 100 if price > 0 else 999
                near_boundary = min(dist_vah, dist_val)

                if near_boundary <= rng_prox:
                    direction = "bearish" if dist_vah < dist_val else "bullish"

                    # P2-1: VA width minimum check
                    chase_cfg = self._cfg.get("chase_risk", {})
                    va_width_pct = (vp.vah - vp.val) / price * 100 if price > 0 else 0
                    if va_width_pct < chase_cfg.get("min_va_width_pct", 0.80):
                        logger.info("L1 skip %s: VA too narrow (%.2f%%)", symbol, va_width_pct)
                        return None  # VA too narrow for mean reversion

                    # P0-1: Local trend filter
                    if not today.empty:
                        trend = compute_local_trend(
                            today,
                            lookback=chase_cfg.get("local_trend_lookback", 30),
                            threshold=chase_cfg.get("local_trend_threshold", 0.02),
                        )
                        if (direction == "bullish" and trend == -1) or \
                           (direction == "bearish" and trend == 1):
                            logger.info("L1 skip %s: trend opposes %s RR direction", symbol, direction)
                            return None  # trend opposes mean-reversion direction

                    # RSI confirmation (disabled by default)
                    rsi_cfg = range_cfg.get("rsi", {})
                    if rsi_cfg.get("enabled", False) and not today.empty:
                        rsi = calculate_rsi(today, period=rsi_cfg.get("period", 14))
                        overbought = rsi_cfg.get("overbought", 70)
                        oversold = rsi_cfg.get("oversold", 30)
                        if direction == "bearish" and rsi <= overbought:
                            logger.info("L1 skip %s: bearish RR but RSI=%.1f <= %d", symbol, rsi, overbought)
                            return None
                        if direction == "bullish" and rsi >= oversold:
                            logger.info("L1 skip %s: bullish RR but RSI=%.1f >= %d", symbol, rsi, oversold)
                            return None

                    signal_type = regime_to_signal_type(USRegimeType.FADE_CHOP, direction)
                    boundary = "VAH" if dist_vah < dist_val else "VAL"
                    triggers = [f"接近 {boundary} (距离 {near_boundary:.2f}%)"]
                    return {
                        "signal_type": signal_type,
                        "direction": direction,
                        "regime": regime,
                        "price": price,
                        "trigger_reasons": triggers,
                        "_l1_result": l1_result,
                    }

        return None

    # ── P1-4: Regime transition detection ──

    async def _check_regime_transition(
        self,
        symbol: str,
        session: str,
        scan_cfg: dict,
        spy_regime: USRegimeType | None = None,
    ) -> dict | None:
        """Check if a previously scanned symbol has undergone regime transition.

        Only triggers for meaningful upgrades (UNCLEAR/FADE_CHOP → TREND_DAY/GAP_AND_GO).
        Uses existing `regime_upgrade` override to bypass cooldown.
        """
        original = self._last_scan_regimes.get(symbol)
        if not original:
            return None

        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        rvol_cfg = cfg.get("rvol", {})
        regime_cfg = cfg.get("regime", {})

        # Get current bars + VP
        vp_target = vp_cfg.get("lookback_trading_days") or vp_cfg.get("lookback_days", 5)
        rvol_td = rvol_cfg.get("lookback_days", 10)
        fetch_days = calc_fetch_calendar_days(vp_target, rvol_td)
        hist_bars, today, cached_entry = await self._get_bars_split(symbol, fetch_days, vp_target)

        vp_history = get_history_bars(hist_bars, max_trading_days=vp_target) if not hist_bars.empty else hist_bars
        vp = compute_volume_profile(
            vp_history,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
            recency_decay=vp_cfg.get("recency_decay", 0.15),
        )
        if vp.vah <= 0 or vp.val <= 0:
            return None

        snap = await self._collector.get_snapshot(symbol)
        price = snap["last_price"]
        prev_close = snap["prev_close_price"] or 0.0
        if price <= 0:
            return None

        rvol = calculate_us_rvol(
            today, hist_bars,
            skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
            lookback_days=rvol_cfg.get("lookback_days", 10),
        )

        # Adaptive RVOL profile
        adaptive_cfg = regime_cfg.get("adaptive", {})
        rvol_profile = None
        if adaptive_cfg.get("enabled", True):
            rvol_profile = compute_rvol_profile(
                history_bars=hist_bars,
                today_rvol=rvol,
                skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
                gap_and_go_pctl=adaptive_cfg.get("gap_and_go_percentile", 85),
                trend_day_pctl=adaptive_cfg.get("trend_day_percentile", 60),
                fade_chop_pctl=adaptive_cfg.get("fade_chop_percentile", 30),
                fallback_gap_and_go=regime_cfg.get("gap_and_go_rvol", 1.5),
                fallback_trend_day=regime_cfg.get("trend_day_rvol", 1.2),
                fallback_fade_chop=regime_cfg.get("fade_chop_rvol", 1.0),
                min_sample_days=adaptive_cfg.get("min_sample_days", 5),
                min_trend_day_floor=adaptive_cfg.get("min_trend_day_floor", 1.0),
            )

        transitioned, new_regime = detect_regime_transition(
            original=original,
            current_rvol=rvol,
            current_price=price,
            vp=vp,
            spy_regime=spy_regime,
            prev_close=prev_close,
            pmh=cached_entry.pmh,
            pml=cached_entry.pml,
            gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.5),
            trend_day_rvol=regime_cfg.get("trend_day_rvol", 1.2),
            fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 1.0),
            rvol_profile=rvol_profile,
            gap_significance_threshold=adaptive_cfg.get("gap_significance_threshold", 0.3),
            pm_source=cached_entry.pm_source,
            open_price=snap.get("open_price", 0.0),
        )

        if not transitioned or new_regime is None:
            return None

        logger.info(
            "Regime transition detected for %s: %s → %s (conf %.2f)",
            symbol, original.regime.value, new_regime.regime.value, new_regime.confidence,
        )

        direction = "bullish" if price > vp.vah else "bearish"
        signal_type = regime_to_signal_type(new_regime.regime, direction) or "BREAKOUT_LONG"
        triggers = [
            f"Regime 转换: {original.regime.value} → {new_regime.regime.value}",
            f"RVOL {rvol:.2f} | Conf {new_regime.confidence:.0%}",
        ]

        # Update stored regime
        self._last_scan_regimes[symbol] = new_regime

        l1_result = _L1Result(
            hist_bars=hist_bars, today_bars=today, cached_entry=cached_entry,
            vp=vp, snap=snap, price=price, prev_close=prev_close,
            rvol=rvol, rvol_profile=rvol_profile, regime=new_regime,
        )

        return {
            "signal_type": signal_type,
            "direction": direction,
            "regime": new_regime,
            "price": price,
            "trigger_reasons": triggers,
            "_l1_result": l1_result,
        }

    # ── Auto-scan: L2 incremental pipeline (reuses L1 data) ──

    async def _run_l2_incremental(
        self,
        symbol: str,
        l1: _L1Result,
        spy_regime: USRegimeType | None = None,
    ) -> USPlaybookResult | None:
        """L2 pipeline reusing L1 computed data (bars, VP, snap, RVOL, rvol_profile).

        Only fetches: option chain → gamma wall → filters → regime reclassify → option rec.
        """
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        regime_cfg = cfg.get("regime", {})
        filter_cfg = cfg.get("filters", {})
        option_cfg = cfg.get("option_recommend", {})

        # Reuse from L1
        hist_bars = l1.hist_bars
        today = l1.today_bars
        cached_entry = l1.cached_entry
        vp = l1.vp
        snap = l1.snap
        price = l1.price
        prev_close = l1.prev_close
        rvol = l1.rvol
        rvol_profile = l1.rvol_profile

        # Build QuoteSnapshot from snap
        quote_snapshot = QuoteSnapshot(
            symbol=symbol,
            last_price=price,
            open_price=snap.get("open_price", 0.0),
            high_price=snap.get("high_price", 0.0),
            low_price=snap.get("low_price", 0.0),
            prev_close=prev_close,
            volume=snap.get("volume", 0),
            turnover=snap.get("turnover", 0.0),
            bid_price=snap.get("bid_price", 0.0),
            ask_price=snap.get("ask_price", 0.0),
            turnover_rate=snap.get("turnover_rate", 0.0),
            amplitude=snap.get("amplitude", 0.0),
        )

        # VWAP from L1 today bars
        vwap = calculate_vwap(today)

        # Option chain fetch (L2 only)
        gamma_wall: GammaWallResult | None = None
        chain_df = pd.DataFrame()
        expiry_dates: list[str] = []
        target_expiry: str | None = None
        atm_iv = 0.0
        avg_iv = 0.0

        try:
            expiry_dates = await self._collector.get_option_expiration_dates(symbol)
            target_expiry = select_expiry(
                expiry_dates,
                dte_min=option_cfg.get("dte_min", 1),
                dte_preferred_max=option_cfg.get("dte_preferred_max", 7),
            )
            if target_expiry:
                try:
                    options = await asyncio.wait_for(
                        self._collector.get_option_chain(symbol, expiration=target_expiry),
                        timeout=10,
                    )
                    if options:
                        chain_df = option_quotes_to_df(options)
                except Exception:
                    logger.warning("L2 incremental: option chain fetch failed for %s", symbol)

            if not chain_df.empty:
                if cfg.get("gamma_wall", {}).get("enabled", True):
                    try:
                        gamma_wall = calculate_gamma_wall(chain_df, price)
                    except Exception:
                        logger.warning("L2 incremental: gamma wall calc failed for %s", symbol)
                atm_iv, avg_iv = self._extract_iv(chain_df, price)
        except Exception:
            logger.warning("L2 incremental: option data fetch failed for %s", symbol, exc_info=True)

        # Option market snapshot
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

        # Filters
        filters = check_us_filters(
            rvol=rvol,
            prev_high=cached_entry.pdh,
            prev_low=cached_entry.pdl,
            current_high=snap["high_price"],
            current_low=snap["low_price"],
            calendar_path=filter_cfg.get("calendar_file", "config/us_calendar.yaml"),
            inside_day_rvol_threshold=filter_cfg.get("inside_day_rvol_threshold", 0.8),
            symbol=symbol,
        )

        # Re-classify regime WITH gamma_wall (L1 classified without it)
        adaptive_cfg = regime_cfg.get("adaptive", {})
        min_td = vp_cfg.get("min_trading_days", 3)
        regime = classify_us_regime(
            price=price,
            prev_close=prev_close,
            rvol=rvol,
            pmh=cached_entry.pmh,
            pml=cached_entry.pml,
            vp=vp,
            gamma_wall=gamma_wall,
            spy_regime=spy_regime,
            gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.5),
            trend_day_rvol=regime_cfg.get("trend_day_rvol", 1.2),
            fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 1.0),
            vp_trading_days=vp.trading_days,
            min_vp_trading_days=min_td,
            rvol_profile=rvol_profile,
            gap_significance_threshold=adaptive_cfg.get("gap_significance_threshold", 0.3),
            pm_source=cached_entry.pm_source,
            open_price=quote_snapshot.open_price,
            today_bars=today,
        )

        # FADE_CHOP exec chain
        exec_chain_df = chain_df
        rr_option = option_cfg.get("range_reversal", {})
        if regime.regime == USRegimeType.FADE_CHOP and rr_option.get("dte_min"):
            rr_expiry = select_expiry(
                expiry_dates,
                dte_min=rr_option["dte_min"],
                dte_preferred_max=rr_option.get("dte_preferred_max", 7),
            )
            if rr_expiry and rr_expiry != target_expiry:
                try:
                    rr_options = await asyncio.wait_for(
                        self._collector.get_option_chain(symbol, expiration=rr_expiry),
                        timeout=10,
                    )
                    if rr_options:
                        exec_chain_df = option_quotes_to_df(rr_options)
                except Exception:
                    logger.debug("L2 incremental: FADE_CHOP exec chain failed for %s", symbol)

        # Option recommendation
        chase_risk_cfg = self._cfg.get("chase_risk", {})
        option_rec: OptionRecommendation | None = None
        try:
            option_rec = recommend(
                regime=regime,
                vp=vp,
                filters=filters,
                chain_df=exec_chain_df if not exec_chain_df.empty else None,
                expiry_dates=expiry_dates,
                gamma_wall=gamma_wall,
                vwap=vwap,
                chase_risk_cfg=chase_risk_cfg,
                option_cfg=option_cfg,
                today_bars=today,
            )
        except Exception:
            logger.warning("L2 incremental: option rec failed for %s", symbol, exc_info=True)

        # Build key levels
        key_levels = build_key_levels(
            vp, cached_entry.pdh, cached_entry.pdl,
            cached_entry.pmh, cached_entry.pml,
            vwap, gamma_wall, pm_source=cached_entry.pm_source,
        )

        # Strategy text
        _dir = "bullish" if price > vp.vah or (vp.poc > 0 and price > vp.poc) else "bearish"
        if price < vp.val:
            _dir = "bearish"
        strategy_text = get_regime_strategy(regime.regime, _dir)

        name = self.watchlist.get_name(symbol)
        result = USPlaybookResult(
            symbol=symbol,
            name=name,
            regime=regime,
            key_levels=key_levels,
            volume_profile=vp,
            gamma_wall=gamma_wall,
            filters=filters,
            strategy_text=strategy_text,
            generated_at=datetime.now(ET),
            option_rec=option_rec,
            quote=quote_snapshot,
            option_market=option_market,
            market_tone=None,
            avg_daily_range_pct=rvol_profile.avg_daily_range_pct if rvol_profile and hasattr(rvol_profile, 'avg_daily_range_pct') else 0.0,
        )
        self._last_playbooks[symbol] = result
        self._last_today_bars[symbol] = today
        return result

    # ── Auto-scan: L2 verification (structure + execution decoupled) ──

    async def _l2_verify(
        self,
        symbol: str,
        l1_data: dict,
        spy_regime: USRegimeType | None = None,
    ) -> tuple[USScanSignal, str, OptionRecommendation | None, FilterResult, USPlaybookResult] | None:
        """Full pipeline verification. Structure verification gates the push; execution status is informational."""
        l1 = l1_data.get("_l1_result")
        if l1:
            result = await self._run_l2_incremental(symbol, l1, spy_regime)
        else:
            result = await self._run_analysis_pipeline(symbol, spy_regime=spy_regime)  # fallback
        if not result:
            return None

        # Apply market tone confidence modifier (matching on-demand behavior)
        tone = await self._ensure_market_tone()
        if tone:
            if result.market_tone is None:
                result.market_tone = tone
            self._apply_tone_modifier(result.regime, tone)

        # Structure verification (must all pass to push)
        if not result.filters.tradeable:
            logger.info("L2 reject %s: not tradeable", symbol)
            return None
        if result.filters.risk_level == "high":
            logger.info("L2 reject %s: risk_level=high", symbol)
            return None

        signal_type = l1_data["signal_type"]
        direction = l1_data["direction"]

        # Regime consistency check
        expected_regimes = {
            "BREAKOUT": (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY),
            "RANGE_REVERSAL": (USRegimeType.FADE_CHOP,),
        }
        signal_prefix = signal_type.rsplit("_", 1)[0] if "_" in signal_type else signal_type
        # Handle BREAKOUT_LONG → BREAKOUT, RANGE_REVERSAL_LONG → RANGE_REVERSAL
        if signal_prefix.endswith("_LONG") or signal_prefix.endswith("_SHORT"):
            signal_prefix = signal_type.rsplit("_", 1)[0]
        # Map BREAKOUT_LONG/SHORT to BREAKOUT category
        for prefix, regimes in expected_regimes.items():
            if signal_type.startswith(prefix):
                if result.regime.regime not in regimes:
                    logger.info("L2 reject %s: regime mismatch %s not in %s", symbol, result.regime.regime, regimes)
                    return None
                break

        # SPY context consistency
        spy_ctx = self._spy_context[1]
        if spy_ctx and direction == "bullish" and spy_ctx.regime == USRegimeType.FADE_CHOP:
            if result.regime.confidence < 0.75:
                logger.info("L2 reject %s: SPY FADE_CHOP context, conf=%.2f < 0.75", symbol, result.regime.confidence)
                return None

        # P0-1 / P2-1: structural veto from recommend() (trend / VA width etc.)
        if result.option_rec and result.option_rec.structural_veto:
            logger.info("L2 reject %s: structural veto — %s", symbol, result.option_rec.risk_note)
            return None

        signal = USScanSignal(
            signal_type=signal_type,
            direction=direction,
            symbol=symbol,
            regime=result.regime,
            price=result.regime.price,
            trigger_reasons=l1_data["trigger_reasons"],
            timestamp=time.time(),
        )

        # Format playbook HTML
        spy_result = self._last_playbooks.get("SPY")
        qqq_result = self._last_playbooks.get("QQQ")
        playbook_html = format_us_playbook_message(result, spy_result=spy_result, qqq_result=qqq_result)

        return signal, playbook_html, result.option_rec, result.filters, result

    # ── Auto-scan: frequency control ──

    def _reset_scan_history_if_new_day(self) -> None:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if self._scan_history_date != today:
            self._scan_history.clear()
            self._last_scan_regimes.clear()
            self._last_fade_directions.clear()
            self._scan_history_date = today

    def _check_frequency(
        self,
        symbol: str,
        signal: USScanSignal,
        session: str,
        scan_cfg: dict,
    ) -> tuple[bool, str | None]:
        """3-layer frequency control with override exceptions.

        Supports per-type limits (cooldown.per_type) as an additional layer
        before global limits. Global max_per_session/max_per_day are hard ceilings.
        """
        cooldown_cfg = scan_cfg.get("cooldown", {})
        override_cfg = scan_cfg.get("override", {})
        same_signal_mins = cooldown_cfg.get("same_signal_minutes", 30)
        max_per_session = cooldown_cfg.get("max_per_session", 2)
        max_per_day = cooldown_cfg.get("max_per_day", 3)

        records = self._scan_history.get(symbol, [])
        now = signal.timestamp

        # Layer 3: global max per day (hard ceiling)
        if len(records) >= max_per_day:
            if override_cfg.get("regime_upgrade", True):
                last = records[-1]
                if last.signal_type.startswith("RANGE") and signal.signal_type.startswith("BREAKOUT"):
                    return True, "Regime 从 RANGE 升级为 BREAKOUT"
            return False, None

        # Layer 2b: per-type limits (if configured)
        per_type_cfg = cooldown_cfg.get("per_type", {})
        signal_category = "BREAKOUT" if signal.signal_type.startswith("BREAKOUT") else "RANGE_REVERSAL"
        type_limits = per_type_cfg.get(signal_category, {})
        if type_limits:
            type_max_session = type_limits.get("max_per_session", max_per_session)
            type_max_day = type_limits.get("max_per_day", max_per_day)
            type_records = [r for r in records if r.signal_type.startswith(signal_category)]

            if len(type_records) >= type_max_day:
                logger.info("Frequency block %s: %s daily max %d reached", symbol, signal_category, type_max_day)
                return False, None

            type_session_records = [r for r in type_records if r.session == session]
            if len(type_session_records) >= type_max_session:
                logger.info("Frequency block %s: %s session max %d reached", symbol, signal_category, type_max_session)
                return False, None

        # Layer 2: global max per session
        session_records = [r for r in records if r.session == session]
        if len(session_records) >= max_per_session:
            if override_cfg.get("regime_upgrade", True):
                last = session_records[-1]
                if last.signal_type.startswith("RANGE") and signal.signal_type.startswith("BREAKOUT"):
                    return True, "Regime 从 RANGE 升级为 BREAKOUT"
            return False, None

        # Layer 1: same signal cooldown
        same_signals = [
            r for r in records
            if r.signal_type == signal.signal_type
            and r.direction == signal.direction
            and now - r.timestamp < same_signal_mins * 60
        ]
        if same_signals:
            last = same_signals[-1]

            conf_threshold = override_cfg.get("confidence_increase", 0.10)
            if signal.regime.confidence - last.confidence >= conf_threshold:
                return True, f"置信度提升 {signal.regime.confidence - last.confidence:.0%}"

            ext_threshold = override_cfg.get("price_extension_pct", 0.50)
            if last.price > 0:
                if signal.direction == "bullish":
                    price_ext = (signal.price - last.price) / last.price * 100
                else:
                    price_ext = (last.price - signal.price) / last.price * 100
                if price_ext >= ext_threshold:
                    return True, f"价格继续扩展 {price_ext:.2f}%"

            if override_cfg.get("regime_upgrade", True):
                if last.signal_type.startswith("RANGE") and signal.signal_type.startswith("BREAKOUT"):
                    return True, "Regime 从 RANGE 升级为 BREAKOUT"

            return False, None

        return True, None

    def _record_alert(self, symbol: str, signal: USScanSignal, session: str) -> None:
        record = USScanAlertRecord(
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
    def _signal_strength_label(signal: USScanSignal) -> tuple[str, str]:
        """Classify signal strength into tiers based on confidence and RVOL.

        Returns (label, emoji):
          - conf >= 0.85 AND rvol >= 2.0 → "极强信号", 🔥
          - conf >= 0.80 OR  rvol >= 1.8 → "强信号",   🚨
          - else                         → "标准信号", 🔔
        """
        conf = signal.regime.confidence
        rvol = signal.regime.rvol
        if conf >= 0.85 and rvol >= 2.0:
            return "极强信号", "\U0001f525"  # 🔥
        if conf >= 0.80 or rvol >= 1.8:
            return "强信号", "\U0001f6a8"  # 🚨
        return "标准信号", "\U0001f514"  # 🔔

    @staticmethod
    def _format_scan_header(
        signal: USScanSignal,
        risk_level: str,
        option_rec: OptionRecommendation | None,
        override_reason: str | None,
        cooldown_mins: int = 30,
    ) -> str:
        """Format the alert header with structure/execution dual status."""
        is_breakout = signal.signal_type.startswith("BREAKOUT")
        emoji = "\U0001f680" if is_breakout else "\U0001f4e6"
        dir_cn = "看多" if signal.direction == "bullish" else "看空"
        dir_arrow = "\u2191" if signal.direction == "bullish" else "\u2193"

        strength_label, strength_emoji = USPredictor._signal_strength_label(signal)

        lines = [
            f"{strength_emoji} <b>{signal.signal_type} {strength_label}</b> {emoji} {dir_arrow} {dir_cn}",
            f"  置信度: {signal.regime.confidence:.0%}",
        ]

        # Execution status (informational, does not block push)
        if option_rec and option_rec.action != "wait":
            lines.append(f"  期权执行: ✅ 可执行 ({option_rec.action} {option_rec.expiry or ''})")
        else:
            wait_reason = option_rec.risk_note if option_rec else "期权链不可用"
            lines.append(f"  期权执行: ⏳ 暂无合约 ({_esc(wait_reason[:50])})")

        lines.append("  触发原因:")
        for reason in signal.trigger_reasons:
            lines.append(f"  • {_esc(reason)}")

        if risk_level == "elevated":
            lines.append("  ⚠️ 风险偏高, 仓位和出手频率都要收缩")

        if override_reason:
            lines.append(f"  • 冷却期覆盖: {_esc(override_reason)}")

        lines.append(f"  • {cooldown_mins} 分钟内不再重复 (除非信号显著增强)")
        lines.append("\u2501" * 20)

        return "\n".join(lines)

    # ── Bar caching: history cached, today always fresh ──

    async def _get_bars_split(
        self,
        symbol: str,
        fetch_days: int,
        vp_target: int,
    ) -> tuple[pd.DataFrame, pd.DataFrame, _SymbolCache]:
        """Return (hist_bars, today_bars, cache_entry). History is cached, today is always fresh."""
        now = time.time()
        cached = self._hist_cache.get(symbol)

        if cached and now - cached.timestamp < self._cache_ttl:
            # Cache hit: reuse history, fetch fresh today bars
            fresh = await self._collector.get_history_bars(symbol, days=1)
            today_bars = get_today_bars(fresh)
            return cached.hist_bars, today_bars, cached

        # Cache miss: fetch full, split, cache
        full = await self._collector.get_history_bars(symbol, days=fetch_days)
        if full.empty:
            empty = pd.DataFrame()
            entry = _SymbolCache(
                timestamp=now, hist_bars=empty,
                pdh=0.0, pdl=0.0, pmh=0.0, pml=0.0, pm_source="gap_estimate",
            )
            return empty, empty, entry

        hist_bars = get_history_bars(full)
        today_bars = get_today_bars(full)

        # PDH/PDL from full bars (needs today to find "previous day")
        pdh, pdl = extract_previous_day_hl(full)

        # PDH/PDL consistency check vs snapshot prev_close
        snap = await self._collector.get_snapshot(symbol)
        prev_close_snap = snap.get("prev_close_price", 0.0) or 0.0
        if pdh > 0 and pdl > 0 and prev_close_snap > 0:
            if prev_close_snap > pdh * 1.002 or prev_close_snap < pdl * 0.998:
                logger.warning(
                    "%s PDH/PDL mismatch: prev_close=%.2f, PDH=%.2f, PDL=%.2f "
                    "(bar-based 'previous day' may differ from snapshot)",
                    symbol, prev_close_snap, pdh, pdl,
                )

        # PMH/PML (reuse snap from PDH/PDL check above)
        pm_data = await self._collector.get_premarket_hl(symbol, snapshot=snap)
        pmh, pml, pm_source = pm_data.pmh, pm_data.pml, pm_data.source

        entry = _SymbolCache(
            timestamp=now, hist_bars=hist_bars,
            pdh=pdh, pdl=pdl, pmh=pmh, pml=pml, pm_source=pm_source,
        )
        self._hist_cache[symbol] = entry
        return hist_bars, today_bars, entry

    # ── Helpers ──

    def _this_week_friday(self) -> str:
        today = datetime.now(ET).date()
        days_ahead = 4 - today.weekday()
        if days_ahead < 0:
            days_ahead += 7
        friday = today + timedelta(days=days_ahead)
        return friday.strftime("%Y-%m-%d")
