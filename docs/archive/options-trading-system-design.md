> **已归档** — 本文档为系统最初的架构设计方案，部分设计已实现、部分已调整。当前系统架构见 `CLAUDE.md`。仅供历史参考。

# 美股期权日内交易——实时监控与智能通知系统设计方案

---

## 一、系统概述

### 1.1 项目背景

美股期权日内交易对时效性要求极高，交易者面临两大核心痛点：

- **情绪干扰**：手动操作时，恐惧与贪婪频繁覆盖理性策略，导致非计划性亏损。
- **时差约束**：身处中国大陆或其他与美国存在大时差的地区时，无法在美股交易时段（北京时间 21:30 – 次日 04:00，夏令时）全程盯盘。

本系统的核心目标是：**将交易者的策略逻辑从"人脑实时决策"转化为"系统自动监控 + 即时通知"**，实现纪律化交易，同时保留人类最终决策权。

### 1.2 核心设计原则

| 原则 | 说明 |
|------|------|
| **只通知，不自动交易** | 系统仅负责监控与告警，最终买卖决策由交易者本人执行，规避自动交易的法律与资金风险 |
| **亚秒级实时性** | 从行情变动到通知送达，端到端延迟控制在 **< 2 秒** |
| **策略热更新** | 策略配置修改后 **即时生效**，无需重启服务 |
| **高可用** | 交易时段内系统可用性 ≥ 99.9%，关键链路有降级方案 |

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           用户交互层 (User Layer)                            │
│                                                                             │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────────┐  │
│   │  Telegram Bot │    │  Web 管理面板  │    │  策略配置 API (REST/gRPC)    │  │
│   │  (通知接收)    │    │  (策略/监控)   │    │  (程序化接入)                │  │
│   └──────┬───────┘    └──────┬───────┘    └──────────────┬───────────────┘  │
│          │                   │                           │                  │
└──────────┼───────────────────┼───────────────────────────┼──────────────────┘
           │                   │                           │
           ▼                   ▼                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         应用服务层 (Application Layer)                        │
│                                                                             │
│   ┌────────────────────┐   ┌────────────────────┐   ┌──────────────────┐   │
│   │  通知服务            │   │  策略管理服务        │   │  指标计算引擎     │   │
│   │  (Notification)     │   │  (Strategy Manager) │   │  (Indicator Eng.) │   │
│   └────────┬───────────┘   └────────┬───────────┘   └────────┬─────────┘   │
│            │                        │                        │              │
│            │         ┌──────────────▼──────────────┐         │              │
│            │         │     策略匹配引擎              │         │              │
│            ◄─────────┤     (Strategy Matching)      ◄─────────┘              │
│            │         │                              │                        │
│            │         └──────────────▲──────────────┘                        │
│            │                        │                                       │
└────────────┼────────────────────────┼───────────────────────────────────────┘
             │                        │
             │                        │
┌────────────┼────────────────────────┼───────────────────────────────────────┐
│            │        数据层 (Data Layer)                                      │
│            │                        │                                       │
│   ┌────────▼───────┐   ┌───────────┴──────────┐   ┌──────────────────────┐ │
│   │  Redis          │   │  时序数据库            │   │  PostgreSQL          │ │
│   │  (实时行情缓存   │   │  (K线/指标历史)        │   │  (策略/用户/日志)     │ │
│   │   + Pub/Sub)    │   │  TimescaleDB/QuestDB  │   │                      │ │
│   └────────▲───────┘   └───────────▲──────────┘   └──────────────────────┘ │
│            │                       │                                        │
└────────────┼───────────────────────┼────────────────────────────────────────┘
             │                       │
