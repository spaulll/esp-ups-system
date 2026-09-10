"""Persisted last-10 outage log, readable via /history."""


def test_history_empty(pm):
    assert "No outages" in pm.handle_command("/history", None)


def test_history_full_cycle_single_entry(pm, wait_for):
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=5"})
    assert wait_for(lambda: any("Utility Power Lost" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    pm.process_event("shutdown_mains_start", 2, {"event": "shutdown_mains_start"})
    assert wait_for(lambda: any("Shutting Down" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    pm.process_event("mains_restored", 3,
                     {"event": "mains_restored", "data": "downtimeMs=125000"})
    assert wait_for(lambda: any("Power Restored" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    # down + shutdown folded into ONE entry, closed by the restore
    assert len(pm._load_history()) == 1
    reply = pm.handle_command("/history", None)
    assert "power" in reply and "shutdown" in reply, reply
    assert "2m 5s" in reply, reply
    assert "ongoing" not in reply, reply


def test_history_manual_shutdown_without_prior_down(pm, wait_for):
    pm.process_event("shutdown_manual_start", 1, {"event": "shutdown_manual_start"})
    assert wait_for(lambda: any("Manual" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    reply = pm.handle_command("/history", None)
    assert "manual" in reply and "shutdown" in reply, reply
    assert "ongoing" in reply, reply


def test_online_consume_closes_history(pm, wait_for):
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=5"})
    assert wait_for(lambda: any("Utility Power Lost" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    import time as _t
    pm._last_mains_down_at = _t.time() - 100
    pm._save_outage()
    assert "Power was out" in pm._consume_outage_line()
    reply = pm.handle_command("/history", None)
    assert "ongoing" not in reply, reply
    assert "1m" in reply, reply


def test_history_caps_at_ten(pm):
    for i in range(12):
        pm._history_open("mains", shutdown=bool(i % 2))
        pm._history_close(60 + i)
    assert len(pm._load_history()) == 10
    assert "Outage History" in pm.handle_command("/history", None)


def test_history_in_menu_and_help(pm):
    assert any(c["command"] == "history" for c in pm.TG_COMMANDS)
    assert "/history" in pm.handle_command("/bogus", None)
