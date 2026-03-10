from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, Callable, Coroutine

import redis.asyncio as aioredis

from src.utils.logger import setup_logger

logger = setup_logger("redis_store")

QUOTE_HASH_PREFIX = "quote:"
OPTION_HASH_PREFIX = "option:"
INDICATOR_HASH_PREFIX = "indicator:"
QUOTE_CHANNEL_PREFIX = "channel:quote:"
OPTION_CHANNEL_PREFIX = "channel:option:"
HISTORY_CHANNEL_PREFIX = "channel:history:"
INDICATOR_CHANNEL_PREFIX = "channel:indicator:"
STRATEGY_UPDATE_CHANNEL = "channel:strategy_update"
COOLDOWN_KEY_PREFIX = "notify:cooldown:"


class RedisStore:
    def __init__(self, url: str = "redis://localhost:6379", max_connections: int = 10):
        self._url = url
        self._max_connections = max_connections
        self._pool: aioredis.ConnectionPool | None = None
        self._client: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._backoff_base = 1
        self._backoff_max = 60
        self._semaphore = asyncio.Semaphore(max_connections // 2 or 5)

    async def connect(self) -> None:
        self._pool = aioredis.ConnectionPool.from_url(
            self._url, max_connections=self._max_connections
        )
        self._client = aioredis.Redis(connection_pool=self._pool)
        await self._client.ping()
        logger.info("Redis connected: %s", self._url)

    async def close(self) -> None:
        if self._pubsub:
            await self._pubsub.close()
        if self._client:
            await self._client.close()
        if self._pool:
            await self._pool.disconnect()
        logger.info("Redis connection closed")

    async def _ensure_connected(self) -> aioredis.Redis:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        return self._client

    # ── Quote operations ──

    async def publish_quote(self, quote: Any) -> None:
        async with self._semaphore:
            client = await self._ensure_connected()
            data = asdict(quote) if hasattr(quote, "__dataclass_fields__") else quote
            key = f"{QUOTE_HASH_PREFIX}{data['symbol']}"
            pipe = client.pipeline()
            pipe.hset(key, mapping={k: json.dumps(v) for k, v in data.items()})
            pipe.publish(f"{QUOTE_CHANNEL_PREFIX}{data['symbol']}", json.dumps(data))
            await pipe.execute()

    async def get_quote(self, symbol: str) -> dict | None:
        client = await self._ensure_connected()
        raw = await client.hgetall(f"{QUOTE_HASH_PREFIX}{symbol}")
        if not raw:
            return None
        return {k.decode(): json.loads(v.decode()) for k, v in raw.items()}

    # ── Option operations ──

    async def publish_options(self, symbol: str, options: list[Any]) -> None:
        client = await self._ensure_connected()
        pipe = client.pipeline()
        for opt in options:
            data = asdict(opt) if hasattr(opt, "__dataclass_fields__") else opt
            key = f"{OPTION_HASH_PREFIX}{data['contract_symbol']}"
            pipe.hset(key, mapping={k: json.dumps(v) for k, v in data.items()})
        await pipe.execute()

        channel = f"{OPTION_CHANNEL_PREFIX}{symbol}"
        payload = [asdict(o) if hasattr(o, "__dataclass_fields__") else o for o in options]
        await client.publish(channel, json.dumps(payload))

    async def get_option(self, contract_symbol: str) -> dict | None:
        client = await self._ensure_connected()
        raw = await client.hgetall(f"{OPTION_HASH_PREFIX}{contract_symbol}")
        if not raw:
            return None
        return {k.decode(): json.loads(v.decode()) for k, v in raw.items()}

    # ── History (K-line) operations ──

    async def publish_history(self, symbol: str, history_json: str) -> None:
        async with self._semaphore:
            client = await self._ensure_connected()
            channel = f"{HISTORY_CHANNEL_PREFIX}{symbol}"
            await client.publish(channel, history_json)

    # ── Indicator operations ──

    async def publish_indicators(self, symbol: str, timeframe: str, indicators: dict) -> None:
        async with self._semaphore:
            client = await self._ensure_connected()
            key = f"{INDICATOR_HASH_PREFIX}{symbol}:{timeframe}"
            payload = {"symbol": symbol, "timeframe": timeframe, **indicators}
            pipe = client.pipeline()
            pipe.hset(key, mapping={k: json.dumps(v) for k, v in indicators.items()})
            pipe.publish(f"{INDICATOR_CHANNEL_PREFIX}{symbol}", json.dumps(payload))
            await pipe.execute()

    async def get_indicators(self, symbol: str, timeframe: str) -> dict | None:
        client = await self._ensure_connected()
        raw = await client.hgetall(f"{INDICATOR_HASH_PREFIX}{symbol}:{timeframe}")
        if not raw:
            return None
        return {k.decode(): json.loads(v.decode()) for k, v in raw.items()}

    # ── Cooldown / rate-limit ──

    async def set_cooldown(self, strategy_id: str, symbol: str, ttl_seconds: int) -> None:
        async with self._semaphore:
            client = await self._ensure_connected()
            key = f"{COOLDOWN_KEY_PREFIX}{strategy_id}:{symbol}"
            await client.set(key, "1", ex=ttl_seconds)

    async def is_in_cooldown(self, strategy_id: str, symbol: str) -> bool:
        async with self._semaphore:
            client = await self._ensure_connected()
            key = f"{COOLDOWN_KEY_PREFIX}{strategy_id}:{symbol}"
            return await client.exists(key) > 0

    # ── Pub/Sub subscription ──

    async def subscribe(
        self,
        channels: list[str],
        callback: Callable[[str, dict], Coroutine],
    ) -> None:
        client = await self._ensure_connected()
        self._pubsub = client.pubsub()
        await self._pubsub.subscribe(*channels)
        logger.info("Subscribed to channels: %s", channels)

        backoff = self._backoff_base
        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    channel = message["channel"].decode()
                    data = json.loads(message["data"].decode())
                    await callback(channel, data)
                    backoff = self._backoff_base
                await asyncio.sleep(0.01)
            except aioredis.ConnectionError:
                logger.warning("Redis connection lost, reconnecting in %ds...", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)
                try:
                    await self.connect()
                    self._pubsub = self._client.pubsub()  # type: ignore[union-attr]
                    await self._pubsub.subscribe(*channels)
                    logger.info("Reconnected and resubscribed")
                except Exception:
                    logger.exception("Reconnection failed")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in subscription loop")
                await asyncio.sleep(1)

    # ── Generic helpers ──

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        client = await self._ensure_connected()
        payload = json.dumps(value)
        if ttl:
            await client.set(key, payload, ex=ttl)
        else:
            await client.set(key, payload)

    async def get_json(self, key: str) -> Any | None:
        client = await self._ensure_connected()
        raw = await client.get(key)
        if raw is None:
            return None
        return json.loads(raw.decode())
