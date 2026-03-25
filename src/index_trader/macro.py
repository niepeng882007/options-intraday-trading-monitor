"""宏观三剑客分析 — VIX + TNX + UUP → Signal。"""

from __future__ import annotations

from src.index_trader import MacroSnapshot, Signal, VIXRegime
from src.utils.logger import setup_logger

logger = setup_logger("index_macro")


class MacroAnalyzer:
    """分析 VIX/TNX/UUP 宏观状态，输出方向性 Signal。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config.get("macro", {})

    def analyze(self, snapshot: MacroSnapshot) -> Signal:
        """综合 VIX + TNX + UUP 输出单个宏观信号。"""
        sub_signals: list[tuple[str, float]] = []  # (direction, strength)

        # ── VIX 分析 ──
        vix_dir, vix_str = self._analyze_vix(snapshot)
        sub_signals.append((vix_dir, vix_str))

        # ── TNX 分析 ──
        tnx_dir, tnx_str = self._analyze_tnx(snapshot)
        sub_signals.append((tnx_dir, tnx_str))

        # ── UUP 分析 ──
        uup_dir, uup_str = self._analyze_uup(snapshot)
        sub_signals.append((uup_dir, uup_str))

        # ── 综合 ──
        bullish_sum = sum(s for d, s in sub_signals if d == "bullish")
        bearish_sum = sum(s for d, s in sub_signals if d == "bearish")

        if bullish_sum > bearish_sum:
            direction = "bullish"
            strength = min((bullish_sum - bearish_sum) / 3.0, 1.0)
        elif bearish_sum > bullish_sum:
            direction = "bearish"
            strength = min((bearish_sum - bullish_sum) / 3.0, 1.0)
        else:
            direction = "neutral"
            strength = 0.0

        reasons = []
        if snapshot.vix_regime in (VIXRegime.HIGH, VIXRegime.EXTREME):
            reasons.append(f"VIX {snapshot.vix_regime.value}({snapshot.vix_current:.1f})")
        elif snapshot.vix_regime == VIXRegime.LOW:
            reasons.append(f"VIX low({snapshot.vix_current:.1f})")
        if abs(snapshot.tnx_change_bps) >= self._cfg.get("tnx", {}).get("surge_threshold_bps", 5):
            reasons.append(f"TNX {snapshot.tnx_change_bps:+.1f}bps")
        if snapshot.dxy_direction != "flat":
            reasons.append(f"DXY {snapshot.dxy_direction}")

        return Signal(
            source="macro",
            direction=direction,
            strength=round(strength, 3),
            reason=", ".join(reasons) if reasons else "宏观中性",
        )

    def _analyze_vix(self, snap: MacroSnapshot) -> tuple[str, float]:
        """VIX 偏低 → bullish，偏高 → bearish。"""
        if snap.vix_regime == VIXRegime.EXTREME:
            return "bearish", 1.0
        if snap.vix_regime == VIXRegime.HIGH:
            return "bearish", 0.7
        if snap.vix_regime == VIXRegime.LOW:
            return "bullish", 0.5
        return "neutral", 0.0

    def _analyze_tnx(self, snap: MacroSnapshot) -> tuple[str, float]:
        """TNX 突变：利率大涨 → bearish（股票承压），大跌 → bullish。"""
        threshold = self._cfg.get("tnx", {}).get("surge_threshold_bps", 5)
        if snap.tnx_change_bps >= threshold:
            return "bearish", min(abs(snap.tnx_change_bps) / 10, 1.0)
        if snap.tnx_change_bps <= -threshold:
            return "bullish", min(abs(snap.tnx_change_bps) / 10, 1.0)
        return "neutral", 0.0

    def _analyze_uup(self, snap: MacroSnapshot) -> tuple[str, float]:
        """美元走强 → bearish（风险资产承压），走弱 → bullish。"""
        if snap.dxy_direction == "strong":
            return "bearish", 0.4
        if snap.dxy_direction == "weak":
            return "bullish", 0.4
        return "neutral", 0.0
