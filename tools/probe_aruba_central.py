#!/usr/bin/env python3
"""Read-only probe for the HPE Aruba New Central (GreenLake) monitoring API.

Collects masked sample responses for every endpoint used by TA_hpe_aruba_central
so that field names, timestamp fields and pagination can be verified before the
add-on is finalised.

Only HTTP GET requests are sent to the Central API (plus one POST to the SSO
token endpoint). Python standard library only.

Credentials are read from environment variables, never from the command line:

    export ARUBA_CLIENT_ID=...
    export ARUBA_CLIENT_SECRET=...
    export ARUBA_BASE_URL=https://de3.api.central.arubanetworks.com   # optional
    export ARUBA_TOKEN_URL=https://sso.common.cloud.hpe.com/as/token.oauth2  # optional
    python3 tools/probe_aruba_central.py --out probe_out

Masking: serial numbers, MAC addresses, IP addresses, device/site/cluster names,
host names, user names and e-mail addresses are replaced with deterministic
pseudonyms (same input -> same pseudonym within one run, random salt per run).
Structure, data types, numbers and timestamps are preserved. Review the output
before sharing it anyway.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://de3.api.central.arubanetworks.com"
DEFAULT_TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
USER_AGENT = "TA_hpe_aruba_central-probe/1.0"

LIST_ENDPOINTS = [
    ("aps", "/network-monitoring/v1/aps"),
    ("swarms", "/network-monitoring/v1/swarms"),
    ("devices", "/network-monitoring/v1/devices"),
    ("device_inventory", "/network-monitoring/v1/device-inventory"),
    ("sites_health", "/network-monitoring/v1/sites-health"),
    ("alerts", "/network-notifications/v1/alerts"),
]

SAFE_HEADER_DENYLIST = {"set-cookie", "authorization"}

MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(
    r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b"
    r"|\b(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?\b"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")

SENSITIVE_KEY_PARTS = ("serial", "name", "hostname", "user", "email", "mac", "ip")
KEY_EXCEPTIONS = {
    "type", "status", "model", "partnumber", "role", "devicefunction", "deployment",
    "firmwareversion", "lastrebootreason", "ipv4mode", "ipaddressingmode",
    "softwareversion", "version", "osversion", "next",
}


class Masker:
    def __init__(self):
        self._salt = secrets.token_bytes(16)
        self._cache = {}

    def _digest(self, kind, value):
        key = (kind, value)
        if key not in self._cache:
            self._cache[key] = hashlib.sha256(self._salt + kind.encode() + value.encode()).hexdigest()
        return self._cache[key]

    def mac(self, value):
        d = self._digest("mac", value.lower())
        sep = "-" if "-" in value else ":"
        parts = ["02"] + [d[i:i + 2] for i in range(0, 10, 2)]
        return sep.join(parts)

    def ipv4(self, value):
        d = self._digest("ipv4", value)
        return "10.%d.%d.%d" % (int(d[0:2], 16), int(d[2:4], 16), int(d[4:6], 16) or 1)

    def ipv6(self, value):
        d = self._digest("ipv6", value.lower())
        return "fd00:%s:%s:%s::%s" % (d[0:4], d[4:8], d[8:12], d[12:16])

    def token(self, kind, value):
        d = self._digest(kind, value)
        if kind == "serial":
            length = max(len(value), 6)
            return ("SN" + d.upper())[:length]
        return "%s-%s" % (kind, d[:10])

    def scrub_string(self, value):
        value = TOKEN_RE.sub("<redacted-token>", value)
        value = EMAIL_RE.sub(lambda m: self.token("email", m.group(0)) + "@example.invalid", value)
        value = MAC_RE.sub(lambda m: self.mac(m.group(0)), value)
        value = IPV6_RE.sub(lambda m: self.ipv6(m.group(0)), value)
        value = IPV4_RE.sub(lambda m: self.ipv4(m.group(0)), value)
        return value

    def by_key(self, key, value):
        k = key.lower().replace("_", "").replace("-", "")
        if k in KEY_EXCEPTIONS or not isinstance(value, str) or not value:
            return None
        if "serial" in k:
            return self.token("serial", value)
        if "mac" in k and MAC_RE.fullmatch(value):
            return self.mac(value)
        if k.startswith("ip") or k.endswith("ip") or "ipv4" in k or "ipv6" in k or "ipaddress" in k:
            if IPV4_RE.fullmatch(value):
                return self.ipv4(value)
            if ":" in value:
                return self.ipv6(value)
        if "name" in k or "hostname" in k:
            return self.token(k.replace("name", "") or "name", value)
        if "user" in k or "email" in k:
            return self.token("user", value)
        return None

    def walk(self, obj, key=None):
        if isinstance(obj, dict):
            return {k: self.walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key is not None and key.lower().replace("_", "").replace("-", "") in KEY_EXCEPTIONS:
                return obj
            if key is not None:
                masked = self.by_key(key, obj)
                if masked is not None:
                    return masked
            return self.scrub_string(obj)
        return obj


def field_types(items):
    """Union of top-level field names -> sorted list of observed JSON types."""
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            t = type(v).__name__
            if t == "NoneType":
                t = "null"
            result.setdefault(k, set()).add(t)
    return {k: sorted(v) for k, v in sorted(result.items())}


class Probe:
    def __init__(self, base_url, token_url, client_id, client_secret, timeout, ssl_ctx, masker):
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.ssl_ctx = ssl_ctx
        self.masker = masker
        self.access_token = None

    def _request(self, method, url, headers=None, body=None):
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        req.add_header("User-Agent", USER_AGENT)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                status, resp_headers, raw = resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:
            status, resp_headers, raw = exc.code, dict(exc.headers.items()), exc.read()
        except (urllib.error.URLError, OSError) as exc:
            return {"status": None, "error": "%s: %s" % (type(exc).__name__, exc),
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        latency_ms = int((time.monotonic() - started) * 1000)
        headers_out = {k: v for k, v in resp_headers.items() if k.lower() not in SAFE_HEADER_DENYLIST}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            payload = {"_non_json_body_prefix": raw[:500].decode("utf-8", "replace")}
        return {"status": status, "latency_ms": latency_ms, "headers": headers_out,
                "body": payload, "body_bytes": len(raw)}

    def get_token(self):
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()
        result = self._request("POST", self.token_url,
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Accept": "application/json"},
                               body=body)
        payload = result.get("body") if isinstance(result.get("body"), dict) else {}
        self.access_token = payload.get("access_token")
        summary = {
            "status": result.get("status"),
            "latency_ms": result.get("latency_ms"),
            "error": result.get("error"),
            "response_keys": sorted(payload.keys()),
            "token_type": payload.get("token_type"),
            "expires_in": payload.get("expires_in"),
            "scope": payload.get("scope"),
            "error_description": payload.get("error_description") or payload.get("error"),
        }
        return summary

    def get(self, path, params=None, token=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json",
                   "Authorization": "Bearer %s" % (token if token is not None else self.access_token)}
        result = self._request("GET", url, headers=headers)
        result["request"] = {"path": path, "params": params or {}}
        return result


def items_of(body):
    if isinstance(body, dict):
        for key in ("items", "data", "result", "results"):
            if isinstance(body.get(key), list):
                return key, body[key]
    if isinstance(body, list):
        return "<root>", body
    return None, []


def describe(result):
    body = result.get("body")
    items_key, items = items_of(body)
    wrapper = {}
    if isinstance(body, dict):
        wrapper = {k: (v if not isinstance(v, (list, dict)) else "<%s>" % type(v).__name__)
                   for k, v in body.items() if k != items_key}
    return {
        "status": result.get("status"),
        "error": result.get("error"),
        "latency_ms": result.get("latency_ms"),
        "body_bytes": result.get("body_bytes"),
        "request": result.get("request"),
        "headers": result.get("headers"),
        "items_key": items_key,
        "items_returned": len(items),
        "wrapper_fields": wrapper,
        "item_fields": field_types(items),
    }


def iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="probe_out", help="output directory (default: probe_out)")
    parser.add_argument("--limit", type=int, default=5, help="page size for list endpoints (default: 5)")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds (default: 60)")
    parser.add_argument("--ca-bundle", help="optional CA bundle file for TLS verification")
    parser.add_argument("--ap-serial", help="AP serial for throughput-trends (default: first AP returned)")
    parser.add_argument("--burst-test", action="store_true",
                        help="send 25 parallel GET /aps?limit=1 requests to observe HTTP 429 and rate-limit "
                             "headers. The limit is shared by the whole Central account; only use on a test tenant.")
    args = parser.parse_args()

    client_id = os.environ.get("ARUBA_CLIENT_ID")
    client_secret = os.environ.get("ARUBA_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("ARUBA_CLIENT_ID and ARUBA_CLIENT_SECRET must be set as environment variables.")
    base_url = os.environ.get("ARUBA_BASE_URL", DEFAULT_BASE_URL)
    token_url = os.environ.get("ARUBA_TOKEN_URL", DEFAULT_TOKEN_URL)

    ssl_ctx = ssl.create_default_context(cafile=args.ca_bundle) if args.ca_bundle else ssl.create_default_context()
    masker = Masker()
    probe = Probe(base_url, token_url, client_id, client_secret, args.timeout, ssl_ctx, masker)
    os.makedirs(args.out, exist_ok=True)

    def save(name, result):
        masked = dict(result)
        masked["body"] = masker.walk(result.get("body"))
        if masked.get("request"):
            masked["request"] = masker.walk(result["request"])
        with open(os.path.join(args.out, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(masked, fh, indent=2, sort_keys=False)
        return describe(masked)

    summary = {
        "probe_started_utc": iso_utc(datetime.datetime.now(datetime.timezone.utc)),
        "base_url": base_url,
        "token_url": token_url,
        "python": sys.version.split()[0],
        "endpoints": {},
    }

    print("[token] POST %s" % token_url)
    summary["token"] = probe.get_token()
    print("  -> %s expires_in=%s" % (summary["token"]["status"], summary["token"]["expires_in"]))
    if not probe.access_token:
        _write_summary(args.out, summary)
        sys.exit("Token request failed; see %s/summary.json" % args.out)

    first_ap_serial = None
    for name, path in LIST_ENDPOINTS:
        print("[%s] GET %s" % (name, path))
        result = probe.get(path, {"limit": args.limit})
        if name == "aps" and result.get("status") == 200:
            _, ap_items = items_of(result.get("body"))
            if ap_items and isinstance(ap_items[0], dict):
                first_ap_serial = ap_items[0].get("serialNumber")
        summary["endpoints"][name] = save(name, result)
        print("  -> %s (%s ms, %s items)" % (result.get("status"), result.get("latency_ms"),
                                            summary["endpoints"][name]["items_returned"]))

        body = result.get("body")
        next_cursor = body.get("next") if isinstance(body, dict) else None
        if result.get("status") == 200 and next_cursor not in (None, "", 0):
            print("[%s_page2] GET %s next=<cursor>" % (name, path))
            page2 = probe.get(path, {"limit": args.limit, "next": next_cursor})
            summary["endpoints"][name + "_page2"] = save(name + "_page2", page2)
            summary["endpoints"][name + "_page2"]["next_cursor_type"] = type(next_cursor).__name__
            print("  -> %s" % page2.get("status"))

    ap_serial = args.ap_serial or first_ap_serial
    if ap_serial:
        tp_path = "/network-monitoring/v1/aps/%s/throughput-trends" % urllib.parse.quote(ap_serial, safe="")
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        variants = [
            ("throughput_wireless", {"interface-type": "WIRELESS"}),
            ("throughput_wireless_filtered", {
                "interface-type": "WIRELESS",
                "filter": "timestamp gt %s and timestamp lt %s" % (
                    iso_utc(now - datetime.timedelta(hours=1)), iso_utc(now)),
            }),
            ("throughput_wired", {"interface-type": "WIRED"}),
        ]
        for name, params in variants:
            print("[%s] GET throughput-trends %s" % (name, params))
            result = probe.get(tp_path, params)
            summary["endpoints"][name] = save(name, result)
            print("  -> %s (%s ms)" % (result.get("status"), result.get("latency_ms")))
    else:
        summary["endpoints"]["throughput_wireless"] = {"skipped": "no AP serial available"}

    print("[unauthorized] GET /aps with invalid token")
    bad = probe.get("/network-monitoring/v1/aps", {"limit": 1}, token="invalid-token")
    summary["endpoints"]["unauthorized"] = save("unauthorized", bad)
    print("  -> %s" % bad.get("status"))

    if args.burst_test:
        print("[burst] 25 parallel GET /aps?limit=1")
        results = []
        lock = threading.Lock()

        def worker():
            r = probe.get("/network-monitoring/v1/aps", {"limit": 1})
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = {}
        for r in results:
            statuses[str(r.get("status"))] = statuses.get(str(r.get("status")), 0) + 1
        throttled = [r for r in results if r.get("status") == 429]
        summary["burst_test"] = {
            "statuses": statuses,
            "sample_429": (describe(dict(throttled[0], body=masker.walk(throttled[0].get("body"))))
                           if throttled else None),
            "sample_200_headers": next((r.get("headers") for r in results if r.get("status") == 200), None),
        }
        print("  -> %s" % statuses)

    _write_summary(args.out, summary)
    print("Done. Review %s/ before sharing." % args.out)


def _write_summary(out_dir, summary):
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)


if __name__ == "__main__":
    main()
