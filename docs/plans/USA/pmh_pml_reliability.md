# PMH/PML 数据可靠性优化方案

## Status: ✅ Implemented (2026-03-09)

## Context

US Playbook 的 GAP_AND_GO regime 判据依赖 PMH/PML（盘前高低点），但 Futu `get_market_snapshot` 返回的 `pre_high_price`/`pre_low_price` 偶尔为 0。当前 fallback 用 `max(open, prev_close)` 估算，这只是跳空幅度，不是真实盘前范围，导致 GAP_AND_GO 判定失准。

此外，`_run_single_symbol` 中 `get_premarket_hl` 和 `get_snapshot` 分别调用 `get_market_snapshot`，存在冗余 API 调用。

## Changes

### 1. `PremarketData` dataclass (`src/collector/base.py`)
- 新增 `PremarketData(pmh, pml, source)` dataclass
- `source` 取值: `"futu"` | `"yahoo"` | `"gap_estimate"`

### 2. 三级 fallback + 合并 snapshot (`src/collector/futu.py`)
- `_fetch_snapshot` 追加提取 `pre_high_price`/`pre_low_price`
- 新增 `_fetch_yahoo_premarket()` 静态方法：Yahoo Finance prepost=True 1m bars
- 重写 `_build_premarket_data()`：
  - Tier 1: Futu snapshot `pre_high_price`/`pre_low_price` → `source="futu"`
  - Tier 2: Yahoo Finance prepost=True 1m bars → `source="yahoo"`
  - Tier 3: `max(open, prev_close)` / `min(open, prev_close)` → `source="gap_estimate"`
- `get_premarket_hl()` 签名变更：`async def get_premarket_hl(self, symbol, snapshot=None) -> PremarketData`

### 3. 消除冗余 snapshot 调用 (`src/us_playbook/main.py`)
- Before: 2 次 `get_market_snapshot`（`get_premarket_hl` + `get_snapshot`）
- After: 1 次 `get_snapshot`，结果传递给 `get_premarket_hl(symbol, snapshot=snap)`
- `pm_source` 传递给 `classify_us_regime` 和 `build_key_levels`

### 4. `KeyLevels.pm_source` 字段 (`src/us_playbook/__init__.py`)
- `pm_source: str = "futu"` 默认值

### 5. `build_key_levels` 接收 `pm_source` (`src/us_playbook/levels.py`)
- 新增 `pm_source="futu"` 参数，传递给 `KeyLevels`

### 6. Playbook 输出标注 (`src/us_playbook/playbook.py`)
- PMH/PML 在 `_collect_levels` 中根据 `pm_source` 标注：
  - `"yahoo"` → `" (Yahoo)"`
  - `"gap_estimate"` → `" (估)"`

### 7. Regime 置信度惩罚 (`src/us_playbook/regime.py`)
- 新增 `pm_source` 参数
- 当 `pm_source == "gap_estimate"` 且分类为 GAP_AND_GO 时：
  - `confidence -= 0.15`（下限 0.1）
  - `details` 附加 `"; PM estimated (gap range)"`

### 8. 测试 (`tests/test_us_playbook.py`)
- `TestPremarketData`: 验证 dataclass 3 种 source
- `TestKeyLevelsPmSource`: 验证 `pm_source` 默认值和传递
- `TestCollectLevelsPmAnnotation`: 验证 futu/yahoo/gap_estimate 标注
- `TestRegimePmSourcePenalty`: 验证 GAP_AND_GO 置信度降低、非 GAP_AND_GO 不受影响、confidence floor

## Verification

```bash
pytest tests/test_us_playbook.py -v  # 66 passed
```

## Impact
- API 调用减半（每个 symbol 少一次 `get_market_snapshot`）
- GAP_AND_GO 判定准确性提升（真实 PM 数据 vs 跳空估算区分）
- Telegram 输出透明化（用户可见 PMH/PML 数据来源）
