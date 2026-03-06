# 策略打磨与胜率提升指南

---

## 一、当前系统诊断

### 1.1 已有能力

| 模块 | 状态 | 说明 |
|------|------|------|
| 指标引擎 | ✅ 完备 | RSI、MACD、EMA(9/21/50/200)、VWAP、ATR、布林带、K线形态、量比 |
| 策略匹配 | ✅ 完备 | 支持 AND/OR 组合、crosses_above/below、turns_positive/negative、within_pct_of |
| 信号质量评级 | ✅ 完备 | A/B/C/D 四级评分（VWAP距离、量比、K线实体、BB宽度、EMA200位置、RSI极值、VWAP乖离） |
| 状态机 | ✅ 完备 | WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING |
| 信号记录 | ⚠️ 不完整 | `signals` 表只记录触发事件，不记录入场价/出场价/盈亏 |
| 绩效统计 | ❌ 缺失 | 无胜率、盈亏比、最大回撤等统计 |
| 回测框架 | ❌ 缺失 | 无法用历史数据验证参数调整效果 |
| 宏观过滤 | ⚠️ 薄弱 | 仅 `day_change_pct > -0.3` 一个条件 |

### 1.2 核心短板

系统缺少**"信号 → 结果 → 统计 → 优化"闭环**。当前的信号质量评级（A/B/C/D）从未与实际交易结果关联验证，无法证明 A 级信号确实优于 C 级信号。没有数据支撑的参数调整等同于盲猜。

---

## 二、建立信号追踪闭环（最高优先级）

### 2.1 数据库 Schema 扩展

在 `src/store/sqlite_store.py` 的 `signals` 表增加以下字段：

```sql
ALTER TABLE signals ADD COLUMN entry_price REAL;
ALTER TABLE signals ADD COLUMN exit_price REAL;
ALTER TABLE signals ADD COLUMN pnl_pct REAL;
ALTER TABLE signals ADD COLUMN outcome TEXT;          -- 'win' / 'loss' / 'breakeven' / 'skipped'
ALTER TABLE signals ADD COLUMN quality_grade TEXT;     -- 'A' / 'B' / 'C' / 'D'
ALTER TABLE signals ADD COLUMN quality_score INTEGER;
ALTER TABLE signals ADD COLUMN market_context TEXT;    -- JSON: SPY涨跌幅、VIX、大盘趋势
ALTER TABLE signals ADD COLUMN holding_minutes REAL;
ALTER TABLE signals ADD COLUMN exit_reason TEXT;       -- '止盈' / '止损' / '时间退出'
```

### 2.2 关键统计查询

#### 各策略胜率与盈亏比

```sql
SELECT
    strategy_id,
    COUNT(*) AS total_trades,
    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate_pct,
    ROUND(AVG(CASE WHEN outcome = 'win' THEN pnl_pct END), 2) AS avg_win_pct,
    ROUND(AVG(CASE WHEN outcome = 'loss' THEN pnl_pct END), 2) AS avg_loss_pct,
    ROUND(
        ABS(AVG(CASE WHEN outcome = 'win' THEN pnl_pct END))
        / ABS(AVG(CASE WHEN outcome = 'loss' THEN pnl_pct END)),
    2) AS profit_factor
FROM signals
WHERE signal_type = 'entry' AND outcome IS NOT NULL AND outcome != 'skipped'
GROUP BY strategy_id
ORDER BY win_rate_pct DESC;
```

#### 信号质量等级 vs 实际结果

```sql
SELECT
    quality_grade,
    COUNT(*) AS total,
    ROUND(100.0 * SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate_pct,
    ROUND(AVG(pnl_pct), 2) AS avg_pnl_pct
FROM signals
WHERE outcome IS NOT NULL AND outcome != 'skipped'
GROUP BY quality_grade
ORDER BY quality_grade;
```

#### 按时段分析（哪个时间段信号最有效）

