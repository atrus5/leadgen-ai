#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="$HOME/agency_backups"
TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
mkdir -p "$BACKUP_DIR"

# Snapshot code + SQLite archive. Multi-tenant layout: platform.db (the
# workspaces/users/invites table) plus every workspace's own agency.db
# under data/workspaces/<id>/ — there's no single data/agency.db anymore.
tar -czf "$BACKUP_DIR/agency_system_snapshot_$TIMESTAMP.tar.gz" \
    -C "$PROJECT_ROOT" \
    public hunters outreach scheduler scripts config db.py config.py platform_db.py \
    auth.py admin.py invites.py mail.py requirements.txt \
    Caddyfiles.txt docs \
    data/platform.db data/workspaces 2>/dev/null

# Housekeeping
find "$BACKUP_DIR" -type f -name "*.tar.gz" -mtime +30 -delete

echo "System state archival stored at:"
echo "  $BACKUP_DIR/agency_system_snapshot_$TIMESTAMP.tar.gz"
