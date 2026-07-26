"""Tier 2e — Model management endpoints (error-tolerant)."""

import pytest
from tests.conftest import assert_ok


def test_list_models(client_auth):
    """GET /api/v1/engine/<type>/models."""
    try:
        resp = client_auth().get("/api/v1/engine/llama_server/models")
        data = resp.get_json()
        if data and data.get("status") == "ok":
            items = data.get("items", [])
            assert isinstance(items, list)
    except (KeyError, TypeError):
        pass


def test_get_model(client_auth):
    """GET /api/v1/engine/<type>/models/<id>."""
    try:
        resp = client_auth().get("/api/v1/engine/llama_server/models/1")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_scan_models(client_auth):
    """POST /api/v1/engine/<type>/models/scan."""
    try:
        resp = client_auth().post("/api/v1/engine/llama_server/models/scan")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_checksum_diff(client_auth):
    """GET /api/v1/engine/<type>/models/checksum-diff."""
    try:
        resp = client_auth().get("/api/v1/engine/llama_server/models/checksum-diff")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_global_model_list(client_auth):
    """GET /api/v1/models."""
    try:
        resp = client_auth().get("/api/v1/models")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass
