# US Auto-Scan 性能优化与信号质量提升

> 日期：2026-03-12
> 类型：性能优化 | 功能实现
> 难度：⭐⭐⭐⭐

## 一、变更摘要

US Playbook 的 auto-scan 管线存在两大问题：（1）L1 筛选和 L2 验证之间重复执行完整分析管线（snapshot 重新拉取、VP 重新计算、regime 重新分类），导致扫描延迟高；（2）信号质量粒度不够——所有 alert 统一标记为"强信号"，fade 计划的 SL 距离不受控，低 RVOL 方向性行情被误分类为 FADE_CHOP。

本次改动围绕 **性能优化**（L1→L2 数据复用、批量 snapshot 预取、频率预检）和 **信号质量**（信号强度分级、RSI 确认、Gamma Wall 距离过滤、fade SL 封顶、directional trap 检测）两个维度展开，共涉及 8 个文件、1295 行新增代码。

## 二、修改的文件清单

| 文件路径 | 变更类型 | 说明 |
|---------|---------|------|
| `src/us_playbook/main.py` | 修改 | 核心：`_L1Result` 数据容器、`_run_l2_incremental()` 增量 L2、批量 snapshot 预取、频率预检、信号强度分级、auto-scan 图表生成、per-type 频率控制 |
| `src/us_playbook/playbook.py` | 修改 | Gamma Wall 距离过滤、fade SL 封顶 `_cap_fade_sl()`、FADE_CHOP 方向一致性检查、UNCLEAR fade plan 质量门控 |
| `src/us_playbook/regime.py` | 修改 | Directional trap 检测——低 RVOL + 强单向行情路由到 UNCLEAR |
| `src/us_playbook/indicators.py` | 修改 | 新增 `calculate_rsi()` 函数 |
| `src/common/action_plan.py` | 修改 | Suppressed plan 格式优化——只显示触发+警告，隐藏具体 entry/SL/TP |
| `src/main.py` | 修改 | `_us_send_fn` 支持 `photo` 参数，auto-scan 发送图表 |
| `config/us_playbook_settings.yaml` | 修改 | 新增 RSI 配置、per-type 频率限制 |
| `tests/test_us_playbook.py` | 修改 | 新增 687 行测试：Volume Surge 基线修复、频率预检、信号强度分级、RSI、per-type 频率控制 |

## 三、关键代码解析

### 3.1 L1→L2 数据复用架构（`_L1Result` + `_run_l2_incremental`）

**改动前：** L1 筛选通过后，L2 验证调用 `_run_analysis_pipeline()` 从零开始——重新拉取 snapshot、重新计算 VP/RVOL、重新分类 regime，所有 L1 的中间数据被丢弃。

**改动后：**

```python
@dataclass
class _L1Result:
    """L1 筛选的中间数据，供 L2 复用。"""
    hist_bars: pd.DataFrame    # 历史 K 线（已从缓存获取）
    today_bars: pd.DataFrame   # 今日 K 线
    cached_entry: _SymbolCache # PDH/PDL/PMH/PML 缓存
    vp: VolumeProfileResult    # Volume Profile（计算代价最高）
    snap: dict                 # Futu snapshot
    price: float
    prev_close: float
    rvol: float
    rvol_profile: object       # RvolProfile | None
    regime: USRegimeResult     # L1 已分类的 regime
```

L1 结束时把中间结果打包到 `_L1Result`，通过 `l1_data["_l1_result"]` 传递给 L2。L2 的 `_run_l2_incremental()` 只做 L1 没做的事：获取期权链 → 计算 Gamma Wall → 运行 filters → 用 Gamma Wall 重分类 regime → 生成期权推荐。

**解析：**

这是典型的 **管线数据传递（Pipeline Data Passing）** 模式。关键设计决策：
- 用 `dataclass` 而不是 `dict` 做容器——有类型提示，IDE 可以自动补全，避免 key 拼写错误
- `_L1Result` 前缀 `_` 表示内部使用，不暴露到模块外
- L2 仍然重新分类 regime（因为 L1 没有 Gamma Wall 数据），但复用了 bars/VP/RVOL 这些计算代价最高的部分
- 保留 `_run_analysis_pipeline()` 作为 fallback，确保 `_l1_result` 为 None 时不会崩溃

### 3.2 批量 Snapshot 预取

