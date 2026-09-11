"""WAN double-check handshake: ESP suspect -> Pi probes -> confirm/clear.

Locked rules under test:
  - suspect alone never alerts (early alert fires on CONFIRM only)
  - both Pi probes fail -> wan_confirm command + critical alert + history
  - any probe succeeds -> wan_clear command, log only (no alert, no history)
  - ESP-confirmed wan_down without a seen suspect (Pi was deaf) still alerts
  - duplicate wan_down after a confirm is dropped (no double alert)
  - re-suspect after a confirm quietly re-arms the ESP (no second alert)
  - wan_cleared resets state silently
"""
import time


def _stub_probes(pm, ip_ok, web_ok):
    pm._wan_probe_ip = lambda timeout=5: ip_ok
    pm._wan_probe_web = lambda timeout=5: web_ok


def _stub_esp_cmd(pm):
    sent = []
    pm._esp_command = lambda d: (sent.append(d) or True)
    return sent


def _wait_tg(pm, wait_for, needle, timeout=3.0):
    """Wait until a TG delivery containing needle lands (not just queued)."""
    assert wait_for(
        lambda: any(d[0] == "tg" and needle in d[1] for d in pm.delivered),
        timeout=timeout), pm.delivered
    return next(d[1] for d in pm.delivered if d[0] == "tg" and needle in d[1])


def test_suspect_has_no_taxonomy_entry(pm):
    """Suspect must be unrenderable: no taxonomy entry means no code path
    can turn a bare suspect into a user-visible alert by accident."""
    assert "wan_suspect" not in pm.EVENT_TAXONOMY
    assert "wan_cleared" not in pm.EVENT_TAXONOMY


def test_suspect_both_fail_confirms_and_alerts(pm, wait_for):
    _stub_probes(pm, False, False)
    sent = _stub_esp_cmd(pm)
    pm._esp32_state = {"wanTimeoutMs": 600000}
    pm.process_event("wan_suspect", 1, {"event": "wan_suspect"})
    assert sent == [{"cmd": "wan_confirm"}], sent
    msg = _wait_tg(pm, wait_for, "Internet Down")
    assert "Confirmed" in msg, msg
    assert "~10 minutes" in msg, msg
    assert pm._wan_confirmed is True
    hist = pm._load_history()
    assert len(hist) == 1 and hist[0]["cause"] == "wan" and hist[0]["end"] is None, hist


def test_suspect_any_probe_ok_clears_silently(pm):
    for ip_ok, web_ok in ((True, False), (False, True), (True, True)):
        pm._wan_confirmed = False
        pm.delivered.clear()
        with pm._notify_lock:
            pm._notify_queue.clear()
            pm._info_pending = None
        try:
            open(pm.HISTORY_FILE, "w").write("[]")
        except OSError:
            pass
        _stub_probes(pm, ip_ok, web_ok)
        sent = _stub_esp_cmd(pm)
        pm.process_event("wan_suspect", 1, {"event": "wan_suspect"})
        assert sent == [{"cmd": "wan_clear"}], (ip_ok, web_ok, sent)
        time.sleep(0.3)
        assert pm.delivered == [], (ip_ok, web_ok, pm.delivered)
        with pm._notify_lock:
            assert pm._info_pending is None
        assert pm._load_history() == [], "false alarm must leave no history"
        assert pm._wan_confirmed is False


def test_wan_down_without_suspect_still_alerts(pm, wait_for):
    """ESP auto-confirmed while this Pi was deaf: adopt + alert (late > never)."""
    sent = _stub_esp_cmd(pm)
    pm._esp32_state = {"wanTimeoutMs": 600000}
    pm.process_event("wan_down", 5, {"event": "wan_down", "data": "src=auto"})
    assert sent == [], "nothing to command — ESP already latched"
    _wait_tg(pm, wait_for, "Internet Down")
    assert pm._wan_confirmed is True
    hist = pm._load_history()
    assert len(hist) == 1 and hist[0]["cause"] == "wan", hist


def test_duplicate_wan_down_dropped(pm, wait_for):
    _stub_probes(pm, False, False)
    _stub_esp_cmd(pm)
    pm._esp32_state = {"wanTimeoutMs": 600000}
    pm.process_event("wan_suspect", 1, {"event": "wan_suspect"})
    _wait_tg(pm, wait_for, "Internet Down")
    n = len(pm.delivered)
    pm.process_event("wan_down", 2, {"event": "wan_down", "data": "src=pi"})
    time.sleep(0.3)
    assert len(pm.delivered) == n, f"duplicate wan_down re-alerted: {pm.delivered[n:]}"


def test_resuspect_after_confirm_rearms_quietly(pm, wait_for):
    """ESP reboot loses its latch and re-suspects: re-confirm, no 2nd alert."""
    _stub_probes(pm, False, False)
    sent = _stub_esp_cmd(pm)
    pm._esp32_state = {"wanTimeoutMs": 600000}
    pm.process_event("wan_suspect", 1, {"event": "wan_suspect"})
    _wait_tg(pm, wait_for, "Internet Down")
    n = len(pm.delivered)
    sent.clear()
    pm.process_event("wan_suspect", 9, {"event": "wan_suspect"})
    assert sent == [{"cmd": "wan_confirm"}], sent
    time.sleep(0.3)
    assert len(pm.delivered) == n, "re-suspect must not re-notify"


def test_wan_cleared_resets_silently(pm):
    pm._wan_confirmed = True
    pm.process_event("wan_cleared", 3, {"event": "wan_cleared", "data": "src=self"})
    time.sleep(0.2)
    assert pm.delivered == []
    assert pm._wan_confirmed is False


def test_wan_down_taxonomy_is_critical(pm):
    klass, fmt = pm.EVENT_TAXONOMY["wan_down"]
    assert klass == "critical", f"confirmed WAN-down must be immediate, got {klass}"
    pm._esp32_state = {"wanTimeoutMs": 600000}
    msg = fmt({})
    assert "Internet Down" in msg and "shut down" in msg, msg


def test_confirm_uses_live_wantimeout(pm):
    pm._esp32_state = {"wanTimeoutMs": 5 * 60000}
    _, fmt = pm.EVENT_TAXONOMY["wan_down"]
    assert "~5 minutes" in fmt({}), fmt({})
    pm._esp32_state = {}
    assert "minutes" in fmt({}), fmt({})


def test_wan_restored_clears_confirm_flag(pm, wait_for):
    """After a restore the next suspect is a new outage: full alert again."""
    _stub_probes(pm, False, False)
    _stub_esp_cmd(pm)
    pm._esp32_state = {"wanTimeoutMs": 600000}
    pm.process_event("wan_suspect", 1, {"event": "wan_suspect"})
    _wait_tg(pm, wait_for, "Internet Down")
    pm.process_event("wan_restored", 2,
                     {"event": "wan_restored", "data": "downtimeMs=125000"})
    assert pm._wan_confirmed is False
    before = sum(1 for d in pm.delivered if d[0] == "tg" and "Internet Down" in d[1])
    pm.process_event("wan_suspect", 3, {"event": "wan_suspect"})
    assert wait_for(
        lambda: sum(1 for d in pm.delivered
                    if d[0] == "tg" and "Internet Down" in d[1]) > before,
        timeout=3.0), pm.delivered
