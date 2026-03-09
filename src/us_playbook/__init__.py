from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.hk import FilterResult, GammaWallResult, VolumeProfileResult  # shared types


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
