"""Input aruba_central_wireless: access points and Instant swarms (snapshots)."""

from splunklib import modularinput as smi

from aruba_central_common import run_inputs, snapshot_endpoint

AP_PATH = "/network-monitoring/v1/aps"
SWARM_PATH = "/network-monitoring/v1/swarms"
AP_PAGE_SIZE = 1000
SWARM_PAGE_SIZE = 100


def collect(ctx) -> None:
    snapshot_endpoint(ctx, AP_PATH, "aruba:central:ap", AP_PAGE_SIZE)
    snapshot_endpoint(ctx, SWARM_PATH, "aruba:central:swarm", SWARM_PAGE_SIZE)


def validate_input(definition: smi.ValidationDefinition):
    return


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    run_inputs(inputs, event_writer, collect)
