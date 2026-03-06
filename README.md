# Options Intraday Trading Monitor

美股期权日内交易实时监控与智能通知系统

## 功能

- **数据采集**: 支持 Futu OpenD（主力，毫秒级推送）和 Yahoo Finance（备用）双数据源，获取股票报价、期权链、分钟级 K 线
- **指标计算**: RSI / MACD / EMA / VWAP / ATR / ADX / Bollinger Bands，支持 1m、5m、15m 时间框架
- **策略匹配**: YAML 配置策略，支持 AND/OR 条件组合，crosses_above/turns_positive 等比较器，confirm_bars 多 bar 确认、min_magnitude 幅度过滤
- **状态机管理**: WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING
- **Telegram 通知**: 入场/出场信号推送，支持 Bot 命令交互
- **策略热更新**: watchdog 监听 YAML 文件变更，自动重载无需重启
- **市场环境过滤**: SPY 日跌幅限制、ADX 趋势强度过滤、午间禁交易时段、每日亏损熔断
- **双轨策略体系**: 6 个左侧埋伏策略 + 4 个右侧突破策略（10 个策略，8 活跃 + 2 禁用）
- **回测框架**: 基于历史数据验证策略参数，输出胜率、盈亏比、利润因子、权益曲线

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 Telegram Bot Token 和 Chat ID
```

### 2. Docker Compose 部署

```bash
docker compose up -d
docker compose logs -f monitor
```

### 3. 本地开发

```bash
pip install -r requirements.txt
python -m src.main
```

### 4. 运行测试

```bash
pip install pytest
pytest tests/ -v
```

### 5. 运行回测

```bash
python -m src.backtest --all -d 5 -v
```

## 策略配置

在 `config/strategies/` 下创建 YAML 文件即可添加策略，支持热更新。

## Telegram Bot 命令

| 命令 | 功能 |
|------|------|
| `/status` | 系统状态 + 策略概览 |
| `/quote AAPL` | 查询实时报价 |
| `/chain AAPL 230 C 0321` | 查期权报价 |
| `/strategies` | 列出所有策略 |
| `/enable <id>` | 启用策略 |
| `/disable <id>` | 禁用策略 |
| `/pause 30` | 静默 30 分钟 |
| `/history` | 今日信号记录 |
| `/confirm <signal_id> <price>` | 确认建仓 |
| `/test` | 发送测试入场/出场提醒，验证推送链路 |
| `/skip <signal_id>` | 跳过信号 |
