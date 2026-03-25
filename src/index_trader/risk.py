"""风控参数计算 — 独立输出，不参与评分。"""

from __future__ import annotations

from src.index_trader import (
    ConfidenceReport,
    MacroSnapshot,
    RiskParams,
    VIXRegime,
    VolatilityRegime,
)
from src.utils.logger import setup_logger

logger = setup_logger("index_risk")


class RiskCalculator:
    """根据 VIX regime 和置信度输出风控参数。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config.get("risk", {})

    def calculate(self, macro: MacroSnapshot, confidence: ConfidenceReport) -> RiskParams:
        """计算当日风控参数。"""
        # VIX regime → 波动率分类
        if macro.vix_regime in (VIXRegime.HIGH, VIXRegime.EXTREME):
            vol_regime = VolatilityRegime.HIGH
            params = self._cfg.get("high_volatility", {})
        else:
            vol_regime = VolatilityRegime.NORMAL
            params = self._cfg.get("normal", {})

        # 低置信度额外收紧
        max_daily = params.get("max_daily_loss_pct", 2.0)
        max_single = params.get("max_single_risk_pct", 1.0)

        if confidence.confidence_grade == "D":
            max_daily *= 0.5
            max_single *= 0.5
        elif confidence.confidence_grade == "C":
            max_daily *= 0.75
            max_single *= 0.75

        return RiskParams(
            volatility_regime=vol_regime,
            max_daily_loss_pct=round(max_daily, 2),
            max_single_risk_pct=round(max_single, 2),
            circuit_breaker_count=params.get("circuit_breaker_count", 3),
            cooldown_minutes=params.get("cooldown_minutes", 30),
        )
