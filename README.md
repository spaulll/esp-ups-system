# UPS Monitor v2

Optocoupler-based UPS power monitor for a Proxmox homelab. Rebuild of the
v1 system (branch [`legacy`](../../tree/legacy)) — v1 inferred mains state from
network reachability (TCP probe to 192.168.0.2) which produced constant fake
powercut alerts. v2 measures electricity directly on a GPIO; the network is
used only for actuation (shutdown webhook, WOL) and reporting (Telegram/ntfy).

**Read [`UPGRADE_PLAN.md`](UPGRADE_PLAN.md) first** — it is the single source
of truth for scope, phased TODOs, acceptance gates, and deployment automation.

## Architecture

```
Wall mains ──→ 5V USB adapter ──→ PC817 optocoupler ──→ ESP32 GPIO 13 (D13)
                                                           │
                                      ┌────────────────────┤
                                      ▼                    ▼
                                  Pi brain            Telegram / ntfy
                                 (reconciler,          (notification)
                                  webhook, PVE,
                                  countdown cards)
```

- **ESP32** (autonomy authority): GPIO 13 mains detection, event ledger, WOL/shutdown, WiFi BSSID-locked
- **Pi** (control plane, thin): `/state` + `/events` reconciler, Telegram bot, ntfy fallback, PVE verification — never mutates state
- **Notifications**: Telegram primary, ntfy fallback, info coalescing (90s window), live countdown cards

## Branch layout

| Branch | Purpose |
|---|---|
| `main` | v2 rebuild — all work happens here |
| `legacy` | sanitized v1 snapshot: firmware `main.cpp`, Pi `ups-monitor.py`, `README.md` (v1 system doc) |

## Current state

| Phase | Title | Status |
|---|---|---|
| 0 | Repo scaffold + deploy tooling | ✅ Done |
| 1 | Firmware v2 core (GPIO mains, event ledger, WOL/shutdown) | 🔶 In progress |
| 2 | Pi brain (reconciler, Telegram, ntfy, PVE) | ✅ Done — 3/3 drills passed |
| 3 | Optocoupler hardware bring-up | 🔶 In progress — wired, verified, soak running |
| 4 | UX polish + observability | 🔶 In progress — pytest (35), live countdown, live-edit /status |
| 5 | Final validation | ☐ Not started |

## Repository layout

```
scripts/       sanitize.py (secret scrubber), inject.py (env -> code filler)
firmware/      ESP32 PlatformIO project (GPIO mains sense, WOL, shutdown agent)
pi/            Pi brain: Telegram bot, event reconciler, ntfy fallback, PVE
hardware/      Optocoupler wiring notes (PC817 + 5V adapter, isolated)
deploy/        deploy-pi.sh, ota-esp32.sh (inject creds from .env at push time)
tests/         Pi-side pytest (35 tests, runs in builder LXC)
```

Secrets live only in a git-ignored `.env` (see `.env.example`). Sources carry
`__PLACEHOLDER__` tokens; deploy scripts fill them in RAM and push.
Cred-embedded code is never persisted on this machine (built in `/dev/shm`).

## Quick reference

- Pi (control plane): `pi@192.168.0.169` (wired LAN)
- ESP32 (sensor/actuator): `192.168.0.178` (OTA-enabled)
- Proxmox node: `192.168.0.50` (shutdown agent `:9999`, API `:8006`)

## Telegram commands

Available after typing `/` in the bot chat:

| Command | Description |
|---|---|
| `/status` | Live power & server status (live-edits every 5s during a power cut) |
| `/diag` | Technical diagnostics |
| `/on` | Wake the server |
| `/off` | Shut the server down |
| `/mainsdelay` | Set power-loss shutdown delay (1–720 min) |
| `/wantimeout` | Set internet-loss shutdown delay (5–120 min) |

## Key invariants

- Mains sensed by **optocoupler on GPIO 13 (D13), active-LOW** — never via network
- ESP32 is the autonomy authority (countdowns, shutdown, WOL); the Pi is control plane only
- Event flow: ESP32 event ledger (seq + `GET /events?since=N`) → Pi reconciler; webhooks are fast-path nudges, ledger is authority
- All secrets are `__PLACEHOLDER__` tokens in source, injected at deploy time from `.env`
- Run `python3 scripts/sanitize.py` before every commit