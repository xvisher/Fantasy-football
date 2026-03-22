#!/usr/bin/env bash
# =============================================================================
# FPL Bot — manual database backup
#
# Creates a timestamped copy of fpl_bot.db from the Docker volume.
# Backup location: ./backups/fpl_bot_YYYYMMDD_HHMMSS.db
#
# Usage:
#   bash scripts/backup-db.sh
#   bash scripts/backup-db.sh /path/to/backup/dir   # custom dir
# =============================================================================

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/fpl_bot_${TIMESTAMP}.db"

mkdir -p "$BACKUP_DIR"

# Find the fpl-data volume
VOLUME=$(docker volume ls --format '{{.Name}}' | grep fpl-data | head -1)

if [[ -z "$VOLUME" ]]; then
    echo "[backup] No fpl-data volume found — has the bot run at least once?"
    exit 1
fi

echo "[backup] Copying from volume $VOLUME → $BACKUP_FILE"
docker run --rm \
    -v "${VOLUME}:/data:ro" \
    -v "$(realpath "$BACKUP_DIR"):/backup" \
    alpine \
    cp /data/fpl_bot.db "/backup/fpl_bot_${TIMESTAMP}.db"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[backup] Done — $BACKUP_FILE ($SIZE)"

# Print decision log summary
echo ""
echo "[backup] Decision log summary:"
docker run --rm \
    -v "${VOLUME}:/data:ro" \
    alpine sh -c \
    'apk add -q sqlite && sqlite3 /data/fpl_bot.db \
    "SELECT gw, action_type, printf(\"xP %.1f\", predicted_xp), \
     CASE WHEN actual_pts IS NULL THEN \"pending\" ELSE printf(\"actual %.0f\", actual_pts) END, \
     CASE WHEN dry_run=1 THEN \"[DRY]\" ELSE \"\" END \
     FROM decisions_log ORDER BY id DESC LIMIT 10;"' 2>/dev/null || true