```sql
SELECT
    CAST(strftime('%H', datetime(timestamp, 'unixepoch', '-5 hours')) AS INTEGER) AS hour_et,
    COUNT(*) AS total,
    ROUND(100.0 * SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate_pct,
    ROUND(AVG(pnl_pct), 2) AS avg_pnl_pct
FROM signals
WHERE outcome IS NOT NULL AND outcome != 'skipped'
GROUP BY hour_et
ORDER BY hour_et;
```

#### 按标的分析

```sql
SELECT
    symbol,
    strategy_id,
    COUNT(*) AS total,
    ROUND(100.0 * SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate_pct,
    ROUND(AVG(pnl_pct), 2) AS avg_pnl_pct
FROM signals
WHERE outcome IS NOT NULL AND outcome != 'skipped'
GROUP BY symbol, strategy_id
ORDER BY win_rate_pct DESC;
```

### 2.3 数据收集流程

```
信号触发时 → 记录: strategy_id, symbol, quality_grade, quality_score, market_context
     ↓
用户 /confirm → 记录: entry_price, entry_timestamp
     ↓
出场信号触发 → 记录: exit_price, pnl_pct, outcome, exit_reason, holding_minutes
     ↓
用户 /skip → 记录: outcome = 'skipped'
```

---

## 三、回测框架设计

### 3.1 核心思路

利用 `indicator_history` 表中已积累的指标快照，模拟 `RuleMatcher` 逻辑进行离线回测。

### 3.2 回测器伪代码

```python
class SimpleBacktester:
    def __init__(self, matcher: RuleMatcher):
        self.matcher = matcher

    def run(
        self,
        strategy: StrategyConfig,
        symbol: str,
        indicator_snapshots: list[dict],  # 从 indicator_history 表读取
        price_data: pd.DataFrame,         # 从 bars_1m 历史读取
    ) -> BacktestResult:
        trades: list[Trade] = []
        in_position = False
        entry_price = 0.0

        for snapshot in indicator_snapshots:
            indicators_by_tf = self._rebuild_indicators(snapshot)

            if not in_position:
                signal = self.matcher.evaluate_entry(strategy, symbol, indicators_by_tf)
                if signal:
                    entry_price = snapshot["close"]
                    in_position = True
            else:
                exit_signal = self.matcher.evaluate_exit(
                    strategy, symbol,
                    current_price=snapshot["close"],
                    entry_price=entry_price,
                    minutes_to_close=self._minutes_to_close(snapshot["timestamp"]),
                )
                if exit_signal:
                    pnl_pct = (snapshot["close"] - entry_price) / entry_price
                    trades.append(Trade(entry_price, snapshot["close"], pnl_pct))
                    in_position = False

        return self._compute_stats(trades)
```

### 3.3 回测指标

| 指标 | 计算方式 | 合格线 |
|------|---------|--------|
| 胜率 | wins / total_trades | ≥ 45% |
| 平均盈亏比 | avg_win / abs(avg_loss) | ≥ 1.5 |
| 期望值 | win_rate × avg_win + (1 - win_rate) × avg_loss | > 0 |
| 最大连亏 | 连续亏损的最大次数 | ≤ 5 |
| 利润因子 | total_profit / total_loss | > 1.2 |
| 最大回撤 | 权益曲线的最大峰谷回撤 | < 25% |

---

## 四、各策略具体优化方向

### 4.1 策略一 / 一-B：VWAP 缩量埋伏

**当前配置要点**（以 `spy-vwap-ambush` 为例）：

| 条件 | 当前值 | 分析 |
|------|--------|------|
| `day_change_pct > -0.3` | -0.3% | 过于宽松，-0.2% 已属弱势日 |
| `close > ema_50` | 5m | 合理的趋势过滤 |
| `within_pct_of(close, vwap, 0.1%)` | 0.1% | 较严格，可能错过刚离开VWAP的机会 |
| `spread_pct < 0.15` | 0.15% | 单一死水判断 |

