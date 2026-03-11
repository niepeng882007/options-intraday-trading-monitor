from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

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


class USRegimeType(Enum):
    GAP_AND_GO = "gap_and_go"    # Gap + high RVOL + PM breakout
    TREND_DAY = "trend_day"      # No big gap but directional + elevated RVOL
    FADE_CHOP = "fade_chop"      # Low RVOL + range-bound
    UNCLEAR = "unclear"          # Mixed signals


@dataclass
class KeyLevels:
    poc: float
    vah: float
    val: float
    pdh: float
    pdl: float
    pmh: float
    pml: float
    vwap: float
    gamma_call_wall: float = 0.0
    gamma_put_wall: float = 0.0
    gamma_max_pain: float = 0.0
    pm_source: str = "futu"  # "futu" | "yahoo" | "gap_estimate"


@dataclass
class USRegimeResult:
    regime: USRegimeType
    confidence: float            # 0-1
    rvol: float
    price: float
    gap_pct: float               # open vs prev_close %
    spy_regime: USRegimeType | None = None
    details: str = ""
    adaptive_thresholds: dict | None = None
    # e.g. {"gap_and_go": 1.73, "trend_day": 1.15, "fade_chop": 0.88, "pctl_rank": 72.3, "sample": 9}
    lean: str = "neutral"        # "bullish" / "bearish" / "neutral" — UNCLEAR sub-type hint


@dataclass
class ORBRange:
    """Opening Range Breakout — SPY first N minutes."""
    high: float
    low: float
    breakout_direction: str | None = None  # "bullish" / "bearish" / None
    confirmed: bool = False
    reversal_failed: bool = False


@dataclass
class VWAPStatus:
    """SPY VWAP position and slope."""
    value: float
    position: str              # "above" / "below"
    slope: float               # %/bar
    slope_label: str           # "rising" / "falling" / "flat"


@dataclass
class BreadthProxy:
    """Multi-stock alignment proxy for market breadth."""
    aligned_count: int
    total_count: int
    alignment_ratio: float
    alignment_label: str       # "strong_aligned" / "mixed" / "divergent"
    index_aligned: bool        # SPY+QQQ+IWM all same direction
    details: str = ""


@dataclass
class VIXContext:
    """VIX level and intraday change context."""
    level: float
    change_pct: float
    signal: str                # "caution" / "neutral" / "supportive"
    stale: bool = False
    timestamp: float = 0.0


@dataclass
class MarketTone:
    """Market-level tone assessment — produced by MarketToneEngine."""
    grade: str                     # "A+" / "A" / "B+" / "B" / "C" / "D"
    grade_score: int               # 0-5
    direction: str                 # "bullish" / "bearish" / "neutral"
    day_type: str                  # "trend" / "chop" / "event"
    confidence_modifier: float
    position_size_hint: str        # "full" / "reduced" / "minimal" / "sit_out"

    macro_signal: str = "clear"
    gap_signal: str = "neutral"
    gap_pct: float = 0.0
    vix: VIXContext | None = None
    orb: ORBRange | None = None
    vwap_status: VWAPStatus | None = None
    breadth: BreadthProxy | None = None

    components_aligned: list[str] = field(default_factory=list)
    components_conflicting: list[str] = field(default_factory=list)
    computed_at: datetime | None = None
    details: str = ""


@dataclass
class USPlaybookResult:
    symbol: str
    name: str
    regime: USRegimeResult
    key_levels: KeyLevels
    volume_profile: VolumeProfileResult
    gamma_wall: GammaWallResult | None
    filters: FilterResult
    strategy_text: str = ""
    generated_at: datetime | None = None
    option_rec: OptionRecommendation | None = None
    quote: QuoteSnapshot | None = None
    option_market: OptionMarketSnapshot | None = None
    market_tone: MarketTone | None = None


@dataclass
class USScanSignal:
    """Result of a successful L1+L2 scan for a single symbol."""
    signal_type: str        # BREAKOUT_LONG / BREAKOUT_SHORT / RANGE_REVERSAL_LONG / RANGE_REVERSAL_SHORT
    direction: str          # bullish / bearish
    symbol: str
    regime: USRegimeResult
    price: float
    trigger_reasons: list[str] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass
class USScanAlertRecord:
    """Tracks a sent scan alert for frequency control."""
    symbol: str
    signal_type: str
    direction: str
    confidence: float
    price: float
    timestamp: float
    session: str            # morning / afternoon
