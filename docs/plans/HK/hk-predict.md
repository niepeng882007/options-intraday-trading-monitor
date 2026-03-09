# Project Context

> **Status: IMPLEMENTED (2026-03-09)**
> 实现代码: `src/hk/` | 配置: `config/hk_settings.yaml`, `config/hk_calendar.yaml`
> 测试: `tests/test_hk.py` (39 tests) | 数据验证: `scripts/hk_data_probe.py` (7/7 PASS)
> 运行: `python -m src.hk`
>
> **更新 (2026-03-09):**
> - Telegram Bot 扩展至 10 个命令: /hk, /hk_playbook, /hk_orderbook, /hk_gamma, /hk_levels, /hk_regime, /hk_quote, /hk_filters, /hk_watchlist, /hk_help
> - 回测优化: 滑点 0.2%→0.05%/leg, TP 0.5%→0.8%, trailing stop, 信号过滤 (exclude_symbols, morning_only, skip_signal_types)
> - 回测结果: 365T/PF=0.15/-104% → 286T/PF=1.37/+19.72%

针对港股市场开发日内行情预测与交易指导功能。该功能不直接进行自动化下单，也不需要实时预测价格涨跌，而是基于富途 API 的港股 LV2 深度行情和期权数据，进行微观市场状态分类（Regime Classification）、精准支撑/阻力计算、交易风格生成，并过滤劣质交易日。最终通过 Telegram Bot 在每日早盘（如 09:35）向我推送量化交易剧本（Playbook）。
目标交易标的：一周左右到期的港股期权（末日/短期期权）。

# Core Requirements & Business Logic
基于富途 API 的港股 LV2 深度行情和期权数据，由于港股和美股的诸多差异，功能和美股完全解耦独立。
## 1. 预测与关键点位计算模块 (Prediction & S/R Calculation)
**禁止使用传统滞后指标（如普通均线、MACD等）！** 请使用以下真实资金微观指标来实现计算逻辑：
*   **成交量分布 (Volume Profile)**：基于过去 3-5 天的 Tick 级或 1 分钟级数据，计算最大成交密集区（POC）、价值区高点（VAH）和价值区低点（VAL），作为系统的核心支撑阻力。
*   **日内动态 VWAP**：计算当日开盘后的成交量加权平均价。
*   **相对成交量 (RVOL)**：计算今日开盘前 30 分钟成交量与过去 10 个交易日同时段平均成交量的比值。
*   **期权痛点与 Gamma 墙**：通过富途 API 获取目标指数/正股的期权链（Option Chain），提取未平仓量（Open Interest）最大的行权价作为“期权阻力/支撑墙”。
*   **LV2 异常盘口监控**：抓取十档买卖盘数据，识别在特定价位是否有显著大于平时的挂单堆积（大单压盘/托底）。

## 2. 交易风格生成模块 (Playbook Generation)
系统需根据上述模块的数据，将今日市场分类，并输出对应的交易策略指导。请在代码中实现以下分类逻辑树：
*   **风格A（单边突破日）**：
    *   触发条件：RVOL > 1.2 且 价格脱离昨日 VAH/VAL 区间。
    *   生成策略：动量风格。建议买入平值（ATM）或轻度虚值（OTM，Delta 0.3-0.5）期权，顺势操作，以 VWAP 为防守线。
*   **风格B（区间震荡日）**：
    *   触发条件：RVOL < 0.8 且 价格在昨日 VAH 和 VAL 之间穿插。
    *   生成策略：均值回归风格。严禁买入虚值期权，建议买入深度实值（ITM，Delta > 0.7）期权。在 VAH 做空，在 VAL 做多，快进快出。
*   **风格C（高波洗盘日）**：
    *   触发条件：IV（隐含波动率）剧烈上升，但价格未能突破关键 Gamma 墙。
    *   生成策略：右侧确认风格。降低仓位，等待带量突破回踩。

## 3. 过滤器模块 (Trade Filter - Risk Management)
系统必须具备“劝退”功能。请实现一个校验函数，若满足以下任一条件，在 TG 推送中打上【🔴 红色警告：今日不宜交易】：
*   当日或次日有重大宏观事件（系统可通过预设的经济日历配置表读取）。
*   内含日收敛（Inside Day）：今日开盘及早盘波动区间完全在昨日最高价与最低价之间，且振幅极小（ATR明显萎缩）。
*   IV极度高估且无明显方向：隐含波动率历史分位点（IV Rank）> 80%，且 RVOL < 1.0。

## 4. Telegram Bot 交互模块 (TG Bot Integration)
*   **定时推送**：每天交易日 09:35 自动触发一次完整计算，并通过 TG 发送结构化的 Markdown 简报。
*   **内容结构**：包含【今日市场定调】、【关键点位(POC/VWAP/LV2异常点)】、【今日交易风格建议】、【交易过滤状态】四个部分。
*   **主动指令**：支持 `/status` (随时获取当前VWAP和LV2快照), `/playbook` (手动重新生成今日剧本)。

