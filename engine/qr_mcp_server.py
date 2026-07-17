# Copyright 2026 comchris quickrobot .de project 
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp"]
# ///
"""Quickrobot MCP SSE server — wraps quickrobot API as MCP tools.

Environment variables (all prefixed with QUICKROBOT_):
  QUICKROBOT_API_HOST: Quickrobot API server host (e.g., 127.0.0.1)
  QUICKROBOT_API_PORT: Quickrobot API server port (e.g., 8039)
  QUICKROBOT_MCP_HOST: MCP listen address (default: 127.0.0.1)
  QUICKROBOT_MCP_PORT: MCP listen port (default: 8040)
  QUICKROBOT_MCP_READ: Expose read tools (default: false)
  QUICKROBOT_MCP_WRITE: Expose write tools (default: false)
  QUICKROBOT_MCP_FULLPROXY: Expose raw API proxy tool (default: false)
  QUICKROBOT_API_BEARER_TOKEN: Bearer token for API authentication

Usage:
  python engine/qr_mcp_server.py --port 8040
"""
import sys as _sys
import os as _os
import argparse as _argparse

# Fix: when run from project root, engine/ subdir shadows stdlib subprocess.
# Remove 'engine' from sys.path[0] so stdlib modules resolve correctly.
_sys_path_0 = _sys.path[0] if _sys.path[0] else _os.getcwd()
if _sys_path_0.endswith("/engine") or _sys_path_0.endswith("engine"):
    del _sys.path[0]

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)



from lib.qr_engine_ids import (
    QR_DEFAULT_LOCALHOST, QR_ENGINE_LLAMA_SERVER, QR_FORBIDDEN_HOSTS,
    QR_ENGINE_LLAMA_SERVER_NAME,
    QR_MCP_DEFAULT_READS, QR_MCP_DEFAULT_WRITES, QR_MCP_DEFAULT_PROXY,
    QR_MCP_CONNECT_RETRIES, QR_MCP_CONNECT_DELAY,
)

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings as _TSS

def _parse_cors_origins():
    """Parse QUICKROBOT_MCP_CORS_ORIGINS env var into a list of origin patterns.

    Format: comma-separated full origin URLs (scheme://host:port).
    Examples: "http://127.0.0.1:8080,http://localhost:8080" or "*" for all.
    Default: ["*"] — accepts all origins.
    """
    raw = os.getenv("QUICKROBOT_MCP_CORS_ORIGINS", "").strip()
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip()]
    return ["*"]


# Configuration from environment — all keys prefixed with QUICKROBOT_
_api_host = os.getenv("QUICKROBOT_API_HOST")
_api_port = os.getenv("QUICKROBOT_API_PORT")
if _api_host and _api_port:
    API_BASE = f"http://{_api_host}:{_api_port}/api/v1"
else:
    raise RuntimeError(
        "MCP server needs QUICKROBOT_API_HOST + QUICKROBOT_API_PORT in .quickrobot.env"
    )
# Accept both naming conventions: QUICKROBOT_MCP_* (harmonized) and legacy MCP_ALLOW_*
def _mcp_bool(primary, fallback, default="false"):
    """Read MCP flag from primary env var, fallback to legacy name, final fallback to default."""
    return os.getenv(primary, os.getenv(fallback, default)).lower() in ("true", "1", "yes")

ALLOW_READS = _mcp_bool("QUICKROBOT_MCP_READ", "MCP_ALLOW_READS", QR_MCP_DEFAULT_READS)
ALLOW_WRITES = _mcp_bool("QUICKROBOT_MCP_WRITE", "MCP_ALLOW_WRITES", QR_MCP_DEFAULT_WRITES)
ALLOW_PROXY = _mcp_bool("QUICKROBOT_MCP_FULLPROXY", "MCP_ALLOW_PROXY", QR_MCP_DEFAULT_PROXY)

# Single-toggle security — all values from .env, no hardcoded strings or branching.
_disable = os.getenv("QUICKROBOT_MCP_DISABLE_DNS_REBINDING", "false").lower() in ("true", "1", "yes")
_transport_security = _TSS(
    enable_dns_rebinding_protection=not _disable,
    allowed_hosts=[f"{os.getenv('QUICKROBOT_MCP_HOST')}:{os.getenv('QUICKROBOT_MCP_PORT')}"],
    allowed_origins=_parse_cors_origins(),
)

# Create MCP server with SSE host/port config and host validation
_mcp_host = os.getenv("QUICKROBOT_MCP_HOST")
_mcp_port_raw = os.getenv("QUICKROBOT_MCP_PORT")
if not _mcp_host:
    raise RuntimeError("MCP server needs QUICKROBOT_MCP_HOST in .quickrobot.env")
if not _mcp_port_raw:
    raise RuntimeError("MCP server needs QUICKROBOT_MCP_PORT in .quickrobot.env")
mcp_host_env = _mcp_host
try:
    mcp_port_env = int(_mcp_port_raw)
