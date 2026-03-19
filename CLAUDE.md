# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此代码库中工作时提供指导。

## 项目概述

Options Intraday Trading Monitor — 一个异步 Python 系统，通过 Telegram 提供按需 playbook 分析和自动扫描警报，覆盖美股和港股期权日内交易。

## 常用命令

```bash
python -m src.main              # 运行组合入口 (US + HK Playbook)
python -m src.hk                # 单独运行港股预测器
python -m src.us_playbook       # 单独运行美股预测器
pytest tests/ -v                # 运行所有测试
pytest tests/test_hk.py -v      # 运行单个测试文件
pytest tests/test_us_playbook.py -v  # 运行美股 Playbook 测试

# 港股回测
python -m src.hk.backtest -d 20
python -m src.hk.backtest -d 30 --exclude HK.800000 HK.00941 --exit-mode trailing -v

# 美股回测
python -m src.us_playbook.backtest -d 30
python -m src.us_playbook.backtest -y SPY,AAPL -d 20 --no-sim -v
python -m src.us_playbook.backtest --exit-mode trailing --no-adaptive -o json

# Daily Bias 信号验证 (Phase 0)
python -m src.us_playbook.backtest.daily_bias_eval -d 180 --all-watchlist -v
python -m src.us_playbook.backtest.daily_bias_eval -d 60 -y SPY,AAPL,TSLA -v
python -m src.us_playbook.backtest.daily_bias_eval -d 180 --all-watchlist -o json

docker compose up --build       # Docker 部署
```

## 架构

**`src/main.py`** — 组合入口点。创建共享 `FutuCollector`，初始化 `USPredictor` + `HKPredictor`，单一 Telegram Application 注册两个模块的 handlers，APScheduler 用于自动扫描，`/kb`/`/kboff` 键盘命令。通过 SIGTERM/SIGINT 优雅关闭。

**`src/collector/`** — `FutuCollector`（共享实时数据源）。返回 `StockQuote`、`OptionQuote` 和 bar DataFrames。`yfinance` 用于获取 VIX、盘前数据回退和回测。

**`src/store/`** — `message_archive.py`（Telegram 消息归档到 SQLite）。

### 共享公共模块 (`src/common/`)

从港股和美股模块中提取的共享工具，消除跨模块依赖。两个市场模块均从 `src/common/` 导入，而非互相导入。

