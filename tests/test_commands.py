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
    assert "Set" in pm.handle_command("/mainsdelay", "1")
    assert "Set" in pm.handle_command("/mainsdelay", "720")
    assert "range" in pm.handle_command("/mainsdelay", "0")
    assert "range" in pm.handle_command("/mainsdelay", "721")
    assert "number" in pm.handle_command("/mainsdelay", "abc")


def test_mainsdelay_reset(pm):
    _stub_esp_command(pm, ok=True)
    reply = pm.handle_command("/mainsdelay", "reset")
    assert "Set" in reply


def test_wantimeout_bounds(pm):
    _stub_esp_command(pm, ok=True)
    assert "Set" in pm.handle_command("/wantimeout", "5")
    assert "Set" in pm.handle_command("/wantimeout", "120")
    assert "range" in pm.handle_command("/wantimeout", "4")
    assert "range" in pm.handle_command("/wantimeout", "121")
    assert "number" in pm.handle_command("/wantimeout", "abc")


def test_wantimeout_reset(pm):
    _stub_esp_command(pm, ok=True)
    reply = pm.handle_command("/wantimeout", "reset")
    assert "Set" in reply


def test_status_and_diag_return_strings(pm):
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.0"}
    assert isinstance(pm.cmd_status(), str)
    assert isinstance(pm.cmd_diag(), str)
    assert "Mains" in pm.cmd_status()
    assert "V7.0" in pm.cmd_diag()