# 架构重构规划 — Options Intraday Trading Monitor v2

> 本文档基于 `docs/playbook_template_v2.md` 规范和代码审计结果，规划从当前架构到 v2 目标架构的重构路径。

---

## 1. 当前架构

### 模块拓扑

```
src/main.py (组合入口)
├── collector/futu.py          ← 共享 FutuCollector
├── us_playbook/main.py        ← USPredictor (on-demand + auto-scan)
│   ├── levels.py              ← PDH/PDL/PMH/PML/VP/GammaWall
│   ├── indicators.py          ← RVOL (窗口式, skip_open=3)
│   ├── regime.py              ← 分类 (GAP_AND_GO/TREND_DAY/FADE_CHOP/UNCLEAR)
│   ├── playbook.py            ← 5段 playbook 生成 + ActionPlan 引擎
│   ├── option_recommend.py    ← 期权推荐 (方向+到期+行权)
│   ├── market_tone.py         ← 市场氛围 A+~D
│   ├── filter.py              ← 日历/Inside Day/IV+RVOL/earnings
│   ├── stabilizer.py          ← 信号稳定器
│   └── telegram.py            ← text handler → handle_query_base
├── hk/main.py                 ← HKPredictor (on-demand only)
│   ├── indicators.py          ← RVOL + IBH/IBL + minutes_to_close_hk()
│   ├── regime.py              ← 5类 regime (GAP_AND_GO/.../WHIPSAW/UNCLEAR)
│   ├── playbook.py            ← 5段 playbook 生成 + ActionPlan
│   ├── option_recommend.py    ← 期权推荐
│   ├── filter.py              ← 日历/Inside Day/IV+RVOL/成交额
│   ├── collector.py           ← HKCollector (同步包装)
│   └── telegram.py            ← text handler → handle_query_base
└── common/                    ← 共享库 (12 文件)
    ├── types.py               ← 10 dataclasses
    ├── action_plan.py         ← ActionPlan + PlanContext + 12 funcs
    ├── volume_profile.py      ← VP (POC/VAH/VAL)
    ├── gamma_wall.py          ← Gamma Wall
    ├── formatting.py          ← 12 playbook 格式化工具
    ├── option_utils.py        ← 期权共享逻辑
    ├── indicators.py          ← VWAP
    ├── trading_days.py        ← 交易日计算
    ├── watchlist.py           ← Watchlist 基类
    ├── telegram_handlers.py   ← handler base funcs
    └── chart.py               ← K线图 + VP 侧边栏
```

### 调用链 (典型 on-demand playbook)

```
Telegram message "AAPL"
→ us_playbook/telegram.py (regex match)
  → common/telegram_handlers.handle_query_base()
    → USPredictor.generate_playbook_for_symbol()
      → _run_analysis_pipeline()
        ├── levels.calculate_levels()
        ├── indicators.calculate_us_rvol()
        ├── regime.classify_us_regime()
        ├── option_recommend.recommend_option()
        └── market_tone.MarketToneEngine.assess()
      → playbook.format_us_message()
        ├── _generate_action_plans()  ← common/action_plan
        └── common/formatting.*
      → common/chart.generate_chart_async()
    → PlaybookResponse(html, chart)
```