- **`types.py`** — 13 个共享 dataclass：`VolumeProfileResult`、`GammaWallResult`、`FilterResult`、`OptionLeg`、`ChaseRiskResult`、`SpreadMetrics`、`OptionRecommendation`、`QuoteSnapshot`、`OptionMarketSnapshot`、`PlaybookResponse`、`DirectionConfidence`、`RelativeStrength`、`PlaybookSnapshot`。港股特有类型（`RegimeType`、`RegimeResult`、`HKKeyLevels`、`Playbook`、`ScanSignal`、`ScanAlertRecord`、`OrderBookAlert`）保留在 `src/hk/__init__.py` 中。
- **`volume_profile.py`** — `calculate_volume_profile()`（POC/VAH/VAL）。`src/hk/volume_profile.py` 为重导出垫片。
- **`gamma_wall.py`** — `calculate_gamma_wall()`、`format_gamma_wall_message()`。`src/hk/gamma_wall.py` 为重导出垫片。
- **`formatting.py`** — 12 个 playbook 格式化工具：`confidence_bar()`、`pct_change()`、`format_percent()`、`split_reason_lines()`、`closest_value_area_edge()`、`action_label()`、`action_plain_language()`、`format_strike()`、`format_leg_line()`、`position_size_text()`、`spread_execution_text()`、`risk_status_text()`。市场特有的格式化函数（`_format_turnover`、`_price_position`、`_regime_reason_lines`）保留在各模块的 `playbook.py` 中。
- **`option_utils.py`** — `classify_moneyness()`、`option_leg_from_row()`、`calculate_spread_metrics()`、`is_positive_ev()`、`recommend_single_leg()`、`recommend_spread()`、`assess_chase_risk()`。默认参数匹配港股值（min_oi=50, chase 2.0/3.5%）；美股调用方传入更严格的覆盖值（min_oi=100, chase 1.5/2.5%）。
- **`indicators.py`** — `calculate_vwap()`、`calculate_vwap_series()`、`calculate_vwap_slope()`。RVOL 保留在各模块中（算法不同）。
- **`action_plan.py`** — `ActionPlan`、`PlanContext` dataclass + 12 个共享计划工具（`calculate_rr`、`reachable_range_pct`、`compact_option_line`、`format_action_plan`、`nearest_levels`、`find_fade_entry_zone`、`cap_tp2`、`check_entry_reachability`、`apply_wait_coherence`、`apply_min_rr_gate`、`check_regime_consistency`）。美股和港股 playbook 均从此处导入。
- **`version_diff.py`** — `extract_snapshot()` / `diff_snapshots()` 冻结 playbook 状态并比较快照（方向、regime、入场价位变化），输出中文 diff 文本。
- **`checklist.py`** — `validate_checklist()` 10 项只读质量检查（观望超时、入场可达、反向对冲、止损下限、TP1 可达、R:R 门槛、版本 diff、RVOL 校正、相对强度、日型归类）。
- **`trading_days.py`** — `previous_trading_day(market, ref_date)`、`trading_day_range(d, market)`（US/HK，跳过周末+假日）。
- **`watchlist.py`** — `Watchlist` 基类，带 `config_parser` 回调。`HKWatchlist` 和 `USWatchlist` 为轻量包装器。
- **`telegram_handlers.py`** — `handle_query_base()`、`handle_add_base()`、`handle_remove_base()`、`handle_watchlist_base()`、`build_combined_keyboard()`。市场模块保留各自的正则模式、帮助文本和 `register_*_handlers()`。
- **`chart.py`** — `generate_chart()` / `generate_chart_async()` 生成深色主题 K 线图 PNG（BytesIO），包含关键价位 + VP 侧边栏。`ChartData` 输入 dataclass。`HKPredictor` 和 `USPredictor` 都从 `generate_playbook_for_symbol()` 返回 `PlaybookResponse(html, chart)`。`handle_query_base()` 先发送图表照片再发送 HTML 文本；图表失败时优雅降级为纯文本。

**向后兼容：** `src/hk/__init__.py` 重导出共享类型，`src/hk/volume_profile.py` 和 `src/hk/gamma_wall.py` 为重导出垫片。旧导入路径（`from src.hk import VolumeProfileResult`）仍可使用。

### 港股 Playbook 模块 (`src/hk/`)

按需港股 playbook 系统，通过共享 Telegram Application 集成。无定时推送 — 纯文本触发的 playbook 生成。

- **核心：** `HKPredictor` 编排器（按需触发，不使用 APScheduler）。`HKCollector` 同步 Futu 包装器，`indicators`（RVOL、交易时间检查、`calculate_initial_balance()` 计算 IBH/IBL、`minutes_to_close_hk()` 用于 330 分钟交易时段、`calculate_avg_daily_range()`、`build_hk_key_levels()`/`hk_key_levels_to_dict()`），`regime`（GAP_AND_GO/TREND_DAY/FADE_CHOP/WHIPSAW/UNCLEAR；已弃用的 BREAKOUT/RANGE 保留用于向后兼容）。
- **关键价位：** `HKKeyLevels` dataclass（POC/VAH/VAL/PDH/PDL/PDC/IBH/IBL/day_open/VWAP/Gamma）。IBH/IBL = Initial Balance（开盘前 30 分钟高低点，替代 PMH/PML）。
- **Playbook：** 使用 `src.common.action_plan` 的 ActionPlan 引擎。5 段式格式：header + 核心结论 + 剧本推演（A/B/C ActionPlans）+ 盘面逻辑 + 数据雷达。市场背景：header 中显示 HSI/HSTECH regime（`_get_market_context_regime`，300s TTL 缓存）。
- **期权推荐：** `option_recommend.py` — 方向由 regime + 价格位置决定，到期日选择（过滤 DTE=0），委托 `src.common.option_utils` 处理单腿/价差/追涨风险，严格观望策略（必须有具体行权价 + 到期日，否则观望）。
- **自选列表：** `watchlist.py` — `HKWatchlist(Watchlist)` 轻量包装器 + `normalize_symbol()`。JSON 持久化（`data/hk_watchlist.json`），`+09988` 添加 / `-09988` 移除 / `wl` 查看。首次运行时回退到 `hk_settings.yaml`。
- **过滤器：** `filter`（5 个过滤器：日历、Inside Day、IV+RVOL、最低成交额、到期风险）。
- **`src/hk/telegram.py`** — 文本触发的 handlers，委托给 `src.common.telegram_handlers` 基础函数。正则模式：`09988`/`HK09988` 查询、`+code` 添加、`-code` 移除、`wl` 列表。`/hk_help` 命令。
- **`src/hk/backtest/`** — 验证 VP 价位（反弹率）和 regime 分类准确性，使用历史数据。交易模拟器支持 fixed/trailing/both 退出模式。通过 `python -m src.hk.backtest` 运行。

