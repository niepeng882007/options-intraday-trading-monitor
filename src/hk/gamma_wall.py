from __future__ import annotations
import pandas as pd
from src.hk import GammaWallResult
from src.utils.logger import setup_logger

logger = setup_logger("hk_gamma_wall")


def calculate_gamma_wall(
    chain: pd.DataFrame,
    current_price: float,
    top_n: int = 5,
) -> GammaWallResult:
    """Calculate Gamma Wall from option chain with OI data.

    Gamma wall = strike price with maximum Open Interest.
    - Call wall: resistance (large call OI = hedging sellers delta-hedge by selling at that strike)
    - Put wall: support (large put OI = hedging sellers delta-hedge by buying at that strike)

    Max Pain = strike where total $ value of OI (calls + puts) is maximized
    (the price at which most options expire worthless).

    Args:
        chain: DataFrame with columns: code, option_type (CALL/PUT), strike_price, open_interest
               This comes from HKCollector.get_option_chain_with_oi()
        current_price: current underlying price
        top_n: number of top strikes to consider

    Returns:
        GammaWallResult
    """
    if chain.empty or "open_interest" not in chain.columns:
        return GammaWallResult(
            call_wall_strike=0, put_wall_strike=0, max_pain=0,
        )

    # Separate calls and puts
    calls = chain[chain["option_type"].str.upper() == "CALL"].copy()
    puts = chain[chain["option_type"].str.upper() == "PUT"].copy()

    # Aggregate OI by strike
    call_oi = {}
    if not calls.empty:
        grouped = calls.groupby("strike_price")["open_interest"].sum()
        call_oi = grouped.to_dict()

    put_oi = {}
    if not puts.empty:
        grouped = puts.groupby("strike_price")["open_interest"].sum()
        put_oi = grouped.to_dict()

    # Call wall: strike with max call OI (above current price preferred)
    call_wall = 0.0
    if call_oi:
        # Prefer strikes above current price
        above = {k: v for k, v in call_oi.items() if k >= current_price and v > 0}
        if above:
            call_wall = max(above, key=above.get)
        else:
            # Fallback to any max
            nonzero = {k: v for k, v in call_oi.items() if v > 0}
            if nonzero:
                call_wall = max(nonzero, key=nonzero.get)

    # Put wall: strike with max put OI (below current price preferred)
    put_wall = 0.0
    if put_oi:
        below = {k: v for k, v in put_oi.items() if k <= current_price and v > 0}
        if below:
            put_wall = max(below, key=below.get)
        else:
            nonzero = {k: v for k, v in put_oi.items() if v > 0}
            if nonzero:
                put_wall = max(nonzero, key=nonzero.get)

    # Max Pain calculation
    # For each strike, calculate total pain ($ value of ITM options)
    all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
    max_pain = 0.0
    max_pain_value = float("inf")

    for test_price in all_strikes:
        total_pain = 0.0
        # Call pain: for each call strike < test_price, OI * (test_price - strike)
        for strike, oi in call_oi.items():
            if test_price > strike:
                total_pain += oi * (test_price - strike)
        # Put pain: for each put strike > test_price, OI * (strike - test_price)
        for strike, oi in put_oi.items():
            if test_price < strike:
                total_pain += oi * (strike - test_price)

        if total_pain < max_pain_value:
            max_pain_value = total_pain
            max_pain = test_price

    logger.info(
        "Gamma Wall: Call=%.0f (OI=%d), Put=%.0f (OI=%d), MaxPain=%.0f",
        call_wall, call_oi.get(call_wall, 0),
        put_wall, put_oi.get(put_wall, 0),
        max_pain,
    )

    return GammaWallResult(
        call_wall_strike=call_wall,
        put_wall_strike=put_wall,
        max_pain=max_pain,
        call_oi_by_strike=call_oi,
        put_oi_by_strike=put_oi,
    )


def format_gamma_wall_message(gw: GammaWallResult, symbol: str = "") -> str:
    """Format gamma wall info for Telegram."""
    if gw.call_wall_strike == 0 and gw.put_wall_strike == 0:
        return f"Gamma Wall ({symbol}): 无有效 OI 数据"

    lines = [f"<b>Gamma Wall {symbol}</b>"]

    if gw.call_wall_strike > 0:
        oi = gw.call_oi_by_strike.get(gw.call_wall_strike, 0)
        lines.append(f"  🔴 Call Wall (阻力): {gw.call_wall_strike:,.0f} (OI={oi:,})")

    if gw.put_wall_strike > 0:
        oi = gw.put_oi_by_strike.get(gw.put_wall_strike, 0)
        lines.append(f"  🟢 Put Wall (支撑): {gw.put_wall_strike:,.0f} (OI={oi:,})")

    if gw.max_pain > 0:
        lines.append(f"  🎯 Max Pain: {gw.max_pain:,.0f}")

    return "\n".join(lines)
