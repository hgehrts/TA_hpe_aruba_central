"""Input aruba_central_ap_throughput: per-AP throughput trend samples (incremental)."""

import time
import urllib.parse

from splunklib import modularinput as smi

from aruba_central_client import (
    ArubaCentralApiError,
    ArubaCentralAuthError,
    ArubaCentralConnectionError,
)
from aruba_central_common import (
    checkpoint_key,
    format_rfc3339,
    get_checkpointer,
    parse_rfc3339,
    read_checkpoint,
    run_inputs,
    write_checkpoint,
)

AP_PATH = "/network-monitoring/v1/aps"
TREND_PATH = "/network-monitoring/v1/aps/%s/throughput-trends"
AP_PAGE_SIZE = 1000
INTERFACE_TYPES = ("WIRELESS", "WIRED", "LTE")
MAX_LOOKBACK_S = 3 * 3600
CHECKPOINT_EVERY = 50
MAX_CONSECUTIVE_FAILURES = 3
AP_CONTEXT_FIELDS = ("serialNumber", "deviceName", "siteId", "siteName", "clusterId", "clusterName")


def samples_to_records(ap, interface_type, body, since_epoch=None):
    """Flatten {graph: {keys: [...], samples: [{timestamp, data: [...]}]}} into one record per sample.

    Returns (records, newest_epoch). Samples at or before since_epoch are dropped.
    """
    graph = body.get("graph") if isinstance(body, dict) else None
    if not isinstance(graph, dict):
        return [], None
    keys = graph.get("keys") or []
    records = []
    newest = None
    for sample in graph.get("samples") or []:
        if not isinstance(sample, dict):
            continue
        ts = parse_rfc3339(sample.get("timestamp"))
        if ts is None or (since_epoch is not None and ts <= since_epoch):
            continue
        record = {field: ap.get(field) for field in AP_CONTEXT_FIELDS if ap.get(field) is not None}
        record["interfaceType"] = interface_type
        if body.get("metric") is not None:
            record["metric"] = body.get("metric")
        record["timestamp"] = sample.get("timestamp")
        data = sample.get("data") or []
        for i, key in enumerate(keys):
            record[key] = data[i] if i < len(data) else None
        records.append(record)
        newest = ts if newest is None or ts > newest else newest
    return records, newest


def trend_params(interface_type, since_epoch, now):
    params = {"interface-type": interface_type}
    if since_epoch is not None:
        start = max(since_epoch, now - MAX_LOOKBACK_S)
        params["filter"] = "timestamp gt %s and timestamp lt %s" % (format_rfc3339(start), format_rfc3339(now))
    return params


def collect(ctx) -> None:
    interface_type = (ctx.item.get("interface_type") or "WIRELESS").upper()
    if interface_type not in INTERFACE_TYPES:
        interface_type = "WIRELESS"

    key = checkpoint_key(ctx.input_type, ctx.name)
    ckpt, state = read_checkpoint(get_checkpointer, ctx.session_key, key)
    if state.get("interface_type") != interface_type:
        state = {"interface_type": interface_type, "last_sample": {}}
    last_sample = state.setdefault("last_sample", {})

    try:
        aps = list(ctx.client.paginate(AP_PATH, {"filter": "status eq 'ONLINE'"}, limit=AP_PAGE_SIZE))
    except Exception as exc:
        ctx.log_error(exc, AP_PATH)
        return

    pending = []
    total = 0

    def flush():
        nonlocal pending, total
        if pending:
            total += ctx.write("aruba:central:ap_throughput", pending, lambda r: parse_rfc3339(r["timestamp"]))
            pending = []
        write_checkpoint(ckpt, key, state)

    failures = 0
    for n, ap in enumerate(aps, start=1):
        serial = ap.get("serialNumber")
        if not serial:
            continue
        since = last_sample.get(serial)
        path = TREND_PATH % urllib.parse.quote(str(serial), safe="")
        try:
            body = ctx.client.get_json(path, trend_params(interface_type, since, time.time()))
            failures = 0
        except (ArubaCentralAuthError, ArubaCentralConnectionError) as exc:
            ctx.log_error(exc, path)
            break
        except ArubaCentralApiError as exc:
            ctx.log_error(exc, path)
            failures += 1
            if exc.status == 429 or failures >= MAX_CONSECUTIVE_FAILURES:
                ctx.logger.warning("action=throughput_aborted processed=%d of=%d", n - 1, len(aps))
                break
            continue

        records, newest = samples_to_records(ap, interface_type, body, since)
        pending.extend(records)
        if newest is not None:
            last_sample[serial] = newest
        if n % CHECKPOINT_EVERY == 0:
            flush()

    flush()
    ctx.logger.info("action=throughput_collected aps=%d samples=%d", len(aps), total)


def validate_input(definition: smi.ValidationDefinition):
    return


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    run_inputs(inputs, event_writer, collect)
