from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.utils.logger import setup_logger

logger = setup_logger("strategy_state")

ENTRY_TRIGGERED_TIMEOUT_SECONDS = 300  # 5 min


class StrategyState(str, Enum):
    WATCHING = "WATCHING"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    HOLDING = "HOLDING"
    EXIT_TRIGGERED = "EXIT_TRIGGERED"


@dataclass
class PositionInfo:
    signal_id: str = ""
    entry_price: float = 0.0
    entry_timestamp: float = 0.0
    contract_symbol: str = ""
    highest_price: float = 0.0
    lowest_price: float = 0.0


@dataclass
class StateEntry:
    strategy_id: str
    symbol: str
    state: StrategyState = StrategyState.WATCHING
    position: PositionInfo = field(default_factory=PositionInfo)
    triggered_at: float = 0.0
    signal_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "state": self.state.value,
            "position": {
                "signal_id": self.position.signal_id,
                "entry_price": self.position.entry_price,
                "entry_timestamp": self.position.entry_timestamp,
                "contract_symbol": self.position.contract_symbol,
                "highest_price": self.position.highest_price,
                "lowest_price": self.position.lowest_price,
            },
            "triggered_at": self.triggered_at,
            "signal_id": self.signal_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateEntry:
        pos_data = data.get("position", {})
        return cls(
            strategy_id=data["strategy_id"],
            symbol=data["symbol"],
            state=StrategyState(data.get("state", "WATCHING")),
            position=PositionInfo(
                signal_id=pos_data.get("signal_id", ""),
                entry_price=pos_data.get("entry_price", 0.0),
                entry_timestamp=pos_data.get("entry_timestamp", 0.0),
                contract_symbol=pos_data.get("contract_symbol", ""),
                highest_price=pos_data.get("highest_price", 0.0),
                lowest_price=pos_data.get("lowest_price", 0.0),
            ),
            triggered_at=data.get("triggered_at", 0.0),
            signal_id=data.get("signal_id", ""),
        )


class StrategyStateManager:
    """Manages per-(strategy, symbol) state machine transitions.

    State flow: WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING
    """

    def __init__(self) -> None:
        self._states: dict[str, StateEntry] = {}
        self._signal_counter: int = 0

    def _key(self, strategy_id: str, symbol: str) -> str:
        return f"{strategy_id}:{symbol}"

    def _generate_signal_id(self) -> str:
        self._signal_counter += 1
        return f"SIG-{int(time.time())}-{self._signal_counter:04d}"

    def get_state(self, strategy_id: str, symbol: str) -> StateEntry:
        key = self._key(strategy_id, symbol)
        if key not in self._states:
            self._states[key] = StateEntry(strategy_id=strategy_id, symbol=symbol)
        return self._states[key]

    def get_all_states(self) -> list[StateEntry]:
        return list(self._states.values())

    def get_holding_positions(self) -> list[StateEntry]:
        return [s for s in self._states.values() if s.state == StrategyState.HOLDING]

    # ── Transitions ──

    def trigger_entry(self, strategy_id: str, symbol: str) -> str | None:
        entry = self.get_state(strategy_id, symbol)
        if entry.state != StrategyState.WATCHING:
            logger.debug(
                "Cannot trigger entry for %s:%s — current state: %s",
                strategy_id, symbol, entry.state,
            )
            return None

        signal_id = self._generate_signal_id()
        entry.state = StrategyState.ENTRY_TRIGGERED
        entry.triggered_at = time.time()
        entry.signal_id = signal_id
        logger.info("Entry triggered: %s:%s signal=%s", strategy_id, symbol, signal_id)
        return signal_id

    def confirm_entry(
        self,
        signal_id: str,
        entry_price: float,
        contract_symbol: str = "",
    ) -> bool:
        entry = self._find_by_signal(signal_id)
        if entry is None or entry.state != StrategyState.ENTRY_TRIGGERED:
            logger.warning("Cannot confirm — signal %s not found or wrong state", signal_id)
            return False

        entry.state = StrategyState.HOLDING
        entry.position = PositionInfo(
            signal_id=signal_id,
            entry_price=entry_price,
            entry_timestamp=time.time(),
            contract_symbol=contract_symbol,
            highest_price=entry_price,
            lowest_price=entry_price,
        )
        logger.info(
            "Entry confirmed: %s:%s @ $%.2f",
            entry.strategy_id, entry.symbol, entry_price,
        )
        return True

    def skip_entry(self, signal_id: str) -> bool:
        entry = self._find_by_signal(signal_id)
        if entry is None or entry.state != StrategyState.ENTRY_TRIGGERED:
            return False

        entry.state = StrategyState.WATCHING
        entry.signal_id = ""
        entry.triggered_at = 0.0
        logger.info("Entry skipped: signal=%s", signal_id)
        return True

    def trigger_exit(self, strategy_id: str, symbol: str) -> bool:
        entry = self.get_state(strategy_id, symbol)
        if entry.state != StrategyState.HOLDING:
            return False

        entry.state = StrategyState.EXIT_TRIGGERED
        logger.info("Exit triggered: %s:%s", strategy_id, symbol)
        return True

    def confirm_exit(self, strategy_id: str, symbol: str) -> bool:
        entry = self.get_state(strategy_id, symbol)
        if entry.state != StrategyState.EXIT_TRIGGERED:
            return False

        entry.state = StrategyState.WATCHING
        entry.position = PositionInfo()
        entry.signal_id = ""
        entry.triggered_at = 0.0
        logger.info("Exit confirmed: %s:%s → WATCHING", strategy_id, symbol)
        return True

    def update_highest_price(self, strategy_id: str, symbol: str, price: float) -> None:
        entry = self.get_state(strategy_id, symbol)
        if entry.state == StrategyState.HOLDING:
            if price > entry.position.highest_price:
                entry.position.highest_price = price
            if price < entry.position.lowest_price:
                entry.position.lowest_price = price

    # ── Timeout handling ──

    def check_timeouts(self) -> list[StateEntry]:
        timed_out: list[StateEntry] = []
        now = time.time()
        for entry in self._states.values():
            if (
                entry.state == StrategyState.ENTRY_TRIGGERED
                and entry.triggered_at > 0
                and (now - entry.triggered_at) > ENTRY_TRIGGERED_TIMEOUT_SECONDS
            ):
                entry.state = StrategyState.WATCHING
                entry.signal_id = ""
                entry.triggered_at = 0.0
                timed_out.append(entry)
                logger.info(
                    "Entry timed out: %s:%s → WATCHING",
                    entry.strategy_id, entry.symbol,
                )
        return timed_out

    def reset(self, strategy_id: str) -> None:
        keys_to_remove = [k for k in self._states if k.startswith(f"{strategy_id}:")]
        for k in keys_to_remove:
            del self._states[k]
        logger.info("Reset all states for strategy %s", strategy_id)

    # ── Serialization ──

    def export_all(self) -> list[dict]:
        return [entry.to_dict() for entry in self._states.values()]

    def import_all(self, data: list[dict]) -> None:
        for item in data:
            entry = StateEntry.from_dict(item)
            key = self._key(entry.strategy_id, entry.symbol)
            self._states[key] = entry
        logger.info("Imported %d state entries", len(data))

    # ── Helpers ──

    def _find_by_signal(self, signal_id: str) -> StateEntry | None:
        for entry in self._states.values():
            if entry.signal_id == signal_id:
                return entry
        return None