┌────────────┼───────────────────────┼────────────────────────────────────────┐
│            │   数据采集层 (Data Ingestion Layer)                              │
│            │                       │                                        │
│   ┌────────┴───────────────────────┴──────────┐                             │
│   │         行情数据采集服务                      │                             │
│   │         (Market Data Collector)             │                             │
│   └────────────────────┬────────────────────────┘                             │
│                        │                                                    │
│          ┌─────────────┼─────────────┐                                      │
│          ▼             ▼             ▼                                      │
│   ┌────────────┐ ┌──────────┐ ┌──────────────┐                             │
│   │ WebSocket  │ │  REST    │ │  备用数据源    │                             │
│   │ 实时流      │ │  轮询     │ │  (降级方案)   │                             │
│   └────────────┘ └──────────┘ └──────────────┘                             │
│          │             │             │                                      │
│          ▼             ▼             ▼                                      │
│   ┌───────────────────────────────────────────┐                             │
│   │        外部行情数据源 (Market Data APIs)      │                             │
│   │  Polygon.io / Tradier / IBKR / Alpaca     │                             │
│   └───────────────────────────────────────────┘                             │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 三、各层详细设计

### 3.1 数据采集层 (Data Ingestion)

#### 3.1.1 行情数据源选型

期权日内交易对数据源的核心要求是：**低延迟、期权链完整、支持实时流推送**。

| 数据源 | 协议 | 期权支持 | 延迟 | 费用 | 推荐场景 |
|--------|------|----------|------|------|----------|
| **Polygon.io** | WebSocket | ✅ 全链 | ~100ms | $199/月 (Options) | ⭐ 首选：期权实时流最完整 |
| **Tradier** | WebSocket/SSE | ✅ 全链 | ~200ms | 免费 (需券商账户) | 预算有限时的替代方案 |
| **Interactive Brokers (IBKR)** | TWS API | ✅ 全链 | ~50ms | 按交易量减免 | 已有 IBKR 账户时优选 |
| **Alpaca** | WebSocket | ✅ 基础 | ~150ms | 免费(基础)/付费(高级) | 股票为主、期权为辅 |

**推荐组合**：主数据源 **Polygon.io**（期权实时 WebSocket 流）+ 备用 **Tradier / Yahoo Finance**（降级）。

#### 3.1.2 数据采集服务设计

```
MarketDataCollector
├── WebSocketManager          # WebSocket 连接管理（自动重连、心跳、多路复用）
│   ├── connect()             # 建立连接，订阅标的
│   ├── on_message()          # 消息解析 → 标准化 → 写入 Redis + 时序DB
│   ├── heartbeat_monitor()   # 心跳超时检测（5s 无数据 → 告警）
│   └── reconnect()           # 指数退避重连（1s → 2s → 4s ... 最大 30s）
│
├── SubscriptionManager       # 订阅管理
│   ├── subscribe(symbols)    # 动态新增标的订阅
│   ├── unsubscribe(symbols)  # 取消不再关注的标的
│   └── sync_from_strategies()# 从策略配置自动同步需订阅的标的
│
└── DataNormalizer            # 数据标准化
    ├── normalize_quote()     # 统一报价格式（bid/ask/last/volume/timestamp）
    ├── normalize_option()    # 统一期权字段（Greeks, IV, OI, 到期日等）
    └── validate()            # 数据质量校验（价格区间、时间戳合理性）
```

**标准化消息格式 (protobuf / JSON)**：

```json
{
  "type": "option_quote",
  "symbol": "AAPL250321C00230000",
  "underlying": "AAPL",
  "timestamp": 1711036800123,
  "bid": 3.45,
  "ask": 3.50,
  "last": 3.47,
  "volume": 1523,
  "open_interest": 8900,
  "iv": 0.32,
  "greeks": {
    "delta": 0.55,
    "gamma": 0.04,
    "theta": -0.08,
    "vega": 0.15
  },
  "underlying_price": 228.50
}
```

### 3.2 数据层 (Data Layer)

#### 3.2.1 存储架构

采用 **"热-温-冷"三级存储**，平衡实时性与成本：

