import json
import logging

import pytest

import aruba_central_ap_throughput_helper as tp
import aruba_central_common as common
import aruba_central_health_helper as health
import aruba_central_inventory_helper as inventory
import aruba_central_wireless_helper as wireless
from aruba_central_client import ArubaCentralApiError, ArubaCentralAuthError
from conftest import load_fixture


class FakeWriter:
    def __init__(self):
        self.events = []

    def write_event(self, event):
        self.events.append(event)


class FakeCheckpointer:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.updates = 0

    def get(self, key):
        return self.data.get(key)

    def update(self, key, state):
        self.updates += 1
        self.data[key] = json.loads(json.dumps(state))

    def delete(self, key):
        self.data.pop(key, None)


class FakeClient:
    """Maps path -> list of items (paginate) or callable(params) -> body (get_json)."""

    def __init__(self, lists=None, bodies=None):
        self.lists = lists or {}
        self.bodies = bodies or {}
        self.calls = []
        self.base_url = "https://de3.api.central.arubanetworks.com"
        self.request_count = 0

    def paginate(self, path, params=None, limit=100):
        self.calls.append(("paginate", path, params, limit))
        value = self.lists[path]
        if isinstance(value, Exception):
            raise value
        return iter(value)

    def get_json(self, path, params=None):
        self.calls.append(("get", path, params))
        value = self.bodies[path]
        if isinstance(value, Exception):
            raise value
        return value(params) if callable(value) else value


def make_ctx(client, item=None, name="aruba_central_wireless://w1"):
    ctx = common.InputContext(name, item or {"index": "aruba", "account": "acc"}, "sk", FakeWriter(),
                              logging.getLogger("test"))
    ctx.client = client
    ctx.host = "de3.api.central.arubanetworks.com"
    return ctx


def events_of(ctx, sourcetype=None):
    return [e for e in ctx.event_writer.events if sourcetype is None or e.sourceType == sourcetype]


def test_wireless_writes_one_event_per_ap_and_swarm():
    aps = load_fixture("aps_documented.json")["items"]
    swarms = [{"clusterId": "c1", "conductorDeviceName": "cond", "conductorSerialNumber": "S1"}]
    client = FakeClient({wireless.AP_PATH: aps, wireless.SWARM_PATH: swarms})
    ctx = make_ctx(client)
    wireless.collect(ctx)
    ap_events = events_of(ctx, "aruba:central:ap")
    assert len(ap_events) == 1 and len(events_of(ctx, "aruba:central:swarm")) == 1
    data = json.loads(ap_events[0].data)
    assert data["clientCount"] == 8 and data["cpuUtilization"] == 25 and data["clusterId"]
    assert ap_events[0].index == "aruba"
    assert ap_events[0].host == "de3.api.central.arubanetworks.com"
    assert ("paginate", wireless.AP_PATH, None, 1000) in client.calls


def test_endpoint_failure_does_not_block_other_endpoint():
    client = FakeClient({wireless.AP_PATH: ArubaCentralApiError("HTTP 500", 500, wireless.AP_PATH),
                         wireless.SWARM_PATH: [{"clusterId": "c1"}]})
    ctx = make_ctx(client)
    wireless.collect(ctx)
    assert ctx.errors == 1
    assert len(events_of(ctx, "aruba:central:swarm")) == 1


def test_inventory_sourcetypes():
    client = FakeClient({inventory.INVENTORY_PATH: [{"a": 1}, {"a": 2}], inventory.DEVICES_PATH: [{"b": 1}]})
    ctx = make_ctx(client, name="aruba_central_inventory://i1")
    inventory.collect(ctx)
    assert len(events_of(ctx, "aruba:central:device_inventory")) == 2
    assert len(events_of(ctx, "aruba:central:device")) == 1


def test_alerts_only_new_or_changed(monkeypatch):
    ckpt = FakeCheckpointer()
    monkeypatch.setattr(health, "get_checkpointer", lambda _sk: ckpt)
    alerts = [{"id": "1", "status": "Active"}, {"id": "2", "status": "Active"}]
    client = FakeClient({health.SITES_HEALTH_PATH: [{"site": "s"}], health.ALERTS_PATH: alerts})
    ctx = make_ctx(client, name="aruba_central_health://h1")
    health.collect(ctx)
    assert len(events_of(ctx, "aruba:central:alert")) == 2

    alerts2 = [{"id": "1", "status": "Active"}, {"id": "2", "status": "Cleared"}]
    client2 = FakeClient({health.SITES_HEALTH_PATH: [], health.ALERTS_PATH: alerts2})
    ctx2 = make_ctx(client2, name="aruba_central_health://h1")
    health.collect(ctx2)
    emitted = [json.loads(e.data) for e in events_of(ctx2, "aruba:central:alert")]
    assert emitted == [{"id": "2", "status": "Cleared"}]
    assert "aruba_central_health_h1" in ckpt.data


