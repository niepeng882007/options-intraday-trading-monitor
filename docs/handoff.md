# 止损与目标计算重构 — 交接文档

> 日期: 2026-03-18
> 前序: 方向判断与入场计算重构 — 已合入 main

---

## 本次完成了什么

### 止损/目标/R:R 校验体系重构

9 个文件修改，79 action_plan 测试通过（+29 新增），977 全量回归通过，0 新回归。

**根本问题**: TSLA 止损仅 0.16%（远低于 5min ATR）、R:R 1:10（超出剩余波动）、SPY Plan A R:R 0.5 仍为首选。原因是止损/目标仅从结构位选取，未校验 ATR 噪音和目标可达性；R:R 是简单距离比，未考虑止损触发概率和目标达成概率。

#### Step 0: Bug fix — warning 覆盖问题

- **`cap_tp1()`** (action_plan.py:523): `plan.warning = ...` → 追加模式 `plan.warning = f"{plan.warning}; {w}" if plan.warning else w`
- **`apply_vwap_deviation_warning()`** (action_plan.py:701/704): 同上，两处 warning 赋值改为追加

#### Step 1: 5 分钟 ATR 计算

- **`src/common/indicators.py`**: +`calculate_atr_5min(today_bars, period=14)` — 1min→5min resample, True Range, 取最近 period 根均值，返回绝对值。数据不足返回 0.0

#### Step 2: 数据结构扩展

| 文件 | 新增字段 |
|------|----------|
| `src/common/action_plan.py` ActionPlan | `effective_rr: float`, `stop_atr_multiple: float`, `stop_floor_applied: bool` |
| `src/common/action_plan.py` PlanContext | `atr_5min: float` |
| `src/us_playbook/__init__.py` USPlaybookResult | `atr_5min: float` |
| `src/hk/__init__.py` Playbook | `atr_5min: float` |

概率常量（模块级，待回测校准）:
```python
_STOP_PROB_TIGHT = 0.80    # stop < 0.5x ATR
_STOP_PROB_NARROW = 0.65   # stop < 1.0x ATR
_STOP_PROB_DEFAULT = 0.40  # stop >= 1.0x ATR
_TP_PROB_DEFAULT = 0.50    # tp <= remaining_vol
_TP_PROB_STRETCH = 0.25    # tp > remaining_vol
_TP_PROB_EXTREME = 0.10    # tp > 1.5x remaining_vol
```

#### Step 3: `enforce_stop_floor()`

止损下限 = `max(1.5 × atr_5min, avg_daily_range_abs × 5%)`。过紧则自动扩大，设置 `stop_floor_applied=True`，计算 `stop_atr_multiple`。

#### Step 4: `validate_target_reachability()`

- Tier 1: TP1 > remaining_vol → 追加 warning
- Tier 2: TP1 > remaining_vol × 1.5 → demote（不 force-adjust，因为 cap_tp1 已尝试过替换）

#### Step 5: `compute_effective_rr()`

概率加权 R:R = `(tp_reach_prob × tp_distance) / (stop_trigger_prob × stop_distance)`。基于 ATR 倍数选择止损触发概率，基于剩余波动选择目标达成概率。

#### Step 6: `check_all_demoted()`

所有有 entry 的方案均被 demote/suppress → 在第一个方案上追加 "所有方案有效R:R不足或被降级, 建议观望"。

#### Step 7: `apply_min_rr_gate()` 增强

在原有 rr_ratio 检查之后新增:
- `effective_rr > 0 且 < 1.5` → demote
- `effective_rr > 8.0` → 追加 "极端R:R" warning

#### Step 8: `format_action_plan()` 增强

- 止损行: `止损: 99.70 (test) | 2.5x ATR ✓` （< 1.5x 显示 ⚠️，`stop_floor_applied` 显示 `[已扩大]`）
- R:R 行: `R:R ≈ 1:3.0 (有效 1:1.8)` （effective_rr 与 rr_ratio 差异 > 0.05 时显示）

#### Step 9-10: US/HK 集成

**Pipeline**: 两个市场的 `_run_analysis_pipeline()` 均调用 `calculate_atr_5min(today_bars)` 并存入 result。

**后处理链** (US 和 HK 各两处 — 主链 + issue4 降级链):
```
ensure_near_entry_exists
→ enforce_stop_floor (★)
→ cap_tp1 → cap_tp2
→ validate_target_reachability (★)
→ compute_effective_rr (★)
→ check_entry_reachability
→ apply_vwap/gamma warnings
→ apply_wait_coherence → apply_min_rr_gate
→ enforce_direction → apply_market_direction
→ check_all_demoted (★)
```

**数据雷达**: US 显示 `▸ 波动: 5min ATR $0.39`，HK 显示 `▸ 波动: 5min ATR HK$0.39`

#### Step 11: 测试 (29 新增)

