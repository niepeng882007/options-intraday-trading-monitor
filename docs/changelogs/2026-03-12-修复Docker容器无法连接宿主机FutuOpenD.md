# 修复 Docker 容器无法连接宿主机 FutuOpenD 网关

> 日期：2026-03-12
> 类型：Bug 修复 | 配置变更
> 难度：⭐⭐⭐

## 一、变更摘要

Docker 容器内的 playbook 服务持续报 `ECONNREFUSED` 错误，无法连接 FutuOpenD 网关。根因是容器内的 `127.0.0.1` 指向容器自身的 loopback 接口，而非宿主机——FutuOpenD 运行在宿主机上，监听 `127.0.0.1:11111`，容器通过默认 bridge 网络根本无法触达。

修复方案：在 `docker-compose.yaml` 中注入 `FUTU_HOST=host.docker.internal` 环境变量，并在所有 FutuCollector/HKCollector 的初始化链路中加入环境变量优先级逻辑，使容器内服务通过 Docker Desktop 提供的 `host.docker.internal` DNS 名称访问宿主机端口。

修复后，US Predictor 和 HK Predictor 均成功连接 FutuOpenD，Telegram Bot 和 auto-scan 调度正常启动。

## 二、修改的文件清单

| 文件路径 | 变更类型 | 说明 |
|---------|---------|------|
| `docker-compose.yaml` | 修改 | 添加 `FUTU_HOST=host.docker.internal` 环境变量 |
| `src/collector/futu.py` | 修改 | `FutuCollector.__init__` 默认 host 改为读取 `FUTU_HOST` 环境变量 |
| `src/hk/collector.py` | 修改 | `HKCollector.__init__` 默认 host 改为读取 `FUTU_HOST` 环境变量 |
| `src/main.py` | 修改 | 组合入口创建 `FutuCollector` 时，环境变量优先于 config |
| `src/hk/main.py` | 修改 | `HKPredictor` 创建 `HKCollector` 时，环境变量优先于 config |
| `src/us_playbook/__main__.py` | 修改 | US Predictor 独立入口创建 `FutuCollector` 时，环境变量优先于 config |

## 三、关键代码解析

### 3.1 Docker Compose 注入宿主机地址

**改动后：**
```yaml
services:
  playbook:
    build: .
    restart: always
    environment:
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
      - FUTU_HOST=host.docker.internal   # 新增：让容器能访问宿主机
      - TZ=America/New_York
```

**解析：**
`host.docker.internal` 是 Docker Desktop（macOS/Windows）内置的特殊 DNS 名称，它解析为宿主机的 IP 地址。容器通过这个名称可以访问宿主机上监听的任何服务。这里将它作为环境变量注入，而非硬编码到代码里，保持了本地开发（直接用 `127.0.0.1`）和容器部署（用 `host.docker.internal`）的兼容性。

### 3.2 FutuCollector 构造函数支持环境变量

**改动前：**
```python
def __init__(
    self,
    host: str = "127.0.0.1",   # 硬编码默认值
    port: int = 11111,
) -> None:
```

**改动后：**
```python
def __init__(
    self,
    host: str = os.getenv("FUTU_HOST", "127.0.0.1"),  # 环境变量优先
    port: int = 11111,
) -> None:
```

**解析：**
使用 `os.getenv("FUTU_HOST", "127.0.0.1")` 作为默认参数值——如果设置了 `FUTU_HOST` 环境变量就用它，否则回退到 `127.0.0.1`。这样本地直接运行 Python 时行为不变（走 `127.0.0.1`），Docker 容器内通过环境变量注入走 `host.docker.internal`。

### 3.3 调用方的三层优先级

**改动前：**
```python
# src/main.py — 只从 config 读取，绕过了构造函数的默认值
collector = FutuCollector(
    host=futu_cfg.get("host", "127.0.0.1"),  # config 有值用 config，否则硬编码
    port=futu_cfg.get("port", 11111),
)
```

**改动后：**
```python
collector = FutuCollector(
    host=os.getenv("FUTU_HOST", futu_cfg.get("host", "127.0.0.1")),
    port=futu_cfg.get("port", 11111),
)
```

**解析：**
这里建立了三层优先级链：**环境变量 > YAML config > 硬编码默认值**。之所以需要在调用方也加这层逻辑，是因为 `futu_cfg.get("host", "127.0.0.1")` 会显式传递 `host=` 参数给构造函数，直接覆盖了构造函数的默认参数值——即使构造函数默认值已经改成读环境变量，只要调用方显式传了值，默认值就不会生效。