except (ValueError, TypeError):
    raise RuntimeError(f"MCP port must be an integer, got: {repr(_mcp_port_raw)}")
mcp = FastMCP(
    "QuickrobotAPI",
    host=mcp_host_env,
    port=mcp_port_env,
    transport_security=_transport_security,
)


def _api_call(method, path, body=None, timeout=None):
    """Make an HTTP call to the quickrobot API.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, PATCH)
        path: API path (relative to /api/v1)
        body: JSON object (dict) or pre-encoded JSON string for POST/PUT/PATCH.
              If already a string, passed as raw data (no double-encoding).
        timeout: Request timeout in seconds. Defaults per method:
                 GET=30s | POST=60s | PUT=60s | DELETE=30s | PATCH=30s
                 Pass a lower value for fire-and-forget operations.
    """
    import requests as _requests
    # Normalize: strip leading /api/v1/ so both styles work identically
    path = path.removeprefix("/api/v1").removeprefix("/api/v1/")
    url = f"{API_BASE}{path}"
    headers = {"Content-Type": "application/json"} if body else {}

    # Default timeouts per method (can be overridden via timeout param)
    _default_timeouts = {
        "GET": 30,
        "POST": 60,
        "PUT": 60,
        "DELETE": 30,
        "PATCH": 30,
    }
    req_timeout = timeout if timeout is not None else _default_timeouts.get(method.upper(), 30)

    try:
        # If body is already a JSON string, pass as raw data to avoid double-encoding.
        # If body is a dict, let requests json= handle the encoding.
        if isinstance(body, str):
            _body_kw = {"data": body}
        else:
            _body_kw = {"json": body} if body else {}

        if method == "GET":
            r = _requests.get(url, headers=headers, timeout=req_timeout)
        elif method == "POST":
            r = _requests.post(url, headers=headers, timeout=req_timeout, **_body_kw)
        elif method == "PUT":
            r = _requests.put(url, headers=headers, timeout=req_timeout, **_body_kw)
        elif method == "DELETE":
            r = _requests.delete(url, headers=headers, timeout=req_timeout)
        elif method == "PATCH":
            r = _requests.patch(url, headers=headers, timeout=req_timeout, **_body_kw)
        else:
            return json.dumps({"error": f"Unknown method: {method}"})
        if r.status_code >= 400:
            return json.dumps({"status": "error", "code": r.status_code, "message": r.text[:500]})
        return r.text
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _normalize_engine_type(engine_type):
    """Normalize engine_type param: empty string or whitespace → None (no filter)."""
    if not engine_type or not str(engine_type).strip():
        return None
    return str(engine_type).strip()


# ============================================================================
# READ TOOLS
# ============================================================================

