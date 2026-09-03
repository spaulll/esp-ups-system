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
    _stub_esp_command(pm, ok=True)
    assert "Wake commanded" in pm.handle_command("/on", None)
    _stub_esp_command(pm, ok=False)
    assert "unreachable" in pm.handle_command("/on", None)


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