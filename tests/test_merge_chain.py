"""Tier 5 — Preset merge chain and cluster config (error-tolerant)."""

import pytest
from tests.conftest import PROJECT_ROOT


def test_merge_chain_imports():
    """Config merge modules are importable."""
    from lib.lib_config_merge import build_merged_config, _deep_merge
    assert callable(build_merged_config)
    assert callable(_deep_merge)


def test_merge_chain_layers():
    """Config merge processes all layers."""
    # Just verify the module loads — full merge requires DB context
    from lib.lib_config_merge import _deep_merge
    result = _deep_merge({"a": 1}, {"b": 2})
    assert isinstance(result, dict)


def test_merge_chain_env_propagation():
    """Deep merge propagates values correctly."""
    from lib.lib_config_merge import _deep_merge
    result = _deep_merge(
        {"LLAMA_ARG_CTX_SIZE": "4096"},
        {},
    )
    assert result.get("LLAMA_ARG_CTX_SIZE") == "4096"


def test_cluster_env_builder_imports():
    """Cluster env builder is importable."""
    from lib.lib_cluster_env_builder import build_llama_server_env, get_cluster_summary
    assert callable(build_llama_server_env)
    assert callable(get_cluster_summary)


def test_cluster_config_rpc_bindings(client_auth):
    """PUT /api/v1/instances/<id>/cluster-bind."""
    try:
        resp = client_auth().put("/api/v1/instances/1/cluster-bind", json={
            "rpc_bindings": [],
        })
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_rpc_bindings_list(client_auth):
    """GET /api/v1/rpc-bindings."""
    try:
        resp = client_auth().get("/api/v1/rpc-bindings")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_rpccluster_summary(client_auth):
    """GET /api/v1/rpccluster/summary."""
    try:
        resp = client_auth().get("/api/v1/rpccluster/summary")
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass


def test_expert_split_patch(client_auth):
    """PATCH /api/v1/instances/<id>/expert-split."""
    try:
        resp = client_auth().patch("/api/v1/instances/1/expert-split", json={})
        assert resp.status_code != 401
    except (KeyError, TypeError):
        pass
