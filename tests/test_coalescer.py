"""Phase 4: notification coalescer logic.

Critical/warning bypass the coalescing window; info events within the window
are batched into exactly one summary; window expiry flushes the batch.
"""
import time


def _flush(pm, timeout=3.0):
    """Force the coalescing window to expire and wait for a NEW summary."""
    before = len(_summaries(pm))
    with pm._notify_lock:
        if pm._info_pending:
            pm._info_pending["notify_at"] = time.time() - 1
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(_summaries(pm)) > before:
            return
        pm._notify_worker_wake()
        time.sleep(0.05)
    raise AssertionError(f"no new Info Summary delivered: {pm.delivered}")


def _summaries(pm):
    return [d[1] for d in pm.delivered if d[0] == "tg" and "Info Summary" in d[1]]


def test_single_info_flushes_after_window(pm):
    pm._handle_info("blip one")
    assert pm.delivered == []
    _flush(pm)
    assert len(_summaries(pm)) == 1
    assert "blip one" in _summaries(pm)[0]


def test_multiple_info_single_summary(pm):
    pm._handle_info("blip one")
    pm._handle_info("blip two")
    pm._handle_info("blip three")
    _flush(pm)
    assert len(_summaries(pm)) == 1
    assert "blip one" in _summaries(pm)[0]
    assert "blip three" in _summaries(pm)[0]


def test_critical_does_not_coalesce_into_info(pm):
    pm._handle_info("blip one")
    pm.notify_event("wake_failed", "critical", "URGENT")
    deadline = time.time() + 3
    while time.time() < deadline:
        if any("URGENT" in d[1] for d in pm.delivered):
            break
        pm._notify_worker_wake()
        time.sleep(0.05)
    assert any("URGENT" in d[1] for d in pm.delivered), pm.delivered
    # info still pending, not delivered with the critical
    assert _summaries(pm) == []
    _flush(pm)
    assert len(_summaries(pm)) == 1
    assert "blip one" in _summaries(pm)[0]


def test_info_after_flush_starts_new_batch(pm):
    pm._handle_info("batch one")
    _flush(pm)
    pm._handle_info("batch two")
    time.sleep(0.3)
    assert len(_summaries(pm)) == 1   # only the first batch so far
    _flush(pm)
    sums = _summaries(pm)
    assert len(sums) == 2, f"expected two batches, got {len(sums)}"
    assert "batch one" in sums[0]
    assert "batch two" in sums[1]