**优化建议**：

1. **收紧非空头日过滤**：
   - 当前：`day_change_pct > -0.3`
   - 建议：`day_change_pct > -0.15`（更严格排除弱势日）
   - 或增加条件：`close > vwap`（确认日内偏多）

2. **增强"死水"判断**：
   - 当前策略一用 `body_pct < 0.05% OR volume_ratio < 0.5`（OR 太松）
   - 建议改为 AND：真正的死水应该**同时**量缩 + K线极小
   - 或追加确认：连续 2 根 5m K 线都满足缩量条件

3. **增加时间权重**：
   - 0DTE ATM Call 的 Theta 衰减在午后加速
   - 建议缩短策略一的交易窗口从 `10:00-15:30` 收窄至 `10:00-14:00`

4. **VWAP 距离分级**：
   - < 0.05%：极度贴合（加分 10）
   - 0.05% - 0.1%：正常范围（加分 5）
   - 0.1% - 0.15%：边缘（加分 0）
   - \> 0.15%：不交易

### 4.2 策略二：布林带极限挤压

**当前配置要点**：

| 条件 | 当前值 | 分析 |
|------|--------|------|
| `day_change_pct > -0.3` | -0.3% | 同上，建议收紧 |
| `bb_width_pct < 0.15` | 0.15% | 固定阈值，不同标的波动率差异大 |
| `close > ema_200` | 5m | EMA200 在午后过于滞后 |

**优化建议**：

1. **BBW 阈值按标的差异化**：
   - SPY 正常 BBW ≈ 0.1% - 0.3%，挤压时 < 0.12%
   - TSLA 正常 BBW ≈ 0.5% - 1.5%，挤压时 < 0.3%
   - 建议在 YAML 中按标的设置 `bb_squeeze_threshold` 或使用百分位数（当天 BBW 最低 10%）

2. **方向预判升级**：
   - 当前：`close > ema_200`（滞后严重）
   - 建议改为：`close > vwap`（更灵敏的日内方向指标）
   - 增加确认：`macd_histogram > 0` 或 `rsi > 50`

3. **增加 Trailing Stop**：
   - 当前止盈 +100% 是固定目标，爆发行情可能远超
   - 建议：到达 +50% 后启用移动止盈（回撤 20% 平仓）
   - `state.py` 中 `PositionInfo.highest_price` 已支持此功能

### 4.3 策略三：极端超卖反转

**当前配置要点**：

| 条件 | 当前值 | 分析 |
|------|--------|------|
| `rsi < 35` (15m) | 35 | 从 25 放宽过多，35 不算极端超卖 |
| `vwap_distance_pct < -1.0` | -1.0% | 从 -1.5% 放宽，可能接到"下跌中继" |
| `close > prev_bar_high` | 5m | K 线反转确认，合理 |

**优化建议**：

1. **RSI 阈值回调**：
   - 建议收回至 < 30（真正的极端超卖）
   - 增加 RSI 底背离确认：价格创新低但 RSI 不创新低

2. **增加成交量确认**：
   - 当前完全没有量的条件
   - 建议增加：`volume_ratio > 1.5`（超卖反弹时需要放量确认）
   - 这是区分"真反弹"与"死猫跳"的关键

3. **连续阳线确认**：
   - 当前只要求一根 K 线 `close > prev_bar_high`
   - 建议增加第二确认：等下一根 5m 也收阳后再入场
   - 可在状态机中增加 `PENDING_CONFIRM` 状态实现

4. **止盈上调**：
   - 当前 +30% 止盈可能过低
   - 均值回归目标是回到 VWAP，如果从 -1.5% 偏离回到 0%，对应期权涨幅远超 30%
   - 建议：+50% 固定止盈 或 回到 VWAP 附近平仓

