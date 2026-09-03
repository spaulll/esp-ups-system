"""Phase 4: adaptive live-countdown cadence.

The countdown card must be a single edited message with cadence that is fast
at the start (5s), slows through the middle, and returns to 5s in the final
2 minutes.
"""
from conftest import stub_esp
import time


def test_interval_fast_at_start(pm):
    # elapsed 30s into a 10-min delay -> first minute, 5s cadence
    assert pm._cd_interval(30_000, 600_000) == 5


def test_interval_slows_in_middle(pm):
    # middle of a 10-min delay (5 min elapsed) -> slower than 5s
    mid = pm._cd_interval(300_000, 600_000)
    assert mid > 5
    # and it never gets absurdly slow
    assert mid <= 45


def test_interval_fast_in_final_two_minutes(pm):
    # 8m50s elapsed of a 10-min delay -> 70s remain (<2min) -> 5s
    assert pm._cd_interval(530_000, 600_000) == 5


def test_interval_monotonic_through_middle(pm):
    # cadence should grow (get slower) as we move through the middle zone
    earlier = pm._cd_interval(90_000, 600_000)
    later = pm._cd_interval(300_000, 600_000)
    assert later >= earlier


def test_status_live_edits_during_countdown(pm):
    """/status during a countdown sends once, then the reply is edited live."""
    stub_esp(pm, state={"mainsFailSinceMs": 30_000, "mainsDelayMs": 600_000,
                        "mainsUp": False, "wanUp": True})
    pm.reconcile_once()   # populate _esp32_state from stub
    pm._tg_send_msg_calls.clear()
    pm._tg_edit_msg_calls.clear()

    # /status while counting down -> sends once, registers for live edits
    assert pm.handle_command("/status", None) is None
    assert len(pm._tg_send_msg_calls) == 1
    with pm._lock:
        assert pm._status_msg_id is not None

    # next live tick -> edits the same reply, no second send
    pm._status_last_sent = 0
    pm._status_live_tick()
    assert len(pm._tg_send_msg_calls) == 1, "must not send a second /status"
    assert len(pm._tg_edit_msg_calls) == 1


def test_status_normal_single_reply_when_no_countdown(pm):
    """/status with no countdown is a normal one-shot reply."""
    stub_esp(pm, state={"mainsUp": True, "wanUp": True, "fw": "V7.0"})
    pm.reconcile_once()
    pm._tg_send_msg_calls.clear()
    reply = pm.handle_command("/status", None)
    assert reply is not None and "Mains" in reply
    assert pm._tg_send_msg_calls == [], "no live registration when mains is up"


def test_updater_sends_then_edits_single_message(pm):
    # simulate mains down so _build_countdown_card returns a card
    stub_esp(pm, state={"mainsFailSinceMs": 10_000, "mainsDelayMs": 600_000,
                        "mainsUp": False})
    pm.reconcile_once()   # populate _esp32_state from stub
    pm._tg_send_msg_calls.clear()
    pm._tg_edit_msg_calls.clear()
    pm._cd_msg_id = None
    pm._cd_last_sent = 0

    # first updater tick with an active countdown -> sends the card once
    pm._countdown_tick()
    sends = pm._tg_send_msg_calls
    assert len(sends) == 1, f"expected 1 send, got {len(sends)}"
    assert "Power down" in sends[0][1]

    # immediately tick again -> edits the SAME message, no second send
    pm._cd_last_sent = 0
    pm._countdown_tick()
    assert len(pm._tg_send_msg_calls) == 1, "must not send a second card"
    edits = pm._tg_edit_msg_calls
    assert len(edits) >= 1, "expected the card to be edited in place"
    assert edits[-1][1] == 42, "edit must target the sent message id"
