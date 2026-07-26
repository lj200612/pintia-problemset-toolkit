from __future__ import annotations

import copy
import json

import pytest

import pta.client as client_module
from pta.client import PTAClient, PTAHTTPError, clear_get_cache
from pta.config import Config


class FakeResponse:
    def __init__(self, payload, *, url="https://example.invalid/api", status_code=200):
        self._payload = payload
        self.url = url
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        return copy.deepcopy(self._payload)


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.get_calls = []
        self.post_calls = []

    def get(self, url, *, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse({"call": len(self.get_calls), "nested": {"value": "original"}}, url=url)

    def post(self, url, *, data=None, timeout=None):
        self.post_calls.append({"url": url, "data": data, "timeout": timeout})
        return FakeResponse({"posted": True}, url=url)


@pytest.fixture(autouse=True)
def isolated_get_cache():
    clear_get_cache()
    yield
    clear_get_cache()


def _client(monkeypatch, *, cookie="session=secret"):
    session = FakeSession()
    monkeypatch.setattr(client_module, "direct_session", lambda: session)
    cfg = Config(
        base_url="https://example.invalid",
        problem_set_id="ps-1",
        cookie=cookie,
        extra_headers={"X-Test": "yes"},
    )
    return PTAClient(cfg), session


def test_client_builds_urls_and_expected_headers(monkeypatch):
    client, session = _client(monkeypatch)

    assert client._url("/api/test") == "https://example.invalid/api/test"
    assert client._url("https://passport.example.invalid/me") == (
        "https://passport.example.invalid/me"
    )
    assert session.headers["Cookie"] == "session=secret"
    assert session.headers["Origin"] == "https://example.invalid"
    assert session.headers["Referer"].endswith("/problem-sets/ps-1/overview")
    assert session.headers["X-Test"] == "yes"


def test_cache_key_is_param_order_independent_and_does_not_contain_raw_cookie(monkeypatch):
    client, session = _client(monkeypatch)

    first = client._cache_key("/api/test", {"b": 2, "a": 1})
    reordered = client._cache_key("/api/test", {"a": 1, "b": 2})
    assert first == reordered
    assert "session=secret" not in repr(first)
    assert len(first[2]) == 64

    session.headers["Cookie"] = "session=different"
    assert client._cache_key("/api/test", {"a": 1, "b": 2}) != first


def test_cache_identity_does_not_collapse_distinct_lone_surrogates(monkeypatch):
    client, session = _client(monkeypatch)
    session.headers["Authorization"] = "Bearer \ud800"
    first = client._cache_key("/api/test", None)
    session.headers["Authorization"] = "Bearer \ud801"
    second = client._cache_key("/api/test", None)

    assert first != second
    assert len(first[2]) == len(second[2]) == 64


def test_get_cache_returns_deep_copies_and_expires(monkeypatch):
    client, session = _client(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(client_module.time, "time", lambda: clock[0])

    first = client.get("/api/cached", params={"q": "x"}, cache_seconds=10)
    first["nested"]["value"] = "mutated-by-caller"
    second = client.get("/api/cached", params={"q": "x"}, cache_seconds=10)

    assert len(session.get_calls) == 1
    assert second == {"call": 1, "nested": {"value": "original"}}
    assert second is not first

    clock[0] = 111.0
    third = client.get("/api/cached", params={"q": "x"}, cache_seconds=10)
    assert len(session.get_calls) == 2
    assert third["call"] == 2


def test_post_uses_json_and_invalidates_get_cache(monkeypatch):
    client, session = _client(monkeypatch)
    monkeypatch.setattr(client_module.time, "time", lambda: 100.0)

    assert client.get("/api/value", cache_seconds=60)["call"] == 1
    assert client.get("/api/value", cache_seconds=60)["call"] == 1

    result = client.post("/api/write", {"answer": 42})

    assert result == {"posted": True}
    assert session.post_calls == [
        {
            "url": "https://example.invalid/api/write",
            "data": json.dumps({"answer": 42}),
            "timeout": 30,
        }
    ]
    assert client.get("/api/value", cache_seconds=60)["call"] == 2
    assert len(session.get_calls) == 2


def test_cache_is_partitioned_by_cookie(monkeypatch):
    sessions = []

    def make_session():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(client_module, "direct_session", make_session)
    monkeypatch.setattr(client_module.time, "time", lambda: 100.0)
    first = PTAClient(Config("https://example.invalid", "ps", "cookie=a"))
    second = PTAClient(Config("https://example.invalid", "ps", "cookie=b"))

    first.get("/api/same", cache_seconds=60)
    second.get("/api/same", cache_seconds=60)

    assert [len(session.get_calls) for session in sessions] == [1, 1]


def test_cache_is_partitioned_by_extra_authorization_headers(monkeypatch):
    sessions = []

    def make_session():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(client_module, "direct_session", make_session)
    monkeypatch.setattr(client_module.time, "time", lambda: 100.0)
    first = PTAClient(
        Config(
            "https://example.invalid",
            "ps",
            "cookie=same",
            extra_headers={"Authorization": "Bearer AUTH_SECRET_ONE"},
        )
    )
    second = PTAClient(
        Config(
            "https://example.invalid",
            "ps",
            "cookie=same",
            extra_headers={"Authorization": "Bearer AUTH_SECRET_TWO"},
        )
    )

    first_key = first._cache_key("/api/same", None)
    second_key = second._cache_key("/api/same", None)
    assert first_key != second_key
    assert len(first_key[2]) == len(second_key[2]) == 64
    assert "AUTH_SECRET_ONE" not in repr(first_key)
    assert "AUTH_SECRET_TWO" not in repr(second_key)

    first.get("/api/same", cache_seconds=60)
    second.get("/api/same", cache_seconds=60)

    assert [len(session.get_calls) for session in sessions] == [1, 1]
    assert "AUTH_SECRET_ONE" not in repr(client_module._GET_CACHE)
    assert "AUTH_SECRET_TWO" not in repr(client_module._GET_CACHE)


def test_get_cache_is_lru_bounded_and_prunes_expired_entries(monkeypatch):
    client, session = _client(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(client_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(client_module, "GET_CACHE_MAX_ENTRIES", 2)

    client.get("/api/one", cache_seconds=60)
    client.get("/api/two", cache_seconds=60)
    # Refresh /one so /two becomes the least-recently-used entry.
    client.get("/api/one", cache_seconds=60)
    client.get("/api/three", cache_seconds=60)

    assert len(client_module._GET_CACHE) == 2
    assert not any(key[0].endswith("/api/two") for key in client_module._GET_CACHE)
    assert len(session.get_calls) == 3

    # The evicted key performs a fresh request and the cache remains bounded.
    client.get("/api/two", cache_seconds=60)
    assert len(session.get_calls) == 4
    assert len(client_module._GET_CACHE) == 2

    # A later insertion removes every expired entry rather than retaining dead keys.
    clock[0] = 200.0
    client.get("/api/fresh", cache_seconds=60)
    assert len(client_module._GET_CACHE) == 1
    assert next(iter(client_module._GET_CACHE))[0].endswith("/api/fresh")


def test_http_error_exposes_only_sanitized_persistent_log_fields(monkeypatch):
    client, _session = _client(monkeypatch)
    response_body = "server echoed source=TOP_SECRET_SOURCE and cookie=TOP_SECRET_COOKIE"
    response = FakeResponse(
        None,
        url=(
            "https://url-user:url-password@example.invalid:8443/api/submissions"
            "?cookie=TOP_SECRET_COOKIE#TOP_SECRET_SOURCE"
        ),
        status_code=500,
    )
    response.text = response_body

    with pytest.raises(PTAHTTPError) as raised:
        client._handle(response)

    error = raised.value
    fields = error.safe_log_fields()
    serialized = json.dumps(fields)
    assert fields == {
        "errorClass": "PTAHTTPError",
        "errorCode": "PTA_HTTP_ERROR",
        "httpStatus": 500,
        "requestUrl": "https://example.invalid:8443/api/submissions",
    }
    assert response_body in str(error)
    assert "url-password" not in str(error)
    assert "TOP_SECRET_COOKIE" not in serialized
    assert "TOP_SECRET_SOURCE" not in serialized