### 4.4 策略四：VWAP 绝望压制 (Put)

**当前配置要点**：

| 条件 | 当前值 | 分析 |
|------|--------|------|
| `day_change_pct < -0.3` | -0.3% | 空头日确认，合理 |
| `within_pct_of(close, vwap, 0.12%)` | 0.12% | 反弹到 VWAP 附近 |
| `rsi < 45` | 45 | 反弹无力确认 |
| `body_pct < 0.05 OR spread_pct < 0.15` | OR组 | 死水判断 |

**优化建议**：

1. **增加 VIX 环境过滤**：
   - VIX > 18 时空头策略更有效（恐慌情绪助力下跌）
   - 可通过增加 VIX 作为监控标的实现

2. **RSI 阈值微调**：
   - `rsi < 45` 可能过于宽松
   - 在 VWAP 附近反弹时，RSI < 42 才更能说明"反弹无力"
   - 或改为：RSI 从高位回落中（`rsi` 前值 > 当前值）

3. **增加量的确认**：
   - 反弹到 VWAP 时缩量 → 说明买盘不足
   - 增加条件：`volume_ratio < 0.8`

### 4.5 策略五：早盘诱多衰竭 (Put)

**当前配置要点**：

| 条件 | 当前值 | 分析 |
|------|--------|------|
| `close > vwap` | 5m | 看似多头（诱多确认） |
| `close crosses_below ema_9` | 5m | 跌破生命线 |
| `macd_histogram turns_negative` | 5m | 动能死叉 |
| `rsi < 60` | 5m | 冲高回落确认 |

**优化建议**：

1. **增加跌破确认的量能条件**：
   - 跌破 EMA 9 时如果缩量，可能只是假跌破
   - 建议增加：`volume_ratio > 1.2`（放量下破更可靠）

2. **增加连续确认**：
   - `crosses_below` 是瞬间事件，容易出现假信号
   - 建议等 1 根 5m K 线确认（下一根仍在 EMA 9 之下）

3. **时间窗口细化**：
   - 当前 10:00-11:30 较合理
   - 可进一步收窄：10:15-11:00（最典型的早盘诱多时间段）

### 4.6 策略六：午后挤压久盘必跌 (Put)

**当前配置要点**：

| 条件 | 当前值 | 分析 |
|------|--------|------|
| `bb_width_pct < 0.15` | 0.15% | 与策略二镜像 |
| `close < vwap` | 5m | 偏空 |
| `close < ema_21` | 5m | 均线压制 |

**优化建议**：

1. **BBW 阈值差异化**：与策略二相同问题，需按标的调整
2. **增加空头动能确认**：增加 `macd_histogram < 0` 或 `rsi < 45`
3. **增加 Trailing Stop**：与策略二镜像，爆发行情应让利润跑

---

## 五、系统性优化框架

### 5.1 三层过滤体系

```
┌──────────────────────────────────────────────────────────┐
│ 第一层：宏观市场环境过滤（新增）                              │
│                                                          │
│  • SPY 日线趋势（20日EMA之上 → 多头环境，之下 → 空头环境）    │
│  • VIX 水平（< 15 低波，15-25 正常，> 25 高波）              │
│  • 重大事件日（FOMC/CPI/非农发布日 → 暂停所有策略）           │
│  • 前日大盘涨跌（连续3日上涨后做多策略胜率下降）              │
│                                                          │
│  → 决定：今天该做哪类策略？做多/做空/都不做？                  │
└──────────────────────────┬───────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────┐
│ 第二层：时段适配（已部分实现）                               │
│                                                          │
│  • 09:30-10:00  开盘30分钟 → 高波动，不适合埋伏策略          │
│  • 10:00-13:00  → 适合 VWAP 埋伏类（策略一/一-B/四）        │
│  • 13:00-14:00  → 适合挤压突破类（策略二/六）                │
│  • 14:00-15:30  → 适合趋势跟随（Power Hour）               │
│  • 10:00-11:30  → 适合早盘诱多（策略五）                    │
│                                                          │
│  → 决定：当前时段启用哪些策略？                              │
└──────────────────────────┬───────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────┐
│ 第三层：信号质量评级（已实现）                               │
│                                                          │
│  • evaluate_entry_quality() → A/B/C/D 评分               │
│  • 建议：只交易 A 和 B 级信号                               │
│  • C 级信号仅纸盘记录，用于验证过滤效果                      │
│  • D 级信号直接丢弃                                        │
│                                                          │
│  → 决定：这个信号值不值得交易？                              │
└──────────────────────────────────────────────────────────┘
```

