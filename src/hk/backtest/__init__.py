"""HK Predict Backtest Framework.

Validates Volume Profile levels (VAH/VAL bounce rates) and Regime classification
accuracy using historical data. Optionally simulates trades based on signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.hk import RegimeType


@dataclass
class LevelEvent:
    """A single touch of VAH or VAL."""
    date: datetime
    symbol: str
    level_type: str  # "VAH" or "VAL"
    level_price: float
    touch_price: float
    touch_bar_idx: int
    bounce_results: dict[float, bool] = field(default_factory=dict)  # threshold -> hit?
    max_reversal_pct: float = 0.0
    session: str = ""  # "morning" or "afternoon"


@dataclass
class LevelEvalResult:
    """Aggregated level evaluation results."""
    events: list[LevelEvent] = field(default_factory=list)
    # threshold -> {vah_touches, vah_bounces, val_touches, val_bounces}
    by_threshold: dict[float, dict[str, int]] = field(default_factory=dict)
    # session -> threshold -> {touches, bounces}
    by_session: dict[str, dict[float, dict[str, int]]] = field(default_factory=dict)
    # symbol -> threshold -> {touches, bounces}
    by_symbol: dict[str, dict[float, dict[str, int]]] = field(default_factory=dict)


@dataclass
class RegimeEvalDay:
    """Regime evaluation for a single day."""
    date: datetime
    symbol: str
    predicted: RegimeType
    confidence: float
    rvol: float
    vah: float
    val: float
    poc: float
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    day_close: float = 0.0
    accurate: bool = False
    details: str = ""


@dataclass
class RegimeEvalResult:
    """Aggregated regime evaluation results."""
    days: list[RegimeEvalDay] = field(default_factory=list)
    # regime_type -> {total, accurate}
    by_regime: dict[str, dict[str, int]] = field(default_factory=dict)
    # symbol -> {total, accurate}
    by_symbol: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class SimTrade:
    """A simulated trade from level or regime signals."""
    symbol: str
    signal_type: str  # "VAH_short", "VAL_long", "BREAKOUT_long", etc.
    entry_price: float
    entry_time: datetime
    exit_price: float = 0.0
    exit_time: datetime | None = None
    exit_reason: str = ""
    stock_pnl_pct: float = 0.0
    net_pnl_pct: float = 0.0  # after slippage
    leveraged_pnl_pct: float = 0.0
    session: str = ""
    peak_pnl_pct: float = 0.0  # max unrealized PnL during trade (stock %)


@dataclass
class SimResult:
    """Trade simulation results."""
    trades: list[SimTrade] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy_pct: float = 0.0
    # Breakdowns
    by_signal_type: dict[str, dict[str, float]] = field(default_factory=dict)
    by_symbol: dict[str, dict[str, float]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class HKBacktestResult:
    """Combined backtest result."""
    level_eval: LevelEvalResult | None = None
    regime_eval: RegimeEvalResult | None = None
    sim_result: SimResult | None = None
    symbols: list[str] = field(default_factory=list)
    days: int = 0
    data_bars: int = 0