| 层级 | 存储 | 数据内容 | 保留时长 | 访问延迟 |
|------|------|----------|----------|----------|
| **热** | Redis (Cluster) | 最新行情快照 + Pub/Sub 通道 | 实时 | < 1ms |
| **温** | TimescaleDB / QuestDB | 分钟级K线、技术指标历史、交易日志 | 90 天 | < 10ms |
| **冷** | PostgreSQL | 策略配置、用户信息、通知记录、审计日志 | 永久 | < 50ms |

#### 3.2.2 Redis 数据结构设计

```
# 1. 最新行情快照（Hash）
HSET quote:AAPL last 228.50 bid 228.48 ask 228.52 volume 12345678 ts 1711036800123

# 2. 期权链快照（Hash）
HSET option:AAPL250321C00230000 bid 3.45 ask 3.50 last 3.47 iv 0.32 delta 0.55 ...

# 3. 实时行情发布通道（Pub/Sub）
PUBLISH channel:quote:AAPL "{...json...}"
PUBLISH channel:option:AAPL250321C00230000 "{...json...}"

# 4. 策略配置缓存（Hash，热更新用）
HSET strategy:{id} config "{...json...}" version 3 active true

# 5. 通知去重/限流（带 TTL 的 Key）
SET notify:cooldown:{user_id}:{strategy_id} 1 EX 60   # 同一策略 60s 内不重复通知
```

#### 3.2.3 时序数据库 Schema（以 TimescaleDB 为例）

```sql
-- K线数据（自动压缩为 hypertable）
CREATE TABLE ohlcv (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    open        DECIMAL(12,4),
    high        DECIMAL(12,4),
    low         DECIMAL(12,4),
    close       DECIMAL(12,4),
    volume      BIGINT
);
SELECT create_hypertable('ohlcv', 'time');

-- 技术指标快照
CREATE TABLE indicator_snapshot (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    indicator   TEXT        NOT NULL,    -- 'RSI_14', 'MACD_12_26_9', 'VWAP' ...
    value       JSONB       NOT NULL     -- {"rsi": 72.3} or {"macd": 1.2, "signal": 0.8, "hist": 0.4}
);
SELECT create_hypertable('indicator_snapshot', 'time');
```

### 3.3 应用服务层 (Application Layer)

#### 3.3.1 指标计算引擎 (Indicator Engine)

**职责**：订阅实时行情流，滚动计算各种技术指标，输出到策略匹配引擎。

```
IndicatorEngine
├── IndicatorRegistry          # 指标注册中心
│   ├── register(name, calc_fn, params)
│   └── get_active_indicators()
│
├── 内置指标库
│   ├── MovingAverage           # SMA / EMA / WMA
│   ├── RSI                     # 相对强弱指标
│   ├── MACD                    # 移动平均收敛散度
│   ├── BollingerBands          # 布林带
│   ├── VWAP                    # 成交量加权均价
│   ├── ATR                     # 平均真实波幅
│   ├── OptionIV                # 隐含波动率变化
│   └── OptionGreeks            # Delta/Gamma/Theta/Vega 实时追踪
│
├── StreamProcessor             # 流式增量计算（非全量回算）
│   ├── on_tick(quote)          # 每 tick 触发增量更新
│   └── on_bar_close(bar)       # 每根 K 线收盘触发指标刷新
│
└── OutputPublisher             # 指标结果发布
    ├── publish_to_redis()      # 写入 Redis（供策略引擎消费）
    └── persist_to_tsdb()       # 持久化到时序 DB（供回溯分析）
```

**性能要求**：单标的指标计算 < 5ms / tick，支持并发 100+ 标的。

#### 3.3.2 策略管理服务 (Strategy Manager)

**职责**：提供策略的 CRUD、版本管理、启停控制，支持热更新。

**策略数据模型 (JSON Schema)**：

