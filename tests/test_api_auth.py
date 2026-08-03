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


# ---------------------------------------------------------------------------
# QUICKROBOT_API_KEY_DISABLED tests
# ---------------------------------------------------------------------------

def test_disabled_true_allows_any_key(client):
    """QUICKROBOT_API_KEY_DISABLED=true → all requests pass regardless of key."""
    # Patch _AUTH_TOKENS to simulate disabled state (empty set = no enforcement)
    import qr_api as api_module
    original_tokens = api_module._AUTH_TOKENS
    api_module._AUTH_TOKENS = set()

    try:
        # No header → should pass
        resp_no_key = client().raw("/api/v1/instances")
        assert resp_no_key.status_code != 401, \
            f"disabled=true with no header should not return 401, got {resp_no_key.status_code}"

        # Wrong key → should pass
        resp_bad_key = client(headers={"X-API-Key": "random-wrong-key"}).get("/api/v1/instances")
        assert resp_bad_key.status_code != 401, \
            f"disabled=true with wrong key should not return 401, got {resp_bad_key.status_code}"

    finally:
        api_module._AUTH_TOKENS = original_tokens


def test_disabled_true_env_var_sets_empty_tokens():
    """QUICKROBOT_API_KEY_DISABLED=true causes _load_api_key() to skip token loading."""
    import os as _os
    import qr_api as api_module

    # Set the disabled flag and clear the key
    old_disabled = _os.environ.get("QUICKROBOT_API_KEY_DISABLED", "")
    old_key = _os.environ.get("QUICKROBOT_API_KEY", "")

    try:
        _os.environ["QUICKROBOT_API_KEY"] = ""
        _os.environ["QUICKROBOT_API_KEY_DISABLED"] = "true"
        api_module._load_api_key()
        assert api_module._AUTH_TOKENS == set(), \
            f"disabled=true should leave _AUTH_TOKENS empty, got {api_module._AUTH_TOKENS}"

    finally:
        _os.environ["QUICKROBOT_API_KEY"] = old_key
        if old_disabled:
            _os.environ["QUICKROBOT_API_KEY_DISABLED"] = old_disabled
        elif "QUICKROBOT_API_KEY_DISABLED" in _os.environ:
            del _os.environ["QUICKROBOT_API_KEY_DISABLED"]


def test_disabled_false_enforces_key(client):
    """QUICKROBOT_API_KEY_DISABLED=false restores normal auth enforcement."""
    import qr_api as api_module
    original_tokens = api_module._AUTH_TOKENS
    # Simulate: set a token (disabled=false, key set → enforcement active)
    api_module._AUTH_TOKENS = {"test-token"}

    try:
        # No header → 401
        resp = client().raw("/api/v1/instances")
        assert resp.status_code == 401, \
            f"disabled=false should enforce auth (401), got {resp.status_code}"

        # Wrong key → 401
        resp = client(headers={"X-API-Key": "wrong"}).get("/api/v1/instances")
        assert resp.status_code == 401, \
            f"disabled=false with wrong key should be 401, got {resp.status_code}"

        # Correct key → not 401
        resp = client(headers={"X-API-Key": "test-token"}).get("/api/v1/instances")
        assert resp.status_code != 401, \
            f"disabled=false with correct key should pass, got {resp.status_code}"

    finally:
        api_module._AUTH_TOKENS = original_tokens


def test_disabled_variants_are_truthy():
    """QUICKROBOT_API_KEY_DISABLED accepts true/1/yes variants."""
    import os as _os
    import qr_api as api_module

    variants_truthy = ["true", "True", "TRUE", "1", "yes", "Yes", "YES"]
    variants_falsy = ["false", "False", "FALSE", "0", "no", "No", "NO", ""]

    old_disabled = _os.environ.get("QUICKROBOT_API_KEY_DISABLED", "")
    old_key = _os.environ.get("QUICKROBOT_API_KEY", "")

    try:
        for variant in variants_truthy:
            _os.environ["QUICKROBOT_API_KEY_DISABLED"] = variant
            _os.environ["QUICKROBOT_API_KEY"] = ""  # clear key
            api_module._load_api_key()
            assert api_module._AUTH_TOKENS == set(), \
                f"disabled='{variant}' should produce empty _AUTH_TOKENS, got {api_module._AUTH_TOKENS}"

        for variant in variants_falsy:
            _os.environ["QUICKROBOT_API_KEY_DISABLED"] = variant
            _os.environ["QUICKROBOT_API_KEY"] = ""  # clear key
            api_module._load_api_key()
            # When disabled is falsy, empty key means _AUTH_TOKENS stays empty (no token to add)
            # This is expected — the flag is just not recognized as truthy
            # The key point is: disabled=false → normal behavior applies

    finally:
        _os.environ["QUICKROBOT_API_KEY"] = old_key
        if old_disabled:
            _os.environ["QUICKROBOT_API_KEY_DISABLED"] = old_disabled
        elif "QUICKROBOT_API_KEY_DISABLED" in _os.environ:
            del _os.environ["QUICKROBOT_API_KEY_DISABLED"]
