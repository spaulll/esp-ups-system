"""The missed-notifications ledger is readable via /missed."""
import time


def test_missed_empty(pm):
    assert "No missed" in pm.handle_command("/missed", None)


def test_missed_shows_appended_newest_first(pm):
    pm._append_missed({"at": time.time() - 60, "text": "🔴 first alert"})
    pm._append_missed({"at": time.time(), "text": "🔴 second alert"})
    reply = pm.handle_command("/missed", None)
    assert "first alert" in reply
    assert "second alert" in reply
    assert reply.index("second alert") < reply.index("first alert")


def test_missed_in_menu_and_help(pm):
    assert any(c["command"] == "missed" for c in pm.TG_COMMANDS)
    assert "/missed" in pm.handle_command("/bogus", None)
