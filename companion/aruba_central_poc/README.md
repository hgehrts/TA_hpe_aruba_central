# Aruba Central POC Dashboards (`aruba_central_poc`)

Search-head app with the dashboard **Aruba Central overview** for data collected by `TA_hpe_aruba_central`:
online/offline APs, wireless sessions (`clientCount`), AP table with swarm conductor, CPU/memory, AP
throughput and top 10 APs.

Requires `TA_hpe_aruba_central` on the search head (search-time knowledge) and the inputs Wireless and
AP Throughput.

Set the index in the macro `aruba_central_index` (default `index=main`), e.g. in
`local/macros.conf`:

```
[aruba_central_index]
definition = index=aruba
```
