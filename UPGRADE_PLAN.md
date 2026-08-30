# UPS Monitor v2 — Complete Rebuild Plan

**Philosophy:** v1 inferred power from network reachability (192.168.0.2 probe) and spent its life fighting TCP noise. v2 **measures electricity directly** — the network exists only to act (shutdown webhook, WOL) and to report (Telegram/ntfy).

| Concern | v1 (inspiration) | v2 (this rebuild) |
|---|---|---|
| Mains detection | TCP connect to extender :80 | **Optocoupler on GPIO — no network in the path** |
| Fake powercuts | Constant (2% probe failures) | Impossible by design (electrical signal) |
| Mains → shutdown | 5 min (TG-adjustable) | Same — 5 min default, TG-adjustable 1–720 |
| Mains restore → wake | WOL + Pi-side verify | **15s settle → WOL → poll Proxmox → re-WOL if not up (max 5)** |
| WAN detection | TCP :53 to 8.8.8.8/1.1.1.1 | Same (network is inherently the signal here) |
| WAN → shutdown | 10 min | Same; WAN restore → WOL |
| Notifications | Fire-and-forget webhooks | Event ledger (seq) + Pi reconciler → nothing lost, no duplicates |
| Pi role | Shadow state machine | Thin: reconcile, notify (TG→ntfy), commands |

---

## Phase Progress Tracker

| Phase | Title | Status | Acceptance gate |
|---|---|---|---|
| 0 | Repo scaffold + deploy tooling | ✅ Done | `deploy-pi.sh` idempotent, git tags work |
| 1 | Firmware v2 core (GPIO mains, state machine, actuation) | ☐ Not started | Bench test with jumper wire: full shutdown→WOL cycle |
| 2 | Pi brain v2 (reconciler, TG, ntfy, alert engine) | ☐ Not started | Kill-restart drill: zero lost/duplicate alerts |
| 3 | Optocoupler hardware bring-up | ⏸ Awaiting part | 20/20 real unplug cycles, 0 false triggers in 7-day soak |
| 4 | UX polish + observability | ☐ Not started | Drill matrix §5 produces exactly the documented messages |
| 5 | Final validation & sign-off | ☐ Not started | All fail-drills pass |

Status legend: `☐ Not started` · `🔶 In progress` · `✅ Done` · `⏸ Blocked`

---

## Target Repository Layout

```
ups-system/
├── .env.example               # secrets template — cp to .env (git-ignored), chmod 600
├── scripts/
│   ├── sanitize.py            # scrubs secrets from repo -> __PLACEHOLDER__ tokens
│   └── inject.py              # fills __PLACEHOLDER__ from env at deploy time
├── firmware/                  # ESP32, PlatformIO (placeholders only, no creds)
│   ├── platformio.ini
│   └── src/main.cpp
├── pi/                        # placeholders only; deploy embeds creds into the pushed copy
│   ├── ups-monitor.py         # Telegram bot + reconciler + webhook receiver
│   ├── ups-monitor.service
│   └── logrotate-ups-monitor
├── hardware/
│   └── optocoupler-wiring.md  # BOM, schematic, photos, calibration notes
├── deploy/
│   ├── deploy-pi.sh           # inject -> /dev/shm -> push -> install
│   └── ota-esp32.sh           # inject -> build in /dev/shm -> OTA -> verify
└── tests/                     # Pi-side pytest (event classification, coalescer)
```

---

## Core State Machine (firmware — the whole system in one diagram)

```
                    ┌────────────── NORMAL (node up) ──────────────┐
                    │                                              │
   GPIO low ≥3s     │                                              │   WAN fail ≥10min
   (debounced)      ▼                                              ▼
          ┌ MAINS COUNTDOWN ──5min──> SHUTDOWN(node, "mains")   SHUTDOWN(node,"wan")
          │      │ ▲                    │                              │
          │ GPIO │ └ cancel + notify    │ flags: sdMains=1 (persist)   │ flags: sdWAN=1
          │ high │                      ▼                              ▼
          └──────┘             ┌──────── NODE DOWN (waiting) ────────┘
                                       │
       mains restore (GPIO high ≥3s)   │   WAN restore (wanUp ≥30s)
       AND sdMains (or manual-off-     │   AND sdWAN
       while-mains-down)               │
                    └────> wait 15s ──> WOL ──> liveness: ICMP ping + TCP :8006
                                               │ every 15s; still down after 120s
                                               │ → WOL again (max 5 attempts)
                                               ▼
                                    clear flags, notify "online confirmed"
```

