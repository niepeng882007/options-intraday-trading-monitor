"""Market Tone Engine — market-level assessment for US Playbook.

Produces a MarketTone grade (A+ ~ D) that influences regime confidence,
auto-scan gating, position sizing, and playbook display.

Signals:
    1. Macro calendar (FOMC / NFP / CPI)
    2. VIX level + intraday change (yfinance, 5min cache)
    3. SPY gap % (snapshot prev_close)
    4. ORB — SPY first 30min high/low + 10AM reversal check
    5. VWAP position + slope
    6. Breadth proxy — 10-stock alignment (batch snapshot)
"""

from __future__ import annotations

import time
from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd

from src.common.indicators import calculate_vwap_series, calculate_vwap_slope
from src.us_playbook import (
    BreadthProxy,
    MarketTone,
    ORBRange,
    VIXContext,
    VWAPStatus,
)
from src.us_playbook.filter import get_today_macro_context
from src.utils.logger import setup_logger

logger = setup_logger("market_tone")

ET = ZoneInfo("America/New_York")

# Grade ordering (index = grade_score)
_GRADES = ["D", "C", "B", "B+", "A", "A+"]
_GRADE_TO_SCORE = {g: i for i, g in enumerate(_GRADES)}

_GRADE_CONF_MOD = {
    "A+": 0.10,
    "A": 0.05,
    "B+": 0.0,
    "B": -0.05,
    "C": -0.10,
    "D": -0.15,
}

_GRADE_POSITION = {
    "A+": "full",
    "A": "full",
    "B+": "reduced",
    "B": "reduced",
    "C": "minimal",
    "D": "sit_out",
}


