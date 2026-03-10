"""US Watchlist — thin wrapper around shared Watchlist base class."""

from __future__ import annotations

import re

from src.common.watchlist import Watchlist

DEFAULT_PATH = "data/us_watchlist.json"

# Normalize US ticker symbols: 1-5 alpha characters
_SYMBOL_RE = re.compile(r"^[A-Za-z]{1,5}$")


def normalize_us_symbol(text: str) -> str | None:
    """Normalize user input to uppercase US ticker.

    Accepts: AAPL, aapl, Aapl
    Returns: AAPL, or None if invalid.
    """
    text = text.strip()
    if not _SYMBOL_RE.match(text):
        return None
    return text.upper()


def _us_config_parser(config: dict) -> dict[str, str]:
    """Parse US config (flat list of {symbol, name}) into {symbol: name}."""
    items: dict[str, str] = {}
    watchlist = config.get("watchlist", [])
    for item in watchlist:
        if isinstance(item, dict):
            items[item["symbol"]] = item.get("name", item["symbol"])
    return items


class USWatchlist(Watchlist):
    """US market watchlist."""

    def __init__(self, path: str = DEFAULT_PATH, initial_config: dict | None = None) -> None:
        super().__init__(
            path=path,
            initial_config=initial_config,
            config_parser=_us_config_parser,
            logger_name="us_watchlist",
        )
