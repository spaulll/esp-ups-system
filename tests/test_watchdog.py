"""Watchdog withholds its systemd heartbeat while any loop is stale."""
import time


def test_watchdog_all_fresh(pm):
    now = time.time()
    with pm._hb_lock:
        for t in ("reconciler", "telegram", "notify", "countdown"):
            pm._heartbeats[t] = now
    assert pm._watchdog_stale(now=now) == []


def test_watchdog_flags_stale_thread(pm):
    now = time.time()
    with pm._hb_lock:
        pm._heartbeats.update({"reconciler": now, "telegram": now - 300,
                               "notify": now, "countdown": now})
    assert pm._watchdog_stale(now=now) == ["telegram"]


def test_beat_refreshes(pm):
    pm._beat("telegram")
    stale = pm._watchdog_stale()
    assert "telegram" not in stale
    assert "reconciler" in stale  # nothing beats it in the test env
