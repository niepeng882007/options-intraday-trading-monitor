# Backtesting Framework

## Overview

回测框架复用实时监控管道的核心组件 (IndicatorEngine / RuleMatcher / StateManager)，逐 bar 回放历史数据，模拟策略触发与退出，跟踪交易盈亏。

**设计原则：** 与实时系统使用完全相同的指标计算和规则评估逻辑，确保回测结果能反映真实信号行为。

## Quick Start

```bash
# 回测单个策略 (最近 5 个交易日)
python -m src.backtest -s bb-squeeze-ambush -y SPY -d 5

# 回测全部策略，显示交易明细
python -m src.backtest --all -d 5 -v

# 指定日期范围
python -m src.backtest -s vwap-low-vol-ambush --start-date 2025-03-15 --end-date 2025-03-20

# 输出为 CSV (可导入 Excel)
python -m src.backtest -s bb-squeeze-ambush -y SPY -d 5 -o csv

# 输出为 JSON (程序化分析)
python -m src.backtest --all -d 5 -o json
```

## CLI Reference

```
python -m src.backtest [OPTIONS]
```

| 参数 | 缩写 | 说明 | 默认值 |
|------|------|------|--------|
| `--strategy` | `-s` | 策略 ID | - |
| `--all` | - | 回测全部活跃策略 | - |
| `--symbol` | `-y` | 逗号分隔标的 (如 `SPY,QQQ`) | 策略 watchlist |
| `--days` | `-d` | 回测交易天数 | 5 |
| `--start-date` | - | 起始日期 (YYYY-MM-DD) | - |
| `--end-date` | - | 结束日期 (YYYY-MM-DD) | - |
| `--verbose` | `-v` | 打印每笔交易明细 | false |
| `--output` | `-o` | 输出格式: `table` / `csv` / `json` | table |
| `--strategies-dir` | - | 策略 YAML 目录 | `config/strategies` |

**互斥参数：** `--strategy` 和 `--all` 必须选一个。`--start-date`/`--end-date` 必须同时使用，会覆盖 `--days`。

## Architecture

```
┌─────────────┐     ┌──────────────────────────────────────────────────────┐
│  DataLoader │     │              BacktestEngine                          │
│  (yfinance  │────>│                                                      │
│  + CSV缓存) │     │  ┌─────────────────┐   ┌────────────────┐           │
└─────────────┘     │  │ IndicatorEngine  │   │  RuleMatcher   │           │
                    │  │ (复用实时组件)      │   │ (复用实时组件)   │           │
                    │  └────────┬────────┘   └───────┬────────┘           │
                    │           │                     │                     │
                    │  ┌────────▼─────────────────────▼────────┐           │
                    │  │         StateManager (状态机)            │           │
                    │  │  WATCHING → ENTRY_TRIGGERED → HOLDING  │           │
                    │  │  → EXIT_TRIGGERED → WATCHING           │           │
                    │  └────────────────────┬──────────────────┘           │
                    │                       │                              │
                    │  ┌────────────────────▼──────────────────┐           │
                    │  │         TradeTracker (交易记录)          │           │
                    │  │  open_trade / close_trade / results    │           │
                    │  └───────────────────────────────────────┘           │
                    └──────────────────────────────────────────────────────┘
                                        │
                                        ▼
                    ┌──────────────────────────────────────────┐
                    │              Report                       │
                    │  format_report / format_csv / format_json │
                    └──────────────────────────────────────────┘
```

## File Structure

```
src/backtest/
  __init__.py          # 空文件
  trade_tracker.py     # Trade/BacktestResult 数据类 + TradeTracker
  data_loader.py       # yfinance 历史数据下载 + CSV 缓存
  engine.py            # BacktestEngine 核心回放引擎
  report.py            # 终端报告输出 (table/csv/json)
  __main__.py          # CLI 入口 (argparse)
tests/
  test_backtest.py     # 13 个测试用例
```

