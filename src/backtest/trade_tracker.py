from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Trade:
    strategy_id: str
    strategy_name: str
    symbol: str
    direction: str  # "call" or "put"
    entry_price: float
    entry_time: datetime
    exit_price: float = 0.0
    exit_time: datetime | None = None
    exit_reason: str = ""
    stock_pnl_pct: float = 0.0
    direction_pnl_pct: float = 0.0
    quality_score: int = 0
    quality_grade: str = ""

    @property
    def holding_minutes(self) -> float:
        if self.exit_time is None:
            return 0.0
        return (self.exit_time - self.entry_time).total_seconds() / 60


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_holding_minutes: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    trades_per_day: float = 0.0
    by_strategy: dict[str, dict] = field(default_factory=dict)
    by_symbol: dict[str, dict] = field(default_factory=dict)


class TradeTracker:
    def __init__(self) -> None:
        self._open_trades: dict[str, Trade] = {}  # key: "strategy_id:symbol"
        self._closed_trades: list[Trade] = []

    def _key(self, strategy_id: str, symbol: str) -> str:
        return f"{strategy_id}:{symbol}"

    def open_trade(
        self,
        strategy_id: str,
        name: str,
        symbol: str,
        direction: str,
        price: float,
        time: datetime,
        quality_score: int = 0,
        quality_grade: str = "",
    ) -> Trade:
        trade = Trade(
            strategy_id=strategy_id,
            strategy_name=name,
            symbol=symbol,
            direction=direction,
            entry_price=price,
            entry_time=time,
            quality_score=quality_score,
            quality_grade=quality_grade,
        )
        self._open_trades[self._key(strategy_id, symbol)] = trade
        return trade

    def close_trade(
        self,
        strategy_id: str,
        symbol: str,
        exit_price: float,
        exit_time: datetime,
        reason: str,
    ) -> Trade | None:
        key = self._key(strategy_id, symbol)
        trade = self._open_trades.pop(key, None)
        if trade is None:
            return None

        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = reason
        trade.stock_pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
        if trade.direction == "put":
            trade.direction_pnl_pct = -trade.stock_pnl_pct
        else:
            trade.direction_pnl_pct = trade.stock_pnl_pct

        self._closed_trades.append(trade)
        return trade

    def force_close_all(
        self, price_map: dict[str, float], time: datetime, reason: str = "日终强平"
    ) -> list[Trade]:
        closed = []
        for key in list(self._open_trades):
            trade = self._open_trades[key]
            price = price_map.get(trade.symbol, trade.entry_price)
            result = self.close_trade(trade.strategy_id, trade.symbol, price, time, reason)
            if result:
                closed.append(result)
        return closed

    def get_open_trade(self, strategy_id: str, symbol: str) -> Trade | None:
        return self._open_trades.get(self._key(strategy_id, symbol))

    def compute_results(self) -> BacktestResult:
        trades = self._closed_trades
        if not trades:
            return BacktestResult()

        winners = [t for t in trades if t.direction_pnl_pct > 0]
        losers = [t for t in trades if t.direction_pnl_pct <= 0]

        total_win = sum(t.direction_pnl_pct for t in winners)
        total_loss = sum(abs(t.direction_pnl_pct) for t in losers)

        # Max drawdown: track cumulative equity curve
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cumulative += t.direction_pnl_pct
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Trades per day
        if trades:
            days = set()
            for t in trades:
                days.add(t.entry_time.date())
            num_days = len(days) or 1
        else:
            num_days = 1

        # By strategy / by symbol breakdowns
        by_strategy: dict[str, dict] = {}
        by_symbol: dict[str, dict] = {}
        for t in trades:
            for group_key, group_dict in [
                (t.strategy_id, by_strategy),
                (t.symbol, by_symbol),
            ]:
                if group_key not in group_dict:
                    group_dict[group_key] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
                group_dict[group_key]["trades"] += 1
                if t.direction_pnl_pct > 0:
                    group_dict[group_key]["wins"] += 1
                group_dict[group_key]["total_pnl"] += t.direction_pnl_pct

        for d in list(by_strategy.values()) + list(by_symbol.values()):
            d["win_rate"] = d["wins"] / d["trades"] * 100 if d["trades"] else 0

        pnl_values = [t.direction_pnl_pct for t in trades]

        return BacktestResult(
            trades=trades,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=len(winners) / len(trades) * 100 if trades else 0,
            profit_factor=total_win / total_loss if total_loss > 0 else float("inf"),
            total_return_pct=sum(pnl_values),
            max_drawdown_pct=max_dd,
            avg_holding_minutes=(
                sum(t.holding_minutes for t in trades) / len(trades) if trades else 0
            ),
            avg_win_pct=total_win / len(winners) if winners else 0,
            avg_loss_pct=-total_loss / len(losers) if losers else 0,
            best_trade_pct=max(pnl_values) if pnl_values else 0,
            worst_trade_pct=min(pnl_values) if pnl_values else 0,
            trades_per_day=len(trades) / num_days,
            by_strategy=by_strategy,
            by_symbol=by_symbol,
        )
