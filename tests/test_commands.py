"""Phase 4: Telegram command parsing and bounds checking.

Commands: /status /diag /on /off /mainsdelay [1-720|reset] /wantimeout [5-120|reset]
Unknown commands return a friendly list.
"""
import pytest


def _stub_esp_command(pm, ok=True):
    pm._esp_command = lambda cmd_dict: ok


def test_unknown_command_returns_friendly_list(pm):
    reply = pm.handle_command("/blah", None)
    assert "/status" in reply
    assert "/diag" in reply
    assert "/mainsdelay" in reply


def test_on_off_commands(pm):
    pm._pve_probe = lambda: (False, None, "Offline")
    _stub_esp_command(pm, ok=True)
    assert "Wake commanded" in pm.handle_command("/on", None)
    _stub_esp_command(pm, ok=False)
    assert "unreachable" in pm.handle_command("/on", None)


def test_on_when_already_up_says_nothing_to_wake(pm):
    """/on when Proxmox is actually up should NOT claim 'wake commanded'."""
    pm._esp32_state = {"mainsUp": True, "wanUp": True,
                       "sdMains": False, "sdWAN": False, "sdManual": False,
                       "manualOverride": False,
                       "mainsFailSinceMs": 0, "wanFailSinceMs": 0}
    pm._pve_probe = lambda: (True, 100, "1m")
    sent = []
    pm._esp_command = lambda cmd: sent.append(cmd) or True
    reply = pm.handle_command("/on", None)
    assert "already up" in reply, reply
    assert sent == [], f"wake should not be sent when node is healthy: {sent}"


def test_on_when_prox_offline_but_no_esp_flags_sends_wake(pm):
    """Regression: node shut down outside the system (manual power-off).

    ESP flags stay clear, but /status shows OFFLINE via the PVE API. /on
    must wake — never answer 'already up' while Proxmox is down."""
    pm._esp32_state = {"mainsUp": True, "wanUp": True,
                       "sdMains": False, "sdWAN": False, "sdManual": False,
                       "manualOverride": False,
                       "mainsFailSinceMs": 0, "wanFailSinceMs": 0}
    pm._pve_probe = lambda: (False, None, "Offline")
    sent = []
    pm._esp_command = lambda cmd: sent.append(cmd) or True
    reply = pm.handle_command("/on", None)
    assert "Wake commanded" in reply, reply
    assert sent == [{"cmd": "wake"}], sent


def test_on_when_countdown_running_sends_wake(pm):
    """/on during a mains countdown must send wake (sets manual override),
    even though Proxmox is still (briefly) online."""
    pm._esp32_state = {"mainsUp": False, "wanUp": True,
                       "sdMains": False, "sdWAN": False, "sdManual": False,
                       "manualOverride": False,
                       "mainsFailSinceMs": 120000, "wanFailSinceMs": 0}
    pm._pve_probe = lambda: (True, 100, "1m")
    sent = []
    pm._esp_command = lambda cmd: sent.append(cmd) or True
    reply = pm.handle_command("/on", None)
    assert "Wake commanded" in reply, reply
    assert sent == [{"cmd": "wake"}], sent


def test_off_when_already_down_says_so(pm):
    """/off when Proxmox is actually down should not send shutdown again."""
    pm._esp32_state = {"sdMains": True, "sdWAN": False, "sdManual": False}
    pm._pve_probe = lambda: (False, None, "Offline")
    sent = []
    pm._esp_command = lambda cmd: sent.append(cmd) or True
    reply = pm.handle_command("/off", None)
    assert "already down" in reply, reply
    assert sent == [], f"shutdown should not be sent when node is down: {sent}"


def test_off_when_prox_offline_but_no_esp_flags_says_already_down(pm):
    """Mirror case: node stopped externally — /off reports already down
    instead of firing a pointless shutdown."""
    pm._esp32_state = {"mainsUp": True, "wanUp": True,
                       "sdMains": False, "sdWAN": False, "sdManual": False}
    pm._pve_probe = lambda: (False, None, "Offline")
    sent = []
    pm._esp_command = lambda cmd: sent.append(cmd) or True
    reply = pm.handle_command("/off", None)
    assert "already down" in reply, reply
    assert sent == [], sent