## Core Engine: Bar-by-Bar Replay

回测引擎逐 bar 回放，而非一次性批量计算指标。这确保了与实时系统完全一致的行为。

### 每个 bar 的处理流程 (`_process_bar`)

```
1. 构建单 bar DataFrame → indicator_engine.update_bars()
2. 检查是否仍在 warmup 期 (前 260 bars) → 跳过
3. indicator_engine.calculate_all() → 获取 1m/5m/15m 三时间框架指标
4. 设置 RuleMatcher._simulated_time → 用 bar 时间替代 datetime.now()
5. 遍历 HOLDING 策略 → evaluate_exit() → 止盈/止损/追踪止盈/时间退出
6. 遍历 WATCHING 策略 → trading window → market context → evaluate_entry()
7. 入场质量评估 → 自动确认 (回测无需人工确认)
8. TradeTracker 记录开仓/平仓
```

### 日间重置 (`_reset_day`)

每个交易日开始时：

| 组件 | 是否重置 | 原因 |
|------|----------|------|
| StateManager | 重置 | 每天从 WATCHING 开始 |
| confirmation_counts | 清除 | N-bar 确认是日内概念 |
| bbw_history | 清除 | BBW 百分位是日内计算 |
| _prev_values | **不重置** | crosses_above 等需要跨日连续性 |
| IndicatorEngine bars | **不重置** | EMA/RSI 需要历史数据延续 |

### Warmup 处理

前 260 个 1m bars (约 4.3 小时交易时段，跨日累积) 仅用于指标预热。在此期间不评估任何入场/出场信号。这确保 EMA-50 在 5m 级别有足够数据 (需要 51 × 5 = 255 bars)。

### 日终强平

每个交易日结束时，所有未平仓位以当日最后一个 bar 的收盘价强制平仓，exit_reason 标记为 "日终强平"。这模拟了 0DTE 期权到期无价值的风险管理。

## P&L Calculation

回测追踪**股票价格变动百分比**，与实时系统的出场阈值一致：

| 方向 | 公式 | 含义 |
|------|------|------|
| Call | `direction_pnl = stock_pnl` | 股票涨 = Call 盈利 |
| Put | `direction_pnl = -stock_pnl` | 股票跌 = Put 盈利 |

实际期权 P&L 约为股票变动的 12-20x (取决于 delta/gamma)，但回测仅追踪股票层面。

## Data Loading

### yfinance 限制与策略

| 回测天数 | 数据间隔 | yfinance 参数 | 说明 |
|----------|----------|--------------|------|
| <= 5 天 | 1 分钟 | `period="7d", interval="1m"` | 最精确 |
| > 5 天 | 5 分钟 | `period="{days+5}d", interval="5m"` | 自动降级 |
| 日期范围 <= 7 天 | 1 分钟 | `start/end, interval="1m"` | 指定范围 |
| 日期范围 > 7 天 | 5 分钟 | `start/end, interval="5m"` | 自动降级 |

### CSV 缓存

首次下载后，数据缓存到 `data/backtest_cache/` 目录 (CSV 格式)：
- 按天数: `SPY_5d_20260306.csv`
- 按日期范围: `SPY_2025-03-15_2025-03-20.csv`

再次运行相同参数时直接读取缓存，跳过网络请求。删除 `data/backtest_cache/` 可强制重新下载。

### SPY 自动加载

如果任一策略配置了 `market_context_filters.max_spy_day_drop_pct`，DataLoader 会自动将 SPY 加入下载列表 (即使用户未指定)。

## Report Output

### Table (默认)

```
======================================================
  Backtest: vwap-low-vol-ambush (SPY)
  Period: 2026-02-27 -> 2026-03-05 (5 days)
======================================================

  Total Trades           6
  Win Rate               83.3%  (5W / 1L)
  Profit Factor          5.42
  Total Return           +1.73%
  Max Drawdown           -0.20%
  Avg Holding Time       35 min
  Avg Win                +0.35%
  Avg Loss               -0.02%
  Best Trade             +0.54%
  Worst Trade            -0.02%
  Trades/Day             2.0
```

