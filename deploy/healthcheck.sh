#!/usr/bin/env bash
# healthcheck.sh — FutuOpenD + Docker + 系统资源 健康检查
# cron: */5 * * * * /opt/trading/deploy/healthcheck.sh
set -euo pipefail

# ---------- 配置 ----------
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
FUTU_PORT=11111
COMPOSE_DIR="/opt/trading/app"
CONTAINER_NAME="app-playbook-1"
DISK_THRESHOLD=90    # 磁盘使用率告警阈值 (%)
MEM_FREE_MIN=15      # 内存可用百分比告警阈值 (%)
LOG_FILE="/var/log/trading-healthcheck.log"

# 从 .env 读取 Telegram 凭据（如果环境变量未设置）
if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
    ENV_FILE="${COMPOSE_DIR}/.env"
    if [[ -f "$ENV_FILE" ]]; then
        TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "$ENV_FILE" || true)
        TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "$ENV_FILE" || true)
    fi
fi

# ---------- 辅助函数 ----------
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

send_alert() {
    local msg="⚠️ *Trading Monitor Alert*\n\n$1\n\n_Host: $(hostname)_\n_Time: $(date '+%Y-%m-%d %H:%M:%S')_"
    if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="$msg" \
            -d parse_mode="Markdown" \
            --max-time 10 > /dev/null 2>&1 || true
    fi
    log "ALERT: $1"
}

ALERTS=""

# ---------- 检查 1: FutuOpenD 端口 ----------
if ! nc -z 127.0.0.1 "$FUTU_PORT" 2>/dev/null; then
    log "FutuOpenD port $FUTU_PORT unreachable, attempting restart..."
    sudo systemctl restart futuopend 2>/dev/null || true
    sleep 5
    if ! nc -z 127.0.0.1 "$FUTU_PORT" 2>/dev/null; then
        ALERTS="${ALERTS}🔴 FutuOpenD 端口 ${FUTU_PORT} 不可达，重启后仍无法恢复\n"
    else
        ALERTS="${ALERTS}🟡 FutuOpenD 已自动重启恢复\n"
    fi
fi

# ---------- 检查 2: Docker 容器状态 ----------
CONTAINER_STATUS=$(docker inspect --format='{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "not_found")
if [[ "$CONTAINER_STATUS" != "running" ]]; then
    ALERTS="${ALERTS}🔴 容器 ${CONTAINER_NAME} 状态异常: ${CONTAINER_STATUS}\n"
    # 尝试重启
    cd "$COMPOSE_DIR" && docker compose up -d 2>/dev/null || true
fi

# 检查容器健康状态
CONTAINER_HEALTH=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "unknown")
if [[ "$CONTAINER_HEALTH" == "unhealthy" ]]; then
    ALERTS="${ALERTS}🟡 容器 healthcheck 报告 unhealthy\n"
fi

# ---------- 检查 3: 磁盘空间 ----------
DISK_USAGE=$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')
if [[ "$DISK_USAGE" -ge "$DISK_THRESHOLD" ]]; then
    ALERTS="${ALERTS}🟠 磁盘使用率 ${DISK_USAGE}% (阈值 ${DISK_THRESHOLD}%)\n"
fi

# ---------- 检查 4: 内存 ----------
MEM_FREE_PCT=$(free | awk '/^Mem:/ {printf "%.0f", $7/$2*100}')
if [[ "$MEM_FREE_PCT" -le "$MEM_FREE_MIN" ]]; then
    ALERTS="${ALERTS}🟠 可用内存仅 ${MEM_FREE_PCT}% (阈值 ${MEM_FREE_MIN}%)\n"
fi

# ---------- 发送汇总告警 ----------
if [[ -n "$ALERTS" ]]; then
    send_alert "$ALERTS"
else
    log "OK - all checks passed"
fi
