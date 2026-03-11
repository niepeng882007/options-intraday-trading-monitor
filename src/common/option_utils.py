"""Shared option recommendation utilities — used by both HK and US modules."""

from __future__ import annotations

import pandas as pd

from src.common.types import (
    ChaseRiskResult,
    OptionLeg,
    SpreadMetrics,
    VolumeProfileResult,
)


# ── Moneyness classification ──


def classify_moneyness(strike: float, price: float, option_type: str) -> str:
    """Classify a strike as ATM / OTM X% / ITM X%."""
    if price == 0:
        return "N/A"
    pct = abs(strike - price) / price * 100
    if pct < 0.5:
        return "ATM"
    if option_type.lower() == "call":
        if strike > price:
            return f"OTM {pct:.1f}%"
        return f"ITM {pct:.1f}%"
    else:  # put
        if strike < price:
            return f"OTM {pct:.1f}%"
        return f"ITM {pct:.1f}%"


# ── Option leg construction ──


def option_leg_from_row(
    row: pd.Series,
    side: str,
    price: float,
    option_type: str,
) -> OptionLeg:
    """Build an OptionLeg from a chain DataFrame row.

    Uses .get() style access for safe handling of missing columns.
    """
    strike = float(row["strike_price"])
    return OptionLeg(
        side=side,
        option_type=option_type.lower(),
        strike=strike,
        pct_from_price=(strike - price) / price * 100 if price > 0 else 0.0,
        moneyness=classify_moneyness(strike, price, option_type),
        delta=float(row["delta"]) if "delta" in row and pd.notna(row.get("delta")) else None,
        open_interest=int(row["open_interest"]) if "open_interest" in row and pd.notna(row.get("open_interest")) else None,
        last_price=float(row["last_price"]) if "last_price" in row and pd.notna(row.get("last_price")) else None,
        implied_volatility=(
            float(row["implied_volatility"])
            if "implied_volatility" in row and pd.notna(row.get("implied_volatility"))
            else None
        ),
        volume=int(row["snap_volume"]) if "snap_volume" in row and pd.notna(row.get("snap_volume")) else None,
    )


# ── Spread metrics ──


def calculate_spread_metrics(
    legs: list[OptionLeg],
    action: str,
) -> SpreadMetrics | None:
    """Calculate P&L metrics for a vertical spread."""
    if len(legs) < 2:
        return None

    sell_leg = next((l for l in legs if l.side == "sell"), None)
    buy_leg = next((l for l in legs if l.side == "buy"), None)
    if not sell_leg or not buy_leg:
        return None

    sell_price = sell_leg.last_price or 0.0
    buy_price = buy_leg.last_price or 0.0
    if sell_price <= 0 or buy_price <= 0:
        return None

    net_credit = sell_price - buy_price
    if net_credit <= 0:
        return None

    strike_width = abs(buy_leg.strike - sell_leg.strike)
    if strike_width <= 0:
        return None

    max_loss = strike_width - net_credit

    # Breakeven
    if action == "bear_call_spread":
        breakeven = sell_leg.strike + net_credit
    elif action == "bull_put_spread":
        breakeven = sell_leg.strike - net_credit
    else:
        breakeven = 0.0

    risk_reward = net_credit / max_loss if max_loss > 0 else 0.0

    # Win probability from sold leg delta
    win_prob = 0.0
    if sell_leg.delta is not None:
        win_prob = 1.0 - abs(sell_leg.delta)

    return SpreadMetrics(
        net_credit=net_credit,
        max_profit=net_credit,
        max_loss=max_loss,
        breakeven=breakeven,
        risk_reward_ratio=risk_reward,
        win_probability=win_prob,
    )


MIN_SPREAD_RR = 0.10  # Minimum R:R ratio for spread recommendation


def is_positive_ev(metrics: SpreadMetrics) -> bool:
    """Check if a spread has positive expected value.

    Rejects if R:R < 0.10 or EV < 0 (win_prob * credit - loss_prob * max_loss).
    """
    if metrics.risk_reward_ratio < MIN_SPREAD_RR:
        return False
    if metrics.win_probability > 0 and metrics.max_loss > 0:
        ev = (metrics.win_probability * metrics.net_credit
              - (1 - metrics.win_probability) * metrics.max_loss)
        if ev < 0:
            return False
    return True


