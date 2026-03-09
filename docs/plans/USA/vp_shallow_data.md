# US Playbook VP 浅数据优化

## 状态：已实施 (2026-03-09)

## 问题

US Playbook 的 Volume Profile 原用 `lookback_days: 3`（日历天），加 `+2` 周末缓冲，实际从 Futu 获取 5 个日历天的数据。遇到假日（MLK Day、Presidents Day、Thanksgiving 等），可能只剩 2 个交易日（~780 根 1m bar）。

VAH/VAL 是止损依据（策略建议 "VAH 附近做空, VAL 附近做多"），数据太浅导致 Value Area 偏窄、代表性不足，直接影响风险控制。

附带问题：RVOL 配置 `lookback_days: 10`，但实际只收到 ~3 天历史数据（与 VP 共用同一批 bars），远不及配置目标。

## 方案

1. VP lookback 从 3 日历天改为 **5 交易日**（1 完整交易周）
2. 获取窗口扩大至同时满足 RVOL（10天）和 VP（5天），在 Python 侧分别截断
3. 计算实际交易日数，不足时 **降低 regime 置信度 + Telegram 警告**
4. 不使用日历解析，用固定缓冲（`max * 2 + 2`）覆盖假日

## 修改清单

### 1. `src/hk/__init__.py` — VolumeProfileResult 新增字段
- `trading_days: int = 0`：实际参与计算的交易日数
- 默认值 0 保证 HK 模块所有现有调用不受影响

### 2. `config/us_playbook_settings.yaml` — 配置更新
```yaml
volume_profile:
  lookback_trading_days: 5   # 原 lookback_days: 3（日历天）
  value_area_pct: 0.70
  min_trading_days: 3         # 低于此数警告+降级
```

### 3. `src/us_playbook/levels.py` — 核心逻辑
- `get_history_bars()` 新增 `max_trading_days` 参数，截断至最近 N 个交易日
- `compute_volume_profile()` 计算后填充 `result.trading_days`
- 新增 `calc_fetch_calendar_days(vp, rvol)` → `max(vp, rvol) * 2 + 2`

### 4. `src/us_playbook/main.py` — 获取与分流
- 使用 `calc_fetch_calendar_days()` 计算获取窗口（10天 → 22日历天）
- VP 用截断 history（5 交易日），RVOL 用完整 history（~10 交易日）
- Regime 调用传入 `vp_trading_days` + `min_vp_trading_days`

### 5. `src/us_playbook/regime.py` — 置信度惩罚
- 新增 `vp_trading_days` / `min_vp_trading_days` 参数
- `0 < vp_trading_days < min_vp_trading_days` 时：confidence -= 0.15，details 追加 `"VP thin (Nd)"`

### 6. `src/us_playbook/playbook.py` — Telegram 警告
- VP trading_days < 3 时显示：`⚠️ VP 仅 N 天数据，VAH/VAL 参考性降低`

### 7. `tests/test_us_playbook.py` — 新增 8 个测试
- `test_calc_fetch_calendar_days` — 验证日历天计算
- `test_get_history_bars_max_trading_days` — 截断逻辑
- `test_get_history_bars_no_cap` — 向后兼容
- `test_compute_vp_trading_days_populated` — trading_days 填充
- `test_compute_vp_empty_bars` — 空数据
- `test_regime_vp_thin_penalty` — 置信度惩罚
- `test_regime_no_penalty_sufficient_days` — 充足数据无惩罚
- `test_regime_no_penalty_zero_days` — 旧代码兼容（0=不检查）
- `test_playbook_vp_thin_warning` — Telegram 警告
- `test_playbook_no_warning_sufficient_days` — 无警告

## RVOL 附带改善

修改后 `history_all` 包含 ~10-12 个交易日，RVOL 的 `sorted(set(hist_dates))[-lookback_days:]` 截断逻辑天然受益，无需额外改动。

## Futu API 约束验证

- 10 交易日 x 390 bar = 3,900 bar，远低于 5,000 上限
- `fetch_days = 22`，`max_count = min(22*400+100, 5000) = 5000`，安全
- 每个 symbol 多获取 ~1,600 bar（从 2,100 到 3,900），延迟增加约 0.5-1s，可接受