**Combination rules (edge cases):**

| Situation | Behavior |
|---|---|
| Mains AND WAN down | Shutdown fires on whichever timer expires first; both flags set |
| Both flags set | Wake only when **both** causes have cleared (mains GPIO high + WAN up) |
| `/off` (manual) while mains down | `manualOffWhileMainsDown=1` → auto-restore allowed on mains return |
| `/off` (manual) normally | `sdManual=1` → never auto-restores; `/on` required |
| `/on` while mains countdown running | `manualOverride=1` → suppresses this mains auto-shutdown until restore |
| GPIO low but WAN up (blip <3s) | Counted as `blip`, logged, no countdown |
| WiFi down during countdown | Countdown keeps running; shutdown webhook queued and retried on reconnect |
| WiFi down while node healthy | `/state` unavailable; Pi marks sensor blind (no fake alerts — GPIO isn't polled over network) |
| Proxmox already off at shutdown time | Webhook fails → retry ×6 → flag stays set, wake logic still armed |

---

## Phase 0 — Repo Scaffold + Deploy Tooling

- [x] `git init` in `/root/ups-system`, v1 preserved on `legacy` branch, target layout created (`firmware/`, `pi/`, `hardware/`, `tests/` as `__STUB__` placeholders)
- [x] Write `deploy/deploy-pi.sh` (backup → copy → restart → health check; §7.1) — + `--dry-run`, sanitize/stub/dirty-tree gates, per-deploy tag `pi-<date>`
- [x] Write `deploy/ota-esp32.sh` (build → OTA → fw-verify; §7.2) — + v1/v2-aware pre-flight, per-deploy tag `esp-<date>`
- [x] Secrets flow in place: `.env` (git-ignored) + `.env.example` template; repo sources carry `__PLACEHOLDER__` tokens only (`scripts/sanitize.py --clean`); deploy injects via `scripts/inject.py` into `/dev/shm` — cred-embedded code never persisted locally
- [x] Real `.env` created from template (chmod 600, key check passed)
- [ ] **Remaining (user):** **rotate the tokens v1 exposed** (TG bot token, PVE token secret, WiFi/OTA passes) before first deploy; live double-run of both scripts happens at first v2 deploy
- [x] Add `git tag` discipline to deploy scripts (per-deploy `pi-<date>` / `esp-<date>` tags; dirty tree refuses to deploy)

**Acceptance:** running each script twice in a row changes nothing and breaks nothing.

---

## Phase 1 — Firmware v2 Core 🔴

> PlatformIO project. Everything on this phase is testable on the bench **before** the optocoupler arrives: mains input is a GPIO that we drive with a jumper wire / pushbutton.

### 1.1 Inputs
- [ ] GPIO input (e.g. GPIO 27, `INPUT_PULLUP`, optocoupler pulls LOW when mains present) with 500ms hardware-ish debounce + **3s stability rule**: 3s low = mains down, 3s high = mains up
- [ ] WAN check: TCP `8.8.8.8:53` / `1.1.1.1:53` (2s budget, any success = up), every 15s
- [ ] No reference to 192.168.0.2 anywhere — the extender is dead to us

### 1.2 Timers & policy (all persisted in NVS, all TG-adjustable)
- [ ] `mainsDelayMs` — default 300000 (5 min); `/mainsdelay <1–720>` from TG; persisted
- [ ] `wanTimeoutMs` — default 600000 (10 min); TG-adjustable for symmetry
- [ ] Restore sequence constants: `SETTLE_MS = 15000`, `PROX_POLL_SEC = 15`, `RE_WOL_AFTER = 120s`, `MAX_WOL = 5`

### 1.3 Actuation
- [ ] Shutdown: `GET http://192.168.0.50:9999/shutdown` (agent on the node) — on failure retry every 10s ×6; every attempt logged to ledger (`shutdown_webhook_ok/failed`)
- [ ] Wake: `SETTLE_MS` → WOL broadcast ×3 packets 1s apart → liveness check every 15s (**ICMP ping, then TCP :8006 — both must pass**) → still down after 120s → re-WOL (≤5 attempts) → `wol_rexmitted` events; give-up emits `wake_failed` (critical)
- [ ] Flags: `sdMains`, `sdWAN`, `sdManual`, `manualOffWhileMainsDown`, `manualOverride` — NVS-persisted, semantics per edge-case table

### 1.4 Interfaces (keep from v1, they were sound)
- [ ] `GET /state` — flags, timers, `mainsRaw`, `wanUp`, `espUptimeMs`, `espResetReason`, heap, rssi (cached, fixed -1 bug), counters, `fw`
- [ ] `GET /events?since=N` — RAM ring buffer (32) of `{seq, event, uptimeMs, data}`; NVS-persisted monotonic `seq`
- [ ] `POST /command` — `wake`, `shutdown`, `mainsdelay`, `wantimeout`, `set_gpio_test` (simulate mains low/high for bench drills) — all deferred out of server context
- [ ] `notifyPi()` webhook = fast-path nudge only (fire-and-forget OK; ledger is authority), carries `&seq=`
- [ ] WiFi: non-blocking reconnect (BSSID-locked), task watchdogs on network tasks, `WiFi.setSleep(false)`

**Phase 1 acceptance (bench):** jumper-wire drill — pull GPIO low 5+ min → shutdown webhook hits test receiver; restore → 15s → WOL packets seen (Wireshark/tcpdump) → re-WOL fires if :8006 unreachable; `/events` replays cleanly to a mock Pi; ESP32 reboot mid-countdown resumes correctly from NVS.

---

## Phase 2 — Pi Brain v2 🟠

> Thin by design: **no state mutation from webhooks ever**. The ESP32 is the single source of truth; the Pi reconciles, notifies, and commands.

- [ ] Reconciler: poll `/state` + `/events?since=<last_seq>` every 15s; process unseen events exactly once (seq gaps → `event_log_gap` alert); counters (`mains_down`, `shutdowns`, `blips`) bump here only
- [ ] Webhook receiver `:9997`: token-auth (`/notify?event=..&seq=..&token=..`), 403 otherwise, idempotent on duplicate seq, triggers an immediate reconcile pass (latency nudge, not authority)
- [ ] Telegram: long-poll with **persisted offset** (`/var/lib/ups-monitor/tg-offset.json`) — no command replay after restarts; commands: `/status`, `/diag`, `/on`, `/off`, `/mainsdelay [1-720|reset]`, `/wantimeout [5-120]`, unknown → friendly command list (never silence)
- [ ] Notification engine: single outbound worker + queue; `critical` (outage confirmed, shutdown, wake, wake_failed) → immediate + ntfy `urgent`; `info` (blips, reconciles) → 90s coalescing window → one summary message; TG fail → ntfy fallback (dual endpoint, `[TG FAILED]` tag); failed deliveries → missed-notifications ledger
- [ ] `/status` data: mains (GPIO), WAN, Proxmox online + uptime via **PVE API** (TCP-reachability alone never counts as "confirmed" for verification messages), live countdowns, daily counters, sensor-blind banner
- [ ] Proxmox shutdown verification: confirm offline via API uptime reset or agent ack — never "node accepted TCP" (v1's false-confirmation bug)
- [ ] Pi code carries creds **embedded at deploy time**: `deploy-pi.sh` injects `.env` values into the RAM copy before scp — the Pi itself has **no** `.env`, no `EnvironmentFile`; the pushed `ups-monitor.py` is self-contained
- [ ] systemd: `WatchdogSec=90` + sd_notify heartbeat, `Restart=always`, logrotate (weekly ×8)

**Phase 2 acceptance:** queue `/off` → `systemctl restart ups-monitor` mid-flight → command executes exactly once; flap the webhook endpoint with 3 duplicate seqs → counter +1 total; block DNS to Telegram during a drill → ntfy delivers with `[TG FAILED]`, ledger records it.

---

## Phase 3 — Optocoupler Hardware Bring-Up ⏸ (part on order)

> 5V USB wall adapter (mains-powered) → PC817 LED side via series resistor; collector → GPIO 27 (`INPUT_PULLUP`), emitter → GND. **Mains isolation via the adapter — never wire mains directly.** Full schematic in `hardware/optocoupler-wiring.md` when the part lands.

- [ ] Bench wire-up + multimeter verify: idle HIGH, adapter-on LOW
- [ ] Firmware `mainsSource: gpio` active; network probe for mains **does not exist**
- [ ] Calibration: switched-socket bench — 3s/5s/10s/60s controlled cuts ×20 → every cut ≥3s detected, zero triggers for <3s dithering; log GPIO bounce count per event (expect ≤2 transitions)
- [ ] Adapter quality check: cheap chargers brown-out on sags — if the adapter resets on short blips, note it; the 3s stability rule absorbs normal sag, and a false *restore* is harmless (countdown only restarts), a false *down* just starts a countdown that a healthy supply cancels
- [ ] Disagreement drill (future hardening): unplug adapter while node has power → v2 simply reports mains down; if we later want a second opinion, it's a one-GPIO addition — out of scope for v2

**Phase 3 acceptance:** 20/20 real unplug cycles detected; 7-day soak with zero false detections; `/diag` shows GPIO state transitions matching reality.

---

## Phase 4 — UX Polish + Observability

- [ ] `/diag` v2: GPIO state + last change age, WiFi state, reset reason, ledger seq/gaps, counters, delay settings, WOL attempt stats
- [ ] Message taxonomy (Appendix A) enforced from a single severity table — TG formatting and ntfy priority derived from it
- [ ] pytest in `tests/`: event classification, coalescer, reconciler seq logic, command parsing (no hardware needed, runs in builder LXC / CI)
- [ ] Daily counters survive Pi restarts (already file-backed) and date-rollover correctly

**Phase 4 acceptance:** every drill scenario in §5 maps 1:1 to a documented, bounded message set.

---

## Phase 5 — Final Validation (fail-drill evening, someone at the breaker)

- [ ] 2s mains cut → `blip` info only; no countdown; no shutdown
- [ ] 30s mains cut → countdown starts; cancels on restore with "restored" + downtime
- [ ] 6 min mains cut → countdown → shutdown confirmed (API) → 15s → WOL → Proxmox online confirmed
- [ ] 6 min mains cut with WiFi-router rebooted mid-way → shutdown webhook retried after reconnect, still fires ≤5 min + reconnect delay
- [ ] `/on` during active countdown → override message, no shutdown; countdown resumes next outage
- [ ] `/off` → stays off through any power events; `/on` → wake
- [ ] WAN pull 11 min → WAN shutdown → restore → WOL; WAN pull 5 min → only info messages
- [ ] ESP32 power-pull mid-outage → boots with NVS flags → completes shutdown/wake correctly
- [ ] Pi power-pull mid-outage → ESP32 autonomously completes the cycle (decoupling proof)
- [ ] Proxmox agent down during shutdown → retry ×6 → honest "webhook failed" alert; wake still armed

---

## 7. Deployment Automation

> **Secret flow (non-negotiable):** real creds live ONLY in `.env` on this dev machine (chmod 600, git-ignored, template: `.env.example`).
> Repo sources carry `__PLACEHOLDER__` tokens only — `scripts/sanitize.py` enforces this (run before every commit).
> Deploy scripts load `.env`, inject values via `scripts/inject.py` into copies inside `/dev/shm` (RAM), and push **creds embedded in the code itself** to both targets.
> The Pi has **no** `.env` — its `ups-monitor.py` arrives self-contained. Firmware gets creds at compile time.
> Cred-embedded code and firmware binaries are NEVER written to persistent disk on this machine; `/dev/shm` copies die with the script.

### 7.0 One-time setup

- [x] `cp .env.example .env && chmod 600 .env` — real values filled (incl. `PI_PASS`)
- [x] `sudo apt install sshpass` on this machine (deploy auth: `sshpass -p "$PI_PASS"`)
- [x] `python3 scripts/sanitize.py` — must report **clean** before any commit

### 7.1 `deploy/deploy-pi.sh` — env-inject, push, install, verify

```bash
#!/usr/bin/env bash
# Usage: ./deploy/deploy-pi.sh        (from repo root; requires .env with PI_PASS)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env missing — cp .env.example .env"; exit 1; }
set -a; . ./.env; set +a
PI="${PI_SSH:?set PI_SSH in .env}"
PASS="${PI_PASS:?set PI_PASS in .env}"
command -v sshpass >/dev/null || { echo "sshpass not installed"; exit 1; }
SSH() { sshpass -p "$PASS" ssh "$@"; }
SCP() { sshpass -p "$PASS" scp "$@"; }
STAMP=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d /dev/shm/ups-pi-XXXXX); trap 'rm -rf "$TMP"' EXIT

echo "==> 1. Inject env -> self-contained RAM copy (creds embedded, never on disk here)"
python3 scripts/inject.py pi/ups-monitor.py > "$TMP/ups-monitor.py"

echo "==> 2. Remote backup"
SSH "$PI" "sudo mkdir -p /var/backups/ups-monitor/$STAMP && \
  sudo cp /usr/local/bin/ups-monitor.py /etc/systemd/system/ups-monitor.service \
          /var/backups/ups-monitor/$STAMP/ 2>/dev/null || true"

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
```

Rollback: `sshpass -p "$PI_PASS" ssh $PI "sudo cp /var/backups/ups-monitor/<STAMP>/* <original paths>; sudo systemctl restart ups-monitor"`.

### 7.2 `deploy/ota-esp32.sh` — env-inject, build in RAM, OTA, verify

```bash
#!/usr/bin/env bash
# Usage: ./deploy/ota-esp32.sh        (from repo root; requires .env)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env missing — cp .env.example .env"; exit 1; }
set -a; . ./.env; set +a
ESP="${ESP32_IP:?set ESP32_IP in .env}"
FW=$(grep -oP 'FW_VERSION\s*=\s*"\K[^"]+' firmware/src/main.cpp)
TMP=$(mktemp -d /dev/shm/ups-fw-XXXXX); trap 'rm -rf "$TMP"' EXIT

echo "==> 1. Copy project to RAM + inject env"
cp -r firmware "$TMP/firmware"
python3 scripts/inject.py "$TMP/firmware/src/main.cpp" --inplace

echo "==> 2. Build V$FW"
pio run -d "$TMP/firmware"

echo "==> 3. Pre-flight: reachable + idle (never OTA mid-incident)"
curl -sf -m 5 "http://$ESP/state" | python3 -c '
import sys,json; d=json.load(sys.stdin)
assert not (d["sdMains"] or d["sdWAN"] or d["sdManual"]), "shutdown flag set!"
assert d["mainsFailSinceMs"] == 0, "countdown running!"
print("pre-flight OK")'

echo "==> 4. OTA push"
pio run -d "$TMP/firmware" -t upload --upload-port "$ESP"
# fallback: espota.py -i "$ESP" -p 3232 --auth "$OTA_PASSWORD" -f "$TMP/firmware/.pio/build/esp32/firmware.bin"

echo "==> 5. Verify"
sleep 12
GOT=$(curl -sf -m 8 "http://$ESP/state" | python3 -c 'import sys,json;print(json.load(sys.stdin)["fw"])')
[ "$GOT" = "$FW" ] && echo "OTA verified ✔" || { echo "MISMATCH (got $GOT)"; exit 1; }
# NB: firmware.bin embeds WiFi/OTA creds — never archive it locally.
# Rollback = git checkout <old tag> + re-run this script.
```

### 7.3 Rollout discipline
- [ ] `python3 scripts/sanitize.py` clean before **every** commit — placeholders only in git
- [ ] Git tag before every deploy; Pi → human `/status` check after; ESP32 → only in §7.2 pre-flight-clean state
- [ ] First firmware deploy uses a **drill GPIO** (`set_gpio_test`) so the full state machine is exercised on the bench before the optocoupler is even wired
- [ ] No cred-embedded artifacts (injected sources, `firmware.bin`) stored locally — `/dev/shm` copies die with the script
- [ ] Rotate any token that ever appeared in a commit, log, or chat export

---

## Appendix A — Event Taxonomy

| Event | Class | Trigger |
|---|---|---|
| `mains_blip` | info | GPIO low <3s, recovered (no countdown) |
| `mains_down` | critical | GPIO low ≥3s → countdown started (mins from `mainsDelayMs`) |
| `mains_restored` | critical | countdown cancelled / post-shutdown restore armed |
| `shutdown_mains_start` / `shutdown_wan_start` | critical | timer expired → webhook to node |
| `shutdown_complete` | critical | agent ack |
| `wake_sequence_start` | critical | settle → WOL begins |
| `wol_rexmitted` | warning | Proxmox not up, re-sending (attempt n/5) |
| `wake_failed` | critical | 5 WOL attempts exhausted |
| `esp_booted` (+reset reason) | info | boot |
| `event_log_gap` | warning | reconciler seq jump |
| `sensor_blind` / `sensor_back` | warning/info | WiFi/ESP32 unreachable ≥45s |

## Appendix B — Timing Constants (single authority: firmware `/state`, TG-adjustable where noted)

| Constant | Default | Adjustable |
|---|---|---|
| GPIO debounce | 500ms | no |
| Mains confirm (stable low) | 3s | no |
| Mains shutdown delay | 5 min | `/mainsdelay` 1–720 min |
| WAN shutdown delay | 10 min | `/wantimeout` 5–120 min |
| Restore settle before WOL | 15s | no |
| Wake liveness check | ICMP ping + TCP :8006, every 15s | no |
| Re-WOL after | 120s (max 5) | no |
| Shutdown webhook retry | 10s (max 6) | no |
