# Aruba Central POC Dashboards (`aruba_central_poc`)

Search-head app with Dashboard Studio views for data collected by `TA_hpe_aruba_central`: overview,
access points, one access point, throughput, devices, sites and alerts. The views use Splunk's dark
theme. Studio keeps the theme stored in the view and does not switch it with the user preference.
Search and the app chrome follow the user because the app declares light and dark. Rows and single
values drill down between the views.

Requires `TA_hpe_aruba_central` on the search head.

Set the index in the macro `aruba_central_index` (default `index=main`), for example in
`local/macros.conf`:

```
[aruba_central_index]
definition = index=aruba
```