# ── Single leg recommendation ──


def recommend_single_leg(
    direction: str,
    chain_df: pd.DataFrame,
    price: float,
    expiry: str,
    prefer_atm: bool = False,
    min_oi: int = 50,
    delta_min: float = 0.30,
    delta_max: float = 0.50,
) -> OptionLeg | None:
    """Pick best single-leg option.

    Call: ATM or slightly OTM (strike >= price, prefer delta in range)
    Put:  ATM or slightly OTM (strike <= price, prefer delta in range)

    When prefer_atm=True, restrict strikes to within ±1% of price (ATM zone).
    """
    if chain_df.empty:
        return None

    opt_type = "CALL" if direction == "bullish" else "PUT"
    expiry_match = chain_df["strike_time"].astype(str).str[:10] == expiry
    type_match = chain_df["option_type"].str.upper() == opt_type
    candidates = chain_df[expiry_match & type_match].copy()

    if candidates.empty:
        return None

    # Filter by OI (skip if all OI=0, meaning snapshot data was unavailable)
    if "open_interest" in candidates.columns:
        has_oi_data = (candidates["open_interest"] > 0).any()
        if has_oi_data:
            candidates = candidates[candidates["open_interest"] >= min_oi]
    if candidates.empty:
        return None

    # Prefer ATM/slightly OTM
    candidates = candidates.copy()
    candidates["dist"] = abs(candidates["strike_price"] - price)

    # When prefer_atm, restrict to strikes within ±1% of price
    if prefer_atm and price > 0:
        atm_zone = candidates[candidates["dist"] <= price * 0.01]
        if not atm_zone.empty:
            candidates = atm_zone

    candidates = candidates.sort_values("dist")

    # Try delta-based selection (when Greeks are available)
    has_delta = (
        "delta" in candidates.columns
        and not prefer_atm
        and candidates["delta"].notna().any()
        and (candidates["delta"].abs() > 0).any()
    )
    if has_delta:
        abs_delta = candidates["delta"].abs()
        good_delta = candidates[(abs_delta >= delta_min) & (abs_delta <= delta_max)]
        if not good_delta.empty:
            candidates = good_delta.sort_values("dist")

    # P2-3: Bid-ask spread filter — reject illiquid contracts
    if "bid_price" in candidates.columns and "ask_price" in candidates.columns:
        valid_spread = candidates[
            (candidates["bid_price"] > 0) & (candidates["ask_price"] > 0)
        ]
        if not valid_spread.empty:
            mid = (valid_spread["ask_price"] + valid_spread["bid_price"]) / 2
            spread_pct = (valid_spread["ask_price"] - valid_spread["bid_price"]) / mid
            tight = valid_spread[spread_pct <= 0.05]
            if not tight.empty:
                candidates = tight.sort_values("dist")

    best = candidates.iloc[0]
    return option_leg_from_row(best, side="buy", price=price, option_type=opt_type)


# ── Spread recommendation ──


def recommend_spread(
    direction: str,
    chain_df: pd.DataFrame,
    price: float,
    expiry: str,
    min_oi: int = 50,
) -> list[OptionLeg] | None:
    """Build vertical spread (2 legs).

    Bullish (Bull Put Spread): sell higher Put + buy lower Put
    Bearish (Bear Call Spread): sell lower Call + buy higher Call
    """
    if chain_df.empty:
        return None

    expiry_match = chain_df["strike_time"].astype(str).str[:10] == expiry

    if direction == "bullish":
        puts = chain_df[expiry_match & (chain_df["option_type"].str.upper() == "PUT")].copy()
        if "open_interest" in puts.columns and (puts["open_interest"] > 0).any():
            puts = puts[puts["open_interest"] >= min_oi]
        if len(puts) < 2:
            return None
        puts = puts.sort_values("strike_price", ascending=False)
        # Sell ATM/slightly OTM put, buy further OTM put
        atm_puts = puts[puts["strike_price"] <= price]
        if len(atm_puts) < 2:
            return None
        sell_leg = atm_puts.iloc[0]
        buy_leg = atm_puts.iloc[1]
        return [
            option_leg_from_row(sell_leg, side="sell", price=price, option_type="PUT"),
            option_leg_from_row(buy_leg, side="buy", price=price, option_type="PUT"),
        ]
    else:  # bearish
        calls = chain_df[expiry_match & (chain_df["option_type"].str.upper() == "CALL")].copy()
        if "open_interest" in calls.columns and (calls["open_interest"] > 0).any():
            calls = calls[calls["open_interest"] >= min_oi]
        if len(calls) < 2:
            return None
        calls = calls.sort_values("strike_price")
        atm_calls = calls[calls["strike_price"] >= price]
        if len(atm_calls) < 2:
            return None
        sell_leg = atm_calls.iloc[0]
        buy_leg = atm_calls.iloc[1]
        return [
            option_leg_from_row(sell_leg, side="sell", price=price, option_type="CALL"),
            option_leg_from_row(buy_leg, side="buy", price=price, option_type="CALL"),
        ]


