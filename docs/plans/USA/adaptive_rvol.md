# US Playbook 自适应 RVOL 阈值优化

## 状态: ✅ 已实现

## 问题

US Playbook 的 regime 分类使用全局静态阈值 (`gap_and_go_rvol: 1.5`, `trend_day_rvol: 1.2`, `fade_chop_rvol: 1.0`)，所有标的共用。但不同标的的 RVOL 分布方差差异显著：
- TSLA 的 RVOL 1.5 可能只是正常波动
- AAPL 的 1.5 则意味着重大事件
- TREND_DAY 的 `gap_pct < 0.5%` 是硬编码绝对值，TSLA 日常 gap 2%+ 而 AAPL 很少超过 1%

## 方案：百分位法 + 日波幅归一化

**核心思路**：从历史 bars 计算每个标的的 RVOL 分布，用百分位排名替代固定阈值；用日均波幅归一化 gap。

### 为什么选百分位而非 z-score
- RVOL 分布通常右偏（事件驱动的极高值），百分位对偏态分布更鲁棒
- 百分位直观易懂（"今天的成交量超过了历史 85% 的交易日"）
- 无需正态性假设

## 实现

### `RvolProfile` dataclass (`src/us_playbook/indicators.py`)

```python
@dataclass
class RvolProfile:
    gap_and_go_rvol: float      # P85 阈值
    trend_day_rvol: float       # P60 阈值
    fade_chop_rvol: float       # P30 阈值
    avg_daily_range_pct: float  # 日均波幅 (H-L)/L %
    percentile_rank: float      # 今日 RVOL 在分布中的百分位 (0-100)
    sample_size: int            # 历史天数
```

### `compute_rvol_profile()` 算法

1. 按日期分组 history_bars → `unique_dates`
2. 对每个历史日 Di（从第 2 天起），用 D1..D(i-1) 的同时段均量计算 Di 的 RVOL → `rvol_samples`
3. 同时计算各日的 `(max(High) - min(Low)) / min(Low) * 100` → `daily_ranges`
4. 如果 `len(rvol_samples) < min_sample_days`（默认 5），返回 fallback 静态阈值
5. 用 `np.percentile()` 计算自适应阈值
6. 保护：`gap_and_go >= trend_day + 0.1`（避免低方差标的阈值重叠）

### `classify_us_regime()` 增强 (`src/us_playbook/regime.py`)

- 新增 `rvol_profile: RvolProfile | None` 和 `gap_significance_threshold: float` 参数
- 如果 `rvol_profile.sample_size >= 5`，自适应阈值覆盖静态参数
- Gap 归一化：`normalized_gap = abs(gap_pct) / avg_daily_range_pct`，若 < 0.3 视为 small gap
- `details` 中标注 `(adaptive)` 或 `(static)`

### `USRegimeResult` 新增字段 (`src/us_playbook/__init__.py`)

```python
adaptive_thresholds: dict | None = None
# e.g. {"gap_and_go": 1.73, "trend_day": 1.15, "fade_chop": 0.88, "pctl_rank": 72.3, "sample": 9}
```

### 集成 (`src/us_playbook/main.py`)

- `_run_single_symbol()` 在 RVOL 计算后调用 `compute_rvol_profile()`
- 将 `rvol_profile` 传入 `classify_us_regime()`

### Telegram 展示 (`src/us_playbook/playbook.py`)

Regime section 中显示：
```
RVOL: 2.31 | Gap: +1.82% | 自适应 P9d=1.73 (rank 92%)
```

### 配置 (`config/us_playbook_settings.yaml`)

```yaml
regime:
  adaptive:
    enabled: true
    gap_and_go_percentile: 85
    trend_day_percentile: 60
    fade_chop_percentile: 30
    min_sample_days: 5
    gap_significance_threshold: 0.3
```

## 修改文件

| 文件 | 修改内容 |
|------|----------|
| `src/us_playbook/indicators.py` | 新增 `RvolProfile` + `compute_rvol_profile()` |
| `src/us_playbook/regime.py` | 新增 `rvol_profile` 参数 + gap 归一化 |
| `src/us_playbook/__init__.py` | `USRegimeResult` 新增 `adaptive_thresholds` 字段 |
| `src/us_playbook/main.py` | 集成 profile 调用 |
| `config/us_playbook_settings.yaml` | 新增 `adaptive` 配置块 |
| `src/us_playbook/playbook.py` | Telegram 消息展示自适应信息 |
| `tests/test_us_playbook.py` | 新增 12 个测试 (TestRvolProfile + TestAdaptiveRegime) |

## 测试

52 tests 全部通过 (`pytest tests/test_us_playbook.py -v`)

### 新增测试覆盖
- `TestRvolProfile`: 充足数据自适应、数据不足 fallback、空数据、高波动宽阈值、百分位排名、日均波幅
- `TestAdaptiveRegime`: profile 覆盖静态、None 保持静态、gap 归一化（通过/阻止）、不足样本用静态、Telegram 消息显示
