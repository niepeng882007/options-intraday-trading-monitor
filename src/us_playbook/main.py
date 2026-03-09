"""US Playbook orchestrator — daily playbook generation with scheduled pushes.

Usage:
    python -m src.us_playbook      # Run standalone
    # Or integrated into OptionsMonitor via shared APScheduler
"""

from __future__ import annotations

import asyncio
import html
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta, timezone

import pandas as pd

from src.hk import FilterResult, GammaWallResult, VolumeProfileResult
from src.hk.gamma_wall import calculate_gamma_wall, format_gamma_wall_message
from src.us_playbook import KeyLevels, USPlaybookResult, USRegimeResult, USRegimeType
from src.us_playbook.filter import check_us_filters
from src.us_playbook.indicators import calculate_us_rvol, calculate_vwap, compute_rvol_profile
from src.us_playbook.levels import (
    build_key_levels,
    calc_fetch_calendar_days,
    compute_volume_profile,
    extract_previous_day_hl,
    get_history_bars,
    get_today_bars,
)
from src.us_playbook.playbook import REGIME_STRATEGY, format_regime_change_alert, format_us_playbook_message
from src.us_playbook.regime import classify_us_regime
from src.utils.logger import setup_logger

logger = setup_logger("us_playbook")

ET = timezone(timedelta(hours=-5))
_executor = ThreadPoolExecutor(max_workers=1)
_esc = html.escape


