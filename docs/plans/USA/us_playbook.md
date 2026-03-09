# 美股期权日内 Playbook 实现方案

## Context

当前系统有两个独立模块：US 策略管道（10 个 YAML 策略 + 10s 轮询实时入场/出场信号）和 HK 预测模块（每日 3 次推送剧本）。需要新增一个类似 HK 模块的 **US Playbook** 功能：在美东 09:45 推送今日量化交易剧本，包括 VP 关键点位、盘前高低点、Gamma 墙、交易风格分类和风险过滤器，指导一周内到期的美股期权交易。仅使用 LV1 数据（OHLCV + Option OI，无盘口深度）。

---

## 架构决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 模块位置 | `src/us_playbook/` 独立模块 | 与 HK 同模式；Playbook 是每日一次的摘要，与 10s 策略管道本质不同 |
| 代码复用 | 直接 `from src.hk` 导入纯函数 | `volume_profile` 和 `gamma_wall` 是市场无关的纯函数。零改动零风险；将来有第三个模块再提取 `src/shared/` 不迟 |
| 数据源 | 复用现有 `FutuCollector` 实例 | 避免重复连接和订阅配额消耗 |
| 数据接口 | 在 `FutuCollector` 中新增 `get_history_bars(symbol, days)` 异步方法 | 现有 `get_history()` 的 `max_count=1000` 不足以覆盖 3-5 天 1m bars（≈1200-1950 根），需要更大 max_count（HK 已验证 Futu 支持 max_count 到 5000） |
| 集成方式 | 挂载到 `OptionsMonitor` 的 APScheduler + Telegram Bot | 共享进程，不需要独立启动 |

---

## 文件结构

```
src/us_playbook/                     # 新建：US Playbook 模块
├── __init__.py                      # USRegimeType, KeyLevels, USRegimeResult, USPlaybookResult
├── __main__.py                      # python -m src.us_playbook 独立运行入口
├── main.py                          # USPlaybook 编排器
├── levels.py                        # PDH/PDL, PMH/PML 提取 + VP 关键位汇总 + US tick_size
├── indicators.py                    # RVOL (前 15 分钟), VWAP (连续)
├── regime.py                        # classify_us_regime(): 4 种风格
├── playbook.py                      # generate + format Telegram HTML 消息
├── filter.py                        # FOMC/NFP/CPI, Monthly OpEx, Inside Day + RVOL
└── telegram.py                      # register_us_playbook_commands()

config/us_playbook_settings.yaml     # 新建：US Playbook 配置
config/us_calendar.yaml              # 新建：美股经济日历 (FOMC/NFP/CPI)
tests/test_us_playbook.py            # 新建：单元测试
```

**需修改的现有文件：**
- `src/collector/futu.py` — 新增 `get_history_bars()` + `get_premarket_hl()`，修复 OI bug
- `src/main.py` — 集成 USPlaybook (scheduler + telegram commands)

**复用但不修改的 HK 模块：**
- `src/hk/volume_profile.py` → `calculate_volume_profile()` — US 通过 `tick_size` 参数显式传入 US 专用值
- `src/hk/gamma_wall.py` → `calculate_gamma_wall()`, `format_gamma_wall_message()`
- `src/hk/__init__.py` → `VolumeProfileResult`, `GammaWallResult`, `FilterResult` 类型

---

## 数据类型定义

### `src/us_playbook/__init__.py`

```python
from src.hk import FilterResult, GammaWallResult, VolumeProfileResult  # 共享类型

class USRegimeType(Enum):
    GAP_AND_GO = "gap_and_go"    # Gap + 高 RVOL + PM 突破
    TREND_DAY  = "trend_day"     # 无大 Gap 但持续方向 + 偏高 RVOL
    FADE_CHOP  = "fade_chop"     # 低 RVOL + 区间震荡
    UNCLEAR    = "unclear"       # 混合信号

@dataclass
class KeyLevels:
    poc, vah, val: float         # Volume Profile
    pdh, pdl: float              # Previous Day High/Low
    pmh, pml: float              # Pre-Market High/Low (或 gap high/low)
    vwap: float                  # 当日动态 VWAP
    gamma_call_wall: float = 0   # Call Wall (阻力)
    gamma_put_wall: float = 0    # Put Wall (支撑)
    gamma_max_pain: float = 0    # Max Pain

@dataclass
class USRegimeResult:
    regime: USRegimeType
    confidence: float            # 0-1
    rvol: float
    price: float
    gap_pct: float               # open vs prev_close %
    spy_regime: USRegimeType | None = None
    details: str = ""

@dataclass
class USPlaybookResult:
    symbol, name: str
    regime: USRegimeResult
    key_levels: KeyLevels
    volume_profile: VolumeProfileResult
    gamma_wall: GammaWallResult | None
    filters: FilterResult
    strategy_text: str = ""
    generated_at: datetime | None = None
```

