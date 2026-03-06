> **已归档** — 本文档为 MVP 阶段的实现规划，系统已演进至 Futu + Yahoo 双数据源、10 策略、回测框架的当前架构。仅供历史参考。

# MVP 实现方案：Yahoo Finance + 云 VPS 跑通全链路

---

## 一、Yahoo Finance 数据源现实评估

### 1.1 可用方案对比

| 方案 | 实时性 | 期权数据 | 限流 | 稳定性 | 推荐 |
|------|--------|----------|------|--------|------|
| **`yfinance` (Python)** | ~15秒延迟（轮询） | ✅ 完整期权链 + Greeks | 无官方限制，实测 ~2000次/小时 | 中等（Yahoo 偶尔改接口） | ⭐ MVP 首选 |
| `yahooquery` | 类似 yfinance | ✅ | 类似 | 中等 | 备选 |
| Yahoo Finance v8 API (直接 HTTP) | ~15秒延迟 | ✅ | 更灵活 | 需自行维护 | 进阶选择 |
| Finnhub 免费层 | 实时（股票）/ 无期权 | ❌ 免费层无期权 | 60次/分钟 | 高 | 股票补充 |
| Alpha Vantage 免费层 | 15分钟延迟 | ✅ 基础期权 | 25次/天 | 高 | 太慢，不推荐 |

### 1.2 yfinance 能拿到什么

```python
import yfinance as yf

# 股票实时报价（实际延迟 ~15秒）
ticker = yf.Ticker("AAPL")
info = ticker.fast_info
print(info.last_price)      # 最新价
print(info.previous_close)  # 前收

# 期权链（完整）
expirations = ticker.options           # 所有到期日
chain = ticker.option_chain("2025-03-21")  # 指定到期日

# chain.calls / chain.puts 包含：
# contractSymbol, lastTradeDate, strike, lastPrice,
# bid, ask, change, percentChange, volume, openInterest,
# impliedVolatility, inTheMoney

# 历史 K 线（分钟级）
hist = ticker.history(period="1d", interval="1m")  # 当日 1 分钟 K 线
# 返回: Open, High, Low, Close, Volume
```

### 1.3 关键限制（必须清楚）

| 限制 | 影响 | MVP 阶段应对 |
|------|------|-------------|
| **数据延迟 ~15秒** | 不适合追求秒级精度的策略 | MVP 够用——验证策略逻辑而非极致时效 |
| **无 WebSocket 推送** | 只能轮询，不能事件驱动 | 每 5-10 秒轮询一次，完全可行 |
| **期权 Greeks 精度一般** | IV/Delta 等由 Yahoo 计算，非交易所级 | MVP 阶段够用，后续切 IBKR 精度自动提升 |
| **无官方 SLA** | Yahoo 可能随时改接口导致中断 | yfinance 社区维护活跃，通常几天内修复 |
| **被限流风险** | 请求过于频繁会 429 | 控制在合理频率，加指数退避 |

**结论**：对 MVP 来说，yfinance 是"刚好够用"的数据源。它的延迟和精度问题不影响你验证以下核心目标：

1. ✅ 数据采集 → Redis 管道是否畅通
2. ✅ 指标计算逻辑是否正确
3. ✅ 策略匹配引擎是否按预期触发
4. ✅ Telegram 通知是否及时送达
5. ✅ 策略配置热更新是否生效

---

## 二、VPS 选型

### 2.1 推荐方案

| 云厂商 | 方案 | 规格 | 月费 | 推荐理由 |
|--------|------|------|------|----------|
| **阿里云（轻量应用服务器-海外）** | 新加坡/美西 | 2C/2G/60G SSD | ¥24-34/月 | 国内备案主体可用，延迟可控，有中文支持 |
| **AWS Lightsail** | us-west-2 (俄勒冈) | 2C/2G/60G SSD | $10/月 | 稳定，全球可达 |
| **腾讯云（轻量-海外）** | 硅谷/新加坡 | 2C/2G/60G SSD | ¥30-40/月 | 与阿里云类似 |
| **Vultr / RackNerd** | 美西洛杉矶 | 2C/2G/40G SSD | $5-12/月 | 便宜，但无中文支持 |

### 2.2 选型建议

**MVP 阶段推荐：阿里云轻量（海外-新加坡或硅谷）或 AWS Lightsail**

原因：
- **阿里云海外轻量**：国内控制台管理方便，¥24/月起步，中文文档齐全。新加坡节点到 Yahoo Finance 服务器延迟约 150ms，完全满足轮询需求。
- **AWS Lightsail**：$10/月，美西节点到 Yahoo/IBKR 延迟更低（~30ms），且未来切换 IBKR 时网络更优。

