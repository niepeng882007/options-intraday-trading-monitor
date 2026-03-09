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
from src.us_playbook.indicators import calculate_us_rvol, calculate_vwap
from src.us_playbook.levels import (
    build_key_levels,
    compute_volume_profile,
    extract_previous_day_hl,
    get_history_bars,
    get_today_bars,
)
from src.us_playbook.playbook import REGIME_STRATEGY, format_us_playbook_message
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

        # 1. Fetch history bars
        lookback = vp_cfg.get("lookback_days", 3)
        bars = await self._collector.get_history_bars(symbol, days=lookback + 2)
        if bars.empty:
            logger.warning("No bars for %s, skipping", symbol)
            return None

        # 2. Split today vs history
        history = get_history_bars(bars)
        today = get_today_bars(bars)

        # 3. Volume Profile
        vp = compute_volume_profile(history, value_area_pct=vp_cfg.get("value_area_pct", 0.70))

        # 4. PDH/PDL
        pdh, pdl = extract_previous_day_hl(bars)

        # 5. Pre-market HL
        pmh, pml = await self._collector.get_premarket_hl(symbol)

        # 6. VWAP + RVOL
        vwap = calculate_vwap(today)
        rvol = calculate_us_rvol(
            today, history,
            skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3),
            lookback_days=rvol_cfg.get("lookback_days", 10),
        )

        # 7. Current price (via snapshot — no subscription needed)
        snap = await self._collector.get_snapshot(symbol)
        price = snap["last_price"]
        prev_close = snap["prev_close_price"] or 0.0

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

        # 10. Regime classification
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
        )

        # 11. Build key levels
        key_levels = build_key_levels(vp, pdh, pdl, pmh, pml, vwap, gamma_wall)

        # 12. Strategy text
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