使用 `-v` 追加交易明细表：

```
  Trade Log
  #   Date             Entry      Exit     P&L%   Reason         Quality
  ----------------------------------------------------------------------
  1   02/27 14:01  $  684.25 $  685.30   +0.15%  收盘前15分钟退出   A(100)
  2   03/04 10:05  $  681.75 $  685.24   +0.51%  止盈 (+0.5%)     A(100)
```

### CSV

```bash
python -m src.backtest -s bb-squeeze-ambush -y SPY -d 5 -o csv > results.csv
```

输出 14 列 CSV：trade_num, strategy_id, symbol, direction, entry_time, exit_time, entry_price, exit_price, stock_pnl_pct, direction_pnl_pct, exit_reason, holding_min, quality_score, quality_grade。

### JSON

```bash
python -m src.backtest --all -d 5 -o json > results.json
```

包含 `summary` (汇总指标)、`by_strategy` (按策略分组)、`by_symbol` (按标的分组)、`trades` (完整交易列表)。

## Metrics Reference

| 指标 | 说明 |
|------|------|
| Win Rate | 盈利交易数 / 总交易数 |
| Profit Factor | 总盈利 / 总亏损绝对值 (>1 为盈利系统) |
| Total Return | 所有交易 direction_pnl_pct 之和 |
| Max Drawdown | 累计收益曲线从峰值到谷底的最大回撤 |
| Avg Holding Time | 平均持仓时间 (分钟) |
| Avg Win / Loss | 盈利/亏损交易的平均 P&L% |
| Best / Worst Trade | 单笔最大盈利/亏损 |
| Trades/Day | 平均每交易日交易次数 |

## Existing Component Integration

回测框架对现有代码的改动极小 (仅 2 行)：

**`src/strategy/matcher.py`:**
```python
class RuleMatcher:
    _simulated_time: datetime | None = None  # 新增类属性

# _quality_time_of_day() 中:
    now = RuleMatcher._simulated_time or datetime.now(et)  # 改动 1 行
```

回测结束后 `_simulated_time` 被重置为 `None`，不影响实时运行。

## Testing

```bash
# 运行回测测试
pytest tests/test_backtest.py -v

# 运行全部测试确认无回归
pytest tests/ -v
```

### 测试覆盖

| 测试类 | 用例数 | 覆盖范围 |
|--------|--------|----------|
| TestTradeTracker | 6 | Call/Put P&L、胜率、利润因子、最大回撤、强平、分组统计 |
| TestBacktestEngine | 7 | Warmup 跳过、止损触发、交易窗口、日间重置、方向 P&L、空数据、多策略 |

## Performance

| 场景 | 数据规模 | 耗时 |
|------|---------|------|
| 5 天 × 5 标的 × 10 策略 | ~9,740 bars | ~2.5 分钟 |
| 5 天 × 1 标的 × 1 策略 | ~1,950 bars | ~30 秒 |

主要耗时在逐 bar 指标计算 (RSI/MACD/EMA/BB 等)。首次运行含 yfinance 下载时间 (~5-10 秒)，后续使用缓存。

## Limitations

1. **仅追踪股票价格层面的 P&L** — 不模拟期权 Greeks (delta/gamma/theta/vega)，实际期权 P&L 取决于行权价和到期日
2. **无滑点/手续费模型** — 入场/出场使用 bar 收盘价，未模拟 bid-ask spread 和佣金
3. **1m 数据限制** — yfinance 仅提供最近 7 个交易日的 1m 数据，更长回测自动降级到 5m
4. **单向持仓** — 每个 (策略, 标的) 同一时间只能有一个持仓
5. **无资金管理** — 不追踪账户资金和仓位大小，P&L 为百分比而非绝对金额
