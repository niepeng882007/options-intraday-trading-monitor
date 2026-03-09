# Backtesting Framework

## Overview

回测框架复用实时监控管道的核心组件 (IndicatorEngine / RuleMatcher / StateManager)，逐 bar 回放历史数据，模拟策略触发与退出，跟踪交易盈亏。

**设计原则：** 与实时系统使用完全相同的指标计算和规则评估逻辑，确保回测结果能反映真实信号行为。

## Quick Start

```bash
# 回测单个策略 (最近 5 个交易日, Futu 数据源)
python -m src.backtest -s bb-squeeze-ambush -y SPY -d 5

# 使用 yfinance 数据源 (无需 FutuOpenD)
python -m src.backtest -s bb-squeeze-ambush -y SPY -d 5 --data-source yahoo

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
| `--data-source` | - | 数据源: `futu` / `yahoo` | settings.yaml 或 `futu` |
| `--futu-host` | - | FutuOpenD 主机地址 | settings.yaml 或 `127.0.0.1` |
| `--futu-port` | - | FutuOpenD 端口 | settings.yaml 或 `11111` |

**互斥参数：** `--strategy` 和 `--all` 必须选一个。`--start-date`/`--end-date` 必须同时使用，会覆盖 `--days`。

## Architecture

```
┌─────────────┐     ┌──────────────────────────────────────────────────────┐
│  DataLoader │     │              BacktestEngine                          │
│ (Futu/Yahoo │────>│                                                      │
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
  data_loader.py       # Futu/yfinance 历史数据下载 + CSV 缓存
  engine.py            # BacktestEngine 核心回放引擎
  report.py            # 终端报告输出 (table/csv/json)
  __main__.py          # CLI 入口 (argparse)
tests/
  test_backtest.py     # 回测引擎测试 (27 用例)
  test_data_loader.py  # DataLoader 测试 (12 用例)
```

## Core Engine: Bar-by-Bar Replay

回测引擎逐 bar 回放，而非一次性批量计算指标。这确保了与实时系统完全一致的行为。

### 每个 bar 的处理流程 (`_process_bar`)

```
1. 设置 RuleMatcher._simulated_time → 用 bar 时间替代 datetime.now()
2. Phase 1 — 棒内模拟 (Intra-bar simulation):
   a. 构建 stub bar (O=H=L=C=Open) → indicator_engine.update_bars()
   b. 依次以 Open/High/Low 价格调用 update_live_price() 获取偏指标
   c. 对每个模拟价格调用 _evaluate_at_price() 检查出/入场
3. Phase 2 — 完整 bar:
   a. 构建最终 bar (完整 OHLCV) → indicator_engine.update_bars()
   b. 检查是否仍在 warmup 期 (前 260 bars) → 跳过
   c. indicator_engine.calculate_all() → 获取 1m/5m/15m 三时间框架指标
   d. 以 Close 价格调用 _evaluate_at_price()
```

### 棒内模拟 (Intra-bar Simulation)

实时交易中，每 10 秒 quote 推送都会触发 `update_live_price()`，在 bar 未完成时产生偏指标 (body ≈ 0, close ≈ open)。纯 bar-close 回测会遗漏这些信号。

棒内模拟通过在每个 bar 内插入 3 个价格探针 (Open, High, Low)，模拟实时推送行为：

```python
# 构建 stub bar: O=H=L=C=Open (模拟 bar 刚开始时的状态)
stub = DataFrame({"Open": [O], "High": [O], "Low": [O], "Close": [O], "Volume": [V]})
indicator_engine.update_bars(symbol, stub)

# 依次以 O/H/L 价格模拟 update_live_price()
for sim_price in [open_p, high_p, low_p]:
    indicators = indicator_engine.update_live_price(symbol, sim_price, ts)
    _evaluate_at_price(symbol, sim_price, bar_dt, indicators)
```

### 价格评估流程 (`_evaluate_at_price`)

```
1. 遍历 HOLDING 策略 → 更新 highest/lowest_price → evaluate_exit()
   → 止盈/止损/追踪止盈/指标止盈/时间退出
2. 午间禁交易窗口检查 → 跳过入场
3. 每日亏损熔断检查 → 跳过入场
4. 遍历 WATCHING 策略:
   → 交易窗口检查
   → 市场环境过滤器 (SPY 跌幅, ADX)
   → 冷却期检查
   → evaluate_entry() → 质量评估 → 自动确认
5. TradeTracker 记录开仓/平仓
```

### 日间重置 (`_reset_day`)

每个交易日开始时：

| 组件 | 是否重置 | 原因 |
|------|----------|------|
| StateManager | 重置 | 每天从 WATCHING 开始 |
| confirmation_counts | 清除 | N-bar 确认是日内概念 |
| bbw_history | 清除 | BBW 百分位是日内计算 |
| _daily_pnl | 归零 | 每日亏损熔断按日计算 |
| _cooldowns | 清除 | 冷却期按日重置 |
| _prev_values | **不重置** | crosses_above 等需要跨日连续性 |
| IndicatorEngine bars | **不重置** | EMA/RSI 需要历史数据延续 |

### Warmup 处理

前 260 个 1m bars (约 4.3 小时交易时段，跨日累积) 仅用于指标预热。在此期间不评估任何入场/出场信号。这确保 EMA-50 在 5m 级别有足够数据 (需要 51 × 5 = 255 bars)。

### 日终强平

每个交易日结束时，所有未平仓位以当日最后一个 bar 的收盘价强制平仓，exit_reason 标记为 "日终强平"。这模拟了 0DTE 期权到期无价值的风险管理。

## Risk Management

回测引擎集成两项风控机制，从 `config/settings.yaml` 的 `risk_management` 配置读取：

```yaml
risk_management:
  midday_no_trade:
    enabled: true
    start: "11:00"
    end: "13:00"
  max_daily_loss_pct: -1.5
```

### 午间禁交易窗口

当 `midday_no_trade.enabled: true` 时，在 `start` ~ `end` 时间段内跳过所有入场评估 (不影响已有持仓的出场)。

### 每日亏损熔断

当单日累计 `direction_pnl_pct` ≤ `max_daily_loss_pct` 时，停止当日所有新入场。每笔平仓后累加到 `_daily_pnl`，日间重置时归零。

## Cooldown Management

每次成功入场后，设置 `cooldown_seconds` 冷却期 (来自策略 YAML `notification.cooldown_seconds`)，防止同一 (策略, 标的) 短时间内重复入场。

```
_cooldowns["{strategy_id}:{symbol}"] = bar_dt + timedelta(seconds=cooldown_seconds)
```

- 冷却期按日重置 (不跨日持续)
- 仅阻止入场，不影响出场评估

## Market Context Filters

入场前检查市场环境条件 (来自策略 YAML `market_context_filters`)：

| 过滤器 | 说明 | 数据源 |
|--------|------|--------|
| `max_spy_day_drop_pct` | SPY 日内跌幅超限则不入场 | SPY 5m 指标 `day_change_pct` |
| `max_adx` | ADX 超限 (趋势过强) 则不入场，左侧/均值回归策略用 | 标的 5m 指标 `adx` |
| `min_adx` | ADX 不足 (趋势不够) 则不入场，右侧突破策略用 | 标的 5m 指标 `adx` |

> **ADX 重叠区间设计：** 左侧策略 `max_adx: 30`，右侧策略 `min_adx: 20`，形成 20-30 的重叠区间。避免 ADX 在边界附近波动时两边都被挡住的"死区"问题。

当任一策略使用 `max_spy_day_drop_pct` 时，DataLoader 自动将 SPY 加入数据下载列表。

## PUT Direction Exit Logic

出场阈值对 PUT 方向做反转处理：

| 出场类型 | CALL | PUT |
|----------|------|-----|
| `take_profit_pct` | `stock_pnl > +threshold` | `stock_pnl < -threshold` (股价跌 = 盈利) |
| `stop_loss_pct` | `stock_pnl < -threshold` | `stock_pnl > +threshold` (股价涨 = 亏损) |
| `trailing_stop` | 追踪 `highest_price`，从峰值回撤触发 | 追踪 `lowest_price`，从谷底反弹触发 |
| `indicator_target` | 价格 ≥ 指标目标值 | 价格 ≤ 指标目标值 |

`PositionInfo` 同时追踪 `highest_price` 和 `lowest_price`，在每次 `_evaluate_at_price` 中更新。

## P&L Calculation

回测追踪**股票价格变动百分比**，与实时系统的出场阈值一致：

| 方向 | 公式 | 含义 |
|------|------|------|
| Call | `direction_pnl = stock_pnl` | 股票涨 = Call 盈利 |
| Put | `direction_pnl = -stock_pnl` | 股票跌 = Put 盈利 |

实际期权 P&L 约为股票变动的 12-20x (取决于 delta/gamma)，但回测仅追踪股票层面。

## Data Loading

DataLoader 支持两种数据源，通过 `--data-source` 或 `settings.yaml` 的 `data_source` 配置。

### Futu 数据源 (默认)

通过 FutuOpenD 的 `request_history_kline` API 下载 1m K 线，无时间范围限制。

**特性：**
- 分页拉取：每页 `max_count=1000`，通过 `page_req_key` 自动翻页合并
- 连接验证：启动时调用 `get_global_state()` 确认 FutuOpenD 存活
- 限速保护：每个标的下载后 sleep 0.5s (Futu 60 次/30s 限制)
- 数据规范化：`normalize_futu_kline()` 转换列名 + 时区 → `America/New_York`
- 交易时间过滤：`between_time("09:30", "15:59")`
- 天数截断：`days` 模式下仅保留最后 N 个交易日

**连接失败处理：** 抛出 `ConnectionError` 并提示使用 `--data-source yahoo` 回退。

### yfinance 回退数据源

| 回测天数 | 数据间隔 | yfinance 参数 | 说明 |
|----------|----------|--------------|------|
| ≤ 5 天 | 1 分钟 | `period="7d", interval="1m"` | 最精确 |
| > 5 天 | 5 分钟 | `period="{days+5}d", interval="5m"` | 自动降级 (上限 59 天) |
| 日期范围 ≤ 7 天 | 1 分钟 | `start/end, interval="1m"` | 指定范围 |
| 日期范围 > 7 天 | 5 分钟 | `start/end, interval="5m"` | 自动降级 |

### CSV 缓存

首次下载后，数据缓存到 `data/backtest_cache/` 目录 (CSV 格式)：
- 按天数: `SPY_5d_20260306_futu.csv` / `SPY_5d_20260306_yahoo.csv`
- 按日期范围: `SPY_2025-03-15_2025-03-20_futu.csv`

缓存键包含数据源后缀，防止 Futu 与 Yahoo 缓存冲突。删除 `data/backtest_cache/` 可强制重新下载。

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

多策略/多标的时追加 By Strategy / By Symbol 分组汇总。

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

回测框架对现有代码的改动极小：

**`src/strategy/matcher.py`:**
```python
class RuleMatcher:
    _simulated_time: datetime | None = None  # 类属性

# _quality_time_of_day() 中:
    now = RuleMatcher._simulated_time or datetime.now(et)

# evaluate_exit() 增加 direction/lowest_price 参数:
    def evaluate_exit(self, strategy, symbol, current_price, entry_price,
                      minutes_to_close, highest_price=None, lowest_price=None,
                      direction="call", indicators_by_tf=None):
```

**`src/strategy/state.py`:**
```python
# PositionInfo 增加 lowest_price 字段
# update_highest_price() 同时更新 lowest_price
```

回测结束后 `_simulated_time` 被重置为 `None`，不影响实时运行。

## Testing

```bash
# 运行回测测试
pytest tests/test_backtest.py tests/test_data_loader.py -v

# 运行全部测试确认无回归
pytest tests/ -v
```

### 测试覆盖

**test_backtest.py (27 用例):**

| 测试类 | 用例数 | 覆盖范围 |
|--------|--------|----------|
| TestTradeTracker | 6 | Call/Put P&L、胜率、利润因子、最大回撤、强平、分组统计 |
| TestBacktestEngine | 7 | Warmup 跳过、止损触发、交易窗口、日间重置、方向 P&L、空数据、多策略 |
| TestIntraBarEntry | 1 | 棒内模拟产生交易 |
| TestPutExitDirection | 3 | PUT 止盈/止损方向反转、CALL 基准对比 |
| TestPutTrailingStop | 2 | PUT 追踪止盈触发/未激活 |
| TestCooldownPreventsReentry | 1 | 冷却期阻止重入 |
| TestMinMatch | 2 | MIN_MATCH 规则 3/4 通过 / 2/4 不通过 |
| TestIndicatorExit | 3 | 指标止盈 (Call BB middle / Put BB middle / 未触发) |
| TestBBPiercing | 2 | BB Piercing 策略 YAML 加载 (call/put) |

**test_data_loader.py (12 用例):**

| 测试类 | 用例数 | 覆盖范围 |
|--------|--------|----------|
| TestPageReqKeyPagination | 4 | 多页合并、单页、空结果、API 错误 |
| TestCaching | 3 | 缓存命中、缓存未命中下载、缓存键含数据源 |
| TestTradingHoursFilter | 1 | 盘前/盘后数据过滤 |
| TestDaysModeTruncation | 1 | days=5 截断为最后 5 个交易日 |
| TestConnectionError | 2 | Futu 连接失败、Yahoo 回退无连接 |
| TestContextManager | 1 | 上下文管理器关闭资源 |

## Performance

| 场景 | 数据规模 | 耗时 |
|------|---------|------|
| 5 天 × 5 标的 × 10 策略 | ~9,740 bars | ~2.5 分钟 |
| 5 天 × 1 标的 × 1 策略 | ~1,950 bars | ~30 秒 |

主要耗时在逐 bar 指标计算 (RSI/MACD/EMA/BB 等)。首次运行含数据下载时间 (~5-10 秒)，后续使用缓存。

## Limitations

1. **仅追踪股票价格层面的 P&L** — 不模拟期权 Greeks (delta/gamma/theta/vega)，实际期权 P&L 取决于行权价和到期日
2. **无滑点/手续费模型** — 入场/出场使用 bar 收盘价，未模拟 bid-ask spread 和佣金
3. **yfinance 1m 数据限制** — yfinance 仅提供最近 7 个交易日的 1m 数据，更长回测自动降级到 5m (Futu 无此限制)
4. **单向持仓** — 每个 (策略, 标的) 同一时间只能有一个持仓
5. **无资金管理** — 不追踪账户资金和仓位大小，P&L 为百分比而非绝对金额
