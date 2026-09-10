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


def test_wan_restored_taxonomy(pm):
    """fw V7.3 emits wan_restored with downtimeMs — mirrors mains_restored."""
    klass, fmt = pm.EVENT_TAXONOMY["wan_restored"]
    assert klass == "critical", f"wan_restored must be immediate, got {klass}"
    msg = fmt({"data": "downtimeMs=125000"})
    assert "Internet Restored" in msg, msg
    assert "2m 5s" in msg, msg
    # malformed payload degrades gracefully — never crashes the reconciler
    assert "Internet Restored" in fmt({"data": ""}), fmt({"data": ""})
    assert "Internet Restored" in fmt({}), fmt({})


def test_wan_restored_below_threshold_dropped_silently(pm, wait_for):
    """Regression (2026-09-10): a 14s TCP flap produced 'Internet Restored'
    + a /history 'power' line with no real outage. Sub-60s restores must
    notify nothing and record nothing."""
    pm.process_event("shutdown_wan_start", 1, {"event": "shutdown_wan_start"})
    assert wait_for(lambda: any("No Internet" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    pm.process_event("wan_restored", 2,
                     {"event": "wan_restored", "data": "downtimeMs=14000"})
    time.sleep(0.3)
    assert not any("Internet Restored" in d[1] for d in pm.delivered), pm.delivered
    assert pm._load_history() == [], "sub-threshold flap must leave no history"


def test_wan_restored_above_threshold_alerts(pm, wait_for):
    pm.process_event("shutdown_wan_start", 1, {"event": "shutdown_wan_start"})
    assert wait_for(lambda: any("No Internet" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    pm.process_event("wan_restored", 2,
                     {"event": "wan_restored", "data": "downtimeMs=103199"})
    assert wait_for(lambda: _seen(pm, "Internet Restored")), pm.delivered
    msg = _get(pm, "Internet Restored")
    assert "1m 43s" in msg, msg
    hist = pm._load_history()
    assert len(hist) == 1 and hist[0]["cause"] == "wan", hist


def test_restored_never_crashes_on_bad_data(pm):
    _, fmt = pm.EVENT_TAXONOMY["mains_restored"]
    assert "Power Restored" in fmt({"data": ""})
    assert "Power Restored" in fmt({})


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


def test_confirmations_deliver_immediately(pm, wait_for):
    """Offline/online confirmations are critical: immediate delivery, never
    parked in the 90s info coalescer."""
    pm._esp32_state = {"mainsDelayMs": 300000}
    pm._pve_probe = lambda: (False, None, "Offline")
    pm.pve_verify(expect_up=False, label="shutdown_mains_start",
                  timeout_sec=5, interval=0.05)
    assert wait_for(lambda: any("Confirmed Offline" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    with pm._notify_lock:
        assert pm._info_pending is None, "confirmation must not coalesce"


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