| 测试类 | 用例数 | 覆盖 |
|--------|--------|------|
| TestCalculateAtr5min | 3 | 正常/空/不足 |
| TestEnforceStopFloor | 5 | 扩大/不变/无ATR/bearish/倍数 |
| TestValidateTargetReachability | 4 | 范围内/warn/demote/无ADR |
| TestComputeEffectiveRR | 5 | 正常/紧止损/远目标/无ATR/无entry |
| TestApplyMinRRGateEffectiveRR | 3 | <1.5→demote / >8→warn / OK |
| TestCheckAllDemoted | 3 | 全demote/部分/无entry |
| TestFormatActionPlanATR | 4 | ATR倍数/低倍数/有效RR/已扩大 |
| TestWarningAppend | 2 | cap_tp1/vwap追加不覆盖 |

---

## 下次应该从哪里开始

### 1. 概率常量校准（最高优先级）

当前 `_STOP_PROB_*` 和 `_TP_PROB_*` 是经验初始值，需要通过回测校准:

```bash
python -m src.us_playbook.backtest -d 30 -v
python -m src.hk.backtest -d 20 -v
```

校准方法: 在回测 simulator 中统计:
- 不同 ATR 倍数下的实际止损触发率（对比 `_STOP_PROB_TIGHT/NARROW/DEFAULT`）
- 不同 remaining_vol 比例下的 TP1 达成率（对比 `_TP_PROB_DEFAULT/STRETCH/EXTREME`）

如果实际数据偏离较大，调整常量值。这些常量位于 `src/common/action_plan.py` 模块级。

### 2. 实盘验证

手动触发 playbook，确认:
- TSLA 止损不再出现 0.16%（应至少 1.5x ATR，如 ATR=0.39 则止损 ≥ $0.59）
- R:R 极端值 (>8) 有 ⚠️ 标注
- 有效 R:R < 1.5 的方案被标记为降级
- 所有方案降级时显示观望提示
- 止损行显示 `x.xx ATR ✓/⚠️` 标注
- `[已扩大]` 标签出现在止损被 floor 调整的方案上

### 3. effective_rr 阈值调优

当前 effective_rr < 1.5 直接 demote，可能过于激进（特别是震荡日 Plan A 经常 R:R 在 1.0-1.5 之间）。观察实盘后决定是否:
- 将 1.5 改为可配置参数（放入 `us_playbook_settings.yaml`）
- 或按 regime family 分层（trend: 1.5, fade: 1.0, unclear: 0.8）

### 4. 上一轮遗留任务

参见上一轮交接中的遗留:
- HK 模块 Plan B 语义对齐（F11）
- Playbook 版本 diff（模板 v2 要求）
- `direction_confidence` 回填到 USPlaybookResult

---

## 未解决的问题

### 设计层面

1. **概率常量未校准**。`_STOP_PROB_*` 和 `_TP_PROB_*` 是经验值（0.80/0.65/0.40 和 0.50/0.25/0.10），未通过回测数据验证。实际触发/达成率可能偏离。应在回测框架中增加统计模块。

2. **effective_rr demote 阈值 1.5 可能过严**。震荡日的 Plan A 经常 R:R 在 1.0-1.5 之间（VA 边沿到 POC 距离有限），可能被过度 demote。需要实盘观察后决定是否分层。

3. **enforce_stop_floor 不追加 stop_loss_reason**。计划设计决定由 format 层通过 `stop_atr_multiple + stop_floor_applied` 统一展示，但用户可能困惑为什么止损位和 reason 不匹配。可考虑在 reason 后追加 "(ATR floor)" 标注。

4. **cap_tp1 与 validate_target_reachability 的顺序交互**。cap_tp1 先尝试找结构位替换 TP1，如果找不到则 warn；随后 validate_target_reachability 可能再次 warn 或 demote。两层 warn 可能叠加（如 "TP1 距入场 3.2%, 超出预估波动 2.0%; ⚠️ TP1 超出剩余空间 (3.2% > 2.0%)"）。信息重复但不矛盾，可接受但略冗余。

### 代码层面

5. **预先存在的测试失败（与本次无关，共 12 个）**:
   - `test_us_playbook.py` 3 个: telegram application + option expiry 环境问题
   - `test_hk.py` 5 个: HK 模块预存问题
   - `test_collector_futu.py` 4 个: Futu API mock 问题

6. **HK playbook.py 被 linter 回退**。git stash 过程中 linter 将 HK playbook.py 回退到旧版（不含新函数导入）。已通过 `git stash pop` 恢复，但如果再次发生类似情况需注意。最终版本已确认包含所有新导入和后处理链修改。

7. **US/HK 后处理链有两个入口**。`_generate_action_plans()` 中有两个 `if ctx:` 块（主链 + issue4 trend downgrade 链），两处都需要保持同步。未来如果新增后处理步骤，容易遗漏其中一处。可考虑提取为共享函数。

### 性能层面

8. **`calculate_atr_5min` 每次 pipeline 调用一次**。resample + True Range 对 ~390 bar 数据开销极小（<1ms），无需缓存。

9. **新增 4 个后处理步骤**。每个都是 O(1) per plan（最多 3 个 plan），总开销可忽略。