if ALLOW_READS:
    @mcp.tool()
    def list_instances(instance_ids: int | list[int] = None) -> str:
        """List all instances with full details (config_override, ansible_vars, uuids).

        Use list_instances_summary() for operational overview — 95% less data.

        Args:
            instance_ids: Filter to specific instances. Accepts a single int or list of ints.
                         If omitted, returns all instances.
        """
        if not instance_ids:
            return _api_call("GET", "/instances")
        # Normalize single int to list
        ids = [instance_ids] if isinstance(instance_ids, int) else instance_ids
        raw = _api_call("GET", "/instances")
        try:
            data = json.loads(raw)
            all_items = data.get("items", []) if isinstance(data, dict) else []
            id_set = set(str(i) for i in ids)
            filtered = [i for i in all_items if str(i.get("id", "")) in id_set]
            return json.dumps({"status": "ok", "total": len(filtered), "items": filtered})
        except Exception as _e:
            logger.debug("list_instances compact JSON parse failed: %s", _e)
            return raw

    @mcp.tool()
    def get_instance_status(instance_id: int) -> str:
        """Detailed status of a specific instance including merged_config (env+cli_opts)."""
        return _api_call("GET", f"/instances/{instance_id}/status")

    @mcp.tool()
    def list_nodes() -> str:
        """Full node list with capabilities JSON (CPU, RAM, GPU, OS, hardware inventory).

        Use list_nodes_summary() for operational overview — 98% less data.
        """
        return _api_call("GET", "/nodes")

    @mcp.tool()
    def list_presets(engine_type: str = QR_ENGINE_LLAMA_SERVER_NAME) -> str:
        """List presets with full config_template JSON (env vars, CLI options).

        Use list_presets_summary() for selection — 77% less data.

        Args:
            engine_type: Engine type filter (default: llama_server). Empty string returns all.
        """
        _et = _normalize_engine_type(engine_type)
        if _et is None:
            return _api_call("GET", "/engine/presets")
        return _api_call("GET", f"/engine/{_et}/presets")

    @mcp.tool()
    def get_preset(preset_id: int, engine_type: str = QR_ENGINE_LLAMA_SERVER_NAME) -> str:
        """Get full details of a specific preset including config_template JSON.

        Args:
            preset_id: Preset ID to look up
            engine_type: Engine type filter (default: llama_server). Empty string returns all.
        """
        _et = _normalize_engine_type(engine_type)
        if _et is None:
            return _api_call("GET", f"/engine/presets/{preset_id}")
        return _api_call("GET", f"/engine/{_et}/presets/{preset_id}")

    @mcp.tool()
    def list_models(engine_type: str = QR_ENGINE_LLAMA_SERVER_NAME) -> str:
        """List models with SHA256 hashes, verification timestamps, and model_params JSON.

        Use list_models_summary() for selection — 79% less data.

        Args:
            engine_type: Engine type filter (default: llama_server). Empty string returns all.
        """
        _et = _normalize_engine_type(engine_type)
        if _et is None:
            return _api_call("GET", "/engine/models")
        return _api_call("GET", f"/engine/{_et}/models")

    @mcp.tool()
    def get_model(model_id: int, engine_type: str = QR_ENGINE_LLAMA_SERVER_NAME) -> str:
        """Get full details of a specific model including all fields.

        Args:
            model_id: Model ID to look up
            engine_type: Engine type filter (default: llama_server). Empty string returns all.
        """
        _et = _normalize_engine_type(engine_type)
        if _et is None:
            return _api_call("GET", f"/engine/models/{model_id}")
        return _api_call("GET", f"/engine/{_et}/models/{model_id}")

    @mcp.tool()
    def list_instances_summary(instance_ids: int | list[int] = None) -> str:
        """Compact instance list (id, name, state, engine, node, port).

        Prefer for all operational decisions — 95% less data than list_instances().

        Args:
            instance_ids: Filter to specific instances. Accepts a single int or list of ints.
                         If omitted, returns all instances.
        """
        raw = _api_call("GET", "/instances")
        try:
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else []
            # Filter by instance_ids if provided (empty list = no filter = all)
            if instance_ids:
                ids = [instance_ids] if isinstance(instance_ids, int) else instance_ids
                id_set = set(str(i) for i in ids)
                items = [i for i in items if str(i.get("id", "")) in id_set]
            compact = []
            for inst in items:
                compact.append({
                    "id": inst.get("id"),
                    "name": inst.get("name"),
                    "state": inst.get("state"),
                    "engine_type_name": inst.get("engine_type_name"),
                    "node_hostname": inst.get("node_hostname"),
                    "port_assigned": inst.get("port_assigned"),
                    "preset_name": inst.get("preset_name"),
                    "model_name": inst.get("model_name"),
                    "system_managed": inst.get("system_managed", False),
                    "has_custom_config": inst.get("has_custom_config", False),
                    "host_inactive": inst.get("_host_inactive", False),
                })
            return json.dumps({"status": "ok", "total": len(compact), "items": compact})
        except Exception as _e:
            logger.debug("list_instances compact JSON parse failed: %s", _e)
            return raw

    @mcp.tool()
    def list_nodes_summary() -> str:
        """Compact node list (id, name, hostname, status, ping_state).

        Prefer for availability checks — 98% less data than list_nodes().
        """
        raw = _api_call("GET", "/nodes")
        try:
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else []
            compact = []
            for node in items:
                compact.append({
                    "id": node.get("id"),
                    "name": node.get("name"),
                    "hostname": node.get("hostname"),
                    "status": node.get("status"),
                    "is_active": node.get("is_active", True),
                    "ping_state": node.get("ping_state"),
                })
            return json.dumps({"status": "ok", "total": len(compact), "items": compact})
        except Exception as _e:
            logger.debug("list_nodes compact JSON parse failed: %s", _e)
            return raw

    @mcp.tool()
    def list_presets_summary(engine_type: str = QR_ENGINE_LLAMA_SERVER_NAME) -> str:
        """Compact preset list (id, name, category, model_name, gpu_device).

        Prefer for selection — 77% less data.

        Args:
            engine_type: Engine type filter (default: llama_server). Empty string returns all.
        """
        _et = _normalize_engine_type(engine_type)
        if _et is None:
            raw = _api_call("GET", "/engine/presets")
        else:
            raw = _api_call("GET", f"/engine/{_et}/presets")
        try:
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else []
            compact = []
            for preset in items:
                compact.append({
                    "id": preset.get("id"),
                    "name": preset.get("name"),
                    "category": preset.get("category"),
                    "model_name": preset.get("model_name"),
                    "gpu_device": preset.get("gpu_device"),
                })
            return json.dumps({"status": "ok", "total": len(compact), "items": compact})
        except Exception as _e:
            logger.debug("list_presets compact JSON parse failed: %s", _e)
            return raw

    @mcp.tool()
    def list_models_summary(engine_type: str = QR_ENGINE_LLAMA_SERVER_NAME) -> str:
        """Compact model list (id, name, path, quantization, size, preset_count, discovered).

        Prefer for selection — 79% less data.

        Args:
            engine_type: Engine type filter (default: llama_server). Empty string returns all.
        """
        _et = _normalize_engine_type(engine_type)
        if _et is None:
            raw = _api_call("GET", "/engine/models")
        else:
            raw = _api_call("GET", f"/engine/{_et}/models")
        try:
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else []
            compact = []
            for model in items:
                compact.append({
                    "id": model.get("id"),
                    "name": model.get("name"),
                    "model_path": model.get("model_path"),
                    "quantization": model.get("quantization"),
                    "size_bytes": model.get("size_bytes"),
                    "preset_count": model.get("preset_count", 0),
                    "discovered": model.get("discovered", False),
                })
            return json.dumps({"status": "ok", "total": len(compact), "items": compact})
        except Exception as _e:
            logger.debug("list_models compact JSON parse failed: %s", _e)
            return raw

    @mcp.tool()
    def list_benchmark_prompts() -> str:
        """List all available benchmark prompts with their IDs, names, and text content.

        Useful for selecting a prompt to use with run_benchmark().
        """
        return _api_call("GET", "/benchmarks/prompts")

    @mcp.tool()
    def run_benchmark(instance_id: int, prompt_id: int, timeout_seconds: int = None) -> str:
        """Run a benchmark on a running llama_server instance using the specified prompt.

        Args:
            instance_id: Running llama_server instance to benchmark
            prompt_id: Prompt ID to use (list with list_benchmark_prompts() first)
            timeout_seconds: Optional custom timeout in seconds (default: auto-calculated from prompt)
        """
        body = {"instance_id": instance_id, "prompt_id": prompt_id}
        if timeout_seconds:
            body["timeout_seconds"] = timeout_seconds
        return _api_call("POST", "/benchmarks/run", body, timeout=15)

    @mcp.tool()
    def list_benchmark_results(instance_ids: int | list[int] = None, limit: int = 50) -> str:
        """List benchmark results with optional instance filtering.

        Args:
            instance_ids: Filter to specific instances. Accepts a single int or list of ints.
                         If omitted, returns results across all instances.
            limit: Maximum number of rows to return (default 50).
        """
        if not instance_ids:
            return _api_call("GET", f"/benchmarks/results?limit={limit}")
        # Normalize single int to list
        ids = [instance_ids] if isinstance(instance_ids, int) else instance_ids
        all_results = []
        seen_run_ids = set()
        for inst_id in ids:
            path = f"/benchmarks/results?instance_id={inst_id}&limit={limit}"
            raw = _api_call("GET", path)
            try:
                data = json.loads(raw)
                items = data.get("items", []) if isinstance(data, dict) else []
                for r in items:
                    rid = str(r.get("id", ""))
                    if rid not in seen_run_ids:
                        seen_run_ids.add(rid)
                        all_results.append(r)
            except Exception as _e:
                logger.debug("benchmark results dedup failed: %s", _e)
                pass
        return json.dumps({"status": "ok", "total": len(all_results), "items": all_results})

    @mcp.tool()
    def get_node(node_id: int) -> str:
        """Get full details of a specific node including capabilities (CPU, RAM, GPU, OS)
        and list of attached instances.

        Args:
            node_id: ID of the node to look up
        """
        return _api_call("GET", f"/nodes/{node_id}")


