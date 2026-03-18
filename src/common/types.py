"""Shared dataclass types used across HK and US modules."""

from __future__ import annotations

from dataclasses import dataclass, field


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
class FilterResult:
    tradeable: bool
    warnings: list[str] = field(default_factory=list)
    risk_level: str = "normal"  # normal, elevated, high, blocked
    block_reasons: list[str] = field(default_factory=list)  # "calendar", "inside_day_rvol", "opex_combo", "earnings"


@dataclass
class OptionLeg:
    side: str          # "buy" | "sell"
    option_type: str   # "call" | "put"
    strike: float
    pct_from_price: float  # distance from current price %
    moneyness: str     # "ATM" | "OTM 3.2%" | "ITM 1.5%"
    delta: float | None = None
    open_interest: int | None = None
    last_price: float | None = None
    implied_volatility: float | None = None
    volume: int | None = None


@dataclass
class ChaseRiskResult:
    level: str = "none"                # "none" | "moderate" | "high"
    reasons: list[str] = field(default_factory=list)
    vwap_dev_pct: float = 0.0
    va_dist_pct: float = 0.0
    pullback_target: float = 0.0


@dataclass
class SpreadMetrics:
    net_credit: float = 0.0        # net premium received (credit spread)
    max_profit: float = 0.0        # max profit = net_credit
    max_loss: float = 0.0          # max loss = strike_width - net_credit
    breakeven: float = 0.0         # breakeven price at expiry
    risk_reward_ratio: float = 0.0  # max_profit / max_loss
    win_probability: float = 0.0   # 1 - |sold leg delta|


@dataclass
class OptionRecommendation:
    action: str                    # "call" | "put" | "bull_put_spread" | "bear_call_spread" | "wait"
    direction: str                 # "bullish" | "bearish" | "neutral"
    expiry: str | None = None      # "2026-03-18"
    legs: list[OptionLeg] = field(default_factory=list)
    moneyness: str = ""            # "ATM" | "OTM" | "ITM"
    rationale: str = ""            # recommendation rationale
    risk_note: str = ""            # risk note / wait reason
    wait_conditions: list[str] = field(default_factory=list)
    liquidity_warning: str | None = None
    spread_metrics: SpreadMetrics | None = None
    dte: int = 0
    structural_veto: bool = False   # structural veto (trend/VA width etc.) — L2 should reject push
    wait_category: str = "market"   # "market" = regime/direction-based wait; "data" = option chain/expiry data issue


@dataclass
class QuoteSnapshot:
    symbol: str
    last_price: float
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    prev_close: float = 0.0
    volume: int = 0
    turnover: float = 0.0
    bid_price: float = 0.0
    ask_price: float = 0.0
    amplitude: float = 0.0
    turnover_rate: float = 0.0
    volume_ratio: float = 0.0
    timestamp: float = 0.0


@dataclass
class PlaybookResponse:
    """Wrapper for playbook output: HTML text + optional chart PNG."""
    html: str
    chart: bytes | None = None  # PNG bytes


@dataclass
class DirectionConfidence:
    """Aggregated directional confidence from multiple signals."""
    direction: str             # "bullish" / "bearish" / "neutral"
    score: float               # 0.0 - 1.0
    signals: dict[str, str] = field(default_factory=dict)
    # e.g. {"regime": "bullish", "vwap_slope": "bullish", "structure": "neutral", ...}


@dataclass
class RelativeStrength:
    """Individual stock relative strength vs benchmark (SPY)."""
    rs_ratio: float = 0.0          # stock_return / spy_return (>1 = outperforming)
    stock_return_pct: float = 0.0  # intraday return %
    spy_return_pct: float = 0.0    # SPY intraday return %
    correlation: float = 0.0       # rolling correlation (0-1)
    decoupled: bool = False        # True when |correlation| < threshold
    label: str = ""                # "强势" / "弱势" / "同步" / "脱钩"


@dataclass
class OptionMarketSnapshot:
    expiry: str | None = None
    contract_count: int = 0
    call_contract_count: int = 0
    put_contract_count: int = 0
    atm_iv: float = 0.0
    avg_iv: float = 0.0
    iv_ratio: float = 0.0
