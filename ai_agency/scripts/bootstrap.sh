#!/bin/bash
# Bootstraps the LeadGen AI orchestrator on a fresh Linux host.
#   1) creates a Python venv at .venv
#   2) installs requirements.txt (+ checks the LEADGEN_* env vars)
#   3) initialises the SQLite database (data/agency.db)
#   4) writes a sample config/settings.json if missing
#   5) starts the Flask app on port 5000 (foreground by default)
#
# Env vars expected in production (write them to /etc/leadgen.env and
# `source` it from a systemd unit or your supervisor of choice):
#
#   LEADGEN_FLASK_SECRET   strong random hex, used to sign session
#                          cookies. Session-stable across restarts.
#   LEADGEN_MASTER_KEY     passphrase for settings-at-rest encryption.
#                          Rotate by rotating the env var AND running
#                          scripts/encrypt_secrets.py to re-encrypt.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PROJECT_ROOT points at the parent of ai_agency/ so the `ai_agency`
# package can be imported as a normal package from the inline python and
# from `python -m ai_agency.app`.
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# PACKAGE_ROOT points one level up so we can run imports correctly.
PACKAGE_PARENT="$(cd "$PROJECT_ROOT/.." && pwd)"

# Source the operator's env file if it exists, but don't fail if missing
# (we just warn about the consequences below).
if [ -f /etc/leadgen.env ]; then
    echo "✓ sourcing /etc/leadgen.env"
    set -a
    # shellcheck disable=SC1091
    . /etc/leadgen.env
    set +a
fi

# Warn loudly if the production env vars are missing. Don't fail — the
# orchestrator runs in DEV MODE with random keys + plaintext secrets,
# which is fine for getting started locally.
if [ -z "${LEADGEN_FLASK_SECRET:-}" ]; then
    echo "⚠️  LEADGEN_FLASK_SECRET is unset — sessions will reset on every restart." >&2
fi
if [ -z "${LEADGEN_MASTER_KEY:-}" ]; then
    echo "⚠️  LEADGEN_MASTER_KEY is unset — settings stored in plaintext SQLite." >&2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

# 1) venv
if [ ! -d "$PROJECT_ROOT/.venv" ]; then
    echo "→ creating virtualenv"
    "$PYTHON_BIN" -m venv "$PROJECT_ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$PROJECT_ROOT/.venv/bin/activate"

# 2) pip install
echo "→ installing requirements"
pip install --upgrade pip >/dev/null
pip install -r "$PROJECT_ROOT/requirements.txt"

# 3) init platform.db (idempotent). Per-workspace agency.db files are
# initialised lazily on first touch (see db.py) — there's no "the"
# database anymore to eagerly create here.
# Run from PACKAGE_PARENT so `from ai_agency import platform_db` resolves
# correctly because `ai_agency/` is then a top-level package on the cwd.
echo "→ initialising platform database"
cd "$PACKAGE_PARENT"
PYTHONPATH="$PACKAGE_PARENT" python - <<'PY'
from ai_agency import platform_db
platform_db.init_schema()
platform_db.ensure_default_platform_settings()
print("✓ platform db ready at", platform_db.DB_PATH)
print("  If this is a fresh install, create your admin account with:")
print("    python -m ai_agency.scripts.create_admin --email you@example.com")
print("  If migrating an existing single-tenant install, instead run:")
print("    python -m ai_agency.scripts.migrate_to_workspace")
PY

# 4) start
echo "→ starting Flask app on 0.0.0.0:5000"
cd "$PACKAGE_PARENT"
exec env PYTHONPATH="$PACKAGE_PARENT" \
    LEADGEN_FLASK_SECRET="${LEADGEN_FLASK_SECRET:-}" \
    LEADGEN_MASTER_KEY="${LEADGEN_MASTER_KEY:-}" \
    python -m ai_agency.app
