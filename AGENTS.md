# AGENTS.md

## What this repo is

Homelab UPS monitor (Proxmox + ESP32 + Raspberry Pi), being rebuilt as **v2**.
`UPGRADE_PLAN.md` is the source of truth for scope, phased TODOs, acceptance
gates, and locked architecture decisions (Appendix A/B). Read it before any work.

- Branches: `main` (default — all v2 work happens here) and `legacy` (frozen
  sanitized v1 snapshot: `main.cpp`, `ups-monitor.py`, `README.md` = v1 system doc).
- v1 code is inspiration only. v2 code lands in `firmware/`, `pi/`, `deploy/`,
  `hardware/`, `tests/` per the plan — those dirs mostly don't exist yet.
- History restart was deliberate: the old repo was deleted because v1 committed
  real secrets. Run `scripts/sanitize.py` before EVERY commit — placeholders
  only, never real credentials.

## Secrets — non-negotiable

- Sources contain `__PLACEHOLDER__` tokens only. Real values live **only** in
  `.env` (chmod 600, git-ignored; template `.env.example`; doesn't exist until created).
- **Always run `python3 scripts/sanitize.py` before every commit AND before any
  push** — including routine ones the user asks for casually. It is a real
  gate: exit 0 = safe, exit 1 = secrets found, commit/push must not proceed.
  `--clean` scrubs in place and exits 0 only after re-verifying the scrub.
  Chain it: `python3 scripts/sanitize.py && git commit ...`
- `scripts/inject.py <file> [--inplace]` fills tokens from env at deploy time —
  deploy scripts write injected copies to `/dev/shm` and delete them on exit.
  Cred-embedded code or `firmware.bin` is **never persisted on this machine**.
- Targets receive creds **embedded in the code itself**: the Pi's pushed
  `ups-monitor.py` is self-contained (no `.env`, no `EnvironmentFile` on the Pi);
  firmware gets creds at compile time.
- Adding a new secret type: add a `RULES` entry in `scripts/sanitize.py` + a
  matching key in `.env.example`. Gotchas already paid for: regexes must not
  match their own source (build patterns via concatenation), quoted-string
  rules need `(?!__)` to stay idempotent, value patterns must not swallow
  trailing quotes, `.env.example` is skip-listed by name.

## Architecture invariants (do not regress)

- **Mains is sensed by an optocoupler on GPIO 13 / D13 (active-LOW, 3s stable).**
  Never detect mains via network reachability — v1's TCP probe of the extender
  was the root cause of constant fake powercut alerts.
- ESP32 is the autonomy authority (countdowns, shutdown, WOL); the Pi is control
  plane only (Telegram/ntfy, event reconciliation, no state mutation).
- Event flow: ESP32 event ledger (seq + `GET /events?since=N`) → Pi reconciler;
  webhooks are fast-path nudges carrying `&seq=`, auth'd by shared token.
- Locked timings: 5-min mains delay (`/mainsdelay` 1–720), 10-min WAN
  (`/wantimeout` 5–120), restore = 15s settle → WOL → ping + TCP :8006 every 15s
  → re-WOL after 120s (max 5). Manual `/off`/`/on` semantics match v1.

## Live environment (v1 still in production)

- Pi `pi@192.168.0.169` — deploy auth is `sshpass` with `PI_PASS` from `.env`.
- ESP32 `192.168.0.178` (OTA), Proxmox `192.168.0.50` (agent `:9999`, API `:8006`).
- Never OTA the ESP32 mid-incident: pre-flight in `deploy/ota-esp32.sh` must pass
  (reachable, no shutdown flags, no countdown).

## Commands

- `python3 scripts/sanitize.py [--clean]` — secret scan/scrub (whole repo).
- `python3 scripts/inject.py <file> [--inplace]` — requires env sourced (`set -a; . ./.env; set +a`).
- `python3 -m py_compile ups-monitor.py` — quick syntax check (no test suite yet;
  pytest under `tests/` is planned, not built).
- Deploy: `deploy/deploy-pi.sh` and `deploy/ota-esp32.sh` — planned in Phase 0/1,
  do not assume they exist.

## Conventions

- Personal repo: **short commit messages**, no essays.
- ESP32 target is PlatformIO (`pio run -d firmware`); Pi is plain python3.