class BrokenCheckpointer(FakeCheckpointer):
    def get(self, key):
        raise RuntimeError("HTTP 503 Service Unavailable -- KV Store is initializing.")


def test_alerts_skipped_when_checkpoint_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(health, "get_checkpointer", lambda _sk: BrokenCheckpointer())
    client = FakeClient({health.SITES_HEALTH_PATH: [{"site": "s"}], health.ALERTS_PATH: [{"id": "1"}]})
    ctx = make_ctx(client, name="aruba_central_health://h1")
    with caplog.at_level(logging.INFO):
        health.collect(ctx)
    assert events_of(ctx, "aruba:central:alert") == []
    assert len(events_of(ctx, "aruba:central:site_health")) == 1
    assert ("paginate", health.ALERTS_PATH, None, health.ALERTS_PAGE_SIZE) not in client.calls
    assert ctx.errors == 1
    assert 'exc_l="KV Store Error"' in caplog.text and "KV Store is initializing" in caplog.text


def test_alerts_checkpoint_write_failure_is_logged(monkeypatch):
    class WriteBroken(FakeCheckpointer):
        def update(self, key, state):
            raise RuntimeError("write failed")

    monkeypatch.setattr(health, "get_checkpointer", lambda _sk: WriteBroken())
    client = FakeClient({health.SITES_HEALTH_PATH: [], health.ALERTS_PATH: [{"id": "1"}]})
    ctx = make_ctx(client, name="aruba_central_health://h1")
    health.collect(ctx)
    assert len(events_of(ctx, "aruba:central:alert")) == 1
    assert ctx.errors == 1


def test_throughput_stops_before_api_calls_when_checkpoint_unavailable(monkeypatch):
    monkeypatch.setattr(tp, "get_checkpointer", lambda _sk: BrokenCheckpointer())
    client = FakeClient({tp.AP_PATH: [{"serialNumber": "S1"}]})
    ctx = make_ctx(client, name="aruba_central_ap_throughput://t1")
    with pytest.raises(common.CheckpointError, match="KV store checkpoint read failed"):
        tp.collect(ctx)
    assert client.calls == []


def test_samples_to_records_documented_payload():
    body = load_fixture("throughput_documented.json")
    ap = {"serialNumber": "AP00000001", "deviceName": "ArubaAP", "siteName": "site1", "status": "ONLINE"}
    records, newest = tp.samples_to_records(ap, "WIRELESS", body)
    assert records == [{
        "serialNumber": "AP00000001", "deviceName": "ArubaAP", "siteName": "site1",
        "interfaceType": "WIRELESS", "metric": "ap_throughput",
        "timestamp": "2022-07-30T20:00:00Z", "tx": 10, "rx": 12,
    }]
    assert newest == common.parse_rfc3339("2022-07-30T20:00:00Z")


def test_samples_at_or_before_checkpoint_are_dropped():
    body = {"graph": {"keys": ["tx", "rx"], "samples": [
        {"timestamp": "2026-11-12T08:00:00Z", "data": [1, 2]},
        {"timestamp": "2026-11-12T08:05:00Z", "data": [3, 4]},
    ]}}
    since = common.parse_rfc3339("2026-11-12T08:00:00Z")
    records, newest = tp.samples_to_records({"serialNumber": "S"}, "WIRELESS", body, since)
    assert [r["tx"] for r in records] == [3]
    assert newest == common.parse_rfc3339("2026-11-12T08:05:00Z")


def test_samples_with_missing_graph_or_short_data():
    assert tp.samples_to_records({}, "WIRELESS", {"foo": 1}) == ([], None)
    body = {"graph": {"keys": ["tx", "rx"], "samples": [{"timestamp": "2026-11-12T08:00:00Z", "data": [5]}]}}
    records, _ = tp.samples_to_records({"serialNumber": "S"}, "WIRED", body)
    assert records[0]["tx"] == 5 and records[0]["rx"] is None


def test_trend_params():
    now = common.parse_rfc3339("2026-11-12T12:00:00Z")
    assert tp.trend_params("WIRELESS", None, now) == {"interface-type": "WIRELESS"}
    p = tp.trend_params("WIRELESS", now - 600, now)
    assert p["filter"] == "timestamp gt 2026-11-12T11:50:00Z and timestamp lt 2026-11-12T12:00:00Z"
    p = tp.trend_params("WIRED", now - 86400, now)
    assert p["filter"].startswith("timestamp gt 2026-11-12T09:00:00Z")


