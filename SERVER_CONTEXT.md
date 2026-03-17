# Server Context — Options Intraday Trading Monitor

> This document is designed for AI assistants. It contains all server configuration details needed to diagnose issues, perform maintenance, or guide the operator through tasks.

---

## Infrastructure

| Item | Value |
|------|-------|
| Provider | Tencent Cloud (腾讯云轻量应用服务器) |
| Region | Hong Kong |
| OS | Ubuntu 22.04 LTS |
| CPU | 2 cores |
| RAM | 4 GB |
| Disk | 70 GB SSD |
| Public IP | 124.156.176.78 |
| SSH User | `trading` (uid=1002, gid=1003) |
| Default User | `ubuntu` (uid=1000, gid=1000) |

> ⚠️ The system's UID 1000 belongs to `ubuntu`, NOT `trading`. Docker containers run as `user: 1000:1000`, so any host-mounted writable directories must be owned by `1000:1000` (i.e. `ubuntu`), not `trading`.

---

## Application Stack

### FutuOpenD (Market Data Gateway)

| Item | Value |
|------|-------|
| Version | 10.0.6018 |
| Install Path | `/opt/futuopend/` |
| Executable | `/opt/futuopend/FutuOpenD` |
| Config File | `/opt/futuopend/FutuOpenD.xml` |
| Listen Address | `0.0.0.0:11111` |
| Systemd Service | `futuopend.service` |
| Run As User | `trading` |
| Login Account | 7458829 |

Key config override (must be preserved after every `FutuOpenD.xml` re-upload):
```xml
<ip>0.0.0.0</ip>  <!-- DEFAULT is 127.0.0.1, must be 0.0.0.0 for Docker access -->
```

Token expiry: FutuOpenD login tokens expire every 30–90 days. Renewal requires local Mac/PC scan-login → re-upload `FutuOpenD.xml` → sed fix `<ip>` → restart service.

Commands:
```bash
sudo systemctl start|stop|restart|status futuopend
journalctl -u futuopend -f --no-pager
nc -z 127.0.0.1 11111 && echo OK || echo FAILED
ss -tlnp | grep 11111  # verify listen address is 0.0.0.0
```

### Application Container

| Item | Value |
|------|-------|
| Code Path | `/opt/trading/app/` |
| Git Remote | `git@github.com:niepeng882007/options-intraday-trading-monitor.git` |
| Docker Image | `app-playbook:latest` |
| Container Name | `app-playbook-1` |
| Container User | `1000:1000` (matches `ubuntu` user, NOT `trading`) |
| Compose File | `/opt/trading/app/docker-compose.yaml` |
| Dockerfile | `/opt/trading/app/Dockerfile` |
| Env File | `/opt/trading/app/.env` (chmod 600) |
| Config Mount | `./config:/app/config:ro` |
| Data Mount | `./data:/app/data` (must be owned by 1000:1000) |
| FutuOpenD Access | `host.docker.internal:11111` → resolves to `172.17.0.1` |
| Timezone | `America/New_York` |
| Memory Limit | 1 GB |
| CPU Limit | 1.0 |
| Healthcheck | TCP connect to `host.docker.internal:11111`, interval=120s |

Dockerfile customization (added during deployment):
```dockerfile
# Line added after WORKDIR /app to fix futu SDK permission error
RUN mkdir -p /.com.futunn.FutuOpenD && chmod 777 /.com.futunn.FutuOpenD
```

Environment variables (`.env`):
```
TELEGRAM_BOT_TOKEN=<redacted>
TELEGRAM_CHAT_ID=<redacted>
```

Commands:
```bash
cd /opt/trading/app
docker compose ps
docker compose logs -f --tail 100
docker compose restart
docker compose down && docker compose up -d
docker compose build  # rebuild image after code/Dockerfile changes
```

---

## Networking & Firewall

| Item | Value |
|------|-------|
| Firewall | UFW (enabled) |
| SSH | Allowed (port 22) |
| FutuOpenD 11111 | NOT exposed to public; accessed only via Docker bridge |
| Docker Bridge | `docker0`, subnet `172.17.0.0/16`, gateway `172.17.0.1` |

