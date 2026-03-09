> **注意：** 所有止盈止损阈值基于**股票价格变动百分比**，期权实际盈亏约为股价变动的 12-20 倍。

# 交易哲学：三轨体系

从 Yahoo Finance（15-20秒延迟）切换到 Futu API（毫秒级实时推送）后，交易哲学升级为**三轨体系**：

- **左侧：死水中从容埋伏**（策略 1-6）— 在波动率枯竭、成交量萎缩的"死水"时段提前布局
- **均值回归：刺穿后果断回扑**（策略 7）— 多时间框架共振，布林带刺穿后四维打分精确入场
- **右侧：突破时果断追击**（策略 8-10）— Futu 低延迟解锁的新能力，在价格突破关键位时配合放量确认果断进场

### 右侧策略约束
- 仅使用 LV1 行情（无 tick/order book 深度）
- 三重确认原则：价格突破 + 量能突变 + 动能指标方向一致
- Cooldown 300秒，防止同一位置反复触发

---

## 左侧埋伏策略

### 策略 1：VWAP "极度缩量"埋伏
**YAML**: `config/strategies/vwap_low_vol_ambush.yaml` (`vwap-low-vol-ambush`)

大趋势向上时，价格回落到 VWAP 附近且成交量极度萎缩（死水状态），在横盘中从容埋伏进场。

- **非空头日过滤**：`day_change_pct > -0.15`（收紧）
- 价格 > EMA 50 + VWAP 偏离 < 0.08%（收紧）
- K线实体小 AND 量比低（双重确认，从 OR 改为 AND）
- **标的**：`["QQQ", "AAPL", "TSLA", "META", "AMZN"]`（已移除 NVDA — 3T 33%WR -0.61%）
- 止盈 +0.5% 股票价格 / 止损 -0.3% 股票价格 / 质量门槛 65 分
- `max_adx: 30`

### 策略 1-B：SPY VWAP 缩量埋伏（收紧版）— 已禁用
**YAML**: `config/strategies/spy_vwap_ambush.yaml` (`spy-vwap-ambush`) — `enabled: false`

SPY 专属。价格在 VWAP ±0.08% 内 + K 线高低价差 < 0.12%。
- 止盈 +0.5% 股票价格 / 止损 -0.3% 股票价格

### 策略 2：布林带极限挤压 (The Squeeze)
**YAML**: `config/strategies/bb_squeeze_ambush.yaml` (`bb-squeeze-ambush`)

午后垃圾时间，布林带宽度跌至全天百分位 10% 以下（改用相对值）。
- **标的**：`["SPY", "QQQ", "NVDA", "TSLA", "META"]`（无 AAPL）
- 方向判定升级：`close > vwap`（更灵敏，替代 EMA200）
- 新增 RSI > 48 多头动能确认
- 新增 trailing_stop（股价 0.5% 激活，0.2% 回撤）
- 止盈 +0.8% 股票价格 / 止损 -0.5% 股票价格
- `max_adx: 30`

### 策略 3：极端超卖钝化反转
**YAML**: `config/strategies/extreme_oversold_reversal.yaml` (`extreme-oversold-reversal`)

- **标的**：`["QQQ", "TSLA", "AMD"]`（已移除 AAPL、NVDA、SPY）
- 15m RSI 回到真正极端 < 30
- VWAP 乖离率收紧至 < -0.8%
- 新增 `volume_spike > 1.3` 反弹放量确认
- 时间窗口截止 11:00
- Cooldown 1800秒
- 止盈 +0.8% 股票价格 / 止损 -0.3% 股票价格

### 策略 4（做空）：VWAP 绝望压制
**YAML**: `config/strategies/vwap_rejection_put.yaml` (`vwap-rejection-put`)