```json
{
  "strategy_id": "str-001",
  "name": "AAPL 看涨突破",
  "enabled": true,
  "version": 3,
  "created_at": "2025-03-01T10:00:00Z",
  "updated_at": "2025-03-20T14:30:00Z",

  "watchlist": {
    "underlyings": ["AAPL"],
    "option_filters": {
      "type": "call",
      "min_dte": 0,
      "max_dte": 7,
      "moneyness": "ATM",          // ATM / ITM / OTM / 或具体 delta 范围
      "delta_range": [0.40, 0.60],
      "min_volume": 500,
      "min_open_interest": 1000,
      "max_bid_ask_spread_pct": 0.10
    }
  },

  "entry_conditions": {
    "operator": "AND",
    "rules": [
      {
        "indicator": "RSI_14",
        "field": "rsi",
        "comparator": "crosses_above",
        "threshold": 30,
        "timeframe": "5m"
      },
      {
        "indicator": "MACD_12_26_9",
        "field": "histogram",
        "comparator": "turns_positive",
        "timeframe": "5m"
      },
      {
        "indicator": "PRICE",
        "field": "last",
        "comparator": ">",
        "reference": "VWAP",
        "timeframe": "1m"
      }
    ]
  },

  "exit_conditions": {
    "operator": "OR",
    "rules": [
      {
        "type": "take_profit",
        "metric": "option_price_change_pct",
        "threshold": 0.50
      },
      {
        "type": "stop_loss",
        "metric": "option_price_change_pct",
        "threshold": -0.20
      },
      {
        "type": "trailing_stop",
        "metric": "option_price_from_high_pct",
        "threshold": -0.10
      },
      {
        "type": "time_based",
        "exit_before_minutes": 15,
        "reference": "market_close"
      }
    ]
  },

  "notification": {
    "channels": ["telegram"],
    "cooldown_seconds": 60,
    "priority": "high"
  }
}
```

**策略热更新机制**：

```
用户修改策略
     │
     ▼
Strategy Manager API
     │
     ├── 1. 校验策略 JSON Schema
     ├── 2. 写入 PostgreSQL (version + 1)
     ├── 3. 更新 Redis 缓存
     └── 4. 发布 Redis 事件: PUBLISH channel:strategy_update "{strategy_id, version}"
                │
                ▼
       Strategy Matching Engine（监听事件）
                │
                └── 热加载新版本策略，无需重启
```

#### 3.3.3 策略匹配引擎 (Strategy Matching Engine) ⭐ 核心

**职责**：实时将行情数据 + 指标数据与所有活跃策略进行匹配，触发通知。

**架构模式**：**事件驱动 + 规则引擎**

```
                    Redis Pub/Sub
                         │
           ┌─────────────┼──────────────┐
           ▼             ▼              ▼
     quote:AAPL   option:AAPL...  indicator:AAPL
           │             │              │
           └─────────────┼──────────────┘
                         ▼
              ┌──────────────────────┐
              │   Event Router       │
              │   (按标的分发)         │
              └──────────┬───────────┘
                         │
          ┌──────────────┼───────────────┐
          ▼              ▼               ▼
   ┌────────────┐ ┌────────────┐  ┌────────────┐
   │ Worker:AAPL│ │ Worker:TSLA│  │ Worker:SPY │  ...
   │            │ │            │  │            │
   │ 策略列表:   │ │ 策略列表:   │  │ 策略列表:   │
   │ [str-001]  │ │ [str-002]  │  │ [str-003]  │
   │ [str-004]  │ │            │  │ [str-005]  │
   └─────┬──────┘ └─────┬──────┘  └─────┬──────┘
         │              │               │
         ▼              ▼               ▼
   ┌───────────────────────────────────────┐
   │        Rule Evaluation Pipeline       │
   │                                       │
   │  1. 条件预检 (快速跳过不相关事件)        │
   │  2. 状态机更新 (crosses_above 等需历史)  │
   │  3. 条件树求值 (AND/OR 组合)            │
   │  4. 去重/限流检查                       │
   │  5. 触发通知                            │
   └───────────────────────────────────────┘
```