> 注意：阿里云**国内**节点不推荐——部分境外 API（Yahoo Finance、Telegram）可能无法直接访问。一定要选**海外**节点。

---

## 三、MVP 技术架构

### 3.1 技术选型

MVP 阶段追求**快速实现**，全部用 Python 统一技术栈：

| 模块 | 技术选择 | 理由 |
|------|----------|------|
| 语言 | **Python 3.11+** | yfinance 原生 Python，生态丰富，开发速度快 |
| 数据采集 | **yfinance** + **httpx**(备用) | 成熟稳定 |
| 缓存/消息 | **Redis** | Pub/Sub + 缓存二合一 |
| 指标计算 | **pandas-ta** 或 **ta-lib** | 技术指标库，覆盖 RSI/MACD/VWAP 等 |
| 持久化 | **SQLite** | MVP 够用，零运维，后续可换 PostgreSQL |
| 定时/调度 | **APScheduler** | Python 原生调度器 |
| Telegram | **python-telegram-bot** | 官方维护，异步支持 |
| 配置管理 | **YAML 文件** + **watchdog** | 文件变更自动热加载 |
| 进程管理 | **supervisord** 或 **systemd** | 崩溃自动重启 |

### 3.2 系统架构图

```
┌─────────────────────────────────────────────────────────┐
│                   VPS (阿里云海外 / AWS)                   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Python 主进程                         │   │
│  │                                                  │   │
│  │  ┌────────────┐    ┌─────────────────────────┐   │   │
│  │  │ Scheduler  │    │  DataCollector           │   │   │
│  │  │ (APSched)  │───▶│  每 5s 轮询 yfinance     │   │   │
│  │  └────────────┘    │  标准化 → 写 Redis        │   │   │
│  │                    └────────────┬────────────┘   │   │
│  │                                 │ publish        │   │
│  │                                 ▼                │   │
│  │                         ┌──────────────┐         │   │
│  │                         │    Redis     │         │   │
│  │                         │  (Pub/Sub +  │         │   │
│  │                         │   Cache)     │         │   │
│  │                         └──────┬───────┘         │   │
│  │                                │ subscribe       │   │
│  │                                ▼                │   │
│  │  ┌─────────────────────────────────────────┐    │   │
│  │  │         StrategyEngine                  │    │   │
│  │  │                                         │    │   │
│  │  │  1. IndicatorCalc (pandas-ta)           │    │   │
│  │  │     RSI / MACD / EMA / VWAP / ATR ...   │    │   │
│  │  │                                         │    │   │
│  │  │  2. RuleMatcher                         │    │   │
│  │  │     加载 YAML 策略 → 条件求值            │    │   │
│  │  │                                         │    │   │
│  │  │  3. StateManager                        │    │   │
│  │  │     WATCHING / TRIGGERED / HOLDING      │    │   │
│  │  └──────────────────┬──────────────────────┘    │   │
│  │                     │ 触发                       │   │
│  │                     ▼                           │   │
│  │  ┌─────────────────────────────────────────┐    │   │
│  │  │       NotificationService               │    │   │
│  │  │       python-telegram-bot               │    │   │
│  │  │       去重 / 限流 / 格式化               │    │   │
│  │  └─────────────────────────────────────────┘    │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐     │
│  │  Redis   │  │  SQLite  │  │ strategies/*.yaml │     │
│  │ (docker) │  │  (.db)   │  │ (策略配置文件)      │     │
│  └──────────┘  └──────────┘  └───────────────────┘     │
│                                                         │
└─────────────────────────────────────────────────────────┘
                         │
                   Telegram Bot API
                         │
                         ▼
                    📱 你的手机
```

### 3.3 项目目录结构

```
options-monitor/
├── config/
│   ├── settings.yaml          # 全局配置（Redis地址、Telegram Token等）
│   └── strategies/
│       ├── aapl_rsi_bounce.yaml
│       ├── spy_macd_cross.yaml
│       └── tsla_iv_surge.yaml
│
├── src/
│   ├── __init__.py
│   ├── main.py                # 入口：初始化各模块，启动调度器
│   │
│   ├── collector/
│   │   ├── __init__.py
│   │   ├── base.py            # DataCollector 抽象基类
│   │   ├── yahoo.py           # YahooCollector（yfinance 实现）
│   │   └── ibkr.py            # IBKRCollector（未来替换，预留接口）
│   │
│   ├── indicator/
│   │   ├── __init__.py
│   │   └── engine.py          # 指标计算引擎（pandas-ta 封装）
│   │
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── loader.py          # YAML 策略加载 + watchdog 热更新
│   │   ├── matcher.py         # 规则匹配引擎
│   │   └── state.py           # 状态机管理
│   │
│   ├── notification/
│   │   ├── __init__.py
│   │   └── telegram.py        # Telegram Bot 通知 + 交互命令
│   │
│   ├── store/
│   │   ├── __init__.py
│   │   ├── redis_store.py     # Redis 读写封装
│   │   └── sqlite_store.py    # SQLite 日志/历史持久化
│   │
│   └── utils/
│       ├── __init__.py
│       └── logger.py          # 日志配置
│
├── docker-compose.yaml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## 四、核心模块实现要点

### 4.1 数据采集器（可替换设计）

```python
# src/collector/base.py — 抽象基类，确保未来可无缝切换数据源
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class StockQuote:
    symbol: str
    price: float
    bid: float
    ask: float
    volume: int
    timestamp: float  # Unix timestamp