class MarketToneEngine:
    """Compute and cache the market-level tone grade."""

    def __init__(self, config: dict, collector) -> None:
        self._cfg = config.get("market_tone", {})
        self._collector = collector
        self._tone_cache: tuple[float, MarketTone | None] = (0.0, None)
        self._orb_cache: dict[str, ORBRange] = {}  # keyed by date str
        self._vix_cache: tuple[float, VIXContext | None] = (0.0, None)

    # ── Public API ──

    async def get_tone(
        self,
        spy_today_bars: pd.DataFrame | None = None,
    ) -> MarketTone | None:
        """Return cached tone or recompute.

        *spy_today_bars* from the caller avoids a duplicate Futu fetch.
        """
        ttl = self._cfg.get("cache_ttl", 120)
        now = time.time()
        if self._tone_cache[1] and now - self._tone_cache[0] < ttl:
            return self._tone_cache[1]

        if spy_today_bars is None or spy_today_bars.empty:
            return self._tone_cache[1]  # can't compute without SPY bars

        tone = await self.compute_tone(spy_today_bars)
        self._tone_cache = (now, tone)
        return tone

    async def compute_tone(self, spy_today_bars: pd.DataFrame) -> MarketTone:
        """Full market tone computation."""
        now_et = datetime.now(ET)
        today = now_et.date()
        filter_cfg = self._cfg  # already the market_tone sub-dict

        # ── 1. Macro ──
        cal_path = "config/us_calendar.yaml"
        macro_ctx = get_today_macro_context(cal_path, today)
        macro_signal = macro_ctx["behavior"]  # "clear" / "range_then_trend" / "data_reaction" / "blocked"

        # ── 2. VIX ──
        vix = await self._fetch_vix()

        # ── 3. Gap ──
        gap_signal, gap_pct = await self._classify_gap()

        # ── 4. ORB ──
        orb = self._compute_orb(spy_today_bars, now_et)

        # ── 5. VWAP ──
        vwap_status = self._compute_vwap_status(spy_today_bars)

        # ── 6. Breadth ──
        breadth = await self._compute_breadth()

        # ── Aggregate ──
        return self._aggregate(
            macro_signal=macro_signal,
            gap_signal=gap_signal,
            gap_pct=gap_pct,
            orb=orb,
            vwap_status=vwap_status,
            breadth=breadth,
            vix=vix,
            now_et=now_et,
            macro_ctx=macro_ctx,
        )

    # ── Component calculators ──

    async def _classify_gap(self) -> tuple[str, float]:
        """Classify SPY gap from snapshot prev_close. Returns (signal, gap_pct)."""
        gap_cfg = self._cfg.get("gap", {})
        small = gap_cfg.get("small_threshold", 0.5)
        large = gap_cfg.get("large_threshold", 1.0)

        try:
            snap = await self._collector.get_snapshot("SPY")
            price = snap.get("last_price", 0.0)
            prev_close = snap.get("prev_close_price", 0.0)
            if prev_close <= 0 or price <= 0:
                return "neutral", 0.0

            gap_pct = (price - prev_close) / prev_close * 100

            if abs(gap_pct) >= large:
                return "gap_and_go", gap_pct
            if abs(gap_pct) < small:
                return "gap_fill", gap_pct
            return "neutral", gap_pct
        except Exception:
            logger.debug("Gap classification failed, defaulting to neutral")
            return "neutral", 0.0

    def _compute_orb(self, spy_bars: pd.DataFrame, now_et: datetime) -> ORBRange:
        """Compute Opening Range Breakout from SPY bars."""
        orb_cfg = self._cfg.get("orb", {})
        window_min = orb_cfg.get("window_minutes", 30)

        date_key = now_et.strftime("%Y-%m-%d")

        # Filter bars within ORB window (09:30 - 10:00 by default)
        orb_start = dt_time(9, 30)
        orb_end_minute = 30 + window_min
        orb_end_h, orb_end_m = divmod(orb_end_minute, 60)
        orb_end = dt_time(9 + orb_end_h, orb_end_m)

        if spy_bars.empty:
            return ORBRange(high=0.0, low=0.0)

        orb_bars = spy_bars[
            (spy_bars.index.time >= orb_start) & (spy_bars.index.time < orb_end)
        ]
        if orb_bars.empty:
            return ORBRange(high=0.0, low=0.0)

        orb_high = float(orb_bars["High"].max())
        orb_low = float(orb_bars["Low"].min())

        orb = ORBRange(high=orb_high, low=orb_low)

        # Check for breakout after ORB window
        rev_time_str = orb_cfg.get("reversal_check_time", "10:00")
        rev_h, rev_m = map(int, rev_time_str.split(":"))
        if now_et.hour > rev_h or (now_et.hour == rev_h and now_et.minute >= rev_m):
            # Use current price (last bar close)
            current_price = float(spy_bars.iloc[-1]["Close"])
            if current_price > orb_high:
                orb.breakout_direction = "bullish"
                orb.confirmed = True
            elif current_price < orb_low:
                orb.breakout_direction = "bearish"
                orb.confirmed = True

            # 10AM reversal check
            orb = self._check_10am_reversal(spy_bars, orb, now_et)

        # Cache ORB for the day
        self._orb_cache[date_key] = orb
        return orb

    def _check_10am_reversal(
        self,
        spy_bars: pd.DataFrame,
        orb: ORBRange,
        now_et: datetime,
    ) -> ORBRange:
        """Check if 10AM reversal failed, confirming original trend."""
        orb_cfg = self._cfg.get("orb", {})
        rev_window = orb_cfg.get("reversal_window_minutes", 15)
        rev_time_str = orb_cfg.get("reversal_check_time", "10:00")
        rev_h, rev_m = map(int, rev_time_str.split(":"))

        rev_start = dt_time(rev_h, rev_m)
        rev_end_total = rev_h * 60 + rev_m + rev_window
        rev_end = dt_time(rev_end_total // 60, rev_end_total % 60)

        # Get the first 30min direction
        orb_start = dt_time(9, 30)
        first_30 = spy_bars[
            (spy_bars.index.time >= orb_start) & (spy_bars.index.time < dt_time(10, 0))
        ]
        if first_30.empty or len(first_30) < 2:
            return orb

        first_open = float(first_30.iloc[0]["Open"])
        first_close = float(first_30.iloc[-1]["Close"])
        first_direction = "up" if first_close > first_open else "down"

        # Get reversal window bars
        rev_bars = spy_bars[
            (spy_bars.index.time >= rev_start) & (spy_bars.index.time < rev_end)
        ]
        if rev_bars.empty:
            return orb

        # Check if reversal occurred: price crosses back through open in opposite direction
        if first_direction == "up":
            # A reversal would be price dropping below open
            reversal_happened = float(rev_bars["Low"].min()) < first_open
        else:
            reversal_happened = float(rev_bars["High"].max()) > first_open

        if not reversal_happened:
            orb.reversal_failed = True  # No reversal → original trend confirmed

        return orb

    def _compute_vwap_status(self, spy_bars: pd.DataFrame) -> VWAPStatus | None:
        """Compute SPY VWAP position and slope."""
        if spy_bars.empty:
            return None

        vwap_cfg = self._cfg.get("vwap", {})
        lookback = vwap_cfg.get("slope_lookback", 15)
        flat_thresh = vwap_cfg.get("slope_flat_threshold", 0.005)

        vwap_series = calculate_vwap_series(spy_bars)
        if vwap_series.empty:
            return None

        vwap_val = float(vwap_series.iloc[-1])
        if vwap_val == 0:
            return None

        current_price = float(spy_bars.iloc[-1]["Close"])
        position = "above" if current_price > vwap_val else "below"

        slope = calculate_vwap_slope(spy_bars, lookback=lookback)
        if abs(slope) < flat_thresh:
            slope_label = "flat"
        elif slope > 0:
            slope_label = "rising"
        else:
            slope_label = "falling"

        return VWAPStatus(
            value=vwap_val,
            position=position,
            slope=slope,
            slope_label=slope_label,
        )

    async def _fetch_vix(self) -> VIXContext | None:
        """Fetch VIX from yfinance with caching. Failure → None."""
        vix_cfg = self._cfg.get("vix", {})
        if not vix_cfg.get("enabled", True):
            return None

        ttl = vix_cfg.get("cache_ttl", 300)
        now = time.time()
        if self._vix_cache[1] and now - self._vix_cache[0] < ttl:
            cached = self._vix_cache[1]
            cached.stale = (now - cached.timestamp) > 1200  # >20min
            return cached

        try:
            import yfinance as yf
            ticker = yf.Ticker("^VIX")
            info = ticker.fast_info
            level = float(getattr(info, "last_price", 0) or 0)
            prev = float(getattr(info, "previous_close", 0) or 0)

            if level <= 0:
                return self._vix_cache[1]

            change_pct = ((level - prev) / prev * 100) if prev > 0 else 0.0

            caution_surge = vix_cfg.get("caution_surge_pct", 5.0)
            supportive_drop = vix_cfg.get("supportive_drop_pct", 3.0)

            if change_pct >= caution_surge:
                signal = "caution"
            elif change_pct <= -supportive_drop:
                signal = "supportive"
            else:
                signal = "neutral"

            ctx = VIXContext(
                level=level,
                change_pct=change_pct,
                signal=signal,
                stale=False,
                timestamp=now,
            )
            self._vix_cache = (now, ctx)
            return ctx
        except Exception:
            logger.debug("VIX fetch failed, using cached or None")
            return self._vix_cache[1]

    async def _compute_breadth(self) -> BreadthProxy | None:
        """Compute breadth proxy from batch snapshot of basket stocks."""
        breadth_cfg = self._cfg.get("breadth", {})
        basket = breadth_cfg.get("basket", [
            "SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        ])
        if not basket:
            return None

        try:
            snapshots = await self._collector.get_snapshots(basket)
        except Exception:
            logger.debug("Breadth snapshot fetch failed")
            return None

        if not snapshots:
            return None

        bullish_count = 0
        bearish_count = 0
        total = 0
        index_symbols = {"SPY", "QQQ", "IWM"}
        index_dirs: list[str] = []

        for sym, snap in snapshots.items():
            last = snap.get("last_price", 0.0)
            prev = snap.get("prev_close_price", 0.0)
            if last <= 0 or prev <= 0:
                continue
            total += 1
            direction = "bullish" if last >= prev else "bearish"
            if direction == "bullish":
                bullish_count += 1
            else:
                bearish_count += 1
            if sym in index_symbols:
                index_dirs.append(direction)

        if total == 0:
            return None

        majority = max(bullish_count, bearish_count)
        alignment_ratio = majority / total

        strong_thresh = breadth_cfg.get("alignment_strong", 0.75)
        weak_thresh = breadth_cfg.get("alignment_weak", 0.50)

        if alignment_ratio >= strong_thresh:
            alignment_label = "strong_aligned"
        elif alignment_ratio < weak_thresh:
            alignment_label = "divergent"
        else:
            alignment_label = "mixed"

        # Index alignment: SPY+QQQ+IWM all same direction
        index_aligned = len(index_dirs) >= 2 and len(set(index_dirs)) == 1

        majority_dir = "bullish" if bullish_count >= bearish_count else "bearish"
        details = f"{bullish_count}↑ {bearish_count}↓ / {total}"

        return BreadthProxy(
            aligned_count=majority,
            total_count=total,
            alignment_ratio=alignment_ratio,
            alignment_label=alignment_label,
            index_aligned=index_aligned,
            details=details,
        )

    # ── Aggregation ──

    def _aggregate(
        self,
        macro_signal: str,
        gap_signal: str,
        gap_pct: float,
        orb: ORBRange | None,
        vwap_status: VWAPStatus | None,
        breadth: BreadthProxy | None,
        vix: VIXContext | None,
        now_et: datetime,
        macro_ctx: dict,
    ) -> MarketTone:
        """Aggregate component signals into a single grade."""
        grade_cfg = self._cfg.get("grade", {})
        conf_mods = grade_cfg.get("confidence_modifiers", _GRADE_CONF_MOD)

        aligned: list[str] = []
        conflicting: list[str] = []

        # Determine dominant direction from directional signals
        direction_votes: list[str] = []

        # Signal 1: Macro
        macro_aligned = macro_signal in ("clear",)
        if macro_aligned:
            aligned.append("宏观: 无重大事件")
        else:
            conflicting.append(f"宏观: {macro_ctx.get('event_name', '事件日')}")

        # Signal 2: Gap
        gap_direction: str | None = None
        if gap_signal == "gap_and_go":
            gap_direction = "bullish" if gap_pct > 0 else "bearish"
            direction_votes.append(gap_direction)
            aligned.append(f"Gap: SPY {gap_pct:+.1f}%")
        elif gap_signal == "gap_fill":
            # Small gap — could go either way, counted as aligned (neutral)
            aligned.append(f"Gap: 小幅 {gap_pct:+.1f}%")
        else:
            # neutral gap
            pass  # not aligned, not conflicting

        # Signal 3: ORB
        if orb and orb.confirmed and orb.breakout_direction:
            orb_dir = orb.breakout_direction
            direction_votes.append(orb_dir)
            extra = " 10AM 确认" if orb.reversal_failed else ""
            aligned.append(f"ORB: 突破{'上轨' if orb_dir == 'bullish' else '下轨'}{extra}")
        elif orb and orb.high > 0:
            conflicting.append("ORB: 待确认")

        # Signal 4: VWAP
        if vwap_status:
            vwap_dir = "bullish" if vwap_status.position == "above" else "bearish"
            slope_consistent = (
                (vwap_status.slope_label == "rising" and vwap_dir == "bullish")
                or (vwap_status.slope_label == "falling" and vwap_dir == "bearish")
                or vwap_status.slope_label == "flat"
            )
            if slope_consistent:
                direction_votes.append(vwap_dir)
                slope_arrow = {"rising": "↑", "falling": "↓", "flat": "→"}.get(
                    vwap_status.slope_label, ""
                )
                aligned.append(f"VWAP: SPY {vwap_status.position} 斜率{slope_arrow}")
            else:
                conflicting.append(
                    f"VWAP: {vwap_status.position} 但斜率{vwap_status.slope_label} 矛盾"
                )

        # Signal 5: Breadth
        if breadth:
            if breadth.alignment_label == "strong_aligned":
                majority_dir = (
                    "bullish" if breadth.aligned_count > breadth.total_count / 2
                    else "bearish"
                )
                # Infer majority direction from details
                if "↑" in breadth.details:
                    up_count = int(breadth.details.split("↑")[0])
                    down_count = int(
                        breadth.details.split("↑")[1].split("↓")[0].strip()
                    )
                    majority_dir = "bullish" if up_count > down_count else "bearish"
                direction_votes.append(majority_dir)
                idx_note = " 指数共振" if breadth.index_aligned else ""
                aligned.append(
                    f"广度: {breadth.aligned_count}/{breadth.total_count} 同向{idx_note}"
                )
            elif breadth.alignment_label == "divergent":
                conflicting.append(f"广度: 分化 ({breadth.details})")
            else:
                conflicting.append(f"广度: 混合 ({breadth.details})")

        # Direction consistency: all directional votes must agree
        # If there are conflicting directions, demote those signals
        final_aligned: list[str] = []
        final_conflicting = list(conflicting)

        if direction_votes:
            from collections import Counter
            vote_counts = Counter(direction_votes)
            dominant_dir, dominant_count = vote_counts.most_common(1)[0]
            minority_count = len(direction_votes) - dominant_count

            if minority_count > 0:
                # Some directional signals conflict — only count majority-aligned
                # Re-check each aligned item to see if its direction matches dominant
                for item in aligned:
                    # Macro is always direction-neutral
                    if item.startswith("宏观"):
                        final_aligned.append(item)
                        continue
                    # Gap direction check
                    if item.startswith("Gap") and gap_direction and gap_direction != dominant_dir:
                        final_conflicting.append(item + " (方向冲突)")
                        continue
                    # ORB direction check
                    if item.startswith("ORB") and orb and orb.breakout_direction != dominant_dir:
                        final_conflicting.append(item + " (方向冲突)")
                        continue
                    final_aligned.append(item)
            else:
                final_aligned = list(aligned)
        else:
            final_aligned = list(aligned)

        # Determine dominant direction
        if direction_votes:
            from collections import Counter
            dominant_dir = Counter(direction_votes).most_common(1)[0][0]
        else:
            dominant_dir = "neutral"

        aligned_count = len(final_aligned)

        # Base grade from aligned count (0-5 → D to A+)
        grade_map = {5: "A+", 4: "A", 3: "B+", 2: "B", 1: "C", 0: "D"}
        base_grade = grade_map.get(min(aligned_count, 5), "D")

        # VIX modifier
        if vix:
            if vix.signal == "caution":
                base_score = _GRADE_TO_SCORE.get(base_grade, 0)
                base_score = max(0, base_score - 1)
                base_grade = _GRADES[base_score]
                final_conflicting.append(
                    f"VIX: {vix.level:.1f} ({vix.change_pct:+.1f}%) 飙升"
                )
            elif vix.signal == "supportive":
                base_score = _GRADE_TO_SCORE.get(base_grade, 0)
                base_score = min(len(_GRADES) - 1, base_score + 1)
                base_grade = _GRADES[base_score]
                final_aligned.append(
                    f"VIX: {vix.level:.1f} ({vix.change_pct:+.1f}%) 回落"
                )

        # FOMC day cap
        fomc_max = grade_cfg.get("fomc_max_grade", "B")
        if macro_signal == "range_then_trend":
            event_cfg = grade_cfg.get("event_day", {})
            unlock_str = event_cfg.get("fomc_trend_unlock_time", "14:00")
            unlock_h, unlock_m = map(int, unlock_str.split(":"))
            if now_et.hour < unlock_h or (now_et.hour == unlock_h and now_et.minute < unlock_m):
                fomc_score = _GRADE_TO_SCORE.get(fomc_max, 2)
                current_score = _GRADE_TO_SCORE.get(base_grade, 0)
                if current_score > fomc_score:
                    base_grade = fomc_max

        grade_score = _GRADE_TO_SCORE.get(base_grade, 0)

        # Day type
        if macro_signal in ("range_then_trend", "data_reaction"):
            day_type = "event"
        elif base_grade in ("A+", "A"):
            day_type = "trend"
        else:
            day_type = "chop"

        # Confidence modifier
        conf_mod = conf_mods.get(base_grade, 0.0)
        if isinstance(conf_mod, str):
            conf_mod = float(conf_mod)

        # Position size hint
        pos_hint = _GRADE_POSITION.get(base_grade, "reduced")

        details_parts = [f"grade={base_grade}"]
        details_parts.append(f"aligned={aligned_count}/5")
        if macro_signal != "clear":
            details_parts.append(f"macro={macro_signal}")
        if vix:
            details_parts.append(f"VIX={vix.level:.1f}")

        return MarketTone(
            grade=base_grade,
            grade_score=grade_score,
            direction=dominant_dir,
            day_type=day_type,
            confidence_modifier=conf_mod,
            position_size_hint=pos_hint,
            macro_signal=macro_signal,
            gap_signal=gap_signal,
            gap_pct=gap_pct,
            vix=vix,
            orb=orb,
            vwap_status=vwap_status,
            breadth=breadth,
            components_aligned=final_aligned,
            components_conflicting=final_conflicting,
            computed_at=now_et,
            details="; ".join(details_parts),
        )
