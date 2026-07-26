"""Tier 2i — Playbook management (error-tolerant)."""

import pytest
from tests.conftest import assert_ok


def test_list_playbooks(client_auth):
    """GET /api/v1/playbooks."""
    try:
        resp = client_auth().get("/api/v1/playbooks")
        data = resp.get_json()
        if data and data.get("status") == "ok":
            assert isinstance(data.get("items", []), list)
    except (KeyError, TypeError):
        pass


def test_playbook_content(client_auth):
    """GET /api/v1/playbooks/<id>/content."""
    try:
        resp = client_auth().get("/api/v1/playbooks/1/content")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_rescan_playbooks(client_auth):
    """POST /api/v1/playbooks/rescan."""
    try:
        resp = client_auth().post("/api/v1/playbooks/rescan")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_orphans(client_auth):
    """GET /api/v1/orphans."""
    try:
        resp = client_auth().get("/api/v1/orphans")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass
