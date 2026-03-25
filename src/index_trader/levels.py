"""关键点位聚合 + 价格位置信号 → Signal。"""

from __future__ import annotations

from src.index_trader import LevelMap, Signal
from src.utils.logger import setup_logger

logger = setup_logger("index_levels")


class LevelsAnalyzer:
    """从各指数的 LevelMap 中推导价格位置信号。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config
        self._proximity_pct = config.get("level_proximity_pct", 0.001)

    def analyze(self, levels: dict[str, LevelMap]) -> Signal:
        """综合所有指数点位生成信号。"""
        if not levels:
            return Signal(
                source="levels", direction="neutral", strength=0.0,
                reason="无点位数据",
            )

        sub_signals: list[tuple[str, float]] = []
        reasons: list[str] = []

        for sym, lm in levels.items():
            direction, strength, reason = self._analyze_single(lm)
            sub_signals.append((direction, strength))
            if reason:
                reasons.append(f"{sym}: {reason}")

        # 综合各标的
        bullish_sum = sum(s for d, s in sub_signals if d == "bullish")
        bearish_sum = sum(s for d, s in sub_signals if d == "bearish")
        total = len(sub_signals)

        if bullish_sum > bearish_sum:
            direction = "bullish"
            strength = min((bullish_sum - bearish_sum) / total, 1.0)
        elif bearish_sum > bullish_sum:
            direction = "bearish"
            strength = min((bearish_sum - bullish_sum) / total, 1.0)
        else:
            direction = "neutral"
            strength = 0.0

        return Signal(
            source="levels",
            direction=direction,
            strength=round(strength, 3),
            reason="; ".join(reasons) if reasons else "价位中性",
        )

    def _analyze_single(self, lm: LevelMap) -> tuple[str, float, str]:
        """分析单个标的的价格位置。"""
        price = lm.current_price
        if price <= 0:
            return "neutral", 0.0, ""

        # 优先级：PMH/PML > PDH/PDL > VAH/VAL > 周线

        # 盘前高点上方 → bullish
        if lm.pmh > 0 and price > lm.pmh:
            return "bullish", 0.7, f"价格在 PMH({lm.pmh:.2f}) 上方"

        # 盘前低点下方 → bearish
        if lm.pml > 0 and price < lm.pml:
            return "bearish", 0.7, f"价格在 PML({lm.pml:.2f}) 下方"

        # PDH 上方 → bullish
        if lm.pdh > 0 and price > lm.pdh:
            return "bullish", 0.6, f"价格在 PDH({lm.pdh:.2f}) 上方"

        # PDL 下方 → bearish
        if lm.pdl > 0 and price < lm.pdl:
            return "bearish", 0.6, f"价格在 PDL({lm.pdl:.2f}) 下方"

        # VAH 附近 → 偏空（阻力）
        if lm.vah > 0 and self._near(price, lm.vah):
            return "bearish", 0.3, f"价格接近 VAH({lm.vah:.2f})"

        # VAL 附近 → 偏多（支撑）
        if lm.val > 0 and self._near(price, lm.val):
            return "bullish", 0.3, f"价格接近 VAL({lm.val:.2f})"

        # POC 附近 → 中性
        if lm.poc > 0 and self._near(price, lm.poc):
            return "neutral", 0.1, f"价格在 POC({lm.poc:.2f}) 附近"

        return "neutral", 0.0, ""

    def _near(self, price: float, level: float) -> bool:
        """判断价格是否接近某个价位。"""
        if level <= 0:
            return False
        return abs(price - level) / level <= self._proximity_pct
