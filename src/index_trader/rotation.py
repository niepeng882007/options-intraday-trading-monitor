"""板块轮动分析 — QQQ/SPY/IWM 相对强弱 → Signal。"""

from __future__ import annotations

from src.index_trader import IndexQuote, RotationScenario, RotationSnapshot, Signal
from src.utils.logger import setup_logger

logger = setup_logger("index_rotation")


class RotationAnalyzer:
    """分析 QQQ/SPY/IWM 的相对强弱和轮动场景。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config.get("rotation", {})

    def analyze(self, indices: list[IndexQuote]) -> tuple[RotationSnapshot, Signal]:
        """分析三大指数轮动，返回 (snapshot, signal)。"""
        if not indices:
            empty_snap = RotationSnapshot(
                indices=[], leader="", laggard="", spread_pct=0.0,
                scenario=RotationScenario.SYNC,
            )
            return empty_snap, Signal(
                source="rotation", direction="neutral", strength=0.0,
                reason="无指数数据",
            )

        # 按涨跌幅排序
        sorted_idx = sorted(indices, key=lambda x: x.change_pct, reverse=True)
        leader = sorted_idx[0]
        laggard = sorted_idx[-1]
        spread = leader.change_pct - laggard.change_pct

        # 判定场景
        sync_threshold = self._cfg.get("sync_threshold_pct", 0.2)
        spread_threshold = self._cfg.get("spread_threshold_pct", 1.0)

        if spread < sync_threshold:
            scenario = RotationScenario.SYNC
        elif spread >= spread_threshold:
            scenario = RotationScenario.DIVERGE
        else:
            scenario = RotationScenario.SEESAW

        snapshot = RotationSnapshot(
            indices=sorted_idx,
            leader=leader.symbol,
            laggard=laggard.symbol,
            spread_pct=round(spread, 3),
            scenario=scenario,
        )

        # 生成信号
        direction, strength, reason = self._derive_signal(snapshot)

        signal = Signal(
            source="rotation",
            direction=direction,
            strength=round(strength, 3),
            reason=reason,
        )
        return snapshot, signal

    def _derive_signal(self, snap: RotationSnapshot) -> tuple[str, float, str]:
        """从轮动快照推导方向信号。"""
        if snap.scenario == RotationScenario.SYNC:
            # 三大指数同步 — 方向由均值决定
            avg_change = sum(i.change_pct for i in snap.indices) / len(snap.indices)
            if avg_change > 0.1:
                return "bullish", min(abs(avg_change) / 1.0, 0.8), f"三指同步涨 {avg_change:+.2f}%"
            if avg_change < -0.1:
                return "bearish", min(abs(avg_change) / 1.0, 0.8), f"三指同步跌 {avg_change:+.2f}%"
            return "neutral", 0.0, "三指同步但变动微弱"

        if snap.scenario == RotationScenario.DIVERGE:
            # 明显分化 — 偏中性，冲突信号
            return "neutral", 0.3, f"{snap.leader}领先/{snap.laggard}落后 spread={snap.spread_pct:.2f}%"

        # SEESAW — 根据领先者推导风格偏好
        if snap.leader == "IWM":
            return "bullish", 0.5, "IWM 领先 → risk-on"
        if snap.leader == "QQQ":
            direction = "bullish" if self._avg_change(snap) > 0 else "neutral"
            return direction, 0.4, "QQQ 领先 → growth 偏好"
        return "neutral", 0.2, f"{snap.leader} 领先（温和轮动）"

    @staticmethod
    def _avg_change(snap: RotationSnapshot) -> float:
        if not snap.indices:
            return 0.0
        return sum(i.change_pct for i in snap.indices) / len(snap.indices)
