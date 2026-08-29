#!/usr/bin/env python3
"""
UPS Monitor Brain — Pi (192.168.0.169)
Version: 6.5 (ntfy fallback when Telegram unreachable)
"""

import html as html_lib
import json
import logging
import os
import re
import socket
import ssl
import http.client
import threading
import time
import urllib.request
import urllib.parse
import requests
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ===================== CONFIG =====================
ESP32_IP            = "192.168.0.178"
ESP32_PORT          = 80
ESP32_POLL_INTERVAL = 15        # seconds between state polls
ESP32_TIMEOUT       = 12
ESP32_DEAD_THRESH   = 3         # consecutive failures → alert

PROXMOX_IP          = "192.168.0.50"
PROXMOX_PORT        = 8006
PROXMOX_NODE        = "prox"
PVE_API_TOKEN       = "__PVE_TOKEN__"
PROXMOX_TIMEOUT     = 5

EXTENDER_URL        = "http://127.0.0.1:9998/extender-uptime"
EXTENDER_TIMEOUT    = 4

TG_BOT_TOKEN        = "__TG_BOT_TOKEN__"
TG_CHAT_ID          = "__TG_CHAT_ID__"
TG_POLL_TIMEOUT     = 5         # Telegram long-poll seconds
TG_RETRY_DELAY      = 5         # wait after Telegram error

# ntfy fallback — used when Telegram delivery fails (e.g. WAN down).
# Domain first (via NPM reverse proxy), then raw LXC IP in case the
# domain/DNS path breaks during a WAN failover.
NTFY_URLS           = [
    "https://ntfy.__NTFY_DDNS_HOST__",
    "http://10.10.10.241",
]
NTFY_TOPIC          = "ups"
NTFY_TIMEOUT        = 6

# Plug-and-play SOCKS5 proxy
TG_PROXY            = None  
# TG_PROXY = "__TG_PROXY__"
# Timeouts for mains/WAN failures
MAINS_FAILURE_TIMEOUT_MS = 300_000    # 5 min
WAN_FAILURE_TIMEOUT_MS   = 600_000    # 10 min
EXTENDER_BOOT_LAG_SEC    = 40         # 40 seconds approx time extender takes to respond after mains restores


LOG_FILE = "/var/log/ups-monitor.log"
COUNTERS_FILE = "/var/lib/ups-monitor/daily-counters.json"
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# Fail fast (and clearly) if a SOCKS proxy is configured without PySocks
if TG_PROXY and TG_PROXY.startswith("socks"):
    try:
        import socks  # noqa: F401  — provided by PySocks, required for socks5h://
    except ImportError:
        log.error("TG_PROXY is set but PySocks is missing (pip install requests[socks]) — disabling proxy.")
        TG_PROXY = None

# --- Shared State ---
_lock          = threading.Lock()
_esp32_state   = {}       
_esp32_fail    = 0        
_esp32_alerted = False    
_tg_last_id    = -1
_tg_session = requests.Session()
_tg_lock = threading.Lock()  # requests.Session is not officially thread-safe
_mains_down_started_at = None 
_daily_counters = {"date": None, "mains_down": 0, "shutdowns": 0}

# Reconciliation: tracks how long Proxmox has been continuously confirmed
# online while the ESP32 still thinks it's shut down (e.g. BIOS auto-power-on
# after a UPS-draining outage, bypassing WOL entirely).
_prox_online_since = None
RECONCILE_MIN_ONLINE_SEC = 150   # must be online this long before we touch flags

# ==================================================
# NETWORK OPERATIONS HELPERS
# ==================================================

