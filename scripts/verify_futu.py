#!/usr/bin/env python3
"""Futu API 连接验证脚本 — 逐项检查 FutuOpenD 连接和各 API 能力。"""

import argparse
import threading
import time
from datetime import datetime

from futu import (
    KLType,
    OpenQuoteContext,
    RET_OK,
    StockQuoteHandlerBase,
    SubType,
)


def fmt_result(index: int, name: str, status: str, detail: str) -> str:
    label = f"[{index}/5] {name}"
    return f"{label:<30} {status} ({detail})"


def check_connection(host: str, port: int) -> tuple[OpenQuoteContext | None, bool, str]:
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        return ctx, True, f"FutuOpenD {host}:{port}"
    except Exception as e:
        return None, False, str(e)


def check_stock_quote(ctx: OpenQuoteContext, symbol: str) -> tuple[bool, str]:
    ret, msg = ctx.subscribe([symbol], [SubType.QUOTE])
    if ret != RET_OK:
        return False, f"subscribe failed: {msg}"
    ret, data = ctx.get_stock_quote([symbol])
    if ret != RET_OK:
        return False, str(data)
    row = data.iloc[0]
    last = row.get("last_price", "N/A")
    bid = row.get("bid_price", "N/A")
    ask = row.get("ask_price", "N/A")
    vol = row.get("volume", "N/A")
    vol_str = f"{int(vol):,}" if isinstance(vol, (int, float)) else vol
    return True, f"{symbol} ${last}, bid=${bid}, ask=${ask}, vol={vol_str}"


def check_kline(ctx: OpenQuoteContext, symbol: str) -> tuple[bool, str]:
    ret, data, _ = ctx.request_history_kline(
        symbol, ktype=KLType.K_1M, max_count=500
    )
    if ret != RET_OK:
        return False, str(data)
    n = len(data)
    if n == 0:
        return False, "no bars returned"
    last = data.iloc[-1]
    ts = last.get("time_key", "")
    o, h, l, c = last.get("open", ""), last.get("high", ""), last.get("low", ""), last.get("close", "")
    return True, f"{n} bars, last: {ts} O={o} H={h} L={l} C={c}"


def check_option_chain(ctx: OpenQuoteContext, symbol: str) -> tuple[bool, str]:
    ret, data = ctx.get_option_chain(symbol)
    if ret != RET_OK:
        return False, str(data)
    if data is None or data.empty:
        return False, "no option chain data"
    expirations = sorted(data["strike_time"].unique()) if "strike_time" in data.columns else []
    n_exp = len(expirations)
    total = len(data)
    first_exp = expirations[0] if expirations else "N/A"
    first_count = len(data[data["strike_time"] == first_exp]) if expirations else 0
    return True, f"{n_exp} expirations, {first_count} contracts for {first_exp}"


def check_realtime_push(ctx: OpenQuoteContext, symbol: str) -> tuple[bool | None, str]:
    """返回 (True=PASS, False=FAIL, None=SKIP), detail"""
    push_data: list[dict] = []
    done = threading.Event()

    class Handler(StockQuoteHandlerBase):
        def on_recv_rsp(self, rsp_pb):
            ret, data = super().on_recv_rsp(rsp_pb)
            if ret == RET_OK and data is not None and not data.empty:
                row = data.iloc[0]
                push_data.append({
                    "price": row.get("last_price", "N/A"),
                    "time": row.get("data_time", ""),
                })
                done.set()
            return ret, data

    ctx.set_handler(Handler())
    ret, msg = ctx.subscribe([symbol], [SubType.QUOTE])
    if ret != RET_OK:
        return False, f"subscribe failed: {msg}"

    done.wait(timeout=10)

    count = len(push_data)
    if count == 0:
        return None, "no pushes in 10s (market may be closed)"
    last = push_data[-1]
    return True, f"received {count} pushes in 10s, last: ${last['price']} @ {last['time']}"


def main():
    parser = argparse.ArgumentParser(description="Verify Futu API connectivity")
    parser.add_argument("--host", default="127.0.0.1", help="FutuOpenD host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=11111, help="FutuOpenD port (default: 11111)")
    parser.add_argument("--symbol", default="AAPL", help="Stock symbol to test (default: AAPL)")
    args = parser.parse_args()

    symbol = f"US.{args.symbol}" if not args.symbol.startswith("US.") else args.symbol
    passed, failed, skipped = 0, 0, 0

    print(f"\n=== Futu API Verification ===\n")

    # 1. Connection
    ctx, ok, detail = check_connection(args.host, args.port)
    status = "PASS" if ok else "FAIL"
    print(fmt_result(1, "Connection", status, detail))
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"\nResult: {passed}/5 PASSED, {failed} FAILED (cannot continue without connection)")
        return

    # 2-4: quote, kline, option chain
    checks = [
        (2, "Stock Quote", lambda: check_stock_quote(ctx, symbol)),
        (3, "History K-line", lambda: check_kline(ctx, symbol)),
        (4, "Option Chain", lambda: check_option_chain(ctx, symbol)),
    ]
    for idx, name, fn in checks:
        try:
            ok, detail = fn()
            status = "PASS" if ok else "FAIL"
        except Exception as e:
            ok, status, detail = False, "FAIL", str(e)
        print(fmt_result(idx, name, status, detail))
        if ok:
            passed += 1
        else:
            failed += 1

    # 5. Real-time push
    try:
        result, detail = check_realtime_push(ctx, symbol)
        if result is None:
            status = "SKIP"
            skipped += 1
        elif result:
            status = "PASS"
            passed += 1
        else:
            status = "FAIL"
            failed += 1
    except Exception as e:
        status, detail = "FAIL", str(e)
        failed += 1
    print(fmt_result(5, "Real-time Push", status, detail))

    ctx.close()

    # Summary
    parts = [f"{passed}/5 PASSED"]
    if failed:
        parts.append(f"{failed} FAILED")
    if skipped:
        parts.append(f"{skipped} SKIPPED")
    print(f"\nResult: {', '.join(parts)}\n")


if __name__ == "__main__":
    main()
