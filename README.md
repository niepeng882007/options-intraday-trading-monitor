# Options Intraday Trading Monitor

美股 & 港股期权日内交易实时监控与智能通知系统

## 功能

### 美股监控 (`src/main.py`)

- **数据采集**: 支持 Futu OpenD（主力，毫秒级推送）和 Yahoo Finance（备用）双数据源，获取股票报价、期权链、分钟级 K 线
- **指标计算**: RSI / MACD / EMA / VWAP / ATR / ADX / Bollinger Bands，支持 1m、5m、15m 时间框架
- **策略匹配**: YAML 配置策略，支持 AND/OR 条件组合，crosses_above/turns_positive 等比较器，confirm_bars 多 bar 确认、min_magnitude 幅度过滤
- **状态机管理**: WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING
- **Telegram 通知**: 入场/出场信号推送，支持 Bot 命令交互
- **策略热更新**: watchdog 监听 YAML 文件变更，自动重载无需重启
- **市场环境过滤**: SPY 日跌幅限制、ADX 趋势强度过滤、午间禁交易时段、每日亏损熔断
- **双轨策略体系**: 6 个左侧埋伏策略 + 4 个右侧突破策略（10 个策略，8 活跃 + 2 禁用）
- **回测框架**: 基于历史数据验证策略参数，输出胜率、盈亏比、利润因子、权益曲线

### 美股 Playbook (`src/us_playbook/`)

每日交易剧本生成模块，集成于 `OptionsMonitor`。

- **Volume Profile**: 基于 3 天 1m K 线计算 POC/VAH/VAL，US 专用 tick_size
- **RVOL**: 窗口 RVOL（前 15 分钟对比历史同窗口均量）
- **Regime 分类**: 4 种风格 — 缺口追击(GAP_AND_GO) / 趋势日(TREND_DAY) / 震荡日(FADE_CHOP) / 不明确(UNCLEAR)
- **SPY 市场背景**: SPY 先分类，结果影响个股置信度
- **Playbook 推送**: 每日 09:45 ET（初步，15min RVOL） / 10:15 ET（确认，45min RVOL）
- **Gamma Wall**: 期权链 OI 分析 Call Wall / Put Wall / Max Pain，10s 超时降级
- **关键点位**: VP (POC/VAH/VAL) + PDH/PDL + PMH/PML + VWAP + Gamma Wall 统一展示
- **风险过滤**: FOMC/NFP/CPI 日历、Monthly OpEx 自动检测、Inside Day + 低 RVOL

### 港股预测 (`src/hk/`)

与美股完全解耦的独立模块，面向恒指/恒科期权日内交易。

- **Volume Profile**: 基于 3-5 天 1m K 线计算 POC/VAH/VAL 关键点位
- **VWAP & RVOL**: 日内 VWAP（跨午休连续计算）、相对成交量（按早午盘分 session 对比）
- **Regime 分类**: 4 种市场风格 — 单边突破(A) / 区间震荡(B) / 高波洗盘(C) / 不明确(D)
- **Playbook 推送**: 每日 09:35 / 10:05 / 13:05 HKT 三次 Telegram 推送交易剧本
- **Gamma Wall**: 期权链 OI 分析，计算 Call Wall / Put Wall / Max Pain（仅 HSI/HSTECH 指数）
- **LV2 盘口监控**: 十档盘口异常大单检测，实时告警
- **交易过滤器**: 经济日历、Inside Day、IV+RVOL 错配、最低成交额、末日期权风险

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 Telegram Bot Token 和 Chat ID
```

### 2. Docker Compose 部署

```bash
docker compose up -d
docker compose logs -f monitor
```

### 3. 本地开发

```bash
pip install -r requirements.txt

# 启动美股监控
python -m src.main

# 启动港股预测（独立运行）
python -m src.hk

# 启动美股 Playbook（独立运行）
python -m src.us_playbook
```

### 4. 运行测试

```bash
pip install pytest
pytest tests/ -v
```

### 5. 运行回测（美股）

```bash
python -m src.backtest --all -d 5 -v
```

### 6. HK 数据验证

```bash
# 验证 Futu API 对港股数据的可用性（需 FutuOpenD 运行中）
python scripts/hk_data_probe.py
```

## 策略配置

在 `config/strategies/` 下创建 YAML 文件即可添加美股策略，支持热更新。

港股预测配置在 `config/hk_settings.yaml`（watchlist、regime 阈值、推送时间、simulation 参数）和 `config/hk_calendar.yaml`（经济日历）。`simulation` 配置块包含 TP/SL/滑点、退出模式（fixed/trailing/both）、排除标的和信号过滤。

## Telegram Bot 命令

### 美股

| 命令 | 功能 |
|------|------|
| `/status` | 系统状态 + 策略概览 |
| `/quote AAPL` | 查询实时报价 |
| `/chain AAPL 230 C 0321` | 查期权报价 |
| `/strategies` | 列出所有策略 |
| `/enable <id>` | 启用策略 |
| `/disable <id>` | 禁用策略 |
| `/pause 30` | 静默 30 分钟 |
| `/history` | 今日信号记录 |
| `/confirm <signal_id> <price>` | 确认建仓 |
| `/test` | 发送测试入场/出场提醒，验证推送链路 |
| `/skip <signal_id>` | 跳过信号 |

### 美股 Playbook

| 命令 | 功能 |
|------|------|
| `/us_playbook [symbol]` | 生成 US Playbook（别名: `/uspb`） |
| `/us_levels [symbol]` | 关键点位 VP/PDH/PDL/Gamma（别名: `/usl`） |
| `/us_regime [symbol]` | Regime 分类 + 交易建议（别名: `/usr`） |
| `/us_filters` | 风险过滤状态 FOMC/OpEx/Inside Day（别名: `/usf`） |
| `/us_gamma [symbol]` | Gamma Wall + Max Pain（别名: `/usg`） |
| `/us_help` | US Playbook 指令列表（别名: `/ush`） |

### 港股

| 命令 | 功能 |
|------|------|
| `/hk` | HK 市场状态快照 |
| `/hk_playbook [symbol]` | 手动生成今日 Playbook |
| `/hk_orderbook [symbol]` | LV2 盘口快照 |
| `/hk_gamma [symbol]` | Gamma Wall（仅指数） |
| `/hk_levels [symbol]` | 关键点位 (POC/VAH/VAL/VWAP) |
| `/hk_regime [symbol]` | 当前 Regime 分类与置信度 |
| `/hk_quote <symbol>` | 单个标的详细报价 (OHLC/成交量/买卖盘) |
| `/hk_filters [symbol]` | 交易过滤状态与风险等级 |
| `/hk_watchlist` | 全部监控标的行情总览 |
| `/hk_help` | HK 指令列表 |

### HK 回测

```bash
# 基础回测（30 天，含交易模拟）
python -m src.hk.backtest -d 30

# 指定标的、排除低胜率标的
python -m src.hk.backtest -d 30 --exclude HK.800000 HK.00941

# 自定义退出模式（fixed / trailing / both）
python -m src.hk.backtest -d 30 --exit-mode trailing --trail-activation 0.5 --trail-pct 0.3

# 对比不同滑点参数
python -m src.hk.backtest -d 30 --slippage 0.2   # 旧参数
python -m src.hk.backtest -d 30 --slippage 0.05  # 优化后

# 仅评估 VP 点位和 Regime，不模拟交易
python -m src.hk.backtest -d 20 --no-sim

# JSON/CSV 输出、详细交易日志
python -m src.hk.backtest -d 30 -o json -v
```
