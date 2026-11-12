# Changelog

## 1.3.0

- Companion app is now `aruba_central_poc`. Install it as a new app
  and remove the old one. Its dashboards and drilldowns are unchanged.

## 1.2.0

- App icon is the divi-divi tree (*Libidibia coriaria*).
- The Documentation page shows the same picture. Clicking it opens
  <https://en.wikipedia.org/wiki/Libidibia_coriaria> in a new window.
- The add-on is licensed under Apache-2.0.

## 1.1.0

- Wireless input defaults to 60 seconds. CPU and memory are point-in-time values from that input.
- AP Throughput accepts 60 seconds. The default stays 900 seconds because the API returns 5-minute samples
  and each run calls the API once per online AP.
- DEBUG logs every HTTP request without the token, the secret or the response body. INFO, WARNING and ERROR
  hide that line. Account validation uses the same log level.
- The add-on menu has a Documentation page next to Configuration.
- Companion app 1.1.0 replaces the Simple XML overview with Dashboard Studio dashboards for access points,
  one access point, throughput, devices, sites and alerts, with drilldowns between them. The dashboards
  and the Documentation page use the dark theme. Studio does not switch a dashboard when the user
  changes their preference. The companion app declares light and dark, so Search and the app chrome follow
  the user.

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
