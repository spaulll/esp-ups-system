"""Tri-state PVE probe classification (up/down/unknown).

Locked rules:
  - HTTP 200 + uptime -> up
  - HTTP 401/403/5xx / garbage -> unknown (node may well be up)
  - connection refused / timeout / no route -> down

These tests are fully self-contained: they load a pristine copy of the Pi
module (the session-scoped `pm` is mutated by other test files, which would
make classifier tests order-dependent).
"""
import json
import socket
import types

import pytest

import conftest


@pytest.fixture()
def probe_mod():
    return conftest._load_pi_module()


def test_probe_up_parses_uptime(probe_mod):
    body = json.dumps({"data": {"uptime": 100}})
    probe_mod._https_get_insecure = lambda *a, **k: (200, body)
    status, secs, label = probe_mod._pve_probe()
    assert status == "up" and secs == 100 and "1m" in label


def test_probe_401_is_unknown_not_down(probe_mod):
    probe_mod._https_get_insecure = lambda *a, **k: (401, "")
    status, _, label = probe_mod._pve_probe()
    assert status == "unknown" and label == "Unauthorized"


def test_probe_5xx_is_unknown(probe_mod):
    probe_mod._https_get_insecure = lambda *a, **k: (502, "bad gateway")
    status, _, _ = probe_mod._pve_probe()
    assert status == "unknown"


def test_probe_malformed_body_is_unknown(probe_mod):
    probe_mod._https_get_insecure = lambda *a, **k: (200, "not json")
    status, _, _ = probe_mod._pve_probe()
    assert status == "unknown"


def test_probe_negative_uptime_is_unknown(probe_mod):
    body = json.dumps({"data": {"uptime": -1}})
    probe_mod._https_get_insecure = lambda *a, **k: (200, body)
    status, _, _ = probe_mod._pve_probe()
    assert status == "unknown"


def test_probe_connection_refused_is_down(probe_mod):
    def refused(*a, **k):
        raise ConnectionRefusedError
    probe_mod._https_get_insecure = refused
    status, _, label = probe_mod._pve_probe()
    assert status == "down" and label == "Offline"


def test_probe_timeout_is_down(probe_mod):
    def to(*a, **k):
        raise socket.timeout
    probe_mod._https_get_insecure = to
    status, _, _ = probe_mod._pve_probe()
    assert status == "down"


@pytest.mark.parametrize("exc", [OSError("no route"), TimeoutError])
def test_probe_other_network_errors_are_down(probe_mod, exc):
    def raise_it(*a, **k):
        raise exc
    probe_mod._https_get_insecure = raise_it
    status, _, _ = probe_mod._pve_probe()
    assert status == "down"