- **标的**：`["SPY", "QQQ", "AMZN"]`（已移除 AAPL — 8T 25%WR，系统最大亏损源）
- RSI 收紧至 < 40（更严格弱势确认）
- VWAP 范围收紧至 ±0.10%
- `volume_ratio < 0.7`（缩量要求更严格，过滤正常量假信号）
- 止盈 +0.5% 股票价格 / 止损 -0.3% 股票价格
- `max_adx: 30`

### 策略 5（做空）：早盘诱多衰竭
**YAML**: `config/strategies/morning_trap_put.yaml` (`morning-trap-put`)

- RSI 收紧至 < 60
- 时间窗口收紧至 10:00-12:00
- 新增 `volume_ratio > 0.8` 跌破放量确认
- 新增 `vwap_distance_pct > 0.05` 明确在 VWAP 之上
- MACD `turns_negative` 附带 `min_magnitude: 0.003`
- 止盈 +0.5% 股票价格 / 止损 -0.3% 股票价格
- `max_adx: 30`

### 策略 6（做空）：午后挤压久盘必跌
**YAML**: `config/strategies/bb_squeeze_bearish.yaml` (`bb-squeeze-bearish`)

- 改用 `bb_width_percentile < 10`（相对值）
- 新增 MACD histogram < -0.01 + RSI < 40 空头确认
- 止盈 +1.5% 股票价格 / 止损 -0.5% 股票价格
- `max_adx: 30`

---

## 均值回归策略

### 策略 7：布林带刺穿回扑做多 (BB Piercing Reversion Call)
**YAML**: `config/strategies/bb_piercing_reversion_call.yaml` (`bb-piercing-reversion-call`)

多时间框架共振均值回归。15 分钟线刺穿下轨后四维打分，5 分钟精确入场，目标回归中轨。

- **标的**：`["SPY", "QQQ", "TSLA", "META"]`
- **交易窗口**：10:30-14:30 ET
- **非大跌日过滤**：`day_change_pct > -1.0`
- **15m 战术层**：前根 K 线刺穿下轨（Low < BB Lower）+ 收盘回到轨道内 + 当根确认回扑
- **Squeeze 过滤**：`bb_width_expansion < 1.5`（排除 Squeeze 后突破）
- **四维打分**（`MIN_MATCH >= 3/4`）：
  1. BB %B 从极端区域回归（> -0.1）
  2. 动量：RSI < 35 OR Stochastic K 金叉 D
  3. 缩量刺穿：`volume_spike < 1.5`
  4. 下影线较长（多头抵抗）：`lower_shadow_pct > 0.1`
- **5m 执行层**：%B crosses_above 0（回到 BB 内）OR 超卖区 KD 金叉
- **动态止盈**：到达 15m BB 中轨（`indicator_target`）/ 安全网 +0.8% / 止损 -0.35%
- **质量门槛** 65 分（base_score: 50，含 %B 极端奖励、ADX 环境、回扑力度评分）
- `max_adx: 30` / Cooldown 1800秒

### 策略 7-B：布林带刺穿回扑做空 (BB Piercing Reversion Put)
**YAML**: `config/strategies/bb_piercing_reversion_put.yaml` (`bb-piercing-reversion-put`)

策略 7 的做空镜像。15 分钟线刺穿上轨后四维打分，5 分钟精确入场。

- **标的**：`["SPY", "QQQ", "TSLA", "META"]`
- **非大涨日过滤**：`day_change_pct < 1.0`
- **15m 战术层**：前根 K 线刺穿上轨（High > BB Upper）+ 收盘回到轨道内 + 当根确认回落
- **四维打分**（`MIN_MATCH >= 3/4`）：RSI > 65 OR KD 死叉、缩量、上影线长
- **5m 执行层**：%B crosses_below 1.0 OR 超买区 KD 死叉
- **动态止盈**：到达 15m BB 中轨 / 安全网 +0.8% / 止损 -0.35%
- `max_adx: 30`

---

## 右侧突破策略

### 策略 8：VWAP 突破追涨 (Breakout Call) — 已禁用
**YAML**: `config/strategies/vwap_breakout_momentum.yaml` (`vwap-breakout-momentum`) — `enabled: false`

