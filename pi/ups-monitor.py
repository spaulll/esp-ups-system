#!/usr/bin/env python3
"""
UPS Monitor v2 — Pi brain (control plane only, never mutates state).

The ESP32 is the single source of truth. This process:
  - reconciles /state + /events?since= from the ESP32 every 15s
  - receives fast-path webhook nudges on :9997 (auth'd, idempotent)
  - notifies via Telegram with ntfy fallback
  - answers commands (/status /diag /on /off /mainsdelay /wantimeout)

Self-contained: deploy-pi.sh injects creds into the pushed copy at deploy
time. The Pi has no .env and no EnvironmentFile — creds are embedded here.
"""
import html
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.request
import urllib.parse
import http.client
import http.server

# ===================== CONFIG (injected at deploy time) =====================
ESP32_IP           = "__ESP32_IP__"
ESP32_PORT         = 80
POLL_INTERVAL      = 15
ESP32_TIMEOUT      = 10
SENSOR_DEAD_SEC    = 45          # unreachable this long -> sensor_blind alert

PROXMOX_IP         = "__PROXMOX_IP__"
PROXMOX_PORT       = 8006
PROXMOX_NODE       = "__PROXMOX_NODE__"
PVE_TOKEN          = "__PVE_TOKEN__"
PVE_TIMEOUT        = 6

TG_BOT_TOKEN       = "__TG_BOT_TOKEN__"
TG_CHAT_ID         = "__TG_CHAT_ID__"
TG_POLL_TIMEOUT    = 30   # long-poll wait: idle getUpdates re-issues (and re-resolves
                           # api.telegram.org) this often; Telegram still returns
                           # instantly when a command arrives, so no latency cost
TG_PROXY          = "__TG_PROXY__"

NTFY_URLS          = ["__NTFY_URL__"]
NTFY_TOPIC         = "__NTFY_TOPIC__"
NTFY_TIMEOUT       = 6

NOTIFY_TOKEN       = "__NOTIFY_TOKEN__"
WEBHOOK_PORT       = 9997

MAINS_DELAY_DEFAULT_MIN = int("__MAINS_DELAY_MIN__")
WAN_TIMEOUT_DEFAULT_MIN = int("__WAN_TIMEOUT_MIN__")
INFO_COALESCE_SEC       = 90      # info events batch into one summary

STATE_DIR    = "/var/lib/ups-monitor"
SEQ_FILE     = os.path.join(STATE_DIR, "last-seq.json")
TG_OFFSET    = os.path.join(STATE_DIR, "tg-offset.json")
MISSED_FILE  = os.path.join(STATE_DIR, "missed-ledger.json")
COUNTERS_FILE = os.path.join(STATE_DIR, "daily-counters.json")
OUTAGE_FILE  = os.path.join(STATE_DIR, "outage.json")
HISTORY_FILE = os.path.join(STATE_DIR, "outage-history.json")
HISTORY_MAX  = 10
LOG_FILE     = "/var/log/ups-monitor.log"
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

if TG_PROXY and TG_PROXY.startswith("socks"):
    try:
        import socks  # noqa: F401
    except ImportError:
        log.error("TG_PROXY set but PySocks missing — disabling proxy")
        TG_PROXY = None

# ===================== SHARED STATE =====================
_lock        = threading.Lock()
_esp32_state = {}
_last_seq    = 0
_sensor_dead_since = None
# True once a sensor_blind has actually been announced for the current
# outage. A single missed /state poll arms _sensor_dead_since but stays
# silent — sensor_back fires only if the user was told we were blind,
# otherwise every transient WiFi/ESP hiccup produces a confusing "Back"
# with no preceding "Blind".
_sensor_blind_announced = False
_daily_counters = {"date": None, "mains_down": 0, "shutdowns": 0, "blips": 0}

# outage tracking for the online confirmation: mains_down stamps the start,
# mains_restored stashes its downtimeMs as fallback (firmware skips the
# restored event while the node is down, so the wake path usually needs the
# computed start->online span). Consumed once by the online verify.
_last_mains_down_at = None
_last_mains_downtime_sec = None

# notification engine
_notify_queue = []          # list of (event_key, class, payload_dict)
_notify_lock  = threading.Lock()
_info_pending = None        # {events: [..], since: ts, notify_at: ts}

# live countdown updater
_cd_last_sent = 0.0         # last time we pushed a live countdown update
_cd_msg_id = None           # message_id of the single live countdown card
_esp_state_ts = 0.0         # wall-clock when _esp32_state was last refreshed

# live /status reply (edited in place while a countdown runs)
_status_msg_id = None
_status_last_sent = 0.0

# pending live-command confirmations: cmd -> {"message_id","mins","sent"}
_pending_conf = {}
_pending_conf_lock = threading.Lock()

# ===================== FILE PERSISTENCE =====================
def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning(f"failed to persist {path}: {e}")

def _load_seq():
    global _last_seq
    _last_seq = int(_load_json(SEQ_FILE, {"seq": 0}).get("seq", 0))

def _save_seq():
    _save_json(SEQ_FILE, {"seq": _last_seq})

def _load_tg_offset():
    return int(_load_json(TG_OFFSET, {"offset": 0}).get("offset", 0))

def _save_tg_offset(offset):
    _save_json(TG_OFFSET, {"offset": offset})

def _load_missed():
    return _load_json(MISSED_FILE, {"missed": []})

def _append_missed(entry):
    data = _load_missed()
    data["missed"].append(entry)
    data["missed"] = data["missed"][-50:]
    _save_json(MISSED_FILE, data)

def _today():
    return time.strftime("%Y-%m-%d")

def _load_counters():
    global _daily_counters
    data = _load_json(COUNTERS_FILE, None)
    if data and data.get("date") == _today():
        _daily_counters = data
    else:
        _daily_counters = {"date": _today(), "mains_down": 0, "shutdowns": 0, "blips": 0}

def _bump_counter(key):
    global _daily_counters
    with _lock:
        if _daily_counters.get("date") != _today():
            _daily_counters = {"date": _today(), "mains_down": 0, "shutdowns": 0, "blips": 0}
        _daily_counters[key] = _daily_counters.get(key, 0) + 1
        _save_json(COUNTERS_FILE, _daily_counters)

# ===================== SYSTEMD WATCHDOG (sd_notify) =====================
def _sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except Exception as e:
        log.debug(f"sd_notify failed: {e}")

def watchdog_thread():
    while True:
        stale = _watchdog_stale()
        if stale:
            # Withhold the systemd heartbeat so WatchdogSec=90 restarts us.
            # A wedged loop previously went unnoticed forever (the notify
            # below fired even with every worker dead).
            log.error(f"watchdog: stale threads {stale} — withholding sd_notify")
        else:
            _sd_notify("WATCHDOG=1")
        time.sleep(30)