这是一个容易踩的坑：**Python 默认参数值只有在调用方不传该参数时才会使用**。

## 四、涉及的知识点

### 4.1 Docker 容器网络模型与宿主机通信

**是什么：** Docker 默认使用 bridge 网络模式，为每个容器分配独立的网络命名空间。容器内的 `127.0.0.1`（loopback）指向容器自身，而非宿主机。

**为什么重要：** 这是 Docker 化部署中最常见的连接问题之一。任何在宿主机上运行的服务（数据库、API 网关、消息队列），容器默认都无法通过 `127.0.0.1` 访问。不理解这一点会导致"本地能跑，容器里连不上"的困惑。

**在本次变更中如何体现：** FutuOpenD 监听在宿主机的 `127.0.0.1:11111`，容器内的代码默认也连 `127.0.0.1:11111`，但连的是容器自己——自然 `ECONNREFUSED`。

**三种解决方案对比：**

| 方案 | 原理 | macOS | Linux | 适用场景 |
|------|------|-------|-------|---------|
| `host.docker.internal` | Docker Desktop 提供的 DNS 别名，解析到宿主机 IP | ✅ | ⚠️ 需额外配置 | macOS/Windows 开发环境 |
| `network_mode: host` | 容器直接使用宿主机网络栈，无网络隔离 | ❌ 不生效 | ✅ | Linux 生产环境 |
| `extra_hosts` | 手动注入 DNS 映射 | ✅ | ✅ | 需要精确控制时 |

**延伸阅读：** `Docker networking overview`、`host.docker.internal`、`Docker bridge vs host network mode`

### 4.2 配置优先级设计模式（环境变量 > 配置文件 > 默认值）

**是什么：** 应用配置的来源通常有多层：环境变量、配置文件、命令行参数、硬编码默认值。业界惯例（12-Factor App）推荐的优先级是：命令行参数 > 环境变量 > 配置文件 > 默认值。

**为什么重要：** 同一份代码在不同环境（本地开发、Docker、CI、生产）运行时，配置需求不同。环境变量是最灵活的注入方式，不需要修改代码或配置文件，容器编排工具（Docker Compose、K8s）天然支持。

**在本次变更中如何体现：** `os.getenv("FUTU_HOST", futu_cfg.get("host", "127.0.0.1"))` 构成了三层 fallback 链。本地开发不设环境变量、config 也没配 → 用 `127.0.0.1`；Docker 里设了 `FUTU_HOST` → 用 `host.docker.internal`；如果某天 config 里配了 host → 环境变量仍然可以覆盖。

**延伸阅读：** `12-Factor App Config`、`Python os.getenv vs os.environ`、`Kubernetes ConfigMap and Secrets`

### 4.3 Python 默认参数值的求值时机

**是什么：** Python 函数的默认参数值在**函数定义时**求值一次，而非每次调用时求值。但对于 `os.getenv()` 这类在模块加载时就能确定的值，定义时求值通常是预期行为。

**为什么重要：** 本次修复暴露了一个微妙问题——即使构造函数的默认参数改成了 `os.getenv("FUTU_HOST", "127.0.0.1")`，如果调用方显式传了 `host=futu_cfg.get("host", "127.0.0.1")`，默认值完全不会被使用。这说明**防御性设计需要在每个入口点都做**，不能只改一处就觉得万事大吉。

**在本次变更中如何体现：** 共修改了 6 个文件——不仅改了 `FutuCollector` 和 `HKCollector` 的构造函数默认值，还改了 `main.py`、`hk/main.py`、`us_playbook/__main__.py` 三个调用方。如果只改构造函数而不改调用方，bug 不会被修复。

**延伸阅读：** `Python default argument values mutable gotcha`、`Python function parameter evaluation`

### 4.4 排查网络连通性问题的诊断方法论

**是什么：** 面对 `ECONNREFUSED` 这类网络错误，需要按层次逐步排查：服务是否在运行 → 监听在哪个地址/端口 → 客户端和服务端之间的网络是否可达。

**为什么重要：** 网络问题的表象往往相同（连不上），但根因可能在不同层面。系统化的诊断方法能快速定位问题，而非盲目猜测。

**本次排查过程：**

