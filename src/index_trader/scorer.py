"""置信度评分引擎 — 5 信号加权 → ConfidenceReport。

Risk 不参与评分（下游输出）。
"""

from __future__ import annotations

from src.index_trader import ConfidenceReport, Signal
from src.utils.logger import setup_logger

logger = setup_logger("index_scorer")

DEFAULT_WEIGHTS = {
    "macro": 0.25,
    "rotation": 0.20,
    "mag7": 0.15,
    "levels": 0.15,
    "script": 0.25,
}


class ConfidenceScorer:
    """加权评分：按方向分组，取净信号强度。"""

    def __init__(self, config: dict) -> None:
        cfg = config.get("confidence", {})
        self._weights = cfg.get("weights", DEFAULT_WEIGHTS)
        self._grade_thresholds = cfg.get("grade_thresholds", {
            "A": {"min_score": 75, "min_resonance": 4},
            "B": {"min_score": 60, "min_resonance": 3},
            "C": {"min_score": 40, "min_resonance": 0},
        })

    def score(self, signals: list[Signal]) -> ConfidenceReport:
        """从 5 个方向性信号计算置信度报告。"""
        # 填充权重
        for s in signals:
            s.weight = self._weights.get(s.source, 0.0)

        # 按方向分组计算
        bullish_score = 0.0
        bearish_score = 0.0

        for s in signals:
            module_score = s.strength * s.weight * 100
            if s.direction == "bullish":
                bullish_score += module_score
            elif s.direction == "bearish":
                bearish_score += module_score
            # neutral 不计入任何方向

        # 总分 = 全部模块加权综合（不区分方向）
        total_score = sum(s.strength * s.weight * 100 for s in signals)

        # 主导方向
        if bullish_score > bearish_score:
            direction = "bullish"
            direction_pct = bullish_score / (bullish_score + bearish_score) if (bullish_score + bearish_score) > 0 else 0.0
        elif bearish_score > bullish_score:
            direction = "bearish"
            direction_pct = bearish_score / (bullish_score + bearish_score) if (bullish_score + bearish_score) > 0 else 0.0
        else:
            direction = "neutral"
            direction_pct = 0.0

        # 共振数：同方向且 strength > 0
        resonance = sum(
            1 for s in signals
            if s.direction == direction and s.strength > 0
        )

        # 冲突检测：存在反向强信号
        opposite = "bearish" if direction == "bullish" else "bullish"
        conflict_signals = [s for s in signals if s.direction == opposite and s.strength >= 0.7]
        has_conflict = len(conflict_signals) > 0
        conflict_detail = ", ".join(f"{s.source}({s.direction})" for s in conflict_signals) if has_conflict else ""

        # 等级
        grade = self._compute_grade(total_score, resonance)

        return ConfidenceReport(
            signals=signals,
            total_score=round(total_score, 1),
            bullish_score=round(bullish_score, 1),
            bearish_score=round(bearish_score, 1),
            direction=direction,
            direction_pct=round(direction_pct, 3),
            resonance_count=resonance,
            confidence_grade=grade,
            has_conflict=has_conflict,
            conflict_detail=conflict_detail,
        )

    def _compute_grade(self, score: float, resonance: int) -> str:
        """A/B/C/D 等级判定。"""
        for grade in ["A", "B", "C"]:
            thresholds = self._grade_thresholds.get(grade, {})
            if score >= thresholds.get("min_score", 100) and resonance >= thresholds.get("min_resonance", 99):
                return grade
        return "D"
