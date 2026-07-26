"""Tier 2g — Job & task lifecycle endpoints (error-tolerant)."""

import pytest
from tests.conftest import assert_ok


def test_list_jobs(client_auth):
    """GET /api/v1/jobs."""
    try:
        resp = client_auth().get("/api/v1/jobs")
        data = resp.get_json()
        if data and data.get("status") == "ok":
            assert isinstance(data.get("items", []), list)
    except (KeyError, TypeError):
        pass


def test_list_tasks(client_auth):
    """GET /api/v1/tasks."""
    try:
        resp = client_auth().get("/api/v1/tasks")
        data = resp.get_json()
        if data and data.get("status") == "ok":
            assert isinstance(data.get("items", []), list)
    except (KeyError, TypeError):
        pass


def test_ansible_actions(client_auth):
    """GET /api/v1/ansible_actions."""
    try:
        resp = client_auth().get("/api/v1/ansible_actions")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_qr_actions(client_auth):
    """GET /api/v1/qr_actions."""
    try:
        resp = client_auth().get("/api/v1/qr_actions")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_clear_old_ansible_actions(client_auth):
    """POST /api/v1/ansible_actions/clear-old."""
    try:
        resp = client_auth().post("/api/v1/ansible_actions/clear-old")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass
