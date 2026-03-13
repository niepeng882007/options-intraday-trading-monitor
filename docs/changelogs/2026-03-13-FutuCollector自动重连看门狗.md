# FutuCollector 自动重连看门狗——僵尸连接主动探活与回收

> 日期：2026-03-13
> 类型：功能实现
> 难度：⭐⭐⭐

## 一、变更摘要

**问题**：当 FutuOpenD（Futu 行情网关）断开后重连，`FutuCollector` 持有的 `OpenQuoteContext` 对象不会变成 `None`，但其底层 TCP socket 已失效——这就是所谓的"僵尸连接"。后续 API 调用会 hang 整整 30 秒（`CALL_TIMEOUT_SECONDS`）才触发超时重连，导致用户在 Telegram 发送查询后长时间无响应。

**改动**：新增后台看门狗（Watchdog）机制——在独立线程池上定期探活 `get_global_state()`，5 秒无响应即主动回收僵尸 ctx 并重置主线程池，将故障恢复时间从 30 秒降到约 5 秒。同时引入 `_safe_close_ctx()` 统一管理 socket 释放。

**效果**：用户查询中断窗口从最长 30 秒缩短到一个看门狗探活周期（默认 30 秒）+ 探活超时（5 秒），且下一次查询立即可用（不再需要等待 hang 的调用超时）。

## 二、修改的文件清单

| 文件路径 | 变更类型 | 说明 |
|---------|---------|------|
| `src/collector/futu.py` | 修改 | 核心：新增 `_watchdog_pool`、`_safe_close_ctx()`、`start/stop_watchdog()`、`_watchdog_loop()`；修改 `_run_sync()` 和 `_retry()` 使用 safe close |
| `src/main.py` | 修改 | 组合入口：`connect()` 后启动 watchdog |
| `src/us_playbook/__main__.py` | 修改 | 独立入口：同上 |
| `tests/test_collector_futu.py` | 修改 | 新增 11 个测试覆盖 safe close、生命周期、探活成功/失败、联动关闭等 |

## 三、关键代码解析

### 3.1 独立看门狗线程池——解决"探活被 hang 调用阻塞"问题

```python
# Futu 协议要求串行访问——主线程池只有 1 个 worker
_thread_pool = ThreadPoolExecutor(max_workers=1)
# 看门狗独立线程池——永远不会被主线程池上 hang 住的 API 调用阻塞
_watchdog_pool = ThreadPoolExecutor(max_workers=1)
```

**解析**：这是整个方案的关键设计决策。为什么不复用 `_thread_pool`？

假设场景：一个 `get_stock_quote()` 调用正在 `_thread_pool` 的唯一 worker 线程上 hang 住（底层 socket 已断但 Futu SDK 还在等待）。如果看门狗也提交到同一个线程池，它的探活任务会排在 hang 住的任务后面——永远执行不到。这就像急救通道被堵住的车挡住了一样。

独立线程池让看门狗拥有自己的"急救通道"，即使主线程池完全阻塞，探活仍然能正常执行。

### 3.2 `_safe_close_ctx()`——防御性 socket 释放

```python
def _safe_close_ctx(self) -> None:
    """Close the current context and release the TCP socket."""
    if self._ctx is not None:
        try:
            self._ctx.close()  # 主动关闭底层 TCP socket
        except Exception:
            pass  # 僵尸 ctx 的 close() 也可能抛异常，静默忽略
        self._ctx = None  # 无论 close 成败，都清空引用
```

**改动前**：`_run_sync()` 超时和 `_retry()` 失败时只做 `self._ctx = None`：

```python
# 旧代码 —— _run_sync 超时处理
except asyncio.TimeoutError:
    self._ctx = None          # ← 只是清空引用，socket 没有被关闭！
    self._reset_thread_pool()
    raise

# 旧代码 —— _retry 失败时
if reconnect_on_fail:
    self._ctx = None          # ← 同样只是清空引用
```

**解析**：`self._ctx = None` 只是让 Python 解除对 `OpenQuoteContext` 对象的引用。但 GC 何时回收、回收时是否调用 `close()` 释放底层 socket，都是不确定的。在高频故障场景下，可能积累大量泄漏的 socket 连接。`_safe_close_ctx()` 确保：
1. **主动关闭** socket（不依赖 GC）
2. **异常安全**——即使 `close()` 自身抛异常也不影响后续逻辑
3. **幂等性**——多次调用不会出错（`if self._ctx is not None` 守护）

