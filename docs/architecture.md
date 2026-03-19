# Architecture Overview

Options Intraday Trading Monitor — 异步 Python 系统，通过 Telegram 提供按需 playbook 分析和自动扫描警报。

## 系统架构图

```
┌─────────────────────────────────────────────────────────┐
│                    src/main.py                          │
│  组合入口: FutuCollector + USPredictor + HKPredictor    │
│  单一 Telegram Application + APScheduler (自动扫描)     │
└────────┬──────────────────┬──────────────────┬──────────┘
         │                  │                  │
    ┌────▼────┐      ┌──────▼──────┐    ┌──────▼──────┐
    │Collector│      │ US Playbook │    │ HK Playbook │
    │  (共享)  │      │ src/us_pb/  │    │  src/hk/    │
    └────┬────┘      └──────┬──────┘    └──────┬──────┘
         │                  │                  │
         │           ┌──────▼──────────────────▼──────┐
         │           │        src/common/              │
         │           │  共享模块 (action_plan, types,   │
         │           │  formatting, option_utils, etc.) │
         └───────────┴─────────────────────────────────┘
```

## 模块清单

### 入口层

| 文件 | 职责 |
|------|------|
| `src/main.py` | 组合入口。创建共享 FutuCollector，初始化 US+HK Predictor，注册 Telegram handlers，APScheduler 自动扫描 |
| `src/us_playbook/__main__.py` | 美股独立入口 |
| `src/hk/__main__.py` | 港股独立入口 |

### 数据层

| 模块 | 职责 |
|------|------|
| `src/collector/futu.py` | `FutuCollector` — 共享 Futu API 包装器。异步重试 + 超时 + 看门狗健康检查。`get_snapshot()` / `get_history_bars()` / `get_option_chain()` |
| `src/store/message_archive.py` | Telegram 消息归档到 SQLite |

### 共享公共模块 (`src/common/`)

| 模块 | 职责 |
|------|------|
| `types.py` | 13 个共享 dataclass (VolumeProfileResult, PlaybookResponse, PlaybookSnapshot 等) |
| `action_plan.py` | ActionPlan/PlanContext + 12 个计划函数 (calculate_rr, check_entry_reachability, check_regime_consistency 等) |
| `version_diff.py` | Playbook 快照比较引擎 (extract_snapshot/diff_snapshots) |
| `checklist.py` | 10 项只读质量检查 (观望超时、入场可达、对冲、止损、R:R 等) |
| `volume_profile.py` | Volume Profile (POC/VAH/VAL) |
| `gamma_wall.py` | Gamma Wall 计算 + 格式化 |
| `formatting.py` | 12 个 playbook 格式化工具 |
| `option_utils.py` | 期权分类/推荐/追涨风险评估 |
| `indicators.py` | VWAP + slope + series |
| `trading_days.py` | 交易日计算 (US/HK，跳过假日) |
| `watchlist.py` | 自选列表基类 + JSON 持久化 |
| `telegram_handlers.py` | Telegram handler 基础函数 |
| `chart.py` | 深色主题 K 线图 PNG (关键价位 + VP 侧边栏) |

### 美股模块 (`src/us_playbook/`)

| 模块 | 职责 |
|------|------|
| `main.py` | `USPredictor` 编排器 (按需 + 自动扫描) |
| `regime.py` | 8 类 regime 分类 + 转换检测 (详见下方) |
| `stabilizer.py` | L1 扫描 regime 防抖 (迟滞 + 持续时间 + UNCLEAR 超时) |
| `market_tone.py` | `MarketToneEngine` 市场基调 A+~D 评级 (6 信号 + VIX) |
| `playbook.py` | 5 段式 playbook 生成 (header+预期/核心结论/剧本A-B-C/盘面逻辑/数据雷达) |
| `levels.py` | 关键价位 (PDH/PDL/PMH/PML/VP/Gamma) |
| `indicators.py` | 窗口式 RVOL (自适应阈值) |
| `option_recommend.py` | 方向 + DTE + 行权价选择 |
| `filter.py` | 日历/OpEx/Inside Day/Earnings 过滤 |
| `watchlist.py` | `USWatchlist` 轻量包装器 |
| `telegram.py` | 文本触发 handlers (SPY/AAPL/+code/-code/uswl) |

### 港股模块 (`src/hk/`)