@dataclass
class OptionQuote:
    contract_symbol: str
    underlying: str
    strike: float
    option_type: str     # 'call' / 'put'
    expiration: str      # 'YYYY-MM-DD'
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_volatility: float
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    timestamp: float

class BaseCollector(ABC):
    """所有数据源都实现这个接口，切换数据源只需换实现类"""

    @abstractmethod
    async def get_stock_quote(self, symbol: str) -> StockQuote: ...

    @abstractmethod
    async def get_option_chain(self, symbol: str,
                                expiration: str | None = None
                                ) -> list[OptionQuote]: ...

    @abstractmethod
    async def get_history(self, symbol: str,
                          interval: str = "1m",
                          period: str = "1d") -> "pd.DataFrame": ...
```

```python
# src/collector/yahoo.py — yfinance 实现
import yfinance as yf
from .base import BaseCollector, StockQuote, OptionQuote

class YahooCollector(BaseCollector):

    async def get_stock_quote(self, symbol: str) -> StockQuote:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        return StockQuote(
            symbol=symbol,
            price=info.last_price,
            bid=getattr(info, 'bid', 0.0),
            ask=getattr(info, 'ask', 0.0),
            volume=info.last_volume or 0,
            timestamp=time.time()
        )

    async def get_option_chain(self, symbol, expiration=None):
        ticker = yf.Ticker(symbol)
        if expiration is None:
            expiration = ticker.options[0]  # 最近到期日
        chain = ticker.option_chain(expiration)
        results = []
        for _, row in chain.calls.iterrows():
            results.append(OptionQuote(
                contract_symbol=row['contractSymbol'],
                underlying=symbol,
                strike=row['strike'],
                option_type='call',
                expiration=expiration,
                bid=row.get('bid', 0),
                ask=row.get('ask', 0),
                last=row.get('lastPrice', 0),
                volume=int(row.get('volume', 0) or 0),
                open_interest=int(row.get('openInterest', 0) or 0),
                implied_volatility=row.get('impliedVolatility', 0),
                delta=None,  # Yahoo 不直接提供 delta
                gamma=None, theta=None, vega=None,
                timestamp=time.time()
            ))
        # 同理处理 chain.puts ...
        return results
```

```python
# src/collector/ibkr.py — 未来替换，同一接口
class IBKRCollector(BaseCollector):
    """
    未来接入 IBKR TWS API 时实现此类。
    由于继承 BaseCollector，上层代码零改动。
    """
    pass
```

**关键点**：通过 `BaseCollector` 抽象，main.py 中只需改一行配置即可切换数据源：

```yaml
# config/settings.yaml
data_source: yahoo    # 改为 ibkr 即切换
```

```python
# main.py
if config['data_source'] == 'yahoo':
    collector = YahooCollector()
elif config['data_source'] == 'ibkr':
    collector = IBKRCollector(config['ibkr'])
```

### 4.2 策略配置（YAML）

```yaml
# config/strategies/aapl_rsi_bounce.yaml
strategy_id: "aapl-rsi-bounce"
name: "AAPL RSI 超卖反弹"
enabled: true

watchlist:
  underlyings: ["AAPL"]
  option_filter:
    type: "call"
    max_dte: 7
    moneyness: "ATM"      # ATM / ITM / OTM
    min_volume: 100
    max_spread_pct: 0.15  # bid-ask spread < 15%

entry_conditions:
  operator: "AND"
  rules:
    - indicator: "RSI"
      params: { period: 14 }
      field: "value"
      comparator: "crosses_above"
      threshold: 30
      timeframe: "5m"

    - indicator: "MACD"
      params: { fast: 12, slow: 26, signal: 9 }
      field: "histogram"
      comparator: "turns_positive"
      timeframe: "5m"

