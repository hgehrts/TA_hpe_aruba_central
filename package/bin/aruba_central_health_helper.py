"""Input aruba_central_health: site health (snapshot) and alerts (new or changed only)."""

import hashlib
import json
import time

from splunklib import modularinput as smi

from aruba_central_common import (
    CheckpointError,
    checkpoint_key,
    get_checkpointer,
    read_checkpoint,
    run_inputs,
    snapshot_endpoint,
    write_checkpoint,
)

SITES_HEALTH_PATH = "/network-monitoring/v1/sites-health"
ALERTS_PATH = "/network-notifications/v1/alerts"
SITES_PAGE_SIZE = 100
ALERTS_PAGE_SIZE = 50


def alert_fingerprint(alert) -> str:
    return hashlib.sha256(
        json.dumps(alert, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def new_or_changed(alerts, previous_hashes):
    """Split alerts into (to_emit, all_current_hashes). Unchanged alerts are not re-emitted."""
    current = []
    emit = []
    previous = set(previous_hashes or [])
    for alert in alerts:
        digest = alert_fingerprint(alert)
        current.append(digest)
        if digest not in previous:
            emit.append(alert)
    return emit, current


def collect_alerts(ctx) -> None:
    """Alerts are skipped while the KV store is unavailable; emitting without the stored
    hashes would re-ingest every open alert (e.g. after each Splunk restart)."""
    key = checkpoint_key(ctx.input_type, ctx.name)
    try:
        ckpt, state = read_checkpoint(get_checkpointer, ctx.session_key, key)
    except CheckpointError as exc:
        ctx.log_error(exc, "alerts_checkpoint_read")
        ctx.logger.warning("action=alerts_skipped reason=kvstore_unavailable")
        return
    previous = state.get("alert_hashes")

    poll_time = time.time()
    try:
        alerts = list(ctx.client.paginate(ALERTS_PATH, limit=ALERTS_PAGE_SIZE))
    except Exception as exc:
        ctx.log_error(exc, ALERTS_PATH)
        return

    emit, current = new_or_changed(alerts, previous)
    ctx.write("aruba:central:alert", emit, lambda _r: poll_time)
    ctx.logger.info("action=alerts_collected returned=%d emitted=%d", len(alerts), len(emit))
    try:
        write_checkpoint(ckpt, key, {"alert_hashes": current, "updated": int(poll_time)})
    except CheckpointError as exc:
        ctx.log_error(exc, "alerts_checkpoint_write")


def collect(ctx) -> None:
    snapshot_endpoint(ctx, SITES_HEALTH_PATH, "aruba:central:site_health", SITES_PAGE_SIZE)
    collect_alerts(ctx)


def validate_input(definition: smi.ValidationDefinition):
    return


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    run_inputs(inputs, event_writer, collect)
