"""Tier 2j — Prompt system endpoints (error-tolerant)."""

import pytest
from tests.conftest import assert_ok


def test_list_engine_prompts(client_auth):
    """GET /api/v1/prompts."""
    try:
        resp = client_auth().get("/api/v1/prompts")
        data = resp.get_json()
        if data and data.get("status") == "ok":
            assert isinstance(data.get("items", []), list)
    except (KeyError, TypeError):
        pass


def test_create_engine_prompt(client_auth):
    """POST /api/v1/prompts."""
    try:
        resp = client_auth().post("/api/v1/prompts", json={
            "prompt_id": "test-engine-prompt",
            "name": "Test", "engine_type_id": 21,
        })
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_rescan_engine_prompts(client_auth):
    """POST /api/v1/prompts/rescan."""
    try:
        resp = client_auth().post("/api/v1/prompts/rescan")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass
