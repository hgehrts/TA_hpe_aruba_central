# Changelog

## 1.0.0

First release for HPE Aruba Networking New Central (GreenLake API). Not an upgrade of the classic
Aruba Central add-on: new app ID, new sourcetypes, new configuration.

- Account with OAuth2 client credentials (encrypted secret), validation on save, shared token cache.
- Inputs: Wireless (APs, swarms), AP Throughput (incremental, KV-store checkpoint per AP),
  Inventory (device inventory, devices), Site Health and Alerts (alerts de-duplicated by content hash).
- Cursor pagination, per-input rate limiter, retries for HTTP 429 (`Retry-After`), 5xx and connection errors.
- HTTP proxy support, logging tab, UCC monitoring dashboard, one-line error logs with `exc_l` categories.
- CIM: Inventory (Network) and Performance (CPU) for `aruba:central:ap`.
- Python 3.9 and 3.13; pure-Python dependencies only.
- Companion app `aruba_central_poc` 1.0.0 with the POC overview dashboard.