---

## 实现细节

### Phase 0: 验证 Futu 数据可用性

在编码前先运行验证脚本确认：

```python
# 验证脚本 (非提交代码)
ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

# 1. 验证 snapshot 是否包含 pre-market 字段
ret, snap = ctx.get_market_snapshot(["US.SPY"])
print(snap.columns.tolist())   # 检查是否有 pre_high_price / pre_low_price
print(snap.iloc[0].to_dict())  # 打印全部字段

# 2. 验证 get_history max_count > 1000 是否有效
ret, data, key = ctx.request_history_kline("US.SPY", ktype=KLType.K_1M,
    start="2026-03-03", end="2026-03-09", max_count=3000)
print(f"Bars returned: {len(data)}")  # 期望 ~2340 (6 trading days × 390)

# 3. 验证期权 OI 字段
ret, snap = ctx.get_market_snapshot(["US.SPY260313C00560000"])  # 示例期权代码
print(f"option_open_interest: {snap.iloc[0].get('option_open_interest')}")
```

**Phase 0 的结论将影响 Phase 1 的实现**：
- 若 `pre_high_price` 不存在 → PMH/PML 改用 `max(open, prev_close)` / `min(open, prev_close)`（gap 范围）
- 若 `max_count > 1000` 无效 → 改用分页 (`page_req_key`) 或按天分批请求

---

### Phase 1: FutuCollector 增强

#### 1.1 修复 OI Bug (`src/collector/futu.py:348`)

```python
# 现有（错误）— option_area_type 是期权类型枚举 (AMERICAN/EUROPEAN)，不是 OI
open_interest=int(row.get("option_area_type", 0) or 0),

# 修复 — snapshot 数据已在 line 312 获取到 greeks_map，读取正确字段
open_interest=int(g.get("option_open_interest", 0) or 0),
```

此修复同时改善现有 10 个策略的期权数据质量。

#### 1.2 新增 `get_history_bars(symbol, days)`

```python
def _fetch_history_bars(self, symbol: str, days: int, interval: str = "1m") -> pd.DataFrame:
    """按天数获取历史 K 线，自动计算 max_count 确保完整覆盖。"""
    ctx = self._ensure_connected()
    futu_code = to_futu(symbol)
    kl_type = INTERVAL_MAP.get(interval, KLType.K_1M)

    today = datetime.now(ET).date()
    start = (today - timedelta(days=days + 3)).strftime("%Y-%m-%d")  # buffer for weekends
    end = today.strftime("%Y-%m-%d")
    max_count = min(days * 400 + 100, 5000)  # 与 HKCollector 同模式

    ret, data, _ = ctx.request_history_kline(
        futu_code, start=start, end=end, ktype=kl_type, max_count=max_count,
    )
    if ret != RET_OK:
        raise RuntimeError(f"request_history_kline failed: {data}")
    if data.empty:
        return pd.DataFrame()
    return normalize_futu_kline(data)

async def get_history_bars(self, symbol: str, days: int = 5, interval: str = "1m") -> pd.DataFrame:
    df = await self._retry(self._fetch_history_bars, symbol, days, interval)
    logger.debug("History bars %s (%dd %s): %d bars", symbol, days, interval, len(df))
    return df
```

#### 1.3 新增 `get_premarket_hl(symbol)`

```python
def _fetch_premarket_hl(self, symbol: str) -> tuple[float, float]:
    """通过 get_market_snapshot 获取盘前高低点。

    若 Futu 不提供 pre_high_price/pre_low_price 字段，
    回退到 max/min(open_price, prev_close) 作为 gap 范围。
    """
    ctx = self._ensure_connected()
    futu_code = to_futu(symbol)
    ret, data = ctx.get_market_snapshot([futu_code])
    if ret != RET_OK:
        raise RuntimeError(f"get_market_snapshot failed: {data}")

    row = data.iloc[0]

    # 尝试 pre-market 专用字段（Phase 0 验证后确认字段名）
    pmh = float(row.get("pre_high_price", 0) or 0)
    pml = float(row.get("pre_low_price", 0) or 0)

    if pmh > 0 and pml > 0:
        return pmh, pml

    # 回退：用 open vs prev_close 定义 gap 范围
    open_p = float(row.get("open_price", 0) or 0)
    prev_c = float(row.get("prev_close_price", 0) or 0)
    if open_p > 0 and prev_c > 0:
        return max(open_p, prev_c), min(open_p, prev_c)
    return open_p or prev_c, open_p or prev_c

async def get_premarket_hl(self, symbol: str) -> tuple[float, float]:
    return await self._retry(self._fetch_premarket_hl, symbol)
```

---

### Phase 2: 核心计算模块

#### 2.1 `src/us_playbook/indicators.py` — RVOL + VWAP

```python
def calculate_vwap(bars: pd.DataFrame) -> float:
    """当日 VWAP = cumsum(typical_price * volume) / cumsum(volume)"""
    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    cum_tp_vol = (typical * bars["Volume"]).cumsum()
    cum_vol = bars["Volume"].cumsum()
    return float((cum_tp_vol / cum_vol).iloc[-1])

def calculate_us_rvol(today_bars, history_bars, window_minutes=15, lookback_days=10) -> float:
    """前 N 分钟 RVOL = 今日窗口成交量 / 过去 lookback_days 日同窗口均量

    - today_bars: 今日全部 1m bars
    - history_bars: 非今日 bars (多天)
    - window_minutes: 09:30 后多少分钟 (默认 15 → 09:45 ET)
    """
    # 1. 今日：筛选 index < first_bar + window_minutes 的 bars，求 sum(Volume)
    # 2. 历史：按 date 分组，每天取同窗口 sum(Volume)，取最近 lookback_days 天
    # 3. RVOL = today_vol / mean(hist_vols)
```

#### 2.2 `src/us_playbook/levels.py` — 关键点位

**US tick_size 策略**（不同于 HK 的自动检测，显式控制）：

```python
def us_tick_size(avg_price: float) -> float:
    """US 专用 VP price bucket 粒度。

    比 HK 更精细 — US 股票以 $0.01 为最小变动，
    但 VP binning 按价位范围用合理的桶宽。
    """
    if avg_price > 400:   return 0.50    # SPY ~$550
    if avg_price > 100:   return 0.25    # AAPL ~$230, NVDA ~$140
    if avg_price > 20:    return 0.10    # AMD ~$110 (边界)
    return 0.05

def extract_previous_day_hl(bars) -> tuple[float, float]:
    """从 1m bars 提取昨日 regular hours (09:30-16:00 ET) 的 High/Low"""

def get_today_bars(bars) -> pd.DataFrame:
    """筛选今日 bars（America/New_York 时区）"""

def get_history_bars(bars) -> pd.DataFrame:
    """筛选非今日 bars（用于 VP 计算）"""

def compute_volume_profile(history_bars) -> VolumeProfileResult:
    """调用 src.hk.volume_profile.calculate_volume_profile()，传入 US tick_size"""
    avg_price = history_bars["Close"].mean()
    tick = us_tick_size(avg_price)
    return calculate_volume_profile(history_bars, value_area_pct=0.70, tick_size=tick)

def build_key_levels(vp, pdh, pdl, pmh, pml, vwap, gamma=None) -> KeyLevels:
    """汇总所有关键位到一个对象"""
```

#### 2.3 `src/us_playbook/regime.py` — 4 种交易风格

```python
def classify_us_regime(
    price: float,
    prev_close: float,
    rvol: float,
    pmh: float,
    pml: float,
    vp: VolumeProfileResult,
    gamma_wall: GammaWallResult | None = None,
    spy_regime: USRegimeType | None = None,
    gap_and_go_rvol: float = 1.5,
    trend_day_rvol: float = 1.2,
    fade_chop_rvol: float = 1.0,
    is_preliminary: bool = False,      # 09:45 用更宽松阈值
) -> USRegimeResult:
```