### 已知痛点

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| 1 | 市场参数硬编码 (16:00, 390/330, timezone) | playbook.py, indicators.py, regime.py | 无法复用、Early Close 不支持 |
| 2 | RVOL 算法 US/HK 完全分离，无法共享 | us_playbook/indicators.py, hk/indicators.py | 代码重复，参数不同步 |
| 3 | Regime 分类 US/HK 各有一套，部分逻辑重叠 | us_playbook/regime.py, hk/regime.py | 改一个忘另一个 |
| 4 | `minutes_to_close` 在 HK 有函数，US 是内联硬编码 | hk/indicators.py vs us_playbook/playbook.py:1803 | 不一致 |
| 5 | 观望无超时机制 (v2 要求 60min 强制归类) | regime + playbook | 缺失 |
| 6 | 无入场可执行性校验 (v2 要求近端 < 0.3%) | playbook 生成 | 缺失 |
| 7 | 无多空对称对冲方案 (v2 要求 Plan B 反向) | action_plan 生成 | 部分支持 |
| 8 | 止损无下限校验 (v2 要求 > 1.5x 5min ATR) | action_plan | 缺失 |
| 9 | R:R 无异常检查 (v2 要求 > 8:1 警告) | action_plan | 缺失 |
| 10 | 无版本 diff (v2 要求 "vs 上一版") | playbook | 缺失 |
| 11 | 无相对强度计算 (v2 要求个股 vs SPY 相关性) | 无 | 缺失 |
| 12 | RVOL 开盘校正仅 skip，无注释标注 (v2 要求显示校正值) | indicators.py | 部分 |

---

## 2. v2 目标架构

### 核心变化

```
src/config/market.py  ← NEW: MarketConfig (frozen dataclass) + US_CONFIG / HK_CONFIG
                        消除所有硬编码市场参数

src/common/
├── rvol.py           ← NEW: 统一 RVOL 引擎 (skip_open + correction_window + 分段)
├── regime_base.py    ← NEW: RegimeClassifier 基类 (共享阈值逻辑 + 观望超时)
├── relative_strength.py  ← NEW: 个股 vs 基准相关性计算
├── playbook_diff.py  ← NEW: 版本 diff 引擎 ("vs 上一版" 字段)
└── action_plan.py    ← ENHANCED: 止损下限校验 + R:R 异常检查 + 入场可执行性
```

### v2 对照表

| v2 条目 | CLAUDE.md 编号 | 状态 | 对应重构步骤 |
|---------|---------------|------|------------|
| 观望超时 60min | 1 | 缺失 | Step 4: regime_base |
| 入场可执行性 (< 0.3%) | 2 | 缺失 | Step 5: action_plan 增强 |
| 多空对称 (Plan B) | 3 | 部分 | Step 5: action_plan 增强 |
| 止损下限 > 1.5x ATR | 4 | 缺失 | Step 5: action_plan 增强 |
| R:R ≥ 1.5:1, > 8:1 警告 | 5 | 缺失 | Step 5: action_plan 增强 |
| 版本 diff | 6 | 缺失 | Step 6: playbook_diff |
| 相对强度 | 7 | 缺失 | Step 7: relative_strength |
| RVOL 开盘校正标注 | 8 | 部分 | Step 3: rvol 统一 |

---

## 3. 重构顺序

按依赖关系排序。每步独立可测、可部署，不破坏现有功能。

### Step 1: `src/config/market.py` — MarketConfig ✅ 已完成

- **内容**: `MarketConfig` frozen dataclass + `US_CONFIG` / `HK_CONFIG`
- **改动量**: 新文件，不修改消费者
- **v2 条目**: 基础设施（支撑后续所有步骤）
- **验证**: `python -c "from src.config.market import US_CONFIG, HK_CONFIG"`

### Step 2: 接入 MarketConfig

- **内容**: 替换消费者中的硬编码
  - `src/us_playbook/playbook.py:1803` → `US_CONFIG.minutes_to_close(now)`
  - `src/hk/indicators.py:minutes_to_close_hk()` → `HK_CONFIG.minutes_to_close(now)`
  - `src/us_playbook/regime.py` 中的 `390` 常量 → `US_CONFIG.total_session_minutes`
  - `src/common/action_plan.py` PlanContext 默认值 → 由调用方传入 config
- **改动量**: ~8 个文件，每个改 2-5 行
- **v2 条目**: 无直接对应，但消除痛点 #1 #4
- **风险**: 低。纯参数替换，行为不变
- **验证**: `pytest tests/ -v` 全绿

### Step 3: 统一 RVOL 引擎 (`src/common/rvol.py`)

