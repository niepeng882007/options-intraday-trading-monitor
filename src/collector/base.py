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

    def subscribe_quotes(self, symbols: list[str], callback) -> None:
        """Subscribe to real-time push. No-op by default."""