| 风格 | 条件 | 交易建议 |
|------|------|---------|
| **GAP_AND_GO** 🚀 | RVOL ≥ 1.5 (preliminary: ≥ 2.0) 且 价格突破 PMH/PML | 顺势：ATM/轻度 OTM (Delta 0.3-0.5)，VWAP 止损 |
| **TREND_DAY** 📈 | RVOL ≥ 1.2 且 \|gap\| < 0.5% 且 价格在 VA 外 | 方向跟随：ATM (Delta 0.4-0.6)，PDH/PDL 止损 |
| **FADE_CHOP** 📦 | RVOL < 1.0 且 价格在 VA 内或触及 Gamma Wall | 均值回归：**严禁 OTM**，深度 ITM (Delta > 0.7) 反向 |
| **UNCLEAR** ❓ | 混合信号 (RVOL 在中性区间，价格/量不匹配) | 观望：等 10:15 确认，仅高确定性机会 |

**SPY 市场背景处理：**
- SPY 先分类，结果作为 `spy_regime` 传入个股
- 若 SPY=FADE_CHOP，降低个股 GAP_AND_GO confidence (-0.2)
- 若 SPY=GAP_AND_GO，提升个股 GAP_AND_GO confidence (+0.1)

**Preliminary（09:45）vs Confirmed（10:15）：**
- 09:45 `is_preliminary=True`：GAP_AND_GO 阈值提高到 2.0（15 分钟 RVOL 噪声大，需更强信号）
- 10:15 `is_preliminary=False`：使用标准阈值 1.5（45 分钟 RVOL 更稳定）

#### 2.4 `src/us_playbook/filter.py` — 过滤器

```python
def check_us_filters(
    rvol: float,
    prev_high: float, prev_low: float,
    current_high: float, current_low: float,
    calendar_path: str = "config/us_calendar.yaml",
    today: date | None = None,
) -> FilterResult:
```

| Filter | 触发条件 | 风险级别 | 说明 |
|--------|---------|---------|------|
| **1. 宏观日历** | FOMC / NFP / CPI 当日 | 🔴 `blocked` | 从 `us_calendar.yaml` 读取，高波动不可预测 |
| **2. Monthly OpEx** | 每月第三个周五 | 🟡 `elevated` | **非 blocked** — OpEx 日高成交量，有些策略可操作 |
| **3. Inside Day + 低 RVOL** | H ≤ prev_H 且 L ≥ prev_L 且 RVOL < 0.8 | 🔴 `blocked` | 收敛 + 低量 = 假突破概率极高 |
| **2+3 叠加** | OpEx + Inside Day + RVOL < 0.8 | 🔴 `blocked` | 三因素叠加才升级为阻断 |

```python
def _is_monthly_opex(d: date) -> bool:
    """第三个周五: weekday==4 且 15<=day<=21，自动计算无需维护日历"""
    return d.weekday() == 4 and 15 <= d.day <= 21
```

---

### Phase 3: Playbook 生成与格式化

#### `src/us_playbook/playbook.py`

```python
def format_us_playbook_message(result: USPlaybookResult, update_type="morning") -> str:
    """生成 Telegram HTML 消息

    update_type:
      "morning" → 标注 "⚠️ 初步" (09:45, 15 分钟数据)
      "confirm" → 标注 "✅ 确认" (10:15, 45 分钟数据)
    """
```

**消息结构（4 个板块）：**

```
━━━ 🇺🇸 AAPL Playbook ⚠️初步 ━━━

📊 【大盘环境】
SPY: 📦 震荡日 (RVOL 0.82)
QQQ: 📦 震荡日 (RVOL 0.91)

🎯 AAPL — 📈 趋势日 (置信度 ████░░ 72%)
RVOL: 1.35 | Gap: +0.42%

📍 【关键点位】(价格降序)
  Call Wall   562.0  (OI=12,345)
  PDH         558.3
  VAH         556.5
  VWAP        554.2  ← current
  POC         553.0
  VAL         550.5
  PDL         548.7
  Put Wall    545.0  (OI=8,910)
  Max Pain    550.0

📋 【交易建议】
📈 趋势日 — 方向跟随
• ATM 期权 (Delta 0.4-0.6)
• PDH 558.3 为止损线
• 目标 VAH 556.5 → Call Wall 562.0

⚡ 【风险过滤】
🟢 今日无宏观事件
🟡 月度期权到期日 (OpEx) — 注意尾盘波动
```

---

