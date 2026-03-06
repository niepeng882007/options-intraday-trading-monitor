# 交易哲学：双轨体系

从 Yahoo Finance（15-20秒延迟）切换到 Futu API（毫秒级实时推送）后，交易哲学升级为**双轨体系**：

- **左侧：死水中从容埋伏**（策略 1-6）— 延续原有优势，在波动率枯竭、成交量萎缩的"死水"时段提前布局
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
- 止盈 50% / 止损 20% / 质量门槛 65 分

### 策略 1-B：SPY VWAP 缩量埋伏（收紧版）
**YAML**: `config/strategies/spy_vwap_ambush.yaml` (`spy-vwap-ambush`)

SPY 专属。价格在 VWAP ±0.08% 内 + K 线高低价差 < 0.12%。
- 止盈提升至 35%

### 策略 2：布林带极限挤压 (The Squeeze)
**YAML**: `config/strategies/bb_squeeze_ambush.yaml` (`bb-squeeze-ambush`)

午后垃圾时间，布林带宽度跌至全天百分位 10% 以下（改用相对值）。
- 方向判定升级：`close > vwap`（更灵敏，替代 EMA200）
- 新增 RSI > 48 多头动能确认
- 新增 trailing_stop（50% 激活，20% 回撤）

### 策略 3：极端超卖钝化反转
**YAML**: `config/strategies/extreme_oversold_reversal.yaml` (`extreme-oversold-reversal`)

- 15m RSI 回到真正极端 < 30
- VWAP 乖离率收紧至 < -1.2%
- 新增 `volume_spike > 1.5` 反弹放量确认
- 止盈提升至 50%

### 策略 4（做空）：VWAP 绝望压制
**YAML**: `config/strategies/vwap_rejection_put.yaml` (`vwap-rejection-put`)

- RSI 收紧至 < 42
- VWAP 范围收紧至 ±0.10%
- 新增 `volume_ratio < 0.8` 反弹缩量确认

### 策略 5（做空）：早盘诱多衰竭
**YAML**: `config/strategies/morning_trap_put.yaml` (`morning-trap-put`)

- RSI 收紧至 < 55
- 时间窗口收紧至 10:15-11:00
- 新增 `volume_ratio > 1.0` 跌破放量确认
- 新增 `vwap_distance_pct > 0.1` 明确在 VWAP 之上

### 策略 6（做空）：午后挤压久盘必跌
**YAML**: `config/strategies/bb_squeeze_bearish.yaml` (`bb-squeeze-bearish`)

- 改用 `bb_width_percentile < 10`（相对值）
- 新增 MACD histogram < 0 + RSI < 48 空头确认

---

## 右侧突破策略

### 策略 8：VWAP 突破追涨 (Breakout Call)
**YAML**: `config/strategies/vwap_breakout_momentum.yaml` (`vwap-breakout-momentum`)

经典突破策略，Yahoo 延迟下无法执行。
- 1m `close crosses_above vwap` + `volume_spike > 1.5`
- 5m MACD histogram > 0
- 止盈 40% / 止损 15% / 质量门槛 55 分

### 策略 9：EMA 金叉动能追击 (Momentum Call)
**YAML**: `config/strategies/ema_momentum_breakout.yaml` (`ema-momentum-breakout`)

- 5m `ema_9 crosses_above ema_21`（需要修复后的 reference_field 组合）
- `close > vwap` + RSI 50-70 + `volume_ratio > 1.2`
- 止盈 50% / 止损 20%

### 策略 10：VWAP 跌破追空 (Breakdown Put)
**YAML**: `config/strategies/breakdown_vwap_put.yaml` (`breakdown-vwap-put`)

策略 8 的空头镜像。
- 1m `close crosses_below vwap` + `volume_spike > 1.5`
- 5m MACD histogram < 0
- 止盈 40% / 止损 15%

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
| 布林带 | `BOLLINGER.upper / lower / width_pct / width_percentile` | `width_percentile` = 当天 BBW 百分位数 (0-100) |
| K线 | `CANDLE.body_pct / range_pct / spread_pct` | `spread_pct` 是 `range_pct` 的别名 |
| 价格 | `PRICE.close / open / high / low / ...` | 含 `volume_spike`（前3根均量比）、`volume_ratio`（20根均量比）等 |

### 可用 Comparator（`src/strategy/matcher.py`）
| Comparator | 说明 |
|---|---|
| `>`, `<`, `>=`, `<=`, `==` | 标准比较 |
| `crosses_above`, `crosses_below` | 穿越（需要前值） |
| `breaks_above`, `breaks_below` | 突破确认（含微小 margin 防边缘触发） |
| `turns_positive`, `turns_negative` | 由负转正 / 由正转负 |
| `within_pct_of` | 区间判断 |

### 退出类型
| 类型 | 说明 |
|---|---|
| `take_profit_pct` | 固定止盈 |
| `stop_loss_pct` | 固定止损 |
| `trailing_stop` | 追踪止盈（`activation_pct` + `trail_pct`） |
| `time_exit` | 收盘前强制退出 |
