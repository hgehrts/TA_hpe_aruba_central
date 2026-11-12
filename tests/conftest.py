import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "package", "bin"))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _install_splunk_stub():
    """splunktaucclib imports `splunk.admin`, which only exists inside Splunk."""
    if "splunk" in sys.modules:
        return
    splunk = types.ModuleType("splunk")

    class RESTException(Exception):
        def __init__(self, statusCode=500, msg=""):
            super().__init__(msg)
            self.statusCode = statusCode
            self.msg = msg

    splunk.RESTException = RESTException
    admin = types.ModuleType("splunk.admin")

    class MConfigHandler:
        def __init__(self, *args, **kwargs):
            pass

    admin.MConfigHandler = MConfigHandler
    for i, name in enumerate(["ACTION_CREATE", "ACTION_LIST", "ACTION_EDIT", "ACTION_REMOVE",
                              "ACTION_MEMBERS", "ACTION_RELOAD"]):
        setattr(admin, name, i + 1)
    admin.CONTEXT_NONE = 0
    admin.CONTEXT_APP_ONLY = 1
    admin.CONTEXT_APP_AND_USER = 2
    admin.InternalException = Exception
    admin.ArgValidationException = Exception
    admin.NotFoundException = Exception
    admin.init = lambda *a, **k: None
    splunk.admin = admin
    sys.modules["splunk"] = splunk
    sys.modules["splunk.admin"] = admin


_install_splunk_stub()


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Replays queued responses (or exceptions) and records every call."""

    def __init__(self, token_responses=None, get_responses=None):
        self.headers = {}
        self.proxies = {}
        self.trust_env = True
        self.token_responses = list(token_responses or [])
        self.get_responses = list(get_responses or [])
        self.posts = []
        self.gets = []

    @staticmethod
    def _next(queue):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        return self._next(self.token_responses)

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self._next(self.get_responses)


def token_ok(token="tok-1", expires_in=7199):
    return FakeResponse(200, {"access_token": token, "token_type": "Bearer", "expires_in": expires_in})


@pytest.fixture
def fake_session_cls():
    return FakeSession


@pytest.fixture
def make_client():
    from aruba_central_client import ArubaCentralClient

    def _make(session, **kwargs):
        kwargs.setdefault("max_rps", 10)
        kwargs.setdefault("sleep", lambda _s: None)
        return ArubaCentralClient(
            base_url="https://de3.api.central.arubanetworks.com",
            token_url="https://sso.common.cloud.hpe.com/as/token.oauth2",
            client_id="cid",
            client_secret="secret",
            session=session,
            **kwargs,
        )

    return _make
