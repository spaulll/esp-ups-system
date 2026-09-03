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
TG_POLL_TIMEOUT    = 10
TG_PROXY          = "__TG_PROXY__"

NTFY_URLS          = [
    "https://ntfy.__NTFY_DDNS_HOST__",
    "__NTFY_URL_2__",
]
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
_daily_counters = {"date": None, "mains_down": 0, "shutdowns": 0, "blips": 0}

# notification engine
_notify_queue = []          # list of (event_key, class, payload_dict)
_notify_lock  = threading.Lock()
_info_pending = None        # {events: [..], since: ts, notify_at: ts}

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
        _sd_notify("WATCHDOG=1")
        time.sleep(30)

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

def pve_verify(expect_up, label, timeout_sec=180, interval=10):
    """Confirm offline/online via the PVE API (never TCP alone), background thread."""
    def run():
        start = time.time()
        while time.time() - start < timeout_sec:
            online, _uptime_sec, up_str = _pve_probe()
            if expect_up and online:
                notify_event("system_info", "info",
                             f"✅ <b>Proxmox Confirmed Online</b>\n\n"
                             f"Trigger: {label}\n⌚ Uptime: {up_str}")
                return
            if not expect_up and not online:
                notify_event("system_info", "info",
                             f"✅ <b>Proxmox Confirmed Offline</b>\n\nTrigger: {label}")
                return
            time.sleep(interval)
        state_word = "online" if expect_up else "offline"
        notify_event("system_info", "critical",
                     f"⚠️ <b>Verification Timeout</b>\n\nTrigger: {label}\n"
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
EVENT_TAXONOMY = {
    "esp_booted":            ("info",      lambda d: f"🟢 <b>ESP32 Online</b>\n\nBoot reason: {d.get('data') or 'unknown'}"),
    "mains_blip":            ("info",      lambda d: f"⚡ <b>Mains Blip</b>\n\nBrief dip ({d.get('data') or '1x'}), no action."),
    "mains_down":            ("critical",  lambda d: f"🔴 <b>Mains Down</b>\n\nGPIO confirms mains lost. Shutdown in {d.get('data') or '5'} if not restored."),
    "mains_restored":        ("critical",  lambda d: f"✅ <b>Mains Restored</b>\n\nPower returned. Downtime: {fmt_downtime(int(d.get('data','0').split('=')[-1])//1000)}"),
    "shutdown_mains_start":  ("critical",  lambda d: "🔴 <b>Shutting Down — Mains Timeout</b>\n\nMains down past delay. Sending shutdown webhook."),
    "shutdown_wan_start":    ("critical",  lambda d: "🔴 <b>Shutting Down — WAN Timeout</b>\n\nNo internet past timeout. Sending shutdown webhook."),
    "shutdown_manual_start": ("critical",  lambda d: "🔴 <b>Shutting Down — Manual</b>\n\n/off received."),
    "shutdown_webhook_ok":   ("info",      lambda d: "✅ Shutdown webhook acknowledged by node."),
    "shutdown_webhook_failed":("warning",  lambda d: f"⚠️ <b>Shutdown Webhook Failed</b>\n\nAttempt {d.get('data') or '?'} — retrying."),
    "shutdown_complete":     ("critical",  lambda d: "✅ <b>Shutdown Complete</b>\n\nNode confirmed off."),
    "webhook_gave_up":       ("critical",  lambda d: "🚨 <b>Shutdown Webhook Gave Up</b>\n\nNode did not ack after 6 tries. Flag stays set; wake logic armed."),
    "wake_sequence_start":   ("critical",  lambda d: "🟡 <b>Wake Sequence</b>\n\nRestore detected — 15s settle then WOL."),
    "wol_rexmitted":         ("warning",   lambda d: f"📡 <b>WOL Re-sent</b>\n\nProxmox not up yet ({d.get('data') or 'n/5'})."),
    "wake_failed":           ("critical",  lambda d: "🚨 <b>Wake Failed</b>\n\n5 WOL attempts exhausted. Manual intervention needed."),
    "online_confirmed":      ("critical",  lambda d: "✅ <b>Proxmox Online Confirmed</b>\n\nNode is back after power event."),
    "manual_on":             ("info",      lambda d: "✅ <b>Manual On</b>\n\nWake commanded."),
    "manual_override":       ("warning",   lambda d: "⚠️ <b>Manual Override</b>\n\nAuto-shutdown suppressed by /on."),
    "gpio_test":             ("info",      lambda d: f"🧪 GPIO test: value={d.get('data') or '?'}"),
    "mains_delay_set":       ("info",      lambda d: f"⏱️ Mains delay set to {d.get('data') or '?'}."),
    "wan_timeout_set":       ("info",      lambda d: f"⏱️ WAN timeout set to {d.get('data') or '?'}."),
}

# events that also trigger PVE verification
VERIFY_OFFLINE = {"shutdown_mains_start", "shutdown_wan_start", "shutdown_manual_start"}
VERIFY_ONLINE  = {"wake_sequence_start", "online_confirmed"}

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
    with _tg_opener().open(req, timeout=TG_POLL_TIMEOUT + 5) as r:
        return json.loads(r.read().decode()).get("ok", False)

def _strip_html(text):
    text = re.sub(r"</?(b|i|u|s|code|pre|a)[^>]*>", "", text)
    return html.unescape(text)

def _ntfy_send(text, urgent=False):
    body = _strip_html(text)
    if len(body) > 4000:
        body = body[:4000] + "\n… [truncated]"
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    title = lines[0] if lines else "UPS Notification"
    payload = ("\n".join(lines[1:]) or title).encode()
    priority, tag = ("urgent", "rotating_light") if urgent else ("default", "information_source")
    safe_title = re.sub(r"[^\x20-\x7e]", "", title).strip() or "UPS Notification"
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
    if _tg_send(text):
        return True
    log.warning("Telegram delivery failed — falling back to ntfy")
    ok = _ntfy_send(text, urgent=urgent)
    if not ok:
        _append_missed({"at": time.time(), "text": text[:500]})
        log.error("ntfy also failed — recorded in missed ledger")
    return ok

def _notify_worker():
    global _info_pending
    while True:
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

def process_event(evt, seq, data):
    global _last_seq
    taxonomy = EVENT_TAXONOMY.get(evt)
    if not taxonomy:
        log.warning(f"unknown event {evt} (seq={seq})")
        return
    klass, fmt = taxonomy
    if evt == "mains_down":
        _bump_counter("mains_down")
    elif evt == "mains_blip":
        _bump_counter("blips")
    elif evt in ("shutdown_mains_start", "shutdown_wan_start", "shutdown_manual_start"):
        _bump_counter("shutdowns")
    notify_event(evt, klass, fmt(data))
    if evt in VERIFY_OFFLINE:
        pve_verify(expect_up=False, label=evt)
    elif evt in VERIFY_ONLINE:
        pve_verify(expect_up=True, label=evt)

def reconcile_once():
    global _last_seq, _sensor_dead_since
    with _reconcile_lock:
        state = _esp_state()
        if state is not None:
            with _lock:
                was_dead = _sensor_dead_since is not None
                _esp32_state.clear()
                _esp32_state.update(state)
                _sensor_dead_since = None
            if was_dead:
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
def _send_esp(cmd, extra=None):
    d = {"cmd": cmd}
    if extra: d.update(extra)
    return _esp_command(d)

def _bar(frac, width=10):
    frac = max(0.0, min(1.0, float(frac)))
    filled = round(frac * width)
    return "▰" * filled + "▱" * (width - filled)

def cmd_status():
    with _lock:
        s = dict(_esp32_state)
    prox_online, _, prox_up = _pve_probe()
    header = f"⚡ <b>UPS STATUS</b>  <i>{time.strftime('%H:%M:%S')}</i>\n" + "─" * 20
    if not s:
        return header + "\n\n⚠️ Sensor data unavailable."
    mains = s.get("mainsUp", False)
    wan = s.get("wanUp", False)
    lines = [header]
    lines.append(f"{'🟢' if mains else '🔴'} <b>Mains</b>   <code>{'UP' if mains else 'DOWN'}</code>")
    lines.append(f"{'🟢' if wan else '🔴'} <b>WAN</b>     <code>{'UP' if wan else 'DOWN'}</code>")
    lines.append(f"{'🟢' if prox_online else '🔴'} <b>Proxmox</b> <code>{prox_up}</code>")
    mfail = s.get("mainsFailSinceMs", 0)
    wfail = s.get("wanFailSinceMs", 0)
    mdelay = s.get("mainsDelayMs", 300000)
    if mfail:
        frac = mfail / max(mdelay, 1)
        lines.append(f"⏳ Mains countdown {_bar(frac)} ({fmt_downtime((mdelay - mfail)/1000)} left)")
    if wfail:
        wto = s.get("wanTimeoutMs", 600000)
        frac = wfail / max(wto, 1)
        lines.append(f"⏳ WAN countdown  {_bar(frac)}")
    flags = []
    for k, label in (("sdMains", "sdMains"), ("sdWAN", "sdWAN"), ("sdManual", "sdManual")):
        if s.get(k): flags.append(label)
    if flags:
        lines.append("🚩 Flags: " + ", ".join(flags))
    lines.append("")
    lines.append(f"📅 Today: mains↓ {_daily_counters.get('mains_down',0)} · shutdowns {_daily_counters.get('shutdowns',0)} · blips {_daily_counters.get('blips',0)}")
    return "\n".join(lines)

def cmd_diag():
    with _lock:
        s = dict(_esp32_state)
    prox_online, _, prox_up = _pve_probe()
    lines = ["🔧 <b>DIAG</b>\n" + "─" * 20]
    if not s:
        lines.append("⚠️ No sensor data.")
    else:
        lines.append(f"Firmware:  <code>{s.get('fw')}</code>")
        lines.append(f"Uptime:    {fmt_downtime(s.get('espUptimeMs',0)/1000)}")
        lines.append(f"Reset:     <code>{s.get('espResetReason')}</code>")
        lines.append(f"Heap:      {s.get('freeHeap')} B")
        lines.append(f"RSSI:      <code>{s.get('rssi')}</code> dBm")
        lines.append(f"mainsRaw:  <code>{s.get('mainsRaw')}</code> · mainsUp {s.get('mainsUp')}")
        lines.append(f"wanUp:     {s.get('wanUp')}")
        lines.append(f"MainsDelay: <code>{s.get('mainsDelayMs',0)//60000}</code> min · WAN timeout <code>{s.get('wanTimeoutMs',0)//60000}</code> min")
        lines.append(f"LEDGER seq: <code>{s.get('seq')}</code> · last processed <code>{_last_seq}</code>")
        lines.append(f"Flags: sdMains={s.get('sdMains')} sdWAN={s.get('sdWAN')} sdManual={s.get('sdManual')} override={s.get('manualOverride')}")
        c = s.get("counters", {})
        lines.append(f"Counters: {c}")
    lines.append(f"Proxmox:  <code>{prox_up}</code>")
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
    ok = _esp_command({"cmd": cmd, "minutes": mins})
    return "✅ Set." if ok else "❌ ESP32 unreachable."

def _tg_get(path, params):
    """GET a Telegram API path with optional SOCKS proxy."""
    qs = urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{path}?{qs}"
    with _tg_opener().open(url, timeout=TG_POLL_TIMEOUT + 5) as r:
        return json.loads(r.read().decode())

def _tg_poll():
    offset = _load_tg_offset()
    while True:
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

def handle_command(cmd, arg):
    if cmd == "/status":
        return cmd_status()
    if cmd == "/diag":
        return cmd_diag()
    if cmd == "/on":
        ok = _send_esp("wake")
        return "✅ Wake commanded." if ok else "❌ ESP32 unreachable."
    if cmd == "/off":
        ok = _send_esp("shutdown")
        return "✅ Shutdown commanded." if ok else "❌ ESP32 unreachable."
    if cmd in ("/mainsdelay", "/wantimeout"):
        return cmd_set_delay(cmd.lstrip("/"), arg)
    return ("Available: /status /diag /on /off "
            "/mainsdelay [1-720|reset] /wantimeout [5-120|reset]")

def telegram_loop():
    log.info("telegram loop started")
    _tg_poll()

# ===================== MAIN =====================
def main():
    _load_seq()
    _load_counters()
    _sd_notify("READY=1")
    threading.Thread(target=watchdog_thread, daemon=True).start()
    threading.Thread(target=reconciler_loop, daemon=True).start()
    threading.Thread(target=webhook_server, daemon=True).start()
    threading.Thread(target=_notify_worker, daemon=True).start()
    telegram_loop()

if __name__ == "__main__":
    main()
