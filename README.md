# Options Intraday Trading Monitor

US Playbook + HK Playbook 的 Telegram 期权交易辅助系统。

## 模块概览

### 合并入口 (`src/main.py`)

- 创建共享 `FutuCollector`，初始化 `USPredictor` + `HKPredictor`
- 单一 Telegram Application 注册两个模块的 handler
- APScheduler 调度 US + HK 自动扫描
- `/kb` 快捷键盘、`/kboff` 关闭键盘
- 优雅关闭 (SIGTERM/SIGINT)

### US Playbook (`src/us_playbook/`)

- 按需生成单标的交易剧本，也可在集成模式或独立模式下运行自动扫描
- 使用多日 `1m` K 线计算 `Volume Profile / PDH / PDL / PMH / PML / VWAP / RVOL`
- Regime 分类为 `GAP_AND_GO / TREND_DAY / FADE_CHOP / UNCLEAR`
- 支持 `SPY / QQQ` 市场背景过滤
- `Gamma Wall` 带 10 秒超时降级
- 输出期权建议时会过滤 `0DTE`，优先 `1-7 DTE`

### HK Playbook (`src/hk/`)

- 港股期权日内分析模块，支持文本触发剧本查询
- 使用多日 `1m` K 线计算 `Volume Profile / VWAP / RVOL / IBH / IBL`
- Regime 分类为 `GAP_AND_GO / TREND_DAY / FADE_CHOP / WHIPSAW / UNCLEAR`
- ActionPlan A/B/C 引擎 + 5 段式剧本格式（对齐 US Playbook）
- 大盘情绪：HSI / HSTECH regime 显示在剧本头部
- 指数支持 `Gamma Wall / Max Pain`
- 集成到 `src/main.py` 时可启用自动扫描；独立运行模式以按需查询为主

## 运行模式

| 模式 | 命令 | 说明 |
| ------ | ------ | ------ |
| 集成模式 | `python -m src.main` | 启动 US Playbook 与 HK Playbook 合并入口；US/HK 自动扫描也在这里调度 |
| US 独立模式 | `python -m src.us_playbook` | 启动 US Predictor，支持文本查询和自动扫描 |
| HK 独立模式 | `python -m src.hk` | 启动 HK Playbook，按需查询模式，不做定时推送 |
| HK 回测 | `python -m src.hk.backtest` | 回测 HK Playbook 的 VP / Regime / 交易模拟 |
| US 回测 | `python -m src.us_playbook.backtest` | 回测 US Playbook 的 VP / Regime / 交易模拟 |

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 启动服务

```bash
# 集成模式
python -m src.main

# US Playbook 独立模式
python -m src.us_playbook

# HK Predictor 独立模式
python -m src.hk
```

### 4. Docker

```bash
docker compose up -d
docker compose logs -f playbook
```

### 5. 测试

```bash
pytest tests/ -v
pytest tests/test_hk.py -v
pytest tests/test_us_playbook.py -v
```

### 6. 回测

```bash
# HK
python -m src.hk.backtest -d 30

# US
python -m src.us_playbook.backtest -d 30
```

## Telegram 交互

### US Playbook 触发词

| 输入 | 功能 |
| ------ | ------ |
| `SPY` / `AAPL` / `TSLA` | 直接生成该标的完整 US Playbook |
| `+AAPL Apple` | 添加标的到 US watchlist，名称可选 |
| `-AAPL` | 从 US watchlist 删除标的 |
| `uswl` | 查看当前 US watchlist |
| `/us_help` | 查看使用说明 |

自动扫描能力：

- 由 `auto_scan` 配置驱动
- 默认扫描窗口为 `09:40-11:30 ET` 与 `13:00-15:00 ET`
- 默认每 `180s` 扫描一次
- 频控规则包括同信号 `30` 分钟冷却、单 session 最多 `2` 次、单日最多 `3` 次

### HK Playbook 触发词

| 输入 | 功能 |
| ------ | ------ |
| `09988` / `HK09988` / `HK.09988` | 直接生成该标的完整 HK Playbook |
| `+09988 阿里巴巴` | 添加标的到 HK watchlist，名称可选 |
| `-09988` | 从 HK watchlist 删除标的 |
| `wl` | 查看当前 HK watchlist |
| `/hk_help` | 查看使用说明 |

自动扫描能力：

- 集成到 `python -m src.main` 时按 `auto_scan` 配置调度
- 默认扫描窗口为 `09:35-12:00 HKT` 与 `13:05-15:45 HKT`
- 默认每 `180s` 扫描一次
- 频控规则包括同信号 `30` 分钟冷却、单 session 最多 `2` 次、单日最多 `3` 次

### 快捷键盘

| 命令 | 功能 |
| ------ | ------ |
| `/kb` 或 `/start` | 显示 US + HK 合并快捷键盘 |
| `/kboff` | 关闭快捷键盘 |

## 动态 Watchlist

US 与 HK 的 watchlist 都不是只读 YAML 配置。

- 首次启动时，US 从 `config/us_playbook_settings.yaml` 初始化到 `data/us_watchlist.json`
- 首次启动时，HK 从 `config/hk_settings.yaml` 初始化到 `data/hk_watchlist.json`
- 之后运行中的增删主要通过 Telegram 文本触发完成，并持久化到 JSON 文件

## 期权建议规则

### US Playbook

- 过滤 `0DTE`，优先选择 `1-7 DTE`
- 在 `GAP_AND_GO / TREND_DAY` 中给出方向性单腿建议
- 在 `FADE_CHOP` 中优先尝试垂直价差
- 使用 `delta 0.30-0.50` 和最小 `OI` 过滤流动性
- 若 Greeks 缺失，会降级为基于 moneyness 的选择
- 若 Regime、过滤器、到期日或期权链条件不足，会返回 `wait`

### HK Playbook

- 过滤当日到期合约
- `GAP_AND_GO / TREND_DAY` 场景偏向方向性单腿
- `FADE_CHOP` 场景优先尝试 `Bull Put Spread / Bear Call Spread`
- 结合 `VWAP`、`Value Area` 和时段做 chase risk 判断
- 若过滤器、流动性、到期日或期权链条件不足，会返回 `wait`

## 配置文件地图

| 文件 | 用途 |
| ------ | ------ |
| `config/us_playbook_settings.yaml` | US Playbook 的 watchlist、VP、RVOL、Regime、auto-scan、期权建议参数 |
| `config/us_calendar.yaml` | US 宏观日历与假期 |
| `config/hk_settings.yaml` | HK 模块的 watchlist、VP、RVOL、Regime、auto-scan、simulation、gamma wall 等 |
| `config/hk_calendar.yaml` | HK 宏观日历与假期 |

## HK 回测示例

```bash
# 基础回测（30 天，含交易模拟）
python -m src.hk.backtest -d 30

# 指定排除标的
python -m src.hk.backtest -d 30 --exclude HK.800000 HK.00941

# 自定义退出模式（fixed / trailing / both）
python -m src.hk.backtest -d 30 --exit-mode trailing --trail-activation 0.5 --trail-pct 0.3

# 仅评估 VP 点位和 Regime，不模拟交易
python -m src.hk.backtest -d 20 --no-sim
```