### 3.3 看门狗核心循环——探活、状态跟踪与恢复日志

```python
async def _watchdog_loop(self, interval: int) -> None:
    was_healthy = True  # 记录上一轮状态，用于检测"从故障恢复"的时刻
    while True:
        await asyncio.sleep(interval)  # 先 sleep，刚启动时不需要立即探活
        try:
            loop = asyncio.get_running_loop()
            # 在独立线程池上执行探活（不阻塞主线程池）
            fut = loop.run_in_executor(_watchdog_pool, self._check_connection)
            # 5 秒超时——比主调用的 30 秒短得多，快速判定
            await asyncio.wait_for(fut, timeout=WATCHDOG_PROBE_TIMEOUT)
            self._healthy = True
            self._last_ok_ts = time.time()
            if not was_healthy:
                # 刚从故障恢复，记录一条特殊日志
                logger.info("Reconnected to FutuOpenD")
                was_healthy = True
        except Exception as exc:
            self._healthy = False
            was_healthy = False
            logger.warning("Watchdog probe failed: %s — recycling context", exc)
            self._safe_close_ctx()       # 主动关闭僵尸 ctx
            self._reset_thread_pool()    # 替换可能被阻塞的主线程池
```

**解析**：几个设计要点：

1. **先 sleep 再探活**：`await asyncio.sleep(interval)` 放在循环顶部。刚 `connect()` 完毕时连接一定是好的，不需要浪费一次探活。
2. **复用 `_check_connection()`**：不重复造轮子，直接调用已有的健康检查方法（`get_global_state()`）。
3. **`was_healthy` 状态机**：两个布尔值（`_healthy` 和局部 `was_healthy`）协作。`_healthy` 是公开状态，外部可查询；`was_healthy` 是内部状态，用于检测"故障→恢复"的转换时刻，只在这个时刻打一次 `Reconnected` 日志（避免每次探活都打日志）。
4. **失败时同时回收 ctx 和线程池**：因为主线程池的 worker 可能还在 hang 住的调用上，即使 ctx 被回收了，那个 worker 也回不来。必须替换整个线程池。

### 3.4 安全停止看门狗——避免 CancelledError 泄漏

```python
async def stop_watchdog(self) -> None:
    """Cancel the watchdog task."""
    task = self._watchdog_task
    if task is not None:
        self._watchdog_task = None   # 先清引用，防止 close() 再次进入
        task.cancel()                # 发送取消信号
        try:
            await task               # 等待任务实际结束
        except asyncio.CancelledError:
            pass                     # 预期的异常，静默吞掉
        logger.info("Watchdog stopped")
```

**解析**：`task.cancel()` 只是发送取消信号，任务并不会立即停止——它会在下一个 `await` 点（即 `asyncio.sleep(interval)`）抛出 `CancelledError`。我们需要 `await task` 来等待它真正结束，并捕获 `CancelledError` 避免异常泄漏到调用者。

注意 `self._watchdog_task = None` 放在 `task.cancel()` 之前：这是为了防止在 `await task` 过程中如果有其他协程调用了 `stop_watchdog()`，不会重复操作。

### 3.5 `_retry()` 记录成功时间戳

```python
async def _retry(self, fn, *args, retries=MAX_RETRIES, reconnect_on_fail=True):
    backoff = BACKOFF_BASE_SECONDS
    last_exc = None
    for attempt in range(retries):
        try:
            result = await self._run_sync(fn, *args)
            self._last_ok_ts = time.time()  # ← 新增：每次成功都更新
            return result
        except Exception as exc:
            ...
            if reconnect_on_fail:
                self._safe_close_ctx()      # ← 改动：safe close 替代裸置 None
```

**解析**：`_last_ok_ts` 不仅由看门狗更新，业务调用成功时也会更新。这样外部可以通过 `_last_ok_ts` 判断"最近一次成功通信是什么时候"，为未来的监控报警提供数据支撑。

### 3.6 入口集成——一行启动

```python
# src/main.py
await collector.connect()
await collector.start_watchdog()  # ← 新增

# src/us_playbook/__main__.py
await collector.connect()
await collector.start_watchdog()  # ← 新增
```