```bash
# 第一步：确认目标进程是否存在
ps aux | grep python

# 第二步：Docker 容器状态和日志
docker ps
docker logs --tail 20 <container>

# 第三步：确认服务端监听状态
lsof -i :11111 -P -n
# 结果：FutuOpenD 在 127.0.0.1:11111 LISTEN ✅

# 第四步：从不同网络位置测试连通性
nc -z -w 2 127.0.0.1 11111        # 宿主机本地 → OK ✅
nc -z -w 2 host.docker.internal 11111  # Docker DNS → OK ✅

# 第五步：确认容器内环境变量
docker exec <container> env | grep FUTU
```

**延伸阅读：** `lsof network diagnostics`、`nc (netcat) connectivity testing`、`Docker container debugging`

## 五、测试建议

### 5.1 建议的测试用例

- **正常场景：** 设置 `FUTU_HOST=host.docker.internal`，`docker compose up`，确认日志出现 `Connected to FutuOpenD at host.docker.internal:11111`，US 和 HK Predictor 均初始化成功
- **本地开发场景：** 不设 `FUTU_HOST` 环境变量，直接 `python -m src.main` 运行，确认连接 `127.0.0.1:11111` 正常
- **Config 覆盖场景：** 在 `us_playbook_settings.yaml` 中添加 `futu.host: 192.168.1.100`，不设环境变量，确认连接到 config 指定的地址
- **环境变量覆盖 Config 场景：** 同时设置 `FUTU_HOST=host.docker.internal` 和 config 中的 `futu.host`，确认环境变量优先
- **异常场景：** 设置 `FUTU_HOST` 为不可达地址（如 `10.0.0.1`），确认服务有清晰的错误日志而非静默失败

### 5.2 测试思路说明

核心关注点是**三层优先级链是否正确生效**。每层配置来源都应该独立验证，特别要注意"环境变量 > config"这一层——因为代码中 config 的读取会显式传参，有可能绕过环境变量。另外需要确保本地开发体验不受影响（不设环境变量时行为和改动前一致）。

## 六、场景扩展

### 6.1 类似场景

1. **数据库连接地址配置**：如果未来将 Redis、PostgreSQL 等数据存储加入项目，同样需要考虑 Docker 内外的地址差异。可以用相同的 `os.getenv("DB_HOST", config.get("host", "127.0.0.1"))` 模式。

2. **多环境部署（dev/staging/prod）**：当项目需要部署到多个环境时，所有外部服务的连接地址都应该支持环境变量注入。可以考虑统一封装一个 `get_config(key, env_var, default)` 工具函数，避免在每个调用点重复写三层 fallback。

3. **Telegram Bot Webhook 地址**：如果未来从 polling 切换到 webhook 模式，webhook URL 也需要类似的环境变量注入机制，因为不同环境的公网地址不同。

### 6.2 进阶思考

- **统一配置管理**：当前配置散落在 YAML 文件、环境变量、代码默认值三处，随着配置项增多，维护成本会上升。可以考虑引入一个轻量的配置层（如 `pydantic-settings`），自动处理环境变量 > 配置文件 > 默认值的优先级，并提供类型验证。

- **`network_mode: host` 在 macOS 上不生效的深层原因**：macOS 上 Docker Desktop 实际运行在一个 Linux 虚拟机（LinuxKit VM）内，`network_mode: host` 让容器共享的是这个 VM 的网络栈，而非 macOS 宿主机的。`host.docker.internal` 是 Docker Desktop 在 VM 内维护的一条特殊 DNS 记录，指向宿主机。如果未来部署到 Linux 服务器，`network_mode: host` 可以直接生效，但 `host.docker.internal` 需要额外配置（Docker 20.10+ 可用 `--add-host=host.docker.internal:host-gateway`）。

## 七、总结

本次修复的核心教训：**Docker 容器的 `127.0.0.1` 不是宿主机的 `127.0.0.1`**。这是容器化部署中最经典的网络陷阱之一。解决方案是通过 `host.docker.internal`（macOS/Windows）让容器访问宿主机服务，并通过环境变量注入保持代码在不同运行环境下的兼容性。

另一个值得记住的经验是：修改默认值时，**要追踪所有调用链**。如果调用方显式传了参数，构造函数的默认值形同虚设。本次修复涉及 6 个文件，正是因为要在每个创建 Collector 的入口点都加上环境变量优先级逻辑。

最后，macOS 上 `network_mode: host` 不生效是一个违反直觉的平台差异，值得牢记。