```python
# Phase 2: Batch snapshot pre-fetch for non-context symbols
non_ctx_symbols = [s for s in symbols if s not in context_symbols]
pre_fetched_snaps: dict[str, dict] = {}
if non_ctx_symbols:
    try:
        pre_fetched_snaps = await self._collector.get_snapshots(non_ctx_symbols)
    except Exception:
        logger.warning("Batch snapshot fetch failed, will fallback to individual calls")

# L1 中使用预取数据
snap = pre_fetched_snap if pre_fetched_snap else await self._collector.get_snapshot(symbol)
```

**解析：**

Futu API 的 `get_market_snapshot` 支持批量查询（一次最多 400 个 symbol），但之前 L1 对每个 symbol 单独调用。改为在 scan 开始前一次性预取所有非 context symbol 的 snapshot，然后在 L1 中直接使用。关键点：
- 批量请求失败时 **静默降级** 到单个请求（`pre_fetched_snaps` 为空 dict）
- 只预取非 context symbol（SPY/QQQ 在 Phase 1 已经处理）

### 3.3 频率预检（`_quick_frequency_precheck`）

```python
def _quick_frequency_precheck(self, symbol, session, scan_cfg) -> bool:
    """保守预过滤：跳过肯定超频的 symbol，避免执行 L1 的开销。"""
    records = self._scan_history.get(symbol, [])
    if not records:
        return True  # 无历史 → 一定通过

    # 如果 override 可能触发（last alert 是 RANGE），不要提前跳过
    can_upgrade = override_cfg.get("regime_upgrade", True) \
                  and records[-1].signal_type.startswith("RANGE")

    if len(records) >= max_per_day and not can_upgrade:
        return False
    # ... session max 同理
```

**解析：**

这是一个 **短路优化（Short-circuit Optimization）**——在进入计算密集的 L1 之前，用 O(1) 的历史记录查询判断 symbol 是否已经达到频率上限。关键点是 `can_upgrade` 的处理：如果最后一次 alert 是 RANGE 类型且配置了 `regime_upgrade`，则不能提前跳过（因为后续可能 upgrade 为 BREAKOUT 并走 override 路径）。

### 3.4 Directional Trap 检测

```python
# regime.py — 在 FADE_CHOP 判定前插入
if (rvol < fade_chop_rvol
    and today_bars is not None and len(today_bars) >= 15):
    _open_bar_price = float(today_bars.iloc[0]["Close"])
    if _open_bar_price > 0:
        _intraday_move = abs(price - _open_bar_price) / _open_bar_price
        if _intraday_move > 0.015:  # >1.5% 单向移动
            _directional_trap = True
            result = USRegimeResult(
                regime=USRegimeType.UNCLEAR, confidence=0.30,
                lean="bearish" if price < _open_bar_price else "bullish",
                details=f"Directional trap: RVOL {rvol:.2f} but {_intraday_move:.1%} move",
            )
```

**解析：**

这解决了一个实际交易中常见的陷阱：低 RVOL + 价格持续单向移动（比如缓慢下跌 2%）。原来的逻辑只看 RVOL < 阈值就判为 FADE_CHOP，但 FADE_CHOP 意味着"震荡回归"——在单向行情中做 fade 会被趋势碾压。改为路由到 UNCLEAR + 带 lean 方向，让 playbook 生成观望/轻仓方案而非激进的 fade 方案。

### 3.5 信号强度分级

```python
@staticmethod
def _signal_strength_label(signal: USScanSignal) -> tuple[str, str]:
    """根据 confidence 和 RVOL 分三档。"""
    conf = signal.regime.confidence
    rvol = signal.regime.rvol
    if conf >= 0.85 and rvol >= 2.0:
        return "极强信号", "🔥"     # 同时满足高置信+高 RVOL
    if conf >= 0.80 or rvol >= 1.8:
        return "强信号", "🚨"       # 满足其一
    return "标准信号", "🔔"          # 兜底
```

**解析：**

之前所有 alert 都标记为"强信号 🔔"，用户无法区分信号质量。改为双维度（confidence × RVOL）三档分级：
- **极强信号**：AND 逻辑，两个指标都很高，出现频率最低
- **强信号**：OR 逻辑，任一指标突出即可
- **标准信号**：兜底，通过了 L1/L2 但指标不突出

### 3.6 Fade Plan SL 封顶

```python
_FADE_MAX_SL_DISTANCE_PCT = 0.02  # 2%

def _cap_fade_sl(entry, sl, sl_reason, direction):
    """限制 fade 计划的 SL 距离不超过 2%。"""
    if sl is None or entry <= 0:
        return sl, sl_reason
    dist = abs(sl - entry) / entry
    if dist > _FADE_MAX_SL_DISTANCE_PCT:
        if direction == "bearish":
            capped = round(entry * (1 + _FADE_MAX_SL_DISTANCE_PCT), 2)
        else:
            capped = round(entry * (1 - _FADE_MAX_SL_DISTANCE_PCT), 2)
        return capped, "固定止损"
    return sl, sl_reason
```

**解析：**

Fade 策略本质是均值回归——预期波动有限。当 `_nearest_levels` 找到的 SL 距离 entry 很远（如 Gamma Wall 在 entry 上方 5%），R:R 看似很好但实际上：（1）SL 几乎不会被触发（意味着它不是有效的风控）；（2）如果真触及，亏损巨大。封顶 2% 确保 fade plan 的 SL 在合理范围内。

### 3.7 Gamma Wall 距离过滤

```python
# playbook.py — _us_key_levels_to_dict()
if gamma_wall and current_price > 0:
    if gamma_wall.call_wall_strike > 0:
        dist = abs(gamma_wall.call_wall_strike - current_price) / current_price * 100
        if dist <= max_gamma_distance_pct:  # 默认 10%
            d["Call Wall"] = gamma_wall.call_wall_strike
```

**解析：**

Gamma Wall 是期权市场的结构性支撑/阻力，但当 wall strike 距离当前价格过远（比如 Put Wall 在 -15%），它对日内交易毫无参考意义，反而会扭曲 action plan 的 SL/TP 计算（产生不现实的 R:R 比率）。加入距离过滤后，只有 ≤10% 范围内的 wall 才参与 key level 计算。

## 四、涉及的知识点

### 4.1 管线数据复用（Pipeline Data Passing）

**是什么：** 在多阶段处理管线中，前一阶段的中间结果被结构化保存并传递给后续阶段，避免重复计算。

**为什么重要：** 在实时扫描场景中，每 180 秒扫 13 个 symbol，每个 symbol 的 VP 计算需要处理数万根 K 线。如果 L2 重复计算 L1 已经得到的结果，扫描时间会翻倍，可能错过交易窗口。

**在本次变更中如何体现：** `_L1Result` dataclass 携带了 L1 的 9 个中间结果（bars、VP、snap、RVOL、regime 等）。`_run_l2_incremental()` 直接解包使用，只补充 L1 缺少的期权链和 Gamma Wall 数据。

**延伸阅读：** 搜索 "pipeline pattern data processing"、"Apache Beam PCollection"、"ETL intermediate materialization"。

### 4.2 短路优化与分层筛选

**是什么：** 在计算密集的管线前放置轻量级预检，用极低代价过滤掉不可能通过的候选者。类似数据库查询优化中的 "predicate pushdown"。

**为什么重要：** 本项目的 auto-scan 有三层筛选：频率预检 → L1 screen → L2 verify。频率预检是 O(1) 的内存查询，L1 需要 API 调用和 VP 计算，L2 需要期权链查询。把最便宜的检查放在最前面，可以避免对已达上限的 symbol 做任何 API 调用。

**在本次变更中如何体现：**
- `_quick_frequency_precheck()`：scan loop 最前面，跳过已达日/session 上限的 symbol
- 批量 snapshot 预取：减少 N 次网络请求为 1 次
- Volume surge 排除开盘旋转条：避免开盘高量污染基线计算

**延伸阅读：** 搜索 "bloom filter pre-screening"、"query predicate pushdown"、"cascade classifier (Viola-Jones)"。

### 4.3 Regime 分类的边界情况——"Directional Trap"

**是什么：** 当量化指标（RVOL）指向一种 regime（FADE_CHOP），但价格行为（单向移动 >1.5%）指向另一种（趋势）时，出现的分类矛盾。

**为什么重要：** 这是量化交易系统中"指标冲突"的经典问题。单纯依赖 RVOL 阈值分类，会在"低量趋势"场景产生致命误判——把缓慢下跌判为震荡，导致逆势交易。实际交易中，"低量 + 持续单向移动"通常意味着卖方主导的有序出货，绝不是该 fade 的场景。

**在本次变更中如何体现：** 在 `classify_us_regime()` 中，FADE_CHOP 判定前插入 directional trap 检测：如果 RVOL 低但开盘以来单向移动 >1.5%，路由到 UNCLEAR + lean 方向，避免生成 fade 计划。

**延伸阅读：** 搜索 "regime detection conflicting indicators"、"low volume drift"、"directional trap day trading"。

### 4.4 防御性数值封顶（Capping）

**是什么：** 对计算结果施加合理的上下限约束，防止极端值产生误导性的决策建议。

**为什么重要：** 在生成交易计划时，SL/TP 来自 `_nearest_levels()` 的自动查找。但 "最近的结构性水平" 可能距离很远（如 Gamma Put Wall 在 -15%），导致 R:R 计算出不现实的大数字（比如 10:1），给用户错误的信心。

**在本次变更中如何体现：**
- `_cap_fade_sl()`：fade 计划的 SL 封顶 2%
- Gamma Wall 距离过滤：>10% 的 wall 不参与 key level 计算
- UNCLEAR fade plan 的 `_MAX_SL_DISTANCE_PCT=1%` 和 `_MIN_FADE_RR=0.8` 门控
- `_MIN_FADE_REWARD_PCT=0.15%`：VWAP 太接近目标 VA edge 时放弃 fade（扣除 spread 后无利可图）

**延伸阅读：** 搜索 "output clamping numerical stability"、"guard clause pattern"、"sanity check trading system"。

## 五、测试建议

### 5.1 建议的测试用例

- **正常场景：**
  - 发送不同 symbol（SPY/AAPL/TSLA）确认 on-demand 查询正常返回
  - 在交易时段观察 auto-scan 是否在 180s 间隔内完成（日志时间戳）
  - 验证 auto-scan alert 消息包含图表（Telegram 先收到图片再收到文字）

- **边界场景：**
  - 测试 per-type 频率控制：触发 1 次 RANGE_REVERSAL 后应被限制，但 BREAKOUT 不受影响
  - 测试 directional trap：低 RVOL + 开盘以来单向跌 2% 的股票不应被判为 FADE_CHOP
  - 测试 Gamma Wall 过滤：Put Wall 在 -20% 的情况下不应出现在 key levels 中

- **异常场景：**
  - 批量 snapshot 预取失败后，L1 应自动 fallback 到单个请求
  - `_l1_result` 为 None 时，L2 应 fallback 到 `_run_analysis_pipeline()`
  - Telegram 代理中断后恢复，bot polling 是否自动重连

### 5.2 测试思路说明

本次改动的核心风险在于 L1→L2 数据复用的 **一致性**——L2 用的是 L1 时刻的 snapshot/bars，如果市场在 L1 和 L2 之间剧烈波动，数据可能过时。目前通过在 L2 中重新分类 regime（带 Gamma Wall）来部分缓解，但 price/RVOL 仍沿用 L1 的值。测试时应关注这个时间差是否导致误判。

## 六、场景扩展

### 6.1 类似场景

1. **多层缓存失效策略：** 类似 L1→L2 数据复用，Web 应用的 L1 Cache / L2 Cache / DB 三层架构中，也需要决定哪些数据可以跨层复用、哪些必须重新验证。设计思路一致：把"计算代价高且短期内不变"的数据缓存传递。

2. **推荐系统的 recall → ranking 管线：** 搜索/推荐系统的"粗排→精排"和本项目的"L1→L2"结构一致：粗排用轻量特征快速筛选候选集，精排对候选集用完整模型打分。数据从粗排传递到精排避免重复计算。

3. **告警分级在 DevOps 监控中的应用：** 本次的信号强度三档分级（极强/强/标准）可以直接迁移到 Prometheus AlertManager 的 severity 分级。通过 confidence × 持续时间的双维度判定，避免所有 alert 都以 critical 发送导致疲劳。

### 6.2 进阶思考

- **L1 数据时效性问题：** 当 scan interval 从 180s 缩短到 60s 甚至 30s 时，L1→L2 的数据复用收益降低（因为 snapshot 更频繁刷新），但频率预检的收益增大。如何根据 interval 动态决定是否复用？
- **Directional trap 的阈值自适应：** 目前写死 1.5%，但高 beta 股票（如 TSLA）日常波动就大，而低 beta 股票（如 SPY）1% 可能就算大移动了。可以考虑用 ATR（Average True Range）的百分位来动态设定阈值。

## 七、总结

本次改动的核心是 **让 auto-scan 更快、更准、更有区分度**。性能方面，L1→L2 数据复用和批量 snapshot 预取减少了重复 API 调用和计算；信号质量方面，directional trap 检测和 fade SL 封顶修复了两个可能导致实际亏损的分类/计划缺陷；用户体验方面，信号强度分级和 auto-scan 图表让 alert 消息更直观可决策。最值得记住的经验：**在多阶段管线中，始终思考"前一阶段的哪些结果可以被下一阶段复用"**，以及 **"数值计算的输出在被用于决策前，必须做合理性封顶"**。