# ===================== THREAD LIVENESS =====================
# Loops beat each iteration; the watchdog withholds its systemd heartbeat
# while any loop is stale, so a wedged telegram/reconciler/notify/countdown
# thread gets restarted instead of silently wedged. The webhook server is
# request-driven (idle = healthy), so it is deliberately not tracked here.
_hb_lock = threading.Lock()
_heartbeats = {}
WATCHDOG_STALE_SEC = 120


def _beat(name):
    with _hb_lock:
        _heartbeats[name] = time.time()


def _watchdog_stale(now=None, stale_after=WATCHDOG_STALE_SEC):
    """Names of loop threads with no recent heartbeat (missing = stale)."""
    now = now if now is not None else time.time()
    with _hb_lock:
        beats = dict(_heartbeats)
    return [n for n in ("reconciler", "telegram", "notify", "countdown")
            if beats.get(n) is None or now - beats[n] > stale_after]

# ===================== HTTP HELPERS =====================
def _urlopen(url, timeout=5, data=None, method="GET"):
    req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()

def _https_get_insecure(host, port, path, timeout=5, headers=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    return r.status, r.read().decode()

# ===================== PVE / PROXMOX =====================
def _pve_probe():
    """Returns (online, uptime_sec, uptime_str). API-only, never TCP-only."""
    try:
        path = f"/api2/json/nodes/{PROXMOX_NODE}/status"
        status, body = _https_get_insecure(
            PROXMOX_IP, PROXMOX_PORT, path, timeout=PVE_TIMEOUT,
            headers={"Authorization": PVE_TOKEN})
        if status != 200:
            return False, None, "Unavailable"
        data = json.loads(body)
        secs = int(data.get("data", {}).get("uptime", -1))
        if secs < 0:
            return False, None, "Unavailable"
        return True, secs, fmt_uptime(secs)
    except Exception as e:
        log.debug(f"pve probe failed: {e}")
        return False, None, "Offline"

def fmt_uptime(secs):
    if secs is None or secs < 0:
        return "Unavailable"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)

def fmt_downtime(secs):
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"

