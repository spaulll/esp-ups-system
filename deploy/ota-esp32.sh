#!/usr/bin/env bash
# ota-esp32.sh — env-inject, build in RAM, OTA, verify (UPGRADE_PLAN §7.2)
#
# Usage: ./deploy/ota-esp32.sh
#
# firmware.bin embeds WiFi/OTA creds — built only inside /dev/shm, never
# archived on this machine. Rollback = git checkout <old tag> + re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo ".env missing — cp .env.example .env"; exit 1; }
set -a; . ./.env; set +a
ESP="${ESP32_IP:?set ESP32_IP in .env}"
OTA_PASS="${OTA_PASSWORD:?set OTA_PASSWORD in .env}"

echo "==> 0. Gates: sanitize, stub check, fw version, clean tree, git tag"
python3 scripts/sanitize.py >/dev/null   # hard gate: placeholders only, exit 1 = blocked

if grep -q "__STUB__" firmware/src/main.cpp; then
    echo "REFUSING: firmware is a Phase-0 stub (real fw lands in Phase 1)."
    exit 1
fi

FW=$(grep -oP 'FW_VERSION\s*=\s*"\K[^"]+' firmware/src/main.cpp) || {
    echo "FW_VERSION not found in firmware/src/main.cpp"; exit 1;
}

if [ -n "$(git status --porcelain)" ]; then
    echo "REFUSING: dirty git tree — commit first so the tag matches the code."
    exit 1
fi

TAG="esp-$(date +%Y%m%d-%H%M%S)"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git tag "$TAG"
echo "tagged $TAG"

command -v pio >/dev/null || { echo "pio (PlatformIO CLI) not installed"; exit 1; }

TMP=$(mktemp -d /dev/shm/ups-fw-XXXXX); trap 'rm -rf "$TMP"' EXIT

echo "==> 1. Copy project to RAM + inject env"
cp -r firmware "$TMP/firmware"
python3 scripts/inject.py "$TMP/firmware/src/main.cpp" --inplace

echo "==> 2. Build V$FW"
pio run -d "$TMP/firmware"

echo "==> 3. Pre-flight: reachable + idle (never OTA mid-incident)"
curl -sf -m 5 "http://$ESP/state" -o "$TMP/state.json"
python3 - "$TMP/state.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
flags = ("sdMains", "sdWAN", "sdManual")
if all(k in d for k in flags + ("mainsFailSinceMs",)):
    assert not (d["sdMains"] or d["sdWAN"] or d["sdManual"]), "shutdown flag set!"
    assert d["mainsFailSinceMs"] == 0, "countdown running!"
    print("pre-flight OK")
else:
    print("v1 firmware detected — pre-flight limited (v2 fields missing); continuing")
EOF

echo "==> 4. OTA push"
pio run -d "$TMP/firmware" -t upload --upload-port "$ESP"
# fallback: espota.py -i "$ESP" -p 3232 --auth "$OTA_PASS" \
#   -f "$TMP/firmware/.pio/build/esp32/firmware.bin"

echo "==> 5. Verify"
sleep 12
curl -sf -m 8 "http://$ESP/state" -o "$TMP/state2.json"
GOT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["fw"])' "$TMP/state2.json")
if [ "$GOT" = "$FW" ]; then
    echo "OTA verified"
else
    echo "MISMATCH (got $GOT)"; exit 1
fi
