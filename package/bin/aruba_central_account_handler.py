"""Account REST handler: validates the API client against HPE SSO and Central before saving."""

import logging

from solnlib import conf_manager, log
from solnlib.credentials import CredentialManager
from splunktaucclib.rest_handler.admin_external import AdminExternalHandler
from splunktaucclib.rest_handler.error import RestError

from aruba_central_client import (
    ArubaCentralApiError,
    ArubaCentralAuthError,
    ArubaCentralClient,
    ArubaCentralConnectionError,
)
from aruba_central_common import (
    ACCOUNT_CONF,
    ADDON_NAME,
    TOKEN_REALM,
    apply_log_level,
    get_proxies,
)

VALIDATION_PATH = "/network-monitoring/v1/aps"
PLACEHOLDER = "******"
FIELDS = ("base_url", "token_url", "client_id", "client_secret")


def _logger() -> logging.Logger:
    return log.Logs().get_logger("%s_account_validation" % ADDON_NAME.lower())


def validate_account(account, proxies=None, logger=None, session=None):
    """Raise RestError(400) with a user-facing message if the account cannot read the API."""
    client = ArubaCentralClient(
        base_url=account["base_url"],
        token_url=account["token_url"],
        client_id=account["client_id"],
        client_secret=account["client_secret"],
        max_rps=1,
        timeout=30,
        proxies=proxies,
        logger=logger,
        session=session,
        max_retries=1,
        sleep=lambda _s: None,
    )
    try:
        client.access_token()
    except ArubaCentralAuthError as exc:
        raise RestError(
            400, "Token request rejected. Check client ID, client secret and token URL. (%s)" % exc
        ) from None
    except ArubaCentralConnectionError as exc:
        raise RestError(400, "Cannot reach the token URL (DNS, proxy, TLS or timeout). (%s)" % exc) from None
    except ArubaCentralApiError as exc:
        raise RestError(400, "Token endpoint returned an error. (%s)" % exc) from None

    try:
        client.get_json(VALIDATION_PATH, {"limit": 1})
    except ArubaCentralAuthError:
        raise RestError(400, "Token accepted by SSO but rejected by the API. Check that the API Base URL "
                             "belongs to the cluster of this API client.") from None
    except ArubaCentralConnectionError as exc:
        raise RestError(400, "Cannot reach the API Base URL (DNS, proxy, TLS or timeout). (%s)" % exc) from None
    except ArubaCentralApiError as exc:
        if exc.status == 403:
            raise RestError(400, "API client has no permission for Network Monitoring (HTTP 403).") from None
        raise RestError(400, "API Base URL returned an error. (%s)" % exc) from None


class ArubaCentralAccountHandler(AdminExternalHandler):
    def __init__(self, *args, **kwargs):
        AdminExternalHandler.__init__(self, *args, **kwargs)

    def _stored_account(self):
        cfm = conf_manager.ConfManager(
            self.getSessionKey(),
            ADDON_NAME,
            realm="__REST_CREDENTIAL__#%s#configs/conf-%s" % (ADDON_NAME, ACCOUNT_CONF),
        )
        try:
            return cfm.get_conf(ACCOUNT_CONF).get(self.callerArgs.id) or {}
        except Exception:
            return {}

    def _validate(self, editing):
        account = {k: (self.payload.get(k) or "").strip() for k in FIELDS}
        if editing and any(not v or v == PLACEHOLDER for v in account.values()):
            stored = self._stored_account()
            for k, v in account.items():
                if not v or v == PLACEHOLDER:
                    account[k] = stored.get(k, "")
        missing = [k for k, v in account.items() if not v]
        if missing:
            raise RestError(400, "Missing required field(s): %s" % ", ".join(missing))
        logger = _logger()
        apply_log_level(logger, self.getSessionKey())
        validate_account(account, proxies=get_proxies(logger, self.getSessionKey()), logger=logger)

    def _drop_cached_token(self):
        try:
            CredentialManager(self.getSessionKey(), ADDON_NAME, realm=TOKEN_REALM).delete_password(
                self.callerArgs.id)
        except Exception:
            pass

    def handleList(self, confInfo):
        AdminExternalHandler.handleList(self, confInfo)

    def handleCreate(self, confInfo):
        self._validate(editing=False)
        self._drop_cached_token()
        AdminExternalHandler.handleCreate(self, confInfo)

    def handleEdit(self, confInfo):
        if self.payload.get("disabled") is None:
            self._validate(editing=True)
            self._drop_cached_token()
        AdminExternalHandler.handleEdit(self, confInfo)

    def handleRemove(self, confInfo):
        self._drop_cached_token()
        AdminExternalHandler.handleRemove(self, confInfo)
