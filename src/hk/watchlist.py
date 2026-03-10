"""HK Watchlist — thin wrapper around shared Watchlist base class."""

from __future__ import annotations

import re

from src.common.watchlist import Watchlist

DEFAULT_PATH = "data/hk_watchlist.json"

# Normalize various user inputs to standard HK.XXXXX format
_SYMBOL_RE = re.compile(r"^(?:HK\.?)?(\d{4,6})$", re.IGNORECASE)


def normalize_symbol(text: str) -> str | None:
    """Normalize user input to HK.XXXXX format.

    Accepts: HK09988, 09988, HK.09988, hk09988
    Returns: HK.09988, or None if invalid.
    """
    text = text.strip()
    m = _SYMBOL_RE.match(text)
    if not m:
        return None
    code = m.group(1).zfill(5)
    return f"HK.{code}"


def _hk_config_parser(config: dict) -> dict[str, str]:
    """Parse HK config (indices + stocks groups) into {symbol: name}."""
    items: dict[str, str] = {}
    watchlist = config.get("watchlist", {})
    for group in ("indices", "stocks"):
        for item in watchlist.get(group, []):
            if isinstance(item, dict):
                items[item["symbol"]] = item.get("name", item["symbol"])
    return items


class HKWatchlist(Watchlist):
    """HK market watchlist."""

    def __init__(self, path: str = DEFAULT_PATH, initial_config: dict | None = None) -> None:
        super().__init__(
            path=path,
            initial_config=initial_config,
            config_parser=_hk_config_parser,
            logger_name="hk_watchlist",
        )