**Futu API 注意事项（港股）：** 使用 `get_market_snapshot` 获取买卖盘（非 `get_stock_quote`）。OI 来自 snapshot 的 `option_open_interest`（非 `option_area_type`）。K 线时区为 HKT。

### 美股预测器模块 (`src/us_playbook/`)

按需美股期权交易预测器。通过共享 Telegram Application 集成。无定时推送 — 文本触发的 playbook 生成 + 强信号自动扫描警报。从 `src/common/` 导入共享逻辑（不依赖 `src/hk/`）。

- **核心：** `USPredictor` 编排器（按需 + 自动扫描）。复用共享 `FutuCollector`。`get_snapshot()` 获取报价（无需订阅），`get_history_bars()` 获取多日 1 分钟 bars，`get_premarket_hl()` 获取盘前范围。二进制 bar 缓存（历史缓存 120s TTL，当天始终刷新）。SPY 上下文 300s TTL，按需查询和自动扫描共享。
- **分析：** `levels`（PDH/PDL、PMH/PML、VP 通过 `src.common.volume_profile`、Gamma Wall 通过 `src.common.gamma_wall`），`indicators`（窗口式 RVOL、自适应阈值），`regime`（8 类 regime 分 4 族：TREND 族 [TREND_STRONG/TREND_WEAK/GAP_GO]、FADE 族 [RANGE/NARROW_GRIND]、REVERSAL 族 [V_REVERSAL/GAP_FILL]、UNCLEAR，带 SPY 上下文）。`stabilizer`（L1 扫描防抖：迟滞 + 时间持续 + 60 分钟 UNCLEAR 超时强制归类）。
- **市场基调：** `market_tone.py` — `MarketToneEngine` 通过 6 个信号（宏观日历、VIX、SPY gap、ORB、VWAP、市场宽度）计算 A+~D 评级，输出 `confidence_modifier`（-0.15~+0.10）、`position_size_hint`、`direction`、`day_type`。评级影响 regime 置信度、自动扫描门控（D=跳过、C=仅高 R:R）、playbook Section 0 展示。
- **期权推荐：** `option_recommend.py` — 方向由 regime + 价格位置决定，到期日选择（过滤 0DTE，优选 2-7 DTE 周期权），委托 `src.common.option_utils` 并传入美股覆盖参数（min_oi=100, chase 1.5/2.5%），Greeks 降级处理（delta 不可用时回退到 moneyness）。
- **自选列表：** `watchlist.py` — `USWatchlist(Watchlist)` 轻量包装器 + `normalize_us_symbol()`。JSON 持久化（`data/us_watchlist.json`），`+AAPL` 添加 / `-AAPL` 移除 / `uswl` 查看。首次运行时回退到 `us_playbook_settings.yaml`。
- **自动扫描：** L1 轻量筛选 → L2 完整流水线验证。结构/执行解耦（结构门控推送，期权推荐仅供参考）。3 层频率控制（同信号 30 分钟冷却、每次扫描最多 2 条、每日最多 3 条）+ 覆盖例外。
- **过滤器：** `filter`（FOMC/NFP/CPI 日历、月度 OpEx 自动检测、Inside Day + 低 RVOL）。
- **`src/us_playbook/telegram.py`** — 文本触发的 handlers，委托给 `src.common.telegram_handlers` 基础函数。正则模式：`SPY`/`AAPL` 查询、`+code` 添加、`-code` 移除、`uswl` 列表。`/us_help` 命令。
- **配置：** `config/us_playbook_settings.yaml`（自选列表、VP/RVOL/regime 参数、auto_scan、chase_risk、option_recommend），`config/us_calendar.yaml`（2026 年 FOMC/NFP/CPI/假日）。

