"""Regime Stabilizer — debounce regime oscillations for auto-scan L1.

Prevents noisy TREND_STRONG ↔ RANGE flips when RVOL is near thresholds.
Only used in L1 scan path; on-demand and L2 use raw regime classification.
"""

from __future__ import annotations

import time
from copy import copy
from dataclasses import dataclass

from src.us_playbook import RegimeFamily, USRegimeResult, USRegimeType
from src.utils.logger import setup_logger

logger = setup_logger("regime_stabilizer")

# Regime strength ordering for upgrade/downgrade detection
_REGIME_STRENGTH: dict[USRegimeType, int] = {
    USRegimeType.UNCLEAR: 0,
    USRegimeType.NARROW_GRIND: 1,
    USRegimeType.RANGE: 1,
    USRegimeType.TREND_WEAK: 2,
    USRegimeType.TREND_STRONG: 2,
    USRegimeType.GAP_GO: 3,
    USRegimeType.V_REVERSAL: 2,
    USRegimeType.GAP_FILL: 1,
}


@dataclass
class _Entry:
    """Stored state for a symbol's last accepted regime."""
    regime: USRegimeResult
    accepted_at: float  # unix timestamp


class RegimeStabilizer:
    """Two-layer regime debounce filter.

    Layer 1 — Hysteresis: RVOL must cross threshold ± buffer to switch.
    Layer 2 — Temporal hold: regime must persist for min duration.
    """

    def __init__(self, cfg: dict) -> None:
        self._enabled = cfg.get("enabled", False)
        self._hysteresis_ratio = cfg.get("hysteresis_ratio", 0.30)
        self._hold_upgrade_min = cfg.get("hold_upgrade_minutes", 15)
        self._hold_downgrade_min = cfg.get("hold_downgrade_minutes", 30)
        self._hold_from_unclear_min = cfg.get("hold_from_unclear_minutes", 10)
        self._bypass_delta = cfg.get("bypass_confidence_delta", 0.20)
        self._unclear_timeout_min = cfg.get("unclear_timeout_minutes", 60)
        self._state: dict[str, _Entry] = {}

    def stabilize(self, symbol: str, raw: USRegimeResult) -> USRegimeResult:
        """Apply stabilization to a raw regime result.

        Returns the original or a stabilized copy with ``stabilized=True``.
        """
        if not self._enabled:
            return raw

        now = time.time()
        entry = self._state.get(symbol)

        # First time seeing this symbol — accept raw
        if entry is None:
            self._state[symbol] = _Entry(regime=raw, accepted_at=now)
            return raw

        prev = entry.regime

        # Same regime — refresh price/rvol, reset timer
        if raw.regime == prev.regime:
            # UNCLEAR timeout: force reclassify after extended unclear period
            if (
                raw.regime == USRegimeType.UNCLEAR
                and self._unclear_timeout_min > 0
            ):
                elapsed_min = (now - entry.accepted_at) / 60.0
                if elapsed_min >= self._unclear_timeout_min:
                    forced = self._force_reclassify(raw)
                    if forced is not None:
                        logger.info(
                            "UNCLEAR timeout %s: %.0fmin >= %dmin, forced → %s",
                            symbol, elapsed_min, self._unclear_timeout_min,
                            forced.regime.value,
                        )
                        self._state[symbol] = _Entry(regime=forced, accepted_at=now)
                        return forced
                # Don't reset accepted_at for UNCLEAR — preserve original timestamp
                entry.regime = raw
                return raw
            entry.regime = raw
            entry.accepted_at = now
            return raw

        # Strong bypass: confidence delta >= threshold
        if abs(raw.confidence - prev.confidence) >= self._bypass_delta:
            logger.debug(
                "Stabilizer bypass %s: conf delta %.2f >= %.2f, %s → %s",
                symbol, abs(raw.confidence - prev.confidence),
                self._bypass_delta, prev.regime.value, raw.regime.value,
            )
            self._state[symbol] = _Entry(regime=raw, accepted_at=now)
            return raw

        # Layer 1: Hysteresis (only if adaptive thresholds available)
        if not self._check_hysteresis(prev, raw):
            return self._hold(entry, raw, symbol)

        # Layer 2: Temporal hold
        hold_minutes = self._get_hold_minutes(prev, raw)
        elapsed = (now - entry.accepted_at) / 60.0
        if elapsed < hold_minutes:
            logger.debug(
                "Stabilizer hold %s: %s → %s, elapsed %.1fmin < hold %.0fmin",
                symbol, prev.regime.value, raw.regime.value, elapsed, hold_minutes,
            )
            return self._hold(entry, raw, symbol)

        # Passed both layers — accept transition
        logger.debug(
            "Stabilizer accept %s: %s → %s after %.1fmin",
            symbol, prev.regime.value, raw.regime.value, elapsed,
        )
        self._state[symbol] = _Entry(regime=raw, accepted_at=now)
        return raw

    def reset(self) -> None:
        """Clear all stored state (called on daily reset / close)."""
        self._state.clear()

    def _check_hysteresis(self, prev: USRegimeResult, raw: USRegimeResult) -> bool:
        """Layer 1: RVOL hysteresis check.

        Returns True if the transition passes (RVOL is decisive enough).
        Returns True (pass) if no adaptive thresholds — skip Layer 1.
        """
        at = raw.adaptive_thresholds
        if not at:
            # No adaptive thresholds → skip Layer 1 entirely
            return True

        trend_th = at.get("trend_day", 0)
        fade_th = at.get("fade_chop", 0)
        if trend_th <= 0 or fade_th <= 0:
            return True

        gap = trend_th - fade_th
        hysteresis = gap * self._hysteresis_ratio

        # TREND/GAP → FADE: RVOL must drop below trend_th - hysteresis
        if (
            prev.regime.family == RegimeFamily.TREND
            and raw.regime.family == RegimeFamily.FADE
        ):
            threshold = trend_th - hysteresis
            if raw.rvol >= threshold:
                logger.debug(
                    "Hysteresis reject: RVOL %.2f >= %.2f (trend_th %.2f - hyst %.2f)",
                    raw.rvol, threshold, trend_th, hysteresis,
                )
                return False

        # FADE → TREND/GAP: RVOL must rise above trend_th + hysteresis
        if (
            prev.regime.family == RegimeFamily.FADE
            and raw.regime.family == RegimeFamily.TREND
        ):
            threshold = trend_th + hysteresis
            if raw.rvol < threshold:
                logger.debug(
                    "Hysteresis reject: RVOL %.2f < %.2f (trend_th %.2f + hyst %.2f)",
                    raw.rvol, threshold, trend_th, hysteresis,
                )
                return False

        return True

    def _get_hold_minutes(self, prev: USRegimeResult, raw: USRegimeResult) -> float:
        """Determine hold duration based on transition type."""
        if prev.regime == USRegimeType.UNCLEAR:
            return self._hold_from_unclear_min

        prev_strength = _REGIME_STRENGTH.get(prev.regime, 0)
        new_strength = _REGIME_STRENGTH.get(raw.regime, 0)

        if new_strength > prev_strength:
            return self._hold_upgrade_min
        return self._hold_downgrade_min

    def _force_reclassify(self, raw: USRegimeResult) -> USRegimeResult | None:
        """Force-reclassify an UNCLEAR regime after timeout.

        Heuristic priority:
        1. If lean is bullish/bearish → TREND_WEAK with that lean
        2. If RVOL < 0.5 → NARROW_GRIND
        3. Default → RANGE
        """
        if raw.regime != USRegimeType.UNCLEAR:
            return None

        forced = copy(raw)
        forced.stabilized = True

        if raw.lean in ("bullish", "bearish") and raw.confidence >= 0.30:
            forced.regime = USRegimeType.TREND_WEAK
            forced.confidence = max(0.35, raw.confidence)
            forced.details = f"UNCLEAR timeout → TREND_WEAK ({raw.lean}); {raw.details}"
        elif raw.rvol < 0.5:
            forced.regime = USRegimeType.NARROW_GRIND
            forced.confidence = 0.40
            forced.details = f"UNCLEAR timeout → NARROW_GRIND (RVOL {raw.rvol:.2f}); {raw.details}"
        else:
            forced.regime = USRegimeType.RANGE
            forced.confidence = 0.40
            forced.details = f"UNCLEAR timeout → RANGE; {raw.details}"

        return forced

    @staticmethod
    def _hold(entry: _Entry, raw: USRegimeResult, symbol: str) -> USRegimeResult:
        """Return a stabilized copy of the previous regime."""
        held = copy(entry.regime)
        # Refresh price/rvol from current observation
        held.price = raw.price
        held.rvol = raw.rvol
        held.stabilized = True
        return held