**关键设计点**：

**a) 条件类型与评估方式**

| 比较器 (comparator) | 说明 | 状态依赖 |
|---------------------|------|----------|
| `>`, `<`, `>=`, `<=`, `==` | 简单阈值比较 | 无 |
| `crosses_above` | 从下方穿越阈值 | 需维护前一个值 |
| `crosses_below` | 从上方穿越阈值 | 需维护前一个值 |
| `turns_positive` | 由负转正 | 需维护前一个值 |
| `turns_negative` | 由正转负 | 需维护前一个值 |
| `in_range` | 落入某区间 | 无 |
| `pct_change_from_ref` | 相对参考价变化 % | 需记录参考价 |

**b) 状态机管理**

每个 `(策略, 标的)` 维护一个轻量状态机：

```
states: WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING
```

- `WATCHING`：持续评估 entry_conditions
- `ENTRY_TRIGGERED`：入场条件满足，已发送入场通知，等待用户操作确认或超时
- `HOLDING`：用户确认已建仓，开始评估 exit_conditions
- `EXIT_TRIGGERED`：出场条件满足，已发送出场通知

#### 3.3.4 通知服务 (Notification Service)

**Telegram Bot 通知设计**：

```
通知消息模板（入场信号）：

🟢 入场信号 | AAPL 看涨突破
━━━━━━━━━━━━━━━━━━━━
📌 标的: AAPL ($228.50)
📋 期权: AAPL 03/21 $230 Call
💰 当前报价: $3.45 / $3.50
📊 触发条件:
   ✅ RSI(14) 上穿 30 → 当前 32.5
   ✅ MACD 柱状图转正 → 当前 +0.4
   ✅ 价格 > VWAP → $228.50 > $227.80
⏱ 触发时间: 10:32:15 ET
━━━━━━━━━━━━━━━━━━━━
[已建仓 ✅]  [忽略 ❌]  [暂停策略 ⏸]


通知消息模板（出场信号）：

🔴 出场信号 | AAPL 看涨突破
━━━━━━━━━━━━━━━━━━━━
📌 标的: AAPL ($231.20)
📋 期权: AAPL 03/21 $230 Call
💰 当前报价: $5.10 / $5.15
📊 触发原因: 止盈 (+47.8%)
   入场价: $3.47 → 当前: $5.13
⏱ 持仓时长: 1h 23m
━━━━━━━━━━━━━━━━━━━━
[已平仓 ✅]  [继续持有 🔄]
```

**通知防骚扰机制**：

| 机制 | 说明 |
|------|------|
| **去重** | 同一策略+标的，60s 内不重复发送相同类型通知 |
| **限流** | 单用户每分钟最多 10 条通知（防止极端行情刷屏） |
| **优先级** | high（止损）> medium（入场/止盈）> low（指标预警），高优先级不受限流约束 |
| **静默时段** | 可配置非交易时段不发送低优先级通知 |

### 3.4 用户交互层

#### 3.4.1 Web 管理面板

```
功能模块：

┌─ Dashboard ─────────────────────────────────────┐
│  • 实时 Watchlist（自选股 + 期权报价面板）          │
│  • 活跃策略状态总览（各策略当前状态/最近触发时间）    │
│  • 今日通知记录 Timeline                          │
└─────────────────────────────────────────────────┘

┌─ 策略管理 ──────────────────────────────────────┐
│  • 可视化策略编辑器（拖拽式条件组合）               │
│  • 策略导入/导出 (JSON)                          │
│  • 策略启用/禁用/版本回滚                         │
│  • 策略回测（基于时序 DB 历史数据）                 │
└─────────────────────────────────────────────────┘

┌─ 系统监控 ──────────────────────────────────────┐
│  • 数据源连接状态 & 延迟监控                      │
│  • 策略引擎处理延迟 & 吞吐量                      │
│  • 通知投递成功率 & 延迟                          │
└─────────────────────────────────────────────────┘
```

