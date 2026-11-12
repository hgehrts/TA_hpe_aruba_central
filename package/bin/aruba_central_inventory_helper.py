"""Input aruba_central_inventory: device inventory and monitored devices (snapshots)."""

from splunklib import modularinput as smi

from aruba_central_common import run_inputs, snapshot_endpoint

INVENTORY_PATH = "/network-monitoring/v1/device-inventory"
DEVICES_PATH = "/network-monitoring/v1/devices"
PAGE_SIZE = 100


def collect(ctx) -> None:
    snapshot_endpoint(ctx, INVENTORY_PATH, "aruba:central:device_inventory", PAGE_SIZE)
    snapshot_endpoint(ctx, DEVICES_PATH, "aruba:central:device", PAGE_SIZE)


def validate_input(definition: smi.ValidationDefinition):
    return


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    run_inputs(inputs, event_writer, collect)
