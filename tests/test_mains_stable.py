"""Mains last-change persistence (Pi-side wall-clock stamp).

The ESP's own age is RAM-only and resets on every reboot/OTA, so /diag's
"last change" is stamped here from confirmed transition events and healed
from ESP polls. These tests pin: event stamping, blip exclusion, survival
across ESP reboots, first-run seeding, gap healing, and reload.
"""
import os
import time


def _diag(pm):
    pm._pve_probe = lambda: (True, 100, "1m")
    return pm.cmd_diag()


def test_mains_down_stamps_wall_clock(pm):
    assert pm._mains_last_change is None
    before = time.time()
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=10"})
    after = time.time()
    assert pm._mains_last_change is not None
    assert before - 1 <= pm._mains_last_change <= after + 1
    assert os.path.exists(pm.MAINS_STABLE_FILE)


def test_mains_restored_updates_stamp(pm):
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=10"})
    first = pm._mains_last_change
    time.sleep(0.05)
    pm.process_event("mains_restored", 2,
                     {"event": "mains_restored", "data": "downtimeMs=66000"})
    assert pm._mains_last_change >= first


def test_blip_does_not_stamp(pm):
    pm.process_event("mains_blip", 1, {"event": "mains_blip", "data": "1x"})
    assert pm._mains_last_change is None


def test_diag_survives_esp_reboot(pm):
    # real transition 1h ago; ESP rebooted 20s ago (age ~= uptime)
    pm._mains_last_change = time.time() - 3600
    pm._save_mains_stable()
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.5",
                       "mainsStableSinceMs": 20000, "espUptimeMs": 20000,
                       "espResetReason": "software", "rssi": -30,
                       "mainsDelayMs": 600000, "wanTimeoutMs": 600000,
                       "seq": 200}
    pm._last_seq = 200
    text = _diag(pm)
    assert "1h 0m" in text, text


def test_diag_seeds_from_esp_when_blank(pm):
    # fresh install, no stamp yet: seed from the ESP poll, not "unknown"
    pm._track_mains_stable_from_esp(
        {"mainsStableSinceMs": 900000, "espUptimeMs": 925000})
    assert pm._mains_last_change is not None
    pm._esp32_state = {"mainsUp": True, "wanUp": True, "fw": "V7.5",
                       "mainsStableSinceMs": 900000, "espUptimeMs": 925000,
                       "espResetReason": "software", "rssi": -30,
                       "mainsDelayMs": 600000, "wanTimeoutMs": 600000,
                       "seq": 200}
    pm._last_seq = 200
    text = _diag(pm)
    assert "15m" in text, text


def test_heal_ignores_post_boot_age(pm):
    # ESP OTA'd 20s ago; the 1h-old transition must NOT be overwritten
    pm._mains_last_change = time.time() - 3600
    pm._track_mains_stable_from_esp(
        {"mainsStableSinceMs": 20000, "espUptimeMs": 20000})
    assert abs(pm._mains_last_change - (time.time() - 3600)) < 5


def test_heal_adopts_missed_transition(pm):
    # gap dropped the events, but the ESP witnessed a transition 60s ago
    pm._mains_last_change = time.time() - 3600
    pm._track_mains_stable_from_esp(
        {"mainsStableSinceMs": 60000, "espUptimeMs": 925000})
    assert abs(pm._mains_last_change - (time.time() - 60)) < 10


def test_reload_restores_stamp(pm):
    pm.process_event("mains_down", 1, {"event": "mains_down", "data": "mins=10"})
    saved = pm._mains_last_change
    pm._mains_last_change = None
    pm._mains_last_src = None
    pm._load_mains_stable()
    assert pm._mains_last_change == saved


def _seed_history(pm, entries):
    import json
    with open(pm.HISTORY_FILE, "w") as f:
        json.dump(entries, f)


def test_backfill_uses_newest_mains_end(pm):
    now = time.time()
    _seed_history(pm, [
        {"start": now - 15000, "end": now - 14600,
         "cause": "mains", "shutdown": False, "downtime_sec": 341},
        {"start": now - 3000, "end": now - 2934,
         "cause": "mains", "shutdown": False, "downtime_sec": 66},
        {"start": now - 600, "end": now - 586,
         "cause": "wan", "shutdown": False, "downtime_sec": 14},
    ])
    pm._load_mains_stable()
    assert pm._mains_last_change is not None
    assert abs(pm._mains_last_change - (now - 2934)) < 5
    assert pm._mains_last_src == "history"


def test_backfill_open_entry_uses_start(pm):
    now = time.time()
    _seed_history(pm, [
        {"start": now - 120, "end": None,
         "cause": "mains", "shutdown": False, "downtime_sec": None},
    ])
    pm._load_mains_stable()
    assert abs(pm._mains_last_change - (now - 120)) < 5


def test_backfill_skips_non_mains(pm):
    now = time.time()
    _seed_history(pm, [
        {"start": now - 600, "end": now - 586,
         "cause": "wan", "shutdown": False, "downtime_sec": 14},
    ])
    pm._load_mains_stable()
    assert pm._mains_last_change is None


def test_history_upgrades_seeded_guess(pm):
    # yesterday's deploy seeded from boot age; history holds the truth
    now = time.time()
    _seed_history(pm, [
        {"start": now - 4000, "end": now - 3600,
         "cause": "mains", "shutdown": False, "downtime_sec": 219},
    ])
    import json
    with open(pm.MAINS_STABLE_FILE, "w") as f:
        json.dump({"at": now - 60}, f)  # old format, no src -> seed
    pm._load_mains_stable()
    assert abs(pm._mains_last_change - (now - 3600)) < 5
    assert pm._mains_last_src == "history"


def test_event_stamp_beats_history(pm):
    now = time.time()
    _seed_history(pm, [
        {"start": now - 4000, "end": now - 3600,
         "cause": "mains", "shutdown": False, "downtime_sec": 219},
    ])
    import json
    with open(pm.MAINS_STABLE_FILE, "w") as f:
        json.dump({"at": now - 60, "src": "event"}, f)
    pm._load_mains_stable()
    assert abs(pm._mains_last_change - (now - 60)) < 5