# ============================================================================
# WRITE TOOLS
# ============================================================================

if ALLOW_WRITES:
    @mcp.tool()
    def create_instance(
        name: str,
        engine_type_id: int = QR_ENGINE_LLAMA_SERVER,
        node_id: int = None,
        preset_id: int = None,
        config_override: dict = None,
        skip_build: bool = None
    ) -> str:
        """Create a new instance and auto-deploy it.

        Creates DB record, assigns port, and begins the staged deploy chain automatically.
        No separate deploy/start call needed — use list_instances_summary() to check progress.

        **Cluster RPC workflow:** Create all RPC instances first, verify they are 'running',
        then create the llama-server instance, bind RPCs via PUT /instances/<id>,
        and restart only the server (RPCs do NOT need restart after binding).
        All RPCs must be running BEFORE the server is (re)started — otherwise the
        server crashes on connect and enters error state.

        **Model files:** Only need to exist on the llama-server node. The server pushes
        model shards to bound RPC instances automatically during startup.

        Args:
            name: Instance display name (e.g., 'dllama1-llama-server')
            engine_type_id: Engine type ID (21=llama_server, 22=llama_rpc, 31=iperf3, 12=subprocess)
            node_id: Target node ID (required for remote engines)
            preset_id: Preset ID to use for initial config
            config_override: Additional config overrides as JSON object
            skip_build: If true, skip git clone + cmake compile (use existing binary).
                        If false, full deploy including source+compile.
                        If None (default), uses engine_configs.skip_build setting.
        """
        body = {"name": name, "engine_type_id": engine_type_id}
        if node_id is not None:
            body["node_id"] = node_id
        if preset_id is not None:
            body["preset_id"] = preset_id
        if config_override:
            body["config_override"] = config_override
        if skip_build is not None:
            body["skip_build"] = skip_build
        return _api_call("POST", "/instances", body, timeout=5)

    @mcp.tool()
    def deploy_instance(
        instance_id: int,
        start_after_deploy: bool = False,
        skip_build: bool = None
    ) -> str:
        """Deploy/redeploy an instance on its target node via the staged playbook chain.

        The engine type is determined from the instance record in the DB — no need to specify it.
        Runs preflight → deps → source → compile → config → start stages with per-stage progress.
        Use after creating an instance, changing preset, or updating config.

        **Cluster note:** After binding RPCs to a server (PUT /instances/<id>), only the server
        needs deploy+restart — bound RPC instances continue running without restart.

        Args:
            instance_id: ID of the instance to deploy (non-system-managed only)
            start_after_deploy: If true, auto-start the service after deploy completes (default: false).
                                Most agents should leave this false and call start_instance() explicitly.
            skip_build: If true, skip git clone + cmake compile (use existing binary).
                        If false, full deploy including source+compile.
                        If None (default), uses engine_configs.skip_build setting.
        """
        body = {}
        if start_after_deploy:
            body["start_after_deploy"] = True
        if skip_build is not None:
            body["skip_build"] = skip_build
        return _api_call("POST", f"/instances/{instance_id}/deploy", body or None, timeout=5)

    @mcp.tool()
    def start_instance(instance_id: int) -> str:
        """Start a stopped or configured instance.

        Starts the systemd service and initiates model loading (for llama_server).
        For llama_rpc, transitions directly to 'running' state.

        Uses 30s timeout to wait for actual result (success/fail message).
        If the operation takes longer, poll via list_instances_summary().

        Args:
            instance_id: ID of the instance to start
        """
        return _api_call("POST", f"/instances/{instance_id}/start", timeout=30)

    @mcp.tool()
    def stop_instance(instance_id: int) -> str:
        """Stop a running instance.

        Stops the systemd service gracefully. The instance remains in 'stopped' state
        and can be restarted later without re-deploying.

        Uses 30s timeout to wait for actual result (success/fail message).
        If the operation takes longer, poll via list_instances_summary().

        Args:
            instance_id: ID of the instance to stop
        """
        return _api_call("POST", f"/instances/{instance_id}/stop", timeout=30)

    @mcp.tool()
    def restart_instance(instance_id: int) -> str:
        """Restart a running instance (stop → start with same config).

        Use when config hasn't changed. If preset or config_override changed, use
        deploy_instance() instead to regenerate env/service files.

        Uses 30s timeout to wait for actual result (success/fail message).
        If the operation takes longer, poll via list_instances_summary().

        Args:
            instance_id: ID of the instance to restart
        """
        return _api_call("POST", f"/instances/{instance_id}/restart", timeout=30)

    @mcp.tool()
    def change_preset(instance_id: int, preset_id: int, skip_build: bool = True) -> str:
        """Change an instance's preset and apply config changes without full redeploy.

        NOTE: instance_id is the running instance to update; preset_id is the target
        preset definition ID (not an instance). Use list_presets_summary() to find
        valid preset IDs, then list_instances_summary() to find the target instance.

        Updates the instance preset in DB and triggers BC-1 reconfigure chain:
        - deploy_config_env (writes env file with new preset CLI args + env vars)
        - service_start (stop → restart to pick up new config)
        No git clone, no cmake build when skip_build=True.
        Works for llama_server, llama_rpc, and iperf3 engines.

        Args:
            instance_id: Instance to reconfigure
            preset_id: New preset ID to apply
            skip_build: If True, config-only update (default). If False, full deploy with git/build.
        """
        body = {"preset_id": preset_id}
        if not skip_build:
            body["skip_build"] = False
        return _api_call("PUT", f"/instances/{instance_id}", body, timeout=5)

    @mcp.tool()
    def delete_instance(instance_id: int, force: bool = False) -> str:
        """Delete an instance and its associated data.

        Args:
            instance_id: ID of the instance to delete
            force: Force delete without checking state (default: false). Use when
                  instance is stuck in a transitional state (e.g., 'deploying' with no response).
        """
        path = f"/instances/{instance_id}"
        if force:
            return _api_call("POST", f"{path}/force-delete", timeout=30)
        return _api_call("DELETE", path, timeout=30)

    @mcp.tool()
    def create_node(
        name: str,
        hostname: str,
        ansible_user: str = None,
        ansible_port: int = 22,
        ansible_key_path: str = None,
        ipv4_address: str = None,
        model_base_path: str = None
    ) -> str:
        """Create a new node (host) entry and auto-validate it via SSH.

        Args:
            name: Display name for the node (e.g., 'dllama6')
            hostname: DNS name or IP for SSH connection (e.g., 'dllama6.lan' or '192.168.31.11')
            ansible_user: SSH user — defaults from .quickrobot.env if not set
            ansible_port: SSH port — defaults 22 if not set
            ansible_key_path: Path to SSH private key — defaults to ssh-agent
            ipv4_address: Numeric IP for reference only
            model_base_path: Default model root path for this node

        Returns:
            Node data with discovered hardware (CPU/RAM/GPU/OS/capabilities).
        """
        body = {"name": name, "hostname": hostname}
        if ansible_user is not None:
            body["ansible_user"] = ansible_user
        if ansible_port != 22:
            body["ansible_port"] = ansible_port
        if ansible_key_path is not None:
            body["ansible_key_path"] = ansible_key_path
        if ipv4_address is not None:
            body["ipv4_address"] = ipv4_address
        if model_base_path is not None:
            body["model_base_path"] = model_base_path
        return _api_call("POST", "/nodes", body, timeout=30)

    @mcp.tool()
    def delete_node(node_id: int, stop_running: bool = False) -> str:
        """Delete a node entry. Optional clean undeploy of attached running instances.

        Default behavior (stop_running=False): deletes the node record, leaves
        instance records in DB as orphans — no cleanup on remote nodes.

        With stop_running=True: triggers remote undeploy chain for all running
        instances first (fire-and-forget), then deletes the node record.

        Args:
            node_id: ID of the node to delete
            stop_running: If true, run remote undeploy on all running instances before
                         deleting (default: false). Use when instances need cleanup.
        """
        path = f"/nodes/{node_id}"
        if stop_running:
            return _api_call("DELETE", f"{path}?stop_running=true", timeout=30)
        return _api_call("DELETE", path, timeout=30)

    @mcp.tool()
    def discover_node(node_id: int) -> str:
        """Re-validate a node by running hardware discovery via SSH.

        Refreshes CPU, RAM, OS, GPU, and capabilities info from the remote node.
        Useful after hardware changes or network reconfiguration.

        Uses 30s timeout — SSH connection + hardware discovery takes time.
        Reports SSH failures (wrong IP/port/keys) as error responses.

        Args:
            node_id: ID of the node to discover
        """
        return _api_call("POST", f"/nodes/{node_id}/discover", timeout=30)

    @mcp.tool()
    def toggle_node_active(node_id: int, is_active: bool) -> str:
        """Toggle a node's admin active/inactive state.

        When inactive, the node is excluded from ping checks, instance lists,
        and most operations (returns NODE_INACTIVE error). This is an admin
        'do not touch' flag — separate from ping connectivity (ping_state).

        Args:
            node_id: ID of the node to toggle
            is_active: true = active (default operations), false = inactive (locked)
        """
        return _api_call("PUT", f"/nodes/{node_id}/host-status", {"is_active": 1 if is_active else 0}, timeout=5)


    @mcp.tool()
    def bind_rpc(instance_id: int, rpc_ids: list[int], split_mode: str = "layer") -> str:
        """Bind RPC instances to a llama-server instance.

        Pure DB update — bindings take effect on next deploy/restart from the
        herd page buttons (not automatically on this call).

        Fast (sub-1s) DB-only operation. RPC states verified at deploy/restart time.

        Args:
            instance_id: llama-server instance ID to bind to
            rpc_ids: List of RPC instance IDs to bind (must be llama_rpc engine type)
            split_mode: Split mode for tensor distribution: "layer", "tensor", or "row"
        """
        body = {"rpc_ids": rpc_ids}
        if split_mode:
            body["split_mode"] = split_mode
        return _api_call("POST", f"/instances/{instance_id}/bind-rpc", body, timeout=10)

    @mcp.tool()
    def unbind_rpc(instance_id: int, rpc_id: int) -> str:
        """Remove a single RPC binding from a llama-server instance.

        Pure DB update — bindings take effect on next deploy/restart from the
        herd page buttons (not automatically on this call).

        Uses 10s timeout — fast DB update.

        Args:
            instance_id: llama-server instance ID to unbind from
            rpc_id: RPC instance ID to remove from binding
        """
        return _api_call("DELETE", f"/instances/{instance_id}/bind-rpc/{rpc_id}", timeout=10)

    @mcp.tool()
    def scan_models(engine_type: str = QR_ENGINE_LLAMA_SERVER_NAME, node_id: int = None) -> str:
        """Scan nodes for GGUF model files.

        Scans all active nodes (or a specific node) for GGUF model files.
        Results are upserted into the engine_models table.

        Uses 3s timeout — triggers playbook execution in background.
        Poll results via list_models() to see discovered models.

        Args:
            engine_type: Engine type filter (default: llama_server). Empty string returns all engines.
            node_id: Optional single-node target. If None, scans all active nodes.
        """
        _et = _normalize_engine_type(engine_type)
        if _et is None:
            # Engine-agnostic scan — requires node_id
            if node_id is None:
                return json.dumps({"error": "node_id required when engine_type is empty (agnostic mode)"})
            return _api_call("POST", "/models/scan", {"node": node_id}, timeout=15)
        # Engine-specific scan via playbook
        params = f"?engine_type={_et}"
        if node_id is not None:
            params += f"&node_id={node_id}"
        return _api_call("POST", f"/engine/{_et}/models/scan{params}", timeout=15)