| 模块 | 职责 |
|------|------|
| `main.py` | `HKPredictor` 编排器 (纯按需) |
| `regime.py` | 5 类 regime (GAP_AND_GO/TREND_DAY/FADE_CHOP/WHIPSAW/UNCLEAR) |
| `playbook.py` | 5 段式 playbook + HSI/HSTECH 市场背景 |
| `indicators.py` | RVOL/IBH/IBL/ADR/交易时间 |
| `collector.py` | `HKCollector` 同步 Futu 包装器 |
| `option_recommend.py` | 方向 + 到期日 + 行权价 |
| `filter.py` | 日历/Inside Day/IV+RVOL/成交额/到期风险 |
| `orderbook.py` | 买卖盘警报检测 |
| `watchlist.py` | `HKWatchlist` 轻量包装器 |
| `telegram.py` | 文本触发 handlers (09988/+code/-code/wl) |

### 回测框架

| 路径 | 职责 |
|------|------|
| `src/us_playbook/backtest/` | 价位评估 + regime 评估 + 交易模拟 + Daily Bias Phase 0 |
| `src/hk/backtest/` | 价位评估 + regime 评估 + 交易模拟 |

## US Regime 分类体系

8 种 regime 类型分 4 个族：

```
RegimeFamily.TREND (趋势族)
├── GAP_GO          高 RVOL + PM 突破 / 大缺口
├── TREND_STRONG    高 RVOL + VWAP 支撑 ≥60min / 方向性
└── TREND_WEAK      结构性趋势或持续性趋势

RegimeFamily.FADE (震荡族)
├── RANGE           低 RVOL + VA 内 / 近 Gamma
└── NARROW_GRIND    超低 RVOL (<0.5) + 窄幅 (<ADR*0.5)

RegimeFamily.REVERSAL (反转族)
├── V_REVERSAL      盘中 V 形反转
└── GAP_FILL        缺口回补

RegimeFamily.UNCLEAR
└── UNCLEAR         混合信号 (3 个子类型, 梯度置信度 0.25-0.40)
```

分类优先级：GAP_GO → TREND_STRONG/WEAK → NARROW_GRIND → RANGE → UNCLEAR

## Market Tone 评级系统

`MarketToneEngine` 从 6 个信号计算市场基调：

| 信号 | 数据源 |
|------|--------|
| 宏观日历 | FOMC/NFP/CPI behavior |
| VIX 水平 + 变化 | yfinance (5min 缓存) |
| SPY 缺口 % | snapshot prev_close |
| ORB (开盘区间突破) | SPY 前 30min |
| VWAP 位置 + 斜率 | SPY VWAP |
| 市场宽度代理 | 10 只股票对齐度 (批量 snapshot) |

输出：grade (A+~D) / confidence_modifier (-0.15~+0.10) / position_size_hint / direction / day_type

## 数据流

### 按需查询

```
用户 Telegram 文本 → handler 正则匹配 → Predictor.generate_playbook_for_symbol()
  → FutuCollector (snapshot + bars) → levels + regime + indicators
  → ActionPlan 生成 (A/B/C 方案) → checklist 验证 → version diff
  → playbook 格式化 → chart 生成 → Telegram 回复 (图 + HTML)
```

### 自动扫描 (仅美股)

```
APScheduler 每 180s → L1 轻量筛选 (regime + stabilizer)
  → L2 完整流水线验证 → 频率控制 (cooldown/session/daily 限制)
  → Telegram 推送警报
```

## 配置

| 文件 | 用途 |
|------|------|
| `config/us_playbook_settings.yaml` | 美股参数 (watchlist/VP/RVOL/regime/scan/option) |
| `config/us_calendar.yaml` | 美股宏观日历 (FOMC/NFP/CPI/假日) |
| `config/hk_settings.yaml` | 港股参数 (watchlist/regime/filter/gamma/simulation) |
| `config/hk_calendar.yaml` | 港股经济日历 |
| `.env` | 密钥 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) |

## 关键设计原则

1. **模块隔离**: US (`src/us_playbook/`) 和 HK (`src/hk/`) 绝不互相导入
2. **共享基础**: 通用逻辑提取到 `src/common/`，默认参数匹配 HK，美股传入覆盖值
3. **向后兼容**: `src/hk/` 中保留重导出垫片
4. **时区**: HK 隐式 HKT；US 使用 `ZoneInfo("America/New_York")` (DST 感知)
5. **缓存**: 历史 bars 120s TTL (当天始终刷新)、SPY 上下文 300s、到期日 5min
