import json
import logging

import pytest

import aruba_central_common as common
from conftest import FakeResponse, FakeSession, token_ok
from solnlib.credentials import CredentialNotExistException


class FakeCredentialManager:
    store = {}

    def __init__(self, session_key, app, realm=None, owner="nobody"):
        self.realm = realm

    def get_password(self, user):
        try:
            return self.store[(self.realm, user)]
        except KeyError:
            raise CredentialNotExistException("missing") from None

    def set_password(self, user, password):
        self.store[(self.realm, user)] = password

    def delete_password(self, user):
        self.store.pop((self.realm, user), None)


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    FakeCredentialManager.store = {}
    monkeypatch.setattr(common, "CredentialManager", FakeCredentialManager)
    return FakeCredentialManager


def test_token_store_roundtrip_and_fingerprint():
    store = common.SplunkTokenStore("sk", "acc", "fp1")
    assert store.load() is None
    store.save({"access_token": "t", "expires_at": 1})
    raw = json.loads(FakeCredentialManager.store[(common.TOKEN_REALM, "acc")])
    assert raw["fingerprint"] == "fp1"
    assert store.load()["access_token"] == "t"
    assert common.SplunkTokenStore("sk", "acc", "other").load() is None


class FakeConf:
    def __init__(self, stanzas):
        self.stanzas = stanzas

    def get(self, name):
        if name not in self.stanzas:
            raise KeyError(name)
        return self.stanzas[name]


class FakeConfManager:
    stanzas = {}

    def __init__(self, *args, **kwargs):
        self.realm = kwargs.get("realm")

    def get_conf(self, name):
        return FakeConf(self.stanzas)


ACCOUNT = {
    "base_url": "https://de3.api.central.arubanetworks.com",
    "token_url": "https://sso.common.cloud.hpe.com/as/token.oauth2",
    "client_id": "cid",
    "client_secret": "secret",
    "max_rps": "3",
}


def test_get_account_and_missing(monkeypatch):
    FakeConfManager.stanzas = {"acc": dict(ACCOUNT), "bad": {"base_url": "x"}}
    monkeypatch.setattr(common.conf_manager, "ConfManager", FakeConfManager)
    assert common.get_account("sk", "acc")["client_id"] == "cid"
    with pytest.raises(common.ConfigurationError, match="no token_url"):
        common.get_account("sk", "bad")
    with pytest.raises(common.ConfigurationError, match="not found"):
        common.get_account("sk", "nope")


def test_build_client_uses_account(monkeypatch):
    FakeConfManager.stanzas = {"acc": dict(ACCOUNT)}
    monkeypatch.setattr(common.conf_manager, "ConfManager", FakeConfManager)
    monkeypatch.setattr(common, "get_proxies", lambda logger, sk: None)
    client = common.build_client("sk", "acc", logging.getLogger("t"))
    assert client.base_url == ACCOUNT["base_url"]
    assert client.token_url == ACCOUNT["token_url"]


