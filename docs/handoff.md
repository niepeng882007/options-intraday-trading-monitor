# US 日型判断引擎重构 (4 → 7+1) — 交接文档

> 日期: 2026-03-18

## 本次完成了什么

### 三个 Phase 全部落地

16 个文件，+1444 / -545 行。517 测试通过（3 个预先存在的失败不受影响）。

#### Phase 1: 枚举 + family 迁移（零行为变更）

| 旧枚举 | 新枚举 | family |
|--------|--------|--------|
| GAP_AND_GO | GAP_GO | TREND |
| TREND_DAY | TREND_STRONG | TREND |
| — | TREND_WEAK | TREND |
| FADE_CHOP | RANGE | FADE |
| — | NARROW_GRIND | FADE |
| — | V_REVERSAL | REVERSAL |
| — | GAP_FILL | REVERSAL |
| UNCLEAR | UNCLEAR | UNCLEAR |

- 新增 `RegimeFamily` 枚举，`USRegimeType.family` property
- `USRegimeResult` 新增 6 个字段（`rvol_corrected`, `vwap_slope`, `vwap_hold_minutes`, `gap_fill_pct`, `reversal_confirmed`, `classified_at`）
- 全量 caller 迁移：main.py / playbook.py / option_recommend.py / stabilizer.py / regime.py / backtest (evaluators + simulator + daily_bias_eval) / action_plan.py / tests
- 旧枚举引用 grep 验证为 0

#### Phase 2: 分类引擎升级

- **TREND_STRONG vs TREND_WEAK 拆分**：RVOL 路径通过 VWAP hold duration（>60min）+ 高 RVOL 区分 → TREND_STRONG；结构路径/持续路径 → TREND_WEAK
- **NARROW_GRIND 检测**：RVOL < 0.5 且日内 range < ADR × 50%
- **RVOL 开盘校正**：`correct_rvol_open()` 在 09:30-09:45 窗口内用历史同时段 RVOL 中位数修正
- **VWAP Hold Duration**：`calculate_vwap_hold_duration()` 从尾部计算连续 VWAP 同侧 bar 数
- **配置扩展**：`config/us_playbook_settings.yaml` 新增 `rvol_correction` / `vwap_trend` / `unclear_timeout_minutes` / `trend_strong_rvol` / `narrow_grind_*`

#### Phase 3: 中场转换 + 差异化行为

- **V_REVERSAL 检测**：`_detect_v_reversal()` — 开盘单方向 >0.5% 后反转穿越 open，尾部确认 bar 趋势 + 量能
- **GAP_FILL 检测**：`_detect_gap_fill()` — 缺口回补 >50%，RVOL 衰减
- **`detect_regime_transition()` 扩展**：TREND family 现在可转 V_REVERSAL / GAP_FILL（不仅是 UNCLEAR/RANGE → TREND 升级）
- **UNCLEAR 超时**：`RegimeStabilizer` 60 分钟后强制归类（有 lean → TREND_WEAK，RVOL <0.5 → NARROW_GRIND，兜底 → RANGE）
- **Playbook 差异化**：8 种日型各有独立的结论文本、原因分析、ActionPlan 路由（TREND_WEAK 缩减仓位警告；NARROW_GRIND demote Plans A/B；V_REVERSAL/GAP_FILL 独立方案）
- **Option 差异化**：NARROW_GRIND 直接 wait，V_REVERSAL 用 `lean` 反转方向
- **Regime 历史追踪**：`USPredictor._regime_history` 记录 symbol 的日型变化时间线
- **10 个新测试**：TestVReversalDetection (3) / TestGapFillDetection (2) / TestUnclearTimeout (3) / TestRegimeTransition7Types (2)

### 关键文件清单

| 文件 | 改动性质 |
|------|----------|
| `src/us_playbook/__init__.py` | 枚举 + dataclass 扩展 |
| `src/us_playbook/regime.py` | 核心分类 + V_REVERSAL/GAP_FILL 检测 + transition |
| `src/us_playbook/main.py` | RVOL 校正集成 + regime 历史 + 新参数传递 |
| `src/us_playbook/playbook.py` | 8 种日型差异化展示 |
| `src/us_playbook/option_recommend.py` | NARROW_GRIND/V_REVERSAL 方向逻辑 |
| `src/us_playbook/stabilizer.py` | UNCLEAR 超时 + 8 枚举 strength |
| `src/us_playbook/indicators.py` | `correct_rvol_open()` |
| `src/common/indicators.py` | `calculate_vwap_hold_duration()` |
| `src/common/action_plan.py` | trend/fade 字符串集合更新 |
| `config/us_playbook_settings.yaml` | 新配置节 |
| `src/us_playbook/backtest/evaluators.py` | 新日型准确性标准 |
| `src/us_playbook/backtest/simulator.py` | TREND_WEAK 模拟 + signal_type.rsplit bug fix |
| `tests/test_us_playbook.py` | 枚举迁移 + 10 个新测试 |