exit_conditions:
  operator: "OR"
  rules:
    - type: "take_profit_pct"
      threshold: 0.50         # 期权涨 50%

    - type: "stop_loss_pct"
      threshold: -0.20        # 期权跌 20%

    - type: "time_exit"
      minutes_before_close: 15

notification:
  cooldown_seconds: 120
  priority: "high"
```

### 4.3 轮询调度（核心循环）

```python
# src/main.py — 主循环逻辑（简化示意）
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

# 股票报价：每 5 秒
@scheduler.scheduled_job('interval', seconds=5, id='stock_quotes')
async def poll_stock_quotes():
    for symbol in watchlist.get_symbols():
        quote = await collector.get_stock_quote(symbol)
        await redis_store.publish_quote(quote)

# 期权链：每 15 秒（期权变化相对慢，降低请求频率）
@scheduler.scheduled_job('interval', seconds=15, id='option_chains')
async def poll_option_chains():
    for symbol in watchlist.get_symbols():
        chain = await collector.get_option_chain(symbol)
        await redis_store.publish_options(symbol, chain)

# 指标计算 + 策略匹配：每次收到新数据时触发
async def on_new_data(channel, data):
    indicators = indicator_engine.calculate(data)
    signals = strategy_matcher.evaluate(indicators)
    for signal in signals:
        await notifier.send(signal)

# 策略配置热更新：监听文件变化
watcher = StrategyFileWatcher("config/strategies/")
watcher.on_change(strategy_matcher.reload)
```

### 4.4 轮询频率与限流安全

```
Yahoo Finance 经验限流阈值: ~2000 请求/小时

你的请求量估算（监控 5 只标的）：
  股票报价:   5 symbols × 12/min = 60/min
  期权链:     5 symbols × 4/min  = 20/min
  历史K线:    5 symbols × 1/min  = 5/min
  ─────────────────────────────────
  合计:       85/min = 5,100/hour

⚠️ 超出安全阈值！需要优化：
```

**优化后的轮询策略**：

```
股票报价:   5 symbols × 6/min (每10秒)  = 30/min
期权链:     5 symbols × 2/min (每30秒)  = 10/min
历史K线:    5 symbols × 1/5min          = 1/min
──────────────────────────────────────────
合计:       41/min ≈ 2,460/hour → 仅略高

进一步优化:
  - 只在交易时段轮询（盘前30min ~ 收盘）
  - 非活跃标的降频（无活跃策略的标的 30秒/次）
  - 加请求缓存（同一秒内多个策略查同一标的，只请求一次）

最终安全目标: ~1,500 请求/小时，留足余量
```

### 4.5 Telegram 通知格式

```python
# src/notification/telegram.py
ENTRY_SIGNAL_TEMPLATE = """
🟢 <b>入场信号 | {strategy_name}</b>
━━━━━━━━━━━━━━━━━━━━
📌 标的: {underlying} (${underlying_price:.2f})
📋 期权: {contract_symbol}
   {option_type} ${strike} | 到期 {expiration}
💰 报价: ${bid:.2f} / ${ask:.2f}
📊 触发条件:
{conditions_detail}
⏱ {trigger_time} ET
━━━━━━━━━━━━━━━━━━━━
⚠️ 数据源: Yahoo Finance (延迟~15s)
"""

EXIT_SIGNAL_TEMPLATE = """
🔴 <b>出场信号 | {strategy_name}</b>
━━━━━━━━━━━━━━━━━━━━
📌 标的: {underlying} (${underlying_price:.2f})
📋 期权: {contract_symbol}
📊 触发: {exit_reason}
   入场价 ${entry_price:.2f} → 当前 ${current_price:.2f} ({pnl_pct:+.1%})
⏱ 持仓 {hold_duration}
━━━━━━━━━━━━━━━━━━━━
⚠️ 数据源: Yahoo Finance (延迟~15s)
"""

# 关键：每条通知都标注数据源延迟，提醒自己当前数据不是实时的
```

### 4.6 Telegram Bot 命令

```python
# 基础命令
/status          # 系统状态 + 所有策略概览
/quote AAPL      # 查询实时报价
/chain AAPL 230 C 0321   # 查期权报价
/strategies      # 列出所有策略及状态
/enable <id>     # 启用策略
/disable <id>    # 禁用策略
/pause 30        # 全局静默 30 分钟
/history         # 今日信号记录

# 建仓确认（收到入场信号后）
/confirm <signal_id> <entry_price>   # 确认已建仓，开始追踪出场
/skip <signal_id>                     # 跳过该信号
```

---

## 五、部署方案

### 5.1 Docker Compose（一键部署）

```yaml
# docker-compose.yaml
version: "3.8"

