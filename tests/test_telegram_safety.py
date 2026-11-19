"""Telegram message safety & display rollover fixes (Task 12).

- truncation for Telegram's 4096-char limit
- HTML parse-mode escaping (no stray < > & from device data)
- optional owner-only command gate
- daily counters roll over at midnight for display
"""
import time


def test_truncate_tg_short_unaffected(pm):
    assert pm._truncate_tg("hi") == "hi"


def test_truncate_tg_long_truncates(pm):
    long_text = "x" * 5000
    out = pm._truncate_tg(long_text)
    assert len(out) <= 4000
    assert out.endswith("…")


def test_sanitize_keeps_own_tags_escapes_others(pm):
    text = "<b>Mains</b> down <script>alert(1)</script> & more"
    out = pm._tg_sanitize(text)
    assert "<b>Mains</b>" in out
    assert "&lt;script&gt;" in out
    assert "&amp; more" in out
    assert "<script>" not in out


def test_sanitize_escapes_device_data(pm):
    text = "Reason: <unknown> · 100%"
    out = pm._tg_sanitize(text)
    assert "&lt;unknown&gt;" in out


def test_sender_allowed_any_when_no_owner(pm):
    pm._tg_owner_id_saved = getattr(pm, "TG_OWNER_ID", "")
    pm.TG_OWNER_ID = ""
    try:
        assert pm._sender_allowed({"from": {"id": 999}}) is True
        assert pm._sender_allowed({}) is True
    finally:
        pm.TG_OWNER_ID = pm._tg_owner_id_saved


def test_sender_allowed_only_owner_when_set(pm):
    pm._tg_owner_id_saved = getattr(pm, "TG_OWNER_ID", "")
    pm.TG_OWNER_ID = "123"
    try:
        assert pm._sender_allowed({"from": {"id": 123}}) is True
        assert pm._sender_allowed({"from": {"id": 999}}) is False
        assert pm._sender_allowed({}) is False
    finally:
        pm.TG_OWNER_ID = pm._tg_owner_id_saved


def test_counters_roll_over_for_display_at_midnight(pm):
    pm._daily_counters = {"date": "2000-01-01", "mains_down": 9,
                          "shutdowns": 3, "blips": 5}
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.0"}
    text = pm.cmd_status()
    assert "0× power loss" in text, text
    assert "9×" not in text, text
    diag = pm.cmd_diag()
    assert "0× power loss" in diag, diag
