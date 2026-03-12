from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Re-export shared types for backward compatibility
from src.common.types import (  # noqa: F401
    ChaseRiskResult,
    FilterResult,
    GammaWallResult,
    OptionLeg,
    OptionMarketSnapshot,
    OptionRecommendation,
    QuoteSnapshot,
    SpreadMetrics,
    VolumeProfileResult,
)


class RegimeType(Enum):
    GAP_AND_GO = "gap_and_go"  # 缺口追击日
    TREND_DAY = "trend_day"    # 趋势日
    FADE_CHOP = "fade_chop"    # 震荡日
    WHIPSAW = "whipsaw"        # 高波洗盘日
    UNCLEAR = "unclear"        # 不明确日
    # DEPRECATED — 保留兼容过渡期
    BREAKOUT = "breakout"
    RANGE = "range"


@dataclass
class HKKeyLevels:
    poc: float
    vah: float
    val: float
    pdh: float           # Previous Day High
    pdl: float           # Previous Day Low
    pdc: float           # Previous Day Close
    ibh: float           # Initial Balance High (first 30min)
    ibl: float           # Initial Balance Low (first 30min)
    day_open: float      # Current Day Open
    vwap: float
    gamma_call_wall: float = 0.0
    gamma_put_wall: float = 0.0
    gamma_max_pain: float = 0.0


@dataclass
class RegimeResult:
    regime: RegimeType
    confidence: float          # 0-1
    rvol: float
    price: float
    vah: float
    val: float
    poc: float
    details: str = ""
    gap_pct: float = 0.0      # 缺口百分比
    direction: str = ""        # "bullish"/"bearish"
    lean: str = "neutral"      # UNCLEAR 子类型倾向


@dataclass
class OrderBookAlert:
    symbol: str
    side: str                  # "bid" or "ask"
    price: float
    volume: int
    avg_volume: float
    ratio: float               # volume / avg_volume
    timestamp: datetime | None = None


@dataclass
class Playbook:
    regime: RegimeResult
    volume_profile: VolumeProfileResult
    gamma_wall: GammaWallResult | None
    filters: FilterResult
    vwap: float
    quote: QuoteSnapshot | None = None
    option_market: OptionMarketSnapshot | None = None
    key_levels: dict[str, float] = field(default_factory=dict)
    strategy_text: str = ""
    generated_at: datetime | None = None
    option_rec: OptionRecommendation | None = None
    key_levels_obj: HKKeyLevels | None = None


@dataclass
class ScanSignal:
    """Result of a successful L1+L2 scan for a single symbol."""
    signal_type: str        # "GAP_AND_GO" | "TREND_DAY" | "FADE_CHOP"
    direction: str          # "bullish" | "bearish"
    symbol: str
    regime: RegimeResult
    price: float
    trigger_reasons: list[str] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass
class ScanAlertRecord:
    """Tracks a sent scan alert for frequency control."""
    symbol: str
    signal_type: str        # "GAP_AND_GO" | "TREND_DAY" | "FADE_CHOP"
    direction: str          # "bullish" | "bearish"
    confidence: float
    price: float
    timestamp: float
    session: str            # "morning" | "afternoon"
