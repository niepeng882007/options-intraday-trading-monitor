对 $ARGUMENTS 运行 playbook 回测:

1. 加载该日历史数据（1min OHLCV + VWAP）
2. 在每个 playbook 时间点（约每 90min）分别用 v1 和 v2 引擎生成输出
3. 对照实际走势评分（日型准确率/方向/入场可执行性/风控/自适应）
4. 输出对比报告

评分标准见 docs/playbook_template_v2.md 底部 checklist。