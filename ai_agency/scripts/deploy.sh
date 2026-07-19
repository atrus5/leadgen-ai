#!/bin/bash
# Laptop-side deploy orchestrator for the LeadGen AI Pi.
#
# Flow:
#   1) If ~/leadgen.env exists, encrypt it with the Pi's PUBLIC age key
#      into the in-repo leadgen.env.enc blob (never syncs plaintext).
#   2) Commit + push any pending code + the encrypted blob.
#   3) SSH to the Pi, git pull, and (unless --no-restart) restart the
#      leadgen.service systemd unit. ExecStartPre decrypts the env first,
#      so the new plaintext is in /etc/leadgen.env on the next start.
#
# Usage:
#   ./scripts/deploy.sh                # full pipeline
#   ./scripts/deploy.sh --code-only    # skip the env-file encryption step
#   ./scripts/deploy.sh --no-restart   # push but don't restart the Pi service
#   ./scripts/deploy.sh --dry-run      # print what would happen; do nothing
#
# Required laptop setup (one time):
#   ~/.config/leadgen/age.pub   # Pi's public age key — copy from /etc/leadgen.agekey.pub
#                                # after running scripts/setup_pi.sh on the Pi.
#   ~/leadgen.env               # plaintext env (gitignored locally).
#
# Required Pi setup (one time):
#   sudo ./scripts/setup_pi.sh
#
set -euo pipefail

# ── Defaults (override with PI_HOST=… ./scripts/deploy.sh) ─────────────────
PI_HOST="${PI_HOST:-10.0.0.8}"
PI_USER="${PI_USER:-paul}"
PI_DIR="${PI_DIR:-/home/$PI_USER/leadgen-ai}"
AGE_PUB="${AGE_PUB:-$HOME/.config/leadgen/age.pub}"
PLAIN_ENV="${PLAIN_ENV:-$HOME/leadgen.env}"
ENC_FILENAME="${ENC_FILENAME:-leadgen.env.enc}"
ENC_PATH="$PI_DIR/$ENC_FILENAME"
REMOTE_UNIT="${REMOTE_UNIT:-leadgen.service}"

code_only=0
no_restart=0
dry_run=0

for arg in "$@"; do
  case "$arg" in
    --code-only)  code_only=1 ;;
    --no-restart) no_restart=1 ;;
    --dry-run)    dry_run=1 ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      echo "try: $0 --help" >&2
      exit 2
      ;;
  esac
done

# Run an action — print loudly under --dry-run, do the work otherwise
dry() {
  if [[ $dry_run -eq 1 ]]; then echo "[dry-run] $*"; else echo "$@"; fi
}
run() {
  if [[ $dry_run -eq 1 ]]; then echo "[dry-run] $*"; else eval "$@"; fi
}

# ── Pre-flight ───────────────────────────────────────────────────────────
if [[ $code_only -eq 0 && ! -f "$AGE_PUB" ]]; then
  echo "ERROR: $AGE_PUB not found." >&2
  echo "  Run scripts/setup_pi.sh on the Pi and copy /etc/leadgen.agekey.pub to" >&2
  echo "  $AGE_PUB on the laptop." >&2
  exit 1
fi

cd "$PI_DIR"

# ── 1) Encrypt plaintext → in-repo blob ──────────────────────────────────
if [[ $code_only -eq 0 ]]; then
  if [[ -f "$PLAIN_ENV" ]]; then
    dry "→ encrypting $PLAIN_ENV → $ENC_PATH with Pi's public age key"
    run "age -R '$AGE_PUB' -o '$ENC_PATH' '$PLAIN_ENV'"
    run "git add '$ENC_PATH'"
  else
    echo "note: no $PLAIN_ENV — skipping encryption (use --code-only to suppress)" >&2
  fi
fi

# ── 2) Commit + push any pending changes ──────────────────────────────────
if ! git diff --cached --quiet 2>/dev/null || ! git diff --quiet 2>/dev/null; then
  msg="deploy: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  dry "→ committing + pushing: $msg"
  run "git commit -am '$msg'"
  BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  run "git push origin '$BRANCH'"
else
  echo "→ no local changes to commit"
fi

# ── 3) Tell the Pi to pull ───────────────────────────────────────────────
dry "→ ssh $PI_USER@$PI_HOST: cd $PI_DIR && git pull --ff-only"
run "ssh '$PI_USER@$PI_HOST' 'cd $PI_DIR && git pull --ff-only'"

# ── 4) Restart the systemd unit (Pi re-decrypts /etc/leadgen.env on start)
if [[ $no_restart -eq 0 ]]; then
  dry "→ ssh …: sudo systemctl restart $REMOTE_UNIT"
  run "ssh '$PI_USER@$PI_HOST' 'sudo systemctl restart $REMOTE_UNIT && sudo systemctl --no-pager --full status $REMOTE_UNIT'"
fi

echo "✓ deploy complete."