**技术栈推荐**：React + TailwindCSS + WebSocket（实时数据推送到前端）。

#### 3.4.2 Telegram Bot 交互

除接收通知外，Bot 还支持如下命令式交互：

| 命令 | 功能 |
|------|------|
| `/status` | 查看所有活跃策略状态 |
| `/quote AAPL` | 查询实时股价 |
| `/option AAPL 230 C 0321` | 查询特定期权报价 |
| `/enable str-001` | 启用策略 |
| `/disable str-001` | 禁用策略 |
| `/pause 30m` | 全局静默 30 分钟 |
| `/history today` | 今日所有通知记录 |

---

## 四、数据流全链路

```
                          端到端延迟目标: < 2 秒
                              │
    ┌─────────┐   ~100ms   ┌─┴───────────┐   <1ms   ┌──────────────┐
    │ Polygon │ ─────────► │  Data       │ ───────► │  Redis       │
    │ WS 流   │  WebSocket │  Collector  │  写入     │  Pub/Sub     │
    └─────────┘            └─────────────┘          └──────┬───────┘
                                                          │ <1ms
                                                          ▼
                                                   ┌──────────────┐   <5ms
                                                   │  Indicator   │ ──────┐
                                                   │  Engine      │       │
                                                   └──────────────┘       ▼
                                                                   ┌──────────────┐
                                                                   │  Strategy    │
                                                                   │  Matcher     │
                                                                   └──────┬───────┘
                                                                          │ <5ms
                                                                          ▼
                                                                   ┌──────────────┐
                                                                   │ Notification │
                                                                   │  Service     │
                                                                   └──────┬───────┘
                                                                          │ ~500ms
                                                                          ▼
                                                                   ┌──────────────┐
                                                                   │  Telegram    │
                                                                   │  Bot API     │
                                                                   └──────────────┘
```

---

## 五、技术选型总结

| 模块 | 推荐技术 | 理由 |
|------|----------|------|
| **语言 (核心)** | **Go** 或 **Rust** | 低延迟、高并发、适合实时流处理 |
| **语言 (Web/策略配置)** | **TypeScript (Node.js)** | 前后端统一，快速迭代 |
| **消息中间件** | **Redis Pub/Sub** (小规模) / **NATS** (中规模) | Redis 够用且架构简单；NATS 在需要 at-least-once 语义时更优 |
| **时序数据库** | **TimescaleDB** 或 **QuestDB** | TimescaleDB 兼容 PostgreSQL 生态；QuestDB 写入性能更强 |
| **关系数据库** | **PostgreSQL** | 策略配置、用户管理、审计日志 |
| **缓存** | **Redis Cluster** | 实时行情快照 + 策略缓存 + 去重限流 |
| **Web 前端** | **React** + **TailwindCSS** | 组件生态丰富，适合管理面板 |
| **通知** | **Telegram Bot API** | 免费、延迟低、全球可达、支持 Inline Keyboard 交互 |
| **部署** | **Docker Compose** (单机) / **K8s** (生产) | 容器化便于跨环境部署和扩缩 |
| **监控** | **Prometheus + Grafana** | 系统延迟、吞吐量、数据源状态可视化 |

---

## 六、部署架构

### 6.1 推荐部署方案

考虑到美股行情服务器主要位于美东（纽约），**系统应部署在靠近数据源的地区**以最小化行情获取延迟：

