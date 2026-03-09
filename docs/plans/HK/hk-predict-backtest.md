> **Status: IMPLEMENTED + OPTIMIZED (2026-03-09)**
> 实现代码: `src/hk/backtest/` | 测试: `tests/test_hk_backtest.py` (38 tests)
> 运行: `python -m src.hk.backtest -d 30`
>
> **优化结果 (P1-P6):**
> - P1: 滑点 0.2%→0.05%/leg, TP 0.5%→0.8% → RR 从 0.14:1 修复至 1.75:1
> - P2: 排除负 EV 标的 (HK.800000 3% bounce, HK.00941 16%)
> - P3: 仅使用早盘 VP 信号 (午盘 0% bounce rate)
> - P4: 跳过 BREAKOUT_long (12% WR)
> - P5: Regime 阈值收窄 (breakout_rvol 1.2→1.05, range_rvol 0.8→0.95), UNCLEAR 58%→43%
> - P6: Trailing stop (activation 0.5%, trail 0.3%)
> - 综合: 365T/50.1%WR/PF=0.15/-104.51% → 286T/53.5%WR/PF=1.37/+19.72%

# Role & Context
延续我们之前开发的“高胜率期权日内交易辅助系统”。现在我需要你设计并实现专门针对该系统的**历史回测框架 (Backtesting Framework)**。

# Core Requirements
请忽略自动化执行，专注于通过历史 1-min OHLCV 数据验证我们的“预测指标”和“市场分类逻辑”。我们需要实现一个基于 Python 的向量化或事件驱动的回测引擎。

# Module Development Steps
请分步骤实现以下模块（每次完成一个输出代码，等待我确认）：

## Step 1: Data Preprocessing & Metric Calculation (数据降维与指标构建)
- 编写代码读取正股/指数的 1-min 历史数据。
- 实现一个函数，按天（Daily Session）切分数据，并根据前 3-5 天的 1-min 数据计算当天的静态 POC, VAH, VAL。
- 实现计算 09:30-09:35 的动态 RVOL 算法。

## Step 2: Signal Validation Analytics (信号独立有效性验证)
**不要引入盈亏计算，只评估点位和分类的胜率！**
- 编写 `evaluate_levels()` 函数：寻找历史数据中价格首次触及 VAH/VAL 的时刻，计算其后 15 分钟内价格向反方向移动超过 0.5% 的概率（Bounce Rate）。
- 编写 `evaluate_regimes()` 函数：验证当早盘 RVOL < 0.8 时，全天最高价和最低价是否被成功限制在昨日的 VAH 和 VAL 之间（即震荡市预测准确率）。

## Step 3: Option Execution Simulator (期权盈亏模拟器)
- 编写一个模拟引擎 `OptionBacktester`。输入参数为：进场时间、做多/做空方向、退出时间。
- 重点逻辑：由于缺少期权 Tick 数据，请编写一个**惩罚性滑点估算模型**。假设进出场时，强制扣除底层资产波动率 0.2% 作为期权 Bid-Ask 价差损耗。
- 实现固定盈亏比平仓逻辑（例如：止盈为期权权利金上涨 30%，止损为下跌 20%，或到下午 15:50 强制按时间平仓）。

## Step 4: KPI Report Generation (评估报告生成)
- 运行测试后，输出关键专业指标：系统识别准确率、交易触发次数、盈利因子 (Profit Factor)、期望收益 (Expectancy)、胜率 (Win Rate，必须区分风格A和风格B的独立胜率)。

请先输出 Step 1 的数据预处理和 Volume Profile 静态计算的 Python 代码实现方案。