- **内容**: 从 `us_playbook/indicators.py` 和 `hk/indicators.py` 提取共享 RVOL 逻辑
  - 参数化: `skip_open_minutes`, `correction_window`, 分段计算 (HK 上午/下午)
  - RVOL 校正标注: 当 `now` 在 `correction_window` 内时，返回 `(raw_rvol, corrected_rvol, is_corrected)` 三元组
  - 保留各模块的 `compute_rvol_profile()` (自适应阈值逻辑市场差异大)
- **改动量**: 1 新文件 + 2 个 indicators.py 简化
- **v2 条目**: #8 RVOL 开盘校正标注
- **依赖**: Step 2 (MarketConfig 提供 correction_window)
- **验证**: 现有 RVOL 测试 + 新增校正标注测试

### Step 4: Regime 基类 (`src/common/regime_base.py`)

- **内容**: 提取共享 regime 逻辑
  - `RegimeClassifier` 基类: 观望超时 (60min)、regime 转换检测、SPY 上下文注入
  - 子类 `USRegimeClassifier` / `HKRegimeClassifier` 保留市场特有阈值
  - 观望超时: 如果上次 regime = UNCLEAR 且已过 60min，强制基于现有信号归类
- **改动量**: 1 新文件 + 2 个 regime.py 重构
- **v2 条目**: #1 观望超时
- **依赖**: Step 2
- **风险**: 中。Regime 分类是核心逻辑，需回测验证
- **验证**: 回测 `python -m src.us_playbook.backtest -d 30` 结果不退化

### Step 5: ActionPlan 增强

- **内容**: 在 `src/common/action_plan.py` 中增加 v2 校验
  - 止损下限: `validate_stop_loss(sl_distance, atr_5min)` → `> 1.5x ATR` 或标注 `⚠️ 过紧`
  - R:R 校验: `validate_rr(rr)` → `< 1.5` 降级, `> 8.0` 标注异常
  - 入场可执行性: `check_entry_proximity(entry, current, remaining_range)` → 近端/远端/不可达
  - Plan C 生成: 当 Plan A 入场距当前价 > 0.3%，自动生成近端备选
  - Plan B 对称: 确保每份 playbook 含反向对冲方案
- **改动量**: action_plan.py 增加 ~100 行 + playbook.py 调用
- **v2 条目**: #2 #3 #4 #5
- **依赖**: Step 2
- **验证**: 单元测试 + playbook 输出人工审查

### Step 6: 版本 Diff (`src/common/playbook_diff.py`)

- **内容**: Playbook 版本对比引擎
  - 缓存上一版 playbook 的结构化数据 (方向、入场位、日型、置信度)
  - `diff_playbooks(prev, curr)` → "方向从观望→偏多 | 入场位下移 0.3%"
  - 缓存存储: 内存 dict，key = symbol，TTL = 当天
  - 集成到 playbook.py 的 `format_*_message()` 中
- **改动量**: 1 新文件 + 2 个 playbook.py 增加 diff 调用
- **v2 条目**: #6 版本 diff
- **依赖**: 无（独立模块）
- **验证**: 单元测试 (合成 prev/curr 对比)

### Step 7: 相对强度 (`src/common/relative_strength.py`)

- **内容**: 个股 vs 基准指数相关性计算
  - `calculate_relative_strength(stock_bars, benchmark_bars, window=30)` → `RelativeStrength(corr, stock_chg, bench_chg, label)`
  - `label`: "脱钩" (corr < 0.3) / "同步" / "走强" / "走弱"
  - 脱钩时大盘信号自动降权
  - 数据源: FutuCollector 已有 bars 缓存，无额外 API 调用
- **改动量**: 1 新文件 + playbook.py 集成
- **v2 条目**: #7 相对强度
- **依赖**: 无
- **验证**: 单元测试 + 回测中验证信号一致性

### Step 8: Playbook 输出格式升级

- **内容**: 按 `docs/playbook_template_v2.md` 模板调整输出格式
  - 顶部方框 (方向 + 入场 + 目标 + 止损 + R:R)
  - 置信度来源标注
  - 日型切换标注
  - 数据参考区整合相对强度 + RVOL 校正值