**Futu API 注意事项（美股）：** 使用 `get_market_snapshot`（非 `get_stock_quote`）获取美股报价 — 避免订阅要求。期权链 `get_stock_quote` 需要订阅；Gamma Wall 使用 10s 硬超时并优雅回退。`get_option_expiration_dates()` 使用轻量 `get_option_chain()` 调用（仅结构，不含报价/快照），5 分钟 TTL 缓存。

### 美股回测 (`src/us_playbook/backtest/`)

验证 VP 价位（VAH/VAL/PDH/PDL 反弹率）和 regime 分类准确性，使用历史数据。所有 regime 参数严格镜像生产环境 `src/us_playbook/main.py`。

- **`data_loader.py`** — `USDataLoader` 从 Futu 获取 1 分钟 bars，CSV 缓存在 `data/us_backtest_cache/`。复用 `normalize_futu_kline()` 处理 ET 时区。美股交易时间 09:30-16:00（无午休）。
- **`evaluators.py`** — `evaluate_levels()` 测试 VAH/VAL/PDH/PDL 反弹率。`evaluate_regimes()` 镜像生产环境 `classify_us_regime()` 的完整参数（自适应 RVOL、SPY 上下文、PM gap_estimate 回退）。UNCLEAR 不计分（D3: `scorable=False`）。
- **`simulator.py`** — `USTradeSimulator` 支持 fixed/trailing/both 退出模式。EOD 退出时间 15:50 ET。Regime 入场时间 09:38 ET。
- **`engine.py`** — `USBacktestEngine` 链式执行：价位评估 → regime 评估 → 可选模拟。
- **`report.py`** — 3 段报告：价位准确度（VAH/VAL/PDH/PDL）、Regime 准确度（D3 评分）、交易模拟。
- **`daily_bias_eval.py`** — Phase 0 每日偏向信号验证。`DailyBiasEvaluator` 针对双标签测试 5 个子信号（日线结构 HH/HL、昨日 K 线、成交量修正、小时 EMA 交叉、ATR 归一化缺口）（Label A：原始方向 close-vs-open/VWAP；Label B：regime 对齐的 P&L 符号，通过 `evaluate_regimes()` + `USTradeSimulator`）。每个信号的参数扫描（structure windows [5,8,10,15]、candle body_ratio [0.3-0.7]、EMA pairs [8/21,13/34,20/50]、gap ATR multipliers [0.2,0.3,0.5]）。分析：按参数胜率 + 二项 p 值、Pearson/Spearman 相关矩阵、5 种聚合权重方案、置信度灵敏度（±modifier 对扫描/观望阈值）、时段分析（AM1/AM2/PM 探索性）、VIX 分层（低/中/高，通过 yfinance 历史数据）。6 个 Go/No-Go 标准（G1-G6），PASS/FAIL/INCONCLUSIVE 判定。CLI：`python -m src.us_playbook.backtest.daily_bias_eval`。
- **配置：** `config/us_playbook_settings.yaml` 中的 `simulation` 块（tp 0.5%、sl 0.25%、slippage 0.03%/腿、trailing 退出）。

**Futu `INTERVAL_MAP`：** 支持 `1m`、`5m`、`15m`、`1d`。未知间隔抛出 `ValueError`（无静默回退）。`_fetch_history_bars` 使用动态 `max_count`（日线：`days+10`，分钟线：`(days+3)*400`）。

