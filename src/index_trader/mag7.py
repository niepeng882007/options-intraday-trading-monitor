"""Mag7 温度计 — 7 股方向一致性 + 绑架检测 → Signal。"""

from __future__ import annotations

from src.index_trader import Mag7Snapshot, Mag7Stock, Signal
from src.utils.logger import setup_logger

logger = setup_logger("index_mag7")


class Mag7Analyzer:
    """分析 Mag7 的方向一致性和异常股检测。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config.get("mag7", {})

    def analyze(
        self,
        stocks: list[Mag7Stock],
        index_avg_change: float,
    ) -> tuple[Mag7Snapshot, Signal]:
        """分析 7 股温度计，返回 (snapshot, signal)。"""
        if not stocks:
            empty = Mag7Snapshot(
                stocks=[], bullish_count=0, bearish_count=0,
                avg_change_pct=0.0, consistency_score=0.0,
            )
            return empty, Signal(
                source="mag7", direction="neutral", strength=0.0,
                reason="无 Mag7 数据",
            )

        bullish = [s for s in stocks if s.change_pct > 0.05]
        bearish = [s for s in stocks if s.change_pct < -0.05]
        total = len(stocks)
        avg_change = sum(s.change_pct for s in stocks) / total

        # 一致性得分：全部同向 = 1.0, 完全分化 = 0.0
        majority = max(len(bullish), len(bearish))
        consistency = majority / total if total > 0 else 0.0

        # 绑架检测：单股偏离 > 指数均值 * kidnap_ratio
        kidnap_ratio = self._cfg.get("kidnap_ratio", 3.0)
        is_kidnapped = False
        kidnap_detail = ""
        if abs(index_avg_change) > 0.05:
            for s in stocks:
                if abs(s.change_pct) > abs(index_avg_change) * kidnap_ratio:
                    is_kidnapped = True
                    kidnap_detail = f"{s.code} {s.change_pct:+.2f}% 偏离指数均值 {index_avg_change:+.2f}%"
                    break

        snapshot = Mag7Snapshot(
            stocks=stocks,
            bullish_count=len(bullish),
            bearish_count=len(bearish),
            avg_change_pct=round(avg_change, 3),
            consistency_score=round(consistency, 2),
            is_kidnapped=is_kidnapped,
            kidnap_detail=kidnap_detail,
        )

        # 生成信号
        direction, strength, reason = self._derive_signal(snapshot)

        signal = Signal(
            source="mag7",
            direction=direction,
            strength=round(strength, 3),
            reason=reason,
        )
        return snapshot, signal

    def _derive_signal(self, snap: Mag7Snapshot) -> tuple[str, float, str]:
        """从 Mag7 快照推导方向信号。"""
        total = len(snap.stocks)
        if total == 0:
            return "neutral", 0.0, "无数据"

        if snap.is_kidnapped:
            # 绑架时降低信号可信度
            return "neutral", 0.3, f"⚠ 绑架: {snap.kidnap_detail}"

        if snap.consistency_score >= 0.85:
            # 高一致性 — 强信号
            if snap.bullish_count > snap.bearish_count:
                return "bullish", 0.8, f"{snap.bullish_count}/{total} 看涨, 一致性 {snap.consistency_score:.0%}"
            return "bearish", 0.8, f"{snap.bearish_count}/{total} 看跌, 一致性 {snap.consistency_score:.0%}"

        if snap.consistency_score >= 0.57:
            # 多数同向 — 中等信号
            if snap.bullish_count > snap.bearish_count:
                return "bullish", 0.5, f"{snap.bullish_count}/{total} 看涨"
            if snap.bearish_count > snap.bullish_count:
                return "bearish", 0.5, f"{snap.bearish_count}/{total} 看跌"

        return "neutral", 0.2, f"Mag7 分化 ({snap.bullish_count}涨/{snap.bearish_count}跌)"
