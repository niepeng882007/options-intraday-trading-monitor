#!/usr/bin/env bash
# backup.sh — 每日备份 SQLite + watchlist + config
# cron: 0 5 * * * /opt/trading/deploy/backup.sh
set -euo pipefail

# ---------- 配置 ----------
APP_DIR="/opt/trading/app"
BACKUP_DIR="/opt/trading/backups"
RETENTION_DAYS=30
DATE=$(date '+%Y%m%d_%H%M%S')
BACKUP_PATH="${BACKUP_DIR}/${DATE}"
LOG_FILE="/var/log/trading-backup.log"

# Telegram 凭据（用于失败告警）
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
    ENV_FILE="${APP_DIR}/.env"
    if [[ -f "$ENV_FILE" ]]; then
        TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "$ENV_FILE" || true)
        TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "$ENV_FILE" || true)
    fi
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

send_alert() {
    local msg="⚠️ *Backup Alert*\n\n$1\n\n_Host: $(hostname)_"
    if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="$msg" \
            -d parse_mode="Markdown" \
            --max-time 10 > /dev/null 2>&1 || true
    fi
}

# ---------- 创建备份目录 ----------
mkdir -p "$BACKUP_PATH"

# ---------- 1. SQLite 安全备份 ----------
DB_FILE="${APP_DIR}/data/monitor.db"
if [[ -f "$DB_FILE" ]]; then
    sqlite3 "$DB_FILE" ".backup '${BACKUP_PATH}/monitor.db'" 2>/dev/null
    if [[ $? -eq 0 ]]; then
        log "OK - SQLite backup: ${BACKUP_PATH}/monitor.db"
    else
        # fallback: 直接复制
        cp "$DB_FILE" "${BACKUP_PATH}/monitor.db"
        log "WARN - SQLite .backup failed, used cp fallback"
    fi
else
    log "SKIP - ${DB_FILE} not found"
fi

# ---------- 2. Watchlist JSON ----------
for f in hk_watchlist.json us_watchlist.json earnings_cache.json; do
    SRC="${APP_DIR}/data/${f}"
    if [[ -f "$SRC" ]]; then
        cp "$SRC" "${BACKUP_PATH}/${f}"
        log "OK - copied ${f}"
    fi
done

# ---------- 3. Config 快照 ----------
if [[ -d "${APP_DIR}/config" ]]; then
    cp -r "${APP_DIR}/config" "${BACKUP_PATH}/config"
    log "OK - config snapshot"
fi

# ---------- 4. .env 备份 ----------
if [[ -f "${APP_DIR}/.env" ]]; then
    cp "${APP_DIR}/.env" "${BACKUP_PATH}/.env"
    chmod 600 "${BACKUP_PATH}/.env"
    log "OK - .env backup (permissions 600)"
fi

# ---------- 5. 压缩 ----------
cd "$BACKUP_DIR"
tar czf "${DATE}.tar.gz" "${DATE}/" && rm -rf "${DATE}/"
log "OK - compressed to ${DATE}.tar.gz"

# ---------- 6. 清理过期备份 ----------
DELETED=$(find "$BACKUP_DIR" -name "*.tar.gz" -mtime +${RETENTION_DAYS} -delete -print | wc -l)
if [[ "$DELETED" -gt 0 ]]; then
    log "OK - cleaned ${DELETED} backups older than ${RETENTION_DAYS} days"
fi

log "Backup complete: ${DATE}.tar.gz"
