"""独立入口: python -m src.index_trader

用于调试和独立测试，直接在终端输出报告。
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

import yaml

from src.utils.logger import setup_logger

logger = setup_logger("index_trader_main")


async def main() -> None:
    from src.collector.futu import FutuCollector
    from src.index_trader.main import IndexTrader

    # 加载配置
    cfg_path = "config/index_trader_settings.yaml"
    try:
        with open(cfg_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("Config not found: %s", cfg_path)
        return

    # 连接 Futu
    futu_cfg = config.get("futu", {})
    collector = FutuCollector(
        host=os.getenv("FUTU_HOST", futu_cfg.get("host", "127.0.0.1")),
        port=futu_cfg.get("port", 11111),
    )
    await collector.connect()

    try:
        trader = IndexTrader(config, collector)
        await trader.start()

        # 生成报告
        report = await trader.generate_report()

        # 格式化输出（去掉 HTML 标签用于终端）
        from src.index_trader.formatter import ReportFormatter
        formatter = ReportFormatter(config)
        html = formatter.format(report)

        import re
        plain = re.sub(r"<[^>]+>", "", html)
        print(plain)

        trader.close()
    finally:
        await collector.close()


if __name__ == "__main__":
    asyncio.run(main())
