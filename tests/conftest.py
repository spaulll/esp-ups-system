"""Shared fixtures for testing the Pi brain (ups-monitor.py) in isolation.

The module is network-bound at import time only for config, so we import it
as source with its outbound calls stubbed: no hardware, no Telegram, no PVE.
"""
import os
import subprocess
import tempfile
import threading
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PI_MODULE = os.path.join(ROOT, "pi", "ups-monitor.py")


def _load_pi_module():
    env = dict(os.environ)
    env.setdefault("TG_BOT_TOKEN", "1234567890:TEST")
    env.setdefault("TG_CHAT_ID", "123456789")
    env.setdefault("TG_PROXY", "")
    env.setdefault("NTFY_URL_2", "http://10.10.10.241")
    env.setdefault("NTFY_DDNS_HOST", "ntfy.test")
    env.setdefault("NTFY_TOPIC", "ups")
    env.setdefault("NOTIFY_TOKEN", "testtoken")
    env.setdefault("ESP32_IP", "192.168.0.178")
    env.setdefault("PROXMOX_IP", "192.168.0.50")
    env.setdefault("PROXMOX_NODE", "prox")
    env.setdefault("PVE_TOKEN", "dummy-pve-token")
    env.setdefault("MAINS_DELAY_MIN", "5")
    env.setdefault("WAN_TIMEOUT_MIN", "10")

    r = subprocess.run(
        ["python3", os.path.join(ROOT, "scripts", "inject.py"), PI_MODULE],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"inject failed: {r.stderr}"

    mod = types.ModuleType("upsmonitor")
    mod.__dict__.setdefault("__name__", "upsmonitor")
    exec(compile(r.stdout, "upsmonitor", "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="session")
def pm():
    """Loaded, stubbed Pi-brain module with a live notification worker."""
    mod = _load_pi_module()

    mod.delivered = []
    mod.tg_fail = False
    mod.ntfy_fail = False

    def _tg_send(text):
        if mod.tg_fail:
            raise OSError("telegram unreachable")
        mod.delivered.append(("tg", text))
        return True

    def _ntfy_send(text, urgent=False, tg_failed=False):
        if mod.ntfy_fail:
            return False
        mod.delivered.append(("ntfy", text, urgent, tg_failed))
        return True

    mod._tg_send = _tg_send
    mod._ntfy_send = _ntfy_send
    # stub the live-edit message helpers so tests don't touch Telegram
    mod._tg_send_msg_calls = []
    mod._tg_send_msg = lambda text: (mod._tg_send_msg_calls.append(("send", text)) or 42)
    mod._tg_edit_msg_calls = []
    mod._tg_edit_msg = lambda mid, text: (mod._tg_edit_msg_calls.append(("edit", mid, text)) or True)

    mod.STATE_DIR = tempfile.mkdtemp(prefix="ups-test-")
    mod.SEQ_FILE = os.path.join(mod.STATE_DIR, "last-seq.json")
    mod.TG_OFFSET = os.path.join(mod.STATE_DIR, "tg-offset.json")
    mod.MISSED_FILE = os.path.join(mod.STATE_DIR, "missed-ledger.json")
    mod.COUNTERS_FILE = os.path.join(mod.STATE_DIR, "daily-counters.json")

    # single worker for the whole session — avoids double-delivery races
    t = threading.Thread(target=mod._notify_worker, daemon=True)
    t.start()
    mod._worker_thread = t
    return mod


@pytest.fixture(autouse=True)
def _clean(pm):
    """Reset in-memory state before each test (worker keeps running)."""
    pm.delivered.clear()
    pm.tg_fail = False
    pm.ntfy_fail = False
    pm._esp32_state.clear()
    pm._last_seq = 0
    pm._sensor_dead_since = None
    pm._tg_send_msg_calls.clear()
    pm._tg_edit_msg_calls.clear()
    with pm._notify_lock:
        pm._notify_queue.clear()
        pm._info_pending = None
    with pm._pending_conf_lock:
        pm._pending_conf.clear()
    yield


def stub_esp(pm, state=None, events=None):
    """Replace ESP client functions with canned data."""
    pm._esp_state_orig = pm._esp_state
    pm._esp_events_orig = pm._esp_events
    pm._esp_state = (lambda: state) if state is not None else (lambda: None)
    pm._esp_events = (lambda since: [e for e in (events or []) if e.get("seq", 0) > since])


@pytest.fixture
def wait_for(pm):
    """Poll a predicate (with worker nudges) until it passes or timeout."""
    def _wait(predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            pm._notify_worker_wake()
            time.sleep(0.05)
        return predicate()
    return _wait
