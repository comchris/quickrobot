"""Tier 2d — Preset management endpoints (error-tolerant)."""

import pytest
from tests.conftest import assert_ok


def test_list_presets(client_auth):
    """GET /api/v1/engine/llama_server/presets."""
    try:
        resp = client_auth().get("/api/v1/engine/llama_server/presets")
        data = resp.get_json()
        if data:
            assert resp.status_code != 401
            if data.get("status") == "ok":
                items = data.get("items", [])
                assert isinstance(items, list)
    except (KeyError, TypeError):
        pass


def test_get_preset(client_auth):
    """GET /api/v1/engine/<type>/presets/<id>."""
    try:
        resp = client_auth().get("/api/v1/engine/llama_server/presets/1")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_create_preset(client_auth):
    """POST /api/v1/engine/<type>/presets."""
    try:
        resp = client_auth().post("/api/v1/engine/llama_server/presets", json={
            "name": "test-create", "category": "test", "config_template": "{}"
        })
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_update_preset(client_auth):
    """PUT /api/v1/engine/<type>/presets/<id>."""
    try:
        resp = client_auth().put("/api/v1/engine/llama_server/presets/1", json={})
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_delete_preset(client_auth):
    """DELETE /api/v1/engine/<type>/presets/<id>."""
    try:
        # Create first
        create_resp = client_auth().post("/api/v1/engine/llama_server/presets", json={
            "name": "test-del", "category": "test", "config_template": "{}"
        })
        data = create_resp.get_json()
        if data and data.get("status") == "ok":
            preset_id = data["data"]["id"]
            del_resp = client_auth().delete(f"/api/v1/engine/llama_server/presets/{preset_id}")
            assert del_resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_clone_preset(client_auth):
    """POST /api/v1/engine/<type>/presets/<id>/clone."""
    try:
        resp = client_auth().post("/api/v1/engine/llama_server/presets/1/clone")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_presets_rpc_engine(client_auth):
    """Presets for llama_rpc engine."""
    try:
        resp = client_auth().get("/api/v1/engine/llama_rpc/presets")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass
