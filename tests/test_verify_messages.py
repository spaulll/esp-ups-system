"""Regression: human verify messages (no Trigger noise, no ACK spam).

Covers the 2026-09-10 UX pass:
  - shutdown_webhook_ok is dropped silently (no Info Summary spam)
  - pve_verify uses human "Reason:" — never raw "Trigger: <event>"
  - online confirmation carries total mains downtime and hides
    fresh-boot uptime (0m); mature uptime still shown.
"""
import time


def test_shutdown_webhook_ok_dropped_silently(pm):
    pm.process_event("shutdown_webhook_ok", 1, {"event": "shutdown_webhook_ok"})
    time.sleep(0.3)
    assert pm.delivered == []
    with pm._notify_lock:
        assert pm._info_pending is None


def test_online_confirmed_single_voice_via_verify(pm):
    """One wake = one online message: the ESP-direct text is dropped, the
    PVE-verified confirmation is the sole voice (still triggered here)."""
    calls = []
    orig = pm.pve_verify
    pm.pve_verify = lambda **kw: calls.append(kw)
    try:
        pm.process_event("online_confirmed", 7, {"event": "online_confirmed"})
        time.sleep(0.3)
    finally:
        pm.pve_verify = orig
    assert calls and calls[0].get("label") == "online_confirmed", calls
    assert calls[0].get("expect_up") is True, calls
    assert pm.delivered == [], f"direct online text must not notify: {pm.delivered}"
    with pm._notify_lock:
        assert pm._info_pending is None


def test_human_reason_replaces_trigger(pm):
    pm._esp32_state = {"mainsDelayMs": 300000, "wanTimeoutMs": 600000}
    for label in ("shutdown_mains_start", "shutdown_wan_start",
                  "shutdown_manual_start", "online_confirmed"):
        reason = pm._human_reason(label)
        assert reason.startswith("Reason:"), reason
        assert "Trigger" not in reason, reason
    assert "5 min" in pm._human_reason("shutdown_mains_start")
    assert "10 min" in pm._human_reason("shutdown_wan_start")


def _seen(pm, needle):
    with pm._notify_lock:
        queued = any(needle in e.get("text", "") for e in pm._notify_queue)
        pending = pm._info_pending is not None and \
            any(needle in t for t in pm._info_pending["events"])
    delivered = any(needle in d[1] for d in pm.delivered if d[0] == "tg")
    return queued or pending or delivered


def _get(pm, needle):
    with pm._notify_lock:
        for e in pm._notify_queue:
            if needle in e.get("text", ""):
                return e["text"]
        if pm._info_pending:
            for t in pm._info_pending["events"]:
                if needle in t:
                    return t
    for d in pm.delivered:
        if d[0] == "tg" and needle in d[1]:
            return d[1]
    return ""


def test_offline_verify_uses_human_reason(pm, wait_for):
    pm._esp32_state = {"mainsDelayMs": 300000}
    pm._pve_probe = lambda: (False, None, "Offline")
    pm.pve_verify(expect_up=False, label="shutdown_mains_start",
                  timeout_sec=5, interval=0.05)
    assert wait_for(lambda: _seen(pm, "Confirmed Offline")), pm.delivered
    msg = _get(pm, "Confirmed Offline")
    assert "Trigger" not in msg, msg
    assert "Mains power was down for more than 5 min" in msg, msg


def test_online_verify_hides_fresh_uptime_shows_outage(pm, wait_for):
    pm._last_mains_down_at = time.time() - 400
    pm._pve_probe = lambda: (True, 30, "0m")
    pm.pve_verify(expect_up=True, label="online_confirmed",
                  timeout_sec=5, interval=0.05)
    assert wait_for(lambda: _seen(pm, "Confirmed Online")), pm.delivered
    msg = _get(pm, "Confirmed Online")
    assert "Trigger" not in msg, msg
    assert "Power was out for" in msg, msg
    assert "Uptime" not in msg, f"fresh boot must hide uptime: {msg}"


def test_online_verify_shows_mature_uptime(pm, wait_for):
    pm._last_mains_down_at = None
    pm._last_mains_downtime_sec = None
    pm._pve_probe = lambda: (True, 600, "10m")
    pm.pve_verify(expect_up=True, label="online_confirmed",
                  timeout_sec=5, interval=0.05)
    assert wait_for(lambda: _seen(pm, "Confirmed Online")), pm.delivered
    msg = _get(pm, "Confirmed Online")
    assert "Uptime: 10m" in msg, msg
    assert "Power was out" not in msg, msg
