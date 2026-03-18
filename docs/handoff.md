# 方向判断与入场计算重构 — 交接文档

> 日期: 2026-03-18
> 前序: US 日型判断引擎重构 (4 → 7+1) — 已合入 main

---

## 本次完成了什么

### 7 个 Phase 全部落地

9 个文件修改，519 测试通过（+34 新增），0 新回归。

#### Phase 1: 数据结构扩展

| 文件 | 新增 |
|------|------|
| `src/common/types.py` | `DirectionConfidence` dataclass（方向 + score + 6 信号 map）、`RelativeStrength` dataclass（rs_ratio, correlation, decoupled, label） |
| `src/common/action_plan.py` | `ActionPlan` +`reachability_tag`/`is_near_entry`；`PlanContext` +`current_price`/`decoupled_from_benchmark` |
| `src/us_playbook/__init__.py` | `USPlaybookResult` +`relative_strength`/`direction_confidence` |

#### Phase 2: 相对强度 (R4)

- **`src/common/indicators.py`**: `compute_relative_strength(stock_bars, spy_bars)` — 日内回报对比、滚动 30-bar 相关性、脱钩判断（|corr| < 0.40）
- **`src/us_playbook/main.py`**: pipeline 步骤 13 集成，跳过 SPY 自身
- **`config/us_playbook_settings.yaml`**: `relative_strength` 配置块（enabled, correlation_window, decouple_threshold）

#### Phase 3: 方向置信度 (R1)

- **`src/us_playbook/playbook.py`**: `_compute_direction_confidence()` — 聚合 6 个信号（regime, vwap_position, vwap_slope, structure, market_tone, relative_strength）
- Section 2 核心结论区显示 `▸ 方向置信: 做多 83% (regime, vwap_position, structure, ...)`

#### Phase 4: 入场可达性 (R2)

- **`check_entry_reachability()`**: 二元→三档（""=近端 / "远端" / "⛔不可达"=demote）
  - 近端阈值: `reachable × 0.3`；远端: `≤ reachable`；不可达: `> reachable`
- **`generate_near_entry_plan()`**: 在当前价 0.3% 内找结构位生成完整 Plan C（entry/SL/TP/RR）
- **`ensure_near_entry_exists()`**: 无近端入场方案时自动注入，替换旧 Plan C
- **`format_action_plan()`**: 支持 `reachability_tag` 渲染（📍远端）、近端 Plan C 完整渲染（非简化格式）
- **`apply_min_rr_gate()`**: 有 entry 的 Plan C 参与 R:R 门控（F6）

#### Phase 5a: Plan B 反向对冲

所有 `_plans_*` 的 Plan B 从**同方向加仓**改为**反向对冲**:

| 函数 | 旧 Plan B | 新 Plan B |
|------|-----------|-----------|
| `_plans_trend_bullish` | 突破加仓 (bullish) | 反向对冲做空 (bearish) |
| `_plans_trend_bearish` | 破位加仓 (bearish) | 反向对冲做多 (bullish) |
| `_plans_fade_bearish` | VWAP 回归做空 (bearish) | 反向对冲做多 (bullish) |
| `_plans_fade_bullish` | VWAP 回归做多 (bullish) | 反向对冲做空 (bearish) |
| `_build_wide_va_plans_bearish` | VWAP 回归做空 | 反向对冲做多 |
| `_build_wide_va_plans_bullish` | VWAP 回归做多 | 反向对冲做空 |

UNCLEAR plans 的 Plan B 保持不变（观察/均值回归/轻仓试探语义不变）。

#### Phase 5b: 近端入场 + 失效 section

- `_generate_action_plans()` 后处理链头部新增 `ensure_near_entry_exists`（在 cap/gate 之前，注入的 Plan C 经过完整检查）
- 新增 `_invalidation_text()` + "🔄 失效与切换" section（从旧 Plan C 逻辑提取，按 regime family 生成失效条件文本）

#### Phase 5c: 共享后处理适配

- **`enforce_direction_consistency()`**: Plan B 豁免——反向对冲 Plan B 不再被 strip（F1）
- **`apply_wait_coherence()`**: 对冲 Plan B 不被 suppress——观望时对冲反而更重要（F5）
- **`apply_market_direction_warning()`**: `decoupled_from_benchmark=True` 时警告降级为 "脱钩, 权重降低"

#### Phase 6: 集成显示

- `PlanContext` 构造传入 `current_price` + `decoupled_from_benchmark`
- Section 4 新增 `▸ 相对强度: vs SPY: 强势 | 个股 +1.2% / SPY +0.3% | 相关性 0.72`
- Section 4 新增 `▸ 预估剩余波动: 1.2% (还剩 180min)`

#### Phase 7: 测试 (34 新增)

| 测试文件 | 新增类/用例数 | 覆盖内容 |
|----------|---------------|----------|
| `test_common_action_plan.py` | +15 | Plan C 完整渲染、reachability_tag、三档可达性、近端生成、近端注入/不注入、enforce 豁免、wait coherence 豁免、脱钩降级 |
| `test_us_playbook.py` | +19 | 方向置信度、相对强度、Plan B 对冲结构（4 regime）、失效 section（trend/unclear）、端到端三方案结构 |

### 关键文件清单