def _http_get_raw(url, timeout=5, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()

def _https_get_insecure(host, port, path, timeout=5, headers=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    return r.status, r.read().decode()

def _tcp_reachable(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

# ==================================================
# TELEGRAM SERVICE CLIENT
# ==================================================

def _tg_request(method, params=None, retries=2, retry_delay=2):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    proxies = {"https": TG_PROXY, "http": TG_PROXY} if TG_PROXY else None
    last_err = None
    for attempt in range(retries + 1):
        try:
            with _tg_lock:
                r = _tg_session.get(url, params=params or {}, proxies=proxies, timeout=TG_POLL_TIMEOUT + 3)
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(retry_delay)
    log.warning(f"Telegram request failed after {retries + 1} attempts: {last_err}")
    return None

def send_telegram(text):
    result = _tg_request("sendMessage", {
        "chat_id":    TG_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    })
    if not result or not result.get("ok"):
        log.error(f"FATAL: Telegram dropped payload (WAN down?): {result}")
        return False
    return True

# ==================================================
# NTFY FALLBACK CHANNEL (used when Telegram fails)
# ==================================================

def _strip_html(text):
    """Converts Telegram HTML markup to plain text for ntfy."""
    text = re.sub(r"</?(b|i|u|s|code|pre|a)[^>]*>", "", text)
    return html_lib.unescape(text)

def send_ntfy(text, tg_failed=False):
    """
    Publishes to every ntfy endpoint until one accepts. Returns True if
    any delivery succeeded. Tries the reverse-proxy domain first, then
    the raw container IP — during a WAN outage DNS/proxy paths can
    degrade differently, so both are attempted.
    """
    body     = _strip_html(text)
    # ntfy rejects bodies over ~4KB just like Telegram — truncate so the
    # fallback channel can still deliver an oversized alert.
    if len(body) > 4000:
        body = body[:4000] + "\n… [truncated]"
    lines    = [l.strip() for l in body.splitlines() if l.strip()]
    title    = lines[0] if lines else "UPS Notification"
    payload  = ("\n".join(lines[1:]) or title).encode()

    if "🚨" in body:
        priority, tag = "urgent", "rotating_light"
    elif ("⚠️" in body) or ("🔴" in body):
        priority, tag = "high", "warning"
    else:
        priority, tag = "default", "information_source"

    # HTTP headers are latin-1 only — drop emoji/unicode from the title.
    safe_title = re.sub(r"[^\x20-\x7e]", "", title).strip() or "UPS Notification"
    if tg_failed:
        safe_title = "[TG FAILED] " + safe_title
    headers = {"Title": safe_title[:200], "Priority": priority, "Tags": tag}

    delivered_via = None
    for base in NTFY_URLS:
        try:
            req = urllib.request.Request(f"{base}/{NTFY_TOPIC}", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT) as r:
                if r.status == 200:
                    delivered_via = base
                    break
        except Exception as e:
            log.warning(f"ntfy delivery via {base} failed: {e}")
    if delivered_via:
        log.info(f"ntfy notification delivered via {delivered_via}")
        return True
    log.error("ntfy delivery failed on all endpoints")
    return False

def notify(text):
    """
    Single entry point for all outbound alerts.
    Primary: Telegram. Fallback: ntfy (message is flagged when TG dropped).
    Returns True only if at least one channel confirmed delivery.
    """
    if send_telegram(text):
        return True
    log.warning("Telegram unreachable — falling back to ntfy")
    return send_ntfy(text, tg_failed=True)

# ==================================================
# CORE LOGIC AGGREGATORS
# ==================================================

def fmt_uptime(secs):
    if secs < 0: return "Unavailable"
    days  = int(secs) // 86400
    hours = (int(secs) % 86400) // 3600
    mins  = (int(secs) % 3600)  // 60
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)

def fmt_downtime(secs):
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {s}s"
    h, m = divmod(mins, 60)
    return f"{h}h {m}m"

def _bar(frac, width=10):
    """Progress bar for countdowns — e.g. ▰▰▰▰▰▰▰▱▱▱"""
    try:
        frac = max(0.0, min(1.0, float(frac)))
    except (TypeError, ValueError):
        frac = 0.0
    filled = round(frac * width)
    return "▰" * filled + "▱" * (width - filled)

def _rssi_bars(rssi):
    """4-segment signal-strength indicator from dBm."""
    if rssi >= -50:   return "▂▄▆█"
    elif rssi >= -65: return "▂▄▆░"
    elif rssi >= -80: return "▂▄░░"
    else:             return "▂░░░"

def _today_str():
    return time.strftime("%Y-%m-%d")

def _load_counters():
    global _daily_counters
    try:
        with open(COUNTERS_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == _today_str():
            _daily_counters = data
        else:
            _daily_counters = {"date": _today_str(), "mains_down": 0, "shutdowns": 0}
    except Exception:
        _daily_counters = {"date": _today_str(), "mains_down": 0, "shutdowns": 0}

def _save_counters():
    try:
        os.makedirs(os.path.dirname(COUNTERS_FILE), exist_ok=True)
        tmp_path = COUNTERS_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(_daily_counters, f)
        os.replace(tmp_path, COUNTERS_FILE)  # atomic on same filesystem
    except Exception as e:
        log.warning(f"Failed to persist daily counters: {e}")

def _bump_counter(key):
    """Increments a daily counter, resetting on date rollover. Call under _lock."""
    today = _today_str()
    if _daily_counters.get("date") != today:
        _daily_counters["date"] = today
        _daily_counters["mains_down"] = 0
        _daily_counters["shutdowns"] = 0
    _daily_counters[key] = _daily_counters.get(key, 0) + 1
    _save_counters()

def _get_counters():
    today = _today_str()
    with _lock:
        if _daily_counters.get("date") != today:
            _daily_counters["date"] = today
            _daily_counters["mains_down"] = 0
            _daily_counters["shutdowns"] = 0
            _save_counters()
        return dict(_daily_counters)

def get_proxmox_uptime():
    # NOTE: TCP reachability counts as "online" on purpose — for a UPS monitor,
    # a node still accepting connections means it has power. A hard-hung node
    # that keeps listening will simply never confirm "offline" and hit the
    # verification timeout warning instead, which is the desired failsafe.
    if not _tcp_reachable(PROXMOX_IP, PROXMOX_PORT, timeout=3):
        return False, "Offline"
    try:
        path = f"/api2/json/nodes/{PROXMOX_NODE}/status"
        status, body = _https_get_insecure(PROXMOX_IP, PROXMOX_PORT, path, timeout=PROXMOX_TIMEOUT, headers={"Authorization": PVE_API_TOKEN})
        if status != 200: return True, "Unavailable"
        data = json.loads(body)
        secs = data.get("data", {}).get("uptime", -1)
        return True, fmt_uptime(int(secs))
    except Exception:
        return True, "Unavailable"

def get_extender_uptime():
    try:
        _, body = _http_get_raw(EXTENDER_URL, timeout=EXTENDER_TIMEOUT)
        secs = int(body.strip())
        return fmt_uptime(secs) if secs > 0 else "Unavailable"
    except Exception:
        return "Unavailable"

def _poll_esp32():
    try:
        url = f"http://{ESP32_IP}:{ESP32_PORT}/state"
        _, body = _http_get_raw(url, timeout=ESP32_TIMEOUT)
        return json.loads(body)
    except Exception as e:
        log.warning(f"Background thread state poll encountered error: {e}")
        return None

def _send_esp32_command(cmd_dict):
    try:
        url  = f"http://{ESP32_IP}:{ESP32_PORT}/command"
        data = json.dumps(cmd_dict).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=ESP32_TIMEOUT) as r:
            return r.status == 200
    except Exception as e:
        log.error(f"Failed to submit execution structural request: {e}")
        return False

def _get_esp32_state():
    with _lock: return dict(_esp32_state)

def _is_esp32_reachable():
    with _lock: return _esp32_fail < ESP32_DEAD_THRESH

def _verify_proxmox_transition(expect_up, trigger_label, timeout_sec=120, interval_sec=10):
    """
    Polls Proxmox until it reaches the expected state (up or down),
    then sends a Telegram confirmation. Runs in its own thread — never
    blocks the caller.
    """
    global _mains_down_started_at
    start = time.time()
    while time.time() - start < timeout_sec:
        prox_up, prox_uptime = get_proxmox_uptime()
        if prox_up == expect_up:
            if expect_up:
                downtime_line = ""
                with _lock:
                    if _mains_down_started_at is not None:
                        elapsed = time.time() - _mains_down_started_at
                        adjusted = max(0, elapsed - EXTENDER_BOOT_LAG_SEC)
                        downtime_line = f"\n⏱️ Approx mains downtime: ~{fmt_downtime(adjusted)}"
                        _mains_down_started_at = None
                notify(
                    f"✅ <b>Proxmox Confirmed Online</b>\n\n"
                    f"Trigger: {trigger_label}\n⌚ Uptime: {prox_uptime}"
                    f"{downtime_line}"
                )
            else:
                notify(
                    f"✅ <b>Proxmox Confirmed Offline</b>\n\n"
                    f"Trigger: {trigger_label}"
                )
            return
        time.sleep(interval_sec)

    # Timed out without reaching expected state
    state_word = "online" if expect_up else "offline"
    notify(
        f"⚠️ <b>Verification Timeout</b>\n\n"
        f"Trigger: {trigger_label}\n"
        f"Proxmox did not confirm {state_word} within {timeout_sec}s. Check manually."
    )

def start_verification(expect_up, trigger_label, timeout_sec=120, interval_sec=10):
    threading.Thread(
        target=_verify_proxmox_transition,
        args=(expect_up, trigger_label, timeout_sec, interval_sec),
        daemon=True,
    ).start()
    
# ==================================================
# ESP32 POLLING DAEMON
# ==================================================

def _check_bios_reconciliation(state):
    """
    If the ESP32 still believes Proxmox is shut down (any sdMains/sdWAN/
    sdManual flag set) but Proxmox has been confirmed ONLINE continuously
    for RECONCILE_MIN_ONLINE_SEC, it almost certainly booted on its own
    (BIOS 'power on after AC loss') without going through executeWakeProxmox().
    Force a wake command to clear the stale flags.

    Deliberately does NOT trigger on mains/WAN health alone — only on
    Proxmox's actual confirmed online state, so a genuine manual /off
    (where Proxmox stays truly offline) is never touched.
    """
    global _prox_online_since

    if not state:
        return
    flags_stuck = state.get("sdMains") or state.get("sdWAN") or state.get("sdManual")
    if not flags_stuck:
        _prox_online_since = None
        return

    prox_up, _ = get_proxmox_uptime()
    if not prox_up:
        _prox_online_since = None
        return

    now = time.time()
    if _prox_online_since is None:
        _prox_online_since = now
        return

    if now - _prox_online_since >= RECONCILE_MIN_ONLINE_SEC:
        log.warning("Reconciling stale shutdown flags — Proxmox online without WOL trigger.")
        _send_esp32_command({"cmd": "wake"})
        notify(
            "⚠️ <b>Flags Reconciled</b>\n\n"
            "Proxmox has been online for 2.5+ min but the ESP32 still had a "
            "shutdown flag set (likely BIOS auto-power-on after mains restored, "
            "bypassing WOL). Sent a wake command to clear stale flags."
        )
        _prox_online_since = None

def esp32_poll_loop():
    global _esp32_fail, _esp32_alerted, _esp32_state
    log.info("Starting background worker polling loop...")
    BACKOFF_CAP = 60  # never wait longer than this between polls
    while True:
        state = _poll_esp32()
        alert_dead, alert_recovery, recovery_fw = False, False, "?"
        with _lock:
            if state is not None:
                if _esp32_alerted:
                    alert_recovery = True
                    recovery_fw = state.get("fw", "?")
                _esp32_state = state
                _esp32_fail = 0
                _esp32_alerted = False
            else:
                _esp32_fail += 1
                if _esp32_fail >= ESP32_DEAD_THRESH and not _esp32_alerted:
                    # Do not set _esp32_alerted to True here yet!
                    alert_dead = True
        
        if alert_recovery:
            notify(f"✅ <b>ESP32 BACK ONLINE</b>\n\nUPS sensor ({ESP32_IP}) recovered.\nFirmware: {recovery_fw}")
        elif alert_dead:
            success = notify(f"⚠️ <b>ESP32 UNREACHABLE</b>\n\nSensor missed metrics. Authority systems remain autonomous.")
            with _lock:
                if success:
                    _esp32_alerted = True # Only lock out future alerts if we actually told the user

        if state is not None:
            _check_bios_reconciliation(state)

        with _lock:
            fail_count = _esp32_fail
        if fail_count >= ESP32_DEAD_THRESH:
            sleep_time = min(ESP32_POLL_INTERVAL * (2 ** (fail_count - ESP32_DEAD_THRESH + 1)), BACKOFF_CAP)
        else:
            sleep_time = ESP32_POLL_INTERVAL
        time.sleep(sleep_time)

# ==================================================
# REALTIME ESP32 NOTIFICATION WEBHOOK SERVER
# ==================================================

class ESPNotifyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/notify":
            params = urllib.parse.parse_qs(parsed_url.query)
            event_type = params.get("event", [""])[0]
            
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            
            if event_type:
                mins_raw = params.get("mins", [None])[0]
                threading.Thread(target=process_esp_notification, args=(event_type, mins_raw)).start()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args): pass

def _patch_esp32_state(patch):
    """
    Applies realtime corrections to the cached ESP32 state. Only patches
    after a real poll has populated the cache — never fabricates partial
    state (a partial dict would make build_status_message() report bogus
    defaults like "WAN DOWN" before the first successful poll).
    Call under _lock.
    """
    if _esp32_state:
        _esp32_state.update(patch)

def process_esp_notification(event, mins_raw=None):
    log.info(f"Asynchronous webhook hit from ESP32: event={event} mins={mins_raw}")
    global _esp32_state, _mains_down_started_at

    approx_downtime_str = None  # populated only on a real restore-from-down event

    # ESP32 reports its current (possibly custom) mains delay in the
    # countdown-start webhook so the Telegram message is always accurate.
    countdown_mins_txt = None
    if mins_raw:
        try:
            m = max(1, int(str(mins_raw)))
            countdown_mins_txt = f"{m} minute{'s' if m != 1 else ''}"
        except (TypeError, ValueError):
            pass

    # --- Real-Time State Overrides to Prevent Cache Stodginess ---
    with _lock:
        if event == "mains_down_countdown_start":
            _mains_down_started_at = time.time()
            _bump_counter("mains_down")
            _patch_esp32_state({"mainsUp": False, "mainsFailSinceMs": 1})  # Force metrics to reflect a running countdown
        elif event == "mains_false_alarm" or event == "mains_restored_override_cleared":
            if _mains_down_started_at is not None:
                elapsed = time.time() - _mains_down_started_at
                adjusted = max(0, elapsed - EXTENDER_BOOT_LAG_SEC)
                approx_downtime_str = fmt_downtime(adjusted)
                _mains_down_started_at = None
            _patch_esp32_state({"mainsUp": True, "mainsFailSinceMs": 0})
        elif event == "shutdown_mains_start":
            _bump_counter("shutdowns")
            _patch_esp32_state({"mainsUp": False, "sdMains": True})
        elif event == "shutdown_wan_start":
            _bump_counter("shutdowns")
            _patch_esp32_state({"wanUp": False, "sdWAN": True})

    mapping = {
        "esp_booted":
            "🟢 <b>ESP32 Online</b>\n\nUPS monitor started. Watching mains and WAN.",
        "power_instability":
            "⚡ <b>Power Instability</b>\n\nMains has flapped 3+ times in the last 10 minutes. Worth checking the supply.",
        "mains_down_countdown_start":
            "⚠️ <b>Mains Down</b>\n\nCan't reach 192.168.0.2. Shutdown in <b>"
            + (countdown_mins_txt or "5 minutes") + "</b> if not restored.",
        "mains_down_override_active":
            "⚠️ <b>Mains Down</b>\n\nManual override is active — auto-shutdown suppressed. Send /off to shut down manually.",
        "mains_down_shutdown_suppressed":
            "🚨 <b>Mains Down — SHUTDOWN SUPPRESSED</b>\n\nA shutdown flag is already stuck set (sdMains/sdWAN/sdManual) so auto-shutdown will NOT fire. This is likely a stale flag from a previous event. Run /diag and reconcile — Proxmox is currently unprotected.",
        "mains_false_alarm":
            "✅ <b>Mains Restored</b>\n\nLine recovered before the 5 min timeout. No action taken."
            + (f"\n⏱️ Approx downtime: ~{approx_downtime_str}" if approx_downtime_str else ""),
        "mains_restored_override_cleared":
            "✅ <b>Mains Restored</b>\n\nManual override cleared. Monitoring resumed normally."
            + (f"\n⏱️ Approx downtime: ~{approx_downtime_str}" if approx_downtime_str else ""),
        "shutdown_mains_start":
            "🔴 <b>Shutting Down — Mains Timeout</b>\n\nMains was down for 5 minutes. Sending shutdown to Proxmox now.",
        "shutdown_wan_start":
            "🔴 <b>Shutting Down — WAN Timeout</b>\n\nNo internet for 10 minutes. Sending shutdown to Proxmox now.",
        "shutdown_manual_mains_down":
            "🔴 <b>Shutting Down — Manual</b>\n\n/off received while mains is down. Proxmox will auto-restore when power returns.",
        "shutdown_manual_normal":
            "🔴 <b>Shutting Down — Manual</b>\n\n/off received. Send /on to bring it back up.",
        "shutdown_complete":
            "✅ <b>Shutdown Complete</b>\n\nProxmox webhook acknowledged. Node going down.",
        "restoring_network_stabilization":
            "🟡 <b>Preparing to Wake</b>\n\nWaiting 15 seconds for network to stabilize before sending WOL...",
        "wol_packet_sent":
            "📡 <b>WOL Sent</b>\n\nWake-on-LAN packet broadcast to M900 (__MAC__). Boot takes ~30–60s.",
        "wan_restored_mains_down_hold":
            "🌐 <b>WAN Restored</b>\n\nInternet is back, but mains is still down. Holding restore until power returns.",
    }
    if event in mapping:
        notify(mapping[event])

    # --- Outcome verification for shutdown/wake triggers ---
    if event in ("shutdown_mains_start", "shutdown_wan_start",
                "shutdown_manual_mains_down", "shutdown_manual_normal"):
        start_verification(expect_up=False, trigger_label=event, timeout_sec=120, interval_sec=8)
    elif event == "wol_packet_sent":
        start_verification(expect_up=True, trigger_label=event, timeout_sec=120, interval_sec=10)

def start_webhook_server():
    log.info("Launching incoming notification intercept engine on port 9997...")
    server = ThreadingHTTPServer(("0.0.0.0", 9997), ESPNotifyHandler)
    server.serve_forever()

# ==================================================
# STATUS CONTEXT ASSEMBLY
# ==================================================

def build_status_message():
    state = _get_esp32_state()
    sensor_alive = _is_esp32_reachable()

    prox_up, prox_uptime = get_proxmox_uptime()
    ext_uptime = get_extender_uptime()

    header = (
        f"⚡ <b>UPS STATUS</b>  <i>{time.strftime('%H:%M:%S')}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    if state:
        mains_up = state.get("mainsUp", False)
        wan_up   = state.get("wanUp",   False)

        sd_mains  = state.get("sdMains",            False)
        sd_wan    = state.get("sdWAN",              False)
        sd_manual = state.get("sdManual",           False)
        man_ovr   = state.get("manualOverride",     False)

        mains_fail_ms = state.get("mainsFailSinceMs", 0)
        wan_fail_ms   = state.get("wanFailSinceMs",   0)

        # Runtime-adjustable delay reported by the ESP32; fall back to the
        # compiled-in default when absent (older firmware / no data).
        mains_delay_ms = state.get("mainsDelayMs", MAINS_FAILURE_TIMEOUT_MS)
        if not isinstance(mains_delay_ms, (int, float)) or mains_delay_ms < 60000:
            mains_delay_ms = MAINS_FAILURE_TIMEOUT_MS

        ext_str = f"  · ext <code>{ext_uptime}</code>" if ext_uptime != "Unavailable" else ""
        mains_line = f"{'🟢' if mains_up else '🔴'} <b>Mains</b>  <code>{'UP' if mains_up else 'DOWN'}</code>{ext_str}"
        wan_line   = f"{'🟢' if wan_up else '🔴'} <b>WAN</b>    <code>{'UP' if wan_up else 'DOWN'}</code>"
        prox_line  = (
            f"{'🟢' if prox_up else '🔴'} <b>Proxmox</b>  "
            f"<code>{'ONLINE' if prox_up else 'OFFLINE'}</code>  · ⏱ <code>{prox_uptime}</code>"
        )

        # Countdown — only when actively counting down, with progress bar
        countdown_line = ""
        if not mains_up and mains_fail_ms > 0 and not man_ovr and not (sd_mains or sd_wan or sd_manual):
            remaining = max(0, mains_delay_ms - mains_fail_ms)
            m = int(remaining // 1000 // 60)
            s = int(remaining // 1000 % 60)
            frac = remaining / mains_delay_ms
            countdown_line = (
                f"\n\n⏳ <b>Shutting down in {m}m {s}s</b>\n"
                f"<code>{_bar(frac)}</code> <i>{int(round(frac * 100))}% left</i>"
            )
        elif not wan_up and wan_fail_ms > 0 and not (sd_mains or sd_wan or sd_manual):
            remaining = max(0, WAN_FAILURE_TIMEOUT_MS - wan_fail_ms)
            m = int(remaining // 1000 // 60)
            s = int(remaining // 1000 % 60)
            frac = remaining / WAN_FAILURE_TIMEOUT_MS
            countdown_line = (
                f"\n\n⏳ <b>WAN shutdown in {m}m {s}s</b>\n"
                f"<code>{_bar(frac)}</code> <i>{int(round(frac * 100))}% left</i>"
            )

        if sd_manual:             sd_reason = "Manual /off"
        elif sd_mains and sd_wan: sd_reason = "Mains &amp; WAN failure"
        elif sd_mains:            sd_reason = "Mains failure"
        elif sd_wan:              sd_reason = "WAN failure"
        else:                     sd_reason = None

    else:
        mains_line = "⚪ <b>Mains</b>  <code>UNKNOWN</code>"
        wan_line   = "⚪ <b>WAN</b>    <code>UNKNOWN</code>"
        prox_line  = f"{'🟢' if prox_up else '🔴'} <b>Proxmox</b>  <code>{'ONLINE' if prox_up else 'OFFLINE'}</code>  · ⏱ <code>{prox_uptime}</code>"
        countdown_line = ""
        sd_reason = None

    stale_note = "\n⚠️ <i>ESP32 offline — showing cached data</i>" if not sensor_alive else ""
    footer     = f"🛡 <b>{sd_reason}</b>" if sd_reason else "🛡 <i>No active shutdown reason</i>"

    counters = _get_counters()
    stats_line = (
        f"📊 Today · <b>{counters['mains_down']}</b> mains down · "
        f"<b>{counters['shutdowns']}</b> shutdown{'s' if counters['shutdowns'] != 1 else ''}"
    )

    return (
        f"{header}\n"
        f"{mains_line}\n"
        f"{wan_line}\n"
        f"{prox_line}"
        f"{countdown_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{footer}\n"
        f"{stats_line}"
        f"{stale_note}"
    )

# ==================================================
# DIAGNOSIS CONTEXT ASSEMBLY
# ==================================================

def build_diag_message():
    state = _get_esp32_state()
    sensor_alive = _is_esp32_reachable()

    if not state:
        return "🔧 <b>ESP32 DIAGNOSTICS</b>\n\n⚪ <i>Unreachable — no data available.</i>"

    fw        = state.get("fw", "?")
    esp_ms    = state.get("espUptimeMs", 0)
    rssi      = state.get("rssi", 0)
    free_heap = state.get("freeHeap", 0)
    flaps     = state.get("recentFlaps", 0)
    man_mains = state.get("manualOffMainsDown", False)
    man_ovr   = state.get("manualOverride", False)

    if rssi >= -50:   rssi_q, bars = "EXCELLENT", _rssi_bars(rssi)
    elif rssi >= -65: rssi_q, bars = "Good",     _rssi_bars(rssi)
    elif rssi >= -80: rssi_q, bars = "Weak",     _rssi_bars(rssi)
    else:             rssi_q, bars = "Critical", _rssi_bars(rssi)

    badge = "🔴 STALE" if not sensor_alive else "🟢 LIVE"

    panel = (
        f"┌─ ESP32 NODE ───────────────\n"
        f"│ ip       {ESP32_IP}\n"
        f"│ firmware {fw}\n"
        f"│ uptime   {fmt_uptime(esp_ms // 1000)}\n"
        f"│ signal   {rssi} dBm {bars} {rssi_q}\n"
        f"│ heap     {free_heap // 1024} KB free\n"
        f"│ flaps    {flaps}/3 · last 10 min\n"
        f"├─ CONFIG ───────────────────\n"
        f"│ mains delay  {int(state.get('mainsDelayMs', MAINS_FAILURE_TIMEOUT_MS)) // 60000} min\n"
        f"│ wan timeout  {WAN_FAILURE_TIMEOUT_MS // 60000} min\n"
        f"│ poll         every {ESP32_POLL_INTERVAL}s\n"
        f"├─ FLAGS ────────────────────\n"
        f"│ override     {'ON ' if man_ovr else 'OFF'}\n"
        f"│ off-while-down {'YES' if man_mains else 'NO'}\n"
        f"└────────────────────────────"
    )

    return (
        f"🔧 <b>ESP32 DIAGNOSTICS</b>  <i>{badge}</i>\n\n"
        f"<code>{panel}</code>"
    )

# ==================================================
# TELEGRAM POLLING & COMMAND DISPATCH ENGINE
# ==================================================

def handle_command(text):
    text = text.strip()
    log.info(f"Processing chat instruction string tokens: {text!r}")

    if text == "/status":
        notify(build_status_message())

    elif text == "/diag":
        notify(build_diag_message())

    elif text == "/on":
        if not _is_esp32_reachable():
            notify(
                "❌ <b>ESP32 Unreachable</b>\n\n"
                "Can't send wake command — no link to sensor.\n"
                "Check 192.168.0.178 manually."
            )
            return
        prox_up, _ = get_proxmox_uptime()
        if prox_up:
            notify(
                "ℹ️ <b>Already Online</b>\n\n"
                "Proxmox is already running. No action taken."
            )
            return
        _send_esp32_command({"cmd": "wake"})

    elif text == "/off":
        if not _is_esp32_reachable():
            notify(
                "❌ <b>ESP32 Unreachable</b>\n\n"
                "Can't send shutdown command — no link to sensor.\n"
                "Shut down Proxmox manually via console."
            )
            return
        prox_up, _ = get_proxmox_uptime()
        if not prox_up:
            notify(
                "ℹ️ <b>Already Offline</b>\n\n"
                "Proxmox is already down. No action taken."
            )
            return
        _send_esp32_command({"cmd": "shutdown"})

    elif text.split()[0] in ("/custom-delay", "/custom_delay"):
        parts = text.split()
        state = _get_esp32_state()
        cur_ms = state.get("mainsDelayMs") if state else None

        if len(parts) == 1:
            # No argument — show current setting and usage
            cur = f"{int(cur_ms) // 60000} min" if isinstance(cur_ms, (int, float)) and cur_ms >= 60000 else "unknown (no data from ESP32)"
            notify(
                "ℹ️ <b>Mains Shutdown Delay</b>\n\n"
                f"Current: <b>{cur}</b> (default 5 min)\n"
                "Usage: <code>/custom-delay &lt;minutes&gt;</code> (1–720)\n"
                "Or: <code>/custom-delay reset</code>"            )
            return

        arg = parts[1].lower()
        if arg == "reset":
            minutes = 5
        else:
            try:
                minutes = int(arg)
            except ValueError:
                notify("⚠️ Usage: <code>/custom-delay &lt;minutes&gt;</code> (1–720) or <code>/custom-delay reset</code>")
                return
            if not (1 <= minutes <= 720):
                notify("⚠️ Delay must be between <b>1</b> and <b>720</b> minutes.")
                return

        if not _is_esp32_reachable():
            notify(
                "❌ <b>ESP32 Unreachable</b>\n\n"
                "Can't change the delay — no link to sensor."
            )
            return

        if _send_esp32_command({"cmd": "custom_delay", "minutes": minutes}):
            suffix = " (default)" if minutes == 5 else ""
            notify(
                f"✅ <b>Mains Shutdown Delay Set</b>\n\n"
                f"Auto-shutdown now fires after <b>{minutes} min{suffix}</b> of mains failure.\n"
                f"Setting is persisted on the ESP32 and survives reboots. WAN timeout unchanged (10 min)."
            )
        else:
            notify("❌ Failed to deliver the delay command to the ESP32.")

def telegram_poll_loop():
    global _tg_last_id
    log.info("Starting long polling service interaction worker threads...")
    while True:
        try:
            params = {"timeout": TG_POLL_TIMEOUT, "allowed_updates": ["message"]}
            if _tg_last_id >= 0: params["offset"] = _tg_last_id + 1
            result = _tg_request("getUpdates", params)
            if result is None or not result.get("ok"):
                time.sleep(TG_RETRY_DELAY)
                continue
            for update in result.get("result", []):
                uid = update.get("update_id", 0)
                if uid > _tg_last_id: _tg_last_id = uid
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) == str(TG_CHAT_ID):
                    text = msg.get("text", "").strip()
                    if text: handle_command(text)
        except Exception as e:
            log.error(f"Long polling engine error: {e}")
            time.sleep(TG_RETRY_DELAY)

if __name__ == "__main__":
    log.info("=== Decoupled UPS Brain System Initialization ===")

    with _lock:
        _load_counters()

    # 1. Start notification server for immediate callback execution
    threading.Thread(target=start_webhook_server, daemon=True, name="webhook-srv").start()
    
    # 2. Start polling background threads for metrics collation
    threading.Thread(target=esp32_poll_loop, daemon=True, name="esp32-poll").start()
    
    time.sleep(1.5)
    
    # 3. Enter permanent polling loops for management plane commands
    telegram_poll_loop()