```
                    美东 VPS (AWS us-east-1 / 纽约)
┌──────────────────────────────────────────────────────────┐
│                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │ Data        │  │ Indicator   │  │ Strategy         │  │
│  │ Collector   │  │ Engine      │  │ Matching Engine  │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
│                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │ Redis       │  │ TimescaleDB │  │ PostgreSQL       │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
│                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │ Notification│  │ Telegram    │  │ Web Panel        │  │
│  │ Service     │  │ Bot         │  │ (Nginx)          │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
│                                                          │
│  ┌─────────────┐  ┌─────────────┐                       │
│  │ Prometheus  │  │ Grafana     │                       │
│  └─────────────┘  └─────────────┘                       │
│                                                          │
└──────────────────────────────────────────────────────────┘
           │                      │
    Telegram API           用户 (中国大陆/全球)
    (全球CDN,低延迟)         通过 HTTPS 访问 Web 面板
                             通过 Telegram 接收通知
```

### 6.2 资源估算 (单用户/少量策略场景)

| 资源 | 规格 | 预估月费 |
|------|------|----------|
| VPS | 2 vCPU / 4GB RAM / 80GB SSD | ~$20-40 |
| Polygon.io Options | 实时期权数据订阅 | ~$199 |
| **合计** | | **~$220-240/月** |

---

## 七、高可用与容错

| 风险场景 | 应对方案 |
|----------|----------|
| **数据源 WebSocket 断连** | 指数退避自动重连 + 备用数据源（Tradier / Yahoo Finance REST 轮询降级） |
| **数据源数据延迟/异常** | 实时监控行情时间戳与本地时间差，超阈值告警并标记数据为 stale |
| **策略引擎崩溃** | Supervisor/systemd 自动重启 + 启动时从 Redis/DB 恢复状态 |
| **Redis 宕机** | Redis Sentinel 或持久化 (AOF) + 启动时从 DB 重建缓存 |
| **Telegram API 限流** | 消息队列缓冲 + 指数退避重试 + 短信 / Email 备用通道 |
| **VPS 宕机** | 健康检查 + 自动迁移(云厂商能力) + 每日快照备份 |

---

## 八、安全性考量

| 维度 | 措施 |
|------|------|
| **数据传输** | 所有外部通信 TLS 加密（WebSocket → WSS, HTTP → HTTPS） |
| **API 认证** | Web 面板 JWT + Refresh Token，API Key 管理器 |
| **Telegram Bot** | Webhook + Secret Token 验证，限制 chat_id 白名单 |
| **敏感信息** | 数据源 API Key、DB 密码等通过环境变量 / Vault 管理，不入代码 |
| **访问控制** | 单用户系统默认只允许 owner 操作；多用户扩展时引入 RBAC |

---

## 九、扩展演进路径

### Phase 1 — MVP（2-4 周）
- 数据采集：Polygon.io WebSocket → Redis
- 指标计算：RSI, MACD, VWAP
- 策略匹配：支持简单阈值条件（AND 组合）
- 通知：Telegram Bot 基础通知
- 配置：JSON 文件或简单 REST API

### Phase 2 — 增强（4-8 周）
- Web 管理面板（策略可视化编辑器）
- 复杂条件支持（crosses_above, 嵌套 AND/OR）
- 期权链智能筛选（自动选择最优合约）
- 通知交互（Inline Keyboard 确认建仓/平仓）
- 策略回测框架

### Phase 3 — 高级（8-12 周）
- LLM 辅助策略生成（自然语言 → 策略 JSON）
- 多账户 / 多用户支持
- 盘前/盘后行情支持
- 异常行情检测（突发波动、闪崩预警）
- 可选：券商 API 对接（一键下单，仍需用户确认）

---

## 十、总结

本系统采用 **"数据采集 → 指标计算 → 策略匹配 → 智能通知"** 四级流水线架构，通过 Redis Pub/Sub 实现各环节的低延迟解耦，核心优势在于：

1. **纪律性**：策略以结构化数据固化，消除情绪干扰。
2. **实时性**：端到端延迟 < 2 秒，满足日内交易时效要求。
3. **灵活性**：策略热更新 + 可组合条件树，适应不同交易风格。
4. **可靠性**：多级容错保障交易时段内系统可用性。
5. **渐进式**：MVP 2-4 周可上线，后续持续增强不影响已有功能。
