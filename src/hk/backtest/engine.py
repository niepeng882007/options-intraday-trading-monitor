"""HK backtest engine — orchestrates data loading, evaluation, and simulation."""

from __future__ import annotations

import pandas as pd

from src.hk.backtest import HKBacktestResult
from src.hk.backtest.evaluators import evaluate_levels, evaluate_regimes
from src.hk.backtest.simulator import TradeSimulator
from src.utils.logger import setup_logger

logger = setup_logger("hk_backtest_engine")


class HKBacktestEngine:
    """Orchestrates the HK backtest pipeline."""

    def __init__(
        self,
        vp_lookback_days: int = 5,
        rvol_lookback_days: int = 10,
        bounce_thresholds: list[float] | None = None,
        bounce_window_bars: int = 15,
        value_area_pct: float = 0.70,
        morning_rvol_minutes: int = 5,
        breakout_rvol: float = 1.05,
        range_rvol: float = 0.95,
        run_sim: bool = True,
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
        self.vp_lookback_days = vp_lookback_days
        self.rvol_lookback_days = rvol_lookback_days
        self.bounce_thresholds = bounce_thresholds or [0.003, 0.005, 0.007, 0.010]
        self.bounce_window_bars = bounce_window_bars
        self.value_area_pct = value_area_pct
        self.morning_rvol_minutes = morning_rvol_minutes
        self.breakout_rvol = breakout_rvol
        self.range_rvol = range_rvol
        self.run_sim = run_sim
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.slippage_per_leg = slippage_per_leg
        self.leverage_factor = leverage_factor
        self.exclude_symbols = exclude_symbols or set()
        self.morning_only_levels = morning_only_levels
        self.skip_signal_types = skip_signal_types or set()
        self.exit_mode = exit_mode
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_trail_pct = trailing_trail_pct

    def run(self, bars_by_symbol: dict[str, pd.DataFrame]) -> HKBacktestResult:
        """Run the full HK backtest pipeline.

        Steps:
        1. Evaluate VAH/VAL level bounce rates
        2. Evaluate regime classification accuracy
        3. (Optional) Simulate trades from signals

        Args:
            bars_by_symbol: Symbol -> 1m bars DataFrame

        Returns:
            HKBacktestResult with all evaluation results
        """
        total_bars = sum(len(df) for df in bars_by_symbol.values())
        all_dates = set()
        for df in bars_by_symbol.values():
            all_dates.update(df.index.date)

        logger.info(
            "Starting HK backtest: %d symbols, %d total bars, %d trading days",
            len(bars_by_symbol), total_bars, len(all_dates),
        )

        # Step 1: Level evaluation
        logger.info("Step 1: Evaluating VAH/VAL level bounce rates...")
        level_eval = evaluate_levels(
            bars_by_symbol,
            vp_lookback_days=self.vp_lookback_days,
            bounce_thresholds=self.bounce_thresholds,
            bounce_window_bars=self.bounce_window_bars,
            value_area_pct=self.value_area_pct,
            exclude_symbols=self.exclude_symbols,
        )
        logger.info(
            "Level evaluation: %d touch events found", len(level_eval.events),
        )

        # Step 2: Regime evaluation
        logger.info("Step 2: Evaluating regime classification accuracy...")
        regime_eval = evaluate_regimes(
            bars_by_symbol,
            vp_lookback_days=self.vp_lookback_days,
            rvol_lookback_days=self.rvol_lookback_days,
            morning_rvol_minutes=self.morning_rvol_minutes,
            breakout_rvol=self.breakout_rvol,
            range_rvol=self.range_rvol,
            exclude_symbols=self.exclude_symbols,
        )
        logger.info(
            "Regime evaluation: %d day-evaluations", len(regime_eval.days),
        )

        # Step 3: Trade simulation (optional)
        sim_result = None
        if self.run_sim:
            logger.info("Step 3: Running trade simulation...")
            simulator = TradeSimulator(
                tp_pct=self.tp_pct,
                sl_pct=self.sl_pct,
                slippage_per_leg=self.slippage_per_leg,
                leverage_factor=self.leverage_factor,
                exclude_symbols=self.exclude_symbols,
                morning_only_levels=self.morning_only_levels,
                skip_signal_types=self.skip_signal_types,
                exit_mode=self.exit_mode,
                trailing_activation_pct=self.trailing_activation_pct,
                trailing_trail_pct=self.trailing_trail_pct,
            )

            # Simulate from levels
            level_sim = simulator.simulate_from_levels(
                bars_by_symbol, level_eval.events,
            )

            # Simulate from regimes
            regime_sim = simulator.simulate_from_regimes(
                bars_by_symbol, regime_eval.days,
            )

            # Merge both sim results
            all_trades = level_sim.trades + regime_sim.trades
            sim_result = simulator._compute_sim_result(all_trades)
            logger.info(
                "Simulation: %d trades (levels=%d, regimes=%d)",
                sim_result.total_trades, len(level_sim.trades), len(regime_sim.trades),
            )

        return HKBacktestResult(
            level_eval=level_eval,
            regime_eval=regime_eval,
            sim_result=sim_result,
            symbols=list(bars_by_symbol.keys()),
            days=len(all_dates),
            data_bars=total_bars,
        )
