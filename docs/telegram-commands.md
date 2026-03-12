# Telegram Bot Commands Reference

## US Playbook Commands

US 每日交易剧本命令，由 `register_us_predictor_handlers()` 注册（`src/us_playbook/telegram.py`）。

### Playbook 查询

| 输入 | 功能 |
|------|------|
| `SPY` / `AAPL` / `TSLA` | 直接生成该标的完整 US Playbook |
| `+AAPL Apple` | 添加标的到 US watchlist，名称可选 |
| `-AAPL` | 从 US watchlist 删除标的 |
| `uswl` | 查看当前 US watchlist |
| `/us_help` | 查看使用说明 |

### 斜杠命令

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/us_playbook` | `/uspb` | `[symbol]` | 生成 US Playbook（Regime + VP 关键点位 + Gamma Wall + 策略建议） |
| `/us_levels` | `/usl` | `[symbol]` | 关键点位：POC / VAH / VAL / VWAP / PDH / PDL / Gamma Wall |
| `/us_regime` | `/usr` | `[symbol]` | Regime 分类（GAP_AND_GO / TREND_DAY / FADE_CHOP / UNCLEAR） |
| `/us_filters` | `/usf` | — | 风险过滤状态（FOMC / NFP / CPI / OpEx / Inside Day） |
| `/us_gamma` | `/usg` | `[symbol]` | Gamma Wall：Call Wall / Put Wall / Max Pain |
| `/us_help` | `/ush` | — | 显示 US Playbook 全部指令列表与别名 |

### 参数说明

- `[symbol]` — 可选参数，默认为 `SPY`
- Watchlist 标的：SPY, QQQ, AAPL, TSLA, NVDA, META, AMD, AMZN

---

## HK Playbook Commands

HK 市场预测命令，由 `register_hk_predictor_handlers()` 注册（`src/hk/telegram.py`）。

### Playbook 查询

| 输入 | 功能 |
|------|------|
| `09988` / `HK09988` / `HK.09988` | 直接生成该标的完整 HK Playbook |
| `+09988 阿里巴巴` | 添加标的到 HK watchlist，名称可选 |
| `-09988` | 从 HK watchlist 删除标的 |
| `wl` | 查看当前 HK watchlist |
| `/hk_help` | 查看使用说明 |

### 斜杠命令

| 命令 | 短别名 | 参数 | 说明 |
|------|--------|------|------|
| `/hk_playbook` | `/hkpb` | `[symbol]` | 重新生成 Playbook（Regime + 关键点位 + 策略建议） |
| `/hk_levels` | `/hkl` | `[symbol]` | 关键点位：POC / VAH / VAL / VWAP + 价格位置 |
| `/hk_regime` | `/hkr` | `[symbol]` | 当前 Regime 分类（GAP_AND_GO / TREND_DAY / FADE_CHOP / WHIPSAW / UNCLEAR） |
| `/hk_filters` | `/hkf` | `[symbol]` | 交易过滤状态（成交额、RVOL、Inside Day、日历事件等） |
| `/hk_gamma` | `/hkg` | `[symbol]` | Gamma Wall：Call Wall / Put Wall / Max Pain（仅指数） |
| `/hk_orderbook` | `/hkob` | `[symbol]` | LV2 盘口快照 + 大单检测 |
| `/hk_help` | `/hkh` | — | 显示 HK 全部指令列表与别名 |

### 参数说明

- `[symbol]` — 可选参数，默认为主指数 `HK.800000`
- `<symbol>` — 必填参数，格式如 `HK.00700`、`HK.800000`

---

## 通用命令

| 命令 | 功能 |
|------|------|
| `/kb` 或 `/start` | 显示 US + HK 合并快捷查询键盘 |
| `/kboff` | 关闭快捷键盘 |
| `/messages` | 查看上一交易日消息归档 |

---

## Quick Reference

### US Playbook 命令速查

```
SPY / AAPL       Playbook 查询
+AAPL Apple      添加到 watchlist
-AAPL            从 watchlist 移除
uswl             查看 watchlist
/uspb [symbol]   Playbook        = /us_playbook
/usl  [symbol]   关键点位        = /us_levels
/usr  [symbol]   Regime 分类     = /us_regime
/usf             过滤状态        = /us_filters
/usg  [symbol]   Gamma Wall      = /us_gamma
/ush             帮助            = /us_help
```

### HK 命令速查

```
09988 / HK09988  Playbook 查询
+09988 阿里巴巴  添加到 watchlist
-09988           从 watchlist 移除
wl               查看 watchlist
/hkpb [symbol]   Playbook        = /hk_playbook
/hkl  [symbol]   关键点位        = /hk_levels
/hkr  [symbol]   Regime 分类     = /hk_regime
/hkf  [symbol]   过滤状态        = /hk_filters
/hkob [symbol]   LV2 盘口        = /hk_orderbook
/hkg  [symbol]   Gamma Wall      = /hk_gamma
/hkh             帮助            = /hk_help
```
