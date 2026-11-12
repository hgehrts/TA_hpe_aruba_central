"""Input REST handler that removes the KV Store checkpoint when an input is deleted."""

from solnlib import log
from splunktaucclib.rest_handler.admin_external import AdminExternalHandler

from aruba_central_common import ADDON_NAME, checkpoint_key, get_checkpointer


class ArubaCentralCheckpointInputHandler(AdminExternalHandler):
    def __init__(self, *args, **kwargs):
        AdminExternalHandler.__init__(self, *args, **kwargs)

    def handleList(self, confInfo):
        AdminExternalHandler.handleList(self, confInfo)

    def handleEdit(self, confInfo):
        AdminExternalHandler.handleEdit(self, confInfo)

    def handleCreate(self, confInfo):
        AdminExternalHandler.handleCreate(self, confInfo)

    def handleRemove(self, confInfo):
        input_type = self.handler.get_endpoint().input_type
        key = checkpoint_key(input_type, self.callerArgs.id)
        try:
            get_checkpointer(self.getSessionKey()).delete(key)
        except Exception as exc:
            log.Logs().get_logger("%s_checkpoint" % ADDON_NAME.lower()).warning(
                "action=checkpoint_delete_failed key=%s error=%s", key, type(exc).__name__)
        AdminExternalHandler.handleRemove(self, confInfo)
