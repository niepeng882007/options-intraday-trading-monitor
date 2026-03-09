# US Playbook RVOL 去噪方案

## 状态：✅ 已实施 (2026-03-09)

## Context

09:45 推送时只有 15 分钟数据，开盘前 3 分钟的集合竞价/开盘轮转量异常集中，导致跳空股票 RVOL 虚高。之前用 `gap_and_go_rvol_preliminary: 2.0` 的高阈值补偿，治标不治本。

## 方案：三项核心改动

### 1. 跳过开盘轮转（skip_open_minutes）

排除前 3 分钟（09:30-09:32）的集合竞价量，today 和 history 对称排除。

### 2. 扩展窗口替代固定窗口

采用 HK 模块的 time-of-day 公平对比：用 skip 之后到当前最新 bar 的全部数据，历史也按同一时间截断。
- 09:45 推送：用 09:33-09:44（12 min 干净数据）
- 10:15 推送：用 09:33-10:14（42 min，非常稳定）
- 手动触发：用全部可用数据

### 3. 移除 preliminary 阈值 hack

RVOL 去噪后，09:45 和 10:15 用统一阈值（1.5），删除 `is_preliminary` 逻辑和 `gap_and_go_rvol_preliminary` 配置。

---

## 修改文件清单

### `src/us_playbook/indicators.py` — 重写 `calculate_us_rvol()`

```python
def calculate_us_rvol(
    today_bars: pd.DataFrame,
    history_bars: pd.DataFrame,
    skip_open_minutes: int = 3,   # 替代 window_minutes
    lookback_days: int = 10,
) -> float:
```

核心逻辑：
- 计算 `skip_cutoff = (09:30 + skip_open_minutes).time()`
- today：过滤 `bar.time >= skip_cutoff`，取最新 bar 的 `cutoff_time`
- history：每天过滤 `skip_cutoff <= bar.time <= cutoff_time`
- 比率 = today_vol / mean(daily_vols)
- 边界：skip 后无 bar → 返回 1.0

### `src/us_playbook/regime.py` — 移除 is_preliminary

- 删除参数 `is_preliminary: bool = False`
- 删除 `gg_rvol = 2.0 if is_preliminary else gap_and_go_rvol`
- 直接用 `gap_and_go_rvol`

### `src/us_playbook/main.py` — 更新调用

- 删除 `is_preliminary = update_type == "morning"`
- RVOL 调用改为 `skip_open_minutes=rvol_cfg.get("skip_open_minutes", 3)`
- regime 调用简化：删除 `gg_rvol_key` 逻辑，统一用 `gap_and_go_rvol: 1.5`
- 删除传给 `classify_us_regime` 的 `is_preliminary` 参数

### `config/us_playbook_settings.yaml`

```yaml
rvol:
  skip_open_minutes: 3    # 替代 window_minutes: 15
  lookback_days: 10

regime:
  gap_and_go_rvol: 1.5    # 移除 gap_and_go_rvol_preliminary: 2.0
  trend_day_rvol: 1.2
  fade_chop_rvol: 1.0
  market_context_symbols: [SPY, QQQ]
```

### `tests/test_us_playbook.py`

- **修改** `test_normal_rvol` / `test_high_rvol`：bars 改为 09:33+ 时间戳
- **修改** `test_no_history`：参数 `window_minutes` → `skip_open_minutes`
- **替换** `test_preliminary_wider_threshold` → `test_gap_and_go_unified_threshold`
- **新增** `test_rvol_skip_open_minutes`：验证 skip zone 内的 bar 被排除
- **新增** `test_rvol_expanding_window`：验证 10:15 时用更多数据
- **新增** `test_rvol_all_bars_in_skip_zone`：09:31 调用返回 1.0

### 不改动

- `playbook.py` 的 `update_type` / 初步/确认 标签保留（仍有语义：数据量少 vs 多）
- HK 模块不受影响

---

## 验证

```bash
pytest tests/test_us_playbook.py -v       # 30 passed ✅
python -c "from src.us_playbook.indicators import calculate_us_rvol"  # OK ✅
```
