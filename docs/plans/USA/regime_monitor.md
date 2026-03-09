# US Playbook Regime 盘中变更监控

## Context

US Playbook 在 09:45 和 10:15 ET 各推送一次。中间 30 分钟无刷新，但市场可能在此期间完全反转（如 GAP_AND_GO 突破失败变为 FADE_CHOP），用户仍看到过时的 regime 分类。

**目标:** 在 09:45-10:15 窗口内检测 regime 变化，仅在变化时推送告警，避免推送噪音。

## 方案：轻量级 Regime 变更监控

每 5 分钟对全部 watchlist 做一次轻量检查（跳过 VP/Gamma Wall/Filters 重算），仅在 regime 翻转或置信度大幅变化时推送告警。

### 轻量检查 vs 完整 Pipeline

| 步骤 | 完整 | 轻量 | 原因 |
|------|------|------|------|
| `get_history_bars` | Yes | Yes | RVOL 需要 today bars |
| `compute_volume_profile` | Yes | No | VP 基于历史，日内近似不变 |
| `extract_previous_day_hl` | Yes | No | PDH/PDL 定值 |
| `get_premarket_hl` | Yes | No | PMH/PML 盘后不变 |
| `calculate_vwap` | Yes | Yes | VWAP 随新 bar 更新 |
| `calculate_us_rvol` | Yes | Yes | RVOL 是核心变量 |
| `get_snapshot` | Yes | Yes | 需要最新价格 |
| Gamma Wall | Yes | No | OI 变化慢，API 开销大 |
| Filters | Yes | No | 日内不变 |
| `compute_rvol_profile` | Yes | No | 基于历史，日内稳定 |
| `classify_us_regime` | Yes | Yes | 核心判断 |

**结果:** 约 30% 的完整 pipeline 计算量，且省去 Gamma Wall 的 10s timeout。

## 文件变更

### 1. `src/us_playbook/main.py` — 核心逻辑

**新增 `_cached_context` 字段**（`__init__` 中）：
```python
self._cached_context: dict[str, dict] = {}
```

**在 `_run_single_symbol` 末尾缓存中间数据：**
```python
self._cached_context[symbol] = {
    "history_all": history_all,
    "vp": vp,
    "pdh": pdh, "pdl": pdl,
    "pmh": pmh, "pml": pml,
    "prev_close": prev_close,
    "rvol_profile": rvol_profile,
    "gamma_wall": gamma_wall,
}
```

**新增 `_check_regime_change(symbol)` 方法：**
1. 从 `_cached_context[symbol]` 取缓存的 VP/PDH/PDL/PMH/PML/prev_close/rvol_profile/gamma_wall
2. `get_snapshot(symbol)` → 最新价格
3. `get_history_bars(symbol, days=fetch_days)` → 提取 today bars
4. `calculate_vwap(today)` + `calculate_us_rvol(today, history_all)`
5. `classify_us_regime()` 用缓存 VP + 新 RVOL + 新 price
6. 比较：`new.regime != old.regime` 或 `|new.confidence - old.confidence| > threshold`
7. 返回 `(changed: bool, new_regime: USRegimeResult | None, old_regime: USRegimeResult | None)`

**新增 `run_regime_monitor_cycle()` 方法：**
1. 时间窗口守卫：仅在 morning push 之后、confirm push 之前运行（通过检查 `_last_playbooks` 是否有数据 + 当前时间判断）
2. Phase 1: 先检查 SPY/QQQ context symbols
3. Phase 2: 检查其余 symbols，传入 spy_regime
4. 有变化 → `format_regime_change_alert()` → `_send_tg()`
5. 更新 `_last_playbooks[symbol].regime` 和 `_cached_context`
6. 防抖：`_regime_flip_count[symbol]` 跟踪翻转次数，10 分钟内 >=2 次则暂停该 symbol 并提示"regime 不稳定"

**新增成员变量：**
- `_regime_flip_count: dict[str, list[datetime]]` — 记录翻转时间戳
- 时间窗口常量: `_MONITOR_START_OFFSET = 5` (分钟), `_MONITOR_END_OFFSET = 2` (分钟)

### 2. `src/us_playbook/playbook.py` — 告警消息格式化

**新增 `format_regime_change_alert()` 函数：**

```
⚠️🔄 REGIME 变更 — TSLA
━━━━━━━━━━━━━━━━━━━━━━
❌ 旧: 🚀 缺口追击日 (72%)
✅ 新: 📦 震荡日 (65%)

📊 变化原因
• RVOL: 2.31 → 1.05
• 价格: $280.50 → $275.20

📍 关键位 (简)
  VAH    280.00
  VWAP   276.50  ← current
  VAL    270.00

📋 新策略: 严禁 OTM，深度 ITM...
⏱ 09:58 ET
```

输入参数：`symbol, name, old_regime, new_regime, key_levels`
- 展示新旧 regime 对比 + RVOL/价格变化
- 简化版 key levels（仅 VAH/VWAP/POC/VAL）
- 对应新 regime 的策略建议

### 3. `config/us_playbook_settings.yaml` — 新增配置

```yaml
regime_monitor:
  enabled: true
  check_interval_seconds: 300        # 每5分钟检查
  start_after_morning_minutes: 5     # 09:45后5分钟开始 (09:50)
  end_before_confirm_minutes: 2      # 10:15前2分钟停止 (10:13)
  confidence_change_threshold: 0.2   # 置信度变化触发阈值
  max_flips_in_window: 2             # 翻转超过此值暂停
```

### 4. `src/main.py` — 调度器集成

在 US Playbook scheduled pushes 后追加 interval job。
`run_regime_monitor_cycle()` 内部自行判断时间窗口，窗口外直接 return。

### 5. `src/us_playbook/__main__.py` — 独立模式同步注册

追加同样的 interval job。

### 6. `tests/test_us_playbook.py` — 新增测试

`TestRegimeMonitor` 类：
- `test_regime_change_detected` — RVOL 从 2.5 降到 0.8，验证检测到 GAP_AND_GO → FADE_CHOP
- `test_no_change_no_alert` — 同样参数，验证不触发
- `test_confidence_change_triggers` — regime 不变但置信度变化 > 0.2
- `test_flip_debounce` — 快速翻转 3 次后暂停
- `test_time_window_guard` — 09:44 / 10:16 时直接 return
- `test_alert_format` — 验证消息包含新旧对比
- `test_cached_levels_reused` — 验证不调用 VP/Gamma

## API 开销评估

8 symbols × (1 snapshot + 1 get_history_bars) = 16 次/轮
09:50-10:10 窗口内最多 4 轮 = 64 次调用，完全可接受。

## 验证方案

1. `pytest tests/test_us_playbook.py -v` — 全部测试通过
2. 手动测试：在市场时段运行 `python -m src.us_playbook`，用 `/us_playbook SPY` 触发初始 playbook，等待 5 分钟后检查 monitor 是否运行
3. 模拟 regime 变化：用 mock 数据让 RVOL 从高降低，验证告警消息格式正确
