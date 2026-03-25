"""开盘剧本引擎 — 4 种规则判定 → Signal。

Gap Fill / Gap and Go / Chop / Reversal
"""

from __future__ import annotations

from src.index_trader import (
    MacroSnapshot,
    Mag7Snapshot,
    RotationScenario,
    RotationSnapshot,
    ScriptCondition,
    ScriptJudgment,
    ScriptType,
    Signal,
    VIXRegime,
)
from src.utils.logger import setup_logger

logger = setup_logger("index_scenario")


class ScenarioEngine:
    """基于规则的开盘剧本判定引擎。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config.get("script", {})
        self._gap_threshold = self._cfg.get("gap_threshold_pct", 0.3)

    def judge(
        self,
        macro: MacroSnapshot,
        rotation: RotationSnapshot,
        mag7: Mag7Snapshot,
        gap_pct: float,
        calendar_events: list[str],
    ) -> tuple[ScriptJudgment, Signal]:
        """判定最可能的开盘剧本，返回 (judgment, signal)。"""
        # 计算各剧本得分
        candidates = [
            (ScriptType.GAP_AND_GO, *self._eval_gap_and_go(macro, rotation, mag7, gap_pct)),
            (ScriptType.GAP_FILL, *self._eval_gap_fill(macro, rotation, mag7, gap_pct)),
            (ScriptType.REVERSAL, *self._eval_reversal(macro, rotation, mag7, gap_pct)),
            (ScriptType.CHOP, *self._eval_chop(macro, rotation, gap_pct, calendar_events)),
        ]

        # 按命中条件数排序，同分时按优先级：GAP_AND_GO > REVERSAL > GAP_FILL > CHOP
        priority = {ScriptType.GAP_AND_GO: 0, ScriptType.REVERSAL: 1, ScriptType.GAP_FILL: 2, ScriptType.CHOP: 3}
        candidates.sort(key=lambda x: (-x[2], priority.get(x[0], 99)))

        primary = candidates[0]
        alternatives = [(c[0], c[2]) for c in candidates[1:] if c[2] > 0]

        judgment = ScriptJudgment(
            primary_script=primary[0],
            primary_conditions=primary[1],
            primary_hit_count=primary[2],
            alternatives=alternatives,
        )

        # 辅助条件不足时降级为中性信号
        if primary[2] < 2:
            signal = Signal(
                source="script",
                direction="neutral",
                strength=0.1,
                reason=f"条件不充分 ({primary[0].value} 仅命中 {primary[2]})",
            )
            return judgment, signal

        # 信号
        direction, strength = self._script_to_signal(primary[0], gap_pct, primary[2])
        reason = f"{primary[0].value} (命中 {primary[2]} 条件)"

        signal = Signal(
            source="script",
            direction=direction,
            strength=round(strength, 3),
            reason=reason,
        )
        return judgment, signal

    # ── 4 种剧本评估 ──

    def _eval_gap_and_go(
        self, macro: MacroSnapshot, rotation: RotationSnapshot,
        mag7: Mag7Snapshot, gap_pct: float,
    ) -> tuple[list[ScriptCondition], int]:
        """Gap and Go: gap > 0.3% + VIX 偏低 + gap = Mag7 方向 + 指数同步。"""
        conditions = []
        hit = 0

        # 前提：gap > threshold
        has_gap = abs(gap_pct) > self._gap_threshold
        conditions.append(ScriptCondition("gap > 0.3%", has_gap, f"gap={gap_pct:+.2f}%", is_prerequisite=True))
        if not has_gap:
            return conditions, 0

        # 辅助 1: VIX 偏离 MA10 < 0（走低）
        vix_below_ma = macro.vix_deviation_pct < 0
        conditions.append(ScriptCondition("VIX偏离MA10&lt;0", vix_below_ma, f"deviation={macro.vix_deviation_pct:+.1%}"))
        if vix_below_ma:
            hit += 1

        # 辅助 2: gap 方向 = Mag7 方向
        gap_dir = "bullish" if gap_pct > 0 else "bearish"
        mag7_dir = "bullish" if mag7.avg_change_pct > 0.05 else ("bearish" if mag7.avg_change_pct < -0.05 else "neutral")
        aligned = gap_dir == mag7_dir
        conditions.append(ScriptCondition("gap=Mag7方向", aligned, f"gap:{gap_dir} mag7:{mag7_dir}"))
        if aligned:
            hit += 1

        # 辅助 3: 指数同步
        is_sync = rotation.scenario == RotationScenario.SYNC
        conditions.append(ScriptCondition("指数同步", is_sync, f"scenario={rotation.scenario.value}"))
        if is_sync:
            hit += 1

        return conditions, hit

    def _eval_gap_fill(
        self, macro: MacroSnapshot, rotation: RotationSnapshot,  # noqa: ARG002
        mag7: Mag7Snapshot, gap_pct: float,
    ) -> tuple[list[ScriptCondition], int]:
        """Gap Fill: gap > 0.3% + VIX 偏离 < 15% + gap ≠ Mag7 方向 + PM 量正常。"""
        conditions = []
        hit = 0

        has_gap = abs(gap_pct) > self._gap_threshold
        conditions.append(ScriptCondition("gap > 0.3%", has_gap, f"gap={gap_pct:+.2f}%", is_prerequisite=True))
        if not has_gap:
            return conditions, 0

        # VIX 偏离温和
        vix_moderate = abs(macro.vix_deviation_pct) < 0.15
        conditions.append(ScriptCondition("VIX偏离&lt;15%", vix_moderate, f"deviation={macro.vix_deviation_pct:.1%}"))
        if vix_moderate:
            hit += 1

        # gap ≠ Mag7 方向
        gap_dir = "bullish" if gap_pct > 0 else "bearish"
        mag7_dir = "bullish" if mag7.avg_change_pct > 0.05 else ("bearish" if mag7.avg_change_pct < -0.05 else "neutral")
        not_aligned = gap_dir != mag7_dir
        conditions.append(ScriptCondition("gap≠Mag7方向", not_aligned, f"gap:{gap_dir} mag7:{mag7_dir}"))
        if not_aligned:
            hit += 1

        # PM 量正常（无异常）
        no_anomaly = not any(s.is_anomaly for s in mag7.stocks)
        conditions.append(ScriptCondition("PM量正常", no_anomaly))
        if no_anomaly:
            hit += 1

        return conditions, hit

    def _eval_reversal(
        self, macro: MacroSnapshot, rotation: RotationSnapshot,
        mag7: Mag7Snapshot, gap_pct: float,
    ) -> tuple[list[ScriptCondition], int]:
        """Reversal: gap > 0.3% + gap ≠ Mag7 + VIX 偏高 + 指数分化。"""
        conditions = []
        hit = 0

        has_gap = abs(gap_pct) > self._gap_threshold
        conditions.append(ScriptCondition("gap > 0.3%", has_gap, f"gap={gap_pct:+.2f}%", is_prerequisite=True))
        if not has_gap:
            return conditions, 0

        # gap ≠ Mag7
        gap_dir = "bullish" if gap_pct > 0 else "bearish"
        mag7_dir = "bullish" if mag7.avg_change_pct > 0.05 else ("bearish" if mag7.avg_change_pct < -0.05 else "neutral")
        not_aligned = gap_dir != mag7_dir
        conditions.append(ScriptCondition("gap≠Mag7方向", not_aligned, f"gap:{gap_dir} mag7:{mag7_dir}"))
        if not_aligned:
            hit += 1

        # VIX 偏高
        vix_high = macro.vix_regime in (VIXRegime.HIGH, VIXRegime.EXTREME)
        conditions.append(ScriptCondition("VIX偏高", vix_high, f"VIX={macro.vix_regime.value}"))
        if vix_high:
            hit += 1

        # 指数分化
        is_diverge = rotation.scenario in (RotationScenario.DIVERGE, RotationScenario.SEESAW)
        conditions.append(ScriptCondition("指数分化", is_diverge, f"scenario={rotation.scenario.value}"))
        if is_diverge:
            hit += 1

        return conditions, hit

    def _eval_chop(
        self, macro: MacroSnapshot, rotation: RotationSnapshot,
        gap_pct: float, calendar_events: list[str],
    ) -> tuple[list[ScriptCondition], int]:
        """Chop: gap ≤ 0.3% + PM 窄幅 + VIX 中性 + 无重大数据。"""
        conditions = []
        hit = 0

        small_gap = abs(gap_pct) <= self._gap_threshold
        conditions.append(ScriptCondition("gap ≤ 0.3%", small_gap, f"gap={gap_pct:+.2f}%", is_prerequisite=True))
        if not small_gap:
            return conditions, 0

        # VIX 中性
        vix_neutral = macro.vix_regime == VIXRegime.NORMAL
        conditions.append(ScriptCondition("VIX中性", vix_neutral, f"VIX={macro.vix_regime.value}"))
        if vix_neutral:
            hit += 1

        # 无重大日历事件
        no_events = len(calendar_events) == 0
        conditions.append(ScriptCondition("无重大数据", no_events, f"events={len(calendar_events)}"))
        if no_events:
            hit += 1

        # 指数低波动
        low_spread = rotation.spread_pct < 0.5
        conditions.append(ScriptCondition("指数低波动", low_spread, f"spread={rotation.spread_pct:.2f}%"))
        if low_spread:
            hit += 1

        return conditions, hit

    # ── 信号推导 ──

    @staticmethod
    def _script_to_signal(script: ScriptType, gap_pct: float, hit_count: int) -> tuple[str, float]:
        """将剧本判定转为方向信号。"""
        base_strength = min(hit_count / 3.0, 1.0)

        if script == ScriptType.GAP_AND_GO:
            direction = "bullish" if gap_pct > 0 else "bearish"
            return direction, base_strength * 0.9

        if script == ScriptType.GAP_FILL:
            # Gap fill 反向
            direction = "bearish" if gap_pct > 0 else "bullish"
            return direction, base_strength * 0.7

        if script == ScriptType.REVERSAL:
            direction = "bearish" if gap_pct > 0 else "bullish"
            return direction, base_strength * 0.6

        # CHOP — 中性
        return "neutral", base_strength * 0.3
