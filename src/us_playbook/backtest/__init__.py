"""US Predictor Backtest Framework.

Validates Volume Profile levels (VAH/VAL/PDH/PDL bounce rates) and Regime
classification accuracy using historical data. Optionally simulates trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.us_playbook import USRegimeType


@dataclass
class LevelEvent:
    """A single touch of VAH, VAL, PDH, or PDL."""
    date: datetime
    symbol: str
    level_type: str  # "VAH" | "VAL" | "PDH" | "PDL"
    level_price: float
    touch_price: float
    touch_bar_idx: int
    bounce_results: dict[float, bool] = field(default_factory=dict)
    max_reversal_pct: float = 0.0


@dataclass
class LevelEvalResult:
    """Aggregated level evaluation results."""
    events: list[LevelEvent] = field(default_factory=list)
    # threshold -> {vah_t, vah_b, val_t, val_b, pdh_t, pdh_b, pdl_t, pdl_b}
    by_threshold: dict[float, dict[str, int]] = field(default_factory=dict)
    # symbol -> threshold -> {touches, bounces}
    by_symbol: dict[str, dict[float, dict[str, int]]] = field(default_factory=dict)


@dataclass
class RegimeEvalDay:
    """Regime evaluation for a single day."""
    date: datetime
    symbol: str
    predicted: USRegimeType
    confidence: float
    rvol: float
    vah: float
    val: float
    poc: float
    prev_close: float = 0.0
    gap_pct: float = 0.0
    pmh: float = 0.0
    pml: float = 0.0
    adaptive_thresholds: dict | None = None
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    day_close: float = 0.0
    accurate: bool = False
    scorable: bool = True  # False for UNCLEAR
    details: str = ""


@dataclass
class RegimeEvalResult:
    """Aggregated regime evaluation results."""
    days: list[RegimeEvalDay] = field(default_factory=list)
    # regime_type -> {total, scorable, accurate}
    by_regime: dict[str, dict[str, int]] = field(default_factory=dict)
    # symbol -> {total, scorable, accurate}
    by_symbol: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class SimTrade:
    """A simulated trade from level or regime signals."""
    symbol: str
    signal_type: str  # "VAH_short", "VAL_long", "PDH_short", "PDL_long", "GAP_AND_GO_long", etc.
    entry_price: float
    entry_time: datetime
    exit_price: float = 0.0
    exit_time: datetime | None = None
    exit_reason: str = ""
    stock_pnl_pct: float = 0.0
    net_pnl_pct: float = 0.0  # after slippage
    leveraged_pnl_pct: float = 0.0
    peak_pnl_pct: float = 0.0


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
    by_signal_type: dict[str, dict[str, float]] = field(default_factory=dict)
    by_symbol: dict[str, dict[str, float]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class USBacktestResult:
    """Combined backtest result."""
    level_eval: LevelEvalResult | None = None
    regime_eval: RegimeEvalResult | None = None
    sim_result: SimResult | None = None
    symbols: list[str] = field(default_factory=list)
    days: int = 0
    data_bars: int = 0