class USPlaybook:
    """Orchestrates US Playbook pipeline.

    Data flow per cycle:
        1. Run market context symbols (SPY/QQQ) first → spy_regime
        2. For each watchlist symbol:
           a. Fetch 5-day 1m bars → VP + PDH/PDL + today bars
           b. Fetch pre-market HL → PMH/PML
           c. Calculate VWAP + RVOL
           d. Fetch option chain → Gamma Wall (graceful fallback)
           e. Check filters
           f. Classify regime (with spy_regime context)
           g. Format & push to Telegram
    """

    def __init__(self, config: dict, collector) -> None:
        self._cfg = config
        self._collector = collector
        self._send_fn = None  # async TG callback
        self._last_playbooks: dict[str, USPlaybookResult] = {}
        self._cached_context: dict[str, dict] = {}
        self._regime_flip_timestamps: dict[str, list[datetime]] = {}

    def set_send_fn(self, fn) -> None:
        """Set async callback for Telegram message sending: fn(text, parse_mode)."""
        self._send_fn = fn

    async def _send_tg(self, text: str) -> None:
        if self._send_fn:
            try:
                await self._send_fn(text, parse_mode="HTML")
            except Exception as e:
                logger.error("TG send failed: %s", e)

    # ── Core pipeline ──

    async def run_playbook_cycle(self, update_type: str = "morning") -> None:
        """Run full playbook pipeline for all configured symbols."""
        watchlist = self._cfg.get("watchlist", [])
        context_symbols = self._cfg.get("regime", {}).get("market_context_symbols", ["SPY", "QQQ"])

        # Phase 1: Run market context symbols first
        spy_result: USPlaybookResult | None = None
        qqq_result: USPlaybookResult | None = None

        for ctx_sym in context_symbols:
            ctx_info = self._find_watchlist_entry(ctx_sym, watchlist)
            if not ctx_info:
                continue
            try:
                result = await self._run_single_symbol(
                    ctx_info["symbol"], ctx_info.get("name", ctx_sym), update_type, None,
                )
                if result:
                    self._last_playbooks[ctx_sym] = result
                    if ctx_sym == "SPY":
                        spy_result = result
                    elif ctx_sym == "QQQ":
                        qqq_result = result
            except Exception:
                logger.exception("Context symbol %s failed", ctx_sym)

        spy_regime = spy_result.regime.regime if spy_result else None

        # Phase 2: Run individual symbols
        for entry in watchlist:
            symbol = entry["symbol"]
            name = entry.get("name", symbol)
            if symbol in context_symbols:
                # Already ran, just format & push
                result = self._last_playbooks.get(symbol)
                if result:
                    msg = format_us_playbook_message(result, update_type, spy_result, qqq_result)
                    await self._send_tg(msg)
                continue

            try:
                result = await self._run_single_symbol(symbol, name, update_type, spy_regime)
                if result:
                    self._last_playbooks[symbol] = result
                    msg = format_us_playbook_message(result, update_type, spy_result, qqq_result)
                    await self._send_tg(msg)
                    logger.info("Playbook sent for %s (%s)", symbol, update_type)
            except Exception:
                logger.exception("Playbook cycle failed for %s", symbol)

            # Rate limit: 1s between symbols (K-line API)
            await asyncio.sleep(1)

    async def _run_single_symbol(
        self,
        symbol: str,
        name: str,
        update_type: str,
        spy_regime: USRegimeType | None,
    ) -> USPlaybookResult | None:
        """Run pipeline for a single symbol."""
        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        rvol_cfg = cfg.get("rvol", {})
        regime_cfg = cfg.get("regime", {})
        filter_cfg = cfg.get("filters", {})

        # 1. Fetch history bars (wide window covers both VP and RVOL)
        vp_target = vp_cfg.get("lookback_trading_days") or vp_cfg.get("lookback_days", 5)
        min_td = vp_cfg.get("min_trading_days", 3)
        rvol_td = rvol_cfg.get("lookback_days", 10)
        fetch_days = calc_fetch_calendar_days(vp_target, rvol_td)
        bars = await self._collector.get_history_bars(symbol, days=fetch_days)
        if bars.empty:
            logger.warning("No bars for %s, skipping", symbol)
            return None

        # 2. Split today vs history — VP uses truncated, RVOL uses full
        history_all = get_history_bars(bars)
        vp_history = get_history_bars(bars, max_trading_days=vp_target)
        today = get_today_bars(bars)

        # 3. Volume Profile (with trading_days populated)
        vp = compute_volume_profile(vp_history, value_area_pct=vp_cfg.get("value_area_pct", 0.70))

        # 4. PDH/PDL
        pdh, pdl = extract_previous_day_hl(bars)

        # 5. Current price (via snapshot — no subscription needed)
        snap = await self._collector.get_snapshot(symbol)
        price = snap["last_price"]
        prev_close = snap["prev_close_price"] or 0.0

        # 6. Pre-market HL (reuse snapshot to avoid duplicate API call)
        pm_data = await self._collector.get_premarket_hl(symbol, snapshot=snap)
        pmh, pml, pm_source = pm_data.pmh, pm_data.pml, pm_data.source

        # 7. VWAP + RVOL (RVOL uses full history for better coverage)
        vwap = calculate_vwap(today)
        rvol = calculate_us_rvol(
            today, history_all,
            skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
            lookback_days=rvol_cfg.get("lookback_days", 10),
        )

        # 8. Gamma Wall (graceful fallback, 10s hard timeout)
        gamma_wall: GammaWallResult | None = None
        if cfg.get("gamma_wall", {}).get("enabled", True):
            try:
                async def _fetch_gamma():
                    expiry = self._this_week_friday()
                    options = await self._collector.get_option_chain(symbol, expiration=expiry)
                    if options:
                        chain_df = pd.DataFrame([
                            {
                                "option_type": o.option_type.upper(),
                                "strike_price": o.strike,
                                "open_interest": o.open_interest,
                            }
                            for o in options
                        ])
                        return calculate_gamma_wall(chain_df, price)
                    return None
                gamma_wall = await asyncio.wait_for(_fetch_gamma(), timeout=10)
            except Exception:
                logger.warning("Gamma wall fetch failed for %s, skipping", symbol)

        # 9. Filters
        filters = check_us_filters(
            rvol=rvol,
            prev_high=pdh,
            prev_low=pdl,
            current_high=snap["high_price"],
            current_low=snap["low_price"],
            calendar_path=filter_cfg.get("calendar_file", "config/us_calendar.yaml"),
            inside_day_rvol_threshold=filter_cfg.get("inside_day_rvol_threshold", 0.8),
        )

        # 10. Adaptive RVOL profile
        adaptive_cfg = regime_cfg.get("adaptive", {})
        rvol_profile = None
        if adaptive_cfg.get("enabled", True):
            rvol_profile = compute_rvol_profile(
                history_bars=history_all,
                today_rvol=rvol,
                skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
                gap_and_go_pctl=adaptive_cfg.get("gap_and_go_percentile", 85),
                trend_day_pctl=adaptive_cfg.get("trend_day_percentile", 60),
                fade_chop_pctl=adaptive_cfg.get("fade_chop_percentile", 30),
                fallback_gap_and_go=regime_cfg.get("gap_and_go_rvol", 1.5),
                fallback_trend_day=regime_cfg.get("trend_day_rvol", 1.2),
                fallback_fade_chop=regime_cfg.get("fade_chop_rvol", 1.0),
                min_sample_days=adaptive_cfg.get("min_sample_days", 5),
            )

        # 11. Regime classification (with VP data quality + adaptive thresholds)
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
        )

        # 12. Build key levels
        key_levels = build_key_levels(vp, pdh, pdl, pmh, pml, vwap, gamma_wall, pm_source=pm_source)

        # 13. Cache intermediate data for lightweight regime monitor
        self._cached_context[symbol] = {
            "history_all": history_all,
            "vp": vp,
            "pdh": pdh, "pdl": pdl,
            "pmh": pmh, "pml": pml,
            "prev_close": prev_close,
            "rvol_profile": rvol_profile,
            "gamma_wall": gamma_wall,
        }

        # 14. Strategy text
        strategy_text = REGIME_STRATEGY.get(regime.regime, "")

        return USPlaybookResult(
            symbol=symbol,
            name=name,
            regime=regime,
            key_levels=key_levels,
            volume_profile=vp,
            gamma_wall=gamma_wall,
            filters=filters,
            strategy_text=strategy_text,
            generated_at=datetime.now(ET),
        )

    # ── Regime Monitor ──

    async def run_regime_monitor_cycle(self) -> None:
        """Lightweight regime check between morning/confirm pushes.

        Runs only in the 09:50-10:13 ET window. Compares current regime
        against cached playbook regime; pushes alert only on change.
        """
        now = datetime.now(ET)
        if not self._is_in_monitor_window(now):
            return

        if not self._last_playbooks:
            return

        monitor_cfg = self._cfg.get("regime_monitor", {})
        conf_threshold = monitor_cfg.get("confidence_change_threshold", 0.2)
        max_flips = monitor_cfg.get("max_flips_in_window", 2)

        watchlist = self._cfg.get("watchlist", [])
        context_symbols = self._cfg.get("regime", {}).get("market_context_symbols", ["SPY", "QQQ"])

        # Phase 1: Check context symbols first to get spy_regime
        spy_regime: USRegimeType | None = None
        for ctx_sym in context_symbols:
            changed, new_regime, old_regime = await self._check_regime_change(
                ctx_sym, None, conf_threshold,
            )
            if new_regime and ctx_sym == "SPY":
                spy_regime = new_regime.regime
            if changed and new_regime and old_regime:
                if self._should_suppress_flip(ctx_sym, now, max_flips):
                    continue
                await self._send_regime_alert(ctx_sym, old_regime, new_regime)

        # Phase 2: Check remaining symbols
        for entry in watchlist:
            symbol = entry["symbol"]
            if symbol in context_symbols:
                continue
            changed, new_regime, old_regime = await self._check_regime_change(
                symbol, spy_regime, conf_threshold,
            )
            if changed and new_regime and old_regime:
                if self._should_suppress_flip(symbol, now, max_flips):
                    continue
                await self._send_regime_alert(symbol, old_regime, new_regime)
            await asyncio.sleep(1)  # rate limit

    async def _check_regime_change(
        self,
        symbol: str,
        spy_regime: USRegimeType | None,
        conf_threshold: float,
    ) -> tuple[bool, USRegimeResult | None, USRegimeResult | None]:
        """Lightweight regime re-check using cached VP/PDH/PDL/PMH/PML.

        Returns (changed, new_regime, old_regime).
        """
        cached = self._cached_context.get(symbol)
        old_pb = self._last_playbooks.get(symbol)
        if not cached or not old_pb:
            return False, None, None

        old_regime = old_pb.regime
        cfg = self._cfg
        rvol_cfg = cfg.get("rvol", {})
        regime_cfg = cfg.get("regime", {})
        adaptive_cfg = regime_cfg.get("adaptive", {})

        try:
            # Fresh data: snapshot + today bars
            vp_target = cfg.get("volume_profile", {}).get("lookback_trading_days", 5)
            rvol_td = rvol_cfg.get("lookback_days", 10)
            fetch_days = calc_fetch_calendar_days(vp_target, rvol_td)
            bars = await self._collector.get_history_bars(symbol, days=fetch_days)
            if bars.empty:
                return False, None, None

            today = get_today_bars(bars)
            history_all = get_history_bars(bars)

            snap = await self._collector.get_snapshot(symbol)
            price = snap["last_price"]

            # Fresh VWAP + RVOL
            vwap = calculate_vwap(today)
            rvol = calculate_us_rvol(
                today, history_all,
                skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
                lookback_days=rvol_cfg.get("lookback_days", 10),
            )

            # Reuse cached: VP, PDH/PDL, PMH/PML, prev_close, rvol_profile, gamma_wall
            new_regime = classify_us_regime(
                price=price,
                prev_close=cached["prev_close"],
                rvol=rvol,
                pmh=cached["pmh"],
                pml=cached["pml"],
                vp=cached["vp"],
                gamma_wall=cached["gamma_wall"],
                spy_regime=spy_regime,
                gap_and_go_rvol=regime_cfg.get("gap_and_go_rvol", 1.5),
                trend_day_rvol=regime_cfg.get("trend_day_rvol", 1.2),
                fade_chop_rvol=regime_cfg.get("fade_chop_rvol", 1.0),
                vp_trading_days=cached["vp"].trading_days,
                min_vp_trading_days=cfg.get("volume_profile", {}).get("min_trading_days", 3),
                rvol_profile=cached["rvol_profile"],
                gap_significance_threshold=adaptive_cfg.get("gap_significance_threshold", 0.3),
            )
        except Exception:
            logger.exception("Regime check failed for %s", symbol)
            return False, None, None

        # Detect change: regime flip or large confidence shift
        regime_changed = new_regime.regime != old_regime.regime
        conf_changed = abs(new_regime.confidence - old_regime.confidence) >= conf_threshold
        changed = regime_changed or conf_changed

        if changed:
            # Update cached playbook with new regime and key levels
            old_pb.regime = new_regime
            old_pb.key_levels = build_key_levels(
                cached["vp"], cached["pdh"], cached["pdl"],
                cached["pmh"], cached["pml"], vwap, cached["gamma_wall"],
            )
            old_pb.generated_at = datetime.now(ET)
            logger.info(
                "Regime change detected for %s: %s→%s (conf %.0f%%→%.0f%%, RVOL %.2f→%.2f)",
                symbol, old_regime.regime.value, new_regime.regime.value,
                old_regime.confidence * 100, new_regime.confidence * 100,
                old_regime.rvol, new_regime.rvol,
            )

        return changed, new_regime, old_regime

    async def _send_regime_alert(
        self,
        symbol: str,
        old_regime: USRegimeResult,
        new_regime: USRegimeResult,
    ) -> None:
        pb = self._last_playbooks.get(symbol)
        name = pb.name if pb else symbol
        key_levels = pb.key_levels if pb else None
        msg = format_regime_change_alert(symbol, name, old_regime, new_regime, key_levels)
        await self._send_tg(msg)

    def _is_in_monitor_window(self, now: datetime) -> bool:
        """Check if current time falls in the regime monitor window."""
        if now.weekday() >= 5:
            return False
        monitor_cfg = self._cfg.get("regime_monitor", {})
        if not monitor_cfg.get("enabled", True):
            return False
        push_times = self._cfg.get("playbook", {}).get("push_times", ["09:45", "10:15"])
        morning_h, morning_m = map(int, push_times[0].split(":"))
        confirm_h, confirm_m = map(int, push_times[1].split(":"))
        start_offset = monitor_cfg.get("start_after_morning_minutes", 5)
        end_offset = monitor_cfg.get("end_before_confirm_minutes", 2)
        window_start = now.replace(
            hour=morning_h, minute=morning_m, second=0, microsecond=0,
        ) + timedelta(minutes=start_offset)
        window_end = now.replace(
            hour=confirm_h, minute=confirm_m, second=0, microsecond=0,
        ) - timedelta(minutes=end_offset)
        return window_start <= now <= window_end

    def _should_suppress_flip(self, symbol: str, now: datetime, max_flips: int) -> bool:
        """Suppress alerts if regime has flipped too many times in the monitor window."""
        timestamps = self._regime_flip_timestamps.setdefault(symbol, [])
        # Prune timestamps older than 10 minutes
        cutoff = now - timedelta(minutes=10)
        timestamps[:] = [ts for ts in timestamps if ts > cutoff]
        timestamps.append(now)
        if len(timestamps) > max_flips:
            logger.warning(
                "Regime flip suppressed for %s: %d flips in 10min (unstable)",
                symbol, len(timestamps),
            )
            return True
        return False

    # ── Helpers ──

    def _this_week_friday(self) -> str:
        """Return this week's Friday as YYYY-MM-DD string."""
        today = datetime.now(ET).date()
        days_ahead = 4 - today.weekday()  # 4 = Friday
        if days_ahead < 0:
            days_ahead += 7
        friday = today + timedelta(days=days_ahead)
        return friday.strftime("%Y-%m-%d")

    @staticmethod
    def _find_watchlist_entry(symbol: str, watchlist: list[dict]) -> dict | None:
        for entry in watchlist:
            if entry["symbol"] == symbol:
                return entry
        return None

    # ── Bot command helpers ──

    async def get_playbook_text(self, symbol: str | None = None) -> str:
        """Generate playbook for a single symbol (manual trigger)."""
        target = symbol or "SPY"
        entry = self._find_watchlist_entry(target, self._cfg.get("watchlist", []))
        if not entry:
            return f"Symbol {target} not in watchlist"
        name = entry.get("name", target)
        result = await self._run_single_symbol(target, name, "manual", None)
        if not result:
            return f"Failed to generate playbook for {target}"
        self._last_playbooks[target] = result
        return format_us_playbook_message(result, "manual")

    async def get_levels_text(self, symbol: str | None = None) -> str:
        target = symbol or "SPY"
        pb = self._last_playbooks.get(target)
        if not pb:
            return f"No playbook cached for {target}. Run /us_playbook first."

        kl = pb.key_levels
        lines = [f"📍 <b>{target} 关键点位</b>", "━" * 20]
        level_pairs = [
            ("Call Wall", kl.gamma_call_wall),
            ("PDH", kl.pdh), ("PMH", kl.pmh), ("VAH", kl.vah),
            ("VWAP", kl.vwap), ("POC", kl.poc), ("VAL", kl.val),
            ("PDL", kl.pdl), ("PML", kl.pml),
            ("Put Wall", kl.gamma_put_wall),
            ("Max Pain", kl.gamma_max_pain),
        ]
        for name, val in sorted(level_pairs, key=lambda x: -x[1]):
            if val > 0:
                lines.append(f"  {name}: {val:,.2f}")
        lines.append(f"\n当前价: {pb.regime.price:,.2f}")
        return "\n".join(lines)

    async def get_regime_text(self, symbol: str | None = None) -> str:
        target = symbol or "SPY"
        pb = self._last_playbooks.get(target)
        if not pb:
            return f"No playbook cached for {target}. Run /us_playbook first."

        r = pb.regime
        from src.us_playbook.playbook import REGIME_EMOJI, REGIME_NAME_CN
        emoji = REGIME_EMOJI.get(r.regime, "❓")
        name = REGIME_NAME_CN.get(r.regime, "未知")
        lines = [
            f"{emoji} <b>{target} Regime</b>",
            f"风格: {name}",
            f"置信度: {r.confidence:.0%}",
            f"RVOL: {r.rvol:.2f}",
            f"Gap: {r.gap_pct:+.2f}%",
            f"详情: {_esc(r.details)}",
        ]
        strategy = REGIME_STRATEGY.get(r.regime, "")
        if strategy:
            lines.extend(["", strategy])
        return "\n".join(lines)

    async def get_filters_text(self) -> str:
        lines = ["⚡ <b>US 风险过滤状态</b>", "━" * 20]
        if not self._last_playbooks:
            lines.append("暂无数据，等待 Playbook 生成")
            return "\n".join(lines)
        for sym, pb in self._last_playbooks.items():
            f = pb.filters
            status = "🟢" if f.tradeable else "🔴"
            lines.append(f"{status} {sym}: {f.risk_level}")
            for w in f.warnings:
                lines.append(f"  ⚠️ {_esc(w)}")
        return "\n".join(lines)

    async def get_gamma_text(self, symbol: str | None = None) -> str:
        target = symbol or "SPY"
        pb = self._last_playbooks.get(target)
        if not pb:
            return f"No playbook cached for {target}. Run /us_playbook first."
        if not pb.gamma_wall:
            return f"{target}: Gamma Wall 数据不可用"
        return format_gamma_wall_message(pb.gamma_wall, symbol=target)