**解析**：`close()` 内部已自动调用 `stop_watchdog()`，所以关闭路径不需要额外代码。这是一个好的 API 设计——启动需要显式调用（opt-in），关闭自动级联（无需记得手动停止）。

## 四、涉及的知识点

### 4.1 僵尸连接（Zombie Connection）与探活机制

**是什么**：僵尸连接是指客户端持有的连接对象在语言层面看起来正常（不是 `None`、没有被标记为 closed），但底层的网络连接（TCP socket）已经断开。调用该对象的方法不会立即报错，而是 hang 住直到超时。这在使用长连接的 SDK 中非常常见。

**为什么重要**：僵尸连接是生产环境中最隐蔽的故障之一。它不像"连接断开"那样能被简单的 `if conn is None` 检测到，也不像"抛异常"那样能被 try-catch 立即处理。它的表现是"变慢"而不是"报错"，这让排查变得困难。

**在本次变更中如何体现**：FutuOpenD 断线重连后，`self._ctx`（`OpenQuoteContext`）就变成了僵尸对象。我们通过独立线程池上的定期 `get_global_state()` 探活来检测这种状态，一旦探活超时就主动回收。

**延伸阅读**：搜索 `TCP keepalive`、`connection pool health check`、`database connection validation query`（如 MySQL 的 `SELECT 1`、Redis 的 `PING`）。

### 4.2 线程池隔离——关键路径与控制路径分离

**是什么**：将不同职责的任务分配到独立的线程池，确保某个池的阻塞不会影响其他池的执行能力。这是一种资源隔离（Bulkhead）模式。

**为什么重要**：如果所有任务共用一个线程池，一个 hang 住的任务就能耗尽所有 worker，导致所有后续任务排队——包括那些本应快速完成的健康检查。这就像医院急诊如果和普通门诊共用一个叫号系统，急诊患者也得排队。

**在本次变更中如何体现**：`_thread_pool`（1 个 worker）处理所有业务 API 调用，`_watchdog_pool`（1 个 worker）专门用于探活。即使业务调用 hang 住了 `_thread_pool` 的唯一 worker，看门狗仍然能在 `_watchdog_pool` 上执行探活并触发恢复。

**延伸阅读**：搜索 `Bulkhead Pattern`、`Circuit Breaker Pattern`、`Hystrix thread pool isolation`、`Resilience4j Bulkhead`。

### 4.3 asyncio.Task 的生命周期管理

**是什么**：`asyncio.create_task()` 创建一个后台协程任务，它会在事件循环中独立运行。正确管理 Task 的启动、取消和清理是 async Python 编程的重要技能。

**为什么重要**：不正确的 Task 管理会导致：
- **泄漏**：忘记 cancel 的 Task 永远运行，消耗资源
- **CancelledError 泄漏**：cancel 后不 await，异常可能在意外的地方浮出
- **重复启动**：没有幂等保护，多次调用 start 创建多个重复任务

**在本次变更中如何体现**：
- **启动保护**：`if self._watchdog_task is not None: return`——幂等，重复调用不会创建多个看门狗
- **安全取消三步曲**：①先清引用 `self._watchdog_task = None` → ②发送取消信号 `task.cancel()` → ③等待结束并捕获 `await task` + `except CancelledError`
- **级联清理**：`close()` 自动调用 `stop_watchdog()`，用户无需记得手动停止

**延伸阅读**：搜索 `asyncio Task cancellation`、`structured concurrency Python`、`asyncio.TaskGroup`（Python 3.11+）。

### 4.4 防御性资源释放——显式 close 而非依赖 GC

**是什么**：在需要释放外部资源（文件句柄、网络连接、数据库连接）时，不依赖 Python 的垃圾回收器（GC）自动清理，而是显式调用 `close()` 并包裹在 try-except 中确保不抛异常。

**为什么重要**：Python 的 GC 时机不确定（CPython 用引用计数+分代 GC，PyPy 只有分代 GC），在以下场景会导致问题：
- 高频创建/销毁连接时，socket fd 可能耗尽（`Too many open files`）
- 服务器端看到大量 `CLOSE_WAIT` 状态的 TCP 连接
- 在容器环境中 fd 限制更严格

