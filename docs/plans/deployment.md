# VPS 部署指南 — Options Intraday Trading Monitor

## 前置条件

### VPS 规格要求

| 项目 | 最低配置 | 推荐配置 |
|------|---------|---------|
| CPU | 2 核 | 2 核 |
| 内存 | 4 GB | 4 GB |
| 磁盘 | 40 GB SSD | 60 GB SSD |
| 系统 | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| 地区 | 香港 / 新加坡 | 香港（Futu 延迟最低） |

### 需要准备

- [ ] VPS root SSH 登录凭据
- [ ] Telegram Bot Token（从 @BotFather 获取）
- [ ] Telegram Chat ID（从 @userinfobot 获取）
- [ ] 富途牛牛账号（已开通 OpenD API 权限）
- [ ] FutuOpenD Linux 安装包（从 [富途官网](https://www.futunn.com/download/OpenAPI) 下载）

> ⚠️ **FutuOpenD 首次登录需要扫码验证**，必须先在有 GUI 的环境完成，详见 Phase 2。

---

## Phase 1: VPS 基础环境

### 1.1 SSH 连接

```bash
ssh root@<VPS_IP>
```

### 1.2 创建专用用户

```bash
adduser trading --disabled-password --gecos ""
usermod -aG sudo trading
usermod -aG docker trading  # Docker 安装后执行
```

> 后续所有操作均以 `trading` 用户执行：`su - trading`

### 1.3 系统更新 + 基础工具

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-plugin sqlite3 netcat-openbsd curl git
```

验证 Docker：

```bash
docker --version
# 预期: Docker version 24.x.x 或更高

docker compose version
# 预期: Docker Compose version v2.x.x
```

### 1.4 将 trading 用户加入 docker 组

```bash
sudo usermod -aG docker trading
# 重新登录使生效
su - trading
```

### 1.5 防火墙配置

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

> ⚠️ **不要** 开放 11111 端口（FutuOpenD）到公网。应用容器通过 `host.docker.internal` 内部访问。

---

## Phase 2: FutuOpenD 部署

### 2.1 安装 FutuOpenD

```bash
sudo mkdir -p /opt/futuopend
cd /opt/futuopend

# 上传或下载 FutuOpenD Linux 包（以 v7.x 为例）
# scp 从本地上传：scp FutuOpenD_x.x.x_Linux.tar.gz trading@<VPS_IP>:/opt/futuopend/
sudo tar xzf FutuOpenD_*.tar.gz --strip-components=1
sudo chown -R trading:trading /opt/futuopend
chmod +x /opt/futuopend/FutuOpenD
```

### 2.2 首次登录（需要扫码）

> ⚠️ **关键步骤**：FutuOpenD 首次登录需要手机扫码验证。有两种方式：

**方式 A：本地登录后迁移（推荐）**

1. 在你的 Mac/PC 上运行 FutuOpenD，完成扫码登录
2. 登录成功后，FutuOpenD 会在同目录生成 `FutuOpenD.xml` 配置文件
3. 将该配置文件上传到 VPS：
   ```bash
   scp FutuOpenD.xml trading@<VPS_IP>:/opt/futuopend/
   ```

**方式 B：VPS 上通过 VNC/X11 登录**

1. 安装临时桌面：`sudo apt install -y xfce4 tigervnc-standalone-server`
2. 启动 VNC，通过 VNC 客户端连接完成扫码
3. 登录成功后可卸载桌面环境

### 2.3 配置 FutuOpenD

确认 `/opt/futuopend/FutuOpenD.xml` 包含以下关键配置：

```xml
<login_account>你的富途账号</login_account>
<login_pwd_md5>MD5加密密码</login_pwd_md5>
<api_ip>127.0.0.1</api_ip>
<api_port>11111</api_port>
```

### 2.4 安装 systemd 服务

```bash
sudo cp /opt/trading/app/deploy/futuopend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable futuopend
sudo systemctl start futuopend
```

验证：

```bash
sudo systemctl status futuopend
# 预期: Active: active (running)

nc -z 127.0.0.1 11111 && echo "FutuOpenD OK" || echo "FutuOpenD FAILED"
# 预期: FutuOpenD OK
```

查看日志：

```bash
journalctl -u futuopend -f --no-pager -n 50
```

---

## Phase 3: 应用部署

### 3.1 Clone 代码

```bash
sudo mkdir -p /opt/trading
sudo chown trading:trading /opt/trading
cd /opt/trading
git clone <YOUR_REPO_URL> app
cd app
```

### 3.2 创建 `.env`

```bash
cat > .env << 'EOF'
TELEGRAM_BOT_TOKEN=你的Bot Token
TELEGRAM_CHAT_ID=你的Chat ID
EOF
chmod 600 .env
```

> ⚠️ VPS 在香港，**不需要**代理相关环境变量。

### 3.3 创建 data 目录

```bash
mkdir -p data
chown $(id -u):$(id -g) data
```

> 容器以 `user: 1000:1000` 运行，data 目录必须对该 UID 可写。

### 3.4 构建镜像

```bash
docker compose build
```

预期输出（首次约 2-3 分钟）：

```
[+] Building 120.5s (9/9) FINISHED
 => [internal] load build definition from Dockerfile
 ...
 => => naming to docker.io/library/app-playbook
```

### 3.5 启动容器

```bash
docker compose up -d
```

### 3.6 端到端验证

```bash
# 1. 查看容器状态
docker compose ps
# 预期: playbook   running (healthy)

# 2. 查看启动日志
docker compose logs -f --tail 50
# 预期: 无代理错误，无连接超时

# 3. 验证 data 目录可写
docker compose exec playbook ls -la /app/data/
# 预期: 文件所有者为 1000:1000

# 4. 在 Telegram 中发送 "SPY"
# 预期: 收到 SPY playbook 回复
```

---

## Phase 4: 监控与备份

### 4.1 配置日志目录

```bash
sudo touch /var/log/trading-healthcheck.log /var/log/trading-backup.log
sudo chown trading:trading /var/log/trading-healthcheck.log /var/log/trading-backup.log
```

### 4.2 安装 cron 任务

```bash
crontab -e
```

添加：

```
# Trading Monitor — 健康检查 (每5分钟)
*/5 * * * * /opt/trading/app/deploy/healthcheck.sh

# Trading Monitor — 每日备份 (UTC 05:00 = HKT 13:00)
0 5 * * * /opt/trading/app/deploy/backup.sh
```

验证 cron：

```bash
crontab -l
# 预期: 显示上面两行

# 手动测试 healthcheck
/opt/trading/app/deploy/healthcheck.sh
cat /var/log/trading-healthcheck.log
# 预期: OK - all checks passed

# 手动测试 backup
sudo mkdir -p /opt/trading/backups && sudo chown trading:trading /opt/trading/backups
/opt/trading/app/deploy/backup.sh
ls -la /opt/trading/backups/
# 预期: 生成 YYYYMMDD_HHMMSS.tar.gz
```

---

## 日常运维手册

### 升级应用

```bash
cd /opt/trading/app
git pull origin main

# 1. 先构建新镜像（不停机）
docker compose build

# 2. 再切换（停机仅 down→up 几秒）
docker compose down && docker compose up -d

# 3. 检查日志
docker compose logs -f --tail 30
```

### 回滚

```bash
cd /opt/trading/app
# 查看最近的 commit
git log --oneline -10

# 回滚到指定版本
git checkout <commit-hash>
docker compose build
docker compose down && docker compose up -d
```

### 配置变更

config 目录通过 volume 挂载，修改后重启容器即可：

```bash
vim config/us_playbook_settings.yaml  # 编辑配置
docker compose restart
```

### 查看日志

```bash
# 应用日志
docker compose logs -f --tail 100

# FutuOpenD 日志
journalctl -u futuopend -f --no-pager

# 健康检查日志
tail -20 /var/log/trading-healthcheck.log

# 备份日志
tail -20 /var/log/trading-backup.log
```

### 常见故障排查

#### 容器启动失败

```bash
docker compose logs --tail 50
# 常见原因：
# - .env 文件缺失或格式错误
# - FutuOpenD 未启动（先检查 nc -z 127.0.0.1 11111）
# - data/ 目录权限问题
```

#### FutuOpenD 连接失败

```bash
# 检查 FutuOpenD 状态
sudo systemctl status futuopend
nc -z 127.0.0.1 11111 && echo OK || echo FAILED

# 查看 FutuOpenD 日志
journalctl -u futuopend --since "10 minutes ago"

# 手动重启
sudo systemctl restart futuopend
```

#### 容器内连不上 FutuOpenD

```bash
# 验证 host.docker.internal 解析
docker compose exec playbook python -c "
import socket
print(socket.getaddrinfo('host.docker.internal', 11111))
"
# 预期: 返回宿主机 IP 地址
```

#### 磁盘空间不足

```bash
# 检查磁盘
df -h /

# 清理 Docker 缓存
docker system prune -f

# 清理旧备份（保留最近 7 天）
find /opt/trading/backups -name "*.tar.gz" -mtime +7 -delete
```

---

## FutuOpenD Token 过期处理

FutuOpenD 的登录 Token 会定期过期（通常 30-90 天），需要人工续期。

### 症状

- healthcheck 告警：FutuOpenD 端口不可达
- `journalctl -u futuopend` 显示认证失败
- Telegram Bot 查询无响应

### 续期步骤

1. **在本地 Mac/PC 运行 FutuOpenD**，完成扫码登录
2. 登录成功后复制新的 `FutuOpenD.xml`
3. 上传到 VPS：
   ```bash
   scp FutuOpenD.xml trading@<VPS_IP>:/opt/futuopend/
   ```
4. 重启 FutuOpenD：
   ```bash
   sudo systemctl restart futuopend
   ```
5. 验证：
   ```bash
   nc -z 127.0.0.1 11111 && echo OK
   # 在 Telegram 发送 SPY 确认可用
   ```

---

## 目录结构参考

```
/opt/trading/
├── app/                    # git clone 的代码
│   ├── .env                # Telegram 凭据（chmod 600）
│   ├── config/             # YAML 配置（volume mount）
│   ├── data/               # 运行时数据（SQLite, watchlist, cache）
│   ├── deploy/             # 部署脚本
│   │   ├── futuopend.service
│   │   ├── healthcheck.sh
│   │   └── backup.sh
│   ├── docker-compose.yaml
│   ├── Dockerfile
│   └── src/
├── backups/                # 每日备份存储
│   ├── 20260316_050000.tar.gz
│   └── ...
/opt/futuopend/
├── FutuOpenD               # 可执行文件
├── FutuOpenD.xml           # 配置 + Token
└── ...
```
