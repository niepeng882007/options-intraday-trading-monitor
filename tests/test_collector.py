import time

import pytest

from src.collector.base import StockQuote, OptionQuote


class TestStockQuote:
    def test_to_dict(self):
        quote = StockQuote(
            symbol="AAPL",
            price=228.50,
            bid=228.48,
            ask=228.52,
            volume=12345678,
            timestamp=time.time(),
        )
        d = quote.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["price"] == 228.50
        assert d["volume"] == 12345678

    def test_option_quote_to_dict(self):
        opt = OptionQuote(
            contract_symbol="AAPL250321C00230000",
            underlying="AAPL",
            strike=230.0,
            option_type="call",
            expiration="2025-03-21",
            bid=3.45,
            ask=3.50,
            last=3.47,
            volume=1523,
            open_interest=8900,
            implied_volatility=0.32,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            timestamp=time.time(),
        )
        d = opt.to_dict()
        assert d["contract_symbol"] == "AAPL250321C00230000"
        assert d["strike"] == 230.0
        assert d["option_type"] == "call"
        assert d["delta"] is None


class TestQuoteDataclasses:
    """Unit tests for StockQuote/OptionQuote dataclasses."""

    def test_stock_quote_fields(self):
        quote = StockQuote(
            symbol="TSLA", price=180.0, bid=179.9, ask=180.1,
            volume=5000000, timestamp=1000.0,
        )
        assert quote.price == 180.0
        assert quote.bid < quote.ask

    def test_option_quote_spread(self):
        opt = OptionQuote(
            contract_symbol="SPY250321P00500000",
            underlying="SPY",
            strike=500.0,
            option_type="put",
            expiration="2025-03-21",
            bid=2.10,
            ask=2.30,
            last=2.20,
            volume=500,
            open_interest=3000,
            implied_volatility=0.25,
            delta=None, gamma=None, theta=None, vega=None,
            timestamp=time.time(),
        )
        spread_pct = (opt.ask - opt.bid) / opt.ask
        assert spread_pct < 0.15
