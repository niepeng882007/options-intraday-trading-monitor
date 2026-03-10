"""Base Watchlist — shared CRUD with JSON persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from src.utils.logger import setup_logger


class Watchlist:
    """In-memory watchlist with JSON file persistence.

    Subclasses provide market-specific config parsing via config_parser.
    """

    def __init__(
        self,
        path: str,
        initial_config: dict | None = None,
        config_parser: Callable[[dict], dict[str, str]] | None = None,
        logger_name: str = "watchlist",
    ) -> None:
        self._path = Path(path)
        self._items: dict[str, str] = {}  # {symbol: name}
        self._logger = setup_logger(logger_name)
        self._load(initial_config, config_parser)

    def _load(
        self,
        initial_config: dict | None,
        config_parser: Callable[[dict], dict[str, str]] | None,
    ) -> None:
        """Load from JSON file, or initialize from config if file doesn't exist."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._items = {item["symbol"]: item["name"] for item in data}
                self._logger.info(
                    "Loaded watchlist: %d symbols from %s", len(self._items), self._path,
                )
                return
            except Exception:
                self._logger.warning(
                    "Failed to load watchlist from %s, re-initializing", self._path,
                )

        # Initialize from config
        if initial_config and config_parser:
            self._items = config_parser(initial_config)
            self._logger.info(
                "Initialized watchlist from config: %d symbols", len(self._items),
            )
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
        self._logger.info("Watchlist add: %s (%s)", symbol, name)
        return True

    def remove(self, symbol: str) -> bool:
        """Remove symbol. Returns True if removed, False if not found."""
        if symbol not in self._items:
            return False
        del self._items[symbol]
        self._save()
        self._logger.info("Watchlist remove: %s", symbol)
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
