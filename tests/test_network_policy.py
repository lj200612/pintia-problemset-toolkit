from __future__ import annotations

import importlib
import os

import pta
from pta.network import chromium_direct_args, direct_session


def test_importing_package_does_not_mutate_proxy_environment(monkeypatch):
    expected = {
        "HTTP_PROXY": "http://proxy.example.invalid:8080",
        "HTTPS_PROXY": "http://proxy.example.invalid:8443",
        "ALL_PROXY": "socks5://proxy.example.invalid:1080",
        "NO_PROXY": "localhost",
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)

    importlib.reload(pta)

    for name, value in expected.items():
        assert os.environ[name] == value


def test_pta_requests_session_does_not_inherit_proxy_environment():
    session = direct_session()
    try:
        assert session.trust_env is False
    finally:
        session.close()


def test_system_proxy_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("PTA_USE_SYSTEM_PROXY", "1")
    session = direct_session()
    try:
        assert session.trust_env is True
        assert tuple(chromium_direct_args()) == ()
    finally:
        session.close()