def _throughput_client(aps, body_for_serial):
    bodies = {tp.TREND_PATH % ap["serialNumber"]: body_for_serial(ap["serialNumber"]) for ap in aps}
    return FakeClient({tp.AP_PATH: aps}, bodies)


def test_throughput_collect_incremental(monkeypatch):
    ckpt = FakeCheckpointer()
    monkeypatch.setattr(tp, "get_checkpointer", lambda _sk: ckpt)
    aps = [{"serialNumber": "S1", "deviceName": "ap1"}, {"serialNumber": "S2", "deviceName": "ap2"}]
    body = {"metric": "ap_throughput", "graph": {"keys": ["tx", "rx"], "samples": [
        {"timestamp": "2026-11-12T08:00:00Z", "data": [1, 2]},
        {"timestamp": "2026-11-12T08:05:00Z", "data": [3, 4]}]}}
    client = _throughput_client(aps, lambda _s: body)
    ctx = make_ctx(client, {"index": "aruba", "account": "acc", "interface_type": "WIRELESS"},
                   name="aruba_central_ap_throughput://t1")
    tp.collect(ctx)

    evts = events_of(ctx, "aruba:central:ap_throughput")
    assert len(evts) == 4
    assert float(evts[0].time) == common.parse_rfc3339("2026-11-12T08:00:00Z")
    assert ("paginate", tp.AP_PATH, {"filter": "status eq 'ONLINE'"}, 1000) in client.calls
    first_get = [c for c in client.calls if c[0] == "get"][0]
    assert first_get[2] == {"interface-type": "WIRELESS"}
    state = ckpt.data["aruba_central_ap_throughput_t1"]
    assert state["last_sample"]["S1"] == common.parse_rfc3339("2026-11-12T08:05:00Z")

    client2 = _throughput_client(aps, lambda _s: body)
    ctx2 = make_ctx(client2, {"index": "aruba", "account": "acc", "interface_type": "WIRELESS"},
                    name="aruba_central_ap_throughput://t1")
    tp.collect(ctx2)
    assert events_of(ctx2) == []
    second_get = [c for c in client2.calls if c[0] == "get"][0]
    assert second_get[2]["filter"].startswith("timestamp gt ")


def test_throughput_interface_change_resets_checkpoint(monkeypatch):
    ckpt = FakeCheckpointer({"aruba_central_ap_throughput_t1": {"interface_type": "WIRELESS",
                                                                "last_sample": {"S1": 9e9}}})
    monkeypatch.setattr(tp, "get_checkpointer", lambda _sk: ckpt)
    aps = [{"serialNumber": "S1"}]
    body = {"graph": {"keys": ["tx"], "samples": [{"timestamp": "2026-11-12T08:00:00Z", "data": [1]}]}}
    ctx = make_ctx(_throughput_client(aps, lambda _s: body),
                   {"index": "aruba", "account": "acc", "interface_type": "WIRED"},
                   name="aruba_central_ap_throughput://t1")
    tp.collect(ctx)
    assert len(events_of(ctx)) == 1
    assert ckpt.data["aruba_central_ap_throughput_t1"]["interface_type"] == "WIRED"


def test_throughput_aborts_on_auth_error_but_keeps_progress(monkeypatch):
    ckpt = FakeCheckpointer()
    monkeypatch.setattr(tp, "get_checkpointer", lambda _sk: ckpt)
    aps = [{"serialNumber": "S1"}, {"serialNumber": "S2"}, {"serialNumber": "S3"}]
    good = {"graph": {"keys": ["tx"], "samples": [{"timestamp": "2026-11-12T08:00:00Z", "data": [1]}]}}
    client = FakeClient({tp.AP_PATH: aps}, {
        tp.TREND_PATH % "S1": good,
        tp.TREND_PATH % "S2": ArubaCentralAuthError("401"),
        tp.TREND_PATH % "S3": good,
    })
    ctx = make_ctx(client, {"index": "aruba", "account": "acc"}, name="aruba_central_ap_throughput://t1")
    tp.collect(ctx)
    assert len(events_of(ctx)) == 1
    assert ctx.errors == 1
    assert list(ckpt.data["aruba_central_ap_throughput_t1"]["last_sample"]) == ["S1"]
    assert (("get", tp.TREND_PATH % "S3", {"interface-type": "WIRELESS"}) not in client.calls)


