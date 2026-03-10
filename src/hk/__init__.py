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
    BREAKOUT = "breakout"      # Style A: 单边突破日
    RANGE = "range"            # Style B: 区间震荡日
    WHIPSAW = "whipsaw"        # Style C: 高波洗盘日
    UNCLEAR = "unclear"        # Style D: 不明确日


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


@dataclass
class ScanSignal:
    """Result of a successful L1+L2 scan for a single symbol."""
    signal_type: str        # "BREAKOUT" | "RANGE"
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
    signal_type: str        # "BREAKOUT" | "RANGE"
    direction: str          # "bullish" | "bearish"
    confidence: float
    price: float
    timestamp: float
    session: str            # "morning" | "afternoon"