### Phase 4: 编排器

#### `src/us_playbook/main.py` — `USPlaybook` 类

```python
class USPlaybook:
    def __init__(self, config: dict, collector: FutuCollector):
        self._cfg = config
        self._collector = collector
        self._send_fn: Callable | None = None
        self._last_playbooks: dict[str, USPlaybookResult] = {}
        self._executor = ThreadPoolExecutor(max_workers=1)

    def set_send_fn(self, fn: Callable) -> None:
        """设置 Telegram 发送回调 (async function)"""
        self._send_fn = fn

    async def run_playbook_cycle(self, update_type: str = "morning") -> None:
        """遍历 watchlist，为每个标的生成 playbook 并推送。

        1. 先跑 market_context_symbols (SPY/QQQ) → 获取 spy_regime
        2. 遍历个股 → 使用 spy_regime 作为 context
        3. 每个标的间隔 1s（K-line API 限频）
        """

    async def _run_single_symbol(
        self, symbol: str, name: str, update_type: str, spy_regime: USRegimeType | None
    ) -> USPlaybookResult | None:
        """单标的 pipeline：

        1. get_history_bars(symbol, days=5) → 分离 history/today bars
        2. history bars → VP (with US tick_size)
        3. history bars → extract PDH/PDL
        4. get_premarket_hl(symbol) → PMH/PML
        5. today bars → VWAP + RVOL
        6. get_option_chain(symbol, this_week_expiry) → Gamma Wall
           ⚡ try/except: 失败时跳过 Gamma Wall 板块，其他正常推送
        7. check_us_filters() → FilterResult
        8. classify_us_regime(is_preliminary = update_type == "morning")
        9. format_us_playbook_message() → 推送
        """

    # ── Bot command helpers ──

    async def get_playbook_text(self, symbol: str | None = None) -> str: ...
    async def get_levels_text(self, symbol: str | None = None) -> str: ...
    async def get_regime_text(self, symbol: str | None = None) -> str: ...
    async def get_filters_text(self) -> str: ...
    async def get_gamma_text(self, symbol: str | None = None) -> str: ...
```

**Gamma Wall graceful 降级：**

```python
# _run_single_symbol() 中
gamma_wall = None
try:
    expiry = self._this_week_friday()
    options = await self._collector.get_option_chain(symbol, expiration=expiry)
    if options:
        chain_df = pd.DataFrame([
            {"option_type": o.option_type.upper(), "strike_price": o.strike,
             "open_interest": o.open_interest}
            for o in options
        ])
        gamma_wall = calculate_gamma_wall(chain_df, current_price)
except Exception:
    logger.warning("Gamma wall fetch failed for %s, skipping", symbol)
    # gamma_wall 保持 None，playbook 正常推送其他板块
```

---

### Phase 5: Telegram 命令

#### `src/us_playbook/telegram.py`

使用 `/us_*` 前缀，与 `/hk_*` 对称：

| 命令 | 别名 | 功能 |
|------|------|------|
| `/us_playbook [symbol]` | `/uspb` | 手动触发 playbook (默认 SPY) |
| `/us_levels [symbol]` | `/usl` | 关键点位 (VP + PDH/PDL + Gamma) |
| `/us_regime [symbol]` | `/usr` | 风格分类 + confidence + 策略建议 |
| `/us_filters` | `/usf` | 过滤器状态 (FOMC/OpEx/Inside Day) |
| `/us_gamma [symbol]` | `/usg` | Gamma Wall + Max Pain |
| `/us_help` | `/ush` | 命令帮助 |

```python
def register_us_playbook_commands(application, us_playbook: USPlaybook) -> None:
    """注册到现有 Telegram Application，与 HK 命令共存。"""
    application.bot_data["us_playbook"] = us_playbook
    # add_handler for each command...
```

---

### Phase 6: 集成到 `src/main.py`