- **改动量**: 2 个 playbook.py 重写格式化部分
- **v2 条目**: 模板全面对齐
- **依赖**: Step 3-7 全部完成
- **验证**: 人工审查 + `/review` skill 对照检查

---

## 4. 可抽取逻辑（当前重复或分散）

| 逻辑 | 当前位置 | 目标位置 | 优先级 |
|------|---------|---------|--------|
| `minutes_to_close` | `hk/indicators.py` (函数) + `us_playbook/playbook.py` (内联) | `MarketConfig.minutes_to_close()` | P0 (Step 2) |
| RVOL 基础计算 | `us_playbook/indicators.py` + `hk/indicators.py` | `common/rvol.py` | P1 (Step 3) |
| Regime 观望超时 | 不存在 | `common/regime_base.py` | P1 (Step 4) |
| 止损/R:R 校验 | 不存在 | `common/action_plan.py` 增强 | P1 (Step 5) |
| Playbook 版本 diff | 不存在 | `common/playbook_diff.py` | P2 (Step 6) |
| 相对强度 | 不存在 | `common/relative_strength.py` | P2 (Step 7) |
| 日型常量定义 | `us_playbook/regime.py` + `hk/regime.py` | 各自保留 (差异太大) | 不动 |
| 期权推荐 | `us_playbook/option_recommend.py` + `hk/option_recommend.py` | 各自保留 (策略差异大) | 不动 |

---

## 5. 需新建模块

| 文件 | 用途 | 依赖 | 步骤 |
|------|------|------|------|
| `src/config/__init__.py` | 包初始化 | 无 | Step 1 ✅ |
| `src/config/market.py` | MarketConfig + 预设 | 无 | Step 1 ✅ |
| `src/common/rvol.py` | 统一 RVOL 引擎 | MarketConfig | Step 3 |
| `src/common/regime_base.py` | Regime 基类 + 观望超时 | MarketConfig | Step 4 |
| `src/common/playbook_diff.py` | Playbook 版本对比 | 无 | Step 6 |
| `src/common/relative_strength.py` | 相对强度计算 | 无 | Step 7 |

---

## 6. 不动的部分

以下模块因市场差异大或已经足够独立，不纳入重构范围：

- **期权推荐** (`option_recommend.py`) — US/HK 策略差异大（DTE 选择、Greeks 回退、流动性阈值），共享部分已在 `common/option_utils.py`
- **过滤器** (`filter.py`) — US/HK 日历和规则完全不同，无共享价值
- **回测框架** (`backtest/`) — 各自独立运行，数据源和评估标准不同
- **Telegram handlers** — 已在 `common/telegram_handlers.py` 共享，市场模块仅保留正则和帮助文本
- **自选列表** — 已在 `common/watchlist.py` 共享，市场模块为薄包装器
- **图表** (`common/chart.py`) — 已统一

---

## 7. 风险与缓解

| 风险 | 严重度 | 缓解措施 |
|------|--------|---------|
| Regime 重构导致分类结果变化 | 高 | 回测前后对比 (WR/PF 不退化) |
| ActionPlan 校验过严导致可用方案减少 | 中 | 校验可配置 (严格/宽松模式) |
| RVOL 统一后 HK 分段逻辑丢失 | 中 | HK 分段作为参数传入，不简化 |
| 版本 diff 缓存丢失 (进程重启) | 低 | 仅影响第一次查询，可接受 |

---

## 8. 里程碑

| 阶段 | 包含步骤 | 交付物 |
|------|---------|--------|
| **M1: 基础设施** | Step 1-2 | MarketConfig 落地 + 消费者接入 |
| **M2: 核心引擎** | Step 3-5 | RVOL 统一 + Regime 基类 + ActionPlan v2 |
| **M3: 信号增强** | Step 6-7 | 版本 diff + 相对强度 |
| **M4: 输出格式** | Step 8 | Playbook 模板 v2 全面对齐 |