services:
  redis:
    image: redis:7-alpine
    restart: always
    ports:
      - "127.0.0.1:6379:6379"
    volumes:
      - redis_data:/data
    command: redis-server --appendonly yes --maxmemory 256mb

  monitor:
    build: .
    restart: always
    depends_on:
      - redis
    environment:
      - REDIS_URL=redis://redis:6379
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
      - TZ=America/New_York    # 统一用美东时间
    volumes:
      - ./config:/app/config   # 策略配置映射，修改后自动热加载
      - ./data:/app/data       # SQLite 数据持久化

volumes:
  redis_data:
```

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/

CMD ["python", "-m", "src.main"]
```

```txt
# requirements.txt
yfinance>=0.2.30
redis>=5.0
pandas>=2.1
pandas-ta>=0.3.14
python-telegram-bot>=21.0
apscheduler>=3.10
pyyaml>=6.0
watchdog>=3.0
httpx>=0.25
```

### 5.2 部署步骤

```bash
# 1. SSH 到 VPS
ssh root@your-vps-ip

# 2. 安装 Docker
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin

# 3. 克隆项目
git clone https://github.com/your-repo/options-monitor.git
cd options-monitor

# 4. 配置环境变量
cat > .env << 'EOF'
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
EOF

# 5. 配置策略（编辑 YAML）
vim config/strategies/aapl_rsi_bounce.yaml

# 6. 一键启动
docker compose up -d

# 7. 查看日志
docker compose logs -f monitor

# 8. 修改策略后自动热加载（无需重启）
vim config/strategies/aapl_rsi_bounce.yaml
# watchdog 自动检测 → 策略重新加载 → Telegram 通知: "策略已更新"
```

---

## 六、从 MVP 到生产的升级路径

### 6.1 数据源替换（唯一需要改的地方）

```
MVP 阶段                          生产阶段
─────────                        ─────────
YahooCollector                   IBKRCollector
  ↓ 轮询 5-10s                     ↓ WebSocket 推送
  ↓ ~15s 延迟                      ↓ ~50ms 延迟
  ↓ Greeks 不全                    ↓ 完整 Greeks
  ↓ 免费                          ↓ $1.50-10/月
  ↓                               ↓
  └──── 都实现 BaseCollector 接口 ────┘
            ↓
    上层代码零改动
    只改 config/settings.yaml:
    data_source: yahoo → ibkr
```

### 6.2 完整升级清单

| 阶段 | 改动项 | 工作量 | 优先级 |
|------|--------|--------|--------|
| **MVP → v1.0** | YahooCollector → IBKRCollector | 2-3 天 | 验证完策略后立即做 |
| **MVP → v1.0** | SQLite → PostgreSQL (可选) | 1 天 | 数据量大时 |
| **v1.0 → v1.5** | 加 Web 管理面板 | 1-2 周 | 策略多了再做 |
| **v1.0 → v1.5** | 加策略回测框架 | 1 周 | 优化策略时 |
| **v1.5 → v2.0** | 加 LLM 辅助策略生成 | 2 周 | 锦上添花 |
| **v1.5 → v2.0** | IBKR 一键下单集成 | 1 周 | 当你信任系统后 |

---

## 七、成本总结

| 项目 | MVP 阶段 | 备注 |
|------|----------|------|
| VPS（阿里云海外轻量 / AWS Lightsail） | ¥24-70/月 ($5-10) | |
| 数据源（Yahoo Finance） | ¥0 | |
| Telegram Bot | ¥0 | |
| Redis（Docker 自建） | ¥0 | 包含在 VPS 内 |
| **月合计** | **¥24-70/月 ($5-10)** | |

---

## 八、MVP 开发排期建议

| 天数 | 任务 | 产出 |
|------|------|------|
| **Day 1** | 搭 VPS + Docker + Redis；创建 Telegram Bot | 基础设施就绪 |
| **Day 2** | 实现 YahooCollector + Redis 写入 | 能采集数据并查看 |
| **Day 3** | 实现指标计算引擎（RSI / MACD / EMA） | 能从 K 线算出指标 |
| **Day 4** | 实现策略 YAML 加载 + 规则匹配引擎 | 策略能触发信号 |
| **Day 5** | 实现 Telegram 通知 + Bot 命令 | 信号能推送到手机 |
| **Day 6** | 实现状态机 + 去重限流 + 热更新 | 系统稳定可用 |
| **Day 7** | 端到端测试 + 用真实行情跑一个交易日 | ✅ MVP 上线 |

**7 天，一个人，零成本数据源，即可跑通全链路。**
