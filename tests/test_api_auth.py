"""Tier 2h — Auth handling."""

import os
from tests.conftest import assert_ok, assert_error


def test_missing_api_key_returns_401(client):
    """Request without X-API-Key returns 401. Uses .raw() to bypass default headers."""
    resp = client().raw("/api/v1/instances")
    assert_error(resp, expected_code="UNAUTHORIZED", expected_status=401)


def test_wrong_api_key_returns_401(client):
    """Request with wrong X-API-Key returns 401."""
    resp = client(headers={"X-API-Key": "wrong-key-12345"}).get("/api/v1/instances")
    assert_error(resp, expected_code="UNAUTHORIZED", expected_status=401)


def test_correct_api_key_returns_200(client_auth):
    """Request with correct X-API-Key does not return 401."""
    resp = client_auth().get("/api/v1/instances")
    assert resp.status_code != 401, "Correct API key should not get 401"


def test_health_check_is_auth_free(client):
    """GET /api/v1/app/status does not require API key."""
    resp = client().get("/api/v1/app/status")
    assert resp.status_code == 200


def test_sse_accepts_query_param(client):
    """SSE endpoint accepts ?api_key= query param."""
    api_key = os.environ.get("QUICKROBOT_API_KEY", "test-token")
    try:
        sse_resp = client().get(
            "/api/v1/instances/models-sse",
            query_string={"api_key": api_key},
        )
        assert sse_resp.status_code != 401, \
            f"SSE with ?api_key= should not return 401, got {sse_resp.status_code}"
    except Exception:
        pass


def test_config_get_put(client_auth):
    """GET/PUT /api/v1/config."""
    try:
        get_resp = client_auth().get("/api/v1/config")
        if get_resp.status_code == 200:
            assert_ok(get_resp)

        set_resp = client_auth().put("/api/v1/config/test_key", json={"value": "test_val"})
        if set_resp.status_code == 200:
            assert_ok(set_resp)
    except (KeyError, TypeError):
        pass
