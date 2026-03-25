"""Index Trader AI — 指数日内交易盘前分析系统。

所有 dataclass 集中定义于此。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


# ── 核心评分信号 ──


@dataclass
class Signal:
    """单个分析模块输出的方向性信号。"""
    source: str              # "macro" / "rotation" / "mag7" / "levels" / "script"
    direction: str           # "bullish" / "bearish" / "neutral"
    strength: float          # 0.0 ~ 1.0
    reason: str
    weight: float = 0.0      # 由 scorer 根据配置填充


# ── 宏观快照 ──


class VIXRegime(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass
class MacroSnapshot:
    """VIX + TNX + UUP 宏观状态。"""
    vix_current: float
    vix_prev_close: float
    vix_ma10: float
    vix_deviation_pct: float          # (current - MA10) / MA10
    vix_regime: VIXRegime
    tnx_current: float                # 10Y yield %
    tnx_prev_close: float
    tnx_change_bps: float             # basis points
    uup_current: float
    uup_prev_close: float
    uup_change_pct: float
    dxy_direction: str                # "strong" / "weak" / "flat"
    timestamp: float = 0.0

    @property
    def is_valid(self) -> bool:
        """VIX < 1 视为数据异常（yfinance 获取失败返回零值）。"""
        return self.vix_current >= 1.0


# ── 轮动快照 ──


@dataclass
class IndexQuote:
    """单个指数/ETF 的报价快照。"""
    symbol: str
    price: float
    prev_close: float
    change_pct: float
    volume: int = 0
    premarket_high: float = 0.0
    premarket_low: float = 0.0
    gap_pct: float = 0.0              # open vs prev_close


class RotationScenario(Enum):
    SYNC = "sync"                     # 三大指数同步
    SEESAW = "seesaw"                 # 跷跷板（科技 vs 小盘）
    DIVERGE = "diverge"               # 明显分化


@dataclass
class RotationSnapshot:
    """QQQ/SPY/IWM 板块轮动状态。"""
    indices: list[IndexQuote]
    leader: str                       # symbol of strongest
    laggard: str                      # symbol of weakest
    spread_pct: float                 # leader - laggard change_pct
    scenario: RotationScenario


# ── Mag7 快照 ──


@dataclass
class Mag7Stock:
    """单个 Mag7 股票的盘前状态。"""
    code: str
    price: float
    change_pct: float
    volume: int = 0
    volume_ratio: float = 0.0        # vs 5-day avg
    is_anomaly: bool = False          # volume_ratio > threshold


@dataclass
class Mag7Snapshot:
    """7 股方向温度计。"""
    stocks: list[Mag7Stock]
    bullish_count: int
    bearish_count: int
    avg_change_pct: float
    consistency_score: float          # 0.0 (分化) ~ 1.0 (全同向)
    is_kidnapped: bool = False
    kidnap_detail: str = ""


# ── 点位集 ──


@dataclass
class LevelMap:
    """单个标的的关键价位汇总。"""
    symbol: str
    current_price: float
    pdc: float                        # previous day close
    pdh: float
    pdl: float
    pmh: float                        # premarket high
    pml: float                        # premarket low
    weekly_high: float = 0.0
    weekly_low: float = 0.0
    poc: float = 0.0
    vah: float = 0.0
    val: float = 0.0
    gamma_call_wall: float = 0.0
    gamma_put_wall: float = 0.0
    vwap: float = 0.0


# ── 剧本判断 ──


class ScriptType(Enum):
    GAP_FILL = "gap_fill"
    GAP_AND_GO = "gap_and_go"
    CHOP = "chop"
    REVERSAL = "reversal"


@dataclass
class ScriptCondition:
    """单个辅助条件的判定。"""
    name: str
    met: bool
    detail: str = ""
    is_prerequisite: bool = False     # 前提条件（不计入 hit）


@dataclass
class ScriptJudgment:
    """开盘剧本判定结果。"""
    primary_script: ScriptType
    primary_conditions: list[ScriptCondition]
    primary_hit_count: int
    alternatives: list[tuple[ScriptType, int]]   # (script, hit_count)


# ── 置信度报告 ──


@dataclass
class ConfidenceReport:
    """加权评分结果。"""
    signals: list[Signal]
    total_score: float                # 0-100
    bullish_score: float
    bearish_score: float
    direction: str                    # "bullish" / "bearish" / "neutral"
    direction_pct: float              # 主导方向的占比
    resonance_count: int              # 同方向且 strength > 0 的信号数
    confidence_grade: str             # "A" / "B" / "C" / "D"
    has_conflict: bool
    conflict_detail: str = ""


# ── 风控参数 ──


class VolatilityRegime(Enum):
    NORMAL = "normal"
    HIGH = "high"


@dataclass
class RiskParams:
    """风控参数输出。"""
    volatility_regime: VolatilityRegime
    max_daily_loss_pct: float
    max_single_risk_pct: float
    circuit_breaker_count: int
    cooldown_minutes: int


# ── 每日完整报告 ──


@dataclass
class DailyReport:
    """完整盘前分析报告。"""
    date: date
    timestamp: float
    macro: MacroSnapshot
    rotation: RotationSnapshot
    mag7: Mag7Snapshot
    levels: dict[str, LevelMap]       # symbol → LevelMap
    script: ScriptJudgment
    confidence: ConfidenceReport
    risk: RiskParams
    calendar_events: list[str] = field(default_factory=list)
    is_premarket: bool = False