| 文件 | 改动性质 |
|------|----------|
| `src/common/types.py` | +2 dataclass |
| `src/common/indicators.py` | +`compute_relative_strength()` |
| `src/common/action_plan.py` | dataclass 扩展 + 5 函数修改 + 2 函数新增 + 格式化适配 |
| `src/us_playbook/__init__.py` | re-export + USPlaybookResult +2 字段 |
| `src/us_playbook/playbook.py` | `_compute_direction_confidence()` + 6 个 `_plans_*` 重构 + `_invalidation_text()` + 显示集成 |
| `src/us_playbook/main.py` | pipeline +relative_strength 集成 |
| `config/us_playbook_settings.yaml` | +`relative_strength` 配置块 |
| `tests/test_common_action_plan.py` | +15 测试 |
| `tests/test_us_playbook.py` | +19 测试 |

---

## 下次应该从哪里开始

### 1. 回测验证（最高优先级）

```bash
python -m src.us_playbook.backtest -d 20 -v
```

确认:
- Plan B 对冲方案在回测 simulator 中的表现（当前 simulator 只跟 Plan A 入场，需要确认是否需要扩展 simulator 支持多 plan 评估）
- 近端 Plan C 注入频率是否合理（过高说明 Plan A/B 入场位经常远离当前价）
- 总体准确率 ≥ 旧系统

### 2. 实盘观察

手动触发 playbook，确认:
- `▸ 方向置信: 做多 67% (regime, vwap_position, structure)` 显示正常
- `▸ 相对强度: vs SPY: 强势 | ...` 显示正常（SPY 自身不显示）
- `🔄 失效与切换:` section 内容与 regime 一致
- 📍远端 标签出现在合理位置
- Plan B 名称显示"反向对冲做空/做多"

### 3. HK 模块 Plan B 对齐（独立任务）

本次仅改 US，HK 的 Plan B 仍是同方向"突破加仓"。审查报告 F11 建议作为后续独立任务处理。需要评估 HK 模块是否也需要反向对冲语义。

### 4. Playbook 版本 diff

审查报告提及的 "每次更新标注与上一版的变化"（模板 v2 要求）。`_regime_history` 已存储数据，但 playbook 格式化中尚未读取和展示 "vs 上一版" 字段。

### 5. `direction_confidence` 回填到 USPlaybookResult

当前 `_compute_direction_confidence()` 在格式化时计算并显示，但未回填到 `result.direction_confidence` 字段。如果 auto-scan 或其他下游需要用方向置信度做决策，需要在 `_run_analysis_pipeline()` 或 `format_us_playbook_message()` 中回填。

---

## 未解决的问题

### 审查报告遗留

1. **F11: HK 模块 Plan B 语义未同步**。HK 的 Plan B 仍是同方向加仓。模板 v2 是通用模板，两个市场 Plan B 含义不一致。声明为后续独立任务。

2. **F12: 方向置信度 ≠ regime 置信度**。`regime.confidence = 0.65` 表示 regime 分类信心，`DirectionConfidence.score` 表示做多/做空信号对齐度。docstring 已区分，但 playbook 输出中两个百分比挨着显示，可能让用户混淆。考虑在 UI 中加注释或调整布局。

3. **F13: HK `check_entry_proximity` 与近端 Plan C 潜在冲突**。HK 的 `check_entry_proximity` 会 demote entry < 0.5% 的方案。若 HK 未来采用近端 Plan C（entry 在 0.3% 以内），会冲突。当前无需操作。

4. **F1 底层问题: HK `enforce_direction_consistency` 实际无效**。HK 传入的 regime name 是 `"GAP_AND_GO"` / `"TREND_DAY"`，不在默认 `trend_regimes = {"GAP_GO", "TREND_STRONG", "TREND_WEAK"}` 中，所以该函数对 HK 是死代码路径。本次 Plan B 豁免不影响 HK，但这是一个应记录的潜在 bug。

### 代码层面

5. **预先存在的测试失败（与本次无关）**:
   - `test_build_telegram_application_wires_dual_requests` — 本地 proxy 配置导致 kwargs 不匹配
   - `TestFadeEntryStaleness::test_recommend_moderate_still_tradeable` — expiry_dates 环境问题
   - `test_hk.py` 5 个失败 — HK 模块预存问题
   - `test_collector_futu.py` 4 个失败 — Futu API mock 问题

6. **`direction_confidence` 未持久化**。仅在格式化时计算，不参与 auto-scan 决策。如需用于 L1/L2 screen，需提前计算并存入 `USPlaybookResult.direction_confidence`。

7. **`relative_strength` 依赖 SPY today_bars 缓存**。首次查询非 SPY 标的时，如果 SPY 尚未被查询（`_last_today_bars` 中无 SPY），相对强度为 None。正常使用流程中 SPY 通常先于个股被查询（作为 market context），但边缘情况下可能缺失。

8. **近端 Plan C 可能与旧 Plan C 失效信息冲突**。`ensure_near_entry_exists` 替换旧 Plan C 时，旧 Plan C 的失效/切换逻辑（如"跌回 VA → 转 RANGE"）被丢弃。但新的"失效与切换" section 已独立生成此信息，所以不存在信息丢失——只是从 Plan C 迁移到了独立 section。

### 性能层面

9. **`compute_relative_strength` 每次 pipeline 调用一次**。涉及 pct_change + corrcoef 计算，对 60-bar 数据开销极小（<1ms）。无需缓存。

10. **`_compute_direction_confidence` 在 format 中调用**。同一个 result 的 playbook 如果被多次格式化（如 auto-scan 重新渲染），会重复计算。开销极小，但如需优化可缓存到 `USPlaybookResult.direction_confidence`。
