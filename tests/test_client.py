import pytest
import requests

from aruba_central_client import (
    ArubaCentralApiError,
    ArubaCentralAuthError,
    ArubaCentralConnectionError,
    RateLimiter,
    TokenStore,
    _retry_after_seconds,
)
from conftest import FakeResponse, FakeSession, load_fixture, token_ok


class MemoryStore(TokenStore):
    def __init__(self, token=None):
        self.token = token
        self.saved = []

    def load(self):
        return self.token

    def save(self, token):
        self.saved.append(token)
        self.token = token


def test_token_request_is_form_encoded_client_credentials(make_client):
    session = FakeSession([token_ok()], [FakeResponse(200, {"items": []})])
    make_client(session).get_json("/network-monitoring/v1/aps", {"limit": 1})
    post = session.posts[0]
    assert post["url"] == "https://sso.common.cloud.hpe.com/as/token.oauth2"
    assert post["data"] == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "secret"}
    assert post["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert session.gets[0]["headers"]["Authorization"] == "Bearer tok-1"
    assert session.gets[0]["url"] == "https://de3.api.central.arubanetworks.com/network-monitoring/v1/aps"


def test_token_is_cached_in_memory_and_store(make_client):
    store = MemoryStore()
    session = FakeSession([token_ok()], [FakeResponse(200, {}), FakeResponse(200, {})])
    client = make_client(session, token_store=store, clock=lambda: 1000.0)
    client.get_json("/a")
    client.get_json("/b")
    assert len(session.posts) == 1
    assert store.saved[0]["access_token"] == "tok-1"
    assert store.saved[0]["expires_at"] == pytest.approx(1000.0 + 7199)


def test_valid_stored_token_is_reused(make_client):
    store = MemoryStore({"access_token": "cached", "expires_at": 10_000})
    session = FakeSession([], [FakeResponse(200, {})])
    make_client(session, token_store=store, clock=lambda: 1000.0).get_json("/a")
    assert session.posts == []
    assert session.gets[0]["headers"]["Authorization"] == "Bearer cached"


def test_stored_token_close_to_expiry_is_refreshed(make_client):
    store = MemoryStore({"access_token": "old", "expires_at": 1100})
    session = FakeSession([token_ok("new")], [FakeResponse(200, {})])
    make_client(session, token_store=store, clock=lambda: 1000.0).get_json("/a")
    assert len(session.posts) == 1
    assert session.gets[0]["headers"]["Authorization"] == "Bearer new"


def test_broken_token_store_does_not_stop_collection(make_client):
    class Broken(TokenStore):
        def load(self):
            raise RuntimeError("kv down")

        def save(self, token):
            raise RuntimeError("kv down")

    session = FakeSession([token_ok()], [FakeResponse(200, {"ok": 1})])
    assert make_client(session, token_store=Broken()).get_json("/a") == {"ok": 1}


def test_token_rejected_raises_auth_error(make_client):
    session = FakeSession([FakeResponse(401, {"error": "invalid_client", "error_description": "bad secret"})])
    with pytest.raises(ArubaCentralAuthError, match="bad secret"):
        make_client(session).get_json("/a")


def test_token_without_access_token_raises(make_client):
    session = FakeSession([FakeResponse(200, {"foo": "bar"})])
    with pytest.raises(ArubaCentralAuthError):
        make_client(session).get_json("/a")


def test_token_connection_error(make_client):
    session = FakeSession([requests.exceptions.ConnectTimeout("x")])
    with pytest.raises(ArubaCentralConnectionError):
        make_client(session).get_json("/a")


def test_401_refreshes_token_once(make_client):
    session = FakeSession([token_ok("t1"), token_ok("t2")],
                          [FakeResponse(401, {"message": "expired"}), FakeResponse(200, {"ok": 1})])
    assert make_client(session).get_json("/a") == {"ok": 1}
    assert [g["headers"]["Authorization"] for g in session.gets] == ["Bearer t1", "Bearer t2"]


def test_second_401_raises_auth_error(make_client):
    session = FakeSession([token_ok("t1"), token_ok("t2")],
                          [FakeResponse(401, {}), FakeResponse(401, {"message": "nope"})])
    with pytest.raises(ArubaCentralAuthError):
        make_client(session).get_json("/a")


def test_429_honours_retry_after(make_client):
    sleeps = []
    session = FakeSession([token_ok()], [FakeResponse(429, {}, headers={"Retry-After": "7"}),
                                         FakeResponse(200, {"ok": 1})])
    client = make_client(session, sleep=sleeps.append)
    assert client.get_json("/a") == {"ok": 1}
    assert 7 in sleeps


def test_429_retry_after_is_capped(make_client):
    sleeps = []
    session = FakeSession([token_ok()], [FakeResponse(429, {}, headers={"Retry-After": "9999"}),
                                         FakeResponse(200, {})])
    make_client(session, sleep=sleeps.append).get_json("/a")
    assert max(sleeps) == 120


def test_5xx_retried_then_api_error(make_client):
    session = FakeSession([token_ok()], [FakeResponse(503, {"message": "down"})] * 3)
    with pytest.raises(ArubaCentralApiError) as err:
        make_client(session, max_retries=2).get_json("/a")
    assert err.value.status == 503
    assert len(session.gets) == 3


def test_connection_error_retried_then_raised(make_client):
    session = FakeSession([token_ok()], [requests.exceptions.ConnectionError("x")] * 2)
    with pytest.raises(ArubaCentralConnectionError):
        make_client(session, max_retries=1).get_json("/a")


def test_4xx_not_retried(make_client):
    session = FakeSession([token_ok()], [FakeResponse(400, {"message": "site-id is required"})])
    with pytest.raises(ArubaCentralApiError, match="site-id is required") as err:
        make_client(session).get_json("/a")
    assert err.value.status == 400
    assert len(session.gets) == 1


def test_invalid_json_raises(make_client):
    session = FakeSession([token_ok()], [FakeResponse(200, None, text="<html>")])
    with pytest.raises(ArubaCentralApiError):
        make_client(session).get_json("/a")


def test_paginate_follows_next_cursor_with_documented_shape(make_client):
    page1 = load_fixture("aps_documented.json")
    page2 = {"items": [{"serialNumber": "AP2"}], "count": 1, "total": 2, "next": None}
    session = FakeSession([token_ok()], [FakeResponse(200, page1), FakeResponse(200, page2)])
    items = list(make_client(session).paginate("/network-monitoring/v1/aps", {"filter": "x"}, limit=1000))
    assert [i["serialNumber"] for i in items] == ["AP00000001", "AP2"]
    assert session.gets[0]["params"] == {"filter": "x", "limit": 1000}
    assert session.gets[1]["params"] == {"filter": "x", "limit": 1000, "next": "2"}


def test_paginate_stops_on_repeated_cursor(make_client):
    page = {"items": [{"a": 1}], "next": "5"}
    session = FakeSession([token_ok()], [FakeResponse(200, page)] * 3)
    items = list(make_client(session).paginate("/x"))
    assert len(items) == 2
    assert len(session.gets) == 2


def test_paginate_stops_on_empty_page(make_client):
    session = FakeSession([token_ok()], [FakeResponse(200, {"items": [], "next": "9"})])
    assert list(make_client(session).paginate("/x")) == []


def test_proxies_disable_env_proxies(make_client):
    session = FakeSession()
    make_client(session, proxies={"https": "http://p:3128"})
    assert session.proxies == {"https": "http://p:3128"}
    assert session.trust_env is False


def test_rate_limiter_spaces_requests():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(2, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.acquire()
    assert slept == [pytest.approx(0.5), pytest.approx(0.5)]


def test_retry_after_parsing():
    assert _retry_after_seconds("5", 0) == 5
    assert _retry_after_seconds(None, 0) is None
    assert _retry_after_seconds("garbage", 0) is None
    assert _retry_after_seconds("Thu, 01 Jan 1970 00:00:10 GMT", 0) == pytest.approx(10)


def test_secret_and_token_not_logged(make_client, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    session = FakeSession([token_ok("SUPERTOKEN")], [FakeResponse(401, {}), FakeResponse(200, {})])
    session.token_responses.append(token_ok("SUPERTOKEN2"))
    make_client(session).get_json("/a")
    assert "SUPERTOKEN" not in caplog.text
    assert "secret" not in caplog.text