# ============================================================================
# PROXY TOOL
# ============================================================================

if ALLOW_PROXY:
    @mcp.tool()
    def quickrobot_api(method: str, path: str, body = None) -> str:
        """Make a direct API call to the quickrobot server.

        Use for endpoints not covered by dedicated tools above.
        All paths are relative to /api/v1 — include the leading slash.

        Endpoint structure (non-exhaustive):
          GET    /instances/<id>            list or get instance
          GET    /instances/<id>/status     status + merged_config
          POST   /instances/<id>/deploy     deploy/redeploy instance
          POST   /instances/<id>/start      start stopped instance
          PUT    /instances/<id>            change preset or update config
          DELETE /instances/<id>            delete instance
          GET    /engine/<type>/presets     list presets (type: llama_server, llama_rpc)
          PUT    /engine/<type>/presets/<id> update a preset's config_template
          GET    /engine/<type>/models      list models
          POST   /benchmarks/run            run benchmark
          GET    /nodes                     list nodes
          POST   /nodes                     create node

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            path: API path including leading / — e.g., /engine/llama_server/presets/148
            body: JSON object for POST/PUT/PATCH. Must be a dict, not a string.

        Returns:
            Raw API response as JSON string.
        """
        return _api_call(method.upper(), path, body)


# ============================================================================
# GLOBAL RESOURCES — Dynamic registration from engine_prompts table
# SKILL.md and SKILL_MCP.md are no longer hardcoded; they are registered
# dynamically from the DB at startup via _register_skill_resources().
# See migration 012_skills_to_prompts.sql for DB entries.
# ============================================================================


