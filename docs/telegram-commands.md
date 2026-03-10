# Telegram Bot Commands Reference

## US Market Commands

US 市场监控命令，由 `TelegramNotifier` 注册（`src/notification/telegram.py`）。

### 市场概览

| 命令 | 参数 | 说明 |
|------|------|------|
| `/status` | — | 系统状态快照：活跃策略、持仓、静默状态 |
| `/market` | — | 全部监控标的实时行情（OHLC、成交量、Bid/Ask） |
| `/chain` | `<symbol> <strike> <C/P> <MMDD>` | 期权链查询，如 `/chain AAPL 230 C 0321` |
| `/conn` | — | Futu 连接诊断（节点、版本、订阅配额） |

### 策略管理

| 命令 | 参数 | 说明 |
|------|------|------|
| `/strategies` | — | 列出所有策略及启用状态 |
| `/enable` | `<strategy_id>` | 启用策略 |
| `/disable` | `<strategy_id>` | 禁用策略 |
| `/pause` | `[minutes]` | 静默通知，默认 30 分钟 |

### 信号操作

| 命令 | 参数 | 说明 |
|------|------|------|
| `/confirm` | `<signal_id> <股票价格>` | 确认建仓（输入底层股票价格，非期权价格） |
| `/skip` | `<signal_id>` | 跳过/忽略信号 |
| `/detail` | `<signal_id>` | 查看信号触发时的完整指标快照 |
| `/history` | — | 今日信号记录（最近 20 条） |

### 测试

| 命令 | 参数 | 说明 |
|------|------|------|
| `/test` | — | 发送模拟入场+出场提醒，验证 Telegram 推送链路 |

---

## HK Market Commands

HK 市场预测命令，由 `register_hk_predictor_handlers()` 注册（`src/hk/telegram.py`）。所有命令支持短别名，在 Telegram 中输入 `/` 可弹出命令菜单。

### 市场概览

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/hk` | — | — | 市场状态快照（连接状态 + 前 3 个标的报价） |
| `/hk_watchlist` | `/hkw` | — | 全部监控列表行情（指数 + 股票分组显示） |
| `/hk_quote` | `/hkq` | `<symbol>` | 单个标的详细报价（OHLC、成交量、Bid/Ask、换手率、振幅） |

### Playbook 与分析

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/hk_playbook` | `/hkpb` | `[symbol]` | 重新生成 Playbook（Regime + 关键点位 + 策略建议） |
| `/hk_levels` | `/hkl` | `[symbol]` | 关键点位：POC / VAH / VAL / VWAP + 价格位置 |
| `/hk_regime` | `/hkr` | `[symbol]` | 当前 Regime 分类（BREAKOUT / RANGE / WHIPSAW / UNCLEAR） |
| `/hk_filters` | `/hkf` | `[symbol]` | 交易过滤状态（成交额、RVOL、Inside Day、日历事件等） |

### 衍生品与盘口

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/hk_gamma` | `/hkg` | `[symbol]` | Gamma Wall：Call Wall / Put Wall / Max Pain（仅指数） |
| `/hk_orderbook` | `/hkob` | `[symbol]` | LV2 盘口快照 + 大单检测 |

### 帮助

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/hk_help` | `/hkh` | — | 显示 HK 全部指令列表与别名 |

### 参数说明

- `[symbol]` — 可选参数，默认为主指数 `HK.800000`
- `<symbol>` — 必填参数，格式如 `HK.00700`、`HK.800000`

### 自动推送

HK 模块会在交易日自动推送以下内容：

| 时间 (HKT) | 类型 | 内容 |
|-------------|------|------|
| 09:35 | Morning Playbook | 开盘后首份 Playbook |
| 10:05 | Confirm Update | 开盘 30 分钟确认更新 |
| 13:05 | Afternoon Playbook | 午后 Playbook |
| 每 60s | Order Book Alert | 交易时段内大单异常警报（09:00-11:59, 13:00-15:59） |

---

## US Playbook Commands

US 每日交易剧本命令，由 `register_us_playbook_commands()` 注册（`src/us_playbook/telegram.py`）。所有命令支持短别名。

### Playbook 与分析

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/us_playbook` | `/uspb` | `[symbol]` | 生成 US Playbook（Regime + VP 关键点位 + Gamma Wall + 策略建议） |
| `/us_levels` | `/usl` | `[symbol]` | 关键点位：POC / VAH / VAL / VWAP / PDH / PDL / Gamma Wall |
| `/us_regime` | `/usr` | `[symbol]` | Regime 分类（GAP_AND_GO / TREND_DAY / FADE_CHOP / UNCLEAR） |
| `/us_filters` | `/usf` | — | 风险过滤状态（FOMC / NFP / CPI / OpEx / Inside Day） |
| `/us_gamma` | `/usg` | `[symbol]` | Gamma Wall：Call Wall / Put Wall / Max Pain |

### 帮助

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/us_help` | `/ush` | — | 显示 US Playbook 全部指令列表与别名 |

### 参数说明

- `[symbol]` — 可选参数，默认为 `SPY`
- Watchlist 标的：SPY, QQQ, AAPL, TSLA, NVDA, META, AMD, AMZN

### 自动推送

US Playbook 模块会在交易日自动推送以下内容：

| 时间 (ET) | 类型 | 内容 |
|------------|------|------|
| 09:45 | ⚠️ 初步 Playbook | 开盘 15 分钟后首份 Playbook（RVOL 阈值更高） |
| 10:15 | ✅ 确认 Playbook | 开盘 45 分钟确认更新（标准 RVOL 阈值） |

---

## Quick Reference

### US 命令速查

```
/status          系统状态
/market          全标的行情
/strategies      策略列表
/enable  <id>    启用策略
/disable <id>    禁用策略
/pause   [min]   静默通知
/confirm <id> <price>  确认建仓
/skip    <id>    跳过信号
/detail  <id>    信号详情
/history         今日记录
/chain   AAPL 230 C 0321  期权链
/conn            连接诊断
/test            推送测试
```

### US Playbook 命令速查

```
/uspb [symbol]   Playbook        = /us_playbook
/usl  [symbol]   关键点位        = /us_levels
/usr  [symbol]   Regime 分类     = /us_regime
/usf             过滤状态        = /us_filters
/usg  [symbol]   Gamma Wall      = /us_gamma
/ush             帮助            = /us_help
```

### HK 命令速查

```
/hk              市场状态
/hkpb [symbol]   Playbook        = /hk_playbook
/hkl  [symbol]   关键点位        = /hk_levels
/hkr  [symbol]   Regime 分类     = /hk_regime
/hkq  <symbol>   详细报价        = /hk_quote
/hkf  [symbol]   过滤状态        = /hk_filters
/hkw             监控列表        = /hk_watchlist
/hkob [symbol]   LV2 盘口        = /hk_orderbook
/hkg  [symbol]   Gamma Wall      = /hk_gamma
/hkh             帮助            = /hk_help
```
