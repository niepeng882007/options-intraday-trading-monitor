"""配置加载器 — 从 config.yaml + .env 加载配置。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent


def load_config(config_path: str | None = None) -> dict:
    """加载 config.yaml 并合并 .env 环境变量。"""
    if config_path is None:
        config_path = str(_BASE_DIR / "config.yaml")

    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 加载 .env（不覆盖已存在的环境变量）
    env_path = _BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    # 注入 Telegram 配置
    tg = cfg.setdefault("telegram", {})
    tg["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
    tg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", tg.get("chat_id", ""))

    # Futu host 支持环境变量覆盖（Docker 中用 host.docker.internal）
    futu = cfg.setdefault("futu", {})
    futu_host_env = os.environ.get("FUTU_HOST")
    if futu_host_env:
        futu["host"] = futu_host_env

    return cfg
