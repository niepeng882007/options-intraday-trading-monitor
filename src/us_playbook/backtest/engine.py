"""US backtest engine -- orchestrates data loading, evaluation, and simulation."""

from __future__ import annotations

import pandas as pd

from src.us_playbook.backtest import USBacktestResult
from src.us_playbook.backtest.evaluators import evaluate_levels, evaluate_regimes
from src.us_playbook.backtest.simulator import USTradeSimulator
from src.utils.logger import setup_logger

logger = setup_logger("us_backtest_engine")


class USBacktestEngine:
    """Orchestrates the US backtest pipeline."""

    def __init__(
        self,
        vp_lookback_days: int = 5,
        rvol_lookback_days: int = 10,
        skip_open_minutes: int = 3,
        eval_minutes: int = 8,
        bounce_thresholds: list[float] | None = None,
        bounce_window_bars: int = 15,
        value_area_pct: float = 0.70,
        regime_cfg: dict | None = None,
        run_sim: bool = True,
        tp_pct: float = 0.005,
        sl_pct: float = 0.0025,
        slippage_per_leg: float = 0.0003,
        leverage_factor: float = 10.0,
        exclude_symbols: set[str] | None = None,
        skip_signal_types: set[str] | None = None,
        exit_mode: str = "trailing",
        trailing_activation_pct: float = 0.004,
        trailing_trail_pct: float = 0.002,
        no_adaptive: bool = False,
    ) -> None:
        self.vp_lookback_days = vp_lookback_days
        self.rvol_lookback_days = rvol_lookback_days
        self.skip_open_minutes = skip_open_minutes
        self.eval_minutes = eval_minutes
        self.bounce_thresholds = bounce_thresholds or [0.003, 0.005, 0.007, 0.010]
        self.bounce_window_bars = bounce_window_bars
        self.value_area_pct = value_area_pct
        self.regime_cfg = regime_cfg or {}
        self.run_sim = run_sim
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.slippage_per_leg = slippage_per_leg
        self.leverage_factor = leverage_factor
        self.exclude_symbols = exclude_symbols or set()
        self.skip_signal_types = skip_signal_types or set()
        self.exit_mode = exit_mode
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_trail_pct = trailing_trail_pct
        self.no_adaptive = no_adaptive
        self.recency_decay = 0.15  # VP recency decay (match production default)

    def run(self, bars_by_symbol: dict[str, pd.DataFrame]) -> USBacktestResult:
        """Run the full US backtest pipeline.

        Steps:
        1. Evaluate VAH/VAL/PDH/PDL level bounce rates
        2. Evaluate regime classification accuracy
        3. (Optional) Simulate trades from signals
        """
        total_bars = sum(len(df) for df in bars_by_symbol.values())
        all_dates = set()
        for df in bars_by_symbol.values():
            all_dates.update(df.index.date)

        logger.info(
            "Starting US backtest: %d symbols, %d total bars, %d trading days",
            len(bars_by_symbol), total_bars, len(all_dates),
        )

        # Step 1: Level evaluation
        logger.info("Step 1: Evaluating VAH/VAL/PDH/PDL level bounce rates...")
        level_eval = evaluate_levels(
            bars_by_symbol,
            vp_lookback_days=self.vp_lookback_days,
            bounce_thresholds=self.bounce_thresholds,
            bounce_window_bars=self.bounce_window_bars,
            value_area_pct=self.value_area_pct,
            exclude_symbols=self.exclude_symbols,
            recency_decay=self.recency_decay,
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
            skip_open_minutes=self.skip_open_minutes,
            eval_minutes=self.eval_minutes,
            value_area_pct=self.value_area_pct,
            regime_cfg=self.regime_cfg,
            exclude_symbols=self.exclude_symbols,
            no_adaptive=self.no_adaptive,
            recency_decay=self.recency_decay,
        )
        logger.info(
            "Regime evaluation: %d day-evaluations", len(regime_eval.days),
        )

        # Step 3: Trade simulation (optional)
        sim_result = None
        if self.run_sim:
            logger.info("Step 3: Running trade simulation...")
            simulator = USTradeSimulator(
                tp_pct=self.tp_pct,
                sl_pct=self.sl_pct,
                slippage_per_leg=self.slippage_per_leg,
                leverage_factor=self.leverage_factor,
                exclude_symbols=self.exclude_symbols,
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

        return USBacktestResult(
            level_eval=level_eval,
            regime_eval=regime_eval,
            sim_result=sim_result,
            symbols=list(bars_by_symbol.keys()),
            days=len(all_dates),
            data_bars=total_bars,
        )
