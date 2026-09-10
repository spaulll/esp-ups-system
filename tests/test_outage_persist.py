"""Outage timing survives a Pi restart (outage.json next to last-seq.json)."""
import os
import time


def test_outage_start_persisted_and_reloaded(pm, wait_for):
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=5"})
    assert wait_for(lambda: any("Utility Power Lost" in d[1]
                                for d in pm.delivered if d[0] == "tg")), pm.delivered
    assert os.path.exists(pm.OUTAGE_FILE), "mains_down must persist outage.json"
    # backdate so the later consume yields a real span, then re-persist
    pm._last_mains_down_at = time.time() - 100
    pm._save_outage()
    # simulate restart: wipe memory, reload from disk
    pm._last_mains_down_at = None
    pm._last_mains_downtime_sec = None
    pm._load_outage()
    assert pm._last_mains_down_at is not None, "down_at lost across restart"
    assert time.time() - pm._last_mains_down_at >= 90
    # consume still works after reload and clears persisted state
    assert "Power was out" in pm._consume_outage_line()
    pm._load_outage()
    assert pm._last_mains_down_at is None
    assert pm._last_mains_downtime_sec is None


def test_load_outage_tolerates_garbage(pm):
    with open(pm.OUTAGE_FILE, "w") as f:
        f.write("{not json")
    pm._load_outage()   # must not raise
    assert pm._last_mains_down_at is None
    with open(pm.OUTAGE_FILE, "w") as f:
        f.write('{"down_at": 9999999999, "downtime_sec": -5}')
    pm._load_outage()
    assert pm._last_mains_down_at is None, "future stamp must be rejected"
    assert pm._last_mains_downtime_sec is None, "negative span must be rejected"
