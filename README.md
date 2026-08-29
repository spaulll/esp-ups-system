# UPS Monitor v2

Optocoupler-based UPS power monitor for a Proxmox homelab. Rebuild of the
v1 system (branch [`legacy`](../../tree/legacy)) — v1 inferred mains state from
network reachability (TCP probe to 192.168.0.2) which produced constant fake
powercut alerts. v2 measures electricity directly on a GPIO; the network is
used only for actuation (shutdown webhook, WOL) and reporting (Telegram/ntfy).

**Read [`UPGRADE_PLAN.md`](UPGRADE_PLAN.md) first** — it is the single source
of truth for scope, phased TODOs, acceptance gates, and deployment automation.

## Branch layout

| Branch | Purpose |
|---|---|
| `main` | v2 rebuild — all new work happens here |
| `legacy` | sanitized v1 snapshot: firmware `main.cpp`, Pi `ups-monitor.py`, `README.md` (v1 system doc) |

## Planned layout (built out during Phase 0–1)

```
scripts/    sanitize.py (secret scrubber), inject.py (.env -> code filler)
firmware/   ESP32 PlatformIO project (GPIO mains sense, WOL, shutdown agent)
pi/         Pi brain: Telegram bot, event reconciler, ntfy fallback
hardware/   Optocoupler wiring notes (PC817 + 5V adapter, isolated)
deploy/     deploy-pi.sh, ota-esp32.sh (inject creds from .env at push time)
tests/      Pi-side pytest
```

Secrets live only in a git-ignored `.env` (see `.env.example`). Sources carry
`__PLACEHOLDER__` tokens; deploy scripts fill them in RAM and push.

## Quick reference

- Pi (control plane): `pi@192.168.0.169` (wired LAN)
- ESP32 (sensor/actuator): `192.168.0.178` (OTA-enabled)
- Proxmox node: `192.168.0.50` (shutdown agent `:9999`, API `:8006`)
