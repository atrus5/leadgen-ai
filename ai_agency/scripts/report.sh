#!/bin/bash
# Reads the SQLite database and prints a weekly agency report.
# Falls back to the legacy CSV if the DB file doesn't exist yet.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_FILE="$SCRIPT_DIR/../data/agency.db"
PRICE_PER_HOT=150

if [ ! -f "$DB_FILE" ]; then
    echo "========================================="
    echo "   WEEKLY AGENCY REVENUE REPORT          "
    echo "   Generated on: $(date)"
    echo "========================================="
    echo "No SQLite DB yet — first boot creates the file at $DB_FILE"
    exit 0
fi

TOTAL_EMAILS=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM prospect_emails WHERE status='sent'")
TOTAL_REPLIES=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM replies")
HOT_REPLIES=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM replies WHERE intent='HOT'")
FORWARDED=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM replies WHERE forwarded=1")
NEW_PROSPECTS=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM prospects WHERE date(added_at) >= date('now','-7 day')")
ACTIVE_CLIENTS=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM clients WHERE status='active'")

echo "========================================="
echo "   WEEKLY AGENCY REVENUE REPORT          "
echo "   Generated on: $(date)"
echo "========================================="
printf "  Active clients          %s\n" "$ACTIVE_CLIENTS"
printf "  New prospects (7 days)  %s\n" "$NEW_PROSPECTS"
printf "  Emails sent (all-time)  %s\n" "$TOTAL_EMAILS"
printf "  Replies received        %s\n" "$TOTAL_REPLIES"
printf "  HOT replies             %s\n" "$HOT_REPLIES"
printf "  Leads forwarded         %s\n" "$FORWARDED"
echo "  -----"
echo "  Performance Revenue Earned (HOT * $${PRICE_PER_HOT}): $((HOT_REPLIES * PRICE_PER_HOT)) CAD"
echo "========================================="