# ── Chase risk assessment ──


def assess_chase_risk(
    price: float,
    vwap: float,
    vp: VolumeProfileResult,
    direction: str,
    is_afternoon: bool = False,
    vwap_moderate_pct: float = 2.0,
    vwap_high_pct: float = 3.5,
    va_moderate_pct: float = 2.5,
    va_high_pct: float = 4.0,
    afternoon_tighten_pct: float = 0.5,
    minutes_to_close: int | None = None,
) -> ChaseRiskResult:
    """Assess chase risk based on VWAP deviation and VA boundary distance.

    Default thresholds are HK values. US callers pass tighter values explicitly.

    If ``minutes_to_close`` is provided, uses proportional tightening instead of
    the binary ``is_afternoon`` flag. This gives a gradual increase in chase risk
    as the trading day progresses (P2-2).

    Only checks directional extension (bullish above VWAP/VAH, bearish below VWAP/VAL).
    Returns ChaseRiskResult with level, reasons, and pullback target.
    """
    if direction == "neutral" or vwap <= 0 or price <= 0:
        return ChaseRiskResult()

    # Tighten thresholds based on time remaining
    if minutes_to_close is not None and minutes_to_close < 240:
        # Proportional tightening: 0% at 240min left → 100% at 0min left
        tighten_factor = (240 - minutes_to_close) / 240
        tighten_amount = afternoon_tighten_pct * tighten_factor
        vwap_moderate_pct -= tighten_amount
        vwap_high_pct -= tighten_amount
        va_moderate_pct -= tighten_amount
        va_high_pct -= tighten_amount
    elif is_afternoon:
        vwap_moderate_pct -= afternoon_tighten_pct
        vwap_high_pct -= afternoon_tighten_pct
        va_moderate_pct -= afternoon_tighten_pct
        va_high_pct -= afternoon_tighten_pct

    # VWAP deviation (directional)
    if direction == "bullish":
        vwap_dev = max(0.0, (price - vwap) / vwap * 100)
    else:
        vwap_dev = max(0.0, (vwap - price) / vwap * 100)

    # VA boundary distance (directional)
    va_dist = 0.0
    if direction == "bullish" and vp.vah > 0 and price > vp.vah:
        va_dist = (price - vp.vah) / vp.vah * 100
    elif direction == "bearish" and vp.val > 0 and price < vp.val:
        va_dist = (vp.val - price) / vp.val * 100

    # Determine level
    reasons: list[str] = []
    level = "none"

    if vwap_dev >= vwap_high_pct or va_dist >= va_high_pct:
        level = "high"
    elif vwap_dev >= vwap_moderate_pct or va_dist >= va_moderate_pct:
        level = "moderate"

    if level != "none":
        if vwap_dev >= vwap_moderate_pct:
            reasons.append(f"VWAP 偏离 {vwap_dev:.1f}%")
        if va_dist >= va_moderate_pct:
            reasons.append(f"VA 边界距离 {va_dist:.1f}%")
        if is_afternoon:
            reasons.append("午后信号可靠性较低")

    return ChaseRiskResult(
        level=level,
        reasons=reasons,
        vwap_dev_pct=vwap_dev,
        va_dist_pct=va_dist,
        pullback_target=vwap,
    )
