from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


@dataclass
class StockQuote:
    symbol: str
    price: float
    bid: float
    ask: float
    volume: int
    timestamp: float
    # Extended fields (populated by Futu; may be None)
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    prev_close_price: float | None = None
    change_pct: float | None = None          # day change %
    turnover: float | None = None            # turnover amount ($)
    turnover_rate: float | None = None       # turnover rate %
    amplitude: float | None = None           # day amplitude %

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OptionQuote:
    contract_symbol: str
    underlying: str
    strike: float
    option_type: str
    expiration: str
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_volatility: float
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    timestamp: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PremarketData:
    pmh: float
    pml: float
    source: str  # "futu" | "yahoo" | "gap_estimate"


class BaseCollector(ABC):
    """Abstract interface for all market data sources.

    Implementing a new data source (e.g. IBKR) only requires subclassing
    this ABC — the rest of the pipeline stays untouched.
    """

    @abstractmethod
    async def get_stock_quote(self, symbol: str) -> StockQuote:
        ...

    @abstractmethod
    async def get_option_chain(
        self,
        symbol: str,
        expiration: str | None = None,
    ) -> list[OptionQuote]:
        ...

    @abstractmethod
    async def get_history(
        self,
        symbol: str,
        interval: str = "1m",
        period: str = "1d",
    ) -> pd.DataFrame:
        ...

    async def connect(self) -> None:
        """Initialize connection. No-op by default."""

    async def close(self) -> None:
        """Close connection. No-op by default."""

    async def health_check(self) -> None:
        """Check connection health. No-op by default."""

    def get_cached_quote(self, symbol: str, max_age: float = 60.0) -> StockQuote | None:
        """Return a recently cached quote if available. Default: None."""
        return None

    async def get_connection_info(self) -> dict:
        """Return connection status details. Override in subclasses."""
        return {"status": "unknown", "source": "base"}

    def subscribe_quotes(self, symbols: list[str], callback) -> None:
        """Subscribe to real-time push. No-op by default."""

    def subscribe_kline(self, symbols: list[str], callback) -> None:
        """Subscribe to real-time K-line push. No-op by default."""
