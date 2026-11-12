# HPE Aruba Central Add-on for Splunk (`TA_hpe_aruba_central`)

Collects access point, swarm, AP throughput, device inventory, site health and alert data from the
**HPE Aruba Networking New Central (GreenLake)** REST API. Built with UCC Framework 6.6.1.

| | |
|---|---|
| Version | 1.3.0 |
| Splunk | Enterprise 10.x (tested on 10.6.1.1); Splunk Cloud (AppInspect 4.4.0 precert/cloud checks: 0 failures). Older releases are untested. |
| Python | 3.13 and 3.9 (`python.required = 3.9, 3.13`); both runtimes tested inside Splunk 10.6.1.1 |
| API | New Central only. Classic Aruba Central (`apigw-*.central.arubanetworks.com`, `/monitoring/v1|v2`) is **not** supported. |
| Companion app | `aruba_central_poc` (POC dashboards, optional, search head only) |

> **Status of field mapping:** field names are based on the documented API sample payloads (HPE API
> reference) and were verified end-to-end against a mock of that API. They have not yet been compared with
> a real tenant. Run the probe (see [Verify against a real tenant](#verify-against-a-real-tenant)) before
> production use.

## Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [Data collected](#data-collected)
- [CIM](#cim)
- [Sizing and API rate limit](#sizing-and-api-rate-limit)
- [Troubleshooting](#troubleshooting)
- [Secret rotation](#secret-rotation)
- [Verify against a real tenant](#verify-against-a-real-tenant)
- [Known limitations](#known-limitations)
- [Development](#development)

## Installation

| Tier | Install | Why |
|---|---|---|
| Heavy forwarder (or the single instance) | `TA_hpe_aruba_central` | Runs the modular inputs. Needs Python, outbound HTTPS and the **KV store** (checkpoints). |
| Search head | `TA_hpe_aruba_central` | Search-time knowledge (field aliases, eventtypes, tags). Inputs stay disabled. |
| Search head (optional) | `aruba_central_poc` | POC dashboard. |
| Indexers | not required | Events are parsed on the heavy forwarder. |

Splunk Cloud: upload the package as a private app. In Victoria Experience, modular inputs run on the search
head tier. The KV store requirement applies there as well.

1. Install the package (`Apps > Manage Apps > Install app from file`, or `splunk install app <file>`), restart.
2. Create an index (for example `aruba`). The inputs default to `index = default`.
3. The KV store must be running on the instance that runs the inputs (`splunk show kvstore-status`).

Outbound access from the input host: the token URL (`sso.common.cloud.hpe.com:443`) and the API base URL
(for example `de3.api.central.arubanetworks.com:443`).

## Configuration

### New Central API client

In New Central / HPE GreenLake create a **Personal API client** with read access to network monitoring and
notifications. The client page shows the **client ID**, the **client secret** (only once) and the API
gateway (**base URL**) of the cluster.

### Account (`Configuration > Account`)

| Field | Default | Notes |
|---|---|---|
| API Base URL | `https://de3.api.central.arubanetworks.com` | Exactly as shown on the API client page. Other clusters, e.g. `de1`, `de2`, `gb1`. |
| Token URL | `https://sso.common.cloud.hpe.com/as/token.oauth2` | OAuth2 client credentials grant. |
| Client ID / Client Secret | | The secret is stored encrypted in `storage/passwords`. |
| Max requests per second | `2` | Applied per input. See [Sizing](#sizing-and-api-rate-limit). |

Saving the account requests a token and calls `GET /network-monitoring/v1/aps?limit=1`. Typical messages:

| Message | Meaning |
|---|---|
| `Token request rejected. Check client ID, client secret and token URL.` | Wrong client ID/secret or token URL. |
| `Token accepted by SSO but rejected by the API ... belongs to the cluster ...` | Base URL is not the cluster of this API client. |
| `API client has no permission for Network Monitoring (HTTP 403).` | Missing role/scope on the API client. |
| `Cannot reach the token URL / API Base URL (DNS, proxy, TLS or timeout).` | Network, proxy or TLS problem. |

### Proxy and logging

`Configuration > Proxy`: HTTP proxy (CONNECT for HTTPS) with optional credentials. `Configuration > Logging`:
log level for all inputs.

TLS-inspecting proxies: the add-on verifies certificates with its bundled CA list (certifi). To trust a
corporate CA, add `REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem` to `$SPLUNK_HOME/etc/splunk-launch.conf` and
restart. This affects every Python `requests` call of that Splunk instance; the bundle must therefore also
contain the public CAs (e.g. append the corporate CA to a copy of the system bundle). TLS verification
cannot be disabled in the add-on.

### Inputs (`Inputs`)

New inputs are created **disabled**; enable them after review.

| Input | Default interval (range) | API calls per run | Sourcetypes |
|---|---|---|---|
| Wireless (APs and Swarms) | 60 s (60–86400) | `ceil(APs/1000) + ceil(swarms/100)` | `aruba:central:ap`, `aruba:central:swarm` |
| AP Throughput | 900 s (60–10800) | `ceil(online APs/1000) + online APs` | `aruba:central:ap_throughput` |
| Inventory (Devices) | 3600 s (300–86400) | `ceil(inventory/100) + ceil(devices/100)` | `aruba:central:device_inventory`, `aruba:central:device` |
| Site Health and Alerts | 300 s (60–86400) | `ceil(sites/100) + ceil(alerts/50)` | `aruba:central:site_health`, `aruba:central:alert` |

CPU and memory are `cpuUtilization` and `memoryUtilization` on each access point, collected by the Wireless
input. They are a point-in-time snapshot, so the default interval is 60 seconds.

AP Throughput has an `Interface type` (`WIRELESS` default, `WIRED`, `LTE`). Changing it resets the input's
checkpoint. The API returns 5-minute samples, so an interval below 300 seconds does not add resolution. It
only notices the next sample sooner and costs one API call per online AP per run. The default stays 900
seconds. 60 seconds is allowed.

## Data collected

One event per API item, JSON, `host` = API host name, plus `vendor`, `product`, `vendor_product`.

| Sourcetype | Endpoint | `_time` | Key fields |
|---|---|---|---|
| `aruba:central:ap` | `GET /network-monitoring/v1/aps` | poll time | `serialNumber`, `deviceName`, `status`, `clientCount`, `cpuUtilization`, `memoryUtilization`, `siteName`, `clusterId`, `clusterName`, `role`, `firmwareVersion`, `ipv4`, `macAddress`, `model` |
| `aruba:central:swarm` | `GET /network-monitoring/v1/swarms` | poll time | `clusterId`, `clusterName`, `conductorDeviceName`, `conductorSerialNumber` |
| `aruba:central:ap_throughput` | `GET /network-monitoring/v1/aps/{serial}/throughput-trends` | sample `timestamp` | `serialNumber`, `deviceName`, `siteName`, `clusterName`, `interfaceType`, `tx`, `rx` (bit/s per API reference), `timestamp` |
| `aruba:central:device_inventory` | `GET /network-monitoring/v1/device-inventory` | poll time | as returned by the API |
| `aruba:central:device` | `GET /network-monitoring/v1/devices` | poll time | as returned by the API |
| `aruba:central:site_health` | `GET /network-monitoring/v1/sites-health` | poll time | as returned by the API |
| `aruba:central:alert` | `GET /network-notifications/v1/alerts` | poll time | as returned by the API |

Behaviour:

- **Snapshots** (AP, swarm, inventory, device, site health): every run writes the full list. Use
  `latest()` per `serialNumber` for the current state.
- **AP throughput** is incremental. A KV-store checkpoint per AP stores the newest sample time; only newer
  samples are written (no duplicates, also across restarts and upgrades). Only `ONLINE` APs are queried.
  The API keeps about 3 hours of history: if the input is stopped for longer, the gap cannot be backfilled.
- **Alerts**: only new or changed alerts are written (content hash stored in the KV store). While the KV
  store is unavailable (e.g. for about a minute after a Splunk restart) alert collection is skipped for that
  run instead of re-ingesting every open alert.
- **Swarm conductor per AP**: join `aruba:central:ap` and `aruba:central:swarm` on `clusterId`
  (the companion dashboard does this).
- Deleting an input deletes its checkpoint.

## CIM

Mapped for `aruba:central:ap` (eventtype `aruba_central_ap`):

| Data model | Tags | Fields |
|---|---|---|
| Inventory (Network) | `inventory`, `network` | `dest`, `serial`, `mac`, `ip`, `version`, `name`, `vendor_product` |
| Performance (CPU) | `performance`, `cpu` | `cpu_load_percent` (from `cpuUtilization`, numeric only) |

`memoryUtilization` is a percentage and is **not** mapped to `mem_used` (CIM expects MB). The other
sourcetypes are not CIM-mapped yet; this is planned once real tenant payloads are available.

## Sizing and API rate limit

New Central allows about **10 requests per second per account** (shared by every tool that uses the same
GreenLake account). Each input has its own limiter (`Max requests per second`); inputs running at the same
time add up. Keep `max_rps × concurrently running inputs` well below 10.

- HTTP 429 is retried honouring `Retry-After` (max 120 s per wait, 5 retries); 5xx and connection errors are
  retried with exponential backoff.
- AP Throughput dominates: one call per online AP. Run time ≈ `online APs / max_rps` seconds
  (1000 APs at 2 req/s ≈ 500 s). Keep the interval above the run time: raise `max_rps` or the interval for
  large estates.
- Tokens are valid for about 2 hours and are shared by all inputs of an account (cached encrypted in
  `storage/passwords`, realm `TA_hpe_aruba_central_token_cache`).

Event volume per run is roughly one event per AP, swarm, device, inventory item and site, plus throughput
samples (one per AP per 5 minutes in the API samples).

## Troubleshooting

Logs: `$SPLUNK_HOME/var/log/splunk/ta_hpe_aruba_central_<input type>_<input name>.log`
(e.g. `ta_hpe_aruba_central_wireless_central_de3.log`) and
`ta_hpe_aruba_central_account_validation.log`. The **Documentation** page in the add-on menu describes the
account, the inputs and these logs. The **Dashboard** page shows ingestion and errors from the logs.

`DEBUG` writes one line per HTTP request (`action=request` with method, path, status, duration and attempt)
and never the token, the secret or a response body. `INFO` keeps the run summary. `WARNING` keeps retries
only. `ERROR` keeps failures only. The same level applies to account validation.

```
index=_internal source=*ta_hpe_aruba_central_* (log_level=ERROR OR log_level=WARNING)
index=_internal source=*ta_hpe_aruba_central_* action=run_summary
| table _time modular_input_name api_requests errors duration_s
```

Errors are logged on one line as `exc_l="<category>" input=... account=... step=<endpoint>: <reason>`:

| `exc_l` | Typical cause / action |
|---|---|
| `Authentication Error` | Secret rotated or wrong (`Token request rejected (HTTP 401)`), or token rejected by the API after one refresh. Update the account. |
| `Permission Error` | HTTP 403: API client lacks the role for this endpoint. |
| `Connection Error` | DNS, proxy, TLS (see TLS-inspecting proxies) or timeout after retries. |
| `Server Error` | HTTP 5xx after retries. |
| `API Error` | Other HTTP 4xx, e.g. 404 for an AP that disappeared, or 429 after all retries. |
| `KV Store Error` | KV store not ready (HTTP 503 right after a restart) or disabled. Throughput resumes from its checkpoint on the next run. |
| `Configuration Error` | Input without account, or account missing. |
| `Unexpected Error` | Bug; the log contains a traceback. |

## Secret rotation

1. Create the new secret on the API client page in New Central.
2. `Configuration > Account > Edit`, enter the new secret, save. The add-on validates it and drops the
   cached token, so all inputs use the new secret from their next run.
3. Revoke the old secret.

Until step 2 is done, inputs log `Authentication Error` and collect nothing; they recover automatically
afterwards (verified in the end-to-end test). Secrets used for testing or shared in tickets/chats must be
rotated after the test.

## Verify against a real tenant

`tools/probe_aruba_central.py` is a read-only probe (Python standard library only, only GET requests plus
the token request). It writes masked sample responses to `probe_out/` (serials, MACs, IPs, names, users,
e-mails replaced by pseudonyms; numbers and timestamps kept). Credentials are read from environment
variables only:

```bash
export ARUBA_CLIENT_ID=...
read -rs ARUBA_CLIENT_SECRET && export ARUBA_CLIENT_SECRET
export ARUBA_BASE_URL=https://de3.api.central.arubanetworks.com   # optional
python3 tools/probe_aruba_central.py --out probe_out
```

Options: `--ap-serial` (AP for the throughput call), `--ca-bundle`, `--limit`, `--burst-test` (sends 25
parallel requests to observe HTTP 429 — the limit is shared by the whole account, use on test tenants only).
Review `probe_out/` before sharing it.

## Known limitations

- Field names and the throughput unit (bit/s) come from the API reference samples, not yet from a real
  tenant (see above). CIM mapping covers `aruba:central:ap` only.
- Only HTTP proxies; SOCKS is not offered.
- `splunk-sdk` is pinned to 2.1.1 because 3.x requires Python 3.13 and would break the Python 3.9 runtime.
  AppInspect reports this as a warning.
- AppInspect warns about `appserver/templates/base.html` (deprecated Mako template). The file is generated by
  UCC 6.6.1 itself.
- The KV store is required on the input host; there is no file-based checkpoint fallback.

## Development

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python3.9 -m venv .venv-build39 && .venv-build39/bin/pip install -r requirements-test39.txt
.venv/bin/ruff check package/bin tests tools additional_packaging.py
.venv/bin/python -m pytest --cov=package/bin tests          # same with .venv-build39/bin/python
scripts/build.sh                                            # -> dist/*.tar.gz (libs installed with Python 3.9)
scripts/appinspect.sh                                       # precert + cloud checks, fails on failures
tests/e2e/run_e2e.sh dist/TA_hpe_aruba_central-1.3.0.tar.gz # Docker: Splunk 10.6.1 + mock API
```

- `scripts/build.sh [version]` writes the version into `globalConfig.json` (UCC behaviour); reset it when
  building test versions.
- Third-party libraries are pure Python. `package/lib/exclude.txt` drops grpcio/protobuf/OpenTelemetry
  (only used by an unused solnlib module); the build fails if any compiled binary ends up in the package.
- The end-to-end test runs a TLS mock of the documented API (`tests/e2e/mock_aruba_api.py`, synthetic
  values) and checks all sourcetypes, throughput de-duplication and the absence of errors. It leaves the
  stack running: `docker compose -f tests/e2e/docker-compose.yml down -v`.
- CI: `.github/workflows/ci.yml` (lint, tests on 3.9 and 3.13, build, AppInspect; E2E on manual dispatch).