def test_get_proxies_handles_missing_settings(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no settings")

    monkeypatch.setattr(common.conf_manager, "get_proxy_dict", boom)
    assert common.get_proxies(logging.getLogger("t"), "sk") is None
    monkeypatch.setattr(common.conf_manager, "get_proxy_dict",
                        lambda *a, **k: {"proxy_enabled": "1", "proxy_url": "p.corp", "proxy_port": "8080"})
    assert common.get_proxies(logging.getLogger("t"), "sk")["https"] == "http://p.corp:8080"


class FakeInputs:
    def __init__(self, inputs):
        self.inputs = inputs
        self.metadata = {"session_key": "sk"}


def test_run_inputs_runs_collect_and_isolates_errors(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    logger_names = []

    def fake_logger(name):
        logger_names.append(name)
        return logging.getLogger("t_" + name)

    monkeypatch.setattr(common, "logger_for_input", fake_logger)
    monkeypatch.setattr(common.conf_manager, "get_log_level", lambda **k: "INFO")

    class C:
        base_url = "https://de3.api.central.arubanetworks.com"
        request_count = 3

    monkeypatch.setattr(common, "build_client", lambda sk, acc, logger: C())
    seen = []

    def collect(ctx):
        seen.append((ctx.input_type, ctx.name, ctx.host))
        if ctx.name == "boom":
            raise RuntimeError("bad")

    inputs = FakeInputs({
        "aruba_central_wireless://ok": {"account": "acc", "index": "aruba"},
        "aruba_central_wireless://boom": {"account": "acc"},
        "aruba_central_wireless://noacc": {},
    })
    common.run_inputs(inputs, None, collect)
    assert ("aruba_central_wireless", "ok", "de3.api.central.arubanetworks.com") in seen
    assert len(seen) == 2
    assert logger_names == ["wireless_ok", "wireless_boom", "wireless_noacc"]
    assert 'exc_l="Unexpected Error"' in caplog.text and "RuntimeError: bad" in caplog.text
    assert 'exc_l="Configuration Error"' in caplog.text
    assert "action=run_summary" in caplog.text and "api_requests=3" in caplog.text


# ---------------------------------------------------------------- handlers
import aruba_central_account_handler as acct  # noqa: E402
import aruba_central_checkpoint_handler as ckh  # noqa: E402
from splunktaucclib.rest_handler.error import RestError  # noqa: E402


class Args:
    def __init__(self, id_):
        self.id = id_


def _bare_account_handler(payload, stored=None):
    h = acct.ArubaCentralAccountHandler.__new__(acct.ArubaCentralAccountHandler)
    h.payload = payload
    h.callerArgs = Args("acc")
    h.getSessionKey = lambda: "sk"
    h._stored_account = lambda: stored or {}
    return h


def test_edit_with_placeholder_uses_stored_secret(monkeypatch):
    captured = {}
    monkeypatch.setattr(acct, "_logger", lambda: logging.getLogger("t"))
    monkeypatch.setattr(acct, "get_proxies", lambda logger, sk: None)
    monkeypatch.setattr(acct, "validate_account", lambda account, **k: captured.update(account))
    payload = dict(ACCOUNT, client_secret="******")
    _bare_account_handler(payload, stored={"client_secret": "real"})._validate(editing=True)
    assert captured["client_secret"] == "real"


def test_create_requires_all_fields(monkeypatch):
    monkeypatch.setattr(acct, "get_proxies", lambda logger, sk: None)
    with pytest.raises(RestError, match="client_secret"):
        _bare_account_handler(dict(ACCOUNT, client_secret=""))._validate(editing=False)


def test_drop_cached_token(monkeypatch):
    monkeypatch.setattr(acct, "CredentialManager", FakeCredentialManager)
    FakeCredentialManager.store[(acct.TOKEN_REALM, "acc")] = "x"
    _bare_account_handler({})._drop_cached_token()
    assert FakeCredentialManager.store == {}


def test_validate_account_end_to_end_with_fake_session():
    session = FakeSession([token_ok()], [FakeResponse(200, {"items": []})])
    acct.validate_account(ACCOUNT, session=session)


def test_checkpoint_handler_deletes_key(monkeypatch):
    deleted = []

    class Ck:
        def delete(self, key):
            deleted.append(key)

    monkeypatch.setattr(ckh, "get_checkpointer", lambda sk: Ck())
    monkeypatch.setattr(ckh.AdminExternalHandler, "handleRemove", lambda self, conf: None)

    class Endpoint:
        input_type = "aruba_central_ap_throughput"

    class H:
        def get_endpoint(self):
            return Endpoint()

    h = ckh.ArubaCentralCheckpointInputHandler.__new__(ckh.ArubaCentralCheckpointInputHandler)
    h.handler = H()
    h.callerArgs = Args("t1")
    h.getSessionKey = lambda: "sk"
    h.handleRemove(None)
    assert deleted == ["aruba_central_ap_throughput_t1"]
