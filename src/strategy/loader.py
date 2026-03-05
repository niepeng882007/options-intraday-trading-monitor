from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import yaml
import platform

from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
from watchdog.observers.polling import PollingObserver

from src.utils.logger import setup_logger

logger = setup_logger("strategy_loader")

REQUIRED_FIELDS = {"strategy_id", "name", "enabled", "watchlist", "entry_conditions"}


class StrategyConfig:
    """Parsed representation of a single YAML strategy file."""

    def __init__(self, data: dict[str, Any], filepath: str = "") -> None:
        self.raw = data
        self.filepath = filepath
        self.strategy_id: str = data["strategy_id"]
        self.name: str = data["name"]
        self.enabled: bool = data.get("enabled", True)
        self.watchlist: dict = data.get("watchlist", {})
        self.entry_conditions: dict = data.get("entry_conditions", {})
        self.exit_conditions: dict = data.get("exit_conditions", {})
        self.notification: dict = data.get("notification", {})
        self.entry_quality_filters: dict = data.get("entry_quality_filters", {})

    @property
    def underlyings(self) -> list[str]:
        return self.watchlist.get("underlyings", [])

    @property
    def option_filter(self) -> dict:
        return self.watchlist.get("option_filter", {})

    @property
    def description(self) -> str:
        return self.raw.get("description", "")

    @property
    def sop_checklist(self) -> list[str]:
        return self.raw.get("sop_checklist", [])

    @property
    def option_selection_text(self) -> str:
        opt = self.raw.get("option_selection", {})
        if isinstance(opt, dict):
            pref = opt.get("preference", "")
            reason = opt.get("reason", "")
            return f"{pref}（{reason}）" if reason else pref
        return str(opt) if opt else ""

    @property
    def exit_plan(self) -> dict[str, str]:
        return self.raw.get("exit_plan", {})

    @property
    def trading_window_text(self) -> str:
        tw = self.raw.get("trading_window", {})
        if isinstance(tw, dict):
            start = tw.get("start", "")
            end = tw.get("end", "")
            tz = tw.get("timezone", "")
            return f"{start}-{end} {tz}" if start else ""
        return ""

    @property
    def trading_window_start(self) -> str | None:
        tw = self.raw.get("trading_window", {})
        if isinstance(tw, dict):
            return tw.get("start")
        return None

    @property
    def trading_window_end(self) -> str | None:
        tw = self.raw.get("trading_window", {})
        if isinstance(tw, dict):
            return tw.get("end")
        return None

    @property
    def trading_window_tz(self) -> str:
        tw = self.raw.get("trading_window", {})
        if isinstance(tw, dict):
            return tw.get("timezone", "US/Eastern")
        return "US/Eastern"

    @property
    def cooldown_seconds(self) -> int:
        return self.notification.get("cooldown_seconds", 120)

    @property
    def priority(self) -> str:
        return self.notification.get("priority", "medium")

    def __repr__(self) -> str:
        status = "ON" if self.enabled else "OFF"
        return f"<Strategy '{self.strategy_id}' [{status}] {self.name}>"


def _validate_strategy(data: dict) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"Missing required fields: {missing}")

    entry = data.get("entry_conditions", {})
    if entry and "rules" not in entry:
        errors.append("entry_conditions must contain 'rules'")

    return errors


def load_strategy_file(filepath: str | Path) -> StrategyConfig | None:
    filepath = Path(filepath)
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            logger.error("Invalid YAML structure in %s", filepath)
            return None

        errors = _validate_strategy(data)
        if errors:
            logger.error("Validation errors in %s: %s", filepath, errors)
            return None

        config = StrategyConfig(data, str(filepath))
        logger.info("Loaded strategy: %s", config)
        return config
    except Exception:
        logger.exception("Failed to load strategy file: %s", filepath)
        return None


class StrategyLoader:
    """Loads all strategy YAML files from a directory, with hot-reload via watchdog."""

    def __init__(self, strategies_dir: str) -> None:
        self._dir = Path(strategies_dir)
        self._strategies: dict[str, StrategyConfig] = {}
        self._observer: PollingObserver | None = None
        self._on_change_callbacks: list[Callable[[str, StrategyConfig | None], Any]] = []

    @property
    def strategies(self) -> dict[str, StrategyConfig]:
        return dict(self._strategies)

    def get(self, strategy_id: str) -> StrategyConfig | None:
        return self._strategies.get(strategy_id)

    def get_active(self) -> list[StrategyConfig]:
        return [s for s in self._strategies.values() if s.enabled]

    def get_all_symbols(self) -> set[str]:
        symbols: set[str] = set()
        for strat in self._strategies.values():
            if strat.enabled:
                symbols.update(strat.underlyings)
        return symbols

    def on_change(self, callback: Callable[[str, StrategyConfig | None], Any]) -> None:
        self._on_change_callbacks.append(callback)

    # ── Loading ──

    def load_all(self) -> None:
        if not self._dir.exists():
            logger.warning("Strategies directory does not exist: %s", self._dir)
            return

        for filepath in sorted(self._dir.glob("*.yaml")):
            config = load_strategy_file(filepath)
            if config:
                self._strategies[config.strategy_id] = config

        logger.info(
            "Loaded %d strategies (%d active)",
            len(self._strategies),
            len(self.get_active()),
        )

    def reload_file(self, filepath: str) -> None:
        config = load_strategy_file(filepath)
        if config:
            old = self._strategies.get(config.strategy_id)
            self._strategies[config.strategy_id] = config
            action = "Updated" if old else "Added"
            logger.info("%s strategy: %s", action, config)
            for cb in self._on_change_callbacks:
                cb(config.strategy_id, config)
        else:
            logger.warning("Failed to reload strategy from %s", filepath)

    def set_enabled(self, strategy_id: str, enabled: bool) -> bool:
        strat = self._strategies.get(strategy_id)
        if strat is None:
            return False
        strat.enabled = enabled
        strat.raw["enabled"] = enabled
        logger.info("Strategy %s %s", strategy_id, "enabled" if enabled else "disabled")
        return True

    # ── File watcher ──

    def start_watching(self) -> None:
        if self._observer is not None:
            return

        handler = _YamlChangeHandler(self)
        self._observer = PollingObserver(timeout=3)
        self._observer.schedule(handler, str(self._dir), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching for strategy changes in %s (polling)", self._dir)

    def stop_watching(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None


class _YamlChangeHandler(FileSystemEventHandler):
    def __init__(self, loader: StrategyLoader) -> None:
        self._loader = loader

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and event.src_path.endswith(".yaml"):
            logger.info("Detected change: %s", event.src_path)
            self._loader.reload_file(event.src_path)

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if not event.is_directory and event.src_path.endswith(".yaml"):
            logger.info("Detected new file: %s", event.src_path)
            self._loader.reload_file(event.src_path)
