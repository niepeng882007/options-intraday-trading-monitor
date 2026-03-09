from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RegimeType(Enum):
    BREAKOUT = "breakout"      # Style A: 单边突破日
    RANGE = "range"            # Style B: 区间震荡日
    WHIPSAW = "whipsaw"        # Style C: 高波洗盘日
    UNCLEAR = "unclear"        # Style D: 不明确日


@dataclass
class VolumeProfileResult:
    poc: float                 # Point of Control
    vah: float                 # Value Area High
    val: float                 # Value Area Low
    volume_by_price: dict[float, float] = field(default_factory=dict)
    total_volume: float = 0.0
    trading_days: int = 0


@dataclass
class GammaWallResult:
    call_wall_strike: float    # Max call OI strike
    put_wall_strike: float     # Max put OI strike
    max_pain: float            # Max pain price
    call_oi_by_strike: dict[float, int] = field(default_factory=dict)
    put_oi_by_strike: dict[float, int] = field(default_factory=dict)


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
class FilterResult:
    tradeable: bool
    warnings: list[str] = field(default_factory=list)
    risk_level: str = "normal"  # normal, elevated, high, blocked


@dataclass
class Playbook:
    regime: RegimeResult
    volume_profile: VolumeProfileResult
    gamma_wall: GammaWallResult | None
    filters: FilterResult
    vwap: float
    key_levels: dict[str, float] = field(default_factory=dict)
    strategy_text: str = ""
    generated_at: datetime | None = None
