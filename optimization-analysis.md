# Options Intraday Trading Monitor - 项目分析与优化建议

## 项目概述

这是一个**实时期权日内交易监控系统**，核心流程：

```
YahooCollector(10s/30s/60s轮询) → IndicatorEngine(RSI/MACD/EMA/VWAP/BB/ATR)
  → RuleMatcher(嵌套规则树) → StateManager(状态机) → TelegramNotifier → SQLite持久化
```

**当前策略风格：** 7个策略，主打 VWAP/BB 伏击 + 0DTE 期权，在低波动整理期买入廉价期权等待突破。

---

## 影响胜率的关键问题 & 优化建议

### 一、数据延迟问题（最高优先级）

**问题：** Yahoo Finance 有 15-20 秒延迟，而多个策略依赖 VWAP ±0.1% 的精确价格区间。信号到达时价格可能已偏离。

**建议：**
- 接入更低延迟数据源（broker API 如 IBKR TWS API，延迟 <1s）
- 或者放宽 VWAP proximity 阈值（0.1% → 0.2-0.3%），适应数据延迟

### 二、缺少 IV（隐含波动率）过滤

**问题：** 系统不检查 IV rank/percentile。0DTE 期权在低 IV 时便宜但可能「死」掉，高 IV 时买入成本过高。

**建议：**
- 在 IndicatorEngine 中添加 IV rank 指标（利用 option chain 数据已有的 impliedVolatility 字段）
- 策略中增加 `iv_rank < 50` 或 `iv_percentile` 过滤条件
- 避免在重大事件（财报、FOMC）前买入高 IV 期权

### 三、出场逻辑过于简单

**问题：** 当前只有固定百分比止盈/止损 + 时间强制平仓，没有追踪止损。持仓期间利润可能回吐。

**建议：**
- 添加 **trailing stop**：如盈利超过 30% 后回撤 15% 即平仓
- 添加 **波动率退出**：BB 宽度突然扩大时平仓（squeeze 已 resolved）
- 添加 **指标反转退出**：RSI 从超买回落、MACD 柱状图翻转等
- 在 `matcher.py` 的 `evaluate_exit()` 中扩展退出规则引擎

### 四、BB Squeeze 方向性不足

**问题：** BB squeeze 策略只检测挤压状态，不判断突破方向。挤压后涨跌概率各 50%。

**建议：**
- 增加方向确认指标：MACD 方向、RSI 趋势、EMA 排列
- 等待 squeeze 释放后第一根确认 K 线再入场（而非在 squeeze 中入场）
- 结合成交量突破确认

### 五、缺少支撑/阻力位验证

**问题：** 纯指标驱动，不考虑价格结构。可能在关键阻力位做多或关键支撑位做空。

**建议：**
- 添加日内 pivot points（PP, R1, R2, S1, S2）计算
- 检测前日高低点作为关键位
- 在入场质量评分中加入「距离关键位」因子

### 六、缺少仓位管理和风控

**问题：** 没有账户级风控。5 个 symbol × 7 个策略 = 最多 35 个同时信号，一个坏日可能爆仓。

**建议：**
- 添加全局风控：每日最大亏损限制、最大同时持仓数
- 策略间相关性检测：避免同方向重复入场（如 SPY call + QQQ call）
- 在 `state.py` 中添加 portfolio-level 约束

### 七、入场质量评分可优化

**问题：** 当前评分以 VWAP 距离和成交量为主，min_score 50-60 门槛偏低。

**建议：**
- 提高 min_score 到 65-70（减少低质量信号）
- 增加趋势强度因子（多时间框架 EMA 一致性）
- 增加市场环境因子（VIX 水平、大盘趋势）
- A/B 级信号才发通知，C/D 级仅记录

### 八、回测框架缺失

**问题：** 无法验证策略修改的效果，只能靠实盘试错。

**建议：**
- 构建简易回测模块：复用现有 IndicatorEngine + RuleMatcher
- 保存历史 bar 数据到 SQLite 供回测使用
- 每次策略修改前先跑回测验证

---

## 快速可实施的优化（代码层面）

| 优化项 | 改动文件 | 复杂度 |
|--------|----------|--------|
| Trailing stop 出场 | `matcher.py`, `state.py` | 中 |
| 提高质量评分门槛 | 各策略 YAML | 低 |
| BB squeeze 方向确认 | 策略 YAML 增加规则 | 低 |
| 日内 pivot points | `engine.py` | 中 |
| 最大同时持仓数限制 | `state.py`, `main.py` | 低 |
| 每日最大亏损限制 | `state.py` | 低 |
| IV rank 过滤 | `engine.py`, 策略 YAML | 中 |
