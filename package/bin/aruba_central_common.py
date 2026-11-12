"""Splunk-side helpers shared by all TA_hpe_aruba_central modular inputs."""

import calendar
import datetime
import hashlib
import json
import logging
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterable, Optional

from solnlib import conf_manager, log
from solnlib.credentials import CredentialManager, CredentialNotExistException
from solnlib.modular_input import checkpointer
from splunklib import modularinput as smi

from aruba_central_client import (
    ArubaCentralApiError,
    ArubaCentralAuthError,
    ArubaCentralClient,
    ArubaCentralConnectionError,
    TokenStore,
)

ADDON_NAME = "TA_hpe_aruba_central"
ACCOUNT_CONF = "ta_hpe_aruba_central_account"
SETTINGS_CONF = "ta_hpe_aruba_central_settings"
TOKEN_REALM = "TA_hpe_aruba_central_token_cache"
CHECKPOINT_COLLECTION = "TA_hpe_aruba_central_checkpoints"
HTTP_TIMEOUT_S = 60


class ConfigurationError(Exception):
    pass


class CheckpointError(Exception):
    """KV store checkpoint could not be read or written (e.g. HTTP 503 while the KV store initializes)."""


def describe_exception(exc: BaseException) -> str:
    return ("%s: %s" % (type(exc).__name__, exc))[:300]


def error_label(exc: BaseException) -> Optional[str]:
    """exc_l category for known failures; None means unexpected (logged with traceback)."""
    if isinstance(exc, ArubaCentralAuthError):
        return "Authentication Error"
    if isinstance(exc, ArubaCentralConnectionError):
        return "Connection Error"
    if isinstance(exc, ConfigurationError):
        return "Configuration Error"
    if isinstance(exc, CheckpointError):
        return "KV Store Error"
    if isinstance(exc, ArubaCentralApiError):
        if exc.status == 403:
            return "Permission Error"
        if (exc.status or 0) >= 500:
            return "Server Error"
        return "API Error"
    return None


def logger_for_input(input_name: str) -> logging.Logger:
    return log.Logs().get_logger("%s_%s" % (ADDON_NAME.lower(), input_name))


def is_true(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def get_account(session_key: str, account_name: str) -> Dict[str, Any]:
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm="__REST_CREDENTIAL__#%s#configs/conf-%s" % (ADDON_NAME, ACCOUNT_CONF),
    )
    try:
        account = cfm.get_conf(ACCOUNT_CONF).get(account_name)
    except Exception as exc:
        raise ConfigurationError("Account '%s' not found or not readable" % account_name) from exc
    for field in ("base_url", "token_url", "client_id", "client_secret"):
        if not account.get(field):
            raise ConfigurationError("Account '%s' has no %s" % (account_name, field))
    return account