def _human_reason(label):
    """Map internal trigger names to plain-language Reason lines.

    Verification messages used to expose raw event keys
    ("Trigger: shutdown_mains_start") — cryptic on a phone. This keeps
    the cause but in human words, with the live delay so "more than
    X min" always matches the configured threshold.
    """
    try:
        with _lock:
            s = dict(_esp32_state)
    except Exception:
        s = {}
    if label == "shutdown_mains_start":
        try:
            mins = int((s.get("mainsDelayMs") or MAINS_DELAY_DEFAULT_MIN * 60000) // 60000)
        except Exception:
            mins = MAINS_DELAY_DEFAULT_MIN
        return f"Reason: Mains power was down for more than {mins} min"
    if label == "shutdown_wan_start":
        try:
            mins = int((s.get("wanTimeoutMs") or WAN_TIMEOUT_DEFAULT_MIN * 60000) // 60000)
        except Exception:
            mins = WAN_TIMEOUT_DEFAULT_MIN
        return f"Reason: Internet was down for more than {mins} min"
    if label == "shutdown_manual_start":
        return "Reason: Manual shutdown requested (/off)"
    if label == "online_confirmed":
        return "Reason: Server was woken and is responding again"
    # fallback: prettify any future/unknown trigger instead of leaking snake_case
    pretty = str(label).replace("_", " ").strip() or "unknown"
    return f"Reason: {pretty}"

def _remember_outage(evt, data):
    """Stamp outage timing for the later online confirmation.

    Persisted to OUTAGE_FILE: a Pi restart mid-outage must not lose the
    start stamp the online message's total-downtime math needs.
    """
    global _last_mains_down_at, _last_mains_downtime_sec
    changed = False
    if evt == "mains_down":
        with _lock:
            _last_mains_down_at = time.time()
        changed = True
    elif evt == "mains_restored":
        try:
            raw = (data.get("data") if isinstance(data, dict) else data) or ""
            ms = int(str(raw).split("=")[-1])
            with _lock:
                _last_mains_downtime_sec = ms // 1000
            changed = True
        except Exception:
            pass
    if changed:
        _save_outage()


def _load_outage():
    """Restore outage timing after a Pi restart (missing/corrupt -> blank)."""
    global _last_mains_down_at, _last_mains_downtime_sec
    data = _load_json(OUTAGE_FILE, {})
    _last_mains_down_at = data.get("down_at")
    _last_mains_downtime_sec = data.get("downtime_sec")
    try:
        if _last_mains_down_at is not None and float(_last_mains_down_at) > time.time():
            _last_mains_down_at = None
        if _last_mains_downtime_sec is not None and int(_last_mains_downtime_sec) < 0:
            _last_mains_downtime_sec = None
    except Exception:
        _last_mains_down_at = None
        _last_mains_downtime_sec = None


def _save_outage():
    with _lock:
        data = {"down_at": _last_mains_down_at,
                "downtime_sec": _last_mains_downtime_sec}
    _save_json(OUTAGE_FILE, data)

# ===================== OUTAGE HISTORY (last-N, persisted) =====================
def _load_history():
    data = _load_json(HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def _save_history(entries):
    _save_json(HISTORY_FILE, entries[-HISTORY_MAX:])


def _history_open(cause, shutdown=False):
    """Open an outage entry, or fold into the already-open one.

    A mains outage followed by its shutdown is ONE outage — the shutdown
    only flips the flag, it never starts a second entry.
    """
    entries = _load_history()
    if entries and entries[-1].get("end") is None:
        if cause == "mains" or not entries[-1].get("cause"):
            entries[-1]["cause"] = cause
        entries[-1]["shutdown"] = bool(entries[-1].get("shutdown") or shutdown)
    else:
        entries.append({"start": time.time(), "end": None, "cause": cause,
                        "shutdown": bool(shutdown), "downtime_sec": None})
    _save_history(entries)


def _history_close(downtime_sec=None):
    """Close the open outage entry, if any. Unknown span stays honest."""
    entries = _load_history()
    if entries and entries[-1].get("end") is None:
        now = time.time()
        entries[-1]["end"] = now
        if downtime_sec is not None:
            entries[-1]["downtime_sec"] = int(downtime_sec)
        else:
            try:
                entries[-1]["downtime_sec"] = int(now - float(entries[-1]["start"]))
            except Exception:
                entries[-1]["downtime_sec"] = None
        _save_history(entries)

def _consume_outage_line():
    """Total mains downtime for the online message, once. Empty if unknown.

    Prefers the live start->now span (covers shutdown->wake, where firmware
    never emits mains_restored); falls back to the stashed restored span.
    Only emitted when a real mains outage was seen — a WAN/manual wake
    with no mains_down stays silent instead of lying.
    """
    global _last_mains_down_at, _last_mains_downtime_sec
    with _lock:
        start = _last_mains_down_at
        fallback = _last_mains_downtime_sec
        _last_mains_down_at = None
        _last_mains_downtime_sec = None
    _save_outage()
    # The online confirmation means the outage is over — close the history
    # entry with the same total the message shows.
    if start is not None:
        secs = int(time.time() - start)
        if secs >= 5:
            _history_close(secs)
            return f"\n⏱ Power was out for {fmt_downtime(secs)} in total."
    if fallback:
        _history_close(fallback)
        return f"\n⏱ Power was out for {fmt_downtime(fallback)} in total."
    return ""

def pve_verify(expect_up, label, timeout_sec=180, interval=10):
    """Confirm offline/online via the PVE API (never TCP alone), background thread.

    Confirmations are critical (immediate + urgent): they close the loop on a
    shutdown/wake the user is watching — parking them in the 90s info
    coalescer produced "shutdown 04:50, confirmed 04:52" confusion.
    """
    def run():
        start = time.time()
        while time.time() - start < timeout_sec:
            online, _uptime_sec, up_str = _pve_probe()
            if expect_up and online:
                outage_line = _consume_outage_line()
                # Fresh boot reports 0m/1m uptime — noise, not information.
                # Show uptime only once the node has been up a while.
                uptime_line = ""
                try:
                    if _uptime_sec is not None and int(_uptime_sec) >= 120:
                        uptime_line = f"\n⌚ Uptime: {up_str}"
                except Exception:
                    uptime_line = ""
                notify_event("system_info", "critical",
                             f"✅ <b>Proxmox Confirmed Online</b>\n\n"
                             f"{_human_reason(label)}{outage_line}{uptime_line}")
                return
            if not expect_up and not online:
                notify_event("system_info", "critical",
                             f"✅ <b>Proxmox Confirmed Offline</b>\n\n{_human_reason(label)}")
                return
            time.sleep(interval)
        state_word = "online" if expect_up else "offline"
        notify_event("system_info", "critical",
                     f"⚠️ <b>Verification Timeout</b>\n\n{_human_reason(label)}\n"
                     f"Proxmox did not confirm {state_word} within {timeout_sec}s.")
    threading.Thread(target=run, daemon=True).start()

# ===================== ESP32 CLIENT =====================
def _esp_state():
    try:
        _, body = _urlopen(f"http://{ESP32_IP}:{ESP32_PORT}/state", timeout=ESP32_TIMEOUT)
        return json.loads(body)
    except Exception as e:
        log.warning(f"esp /state poll failed: {e}")
        return None

def _esp_events(since):
    try:
        _, body = _urlopen(
            f"http://{ESP32_IP}:{ESP32_PORT}/events?since={since}", timeout=ESP32_TIMEOUT)
        return json.loads(body)
    except Exception as e:
        log.warning(f"esp /events poll failed: {e}")
        return None

def _esp_command(cmd_dict):
    try:
        url = f"http://{ESP32_IP}:{ESP32_PORT}/command"
        data = json.dumps(cmd_dict).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=ESP32_TIMEOUT) as r:
            return r.status == 200
    except Exception as e:
        log.error(f"esp command failed: {e}")
        return False

# ===================== EVENT TAXONOMY (single authority) =====================
# class: critical -> immediate; warning -> immediate; info -> coalesce
# style: emoji-first severity, bold title, plain-language body (no internal
# field names), optional actionable hint. Same text flows to ntfy (stripped).
EVENT_TAXONOMY = {
    "esp_booted": (
        "info",
        lambda d: "🟢 <b>Sensor Online</b>\n\nUPS monitor started and is watching power now."
                  + (f"\nReason: {_plain_reset(d.get('data') or 'unknown')}" if d.get('data') else "")),
    "mains_blip": (
        "info",
        lambda d: "⚡ <b>Power Blip</b>\n\nA brief power dip was detected"
                  + (f" ({_parse_blip(d.get('data'))})" if d.get('data') else "")
                  + ". No action needed — power recovered on its own."),
    "mains_down": (
        "critical",
        lambda d: "🔴 <b>Utility Power Lost</b>\n\nThe server will shut down in "
                  + (_parse_mins(d.get('data')) if d.get('data') else "a few minutes")
                  + " if power doesn't return.\n\nTap /status to watch the countdown."),
    "mains_restored": (
        "critical",
        lambda d: "🟢 <b>Power Restored</b>\n\nUtility power is back."
                  + (f" It was out for {_fmt_downtime_span(d)}." if _fmt_downtime_span(d) else "")
                  + "\nMonitoring back to normal."),
    "wan_restored": (
        "critical",
        lambda d: "🟢 <b>Internet Restored</b>\n\nThe internet connection is back."
                  + (f" It was out for {_fmt_downtime_span(d)}." if _fmt_downtime_span(d) else "")
                  + "\nMonitoring back to normal."),
    "shutdown_mains_start": (
        "critical",
        lambda d: "🔴 <b>Shutting Down — Power Timeout</b>\n\nPower has been out past the limit."
                  + " Sending shutdown to the server now."),
    "shutdown_wan_start": (
        "critical",
        lambda d: "🔴 <b>Shutting Down — No Internet</b>\n\nInternet has been down past the limit."
                  + " Sending shutdown to the server now."),
    "shutdown_manual_start": (
        "critical",
        lambda d: "🔴 <b>Shutting Down — Manual</b>\n\nYou asked for shutdown. Server going down now.\n\nUse /on to wake it later."),
    "shutdown_webhook_ok": (
        # Dropped in process_event (never notified) — agent ACK noise.
        # Kept here so the taxonomy stays exhaustive for old firmware.
        "info",
        lambda d: "✅ Shutdown request accepted by the server."),
    "shutdown_webhook_failed": (
        "warning",
        lambda d: "⚠️ <b>Shutdown Not Acknowledged</b>\n\nServer didn't answer"
                  + (f" (attempt {d.get('data') or '?'})" if d.get('data') else "")
                  + " — retrying."),
    "shutdown_complete": (
        # Legacy (pre-V7.2 firmware emitted this on webhook ACK, i.e.
        # "shutdown initiated" — not "server off"). Kept so old firmware
        # never lies loudly; V7.2+ doesn't emit it at all — the Pi's PVE-API
        # "Confirmed Offline" verification is the sole offline authority.
        "info",
        lambda d: "🔄 <b>Shutdown In Progress</b>\n\nThe server acknowledged the"
                  " shutdown request and is powering off. This can take a few"
                  " minutes — confirmation follows when it's actually offline."),
    "webhook_gave_up": (
        "critical",
        lambda d: "🚨 <b>Shutdown Couldn't Be Confirmed</b>\n\n"
                  + "The server never acknowledged after 6 tries. It may already be off,"
                  + " or the shutdown service is down. The wake-up logic stays armed."),
    "wake_sequence_start": (
        "critical",
        lambda d: "🟡 <b>Waking the Server</b>\n\nWake-up signal sending now."
                  " The server can take a few minutes to come up —"
                  " confirmation follows when it's actually online."),
    "wol_rexmitted": (
        "warning",
        lambda d: "📡 <b>Wake Signal Re-sent</b>\n\nServer hasn't come up yet"
                  + (f" ({d.get('data') or 'retrying'})." if d.get('data') else ".")
                  + " Still trying."),
    "wake_failed": (
        "critical",
        lambda d: "🚨 <b>Server Didn't Wake</b>\n\nAfter 5 wake attempts the server is still off."
                  + " It needs manual attention.\n\nTry /on once it has power."),
    "online_confirmed": (
        # Never notified directly (see process_event): the PVE-verified
        # "Proxmox Confirmed Online" is the sole online voice — honest
        # (API-checked), carries outage total, hides fresh-boot uptime.
        # Kept here so the taxonomy stays exhaustive.
        "critical",
        lambda d: "✅ <b>Server Is Back Online</b>\n\nThe server is up and responding."),
    "manual_on": (
        "info",
        lambda d: "✅ <b>Manual Wake</b>\n\nWake-up signal sent as you asked."),
    "manual_override": (
        "warning",
        lambda d: "⚠️ <b>Auto-Shutdown Disabled</b>\n\nYou pressed /on during the outage, so"
                  + " the automatic shutdown is suspended. Use /off if you want it down."),
    "gpio_test": (
        "info",
        lambda d: "🧪 <b>Bench Test</b>\n\nMains input test applied: "
                  + (_parse_gpio_test(d.get('data')) if d.get('data') else "value set") + "."),
    "mains_delay_set": (
        "info",
        lambda d: "⏱️ <b>Power Delay Updated</b>\n\nAuto-shutdown now waits "
                  + (_parse_mins(d.get('data')) if d.get('data') else "the new delay")
                  + " after power loss."),
    "wan_timeout_set": (
        "info",
        lambda d: "⏱️ <b>Internet Delay Updated</b>\n\nShutdown after internet loss now waits "
                  + (_parse_mins(d.get('data')) if d.get('data') else "the new delay") + "."),
}


def _plain_reset(reason):
    """Map firmware reset-reason strings to plain words."""
    return {
        "poweron": "power-on", "software": "software restart", "panic": "a crash",
        "int_wdt": "a watchdog timer", "task_wdt": "a watchdog timer", "wdt": "a watchdog timer",
        "deepsleep": "deep sleep", "brownout": "a power dip", "sdio": "SDIO",
    }.get(str(reason).strip(), str(reason))


def _parse_mins(data):
    """'mins=18' -> '18 minutes'."""
    try:
        n = int(str(data).split("=")[-1].strip())
        return f"{n} minute{'s' if n != 1 else ''}"
    except (TypeError, ValueError):
        return str(data)


def _parse_blip(data):
    """'2x' -> '2 times'."""
    try:
        n = int(str(data).rstrip("x"))
        return f"{n} time{'s' if n != 1 else ''}"
    except (TypeError, ValueError):
        return str(data)


def _parse_gpio_test(data):
    """'value=1' -> 'simulated power loss' (1) / 'normal' (0) / 'real input' (-1)."""
    try:
        v = int(str(data).split("=")[-1].strip())
        return {1: "simulated power loss", 0: "simulated power present",
                -1: "real input restored"}.get(v, f"value {v}")
    except (TypeError, ValueError):
        return str(data)


def _fmt_downtime_span(d):
    """'downtimeMs=123000' (event dict or raw string) -> '2m 3s'.

    Empty string when unparseable — a malformed ledger payload must
    degrade to "is back" without downtime, never crash the reconciler.
    """
    try:
        raw = (d.get("data") if isinstance(d, dict) else d) or ""
        ms = int(str(raw).split("=")[-1])
        return fmt_downtime(ms // 1000)
    except Exception:
        return ""

# events that also trigger PVE verification
# NB: wake_sequence_start is deliberately NOT here — at wake start the node
# is still offline, so verifying from there only duplicates the
# online_confirmed verification (two "Confirmed Online" for one wake).
VERIFY_OFFLINE = {"shutdown_mains_start", "shutdown_wan_start", "shutdown_manual_start"}
VERIFY_ONLINE  = {"online_confirmed"}

# ===================== NOTIFICATION ENGINE =====================
def notify_event(event, klass, text):
    """Queue a notification. critical/warning -> immediate; info -> coalesce."""
    with _notify_lock:
        _notify_queue.append({"event": event, "class": klass, "text": text})
    _notify_worker_wake()

_wake_event = threading.Event()

def _notify_worker_wake():
    _wake_event.set()

def _tg_opener():
    if TG_PROXY:
        from urllib.request import ProxyHandler, build_opener
        return build_opener(ProxyHandler({"http": TG_PROXY, "https": TG_PROXY}))
    return urllib.request.build_opener()

def _tg_send(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _tg_opener().open(req, timeout=TG_POLL_TIMEOUT + 5) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        log.warning(f"telegram send failed: {e}")
        return False


def _tg_send_msg(text):
    """Send a message and return its message_id, or None on failure."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _tg_opener().open(req, timeout=TG_POLL_TIMEOUT + 5) as r:
            res = json.loads(r.read().decode())
            if res.get("ok"):
                return res.get("result", {}).get("message_id")
    except Exception as e:
        log.warning(f"telegram send failed: {e}")
    return None


def _tg_edit_msg(message_id, text):
    """Edit an existing bot message in place. Returns True on success."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageText"
    data = json.dumps({
        "chat_id": TG_CHAT_ID, "message_id": message_id,
        "text": text, "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _tg_opener().open(req, timeout=TG_POLL_TIMEOUT + 5) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        log.warning(f"telegram edit failed: {e}")
        return False


def _tg_del_msg(message_id):
    """Delete a bot message. Returns True on success."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteMessage"
    data = json.dumps({"chat_id": TG_CHAT_ID, "message_id": message_id}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _tg_opener().open(req, timeout=TG_POLL_TIMEOUT + 5) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        log.warning(f"telegram delete failed: {e}")
        return False

def _strip_html(text):
    text = re.sub(r"</?(b|i|u|s|code|pre|a)[^>]*>", "", text)
    return html.unescape(text)

def _ntfy_send(text, urgent=False, tg_failed=False):
    body = _strip_html(text)
    if len(body) > 4000:
        body = body[:4000] + "\n… [truncated]"
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    title = lines[0] if lines else "UPS Notification"
    payload = ("\n".join(lines[1:]) or title).encode()
    priority, tag = ("urgent", "rotating_light") if urgent else ("default", "information_source")
    safe_title = re.sub(r"[^\x20-\x7e]", "", title).strip() or "UPS Notification"
    if tg_failed:
        safe_title = "[TG FAILED] " + safe_title
    headers = {"Title": safe_title[:200], "Priority": priority, "Tags": tag}
    for base in NTFY_URLS:
        try:
            req = urllib.request.Request(
                f"{base}/{NTFY_TOPIC}", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT) as r:
                if r.status == 200:
                    return True
        except Exception as e:
            log.warning(f"ntfy via {base} failed: {e}")
    return False

def _deliver(text, urgent):
    try:
        if _tg_send(text):
            return True
    except Exception as e:
        log.warning(f"telegram send raised: {e}")
    log.warning("Telegram delivery failed — falling back to ntfy")
    ok = _ntfy_send(text, urgent=urgent, tg_failed=True)
    if not ok:
        _append_missed({"at": time.time(), "text": text[:500]})
        log.error("ntfy also failed — recorded in missed ledger")
    return ok

def _notify_worker():
    global _info_pending
    while True:
        _beat("notify")
        _wake_event.wait(2)
        _wake_event.clear()
        while True:
            with _notify_lock:
                if _notify_queue:
                    item = _notify_queue.pop(0)
                else:
                    item = None
                    break
            text = item["text"]
            if item["class"] in ("critical", "warning"):
                _deliver(text, urgent=(item["class"] == "critical"))
            else:
                _handle_info(text)
        # info coalescer: flush batch once its 90s window expires
        now = time.time()
        with _notify_lock:
            if _info_pending and now >= _info_pending["notify_at"]:
                pending = _info_pending
                _info_pending = None
            else:
                pending = None
        if pending:
            msgs = pending["events"]
            summary = "🗂 <b>Info Summary</b>\n\n" + "\n".join(f"• {m}" for m in msgs)
            _deliver(summary, urgent=False)

def _handle_info(text):
    global _info_pending
    now = time.time()
    with _notify_lock:
        if _info_pending is None:
            _info_pending = {"events": [text], "since": now, "notify_at": now + INFO_COALESCE_SEC}
        else:
            _info_pending["events"].append(text)

# ===================== RECONCILER =====================
_reconcile_lock = threading.Lock()

# events that confirm a live "waiting" command message (edited in place)
CONFIRM_EVENTS = {"mains_delay_set": "mainsdelay", "wan_timeout_set": "wantimeout"}


def _maybe_confirm_pending(evt, data):
    """If a delay-set command is pending confirmation, edit its live message."""
    cmd = CONFIRM_EVENTS.get(evt)
    if not cmd:
        return False
    with _pending_conf_lock:
        conf = _pending_conf.pop(cmd, None)
    if not conf:
        return False
    ok = _tg_edit_msg(conf["message_id"],
                      f"✅ {conf['label'].capitalize()} delay confirmed: <b>{conf['human']}</b>")
    if ok:
        log.info(f"confirmed {cmd} -> {conf['human']} (edited live message)")
    return ok


def process_event(evt, seq, data):
    global _last_seq
    # Dropped noise: the agent ACK ("Shutdown request accepted") adds no
    # information — the "Shutting Down" command + PVE "Confirmed Offline"
    # pair already tells the story. Skip silently (seq still advances).
    if evt == "shutdown_webhook_ok":
        log.debug(f"dropping noisy {evt} (seq={seq})")
        return
    taxonomy = EVENT_TAXONOMY.get(evt)
    if not taxonomy:
        log.warning(f"unknown event {evt} (seq={seq})")
        return
    # Delay-set confirmations edit the "waiting" message instead of being
    # batched into the slow info summary — no 1.5-min gap for command users.
    if _maybe_confirm_pending(evt, data):
        return
    klass, fmt = taxonomy
    _remember_outage(evt, data)
    if evt == "mains_down":
        _history_open("mains")
    elif evt == "shutdown_mains_start":
        _history_open("mains", shutdown=True)
    elif evt == "shutdown_wan_start":
        _history_open("wan", shutdown=True)
    elif evt == "shutdown_manual_start":
        _history_open("manual", shutdown=True)
    elif evt in ("mains_restored", "wan_restored"):
        try:
            raw = (data.get("data") if isinstance(data, dict) else data) or ""
            _history_close(int(str(raw).split("=")[-1]) // 1000)
        except Exception:
            _history_close()
    if evt in VERIFY_ONLINE:
        # Single online voice: the ESP's own liveness message would double
        # the PVE-verified confirmation minutes later. Verify (which notifies)
        # is enough — "Waking..." + "Confirmed Online" tells the story.
        pve_verify(expect_up=True, label=evt)
        return
    if evt == "mains_down":
        _bump_counter("mains_down")
    elif evt == "mains_blip":
        _bump_counter("blips")
    elif evt in ("shutdown_mains_start", "shutdown_wan_start", "shutdown_manual_start"):
        _bump_counter("shutdowns")
    notify_event(evt, klass, fmt(data))
    if evt in VERIFY_OFFLINE:
        pve_verify(expect_up=False, label=evt)

def reconcile_once():
    global _last_seq, _sensor_dead_since, _esp_state_ts, _sensor_blind_announced
    with _reconcile_lock:
        state = _esp_state()
        if state is not None:
            with _lock:
                announced = _sensor_blind_announced
                _esp32_state.clear()
                _esp32_state.update(state)
                _esp_state_ts = time.time()
                _sensor_dead_since = None
                _sensor_blind_announced = False
            if announced:
                notify_event("sensor_back", "info",
                             "🟢 <b>Sensor Back</b>\n\nESP32 reachable again — monitoring resumed.")
            # First-ever run: seed from the ESP's current seq so we never
            # replay the ring buffer's stale history as fresh alerts.
            if not os.path.exists(SEQ_FILE) and not state.get("seq", 0) == 0:
                _last_seq = int(state.get("seq", 0))
                _save_seq()
        else:
            now = time.time()
            if _sensor_dead_since is None:
                _sensor_dead_since = now
            elif now - _sensor_dead_since >= SENSOR_DEAD_SEC:
                notify_event("sensor_blind", "warning",
                             "⚠️ <b>Sensor Blind</b>\n\nESP32 unreachable ≥45s. No false alerts — authority stays autonomous.")
                _sensor_dead_since = now
                _sensor_blind_announced = True
            return

        events = _esp_events(_last_seq)
        if events is None:
            return
        if events:
            seqs = [e["seq"] for e in events]
            expected = _last_seq + 1
            if seqs[0] > expected:
                notify_event("event_log_gap", "warning",
                             f"⚠️ <b>Event Log Gap</b>\n\nJumped from seq {_last_seq} to {seqs[0]} — {seqs[0]-expected} event(s) may be lost.")
            for e in events:
                process_event(e.get("event"), e.get("seq"), e)
                _last_seq = max(_last_seq, int(e.get("seq", _last_seq)))
            _save_seq()

def reconciler_loop():
    log.info("reconciler started")
    while True:
        _beat("reconciler")
        reconcile_once()
        time.sleep(POLL_INTERVAL)

# ===================== WEBHOOK RECEIVER (:9997) =====================
class NotifyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/notify":
            self.send_response(404); self.end_headers(); return
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get("token", [""])[0]
        if token != NOTIFY_TOKEN:
            self.send_response(403); self.end_headers(); self.wfile.write(b"forbidden")
            return
        evt = params.get("event", [""])[0]
        seq = params.get("seq", ["0"])[0]
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        # fast-path nudge only — ledger is authority. If the nudged seq is
        # ahead of ours, run a reconcile immediately; duplicate seqs are a no-op.
        try:
            seq_int = int(seq)
        except ValueError:
            seq_int = 0
        if seq_int > _last_seq:
            threading.Thread(target=reconcile_once, daemon=True).start()

    def log_message(self, fmt, *args):
        pass

def webhook_server():
    log.info(f"webhook receiver on :{WEBHOOK_PORT}")
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), NotifyHandler)
    srv.serve_forever()

# ===================== TELEGRAM COMMANDS =====================
TG_COMMANDS = [
    {"command": "status", "description": "Live power & server status"},
    {"command": "diag", "description": "Technical diagnostics"},
    {"command": "on", "description": "Wake the server"},
    {"command": "off", "description": "Shut the server down"},
    {"command": "mainsdelay", "description": "Set power-loss shutdown delay (1-720 min)"},
    {"command": "wantimeout", "description": "Set internet-loss shutdown delay (5-120 min)"},
    {"command": "missed", "description": "Show alerts that failed delivery"},
    {"command": "history", "description": "Recent power/internet outages"},
]


def register_commands():
    """Register the bot command menu with Telegram (so '/' shows the list)."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setMyCommands"
    data = json.dumps({"commands": TG_COMMANDS}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _tg_opener().open(req, timeout=TG_POLL_TIMEOUT + 5) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        log.warning(f"register commands failed: {e}")
        return False
def _send_esp(cmd, extra=None):
    d = {"cmd": cmd}
    if extra: d.update(extra)
    return _esp_command(d)

def _bar(frac, width=10):
    frac = max(0.0, min(1.0, float(frac)))
    filled = round(frac * width)
    return "▰" * filled + "▱" * (width - filled)


def mains_down_notified():
    with _lock:
        return bool(_esp32_state.get("mainsFailSinceMs", 0))


def _countdown_active():
    """True while a real mains countdown is running (not overridden/shut down)."""
    with _lock:
        s = dict(_esp32_state)
    if not s:
        return False
    if s.get("sdMains") or s.get("sdWAN") or s.get("sdManual") or s.get("manualOverride"):
        return False
    return (s.get("mainsFailSinceMs", 0) or 0) > 0

def cmd_status():
    with _lock:
        s = dict(_esp32_state)
        counters = dict(_daily_counters)
        ts = _esp_state_ts
    prox_online, _, prox_up = _pve_probe()
    header = f"⚡ <b>UPS Monitor</b> · <i>{time.strftime('%H:%M:%S')}</i>\n────"

    if not s:
        return header + "\n\n⚠️ <b>Sensor offline</b>\n\nNo live data available right now."
    mains = s.get("mainsUp", False)
    wan = s.get("wanUp", False)
    lines = [header]
    lines.append(f"{'🟢' if mains else '🔴'} <b>Mains</b>   <code>{'UP' if mains else 'DOWN'}</code>")
    lines.append(f"{'🟢' if wan else '🔴'} <b>WAN</b>     <code>{'UP' if wan else 'DOWN'}</code>")
    lines.append(f"{'🟢' if prox_online else '🔴'} <b>Proxmox</b> <code>{'ONLINE' if prox_online else 'OFFLINE'}</code>"
                 + (f" · {prox_up}" if prox_online else ""))
    if s.get("gpioTestOverride", -1) != -1:
        lines.append("")
        lines.append("🧪 <b>TEST MODE</b> — mains input simulated, real outages invisible. "
                     "Restore live sensing on the ESP before trusting this status.")

    # Extrapolate elapsed from cached mainsFailSinceMs using wall-clock, the
    # same way the live countdown card does — so /status never looks stale.
    mfail = s.get("mainsFailSinceMs", 0) or 0
    mdelay = s.get("mainsDelayMs", 300000) or 1
    if mains_down_notified() and mfail:
        mfail = min(mfail + (time.time() - ts) * 1000 if ts else mfail, mdelay)
        down_s = int(mfail // 1000)
        remain = max(0, (mdelay - mfail) // 1000)
        frac = max(0.0, min(1.0, mfail / mdelay))
        lines.append("")
        lines.append(f"⏳ <b>Power down for {fmt_downtime(down_s)}</b>")
        lines.append(f"    Auto-shutdown in <b>{fmt_downtime(remain)}</b>")
        lines.append(f"    <code>{_bar(frac)}</code>  {int(frac * 100)}%")
    if s.get("sdMains") or s.get("sdWAN") or s.get("sdManual"):
        reason = "manual /off"
        if s.get("sdMains") and s.get("sdWAN"): reason = "power + internet loss"
        elif s.get("sdMains"): reason = "power loss"
        elif s.get("sdWAN"): reason = "internet loss"
        lines.append("")
        if prox_online:
            # Shutdown commanded but the node hasn't powered off yet (normal:
            # Proxmox with many LXCs takes minutes). Never claim "down" here.
            lines.append(f"🟡 <b>Shutting down…</b> · {reason} — waiting for the"
                         f" server to power off (can take a few minutes)")
        else:
            lines.append(f"🛡 <b>Server down</b> · {reason}")
    elif not prox_online and s.get("wakePhase") in (1, 2):
        lines.append("")
        lines.append("🟡 <b>Waking…</b> — wake signal sent, waiting for the"
                     " server to come up (can take a few minutes)")
    lines.append("")
    lines.append(f"📊 Today: {counters.get('mains_down',0)}× power loss · "
                 f"{counters.get('shutdowns',0)}× shutdown · {counters.get('blips',0)}× blips")
    return "\n".join(lines)


def cmd_diag():
    with _lock:
        s = dict(_esp32_state)
        counters = dict(_daily_counters)
    prox_online, _, prox_up = _pve_probe()
    header = f"🔧 <b>Diagnostics</b> · <i>{time.strftime('%H:%M:%S')}</i>\n────"
    lines = [header]
    if not s:
        lines.append("\n⚠️ <b>Sensor offline</b>\n\nNo live data available right now.")
    else:
        fw = s.get('fw', '?')
        up = fmt_downtime(s.get('espUptimeMs', 0) // 1000)
        reset = _plain_reset(s.get('espResetReason', 'unknown'))
        rssi = f"{s.get('rssi')} dBm" if isinstance(s.get('rssi'), (int, float)) else "—"
        lines.append(f"🧩 <b>Sensor</b>  {fw} · {up} up · {reset}")
        lines.append(f"📶 <b>WiFi</b>    {rssi}")
        stable_ms = s.get("mainsStableSinceMs", -1)
        age = fmt_downtime(stable_ms // 1000) if isinstance(stable_ms, (int, float)) and stable_ms >= 0 else "unknown"
        lines.append(f"🟢 <b>Mains</b>   {'UP' if s.get('mainsUp') else 'DOWN'} · last change {age}")
        lines.append(f"🟢 <b>WAN</b>     {'UP' if s.get('wanUp') else 'DOWN'}")
        lines.append(f"🟢 <b>Node</b>    {'ONLINE' if prox_online else 'OFFLINE'}" + (f" · {prox_up}" if prox_online else ""))
        lines.append("────")
        lines.append(f"⏱ <b>Delays</b>  mains {s.get('mainsDelayMs', 300000) // 60000}m · wan {s.get('wanTimeoutMs', 600000) // 60000}m")
        flags = []
        if s.get("sdMains"): flags.append("sdMains")
        if s.get("sdWAN"): flags.append("sdWAN")
        if s.get("sdManual"): flags.append("sdManual")
        if s.get("manualOverride"): flags.append("manualOverride")
        lines.append(f"🚩 <b>Flags</b>   {', '.join(flags) if flags else 'none'}")
        override = s.get("gpioTestOverride", -1)
        if override != -1:
            lines.append(f"🧪 <b>Test</b>    OVERRIDE={override} — simulated input, real mains invisible")
        lines.append(f"📊 <b>Today</b>   {counters.get('mains_down', 0)}× power loss · {counters.get('shutdowns', 0)}× shutdown")
        lines.append("────")
        led = f"seq {s.get('seq', '?')}"
        if _last_seq >= int(s.get('seq', _last_seq)):
            led += " · synced"
        lines.append(f"🧠 <b>Ledger</b>  {led}")
        lines.append(f"📡 <b>Wake</b>    {s.get('wolAttempts', 0)} this cycle · {counters.get('wolRexmit', 0)} re-sends")
    return "\n".join(lines)

def cmd_set_delay(cmd, mins_str):
    if mins_str in ("reset", None):
        mins = MAINS_DELAY_DEFAULT_MIN if cmd == "mainsdelay" else WAN_TIMEOUT_DEFAULT_MIN
    else:
        try:
            mins = int(mins_str)
        except ValueError:
            return "❌ Minutes must be a number."
    lo, hi = (1, 720) if cmd == "mainsdelay" else (5, 120)
    if not (lo <= mins <= hi):
        return f"❌ {cmd} range is {lo}–{hi}."
    label = "power" if cmd == "mainsdelay" else "internet"
    human = f"{mins} min"
    # Register the pending confirmation BEFORE sending the command: the ESP
    # can ack within milliseconds via its webhook nudge, and if we register
    # late the event races ahead into the slow info summary. Register first.
    sent_id = _tg_send_msg(f"⏳ Setting {label} delay to <b>{human}</b>… waiting for confirmation")
    if sent_id is not None:
        with _pending_conf_lock:
            _pending_conf[cmd] = {"message_id": sent_id, "label": label,
                                  "human": human, "mins": mins, "sent": time.time()}
    ok = _esp_command({"cmd": cmd, "minutes": mins})
    if not ok:
        # command failed — edit the waiting message immediately
        if sent_id is not None:
            _tg_edit_msg(sent_id,
                         f"❌ Couldn't reach the sensor to set {label} delay. Use /diag.")
        else:
            return "❌ ESP32 unreachable."
        with _pending_conf_lock:
            _pending_conf.pop(cmd, None)
        return None
    return None   # live message gets edited to "confirmed" when the ESP acks

def _tg_get(path, params):
    """GET a Telegram API path with optional SOCKS proxy."""
    qs = urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{path}?{qs}"
    with _tg_opener().open(url, timeout=TG_POLL_TIMEOUT + 5) as r:
        return json.loads(r.read().decode())

def _tg_poll():
    offset = _load_tg_offset()
    while True:
        _beat("telegram")
        try:
            data = _tg_get("getUpdates", {"timeout": TG_POLL_TIMEOUT, "offset": offset + 1})
            if not data.get("ok"):
                time.sleep(2); continue
            for u in data.get("result", []):
                offset = u["update_id"]
                _save_tg_offset(offset)
                msg = u.get("message") or {}
                chat = msg.get("chat", {})
                if str(chat.get("id")) != TG_CHAT_ID:
                    continue
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                parts = text.split(maxsplit=1)
                cmd = parts[0].lower().split("@")[0]
                arg = parts[1] if len(parts) > 1 else None
                reply = handle_command(cmd, arg)
                if reply:
                    _deliver(reply, urgent=False)
        except Exception as e:
            log.warning(f"tg poll error: {e}")
            time.sleep(3)

def _esp_snapshot():
    with _lock:
        return dict(_esp32_state)


def cmd_on():
    """Handle /on — wake unless the node is provably already up.

    ESP flags alone can't prove "up": they only reflect shutdowns the ESP
    itself initiated. A manual shutdown outside the system leaves flags
    clear while Proxmox is actually offline (the exact "/status OFFLINE +
    /on already-up" contradiction). So the gate uses the PVE API — the
    same source /status displays. Countdown-override still fires first so
    /on during an outage suppresses the shutdown even while Proxmox is
    still (briefly) online.
    """
    s = _esp_snapshot()
    if s:
        counting = (s.get("mainsFailSinceMs", 0) or 0) > 0 or (s.get("wanFailSinceMs", 0) or 0) > 0
        if counting and not s.get("manualOverride"):
            ok = _send_esp("wake")
            if not ok:
                return "❌ ESP32 unreachable."
            return ("✅ Override sent — auto-shutdown suspended for this outage.\n\n"
                    "The server stays up. Use /off if you want it down.")
    prox_online, _, _ = _pve_probe()
    if prox_online:
        return "ℹ️ Server is already up — nothing to wake."
    ok = _send_esp("wake")
    if not ok:
        return "❌ ESP32 unreachable."
    return ("✅ Wake commanded.\n\nWake signal sent — the server can take a few"
            " minutes to come up. Confirmation follows when it's actually online.")


def cmd_off():
    """Handle /off — shutdown unless the node is provably already down.

    Uses the PVE API (same source as /status): an externally-stopped node
    reports "already down" instead of firing a pointless shutdown, while a
    node that is still up always gets the command even if ESP flags look
    stale (e.g. a shutdown webhook that never acked).
    """
    prox_online, _, _ = _pve_probe()
    if not prox_online:
        return "ℹ️ Server is already down."
    ok = _send_esp("shutdown")
    if not ok:
        return "❌ ESP32 unreachable."
    return ("✅ Shutdown commanded.\n\nServer is powering off — this can take a"
            " few minutes with many services. Confirmation follows when it's"
            " actually offline.")


def cmd_missed():
    """Show alerts that reached neither Telegram nor ntfy (newest first)."""
    entries = _load_missed().get("missed", [])
    if not entries:
        return "✅ No missed notifications — everything got through."
    lines = ["📭 <b>Missed Notifications</b> (newest first)\n────"]
    for e in reversed(entries[-10:]):
        try:
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(e.get("at", 0))))
        except Exception:
            ts = "--"
        text = _strip_html(str(e.get("text", ""))).replace("\n", " ")
        if len(text) > 160:
            text = text[:160] + "…"
        lines.append(f"• <code>{ts}</code> {html.escape(text)}")
    return "\n".join(lines)


def cmd_history():
    """Recent outages, newest first — no chat scrolling needed."""
    entries = _load_history()
    if not entries:
        return "📭 No outages recorded yet."
    lines = ["🗂 <b>Outage History</b> (newest first)\n────"]
    cause_word = {"mains": "power", "wan": "internet", "manual": "manual"}
    for e in reversed(entries[-HISTORY_MAX:]):
        try:
            start_s = time.strftime("%m-%d %H:%M", time.localtime(float(e["start"])))
        except Exception:
            start_s = "--"
        if e.get("end"):
            try:
                end_s = time.strftime("%H:%M", time.localtime(float(e["end"])))
            except Exception:
                end_s = "?"
            dur = e.get("downtime_sec")
            span = fmt_downtime(dur) if isinstance(dur, (int, float)) and dur >= 0 else "?"
            when = f"{start_s} → {end_s} ({span})"
        else:
            when = f"{start_s} → ongoing"
        flag = " · shutdown" if e.get("shutdown") else ""
        lines.append(f"• {when} — {cause_word.get(e.get('cause'), '?')}{flag}")
    return "\n".join(lines)


def handle_command(cmd, arg):
    global _status_msg_id, _status_last_sent
    if cmd == "/status":
        text = cmd_status()
        # live-edit: during a countdown, send once and keep the reply updated
        if _countdown_active():
            mid = _tg_send_msg(text)
            if mid is not None:
                with _lock:
                    _status_msg_id = mid
                    _status_last_sent = time.time()
                return None
        return text
    if cmd == "/diag":
        return cmd_diag()
    if cmd == "/on":
        return cmd_on()
    if cmd == "/off":
        return cmd_off()
    if cmd in ("/mainsdelay", "/wantimeout"):
        return cmd_set_delay(cmd.lstrip("/"), arg)
    if cmd == "/missed":
        return cmd_missed()
    if cmd == "/history":
        return cmd_history()
    return ("Available: /status /diag /on /off /missed /history "
            "/mainsdelay [1-720|reset] /wantimeout [5-120|reset]")

def telegram_loop():
    log.info("telegram loop started")
    if register_commands():
        log.info("bot commands registered with Telegram")
    else:
        log.warning("could not register bot commands — menu may be missing")
    _tg_poll()


def _build_countdown_card():
    """Live mains countdown card, or None if no countdown is running."""
    with _lock:
        s = dict(_esp32_state)
        ts = _esp_state_ts
    if not s:
        return None
    if s.get("sdMains") or s.get("sdWAN") or s.get("sdManual") or s.get("manualOverride"):
        return None
    mfail = s.get("mainsFailSinceMs", 0) or 0
    mdelay = s.get("mainsDelayMs", 300000) or 1
    if mfail <= 0:
        return None
    now_ms = (time.time() - ts) * 1000 if ts else 0
    elapsed = max(0, min(mfail + now_ms, mdelay))
    remain = max(0, mdelay - elapsed)
    down = int(elapsed // 1000)
    left = int(remain // 1000)
    frac = elapsed / mdelay if mdelay else 0
    bar = _bar(frac)
    return (f"🔴 <b>Power down for {fmt_downtime(down)}</b>\n"
            f"   Auto-shutdown in <b>{fmt_downtime(left)}</b>\n"
            f"   <code>{bar}</code>  {int(frac * 100)}%")


def _cd_interval(elapsed_ms, delay_ms):
    """Adaptive update cadence: 5s at start, slowing through the middle,
    back to 5s in the final 2 minutes (decision window)."""
    remain = delay_ms - elapsed_ms
    if remain <= 120000:          # last 2 min -> fast
        return 5
    if elapsed_ms <= 60000:       # first minute -> fast, shows it's live
        return 5
    ramp_start, ramp_end = 60000, delay_ms - 120000
    if ramp_end > ramp_start:
        f = (elapsed_ms - ramp_start) / (ramp_end - ramp_start)
    else:
        f = 0.0
    return int(round(5 + 40 * min(1.0, max(0.0, f))))   # 5s .. 45s


def _countdown_tick():
    """One update cycle for the live countdown card. Returns True if a card is live."""
    global _cd_last_sent, _cd_msg_id
    # If the user has a live /status reply open, let IT be the single updater
    # instead of also ticking a separate countdown card (no message spam).
    with _lock:
        status_live = _status_msg_id is not None
    if status_live:
        if _cd_msg_id is not None:
            _tg_del_msg(_cd_msg_id)
            _cd_msg_id = None
        _cd_last_sent = 0
        return True
    card = _build_countdown_card()
    now = time.time()
    if card:
        if _cd_msg_id is None:
            # countdown started -> send the live card once
            _cd_msg_id = _tg_send_msg(card)
            _cd_last_sent = now
        else:
            with _lock:
                mfail = _esp32_state.get("mainsFailSinceMs", 0) or 0
                mdelay = _esp32_state.get("mainsDelayMs", 300000) or 1
            interval = _cd_interval(mfail, mdelay)
            if now - _cd_last_sent >= interval:
                # Telegram rejects rapid or identical edits; never resend on a
                # failed edit (a new message would spam the chat). Just retry
                # the edit next cycle.
                _tg_edit_msg(_cd_msg_id, card)
                _cd_last_sent = now
        return True
    # countdown over (restored / shutdown / override) -> clean up the card
    if _cd_msg_id is not None:
        _tg_del_msg(_cd_msg_id)
        _cd_msg_id = None
    _cd_last_sent = 0
    return False


def _status_live_tick():
    """Edit the /status reply in place while a countdown runs."""
    global _status_msg_id, _status_last_sent
    if _status_msg_id is None:
        return
    now = time.time()
    if _countdown_active():
        if now - _status_last_sent >= 5:   # fixed 5s cadence for a live status
            _tg_edit_msg(_status_msg_id, cmd_status())
            _status_last_sent = now
    else:
        # countdown ended -> one final refresh so the reply is not stale, then stop
        _tg_edit_msg(_status_msg_id, cmd_status())
        _status_msg_id = None


def countdown_updater():
    """Live countdown card: ONE message edited in place at an adaptive cadence."""
    log.info("countdown updater started")
    while True:
        _beat("countdown")
        _expire_pending_confirmations()
        _countdown_tick()
        _status_live_tick()
        time.sleep(2)


def _expire_pending_confirmations():
    """Flip stale 'waiting…' command messages to failed after 90s."""
    now = time.time()
    stale = []
    with _pending_conf_lock:
        for cmd, conf in list(_pending_conf.items()):
            if now - conf.get("sent", 0) > 90:
                stale.append((cmd, conf))
                _pending_conf.pop(cmd, None)
    for cmd, conf in stale:
        _tg_edit_msg(conf["message_id"],
                     f"❌ {conf['label'].capitalize()} delay not confirmed — ESP unreachable? "
                     f"Use /diag to check.")


# ===================== MAIN =====================
def main():
    _load_seq()
    _load_counters()
    _load_outage()
    for _t in ("reconciler", "telegram", "notify", "countdown"):
        _beat(_t)
    _sd_notify("READY=1")
    threading.Thread(target=watchdog_thread, daemon=True).start()
    threading.Thread(target=reconciler_loop, daemon=True).start()
    threading.Thread(target=webhook_server, daemon=True).start()
    threading.Thread(target=_notify_worker, daemon=True).start()
    threading.Thread(target=countdown_updater, daemon=True).start()
    telegram_loop()

if __name__ == "__main__":
    main()
