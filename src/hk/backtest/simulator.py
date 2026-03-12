"""Simplified trade simulator for HK backtest.

Uses stock price percentage for P&L (not option premium).
Applies configurable leverage_factor for display purposes only.
"""

from __future__ import annotations

from datetime import time as dt_time

import pandas as pd

from src.hk import RegimeType
from src.hk.backtest import LevelEvent, RegimeEvalDay, SimTrade, SimResult
from src.utils.logger import setup_logger

logger = setup_logger("hk_backtest_sim")

# End-of-day forced exit time
EOD_EXIT_TIME = dt_time(15, 50)


class TradeSimulator:
    """Simulates trades from level/regime signals using stock price movements."""

    def __init__(
        self,
        tp_pct: float = 0.008,
        sl_pct: float = 0.003,
        slippage_per_leg: float = 0.0005,
        leverage_factor: float = 15.0,
        exclude_symbols: set[str] | None = None,
        morning_only_levels: bool = True,
        skip_signal_types: set[str] | None = None,
        exit_mode: str = "trailing",
        trailing_activation_pct: float = 0.005,
        trailing_trail_pct: float = 0.003,
    ) -> None:
        """
        Args:
            tp_pct: Take profit threshold (stock price %)
            sl_pct: Stop loss threshold (stock price %, positive value)
            slippage_per_leg: Slippage per trade leg (0.05% default)
            leverage_factor: Option leverage approximation (display only)
            exclude_symbols: Symbols to skip in simulation
            morning_only_levels: Only trade level signals from morning session
            skip_signal_types: Signal types to skip (e.g. {"BREAKOUT_long"})
            exit_mode: "fixed" (TP/SL only), "trailing", or "both" (first-to-trigger)
            trailing_activation_pct: PnL % to activate trailing stop (stock price)
            trailing_trail_pct: Drawdown from peak to trigger trailing exit (stock price)
        """
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.slippage_round_trip = slippage_per_leg * 2
        self.leverage_factor = leverage_factor
        self.exclude_symbols = exclude_symbols or set()
        self.morning_only_levels = morning_only_levels
        self.skip_signal_types = skip_signal_types or set()
        self.exit_mode = exit_mode
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_trail_pct = trailing_trail_pct

    def simulate_from_levels(
        self,
        bars_by_symbol: dict[str, pd.DataFrame],
        events: list[LevelEvent],
    ) -> SimResult:
        """Simulate trades from VAH/VAL touch events.

        VAH touch → short (expect reversal down)
        VAL touch → long (expect reversal up)
        """
        trades: list[SimTrade] = []

        for event in events:
            symbol = event.symbol
            if symbol in self.exclude_symbols:
                continue
            if symbol not in bars_by_symbol:
                continue
            if self.morning_only_levels and event.session != "morning":
                continue

            signal_type = "VAH_short" if event.level_type == "VAH" else "VAL_long"
            if signal_type in self.skip_signal_types:
                continue

            day_bars = bars_by_symbol[symbol]
            # Get bars from touch point onward for the same day
            touch_date = event.date.date() if hasattr(event.date, "date") else event.date
            same_day = day_bars[day_bars.index.date == touch_date]

            if same_day.empty:
                continue

            # Find touch bar index in same_day
            touch_bar_mask = same_day.index <= event.date
            if not touch_bar_mask.any():
                continue
            start_idx = touch_bar_mask.sum()

            entry_price = event.touch_price
            is_short = event.level_type == "VAH"

            trade = self._simulate_single_trade(
                same_day, start_idx, entry_price, is_short,
                signal_type, symbol, event.date, event.session,
            )
            if trade:
                trades.append(trade)

        return self._compute_sim_result(trades)

    def simulate_from_regimes(
        self,
        bars_by_symbol: dict[str, pd.DataFrame],
        regime_days: list[RegimeEvalDay],
    ) -> SimResult:
        """Simulate trades from regime classifications.

        GAP_AND_GO / TREND_DAY → trend following (long if above POC, short if below)
        FADE_CHOP → mean reversion toward POC (long if near VAL, short if near VAH)
        WHIPSAW/UNCLEAR/RANGE/BREAKOUT → no trade
        """
        trades: list[SimTrade] = []

        for day_eval in regime_days:
            if day_eval.predicted in (
                RegimeType.WHIPSAW, RegimeType.UNCLEAR,
                RegimeType.BREAKOUT, RegimeType.RANGE,
            ):
                continue

            symbol = day_eval.symbol
            if symbol in self.exclude_symbols:
                continue
            if symbol not in bars_by_symbol:
                continue

            all_bars = bars_by_symbol[symbol]
            same_day = all_bars[all_bars.index.date == day_eval.date]
            if len(same_day) < 10:
                continue

            # Entry after morning RVOL assessment window (e.g., 09:35)
            entry_bars = same_day[same_day.index.time >= dt_time(9, 35)]
            if entry_bars.empty:
                continue

            entry_price = float(entry_bars.iloc[0]["Open"])
            entry_time = entry_bars.index[0]
            start_idx_in_day = same_day.index.get_loc(entry_time)
            if isinstance(start_idx_in_day, slice):
                start_idx_in_day = start_idx_in_day.start

            if day_eval.predicted in (RegimeType.GAP_AND_GO, RegimeType.TREND_DAY):
                # Trend following: direction based on price vs POC
                is_short = entry_price < day_eval.poc
                signal_type = f"{day_eval.predicted.value.upper()}_{'short' if is_short else 'long'}"
            elif day_eval.predicted == RegimeType.FADE_CHOP:
                # Mean reversion toward POC
                dist_to_vah = abs(entry_price - day_eval.vah)
                dist_to_val = abs(entry_price - day_eval.val)
                is_short = dist_to_vah < dist_to_val  # nearer VAH → short
                signal_type = f"FADE_CHOP_{'short' if is_short else 'long'}"
            else:
                continue

            if signal_type in self.skip_signal_types:
                continue

            session = "morning"  # Regime trades start in morning
            trade = self._simulate_single_trade(
                same_day, start_idx_in_day + 1, entry_price, is_short,
                signal_type, symbol, entry_time, session,
            )
            if trade:
                trades.append(trade)

        return self._compute_sim_result(trades)

    def _simulate_single_trade(
        self,
        day_bars: pd.DataFrame,
        start_idx: int,
        entry_price: float,
        is_short: bool,
        signal_type: str,
        symbol: str,
        entry_time,
        session: str,
    ) -> SimTrade | None:
        """Run a single trade through bars, checking TP/SL/trailing/time exit."""
        if start_idx >= len(day_bars):
            return None

        peak_pnl_pct = 0.0
        trailing_active = False

        for j in range(start_idx, len(day_bars)):
            row = day_bars.iloc[j]
            bar_time = day_bars.index[j]
            t = bar_time.time() if hasattr(bar_time, "time") else pd.Timestamp(bar_time).time()

            # Check TP/SL at High and Low
            for check_price in [float(row["High"]), float(row["Low"])]:
                if is_short:
                    pnl_pct = (entry_price - check_price) / entry_price
                else:
                    pnl_pct = (check_price - entry_price) / entry_price

                # Track peak PnL for trailing stop
                if pnl_pct > peak_pnl_pct:
                    peak_pnl_pct = pnl_pct

                # Fixed TP check (for "fixed" and "both" modes)
                if self.exit_mode in ("fixed", "both") and pnl_pct >= self.tp_pct:
                    exit_price = entry_price * (1 - self.tp_pct if is_short else 1 + self.tp_pct)
                    return self._make_trade(
                        symbol, signal_type, entry_price, entry_time,
                        exit_price, bar_time, "take_profit", session,
                        peak_pnl_pct=peak_pnl_pct,
                    )

                # Trailing stop check (for "trailing" and "both" modes)
                if self.exit_mode in ("trailing", "both"):
                    if peak_pnl_pct >= self.trailing_activation_pct:
                        trailing_active = True
                    if trailing_active and (peak_pnl_pct - pnl_pct) >= self.trailing_trail_pct:
                        # Exit at the drawdown point
                        trail_exit_pnl = peak_pnl_pct - self.trailing_trail_pct
                        if is_short:
                            exit_price = entry_price * (1 - trail_exit_pnl)
                        else:
                            exit_price = entry_price * (1 + trail_exit_pnl)
                        return self._make_trade(
                            symbol, signal_type, entry_price, entry_time,
                            exit_price, bar_time, "trailing_stop", session,
                            peak_pnl_pct=peak_pnl_pct,
                        )

                # SL check (all modes)
                if pnl_pct <= -self.sl_pct:
                    exit_price = entry_price * (1 + self.sl_pct if is_short else 1 - self.sl_pct)
                    return self._make_trade(
                        symbol, signal_type, entry_price, entry_time,
                        exit_price, bar_time, "stop_loss", session,
                        peak_pnl_pct=peak_pnl_pct,
                    )

            # Time exit at 15:50
            if t >= EOD_EXIT_TIME:
                exit_price = float(row["Close"])
                return self._make_trade(
                    symbol, signal_type, entry_price, entry_time,
                    exit_price, bar_time, "time_exit", session,
                    peak_pnl_pct=peak_pnl_pct,
                )

        # If we ran out of bars, exit at last close
        last_row = day_bars.iloc[-1]
        exit_price = float(last_row["Close"])
        return self._make_trade(
            symbol, signal_type, entry_price, entry_time,
            exit_price, day_bars.index[-1], "eod_close", session,
            peak_pnl_pct=peak_pnl_pct,
        )

    def _make_trade(
        self,
        symbol: str,
        signal_type: str,
        entry_price: float,
        entry_time,
        exit_price: float,
        exit_time,
        exit_reason: str,
        session: str,
        peak_pnl_pct: float = 0.0,
    ) -> SimTrade:
        is_short = "short" in signal_type
        if is_short:
            stock_pnl = (entry_price - exit_price) / entry_price * 100
        else:
            stock_pnl = (exit_price - entry_price) / entry_price * 100

        net_pnl = stock_pnl - self.slippage_round_trip * 100
        leveraged_pnl = net_pnl * self.leverage_factor

        return SimTrade(
            symbol=symbol,
            signal_type=signal_type,
            entry_price=entry_price,
            entry_time=entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=exit_reason,
            stock_pnl_pct=stock_pnl,
            net_pnl_pct=net_pnl,
            leveraged_pnl_pct=leveraged_pnl,
            session=session,
            peak_pnl_pct=peak_pnl_pct * 100,
        )

    def _compute_sim_result(self, trades: list[SimTrade]) -> SimResult:
        """Compute aggregate statistics from trades."""
        if not trades:
            return SimResult()

        winners = [t for t in trades if t.net_pnl_pct > 0]
        losers = [t for t in trades if t.net_pnl_pct <= 0]

        total_win = sum(t.net_pnl_pct for t in winners)
        total_loss = sum(abs(t.net_pnl_pct) for t in losers)

        # Max drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cumulative += t.net_pnl_pct
            peak = max(peak, cumulative)
            max_dd = max(max_dd, peak - cumulative)

        # Breakdowns
        by_signal_type: dict[str, dict[str, float]] = {}
        by_symbol: dict[str, dict[str, float]] = {}
        by_regime: dict[str, dict[str, float]] = {}

        for t in trades:
            for key, group in [
                (t.signal_type, by_signal_type),
                (t.symbol, by_symbol),
                (t.signal_type.split("_")[0], by_regime),
            ]:
                if key not in group:
                    group[key] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
                group[key]["trades"] += 1
                if t.net_pnl_pct > 0:
                    group[key]["wins"] += 1
                group[key]["total_pnl"] += t.net_pnl_pct

        for d in list(by_signal_type.values()) + list(by_symbol.values()) + list(by_regime.values()):
            d["win_rate"] = d["wins"] / d["trades"] * 100 if d["trades"] else 0

        return SimResult(
            trades=trades,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=len(winners) / len(trades) * 100,
            profit_factor=total_win / total_loss if total_loss > 0 else float("inf"),
            total_return_pct=sum(t.net_pnl_pct for t in trades),
            max_drawdown_pct=max_dd,
            avg_win_pct=total_win / len(winners) if winners else 0,
            avg_loss_pct=-total_loss / len(losers) if losers else 0,
            expectancy_pct=sum(t.net_pnl_pct for t in trades) / len(trades),
            by_signal_type=by_signal_type,
            by_symbol=by_symbol,
            by_regime=by_regime,
        )