```python
# OptionsMonitor.__init__():
pb_cfg = self._load_us_playbook_config()
if pb_cfg:
    from src.us_playbook.main import USPlaybook
    self.us_playbook = USPlaybook(pb_cfg, self.collector)
else:
    self.us_playbook = None

# OptionsMonitor.start():
if self.us_playbook:
    self.us_playbook.set_send_fn(self.notifier.send_text)
    from src.us_playbook.telegram import register_us_playbook_commands
    register_us_playbook_commands(self.notifier._app, self.us_playbook)

# OptionsMonitor._register_jobs():
if self.us_playbook:
    from apscheduler.triggers.cron import CronTrigger
    self.scheduler.add_job(
        self.us_playbook.run_playbook_cycle,
        CronTrigger(hour=9, minute=45, day_of_week="mon-fri", timezone="America/New_York"),
        kwargs={"update_type": "morning"}, id="us_playbook_morning",
    )
    self.scheduler.add_job(
        self.us_playbook.run_playbook_cycle,
        CronTrigger(hour=10, minute=15, day_of_week="mon-fri", timezone="America/New_York"),
        kwargs={"update_type": "confirm"}, id="us_playbook_confirm",
    )
```

---

### Phase 7: 配置文件

#### `config/us_playbook_settings.yaml`

```yaml
watchlist:
  - {symbol: SPY, name: "S&P 500 ETF"}
  - {symbol: QQQ, name: "Nasdaq 100 ETF"}
  - {symbol: AAPL, name: Apple}
  - {symbol: TSLA, name: Tesla}
  - {symbol: NVDA, name: NVIDIA}
  - {symbol: META, name: Meta}
  - {symbol: AMD, name: AMD}
  - {symbol: AMZN, name: Amazon}

volume_profile:
  lookback_days: 3
  value_area_pct: 0.70

rvol:
  window_minutes: 15
  lookback_days: 10

regime:
  gap_and_go_rvol: 1.5           # confirmed (10:15) 阈值
  gap_and_go_rvol_preliminary: 2.0  # preliminary (09:45) 更宽松
  trend_day_rvol: 1.2
  fade_chop_rvol: 1.0
  market_context_symbols: [SPY, QQQ]

playbook:
  push_times: ["09:45", "10:15"]
  timezone: "America/New_York"

filters:
  calendar_file: "config/us_calendar.yaml"
  inside_day_rvol_threshold: 0.8

gamma_wall:
  enabled: true
  use_weekly_expiry: true
```

#### `config/us_calendar.yaml`

```yaml
# 美股宏观经济日历
# risk_level: high (FOMC/NFP/CPI → 🔴 blocked), medium (其他 → 🟡 elevated)
# Monthly OpEx (每月第三个周五) 自动计算，不需静态配置

events:
  # 2026 FOMC 会议 (高风险)
  - {date: "2026-01-28", name: "FOMC Meeting", risk_level: high}
  - {date: "2026-03-18", name: "FOMC Meeting", risk_level: high}
  - {date: "2026-05-06", name: "FOMC Meeting", risk_level: high}
  - {date: "2026-06-17", name: "FOMC Meeting", risk_level: high}
  - {date: "2026-07-29", name: "FOMC Meeting", risk_level: high}
  - {date: "2026-09-16", name: "FOMC Meeting", risk_level: high}
  - {date: "2026-11-04", name: "FOMC Meeting", risk_level: high}
  - {date: "2026-12-16", name: "FOMC Meeting", risk_level: high}

  # 2026 非农就业 (高风险 — 每月第一个周五)
  - {date: "2026-01-09", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-02-06", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-03-06", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-04-03", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-05-08", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-06-05", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-07-02", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-08-07", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-09-04", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-10-02", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-11-06", name: "Non-Farm Payroll", risk_level: high}
  - {date: "2026-12-04", name: "Non-Farm Payroll", risk_level: high}

  # 2026 CPI (高风险 — 通常每月 10-15 日)
  - {date: "2026-01-14", name: "CPI Release", risk_level: high}
  - {date: "2026-02-11", name: "CPI Release", risk_level: high}
  - {date: "2026-03-11", name: "CPI Release", risk_level: high}
  - {date: "2026-04-10", name: "CPI Release", risk_level: high}
  - {date: "2026-05-12", name: "CPI Release", risk_level: high}
  - {date: "2026-06-10", name: "CPI Release", risk_level: high}
  - {date: "2026-07-14", name: "CPI Release", risk_level: high}
  - {date: "2026-08-12", name: "CPI Release", risk_level: high}
  - {date: "2026-09-11", name: "CPI Release", risk_level: high}
  - {date: "2026-10-13", name: "CPI Release", risk_level: high}
  - {date: "2026-11-10", name: "CPI Release", risk_level: high}
  - {date: "2026-12-10", name: "CPI Release", risk_level: high}

  # 美股休市日
  - {date: "2026-01-01", name: "New Year's Day", risk_level: high}
  - {date: "2026-01-19", name: "MLK Day", risk_level: high}
  - {date: "2026-02-16", name: "Presidents' Day", risk_level: high}
  - {date: "2026-04-03", name: "Good Friday", risk_level: high}
  - {date: "2026-05-25", name: "Memorial Day", risk_level: high}
  - {date: "2026-06-19", name: "Juneteenth", risk_level: high}
  - {date: "2026-07-03", name: "Independence Day (observed)", risk_level: high}
  - {date: "2026-09-07", name: "Labor Day", risk_level: high}
  - {date: "2026-11-26", name: "Thanksgiving", risk_level: high}
  - {date: "2026-12-25", name: "Christmas", risk_level: high}
```