经典突破策略，Yahoo 延迟下无法执行。
- 5m `close crosses_above vwap` + `volume_spike > 1.3`
- 5m MACD histogram > 0
- 止盈 +0.4% 股票价格 / 止损 -0.2% 股票价格 / 质量门槛 55 分

### 策略 9：EMA 金叉动能追击 (Momentum Call)
**YAML**: `config/strategies/ema_momentum_breakout.yaml` (`ema-momentum-breakout`)

- 5m `ema_9 crosses_above ema_21`（`confirm_bars: 1`）
- `close > vwap` + RSI 50-70 + `volume_ratio > 1.0`
- 止盈 +0.5% 股票价格 / 止损 -0.2% 股票价格
- `min_adx: 20`（需要趋势环境）

### 策略 10：VWAP 跌破追空 (Breakdown Put)
**YAML**: `config/strategies/breakdown_vwap_put.yaml` (`breakdown-vwap-put`)

策略 8 的空头镜像。
- 5m `close crosses_below vwap` + `volume_spike > 1.5`（折中阈值）
- 5m MACD histogram < 0
- 止盈 +0.4% 股票价格 / 止损 -0.15% 股票价格
- `min_adx: 20`（需要趋势环境）

---

## 系统能力

### 可用指标（`src/indicator/engine.py`）
| 指标 | YAML 引用 | 说明 |
|---|---|---|
| RSI | `RSI.value` | 相对强弱指数（14周期） |
| MACD | `MACD.line / signal / histogram` | 移动平均收敛/发散 |
| EMA | `EMA.ema_9 / ema_21 / ema_50 / ema_200` | 指数移动平均 |
| VWAP | `VWAP.value` | 成交量加权平均价 |
| ATR | `ATR.value` | 平均真实波幅 |
| ADX | `ADX.value` | 平均方向指数（14周期） |
| 布林带 | `BOLLINGER.upper / lower / middle / width_pct / width_percentile / pct_b / width_expansion` | `pct_b` = %B 位置；`width_expansion` = 当前BBW/近10期均值；`width_percentile` = 当天百分位 |
| KD 随机 | `STOCHASTIC.k / d` | 随机振荡器（9周期，%K/%D） |
| K线 | `CANDLE.body_pct / range_pct / spread_pct / upper_shadow_pct / lower_shadow_pct` | 含上下影线占比 |
| 价格 | `PRICE.close / open / high / low / prev_bar_close / prev_bar_low / prev_bar_high / ...` | 含 `volume_spike`、`volume_ratio`、`day_change_pct` 等 |

### 可用 Comparator（`src/strategy/matcher.py`）
| Comparator | 说明 |
|---|---|
| `>`, `<`, `>=`, `<=`, `==` | 标准比较 |
| `crosses_above`, `crosses_below` | 穿越（需要前值） |
| `breaks_above`, `breaks_below` | 突破确认（含微小 margin 防边缘触发） |
| `turns_positive`, `turns_negative` | 由负转正 / 由正转负 |
| `within_pct_of` | 区间判断 |

### 规则组合操作符
| 操作符 | 说明 |
|---|---|
| `AND` | 所有规则必须满足 |
| `OR` | 任一规则满足即可 |
| `MIN_MATCH` | 至少 `min_count` 条规则满足（如 3/4 通过） |

### 退出类型
| 类型 | 说明 |
|---|---|
| `take_profit_pct` | 固定止盈（股票价格 %） |
| `stop_loss_pct` | 固定止损（股票价格 %） |
| `trailing_stop` | 追踪止盈（`activation_pct` + `trail_pct`，股票价格 %），PUT 方向追踪最低价 |
| `indicator_target` | 动态指标止盈（如到达 BB 中轨），需指定 `indicator`/`field`/`timeframe` |
| `time_exit` | 收盘前强制退出 |