def test_off_when_prox_online_but_flags_stale_sends_shutdown(pm):
    """Shutdown webhook never acked (flags set, node still up) — /off must
    retry instead of trusting the stale flags."""
    pm._esp32_state = {"sdMains": True, "sdWAN": False, "sdManual": False}
    pm._pve_probe = lambda: (True, 100, "1m")
    sent = []
    pm._esp_command = lambda cmd: sent.append(cmd) or True
    reply = pm.handle_command("/off", None)
    assert "Shutdown commanded" in reply, reply
    assert sent == [{"cmd": "shutdown"}], sent


def test_mainsdelay_bounds(pm):
    _stub_esp_command(pm, ok=True)
    # valid: waiting message sent, no plain-text reply
    assert pm.handle_command("/mainsdelay", "1") is None
    assert pm.handle_command("/mainsdelay", "720") is None
    assert "range" in pm.handle_command("/mainsdelay", "0")
    assert "range" in pm.handle_command("/mainsdelay", "721")
    assert "number" in pm.handle_command("/mainsdelay", "abc")


def test_mainsdelay_reset(pm):
    _stub_esp_command(pm, ok=True)
    assert pm.handle_command("/mainsdelay", "reset") is None


def test_wantimeout_bounds(pm):
    _stub_esp_command(pm, ok=True)
    assert pm.handle_command("/wantimeout", "5") is None
    assert pm.handle_command("/wantimeout", "120") is None
    assert "range" in pm.handle_command("/wantimeout", "4")
    assert "range" in pm.handle_command("/wantimeout", "121")
    assert "number" in pm.handle_command("/wantimeout", "abc")


def test_wantimeout_reset(pm):
    _stub_esp_command(pm, ok=True)
    assert pm.handle_command("/wantimeout", "reset") is None


def test_delay_set_edits_waiting_message_on_confirm(pm):
    _stub_esp_command(pm, ok=True)
    # user sends /mainsdelay 12 -> a "waiting" message is sent, pending tracked
    assert pm.handle_command("/mainsdelay", "12") is None
    with pm._pending_conf_lock:
        conf = pm._pending_conf.get("mainsdelay")
    assert conf, "pending confirmation not registered"
    # ESP confirms via its ledger event -> the SAME message gets edited
    pm.process_event("mains_delay_set", 50, {"event": "mains_delay_set", "data": "12min"})
    edits = [c for c in pm._tg_edit_msg_calls if c[0] == "edit"]
    assert edits, "expected the waiting message to be edited"
    assert "12 min" in edits[-1][2]
    with pm._pending_conf_lock:
        assert "mainsdelay" not in pm._pending_conf, "pending not cleared after confirm"


def test_delay_set_pending_registered_before_esp_call(pm):
    """The pending conf must exist before the ESP command fires, else a fast
    ack (webhook -> reconcile) races ahead into the info summary."""
    order = []
    def slow_esp(cmd_dict):
        with pm._pending_conf_lock:
            has_pending = "mainsdelay" in pm._pending_conf
        order.append(("esp_call", has_pending))
        return True
    pm._esp_command = slow_esp
    pm.handle_command("/mainsdelay", "10")
    assert order and order[0] == ("esp_call", True), \
        f"pending not registered before esp command: {order}"


def test_delay_set_without_pending_falls_through_to_summary(pm):
    # no pending command — the event is a normal info summary
    pm.process_event("mains_delay_set", 50, {"event": "mains_delay_set", "data": "12min"})
    assert pm._tg_edit_msg_calls == [], "should not edit without a pending message"


def test_status_and_diag_return_strings(pm):
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.0"}
    assert isinstance(pm.cmd_status(), str)
    assert isinstance(pm.cmd_diag(), str)
    assert "Mains" in pm.cmd_status()
    assert "V7.0" in pm.cmd_diag()


def test_status_warns_when_gpio_test_override_active(pm):
    """A stuck set_gpio_test override blinds mains sensing — /status must say so."""
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.0",
                       "gpioTestOverride": 0}
    assert "TEST MODE" in pm.cmd_status()


def test_status_quiet_when_live_sensing(pm):
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.0",
                       "gpioTestOverride": -1}
    assert "TEST MODE" not in pm.cmd_status()


def test_diag_shows_gpio_test_override(pm):
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.0",
                       "gpioTestOverride": 1}
    assert "OVERRIDE=1" in pm.cmd_diag()
    pm._esp32_state["gpioTestOverride"] = -1
    assert "OVERRIDE" not in pm.cmd_diag()