**Critical iptables rule** (required for container → host communication):
```bash
sudo iptables -I INPUT -i docker0 -j ACCEPT
```

This rule is needed because UFW blocks traffic from the Docker bridge by default. Without it, the container cannot reach FutuOpenD on the host.

To persist across reboots:
```bash
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

---

## Cron Jobs (run as `trading` user)

```
# Healthcheck every 5 minutes
*/5 * * * * /opt/trading/app/deploy/healthcheck.sh

# Daily backup at UTC 05:00 (HKT 13:00)
0 5 * * * /opt/trading/app/deploy/backup.sh
```

Log files:
- `/var/log/trading-healthcheck.log`
- `/var/log/trading-backup.log`

Backup storage: `/opt/trading/backups/`

---

## Directory Structure

```
/opt/trading/
├── app/                          # git repo (working tree)
│   ├── .env                      # Telegram credentials (chmod 600)
│   ├── config/                   # YAML configs (mounted read-only)
│   ├── data/                     # Runtime data (owned by 1000:1000)
│   ├── deploy/
│   │   ├── futuopend.service
│   │   ├── healthcheck.sh
│   │   └── backup.sh
│   ├── docker-compose.yaml
│   ├── Dockerfile
│   └── src/
├── backups/                      # Daily backup .tar.gz files

/opt/futuopend/
├── FutuOpenD                     # Executable
├── FutuOpenD.xml                 # Config + login token
├── AppData.dat
├── FTUpdate
├── FTWebSocket
├── lib*.so                       # Shared libraries
```

---

## Known Issues & Gotchas

1. **UID mismatch**: `trading` user is UID 1002, but containers run as UID 1000 (`ubuntu`). All writable mounts (`data/`) must be `chown 1000:1000`.

2. **Futu SDK log directory**: The `futu` Python package tries to create `/.com.futunn.FutuOpenD/` at container root. The Dockerfile must pre-create this directory with `chmod 777`.

3. **FutuOpenD listen address**: Default is `127.0.0.1` which blocks Docker container access. Must be changed to `0.0.0.0` in `FutuOpenD.xml`. This gets reset every time `FutuOpenD.xml` is re-uploaded from local machine.

4. **UFW blocks Docker bridge traffic**: Even with UFW disabled/re-enabled, the iptables rule `iptables -I INPUT -i docker0 -j ACCEPT` must be present. Use `iptables-persistent` to survive reboots.

5. **FutuOpenD token expiry (30–90 days)**: Requires local Mac/PC scan-login to renew. After re-uploading the XML config, must fix `<ip>` tag and restart service.

6. **FutuOpenD first-time device verification**: New devices/IPs require SMS verification code. Run `./FutuOpenD` manually, follow `req_phone_verify_code` → `input_phone_verify_code -code=XXXXXX` prompts, then switch back to systemd service.

---

## Common Operations

### Upgrade code
```bash
cd /opt/trading/app
git pull origin main
docker compose build
docker compose down && docker compose up -d
docker compose logs -f --tail 30
```

### Renew FutuOpenD token
```bash
# 1. On local Mac: open FutuOpenD, complete scan login
# 2. Upload new config
scp FutuOpenD.xml trading@124.156.176.78:/opt/futuopend/
# 3. Fix listen address
sudo sed -i 's|<ip>127.0.0.1</ip>|<ip>0.0.0.0</ip>|' /opt/futuopend/FutuOpenD.xml
# 4. Restart
sudo systemctl restart futuopend
# 5. Verify
nc -z 127.0.0.1 11111 && echo OK
ss -tlnp | grep 11111  # confirm 0.0.0.0
```

### Disk cleanup
```bash
docker system prune -f
find /opt/trading/backups -name "*.tar.gz" -mtime +7 -delete
```

### Edit config
```bash
vim /opt/trading/app/config/us_playbook_settings.yaml
docker compose restart
```