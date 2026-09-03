"""Phase 4: event classification per Appendix A.

Every event the ESP32 emits must resolve to (class, format) in the single
authority table, and each class must route correctly:
  critical -> immediate + ntfy urgent
  warning  -> immediate
  info     -> coalesced
"""
import time


def _wait_for(pm, predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        pm._notify_worker_wake()
        time.sleep(0.05)
    return predicate()


def test_taxonomy_covers_all_appendix_a_events(pm):
    """Appendix A lists these — all must be in EVENT_TAXONOMY."""
    required = {
        "esp_booted", "mains_blip", "mains_down", "mains_restored",
        "shutdown_mains_start", "shutdown_wan_start", "shutdown_complete",
        "wake_sequence_start", "wol_rexmitted", "wake_failed",
    }
    missing = required - set(pm.EVENT_TAXONOMY)
    assert not missing, f"Appendix A events missing from taxonomy: {missing}"


def test_unknown_event_is_logged_not_delivered(pm):
    pm.process_event("not_a_real_event", 99, {})
    assert pm.delivered == []


def test_critical_delivers_immediately(pm):
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=5"})
    assert _wait_for(pm, lambda: any(d[0] == "tg" for d in pm.delivered)), pm.delivered


def test_critical_uses_urgent_for_ntfy(pm):
    pm.tg_fail = True
    pm.process_event("wake_failed", 2, {"event": "wake_failed"})
    assert _wait_for(pm, lambda: any(d[0] == "ntfy" for d in pm.delivered)), pm.delivered
    ntfy = [d for d in pm.delivered if d[0] == "ntfy"]
    assert ntfy[0][2] is True, "wake_failed is critical -> urgent"


def test_info_is_coalesced_not_immediate(pm):
    pm.process_event("mains_blip", 1, {"event": "mains_blip", "data": "1x"})
    time.sleep(0.3)
    # blip is info -> must NOT have delivered on its own
    assert pm.delivered == []


def test_info_batch_flushes_as_one_summary(pm):
    pm.process_event("esp_booted", 1, {"event": "esp_booted", "data": "poweron"})
    pm.process_event("mains_blip", 2, {"event": "mains_blip", "data": "1x"})
    time.sleep(0.3)
    assert pm.delivered == []

    with pm._notify_lock:
        if pm._info_pending:
            pm._info_pending["notify_at"] = time.time() - 1
    assert _wait_for(pm, lambda: any("Info Summary" in d[1] for d in pm.delivered)), pm.delivered
    tg = [d for d in pm.delivered if d[0] == "tg"]
    assert len(tg) == 1, f"expected exactly one summary, got {pm.delivered}"


def test_bump_counters_only_for_matching_events(pm):
    pm._daily_counters = {"date": pm._today(), "mains_down": 0, "shutdowns": 0, "blips": 0}
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=5"})
    pm.process_event("mains_blip", 2, {"event": "mains_blip", "data": "1x"})
    pm.process_event("shutdown_mains_start", 3, {"event": "shutdown_mains_start"})
    assert pm._daily_counters["mains_down"] == 1
    assert pm._daily_counters["blips"] == 1
    assert pm._daily_counters["shutdowns"] == 1


def test_restored_format_millis_to_seconds(pm):
    _, fmt = pm.EVENT_TAXONOMY["mains_restored"]
    msg = fmt({"data": "downtimeMs=123000"})
    assert "2m 3s" in msg, msg
