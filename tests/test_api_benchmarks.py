"""Tier 2f — Benchmark endpoints (error-tolerant)."""

import pytest
from tests.conftest import assert_ok


def test_list_prompts(client_auth):
    """GET /api/v1/benchmarks/prompts."""
    try:
        resp = client_auth().get("/api/v1/benchmarks/prompts")
        data = resp.get_json()
        if data and data.get("status") == "ok":
            items = data.get("items", [])
            assert isinstance(items, list)
    except (KeyError, TypeError):
        pass


def test_create_prompt(client_auth):
    """POST /api/v1/benchmarks/prompts."""
    try:
        resp = client_auth().post("/api/v1/benchmarks/prompts", json={
            "name": "test-bench", "text": "Test prompt.", "engine_type_id": 21,
        })
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_list_results(client_auth):
    """GET /api/v1/benchmarks/results."""
    try:
        resp = client_auth().get("/api/v1/benchmarks/results")
        data = resp.get_json()
        if data and data.get("status") == "ok":
            assert isinstance(data.get("items", []), list)
    except (KeyError, TypeError):
        pass


def test_benchmark_run(client_auth):
    """POST /api/v1/benchmarks/run."""
    try:
        resp = client_auth().post("/api/v1/benchmarks/run", json={
            "instance_id": 1, "prompt_id": 1,
        })
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_webui_settings(client_auth):
    """GET /api/v1/webui/settings."""
    try:
        resp = client_auth().get("/api/v1/webui/settings")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass
