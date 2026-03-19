# HK 模块适配工作清单

基于 US Playbook v2 重构已完成的特性，HK 模块需要对齐的适配项。

## P0 — 核心对齐（必须）

### 1. Regime 体系升级
- [ ] 将 5 类 regime (GAP_AND_GO/TREND_DAY/FADE_CHOP/WHIPSAW/UNCLEAR) 扩展为 8 类，对齐 US 的 RegimeFamily 分组
  - TREND_DAY → 拆分为 TREND_STRONG / TREND_WEAK
  - FADE_CHOP → 拆分为 RANGE / NARROW_GRIND
  - 新增 V_REVERSAL / GAP_FILL (反转族)
  - GAP_AND_GO 保留 (对应 US 的 GAP_GO)
- [ ] 添加 `RegimeFamily` 枚举到 `src/hk/__init__.py`
- [ ] 更新 `classify_hk_regime()` 的分类逻辑和优先级
- [ ] 更新所有 regime 相关测试

### 2. Regime Stabilizer
- [ ] 为 HK 实现 `src/hk/stabilizer.py`，镜像 `src/us_playbook/stabilizer.py`
  - 迟滞层 (RVOL threshold ± buffer)
  - 时间持续层 (regime 持续时间验证)
  - UNCLEAR 60 分钟超时强制归类
- [ ] 在 `HKPredictor` 中集成 stabilizer（如启用自动扫描）

### 3. Market Tone 引擎
- [ ] 实现 `src/hk/market_tone.py`，适配港股特征：
  - 宏观日历：HKMA/中国 PMI 替代 FOMC/NFP
  - 波动率：恒指 VIX (VHSI) 替代 VIX
  - 市场宽度：HSI/HSTECH 成分股替代 SPY 10 股
  - ORB：港股 09:30-10:00 (30min) 开盘区间
  - 缺口：HSI 缺口替代 SPY 缺口
- [ ] 评级整合到 regime 置信度和 playbook Section 0

### 4. Version Diff 整合
- [x] `version_diff.py` 已在 `src/common/` 中实现
- [ ] 在 `HKPredictor` 中维护 `_playbook_snapshots` 字典
- [ ] 每次 `generate_playbook_for_symbol()` 调用时保存快照
- [ ] playbook 输出中附加 diff 文本（第二次查询起）

### 5. Checklist 整合
- [x] `checklist.py` 已在 `src/common/` 中实现（HK 豁免 RVOL 校正 #8 和相对强度 #9）
- [ ] 在 `HKPredictor.generate_playbook_for_symbol()` 中调用 `validate_checklist()`
- [ ] 将违规项追加到 playbook 末尾

## P1 — 功能增强

### 6. Relative Strength（相对强度）
- [ ] 实现个股 vs HSI 相关性计算
  - 使用 intraday 1min bars 滚动相关系数
  - 脱钩时降低大盘 regime 权重
- [ ] 在 playbook header 展示 RS 状态

### 7. RVOL 开盘校正
- [ ] 港股 09:30-09:45 期间的 RVOL 需用历史同时段校正
  - 参考 US 的 `checklist.py` #8 逻辑
  - 港股交易时段为 330 分钟，需适配早盘窗口

### 8. ATR-based 止损下限
- [x] `action_plan.py` 中的 ATR 止损逻辑已共享
- [ ] 确保 HK playbook 传入正确的 `atr_5min` 到 `PlanContext`
- [ ] 验证 5min ATR 计算逻辑在港股 1min bars 上正确

### 9. 自动扫描升级
- [ ] 对齐 US 的 L1/L2 架构
  - L1: stabilizer 过滤 + 轻量筛选
  - L2: 完整 playbook pipeline 验证
- [ ] 添加 regime transition 检测 (`detect_regime_transition`)
- [ ] 频率控制升级 (session/daily limit + override)

## P2 — 优化项

### 10. Earnings Filter
- [ ] 港股版 earnings 过滤（使用 Futu 财报日历或 yfinance）
- [ ] 财报日阻止交易，财报后次日提升过滤级别

### 11. 配置参数对齐
- [ ] `config/hk_settings.yaml` 添加新 regime 阈值
  - `trend_strong_rvol`、`narrow_grind_rvol`、`narrow_grind_range_ratio`
- [ ] 添加 `market_tone` 配置块
- [ ] 添加 `stabilizer` 配置块（hysteresis_buffer、min_hold_seconds、unclear_timeout）

### 12. 回测框架更新
- [ ] 更新 `src/hk/backtest/evaluators.py` 适配新 regime 分类
- [ ] 添加 stabilizer 回测验证
- [ ] 添加 market tone 准确度评估

## 依赖关系

```
P0-1 (Regime升级) ← P0-2 (Stabilizer) ← P1-9 (自动扫描升级)
P0-3 (Market Tone) ← P2-11 (配置)
P0-4 (Version Diff) — 独立
P0-5 (Checklist) — 独立，依赖 P0-4 完成后效果更好
P1-6 (RS) ← P0-5 (Checklist #9)
P2-12 (回测) ← P0-1 + P0-2 + P0-3
```

## 已完成项（共享模块已就绪）

- [x] ActionPlan 引擎 (`src/common/action_plan.py`) — HK 已使用
- [x] Version Diff (`src/common/version_diff.py`) — 待 HK 集成
- [x] Checklist (`src/common/checklist.py`) — 待 HK 集成，已预设 HK 豁免规则
- [x] ATR-based 止损 (`action_plan.py` 中 `stop_atr_multiple` + `stop_floor_applied`)
- [x] check_regime_consistency — FADE_CHOP 已加入一致性检查
