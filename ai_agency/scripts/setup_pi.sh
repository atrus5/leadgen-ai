#!/bin/bash
# One-shot Pi provisioning for the LeadGen AI deploy workflow.
#
# Run with sudo ON THE PI, once. Idempotent — re-running skips steps that
# are already in place. After this finishes, copy the public key it prints
# to your laptop (~/.config/leadgen/age.pub) and you can use deploy.sh.
#
# Requires:
#   - You have already git-cloned /home/$PI_USER/leadgen-ai on the Pi and
#     pulled at least once (so the .service template at
#     ai_agency/scripts/leadgen.service exists).
#   - The operator (laptop owner) has ssh access to the Pi as $PI_USER.
#
# Usage:
#   sudo ./scripts/setup_pi.sh
#
# Env vars (override defaults):
#   PI_USER    username that runs the orchestrator (default: paul)
#   REPO_DIR   absolute path to the git checkout (default: /home/$PI_USER/leadgen-ai)
#
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (or with sudo)." >&2
  exit 1
fi

PI_USER="${PI_USER:-paul}"
REPO_DIR="${REPO_DIR:-/home/$PI_USER/leadgen-ai}"

echo "→ setup_pi.sh starting"
echo "   PI_USER=$PI_USER  REPO_DIR=$REPO_DIR"

# ── 1) Install age ──────────────────────────────────────────────────────
if command -v age >/dev/null; then
  echo "→ age already installed: $(age --version 2>&1 | head -1)"
else
  echo "→ installing age"
  installed=0
  if command -v apt-get >/dev/null && apt-get install -y age >/dev/null 2>&1; then
    installed=1
    echo "   installed via apt"
  elif command -v pip3 >/dev/null && pip3 install --quiet pyage >/dev/null 2>&1; then
    installed=1
    echo "   installed via pip (pyage)"
  fi
  if [[ $installed -eq 0 || ! -x "$(command -v age)" ]]; then
    echo "ERROR: could not install age. Install it manually then re-run this script." >&2
    exit 1
  fi
fi

# ── 2) Generate (or reuse) /etc/leadgen.agekey — Pi's PRIVATE key ──────
mkdir -p /etc

if [[ -s /etc/leadgen.agekey ]]; then
  echo "→ /etc/leadgen.agekey already exists; preserving existing key"
  # Older age-rs writes a header line; modern writes the bech32 directly.
  # `age-keygen -y <priv>` echoes the matching public key to stdout.
  PUB_DISPLAY="$(age-keygen -y /etc/leadgen.agekey 2>/dev/null || true)"
else
  echo "→ generating /etc/leadgen.agekey"
  KEYGEN_LOG="$(mktemp)"
  # age-keygen writes the priv to -o FILE and prints "Public key: age1..."
  # to STDERR. Capture stderr so we can show + persist the public key.
  if ! age-keygen -o /etc/leadgen.agekey 2>"$KEYGEN_LOG"; then
    echo "ERROR: age-keygen failed:" >&2
    cat "$KEYGEN_LOG" >&2
    rm -f "$KEYGEN_LOG"
    exit 1
  fi
  PUB_DISPLAY="$(grep -oE 'age1[a-z0-9]+' "$KEYGEN_LOG" | head -1 || true)"
  rm -f "$KEYGEN_LOG"
fi

# Persist the public key next to the private key for easy copy-paste
if [[ -n "${PUB_DISPLAY:-}" ]]; then
  printf '%s\n' "$PUB_DISPLAY" > /etc/leadgen.agekey.pub
  chown root:root /etc/leadgen.agekey.pub
  chmod 0444 /etc/leadgen.agekey.pub
fi

chmod 0400 /etc/leadgen.agekey
chown root:root /etc/leadgen.agekey

if [[ ! -s /etc/leadgen.agekey ]]; then
  echo "ERROR: /etc/leadgen.agekey missing or empty after setup" >&2
  exit 1
fi

# ── 3) Install the systemd unit ─────────────────────────────────────────
SVC_TEMPLATE="$REPO_DIR/ai_agency/scripts/leadgen.service"
SVC_TARGET="/etc/systemd/system/leadgen.service"
if [[ ! -f "$SVC_TEMPLATE" ]]; then
  echo "WARNING: $SVC_TEMPLATE not found (you may need to git pull first)" >&2
  echo "         skipping systemd unit install — re-run after pulling" >&2
else
  echo "→ installing $SVC_TARGET"
  sed -e "s|__USER__|$PI_USER|g" \
      -e "s|__REPO_DIR__|$REPO_DIR|g" \
      "$SVC_TEMPLATE" > "$SVC_TARGET"
  chown root:root "$SVC_TARGET"
  chmod 0644 "$SVC_TARGET"
  systemctl daemon-reload
  systemctl enable leadgen.service
fi

# ── 4) Passwordless sudoers for deploy-restart from the laptop ──────────
SUDOERS=/etc/sudoers.d/leadgen-deploy
if [[ ! -f "$SUDOERS" ]]; then
  echo "→ writing $SUDOERS"
  cat > "$SUDOERS" <<EOF
# Allow $PI_USER to manage the LeadGen service without a password.
# Consumed by scripts/deploy.sh on the operator's laptop.
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart leadgen.service
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl status leadgen.service
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl --no-pager status leadgen.service
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable leadgen.service
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start leadgen.service
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl is-active leadgen.service
$PI_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop leadgen.service
EOF
  chmod 0440 "$SUDOERS"
  if command -v visudo >/dev/null; then
    visudo -c -f "$SUDOERS" || { echo "ERROR: $SUDOERS failed sudoers syntax check" >&2; exit 1; }
  fi
fi

# ── 5) Tidy lingering (optional, doesn't hurt and helps `systemctl --user`)
if command -v loginctl >/dev/null; then
  loginctl enable-linger "$PI_USER" 2>/dev/null || true
fi

echo
echo "✓ Pi-side setup complete."
echo
if [[ -n "${PUB_DISPLAY:-}" ]]; then
  echo "Copy this Pi public key to your laptop (~/.config/leadgen/age.pub):"
  echo
  echo "  $PUB_DISPLAY"
  echo
fi
echo "Next steps (on the laptop):"
echo "  1) Build ~/leadgen.env   (LEADGEN_FLASK_SECRET + LEADGEN_MASTER_KEY)"
echo "  2) ./scripts/deploy.sh   (encrypts + pushes + restarts Pi)"
