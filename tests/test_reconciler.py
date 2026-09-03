"""Phase 4: reconciler seq logic.

Exactly-once processing, gap detection, first-run seeding, sensor_blind.
"""
import os
import time

from conftest import stub_esp


def test_processes_unseen_events_exactly_once(pm):
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 2},
             events=[{"seq": 1, "event": "mains_down", "uptimeMs": 0, "data": "mins=5"},
                     {"seq": 2, "event": "mains_blip", "uptimeMs": 0, "data": "1x"}])
    pm.reconcile_once()
    assert pm._last_seq == 2
    # exactly-once: seq advanced; second pass does not re-process
    first_count = len(pm.delivered)
    pm.reconcile_once()
    assert len(pm.delivered) == first_count
    assert pm._last_seq == 2


def test_seq_gap_triggers_event_log_gap_warning(pm, wait_for):
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 10},
             events=[{"seq": 10, "event": "mains_down", "uptimeMs": 0, "data": "mins=5"}])
    pm.reconcile_once()
    assert pm._last_seq == 10
    assert wait_for(lambda: any(d[0] == "tg" and "Event Log Gap" in d[1] for d in pm.delivered)), pm.delivered


def test_first_run_seeds_seq_without_replaying_history(pm):
    if os.path.exists(pm.SEQ_FILE):
        os.remove(pm.SEQ_FILE)
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 42},
             events=[{"seq": 42, "event": "esp_booted", "uptimeMs": 0, "data": "poweron"}])
    pm.reconcile_once()
    assert pm._last_seq == 42
    time.sleep(0.3)
    assert pm.delivered == [], "must not replay ring history on first run"


def test_sensor_blind_after_dead_threshold(pm, wait_for):
    stub_esp(pm, state=None)
    pm.reconcile_once()                     # first failure: record, no alert
    assert pm.delivered == []
    pm._sensor_dead_since = time.time() - pm.SENSOR_DEAD_SEC - 1
    pm.reconcile_once()
    assert wait_for(lambda: any(d[0] == "tg" and "Sensor Blind" in d[1] for d in pm.delivered)), pm.delivered


def test_sensor_back_notifies_on_recovery(pm, wait_for):
    pm._sensor_dead_since = time.time() - pm.SENSOR_DEAD_SEC - 1
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 1})
    pm.reconcile_once()
    # info-class event may be drained from the queue into the coalescer by the
    # worker at any moment — accept either the queued event or the pending text
    def seen():
        with pm._notify_lock:
            in_queue = any(e.get("event") == "sensor_back" for e in pm._notify_queue)
            pending = pm._info_pending is not None and \
                any("Sensor Back" in t for t in pm._info_pending["events"])
        return in_queue or pending or any("Sensor Back" in d[1] for d in pm.delivered)
    assert wait_for(seen), f"sensor_back not enqueued: {pm._notify_queue}"
    # also verify the state was cleared
    assert pm._sensor_dead_since is None


def test_reconcile_stamps_esp_state_time(pm):
    """reconcile_once must update _esp_state_ts — the live countdown card and
    /status extrapolate from it; a stale 0.0 freezes them and makes every
    card edit a Telegram 400 (message not modified)."""
    pm._esp_state_ts = 0.0
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 0}, events=[])
    before = time.time()
    pm.reconcile_once()
    assert pm._esp_state_ts >= before, \
        f"_esp_state_ts not stamped (still {pm._esp_state_ts})"