## 港股配置

- `config/hk_settings.yaml` — 港股初始自选列表（指数 + 股票，运行时通过 `data/hk_watchlist.json` 管理）、regime 阈值（`gap_and_go_gap_pct`、`trend_day_rvol`、`fade_chop_rvol`、`ib_window_minutes` + 旧版 `breakout_rvol`/`range_rvol`）、`market_context`（hsi_symbol、hstech_symbol、context_ttl_seconds）、过滤器参数（最低成交额）、gamma wall 设置、`simulation` 块（tp/sl/slippage、exit_mode、trailing 参数、exclude_symbols、skip_signal_types）。
- `config/hk_calendar.yaml` — 经济日历（FOMC、HKMA、中国 PMI/GDP、港股假日、HSI 期权到期日）。手动维护。

## 美股配置

- `config/us_playbook_settings.yaml` — 美股自选列表（SPY/QQQ/AAPL/TSLA/NVDA/META/AMD/AMZN，运行时通过 `data/us_watchlist.json` 管理）、VP lookback（5 天）、RVOL 参数（skip_open 3 分钟、lookback 10 天）、regime 阈值（adaptive 启用、gap_and_go 1.5、trend_day 1.2、trend_strong 1.8、fade_chop 1.0）、市场上下文标的、Gamma Wall 开关、auto_scan（间隔 180s、breakout/range_reversal 配置、cooldown/override）、chase_risk 阈值、option_recommend（dte_min 1、dte_preferred_max 7、delta 0.30-0.50、min_oi 100）、hist_cache_ttl 120s、`simulation` 块（tp/sl/slippage、exit_mode、trailing 参数、exclude_symbols、skip_signal_types）。
- `config/us_calendar.yaml` — 2026 年美股宏观日历（FOMC/NFP/CPI/假日）。月度 OpEx 自动计算。

## 关键约定

- Python 3.11，全程异步（asyncio + APScheduler）
- 依赖项在 `requirements.txt` 中（无 pyproject.toml）
- 配置在 `config/us_playbook_settings.yaml`（美股）和 `config/hk_settings.yaml`（港股），密钥通过 `.env`（TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID）
- 测试使用 pytest + 合成 bar 数据辅助函数
- 共享逻辑放在 `src/common/` — 市场专属模块（`src/hk/`、`src/us_playbook/`）从 common 导入，绝不互相导入
- `src/hk/` 中的重导出垫片保持向后兼容（如 `from src.hk import VolumeProfileResult` 仍可使用）
- 添加共享功能时放入 `src/common/` 并设置合理默认值；市场模块按需传入覆盖参数

## 重构目标 (v2) — 已完成

本次重构基于 playbook_template_v2.md 规范，核心改进（均已实现）:

1. ✅ 观望超时机制: "不明确" 最多持续 60 分钟后强制归类（`stabilizer.py` UNCLEAR timeout + `checklist.py` #1/#10）
2. ✅ 入场可执行性: 必须包含近端方案（`check_entry_reachability()` + `checklist.py` #2）
3. ✅ 多空对称: 主方案 + 反向对冲方案（`checklist.py` #3 检查）
4. ✅ 止损下限: > 1.5x 5min ATR（ATR-based stop loss + `checklist.py` #4）
5. ✅ R:R 校验: ≥ 1.5:1（`apply_min_rr_gate()` + `checklist.py` #6）
6. ✅ 版本 diff: 每次更新标注变化（`version_diff.py` + `checklist.py` #7）
7. ✅ 相对强度: 个股 vs SPY 相关性（`checklist.py` #9，指数豁免）
8. ✅ RVOL 开盘校正: 09:35 前警告（`checklist.py` #8，HK 豁免）

## 开发规范

- black + ruff 格式化
- pytest，引擎逻辑 100% 覆盖
- 中文 commit: "feat/fix/refactor: 描述"

## 关键命令

## 重要参考文件

- docs/playbook_template_v2.md  — 输出模板规范（所有输出必须符合
- docs/market_config.md         — 市场参数配置说明