def build_proxies(proxy: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Convert the UCC proxy stanza into a requests `proxies` dict (HTTP proxy only)."""
    if not proxy or not is_true(proxy.get("proxy_enabled")):
        return None
    host = (proxy.get("proxy_url") or "").strip()
    port = (proxy.get("proxy_port") or "").strip()
    if not host or not port:
        raise ConfigurationError("Proxy is enabled but host or port is missing")
    auth = ""
    if proxy.get("proxy_username"):
        auth = urllib.parse.quote(proxy["proxy_username"], safe="")
        if proxy.get("proxy_password"):
            auth += ":" + urllib.parse.quote(proxy["proxy_password"], safe="")
        auth += "@"
    url = "http://%s%s:%s" % (auth, host, port)
    return {"http": url, "https": url}


def get_proxies(logger: logging.Logger, session_key: str) -> Optional[Dict[str, str]]:
    try:
        proxy = conf_manager.get_proxy_dict(logger, session_key, ADDON_NAME, SETTINGS_CONF)
    except Exception:
        return None
    return build_proxies(proxy)


def token_fingerprint(account: Dict[str, Any]) -> str:
    material = "|".join([account.get("token_url", ""), account.get("client_id", "")])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SplunkTokenStore(TokenStore):
    """Caches the access token encrypted in storage/passwords, keyed by account name."""

    def __init__(self, session_key: str, account_name: str, fingerprint: str):
        self._manager = CredentialManager(session_key, ADDON_NAME, realm=TOKEN_REALM)
        self._user = account_name
        self._fingerprint = fingerprint

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            token = json.loads(self._manager.get_password(self._user))
        except CredentialNotExistException:
            return None
        if token.get("fingerprint") != self._fingerprint:
            return None
        return token

    def save(self, token: Dict[str, Any]) -> None:
        payload = dict(token, fingerprint=self._fingerprint)
        self._manager.set_password(self._user, json.dumps(payload))


def build_client(session_key: str, account_name: str, logger: logging.Logger) -> ArubaCentralClient:
    account = get_account(session_key, account_name)
    try:
        max_rps = max(1, min(10, int(account.get("max_rps") or 2)))
    except ValueError:
        max_rps = 2
    return ArubaCentralClient(
        base_url=account["base_url"],
        token_url=account["token_url"],
        client_id=account["client_id"],
        client_secret=account["client_secret"],
        max_rps=max_rps,
        timeout=HTTP_TIMEOUT_S,
        proxies=get_proxies(logger, session_key),
        token_store=SplunkTokenStore(session_key, account_name, token_fingerprint(account)),
        logger=logger,
    )


def parse_rfc3339(value: Any) -> Optional[float]:
    """RFC 3339 / ISO 8601 string (UTC 'Z' or offset) -> epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        frac = ""
        tail = ""
        for i, ch in enumerate(rest):
            if not ch.isdigit():
                tail = rest[i:]
                break
            frac += ch
        text = head + ("." + frac[:6].ljust(6, "0") if frac else "") + tail
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return calendar.timegm(dt.utctimetuple()) + dt.microsecond / 1e6


def format_rfc3339(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkpoint_key(input_type: str, input_name: str) -> str:
    return "%s_%s" % (input_type, input_name)


def get_checkpointer(session_key: str) -> checkpointer.KVStoreCheckpointer:
    return checkpointer.KVStoreCheckpointer(CHECKPOINT_COLLECTION, session_key, ADDON_NAME)


def read_checkpoint(get_ckpt: Callable[[str], Any], session_key: str, key: str):
    """Return (checkpointer, state dict); raises CheckpointError if the KV store is unavailable."""
    try:
        ckpt = get_ckpt(session_key)
        return ckpt, ckpt.get(key) or {}
    except Exception as exc:
        raise CheckpointError("KV store checkpoint read failed (%s)" % describe_exception(exc)) from exc


def write_checkpoint(ckpt: Any, key: str, state: Dict[str, Any]) -> None:
    try:
        ckpt.update(key, state)
    except Exception as exc:
        raise CheckpointError("KV store checkpoint write failed (%s)" % describe_exception(exc)) from exc


class InputContext:
    def __init__(self, full_name: str, item: Dict[str, Any], session_key: str,
                 event_writer: smi.EventWriter, logger: logging.Logger):
        self.full_name = full_name
        self.input_type, _, self.name = full_name.partition("://")
        self.item = item
        self.session_key = session_key
        self.event_writer = event_writer
        self.logger = logger
        self.index = item.get("index") or "default"
        self.account = item.get("account")
        self.client: Optional[ArubaCentralClient] = None
        self.host: Optional[str] = None
        self.errors = 0

    def write(self, sourcetype: str, records: Iterable[Dict[str, Any]],
              event_time: Callable[[Dict[str, Any]], Optional[float]]) -> int:
        count = 0
        for record in records:
            self.event_writer.write_event(
                smi.Event(
                    data=json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str),
                    time="%.3f" % (event_time(record) or time.time()),
                    index=self.index,
                    sourcetype=sourcetype,
                    host=self.host,
                )
            )
            count += 1
        log.events_ingested(self.logger, self.full_name, sourcetype, count, self.index,
                            account=self.account, host=self.host)
        return count

    def log_error(self, exc: Exception, what: str) -> None:
        self.errors += 1
        msg = "input=%s account=%s step=%s: " % (self.full_name, self.account, what)
        label = error_label(exc)
        if label is None:
            log.log_exception(self.logger, exc, "Unexpected Error", full_msg=True,
                              msg_before=msg + describe_exception(exc))
        else:
            # Single line (same exc_l labels as solnlib): Splunk indexes continuation lines as separate events.
            self.logger.error('exc_l="%s" %s%s', label, msg, describe_exception(exc))


def snapshot_endpoint(ctx: InputContext, path: str, sourcetype: str, limit: int) -> None:
    """Collect a list endpoint as a snapshot: one event per item, _time = poll time."""
    poll_time = time.time()
    try:
        ctx.write(sourcetype, ctx.client.paginate(path, limit=limit), lambda _r: poll_time)
    except Exception as exc:
        ctx.log_error(exc, path)


def run_inputs(inputs: smi.InputDefinition, event_writer: smi.EventWriter,
               collect: Callable[[InputContext], None]) -> None:
    for full_name, item in inputs.inputs.items():
        input_kind, _, short_name = full_name.rpartition("://")
        logger = logger_for_input("%s_%s" % (input_kind.replace("aruba_central_", "", 1), short_name))
        session_key = inputs.metadata["session_key"]
        try:
            logger.setLevel(conf_manager.get_log_level(
                logger=logger, session_key=session_key, app_name=ADDON_NAME, conf_name=SETTINGS_CONF))
        except Exception:
            logger.setLevel(logging.INFO)
        log.modular_input_start(logger, full_name)
        ctx = InputContext(full_name, item, session_key, event_writer, logger)
        started = time.time()
        try:
            if not ctx.account:
                raise ConfigurationError("No account selected")
            ctx.client = build_client(session_key, ctx.account, logger)
            ctx.host = urllib.parse.urlparse(ctx.client.base_url).hostname
            collect(ctx)
        except Exception as exc:
            ctx.log_error(exc, "setup")
        finally:
            log.log_event(logger, {
                "action": "run_summary",
                "modular_input_name": full_name,
                "api_requests": ctx.client.request_count if ctx.client else 0,
                "errors": ctx.errors,
                "duration_s": round(time.time() - started, 1),
            })
            log.modular_input_end(logger, full_name)
