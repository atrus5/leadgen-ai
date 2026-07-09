#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="$HOME/agency_backups"
TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
mkdir -p "$BACKUP_DIR"

# Snapshot code + SQLite archive
tar -czf "$BACKUP_DIR/agency_system_snapshot_$TIMESTAMP.tar.gz" \
    -C "$PROJECT_ROOT" \
    public hunters outreach scheduler scripts config db.py config.py requirements.txt \
    Caddyfiles.txt docs \
    data/agency.db 2>/dev/null

# Housekeeping
find "$BACKUP_DIR" -type f -name "*.tar.gz" -mtime +30 -delete

echo "System state archival stored at:"
echo "  $BACKUP_DIR/agency_system_snapshot_$TIMESTAMP.tar.gz"