### 5.2 市场状态自适应

| 市场状态 | 判断方式 | 适合的策略 | 不适合的策略 |
|---------|---------|----------|------------|
| 趋势上涨日 | SPY > VWAP 且持续创新高 | 策略一/一-B/二 | 策略四/五/六 |
| 趋势下跌日 | SPY < VWAP 且持续创新低 | 策略四/五/六 | 策略一/一-B/二 |
| 震荡日 | SPY 围绕 VWAP 来回穿越 | 策略三（均值回归） | 策略二/六（挤压） |
| 高波动日 | ATR > 1.5倍均值 或 VIX > 25 | 缩小仓位或暂停 | 所有策略 |
| 事件日 | FOMC/CPI/非农 | 暂停 | 所有策略 |

### 5.3 动态止盈止损

当前所有策略使用固定百分比止盈止损。建议升级为动态机制：

#### 基于 ATR 的动态止损

```
止损幅度 = 1.5 × ATR（5m级别）/ 入场价 × 100%

波动大的日子 → 止损自动放宽（避免被正常波动震出）
波动小的日子 → 止损自动收紧（减少不必要损失）
```

#### 移动止盈（Trailing Stop）

```
当浮盈达到止盈目标的 50% 时 → 激活移动止盈
移动止盈回撤阈值 = 从最高点回撤 15-20% 时平仓

例：止盈目标 +100%
  浮盈 +50% → 激活移动止盈
  浮盈 +80% → 最高点 = +80%
  浮盈回落到 +64% (80% × 0.8) → 平仓
  实际收益 +64%，优于固定 +50% 止盈
```

**实现方式**：`state.py` 中 `PositionInfo.highest_price` 已支持追踪最高价，只需在 `matcher.py` 的 `evaluate_exit()` 中增加 `trailing_stop` 逻辑。

### 5.4 信号确认等待期

当前：条件满足 → 立即触发信号。

建议：条件满足 → 等待确认 → 确认通过 → 触发信号。

```
状态机扩展：
WATCHING → PENDING_CONFIRM → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING

PENDING_CONFIRM 逻辑：
  - 入场条件满足后，标记为 PENDING_CONFIRM
  - 等待下一根 5m K 线收盘
  - 如果下一根 K 线仍满足条件 → 升级为 ENTRY_TRIGGERED
  - 如果下一根 K 线不满足 → 回退到 WATCHING
  - 超时（2根K线后仍未确认）→ 回退到 WATCHING
```

这样做的核心收益：**排除瞬间假信号**，尤其对 `crosses_below`、`turns_negative` 等状态型比较器特别有效。

---

## 六、实施路线图

### Phase 1：建立数据基础（1-2 天）

| 任务 | 涉及文件 | 说明 |
|------|---------|------|
| 扩展 `signals` 表 Schema | `src/store/sqlite_store.py` | 增加 entry_price, exit_price, pnl_pct, outcome, quality_grade 等字段 |
| 修改 `save_signal()` | `src/store/sqlite_store.py` | 支持写入新字段 |
| `/confirm` 写入 entry_price | `src/notification/telegram.py` | 确认建仓时记录入场价 |
| 出场信号记录 PnL | `src/strategy/matcher.py` | 出场时计算并存储盈亏 |