# ============================================================================
# Main
if __name__ == "__main__":
    # Register MCP prompts (MCP-PROMPTS, 2026-07-12)
    try:
        from engine.qr_mcp_prompts import register_prompts as _register_prompts
        _register_prompts(mcp)
        print("[mcp] Prompts registered", flush=True)
    except Exception as exc:
        print(f"[mcp] Prompt registration skipped: {exc}", flush=True)

    # Register skill resources from DB (SKILLS-MIGRATION, 2026-07-13)
    try:
        from engine.qr_mcp_prompts import register_skill_resources as _register_skill_resources
        _register_skill_resources(mcp)
        print("[mcp] Skill resources registered from DB", flush=True)
    except Exception as exc:
        print(f"[mcp] Skill resource registration skipped: {exc}", flush=True)

    # Root guard — refuse to run as root (non-interactive HTTP server)
    if _os.getuid() == 0:
        print("this robot won't run as root", file=_sys.stderr)
        _sys.exit(1)

    parser = _argparse.ArgumentParser(description="Quickrobot MCP SSE Server")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (overrides QUICKROBOT_MCP_PORT)")
    parser.add_argument("--host", type=str, default=None, help="Host to bind (overrides QUICKROBOT_MCP_HOST)")
    parser.add_argument("--api-host", type=str, default=None, help="Quickrobot API server host (overrides QUICKROBOT_API_HOST)")
    parser.add_argument("--api-port", type=int, default=None, help="Quickrobot API server port (overrides QUICKROBOT_API_PORT)")
    parser.add_argument("--api-token", type=str, default=None, help="Bearer token for API auth (overrides QUICKROBOT_API_BEARER_TOKEN)")
    parser.add_argument("--cors-origins", type=str, default=None, help="CORS origins (comma-separated, overrides QUICKROBOT_MCP_CORS_ORIGINS). Default: *")
    args = parser.parse_args()

    # Build API base URL: CLI args take priority over env vars
    api_host_val = args.api_host or os.getenv("QUICKROBOT_API_HOST")
    api_port_val = args.api_port or os.getenv("QUICKROBOT_API_PORT")
    if api_host_val and api_port_val:
        _api_base = f"http://{api_host_val}:{api_port_val}/api/v1"
    else:
        raise RuntimeError("API base URL not set in CLI args or QUICKROBOT_API_HOST/PORT env vars")

    # Set module-level API_BASE (re-read after argparse)
    import sys as _sys_mod
    _mcp_mod = _sys_mod.modules[__name__]
    _mcp_mod.API_BASE = _api_base

    # WRITE and PROXY imply READ (per design: reads_allowed = allow_reads or allow_writes or allow_proxy)
    _reads_implied = (ALLOW_WRITES or ALLOW_PROXY) and not ALLOW_READS
    if _reads_implied:
        ALLOW_READS = True
        print("[qr] MCP started with WRITE/PROXY flag — READ is implied", flush=True)

    # Set bind host: CLI arg takes priority over env var
    host = args.host or os.getenv("QUICKROBOT_MCP_HOST")
    if not host:
        raise RuntimeError("MCP host not set: use --host CLI arg or QUICKROBOT_MCP_HOST env var")
    if host in QR_FORBIDDEN_HOSTS:
        print(f"[mcp] FATAL: Bind host '{host}' is forbidden.", flush=True)
        sys.exit(1)

    # Read port: CLI arg first, then env var. Ensure it's always an int (not None).
    _env_port = os.getenv("QUICKROBOT_MCP_PORT")
    port_val = int(args.port) if args.port else int(_env_port) if _env_port else None
    if not port_val:
        raise RuntimeError("MCP port not set: use --port CLI arg or QUICKROBOT_MCP_PORT env var")

    # CORS origins: CLI > env var > default ["*"]
    _cors_raw = os.getenv("QUICKROBOT_MCP_CORS_ORIGINS", "").strip()
    if args.cors_origins:
        cors_origins = [o.strip() for o in args.cors_origins.split(",") if o.strip()] or ["*"]
    elif _cors_raw:
        cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
    else:
        cors_origins = ["*"]

    # Log rotation (vC): truncate oversized log files on startup
    from lib.lib_system_engine import get_engine_log_path as _eng_log, rotate_log_if_needed as _rot
    _rot(_eng_log("mcp"), "mcp")
    # Structured startup log — single line with all config info minus tokens
    _pid = os.getpid()
    _log_path = os.getenv("QUICKROBOT_LOG_PATH", "")
    _log_suffix = f" log_path={_log_path}" if _log_path else ""
    print(f"[mcp] STARTUP: pid={_pid} host={host} port={port_val} api={_api_base} read={_mcp_mod.ALLOW_READS} write={_mcp_mod.ALLOW_WRITES} proxy={_mcp_mod.ALLOW_PROXY} cors={cors_origins}{_log_suffix}", flush=True)

    # === Startup API connectivity validation with retries ===
    import time as _time_mod
    import requests as _requests_lib
    _api_connect_retries = int(os.getenv("QUICKROBOT_MCP_CONNECT_RETRIES", str(QR_MCP_CONNECT_RETRIES)))
    _api_connect_delay = int(os.getenv("QUICKROBOT_MCP_CONNECT_DELAY", str(QR_MCP_CONNECT_DELAY)))
    _api_url = f"{_api_base}/app/status"
    
    print(f"[mcp] Checking API connectivity at {_api_url}...", flush=True)
    _api_reachable = False
    
    for _retry in range(1, _api_connect_retries + 1):
        try:
            _resp = _requests_lib.get(_api_url, timeout=10)
            if _resp.status_code == 200:
                _data = _resp.json()
                if _data.get("status") == "ok":
                    _api_reachable = True
                    print(f"[mcp] API reachable on attempt {_retry}/{_api_connect_retries}", flush=True)
                    break
                else:
                    print(f"[mcp] API returned non-OK status on attempt {_retry}: {_data.get('status', 'unknown')}", flush=True)
            else:
                print(f"[mcp] API HTTP error {_resp.status_code} on attempt {_retry}", flush=True)
        except _requests_lib.ConnectionError as _e:
            print(f"[mcp] Connection error (attempt {_retry}/{_api_connect_retries}): {_e}", flush=True)
        except _requests_lib.Timeout as _e:
            print(f"[mcp] Timeout (attempt {_retry}/{_api_connect_retries}): {_e}", flush=True)
        except Exception as _e:
            print(f"[mcp] Unexpected error (attempt {_retry}/{_api_connect_retries}): {_e}", flush=True)
        
        if _retry < _api_connect_retries:
            print(f"[mcp] Retrying in {_api_connect_delay}s...", flush=True)
            _time_mod.sleep(_api_connect_delay)
    
    if not _api_reachable:
        print(f"[mcp] FATAL: API unreachable after {_api_connect_retries} attempts. "
              f"Checking: http://{api_host_val}:{api_port_val}/api/v1/app/status", flush=True)
        sys.exit(1)

    # === Start periodic health check thread ===
    from lib.lib_system_engine import start_health_check_thread as _start_health
    _health_thread = _start_health(
        api_host=api_host_val,
        api_port=api_port_val,
        max_retries=1,
        retry_delay=5,
        check_interval=10
    )
    print(f"[mcp] Health check thread started (interval=10s, kill=10s)", flush=True)

    # MCP server — SSE transport only (reverted from broken dual transport, 2026-07-14).
    # The dual transport (sse_app + streamable_http mounted on custom Starlette)
    # broke session management: initialize returns 202 "Accepted" but subsequent
    # messages to the same session get "Could not find session".
    # FastMCP.run(transport="sse") uses host/port from constructor, not run() args.
    print(f"[mcp] Transport: SSE only (llama.cpp UI). Reverted 2026-07-14.", flush=True)
    mcp.run(transport="sse")
