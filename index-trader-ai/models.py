"""数据模型 — 纯数据容器，无分析逻辑。

所有可选字段用 float | None 区分"值为零"和"数据不可用"。
formatter 层将 None 渲染为 [不可用]。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MacroData:
    """VIX + TNX + UUP 原始数据。"""

    vix_current: float | None = None
    vix_prev_close: float | None = None
    vix_ma10: float | None = None
    vix_deviation_pct: float | None = None  # (current - MA10) / MA10
    tnx_current: float | None = None  # 10Y yield %
    tnx_prev_close: float | None = None
    tnx_change_bps: float | None = None  # basis points
    uup_current: float | None = None
    uup_prev_close: float | None = None
    uup_change_pct: float | None = None
    timestamp: float = 0.0


@dataclass
class IndexData:
    """单个指数/ETF 的完整数据。"""

    symbol: str
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None  # 自算: (price - prev_close) / prev_close
    volume: int | None = None
    gap_pct: float | None = None  # 盘前 = change_pct
    pdc: float | None = None  # previous day close
    pdh: float | None = None  # previous day high
    pdl: float | None = None  # previous day low
    pmh: float | None = None  # premarket high
    pml: float | None = None  # premarket low
    weekly_high: float | None = None
    weekly_low: float | None = None
    # 期权/成交量分布
    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    gamma_call_wall: float | None = None
    gamma_put_wall: float | None = None
    status: str = "ok"  # "ok" / "盘前无成交" / "数据异常" / "不可用"


@dataclass
class Mag7Data:
    """单个 Mag7 股票数据。"""

    symbol: str
    change_pct: float | None = None
    volume: int | None = None
    volume_ratio: float | None = None  # vs 5-day avg daily volume
    status: str = "ok"


@dataclass
class CalendarEvent:
    """单个经济日历事件。"""

    time: str  # "08:30", "14:00", "全天"
    name: str
    importance: str  # "high" / "medium" / "low"
    previous: str = ""
    forecast: str = ""


@dataclass
class DataStatus:
    """数据源健康状态。"""

    source: str  # "futu" / "yfinance_vix" / "yfinance_tnx" / ...
    ok: bool
    detail: str = ""
    last_update: float = 0.0


@dataclass
class RiskLookup:
    """VIX 偏离查找表结果。"""

    vix_deviation_pct: float | None = None
    regime: str = "normal"  # "normal" / "high_volatility"
    max_single_risk_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    circuit_breaker_count: int = 3
    cooldown_minutes: int = 30


@dataclass
class CollectionResult:
    """单次采集的完整结果。"""

    timestamp: float
    date_str: str  # "2026-03-25"
    time_str: str  # "09:00 ET"
    macro: MacroData
    indices: list[IndexData] = field(default_factory=list)
    mag7: list[Mag7Data] = field(default_factory=list)
    calendar: list[CalendarEvent] = field(default_factory=list)
    statuses: list[DataStatus] = field(default_factory=list)
    is_premarket: bool = False
