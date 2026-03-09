from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.indicator.engine import IndicatorEngine, MIN_BARS_WARMUP
from src.strategy.loader import StrategyConfig, StrategyLoader
from src.strategy.matcher import RuleMatcher
from src.strategy.state import StrategyState, StrategyStateManager
from src.backtest.trade_tracker import BacktestResult, TradeTracker
from src.utils.logger import setup_logger

logger = setup_logger("backtest_engine")

ET = timezone(timedelta(hours=-5))


class BacktestEngine:
    """Replays historical bars through the live pipeline to simulate trading."""

    def __init__(
        self,
        strategies: list[StrategyConfig],
        symbols: list[str] | None = None,
        midday_no_trade: bool = True,
        midday_start: int = 11 * 60,
        midday_end: int = 13 * 60,
        max_daily_loss_pct: float | None = -1.5,
    ) -> None:
        self.strategies = strategies
        self.symbols = symbols or self._collect_symbols(strategies)
        self.midday_no_trade = midday_no_trade
        self.midday_start = midday_start
        self.midday_end = midday_end
        self.max_daily_loss_pct = max_daily_loss_pct

        self.indicator_engine = IndicatorEngine()
        self.rule_matcher = RuleMatcher()
        self.state_manager = StrategyStateManager()
        self.trade_tracker = TradeTracker()
        self._daily_pnl: float = 0.0
        self._cooldowns: dict[str, datetime] = {}  # Fix 3: in-memory cooldowns

    @staticmethod
    def _collect_symbols(strategies: list[StrategyConfig]) -> list[str]:
        symbols: set[str] = set()
        for s in strategies:
            symbols.update(s.underlyings)
        return sorted(symbols)

    def run(self, bars_by_symbol: dict[str, pd.DataFrame]) -> BacktestResult:
        # Collect all trading days across all symbols
        all_days: set = set()
        for df in bars_by_symbol.values():
            all_days.update(df.index.date)
        trading_days = sorted(all_days)

        if not trading_days:
            logger.warning("No trading days found in data")
            return self.trade_tracker.compute_results()

        logger.info(
            "Starting backtest: %d strategies, %d symbols, %d days",
            len(self.strategies), len(bars_by_symbol), len(trading_days),
        )

        for day_idx, day in enumerate(trading_days):
            self._reset_day()

            # Get all bars for this day across symbols, sorted chronologically
            day_bars: list[tuple[str, pd.Timestamp, pd.Series]] = []
            for symbol, df in bars_by_symbol.items():
                day_df = df[df.index.date == day]
                for ts, row in day_df.iterrows():
                    day_bars.append((symbol, ts, row))

            day_bars.sort(key=lambda x: x[1])

            if not day_bars:
                continue

            bars_processed = 0
            for symbol, ts, row in day_bars:
                self._process_bar(symbol, ts, row, day)
                bars_processed += 1

            # Force close all open positions at end of day
            last_ts = day_bars[-1][1]
            last_prices = {}
            for symbol, df in bars_by_symbol.items():
                day_df = df[df.index.date == day]
                if not day_df.empty:
                    last_prices[symbol] = float(day_df["Close"].iloc[-1])

            bar_dt = last_ts.to_pydatetime() if hasattr(last_ts, 'to_pydatetime') else last_ts
            if bar_dt.tzinfo is None:
                bar_dt = bar_dt.replace(tzinfo=ET)
            self.trade_tracker.force_close_all(last_prices, bar_dt, "日终强平")

            # Also reset state manager for any still-open states
            for s in self.strategies:
                self.state_manager.reset(s.strategy_id)

            logger.info(
                "Day %d/%d (%s): %d bars processed",
                day_idx + 1, len(trading_days), day, bars_processed,
            )

        # Clean up simulated time
        RuleMatcher._simulated_time = None

        result = self.trade_tracker.compute_results()
        logger.info(
            "Backtest complete: %d trades, %.1f%% win rate, %+.2f%% return",
            result.total_trades, result.win_rate, result.total_return_pct,
        )
        return result

    def _reset_day(self) -> None:
        """Reset per-day state without losing cross-day indicator history."""
        for s in self.strategies:
            self.state_manager.reset(s.strategy_id)
        self.rule_matcher._confirmation_counts.clear()
        # Reset intraday BBW history for all symbols
        for sym_data in self.indicator_engine._data.values():
            sym_data.bbw_history.clear()
        # Reset daily PnL tracker
        self._daily_pnl = 0.0
        # Reset cooldowns per day
        self._cooldowns.clear()
        # Do NOT reset _prev_values (crosses_above needs continuity)
        # Do NOT reset indicator_engine bar data (EMA/RSI need history)

    def _process_bar(
        self,
        symbol: str,
        ts: pd.Timestamp,
        row: pd.Series,
        day,
    ) -> None:
        open_p = float(row["Open"])
        high_p = float(row["High"])
        low_p = float(row["Low"])
        close_p = float(row["Close"])
        volume = float(row["Volume"])

        bar_dt = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=ET)
        RuleMatcher._simulated_time = bar_dt

        # Phase 1: Intra-bar simulation (partial candle effect)
        # Simulates update_live_price() calls that happen in live trading,
        # which create partial-candle indicators (small body, close≈open near VWAP).
        if not self.indicator_engine.needs_warmup(symbol, MIN_BARS_WARMUP):
            stub = pd.DataFrame(
                {"Open": [open_p], "High": [open_p], "Low": [open_p],
                 "Close": [open_p], "Volume": [volume]},
                index=[ts],
            )
            self.indicator_engine.update_bars(symbol, stub)

            for sim_price in [open_p, high_p, low_p]:
                indicators = self.indicator_engine.update_live_price(
                    symbol, sim_price, ts.timestamp()
                )
                if any(v is not None for v in indicators.values()):
                    self._evaluate_at_price(symbol, sim_price, bar_dt, indicators)

        # Phase 2: Final bar with complete OHLCV
        final_bar = pd.DataFrame(
            {"Open": [open_p], "High": [high_p], "Low": [low_p],
             "Close": [close_p], "Volume": [volume]},
            index=[ts],
        )
        self.indicator_engine.update_bars(symbol, final_bar)

        if self.indicator_engine.needs_warmup(symbol, MIN_BARS_WARMUP):
            return

        indicators = self.indicator_engine.calculate_all(symbol)
        self._evaluate_at_price(symbol, close_p, bar_dt, indicators)

    def _evaluate_at_price(
        self,
        symbol: str,
        price: float,
        bar_dt: datetime,
        indicators: dict,
    ) -> None:
        """Check exits and entries at a given price point."""
        # 1. Check exits for HOLDING positions
        for strategy in self.strategies:
            if symbol not in strategy.underlyings:
                continue

            state = self.state_manager.get_state(strategy.strategy_id, symbol)
            if state.state != StrategyState.HOLDING:
                continue

            direction = strategy.option_filter.get("type", "call")
            self.state_manager.update_highest_price(
                strategy.strategy_id, symbol, price
            )

            entry_price = state.position.entry_price
            minutes_to_close = self._minutes_to_close(bar_dt)

            exit_signal = self.rule_matcher.evaluate_exit(
                strategy, symbol, price, entry_price,
                minutes_to_close,
                highest_price=state.position.highest_price,
                lowest_price=state.position.lowest_price,
                direction=direction,
                indicators_by_tf=indicators,
            )

            if exit_signal:
                self.state_manager.trigger_exit(strategy.strategy_id, symbol)
                self.state_manager.confirm_exit(strategy.strategy_id, symbol)
                closed = self.trade_tracker.close_trade(
                    strategy.strategy_id, symbol,
                    price, bar_dt,
                    exit_signal.exit_reason,
                )
                if closed:
                    self._daily_pnl += closed.direction_pnl_pct

        # 2. Check entries for WATCHING strategies
        if self.midday_no_trade:
            t = bar_dt.hour * 60 + bar_dt.minute
            if self.midday_start <= t < self.midday_end:
                return

        if self.max_daily_loss_pct is not None and self._daily_pnl <= self.max_daily_loss_pct:
            return

        for strategy in self.strategies:
            if symbol not in strategy.underlyings:
                continue

            state = self.state_manager.get_state(strategy.strategy_id, symbol)
            if state.state != StrategyState.WATCHING:
                continue

            if not self._is_in_trading_window(strategy, bar_dt):
                continue

            if not self._check_market_context(strategy, symbol, indicators):
                continue

            # Cooldown check
            cd_key = f"{strategy.strategy_id}:{symbol}"
            if cd_key in self._cooldowns and bar_dt < self._cooldowns[cd_key]:
                continue

            entry_signal = self.rule_matcher.evaluate_entry(
                strategy, symbol, indicators,
            )
            if entry_signal is None:
                continue

            quality = self.rule_matcher.evaluate_entry_quality(strategy, indicators)
            min_score = strategy.entry_quality_filters.get("min_score", 0)
            if quality.score < min_score:
                continue

            signal_id = self.state_manager.trigger_entry(strategy.strategy_id, symbol)
            if signal_id is None:
                continue

            self.state_manager.confirm_entry(signal_id, price)

            direction = strategy.option_filter.get("type", "call")

            self.trade_tracker.open_trade(
                strategy_id=strategy.strategy_id,
                name=strategy.name,
                symbol=symbol,
                direction=direction,
                price=price,
                time=bar_dt,
                quality_score=quality.score,
                quality_grade=quality.grade,
            )

            # Set cooldown after successful entry
            self._cooldowns[cd_key] = bar_dt + timedelta(
                seconds=strategy.cooldown_seconds
            )

            logger.debug(
                "Entry: %s %s @ $%.2f [%s] quality=%s(%d)",
                strategy.strategy_id, symbol, price,
                direction, quality.grade, quality.score,
            )

    def _check_market_context(
        self,
        strategy: StrategyConfig,
        symbol: str,
        indicators: dict,
    ) -> bool:
        mcf = strategy.market_context_filters
        if not mcf:
            return True

        spy_ind = self.indicator_engine.get_last("SPY", "5m")
        if spy_ind:
            max_drop = mcf.get("max_spy_day_drop_pct")
            if max_drop is not None and spy_ind.day_change_pct is not None:
                if spy_ind.day_change_pct < max_drop:
                    return False

        max_adx = mcf.get("max_adx")
        if max_adx is not None:
            sym_ind = indicators.get("5m")
            if sym_ind and sym_ind.adx is not None and sym_ind.adx > max_adx:
                return False

        min_adx = mcf.get("min_adx")
        if min_adx is not None:
            sym_ind = indicators.get("5m")
            if sym_ind and sym_ind.adx is not None and sym_ind.adx < min_adx:
                return False

        return True

    @staticmethod
    def _is_in_trading_window(strategy: StrategyConfig, now: datetime) -> bool:
        start_str = strategy.trading_window_start
        end_str = strategy.trading_window_end
        if not start_str or not end_str:
            return True

        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))

        window_start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        window_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return window_start <= now <= window_end

    @staticmethod
    def _minutes_to_close(now: datetime) -> int:
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        delta = (market_close - now).total_seconds()
        return max(0, int(delta / 60))