### Phase 2：积累数据（2-4 周）

- 每日运行系统，记录所有信号触发
- 无论是否实盘，都通过 `/confirm` 或 `/skip` 记录结果
- 纸盘也可以：收到信号后记录"假设入场价"，追踪后续走势
- 目标：每个策略至少积累 20+ 个信号样本

### Phase 3：数据分析与策略裁剪（1 天）

| 分析维度 | 行动 |
|---------|------|
| 整体胜率 < 40% 的策略 | 禁用或大幅修改 |
| C/D 级信号胜率远低于 A/B | 提高 `min_score` 门槛，只交易 A/B |
| 特定标的持续亏损 | 从该策略的 watchlist 中移除 |
| 特定时段效果差 | 收窄 `trading_window` |
| 特定出场原因占比高 | 调整止盈止损参数 |

### Phase 4：构建回测框架（3-5 天）

| 任务 | 说明 |
|------|------|
| 实现 `SimpleBacktester` 类 | 读取 `indicator_history` 模拟策略匹配 |
| 参数扫描工具 | 对关键阈值（RSI、BBW、VWAP距离）进行网格搜索 |
| 回测报告生成 | 输出胜率、盈亏比、利润因子、权益曲线 |

### Phase 5：精细化优化（持续迭代）

| 任务 | 说明 |
|------|------|
| 实施宏观过滤层 | 根据 SPY 日线趋势自动启停策略组 |
| 实施 Trailing Stop | 在 `evaluate_exit()` 中增加移动止盈逻辑 |
| 实施信号确认等待期 | 在 `state.py` 中增加 `PENDING_CONFIRM` 状态 |
| BBW 阈值按标的差异化 | 在 YAML 中支持 per-symbol 配置 |
| 增加量能确认条件 | 在策略三/四/五中增加 `volume_ratio` 条件 |

---

## 七、参数优化备忘录

以下参数需通过回测数据确认最优值，不要凭感觉调整：

| 参数 | 当前值 | 回测扫描范围 | 适用策略 |
|------|--------|-------------|---------|
| 非空头日阈值 | -0.3% | [-0.5%, -0.3%, -0.2%, -0.1%, 0%] | 策略一/一-B/二 |
| VWAP 距离阈值 | 0.1% | [0.05%, 0.08%, 0.1%, 0.12%, 0.15%] | 策略一/一-B |
| BBW 挤压阈值 | 0.15% | [0.10%, 0.12%, 0.15%, 0.18%, 0.20%] | 策略二/六 |
| RSI 超卖阈值 | 35 | [25, 28, 30, 32, 35] | 策略三 |
| VWAP 乖离阈值 | -1.0% | [-0.8%, -1.0%, -1.2%, -1.5%] | 策略三 |
| RSI 反弹无力阈值 | 45 | [40, 42, 45, 48] | 策略四 |
| 止盈比例 | 各不同 | [+20%, +30%, +50%, +80%, +100%] | 所有策略 |
| 止损比例 | 各不同 | [-15%, -20%, -25%, -30%] | 所有策略 |
| quality min_score | 50-60 | [40, 50, 60, 70, 80] | 所有策略 |

**回测原则**：
- 每次只调整一个参数，固定其余变量
- 样本量 < 30 时不做统计推断
- 避免过度拟合：回测结果需在样本外数据上验证
- 参数选择偏保守（宁可错过信号，不可降低胜率）

---

## 八、核心理念总结

```
先有数据 → 再做决策 → 最后精调

没有数据支撑的参数调整 = 碰运气
胜率 × 盈亏比 > 1 才是正期望系统
少交易、精交易 优于 多交易、乱交易
```

策略打磨不是一次性工程，而是**"记录 → 分析 → 优化 → 验证"**的持续循环。系统的核心竞争力不在于单个策略的精妙，而在于这个闭环的执行效率。
