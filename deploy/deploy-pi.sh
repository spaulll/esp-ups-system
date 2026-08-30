#!/usr/bin/env bash
# deploy-pi.sh — env-inject, push, install, verify (UPGRADE_PLAN §7.1)
#
# Usage: ./deploy/deploy-pi.sh [--dry-run]
#   --dry-run  run gates + inject only; nothing touches the Pi
#
# Idempotent: safe to run twice in a row (backup -> copy -> restart -> verify).
# The pushed ups-monitor.py is self-contained (creds embedded); the RAM copy in
# /dev/shm dies with this script. The Pi itself has no .env.
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; fi

[ -f .env ] || { echo ".env missing — cp .env.example .env"; exit 1; }
set -a; . ./.env; set +a
PI="${PI_SSH:?set PI_SSH in .env}"
PASS="${PI_PASS:?set PI_PASS in .env}"

echo "==> 0. Gates: sanitize, stub check, clean tree, git tag"
python3 scripts/sanitize.py >/dev/null   # hard gate: placeholders only, exit 1 = blocked

for f in pi/ups-monitor.py pi/ups-monitor.service pi/logrotate-ups-monitor; do
    if grep -q "__STUB__" "$f"; then
        echo "REFUSING: $f is a Phase-0 stub (real code lands in Phase 2)."
        exit 1
    fi
done

if [ $DRY -eq 0 ] && [ -n "$(git status --porcelain)" ]; then
    echo "REFUSING: dirty git tree — commit first so the tag matches the code."
    exit 1
fi

TAG="pi-$(date +%Y%m%d-%H%M%S)"
if [ $DRY -eq 0 ]; then
    git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git tag "$TAG"
    echo "tagged $TAG"
fi

command -v sshpass >/dev/null || { echo "sshpass not installed"; exit 1; }

SSH() { sshpass -p "$PASS" ssh "$@"; }
SCP() { sshpass -p "$PASS" scp "$@"; }
STAMP=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d /dev/shm/ups-pi-XXXXX); trap 'rm -rf "$TMP"' EXIT

echo "==> 1. Inject env -> self-contained RAM copy (creds embedded, never on disk here)"
python3 scripts/inject.py pi/ups-monitor.py > "$TMP/ups-monitor.py"

if [ $DRY -eq 1 ]; then
    echo "dry-run OK: injected $(wc -l < "$TMP/ups-monitor.py") lines; no changes made."
    exit 0
fi

echo "==> 2. Remote backup"
SSH "$PI" "sudo mkdir -p /var/backups/ups-monitor/$STAMP && \
  sudo cp /usr/local/bin/ups-monitor.py /etc/systemd/system/ups-monitor.service \
          /etc/logrotate.d/ups-monitor /var/backups/ups-monitor/$STAMP/ 2>/dev/null || true"

echo "==> 3. Push code (self-contained) + service + logrotate"
SCP "$TMP/ups-monitor.py"     "$PI:/tmp/"
SCP pi/ups-monitor.service    "$PI:/tmp/"
SCP pi/logrotate-ups-monitor  "$PI:/tmp/"

echo "==> 4. Install + restart + verify"
SSH "$PI" "sudo mv /tmp/ups-monitor.py /usr/local/bin/ups-monitor.py && \
  sudo chmod 755 /usr/local/bin/ups-monitor.py && \
  sudo mv /tmp/ups-monitor.service /etc/systemd/system/ups-monitor.service && \
  sudo mv /tmp/logrotate-ups-monitor /etc/logrotate.d/ups-monitor && \
  sudo systemctl daemon-reload && sudo systemctl restart ups-monitor && sleep 5 && \
  systemctl is-active ups-monitor && sudo journalctl -u ups-monitor -n 10 --no-pager"

echo "Deploy OK — /status from Telegram is the human check."
