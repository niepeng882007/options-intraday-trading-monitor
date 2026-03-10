"""HK Watchlist — dynamic CRUD with JSON persistence."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from src.utils.logger import setup_logger

logger = setup_logger("hk_watchlist")

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


class HKWatchlist:
    """In-memory watchlist with JSON file persistence."""

    def __init__(self, path: str = DEFAULT_PATH, initial_config: dict | None = None) -> None:
        self._path = Path(path)
        # {symbol: name} ordered dict
        self._items: dict[str, str] = {}
        self._load(initial_config)

    def _load(self, initial_config: dict | None) -> None:
        """Load from JSON file, or initialize from YAML config if file doesn't exist."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._items = {item["symbol"]: item["name"] for item in data}
                logger.info("Loaded watchlist: %d symbols from %s", len(self._items), self._path)
                return
            except Exception:
                logger.warning("Failed to load watchlist from %s, re-initializing", self._path)

        # Initialize from YAML config
        if initial_config:
            watchlist = initial_config.get("watchlist", {})
            for group in ("indices", "stocks"):
                for item in watchlist.get(group, []):
                    if isinstance(item, dict):
                        self._items[item["symbol"]] = item.get("name", item["symbol"])
            logger.info("Initialized watchlist from config: %d symbols", len(self._items))
        self._save()

    def _save(self) -> None:
        """Atomic write: temp file + os.replace()."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = [{"symbol": s, "name": n} for s, n in self._items.items()]
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def add(self, symbol: str, name: str = "") -> bool:
        """Add symbol. Returns True if newly added, False if already present."""
        if symbol in self._items:
            return False
        self._items[symbol] = name or symbol
        self._save()
        logger.info("Watchlist add: %s (%s)", symbol, name)
        return True

    def remove(self, symbol: str) -> bool:
        """Remove symbol. Returns True if removed, False if not found."""
        if symbol not in self._items:
            return False
        del self._items[symbol]
        self._save()
        logger.info("Watchlist remove: %s", symbol)
        return True

    def contains(self, symbol: str) -> bool:
        return symbol in self._items

    def get_name(self, symbol: str) -> str:
        return self._items.get(symbol, symbol)

    def list_all(self) -> list[dict[str, str]]:
        """Return list of {symbol, name}."""
        return [{"symbol": s, "name": n} for s, n in self._items.items()]

    def symbols(self) -> list[str]:
        return list(self._items.keys())