def test_throughput_skips_single_ap_404(monkeypatch):
    monkeypatch.setattr(tp, "get_checkpointer", lambda _sk: FakeCheckpointer())
    aps = [{"serialNumber": "S1"}, {"serialNumber": "S2"}]
    good = {"graph": {"keys": ["tx"], "samples": [{"timestamp": "2026-11-12T08:00:00Z", "data": [1]}]}}
    client = FakeClient({tp.AP_PATH: aps}, {
        tp.TREND_PATH % "S1": ArubaCentralApiError("HTTP 404", 404, "x"),
        tp.TREND_PATH % "S2": good,
    })
    ctx = make_ctx(client, {"index": "aruba", "account": "acc"}, name="aruba_central_ap_throughput://t1")
    tp.collect(ctx)
    assert len(events_of(ctx)) == 1


@pytest.mark.parametrize("value,expected", [
    ("2022-07-30T20:00:00Z", 1659211200.0),
    ("2022-07-30T20:00:00.5Z", 1659211200.5),
    ("2022-07-30T20:00:00.123456789Z", 1659211200.123456),
    ("2022-07-30T22:00:00+02:00", 1659211200.0),
    ("2022-07-30T20:00:00", 1659211200.0),
    ("not a date", None),
    (None, None),
])
def test_parse_rfc3339(value, expected):
    result = common.parse_rfc3339(value)
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_format_rfc3339():
    assert common.format_rfc3339(1659211200.9) == "2022-07-30T20:00:00Z"


def test_build_proxies():
    assert common.build_proxies(None) is None
    assert common.build_proxies({"proxy_enabled": "0", "proxy_url": "p"}) is None
    assert common.build_proxies({"proxy_enabled": "1", "proxy_url": "proxy.corp", "proxy_port": "3128"}) == {
        "http": "http://proxy.corp:3128", "https": "http://proxy.corp:3128"}
    p = common.build_proxies({"proxy_enabled": "true", "proxy_url": "proxy.corp", "proxy_port": "8080",
                              "proxy_username": "u@x", "proxy_password": "p:w"})
    assert p["https"] == "http://u%40x:p%3Aw@proxy.corp:8080"
    with pytest.raises(common.ConfigurationError):
        common.build_proxies({"proxy_enabled": "1", "proxy_url": "", "proxy_port": ""})


def test_token_fingerprint_changes_with_client():
    a = common.token_fingerprint({"token_url": "u", "client_id": "a"})
    b = common.token_fingerprint({"token_url": "u", "client_id": "b"})
    assert a != b and len(a) == 64


def test_error_categories(caplog):
    caplog.set_level(logging.INFO)
    ctx = make_ctx(FakeClient())
    ctx.log_error(ArubaCentralAuthError("x"), "s")
    ctx.log_error(ArubaCentralApiError("x", 403), "s")
    ctx.log_error(ArubaCentralApiError("x", 502), "s")
    ctx.log_error(common.ConfigurationError("x"), "s")
    for label in ("Authentication Error", "Permission Error", "Server Error", "Configuration Error"):
        assert 'exc_l="%s"' % label in caplog.text
    assert ctx.errors == 4


def test_events_ingested_logged_for_monitoring_dashboard(caplog):
    caplog.set_level(logging.INFO)
    ctx = make_ctx(FakeClient())
    ctx.write("aruba:central:ap", [{"a": 1}], lambda _r: 1.0)
    assert "action=events_ingested" in caplog.text
    assert "sourcetype_ingested=aruba:central:ap" in caplog.text
    assert "n_events=1" in caplog.text


@pytest.mark.parametrize("exc, label", [
    (ArubaCentralAuthError("Token request rejected (HTTP 401): Invalid client"), "Authentication Error"),
    (common.ArubaCentralConnectionError("GET /x failed: ConnectTimeout"), "Connection Error"),
    (common.ConfigurationError("No account selected"), "Configuration Error"),
    (common.CheckpointError("KV store checkpoint read failed"), "KV Store Error"),
    (ArubaCentralApiError("HTTP 403 from /x", 403, "/x"), "Permission Error"),
    (ArubaCentralApiError("HTTP 503 from /x", 503, "/x"), "Server Error"),
    (ArubaCentralApiError("HTTP 404 from /x", 404, "/x"), "API Error"),
])
def test_known_errors_are_logged_on_one_line_with_reason(caplog, exc, label):
    ctx = make_ctx(FakeClient(), name="aruba_central_wireless://w1")
    with caplog.at_level(logging.ERROR):
        ctx.log_error(exc, "step1")
    (record,) = caplog.records
    line = record.getMessage()
    assert "\n" not in line
    assert line.startswith('exc_l="%s" input=aruba_central_wireless://w1 account=acc step=step1: ' % label)
    assert str(exc) in line
    assert ctx.errors == 1
