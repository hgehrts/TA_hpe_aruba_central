#!/usr/bin/env python3
"""Mock of the HPE Aruba New Central API for end-to-end tests (stdlib only).

Serves the OAuth2 client-credentials token endpoint and the GET endpoints used by
TA_hpe_aruba_central. Payload shapes follow the documented API samples
(tests/fixtures/*_documented.json); values are synthetic. Pages are capped at
PAGE_SIZE items so cursor pagination is exercised.

Environment: MOCK_CLIENT_ID, MOCK_CLIENT_SECRET, MOCK_CERT, MOCK_KEY, MOCK_PORT.
"""
import datetime as dt
import json
import os
import re
import secrets
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "e2e-client")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "e2e-secret")
PAGE_SIZE = 2
TOKENS = set()
LOCK = threading.Lock()
STATS = {}

CLUSTER = "00e6b9e8015c1e1dd8f32d83e39606377a205ba2317015ed76"


def _ap(serial, name, status, ip, cpu, mem, clients, role):
    return {
        "serialNumber": serial, "macAddress": "bb:5d:11:85:c0:%02x" % int(serial[-2:]),
        "deviceName": name, "model": "AP-515", "partNumber": "Q9H62A", "deployment": "Cluster",
        "role": role, "deviceFunction": "Instant", "firmwareVersion": "10.7.1.0_90000",
        "ipv4": ip, "status": status, "lastSeenAt": None, "siteId": "1120972196",
        "siteName": "e2e-site", "clusterName": "e2e-swarm", "clusterId": CLUSTER,
        "deviceGroupId": "8100143262", "deviceGroupName": "DeviceGroup1",
        "cpuUtilization": cpu, "memoryUtilization": mem, "powerConsumption": 11.9,
        "clientCount": clients, "id": serial, "type": "network-monitoring/access-point-monitoring",
    }


APS = [
    _ap("AP00000001", "e2e-ap-01", "ONLINE", "10.0.0.11", 25, 41, 8, "Conductor"),
    _ap("AP00000002", "e2e-ap-02", "ONLINE", "10.0.0.12", 12, 38, 3, "Member"),
    _ap("AP00000003", "e2e-ap-03", "OFFLINE", "10.0.0.13", None, None, 0, "Member"),
]
SWARMS = [{
    "clusterId": CLUSTER, "clusterName": "e2e-swarm", "siteId": "1120972196", "siteName": "e2e-site",
    "conductorDeviceName": "e2e-ap-01", "conductorSerialNumber": "AP00000001", "status": "ONLINE",
    "id": CLUSTER,
}]
DEVICES = [{"serialNumber": a["serialNumber"], "deviceName": a["deviceName"], "deviceType": "ACCESS_POINT",
            "model": a["model"], "status": a["status"], "siteName": a["siteName"], "id": a["serialNumber"]}
           for a in APS]
INVENTORY = [{"serialNumber": a["serialNumber"], "macAddress": a["macAddress"], "model": a["model"],
              "partNumber": a["partNumber"], "deviceType": "ACCESS_POINT", "id": a["serialNumber"]}
             for a in APS]
SITES_HEALTH = [{"siteId": "1120972196", "siteName": "e2e-site", "health": "GOOD", "id": "1120972196"}]
ALERTS = [
    {"id": "alert-1", "summary": "AP e2e-ap-03 is down", "severity": "Major", "status": "Active",
     "createdAt": "2026-11-12T08:00:00Z", "deviceSerialNumber": "AP00000003"},
    {"id": "alert-2", "summary": "High CPU on e2e-ap-01", "severity": "Minor", "status": "Active",
     "createdAt": "2026-11-12T08:05:00Z", "deviceSerialNumber": "AP00000001"},
]
LISTS = {
    "/network-monitoring/v1/aps": APS,
    "/network-monitoring/v1/swarms": SWARMS,
    "/network-monitoring/v1/devices": DEVICES,
    "/network-monitoring/v1/device-inventory": INVENTORY,
    "/network-monitoring/v1/sites-health": SITES_HEALTH,
    "/network-notifications/v1/alerts": ALERTS,
}
TREND_RE = re.compile(r"^/network-monitoring/v1/aps/([^/]+)/throughput-trends$")
TIME_FILTER_RE = re.compile(r"timestamp gt (\S+) and timestamp lt (\S+)")


def _parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _trend(serial, query):
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    match = TIME_FILTER_RE.search(query.get("filter", [""])[0])
    start, end = (_parse(match.group(1)), _parse(match.group(2))) if match else (now - dt.timedelta(hours=3), now)
    t = start.replace(second=0, microsecond=0)
    t -= dt.timedelta(minutes=t.minute % 5)
    samples = []
    while t <= end:
        if t > start:
            seed = int(t.timestamp()) // 300
            samples.append({"data": [1000 + seed % 500, 2000 + seed % 700], "timestamp": _fmt(t)})
        t += dt.timedelta(minutes=5)
    return {"metric": "ap_throughput", "graph": {"keys": ["tx", "rx"], "samples": samples}, "id": serial}


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-aruba/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def _send(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != "/as/token.oauth2":
            return self._send(404, {"error": "not_found"})
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        ok = (form.get("grant_type") == ["client_credentials"] and form.get("client_id") == [CLIENT_ID]
              and form.get("client_secret") == [CLIENT_SECRET])
        if not ok:
            return self._send(401, {"error": "invalid_client", "error_description": "Invalid client or secret"})
        token = secrets.token_urlsafe(24)
        with LOCK:
            TOKENS.add(token)
            STATS["token"] = STATS.get("token", 0) + 1
        return self._send(200, {"access_token": token, "token_type": "Bearer", "expires_in": 7199})

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/_stats":
            return self._send(200, STATS)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in TOKENS:
            return self._send(401, {"message": "Unauthorized"})
        with LOCK:
            STATS[url.path] = STATS.get(url.path, 0) + 1
        trend = TREND_RE.match(url.path)
        if trend:
            serial = urllib.parse.unquote(trend.group(1))
            if serial not in {a["serialNumber"] for a in APS}:
                return self._send(404, {"message": "Device not found"})
            return self._send(200, _trend(serial, query))
        items = LISTS.get(url.path)
        if items is None:
            return self._send(404, {"message": "Not found"})
        if "status eq 'ONLINE'" in query.get("filter", [""])[0]:
            items = [i for i in items if i.get("status") == "ONLINE"]
        offset = int(query.get("next", ["0"])[0] or 0)
        size = min(PAGE_SIZE, int(query.get("limit", [PAGE_SIZE])[0]))
        page = items[offset:offset + size]
        nxt = str(offset + size) if offset + size < len(items) else None
        return self._send(200, {"items": page, "count": len(page), "total": len(items), "next": nxt})


def main():
    port = int(os.environ.get("MOCK_PORT", "8443"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.environ["MOCK_CERT"], os.environ["MOCK_KEY"])
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print("mock-aruba listening on %d" % port, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
