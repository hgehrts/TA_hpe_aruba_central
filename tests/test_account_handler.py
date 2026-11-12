import pytest
import requests

from conftest import FakeResponse, FakeSession, token_ok

handler = pytest.importorskip("aruba_central_account_handler")
from splunktaucclib.rest_handler.error import RestError  # noqa: E402

ACCOUNT = {
    "base_url": "https://de3.api.central.arubanetworks.com",
    "token_url": "https://sso.common.cloud.hpe.com/as/token.oauth2",
    "client_id": "cid",
    "client_secret": "secret",
}


def test_valid_account_passes():
    session = FakeSession([token_ok()], [FakeResponse(200, {"items": [], "count": 0})])
    handler.validate_account(ACCOUNT, session=session)
    assert session.gets[0]["params"] == {"limit": 1}


@pytest.mark.parametrize("token_resp,get_resp,needle", [
    (FakeResponse(401, {"error_description": "invalid client"}), None, "client ID, client secret"),
    (requests.exceptions.ConnectionError("dns"), None, "Cannot reach the token URL"),
    (token_ok(), FakeResponse(403, {}), "no permission"),
    (token_ok(), requests.exceptions.SSLError("tls"), "Cannot reach the API Base URL"),
    (token_ok(), FakeResponse(404, {}), "API Base URL returned an error"),
])
def test_invalid_account_messages(token_resp, get_resp, needle):
    gets = [get_resp, get_resp] if get_resp is not None else []
    session = FakeSession([token_resp], gets)
    with pytest.raises(RestError) as err:
        handler.validate_account(ACCOUNT, session=session)
    assert needle in str(err.value)


def test_wrong_cluster_401_message():
    session = FakeSession([token_ok(), token_ok()], [FakeResponse(401, {}), FakeResponse(401, {})])
    with pytest.raises(RestError) as err:
        handler.validate_account(ACCOUNT, session=session)
    assert "belongs to the cluster" in str(err.value)
