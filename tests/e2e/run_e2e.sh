#!/usr/bin/env bash
# End-to-end test: Splunk (default 10.6.1) + mock Aruba Central API.
#   tests/e2e/run_e2e.sh [path/to/TA.tar.gz]      (build first: scripts/build.sh)
# Leaves the stack running for inspection; stop with: docker compose -f tests/e2e/docker-compose.yml down -v
set -euo pipefail
if [ -n "${1:-}" ]; then
  TA_PKG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
fi
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
TA_PKG="${TA_PKG:-$(ls -t "$ROOT"/dist/TA_hpe_aruba_central-*.tar.gz | head -1)}"
C=aruba-e2e-splunk
MGMT=https://localhost:18089

[ -f certs/ca.pem ] || ./make_certs.sh
[ -f certs/.splunk_pw ] || echo "E2e-$(openssl rand -hex 8)" > certs/.splunk_pw
SPLUNK_PASSWORD="$(cat certs/.splunk_pw)"
export SPLUNK_PASSWORD
AUTH=(-u "admin:$SPLUNK_PASSWORD")

wait_mgmt() {
  for _ in $(seq 1 90); do
    if curl -sk -o /dev/null -w "%{http_code}" "${AUTH[@]}" "$MGMT/services/server/info" 2>/dev/null | grep -q 200; then
      return 0
    fi
    sleep 5
  done
  echo "splunkd did not come up" >&2
  exit 1
}

rest() { curl -sk "${AUTH[@]}" -o /dev/null -w "%{http_code}" "$@"; }

search() {
  curl -sk "${AUTH[@]}" "$MGMT/services/search/jobs/export" -d output_mode=csv -d earliest_time=-4h \
    --data-urlencode "search=$1" | tail -n +2
}

docker compose up -d
wait_mgmt

# Trust the mock CA for the TA's requests (same mechanism as for a TLS-inspecting proxy).
docker exec -u splunk "$C" bash -c 'f=/opt/splunk/etc/splunk-launch.conf
grep -q "^REQUESTS_CA_BUNDLE=" $f || { [ -n "$(tail -c1 $f)" ] && echo >> $f; echo "REQUESTS_CA_BUNDLE=/certs/ca.pem" >> $f; }'

docker cp "$TA_PKG" "$C:/tmp/ta.tgz"
docker exec -u root "$C" chown splunk /tmp/ta.tgz
docker exec -u splunk "$C" /opt/splunk/bin/splunk install app /tmp/ta.tgz -update 1 -auth "admin:$SPLUNK_PASSWORD" 2>&1 \
  | grep -v "^WARNING"
docker restart "$C" >/dev/null
sleep 20
wait_mgmt

B="$MGMT/servicesNS/nobody/TA_hpe_aruba_central"
ACCOUNT=(-d base_url=https://mock-aruba:8443 -d token_url=https://mock-aruba:8443/as/token.oauth2
         -d client_id=e2e-client -d max_rps=5)
body=$(curl -sk "${AUTH[@]}" "$B/TA_hpe_aruba_central_account?output_mode=json" -d name=e2e_bad "${ACCOUNT[@]}" \
  -d client_secret=wrong)
echo "$body" | grep -q "Token request rejected" || { echo "FAIL: wrong secret not rejected: $body" >&2; exit 1; }
echo "$body" | grep -q "wrong" && { echo "FAIL: secret echoed in error message" >&2; exit 1; }
code=$(rest "$B/TA_hpe_aruba_central_account/e2e")
if [ "$code" != 200 ]; then
  code=$(rest "$B/TA_hpe_aruba_central_account" -d name=e2e "${ACCOUNT[@]}" -d client_secret=e2e-secret)
  [ "$code" = 201 ] || { echo "FAIL: account creation ($code)" >&2; exit 1; }
fi

create_input() {  # type interval [extra -d args]
  local type=$1 interval=$2
  shift 2
  if [ "$(rest "$B/TA_hpe_aruba_central_$type/e2e_${type#aruba_central_}")" != 200 ]; then
    rest "$B/TA_hpe_aruba_central_$type" -d name="e2e_${type#aruba_central_}" -d account=e2e -d index=main \
      -d interval="$interval" "$@" >/dev/null
  fi
  rest "$B/TA_hpe_aruba_central_$type/e2e_${type#aruba_central_}" -d disabled=0 >/dev/null
}
create_input aruba_central_wireless 60
create_input aruba_central_ap_throughput 300 -d interface_type=WIRELESS
create_input aruba_central_inventory 300
create_input aruba_central_health 60

echo "waiting for the first collection runs..."
sleep 90

fail=0
for st in ap swarm ap_throughput device device_inventory site_health alert; do
  n=$(search "search index=main sourcetype=aruba:central:$st | stats count" | tr -d '"')
  printf "%-32s %s\n" "aruba:central:$st" "${n:-0}"
  [ "${n:-0}" -gt 0 ] || fail=1
done
dups=$(search 'search index=main sourcetype=aruba:central:ap_throughput | stats count by serialNumber _time | where count>1 | stats count' | tr -d '"')
echo "duplicate throughput samples:   ${dups:-0}"
[ "${dups:-0}" = 0 ] || fail=1
errs=$(search 'search index=_internal source=*ta_hpe_aruba_central_*_e2e_* log_level=ERROR | stats count' | tr -d '"')
echo "TA errors in _internal:         ${errs:-0}"
[ "${errs:-0}" = 0 ] || fail=1

[ "$fail" = 0 ] && echo "E2E PASSED" || { echo "E2E FAILED" >&2; exit 1; }