---

## 下次应该从哪里开始

### 1. 回测验证（最高优先级）

```bash
python -m src.us_playbook.backtest -d 20 -v
```

运行 20 天回测，确认：
- 新日型（TREND_WEAK, NARROW_GRIND）在输出中出现
- 总体准确率 ≥ 旧系统
- TREND_STRONG vs TREND_WEAK 分布是否合理（预期 TREND_WEAK 占多数）

如果准确率下降，调节 `vwap_trend.hold_minutes_trend_bias`（当前 60min）和 `trend_strong_rvol`（当前 0 即用 gap_and_go_rvol 1.5）。

### 2. 实盘观察

手动触发几个标的的 playbook（SPY、TSLA），在不同时段确认：
- 09:35 查询 → RVOL 校正显示 `(⚠️ 开盘放大, 校正值 X.XX)`
- VWAP 趋势显示 `▸ VWAP 趋势: 斜率 +X.XXXX%/bar | 单侧持续 Xmin`
- 各日型 emoji + 中文名正确
- NARROW_GRIND 触发观望
- UNCLEAR 超过 60min 后切换为具体日型

### 3. 自动扫描信号类型映射

`regime_to_signal_type()` 已更新，但 L1/L2 逻辑中的 `expected_regimes` 映射需要验证新类型是否正确路由：
- TREND_WEAK 应触发 BREAKOUT 信号但用更高 confidence 门槛 — **当前未实现此差异化门槛**
- V_REVERSAL 应触发 `REVERSAL_{dir}` 信号 — **当前仅通过 `detect_regime_transition()` 路径触发，未在 L1 screen 中覆盖**

### 4. Playbook 版本 diff

计划中的 "每次更新标注与上一版的变化"（Step 3.4）— `_regime_history` 已存储数据，但 playbook 格式化中**尚未读取和展示** "vs 上一版" 字段。需要在 `format_us_playbook_message()` 中加入。

### 5. 相对强度 (个股 vs SPY)

计划中提到的 "个股 vs SPY 相关性，脱钩时降低大盘权重" — **未实现**。可作为后续独立 PR。

---

## 未解决的问题

### 代码层面

1. **3 个预先存在的测试失败**（与本次重构无关）：
   - `test_build_telegram_application_wires_dual_requests` — 本地 proxy 配置导致 kwargs 不匹配
   - `test_recommend_moderate_still_tradeable` / `test_recommend_none_shows_near_val` — expiry_dates 在测试环境中生成了已过期日期

2. **`signal_type.rsplit` bug**：simulator.py 中旧的 `split("_")[0]` 已修复为 `rsplit("_", 1)[0]`，但 **by_regime 统计的历史数据可能需要清理**（如果存在持久化的 backtest 结果缓存）。

3. **V_REVERSAL / GAP_FILL 在 backtest 中不可评分**：当前标记为 `scorable=False`，因为它们是中场转换不会出现在初始分类中。如需回测验证这两种转换的准确性，需要另建评估框架（在每天多个时间点采样 regime transition）。

4. **RVOL 校正的历史数据依赖**：`correct_rvol_open()` 需要 ≥3 天历史 bars 才能计算中位数。首次运行或历史数据不足时不做校正，这是预期行为但可能让用户困惑。

### 设计层面

5. **TREND_WEAK L1 门槛**：计划说 "L1 screen 用更高 confidence 门槛"，但当前 TREND_WEAK 用的是和 TREND_STRONG 相同的 `bo_min_conf`。需要决定是否为 TREND_WEAK 单独设一个更高的门槛（如 0.80 vs 0.70）。

6. **NARROW_GRIND 不触发自动扫描**：这是设计决策（`regime_to_signal_type()` 返回 None），但也意味着如果市场从 NARROW_GRIND 突然转为趋势日，只能靠 `detect_regime_transition()` 路径捕获。

7. **adaptive_thresholds key 保持旧名**：config keys `gap_and_go` / `trend_day` / `fade_chop` 与枚举名不一致。这是有意为之（避免 stabilizer 缓存失效），但长期可能造成混淆。

8. **Playbook 中文文本硬编码**：`_plans_fade_bearish()` 等函数中仍有 `"转 TREND_STRONG"` 等硬编码字符串用于 Plan C 逻辑触发文本。已从 Phase 1 迁移为新枚举名，但理想方案是用 `REGIME_NAME_CN` 映射替代。
