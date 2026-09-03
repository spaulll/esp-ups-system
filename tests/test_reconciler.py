"""Phase 4: reconciler seq logic.

Exactly-once processing, gap detection, first-run seeding, sensor_blind.
"""
import os
import time

from conftest import stub_esp


def test_processes_unseen_events_exactly_once(pm):
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 2},
             events=[{"seq": 1, "event": "esp_booted", "uptimeMs": 0, "data": "poweron"},
                     {"seq": 2, "event": "mains_blip", "uptimeMs": 0, "data": "1x"}])
    pm.reconcile_once()
    assert pm._last_seq == 2
    first_count = len(pm.delivered)
    # second pass with same data -> no re-fire
    pm.reconcile_once()
    assert len(pm.delivered) == first_count
    assert pm._last_seq == 2


def test_seq_gap_triggers_event_log_gap_warning(pm):
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 10},
             events=[{"seq": 10, "event": "mains_down", "uptimeMs": 0, "data": "mins=5"}])
    pm.reconcile_once()
    assert pm._last_seq == 10
    gap = [d for d in pm.delivered if d[0] == "tg" and "Event Log Gap" in d[1]]
    assert gap, f"expected gap warning, delivered={pm.delivered}"


def test_first_run_seeds_seq_without_replaying_history(pm):
    if os.path.exists(pm.SEQ_FILE):
        os.remove(pm.SEQ_FILE)
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 42},
             events=[{"seq": 42, "event": "esp_booted", "uptimeMs": 0, "data": "poweron"}])
    pm.reconcile_once()
    assert pm._last_seq == 42
    assert pm.delivered == [], "must not replay ring history on first run"


def test_sensor_blind_after_dead_threshold(pm):
    stub_esp(pm, state=None)
    pm.reconcile_once()                     # first failure: record, no alert
    assert pm.delivered == []
    pm._sensor_dead_since = time.time() - pm.SENSOR_DEAD_SEC - 1
    pm.reconcile_once()
    blind = [d for d in pm.delivered if d[0] == "tg" and "Sensor Blind" in d[1]]
    assert blind, f"expected Sensor Blind, delivered={pm.delivered}"


def test_sensor_back_notifies_on_recovery(pm):
    pm._sensor_dead_since = time.time() - pm.SENSOR_DEAD_SEC - 1
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "seq": 1})
    pm.reconcile_once()
    back = [d for d in pm.delivered if d[0] == "tg" and "Sensor Back" in d[1]]
    assert back, f"expected Sensor Back, delivered={pm.delivered}"