**在本次变更中如何体现**：`_safe_close_ctx()` 在所有需要清理 ctx 的地方替代了裸的 `self._ctx = None`。try-except 包裹确保即使 `close()` 本身失败（僵尸 ctx 的 `close()` 可能抛异常），也不会影响后续的恢复流程。

**延伸阅读**：搜索 `Python context manager`、`__del__ vs explicit close`、`file descriptor leak`、`CLOSE_WAIT TCP`。

## 五、测试建议

### 5.1 建议的测试用例

已实现的 11 个测试覆盖了以下场景：

**正常场景：**
- `_safe_close_ctx()` 正确关闭 ctx 并置 None
- `start_watchdog()` → `stop_watchdog()` 正常启停
- 健康探活更新 `_last_ok_ts` 和 `_healthy` 状态
- `_retry()` 成功时更新 `_last_ok_ts`
- `close()` 自动级联停止看门狗

**边界场景：**
- `start_watchdog()` 幂等性——多次调用不创建重复任务
- `stop_watchdog()` 在未启动时调用不抛异常
- `_safe_close_ctx()` 在 ctx 已为 None 时调用不抛异常

**异常场景：**
- `_safe_close_ctx()` 在 `close()` 抛异常时静默处理
- 探活失败时回收 ctx 并标记 `_healthy = False`
- `_run_sync()` 超时时通过 `_safe_close_ctx()` 关闭 socket

### 5.2 未覆盖但值得补充的场景

- **恢复日志测试**：模拟先失败再成功，验证 `"Reconnected to FutuOpenD"` 日志只打一次
- **并发安全**：多个协程同时调用 `start_watchdog()` / `stop_watchdog()` 的竞态测试
- **`_watchdog_pool` 被阻塞**：模拟看门狗线程池自身 hang 住的场景（理论上 5 秒超时会兜底）

### 5.3 测试思路说明

看门狗的测试核心关注点是**生命周期正确性**和**故障恢复行为**。因为看门狗是后台常驻任务，如果生命周期管理不当（比如不能正常停止），会导致测试 hang 住或进程退出时报错。通过设置短 interval（1 秒）并在探活触发后立即停止，可以在测试中快速验证一个完整的探活周期。

## 六、场景扩展

### 6.1 类似场景

1. **数据库连接池探活**：MySQL / PostgreSQL 连接池中的连接同样会变成僵尸（尤其是经过 NAT/防火墙时 idle 连接被静默断开）。大多数连接池（HikariCP、SQLAlchemy）都有 `validation_query`（如 `SELECT 1`）和 `idle_timeout` 机制，原理与本次看门狗完全一致。

2. **WebSocket 心跳**：WebSocket 协议内置 Ping/Pong 帧来检测连接存活。如果你自己实现 WebSocket 客户端，需要定期发送 Ping 并在超时未收到 Pong 时主动断开重连——这就是协议层面的看门狗。

3. **微服务健康检查**：Kubernetes 的 Liveness Probe 本质上也是看门狗——kubelet 定期探测容器的 `/healthz` 端点，超时或返回错误就重启容器。我们的看门狗可以理解为"进程内的 Liveness Probe"。

### 6.2 进阶思考

1. **看门狗本身挂了怎么办？**——当前 `_watchdog_loop` 中未处理的异常会导致 Task 静默退出。可以考虑在 `_watchdog_loop` 外层加一个 `try-except` 捕获所有异常并重启，或者用 `task.add_done_callback()` 检测意外退出并自动重启。

2. **能否做到更快的故障发现？**——当前方案是定期探活（pull 模式），最坏情况下故障发现延迟 = interval + probe_timeout = 35 秒。如果 Futu SDK 支持断线回调（push 模式），可以在回调中立即触发 ctx 回收，将延迟降到接近 0。两种模式可以组合使用——回调做快速响应，看门狗做兜底保障。

## 七、总结

本次变更的核心经验：**当你依赖的外部服务可能静默断开时，不要等用户的请求来触发故障发现**。主动探活 + 快速回收是处理僵尸连接的标准模式。关键设计决策是**线程池隔离**——让看门狗拥有独立的执行资源，确保它在主通道被阻塞时仍然能工作。`_safe_close_ctx()` 的引入则体现了一个重要原则：资源释放要显式、防御性、幂等。这些模式不仅适用于 Futu SDK，在任何使用长连接的系统（数据库、消息队列、WebSocket）中都是通用的。