---

### Phase 8: 测试

#### `tests/test_us_playbook.py`

参照 `tests/test_hk.py` 模式，使用合成 bar 数据：

```python
def _make_bars(data: list[tuple[str, float, float, float, float, int]]) -> pd.DataFrame:
    """创建测试用 bar 数据。

    data: [(datetime_str, open, high, low, close, volume), ...]
    Returns DataFrame with DatetimeIndex (America/New_York)
    """

class TestVWAP:
    def test_basic_vwap(self): ...
    def test_empty_bars(self): ...

class TestRVOL:
    def test_normal_rvol(self): ...
    def test_high_rvol(self): ...
    def test_no_history(self): ...

class TestKeyLevels:
    def test_pdh_pdl_extraction(self): ...
    def test_us_tick_size(self): ...
    def test_volume_profile_integration(self): ...

class TestUSRegime:
    def test_gap_and_go(self): ...
    def test_trend_day(self): ...
    def test_fade_chop(self): ...
    def test_unclear(self): ...
    def test_spy_context_reduces_confidence(self): ...
    def test_preliminary_wider_threshold(self): ...

class TestUSFilters:
    def test_fomc_day_blocked(self): ...
    def test_monthly_opex_elevated(self): ...
    def test_opex_plus_inside_day_low_rvol_blocked(self): ...
    def test_inside_day_low_rvol_blocked(self): ...
    def test_normal_day(self): ...
    def test_is_monthly_opex(self): ...

class TestPlaybookFormat:
    def test_message_contains_all_sections(self): ...
    def test_preliminary_label(self): ...
    def test_confirmed_label(self): ...
```

---

## 实现顺序

| 阶段 | 内容 | 关键验证 |
|------|------|---------|
| **Phase 0** | 运行验证脚本确认 Futu snapshot 字段、max_count、OI | 确定 PMH/PML 方案 |
| **Phase 1** | FutuCollector: OI fix + `get_history_bars()` + `get_premarket_hl()` | 现有 `pytest tests/` 不回归 |
| **Phase 2** | `__init__.py` + `indicators.py` + `levels.py` | 单元测试通过 |
| **Phase 3** | `regime.py` + `filter.py` | 单元测试通过 |
| **Phase 4** | `playbook.py` (格式化) + `main.py` (编排器) | 能生成完整消息文本 |
| **Phase 5** | `telegram.py` + `__main__.py` + `src/main.py` 集成 | Telegram 命令可用 |
| **Phase 6** | 配置文件 + 完整测试 + 端到端验证 | `pytest tests/test_us_playbook.py -v` 全绿 |

---

## 关键风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| Futu snapshot 无 pre-market 字段 | PMH/PML 精度降低 | 回退到 gap 范围 `(max(open,prev_close), min(open,prev_close))` |
| `max_count > 1000` 不生效 | 3 天 VP 数据不完整 | 改用分页 (`page_req_key`) 或按天分批请求 |
| 期权链 + snapshot 批量请求触发频控 | Gamma Wall 获取失败 | try/except graceful 降级：跳过 Gamma 板块，其他正常推送 |
| 09:45 仅 15 分钟数据，RVOL 噪声大 | 错误分类为 GAP_AND_GO | 09:45 用更高阈值 (2.0)；标注"初步"；10:15 确认更新 |
| Futu US/HK 共享 300 订阅配额 | 新增 K 线请求占用配额 | `request_history_kline` 是 REST 请求不占推送配额；期权链批量查询间隔 1s |
