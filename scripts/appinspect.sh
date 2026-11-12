#!/usr/bin/env bash
# AppInspect (precert, cloud + splunk_appinspect tags) for every package in dist/; fails on error/failure.
set -euo pipefail
cd "$(dirname "$0")/.."
APPINSPECT="${APPINSPECT:-.venv/bin/splunk-appinspect}"
PY="${PY:-.venv/bin/python}"
status=0
for pkg in dist/*.tar.gz; do
  report="dist/$(basename "$pkg" .tar.gz).appinspect.json"
  "$APPINSPECT" inspect "$pkg" --mode precert --included-tags cloud --included-tags splunk_appinspect \
    --data-format json --output-file "$report" >/dev/null
  "$PY" - "$pkg" "$report" <<'EOF' || status=1
import json, sys
pkg, report = sys.argv[1], sys.argv[2]
summary = json.load(open(report))["summary"]
bad = {k: summary.get(k, 0) for k in ("error", "failure", "future_failure")}
print("%s: %s warnings=%s" % (pkg, bad, summary.get("warning", 0)))
sys.exit(1 if any(bad.values()) else 0)
EOF
done
exit "$status"
