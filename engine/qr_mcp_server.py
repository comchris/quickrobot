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
  QUICKROBOT_API_PORT: Quickrobot API server port (e.g., 8040)
  QUICKROBOT_MCP_HOST: MCP listen address (default: 127.0.0.1)
  QUICKROBOT_MCP_PORT: MCP listen port (default: 8042)
  QUICKROBOT_MCP_READ: Expose read tools (default: false)
  QUICKROBOT_MCP_WRITE: Expose write tools (default: false)
  QUICKROBOT_MCP_FULLPROXY: Expose raw API proxy tool (default: false)
  QUICKROBOT_API_BEARER_TOKEN: Bearer token for API authentication

Usage:
  python engine/qr_mcp_server.py --port 8042
"""
import sys as _sys
import os as _os

# Fix: when run from project root, engine/ subdir shadows stdlib subprocess.
# Remove 'engine' from sys.path[0] so stdlib modules resolve correctly.
_sys_path_0 = _sys.path[0] if _sys.path[0] else _os.getcwd()
if _sys_path_0.endswith("/engine") or _sys_path_0.endswith("engine"):
    del _sys.path[0]

import argparse
import json
import os
import sys
import asyncio



from lib.qr_engine_ids import QR_FORBIDDEN_HOSTS

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings as _TSS

# Default allowed hosts for MCP server host validation
_DEFAULT_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def _parse_allowed_hosts():
    """Parse QUICKROBOT_MCP_ALLOWED_HOSTS env var into a list of host patterns.

    Format: comma-separated host patterns with optional port wildcards.
    Examples: "*,192.168.31.40:*" or "192.168.31.40:8033,localhost:*"
    If empty or unset, uses defaults + MCP's own host:port wildcard.
    """
    raw = os.getenv("QUICKROBOT_MCP_ALLOWED_HOSTS", "").strip()
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip()]
    # Fallback: defaults + MCP's own host wildcard
    mcp_host = os.getenv("QUICKROBOT_MCP_HOST", "127.0.0.1")
    defaults = list(_DEFAULT_ALLOWED_HOSTS)
    # Avoid duplicate entries
    host_pattern = f"{mcp_host}:*"
    if host_pattern not in defaults:
        defaults.append(host_pattern)
    return defaults


# Configuration from environment — all keys prefixed with QUICKROBOT_
_api_host = os.getenv("QUICKROBOT_API_HOST")
_api_port = os.getenv("QUICKROBOT_API_PORT")
if _api_host and _api_port:
    API_BASE = f"http://{_api_host}:{_api_port}/api/v1"
else:
    raise RuntimeError(
        "MCP server needs QUICKROBOT_API_HOST + QUICKROBOT_API_PORT in .quickrobot.env"
    )
ALLOW_READS = os.getenv("MCP_ALLOW_READS", "false").lower() in ("true", "1", "yes")
ALLOW_WRITES = os.getenv("MCP_ALLOW_WRITES", "false").lower() in ("true", "1", "yes")
ALLOW_PROXY = os.getenv("MCP_ALLOW_PROXY", "false").lower() in ("true", "1", "yes")

# Host validation settings for MCP server (controls 421 Misdirected Request errors)
_disable_dns_rebind = os.getenv("QUICKROBOT_MCP_DISABLE_DNS_REBINDING", "false").lower() in ("true", "1", "yes")
if _disable_dns_rebind:
    _transport_security = _TSS(enable_dns_rebinding_protection=False, allowed_hosts=[])
else:
    _allowed_hosts = _parse_allowed_hosts()
    _transport_security = _TSS(allowed_hosts=_allowed_hosts)

# Create MCP server with SSE host/port config and host validation
_mcp_host = os.getenv("QUICKROBOT_MCP_HOST")
_mcp_port_raw = os.getenv("QUICKROBOT_MCP_PORT")
if not _mcp_host:
    raise RuntimeError("MCP server needs QUICKROBOT_MCP_HOST in .quickrobot.env")
if not _mcp_port_raw:
    raise RuntimeError("MCP server needs QUICKROBOT_MCP_PORT in .quickrobot.env")
mcp_host_env = _mcp_host
mcp_port_env = int(_mcp_port_raw)
mcp = FastMCP(
    "QuickrobotAPI",
    host=mcp_host_env,
    port=mcp_port_env,
    transport_security=_transport_security,
)


def _api_call(method, path, body=None):
    """Make an HTTP call to the quickrobot API."""
    import requests as _requests
    url = f"{API_BASE}{path}"
    headers = {"Content-Type": "application/json"} if body else {}
    try:
        if method == "GET":
            r = _requests.get(url, headers=headers, timeout=30)
        elif method == "POST":
            r = _requests.post(url, json=body, headers=headers, timeout=60)
        elif method == "PUT":
            r = _requests.put(url, json=body, headers=headers, timeout=60)
        elif method == "DELETE":
            r = _requests.delete(url, headers=headers, timeout=30)
        elif method == "PATCH":
            r = _requests.patch(url, json=body, headers=headers, timeout=30)
        else:
            return json.dumps({"error": f"Unknown method: {method}"})
        if r.status_code >= 400:
            return json.dumps({"status": "error", "code": r.status_code, "message": r.text[:500]})
        return r.text
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ============================================================================
# READ TOOLS
# ============================================================================

if ALLOW_READS:
    @mcp.tool()
    def list_instances() -> str:
        """Full instance list with config_override, ansible_vars, uuids, etc. Prefer list_instances_summary() for operational overview — 95% less data."""
        return _api_call("GET", "/instances")

    @mcp.tool()
    def get_instance_status(instance_id: int) -> str:
        """Detailed status of a specific instance including merged_config (env+cli_opts)."""
        return _api_call("GET", f"/instances/{instance_id}/status")

    @mcp.tool()
    def list_nodes() -> str:
        """Full node list with capabilities JSON (hardware inventory). Prefer list_nodes_summary() — 98% less data."""
        return _api_call("GET", "/nodes")

    @mcp.tool()
    def list_presets(engine_type: str = "llama_server") -> str:
        """List presets with full config_template JSON (env, cli_opts). Prefer list_presets_summary() for selection — 77% less data."""
        return _api_call("GET", f"/engine/{engine_type}/presets")

    @mcp.tool()
    def get_preset(preset_id: int, engine_type: str = "llama_server") -> str:
        """Details of a specific preset including full config_template."""
        return _api_call("GET", f"/engine/{engine_type}/presets/{preset_id}")

    @mcp.tool()
    def list_models(engine_type: str = "llama_server") -> str:
        """List models with sha256 hashes, verification timestamps, model_params. Prefer list_models_summary() — 79% less data."""
        return _api_call("GET", f"/engine/{engine_type}/models")

    @mcp.tool()
    def get_model(model_id: int, engine_type: str = "llama_server") -> str:
        """Detailed information about a specific model including all fields."""
        return _api_call("GET", f"/engine/{engine_type}/models/{model_id}")

    @mcp.tool()
    def list_instances_summary() -> str:
        """Compact instance list (id,name,state,engine,node,port). Prefer for all operational decisions — 95% less data than list_instances()."""
        raw = _api_call("GET", "/instances")
        try:
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else []
            compact = []
            for inst in items:
                compact.append({
                    "id": inst.get("id"),
                    "name": inst.get("name"),
                    "state": inst.get("state"),
                    "engine_type_name": inst.get("engine_type_name"),
                    "node_hostname": inst.get("node_hostname"),
                    "port_assigned": inst.get("port_assigned"),
                    "system_managed": inst.get("system_managed", False),
                    "has_custom_config": inst.get("has_custom_config", False),
                })
            return json.dumps({"status": "ok", "total": len(compact), "items": compact})
        except Exception:
            return raw

    @mcp.tool()
    def list_nodes_summary() -> str:
        """Compact node list (id,name,hostname,status,ping_state). Prefer for availability checks — 98% less data than list_nodes()."""
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
        except Exception:
            return raw

    @mcp.tool()
    def list_presets_summary(engine_type: str = "llama_server") -> str:
        """Compact preset list (id,name,category,model_name,gpu_device). Prefer for selection — 77% less data."""
        raw = _api_call("GET", f"/engine/{engine_type}/presets")
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
        except Exception:
            return raw

    @mcp.tool()
    def list_models_summary(engine_type: str = "llama_server") -> str:
        """Compact model list (id,name,path,quant,size,preset_count). Prefer for selection — 79% less data."""
        raw = _api_call("GET", f"/engine/{engine_type}/models")
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
        except Exception:
            return raw

    @mcp.tool()
    def list_benchmark_prompts() -> str:
        """List all benchmark prompts available for selection. Each prompt has id, name, and text content."""
        return _api_call("GET", "/benchmarks/prompts")

    @mcp.tool()
    def run_benchmark(instance_id: int, prompt_id: int, timeout_seconds: int = None) -> str:
        """Run a benchmark on a running instance using the specified prompt.

        Args:
            instance_id: Running llama_server instance to benchmark
            prompt_id: Prompt ID to use (list with list_benchmark_prompts() first)
            timeout_seconds: Optional custom timeout (default: auto-calculated from prompt)
        """
        body = {"instance_id": instance_id, "prompt_id": prompt_id}
        if timeout_seconds:
            body["timeout_seconds"] = timeout_seconds
        return _api_call("POST", "/benchmarks/run", body)

    @mcp.tool()
    def list_benchmark_results(instance_id: int = None) -> str:
        """List benchmark results. Optional instance_id filter to see only results for one instance."""
        path = "/benchmarks/results"
        if instance_id:
            path += f"?instance_id={instance_id}"
        return _api_call("GET", path)


# ============================================================================
# WRITE TOOLS
# ============================================================================

if ALLOW_WRITES:
    @mcp.tool()
    def create_instance(
        name: str,
        engine_type_id: int = 21,
        node_id: int = None,
        preset_id: int = None,
        config_override: dict = None
    ) -> str:
        """Create a new instance.

        Args:
            name: Instance display name
            engine_type_id: Engine type (21=llama_server, 22=rpc, 31=iperf3, 12=subprocess)
            node_id: Target node ID (required for remote engines)
            preset_id: Preset ID to use
            config_override: Additional config overrides
        """
        body = {"name": name, "engine_type_id": engine_type_id}
        if node_id is not None:
            body["node_id"] = node_id
        if preset_id is not None:
            body["preset_id"] = preset_id
        if config_override:
            body["config_override"] = config_override
        return _api_call("POST", "/instances", body)

    @mcp.tool()
    def deploy_instance(instance_id: int, start_after_deploy: bool = False) -> str:
        """Deploy an instance (build + systemd unit).

        Args:
            instance_id: Instance to deploy
            start_after_deploy: Auto-start after deploy (default: false)
        """
        body = {}
        if start_after_deploy:
            body["start_after_deploy"] = True
        return _api_call("POST", f"/instances/{instance_id}/deploy", body or None)

    @mcp.tool()
    def start_instance(instance_id: int) -> str:
        """Start an instance."""
        return _api_call("POST", f"/instances/{instance_id}/start")

    @mcp.tool()
    def stop_instance(instance_id: int) -> str:
        """Stop an instance."""
        return _api_call("POST", f"/instances/{instance_id}/stop")

    @mcp.tool()
    def restart_instance(instance_id: int) -> str:
        """Restart an instance."""
        return _api_call("POST", f"/instances/{instance_id}/restart")

    @mcp.tool()
    def delete_instance(instance_id: int, force: bool = False) -> str:
        """Delete an instance.

        Args:
            instance_id: Instance to delete
            force: Force delete without checking state (default: false)
        """
        path = f"/instances/{instance_id}"
        if force:
            return _api_call("POST", f"{path}/force-delete")
        return _api_call("DELETE", path)


# ============================================================================
# PROXY TOOL
# ============================================================================

if ALLOW_PROXY:
    @mcp.tool()
    def quickrobot_api(method: str, path: str, body: dict = None) -> str:
        """Make a direct API call to the quickrobot server.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            path: API path (e.g., /instances/5/status)
            body: JSON body for POST/PUT/PATCH requests (optional)

        Returns:
            Raw API response as JSON string.
        """
        return _api_call(method.upper(), path, body)


# ============================================================================
# Main
if __name__ == "__main__":
    import argparse as _argparse

    parser = _argparse.ArgumentParser(description="Quickrobot MCP SSE Server")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (overrides QUICKROBOT_MCP_PORT)")
    parser.add_argument("--host", type=str, default=None, help="Host to bind (overrides QUICKROBOT_MCP_HOST)")
    parser.add_argument("--api-host", type=str, default=None, help="Quickrobot API server host (overrides QUICKROBOT_API_HOST)")
    parser.add_argument("--api-port", type=int, default=None, help="Quickrobot API server port (overrides QUICKROBOT_API_PORT)")
    parser.add_argument("--api-token", type=str, default=None, help="Bearer token for API auth (overrides QUICKROBOT_API_BEARER_TOKEN)")
    parser.add_argument("--read", action="store_true", default=False, help="Enable read-only tools (overrides QUICKROBOT_MCP_READ)")
    parser.add_argument("--write", action="store_true", default=False, help="Enable write tools (overrides QUICKROBOT_MCP_WRITE)")
    parser.add_argument("--proxy", action="store_true", default=False, help="Enable proxy tool (overrides QUICKROBOT_MCP_FULLPROXY)")
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

    # Re-read allow flags: CLI args take priority over env vars
    _mcp_mod.ALLOW_READS = args.read if args.read else os.getenv("QUICKROBOT_MCP_READ", "false").lower() in ("true", "1", "yes")
    _mcp_mod.ALLOW_WRITES = args.write if args.write else os.getenv("QUICKROBOT_MCP_WRITE", "false").lower() in ("true", "1", "yes")
    _mcp_mod.ALLOW_PROXY = args.proxy if args.proxy else os.getenv("QUICKROBOT_MCP_FULLPROXY", "false").lower() in ("true", "1", "yes")

    # WRITE and PROXY imply READ (per design: reads_allowed = allow_reads or allow_writes or allow_proxy)
    _reads_implied = (_mcp_mod.ALLOW_WRITES or _mcp_mod.ALLOW_PROXY) and not _mcp_mod.ALLOW_READS
    if _reads_implied:
        _mcp_mod.ALLOW_READS = True
        print("[qr] MCP started with WRITE flag — READ is implied", flush=True)

    # Set bind host: CLI arg takes priority over env var
    host = args.host or os.getenv("QUICKROBOT_MCP_HOST")
    if not host:
        raise RuntimeError("MCP host not set: use --host CLI arg or QUICKROBOT_MCP_HOST env var")
    if host in QR_FORBIDDEN_HOSTS:
        print(f"[mcp] FATAL: Bind host '{host}' is forbidden.", flush=True)
        sys.exit(1)

    port_val = args.port or int(os.getenv("QUICKROBOT_MCP_PORT")) if os.getenv("QUICKROBOT_MCP_PORT") else None
    if not port_val:
        raise RuntimeError("MCP port not set: use --port CLI arg or QUICKROBOT_MCP_PORT env var")
    print(f"[mcp] Starting on {host}:{port_val}", flush=True)
    print(f"[mcp] API base: {_api_base}", flush=True)
    print(f"[qr] MCP server: using {sys.executable} at engine/qr_mcp_server.py", flush=True)
    print(f"[mcp] allow_reads={_mcp_mod.ALLOW_READS} allow_writes={_mcp_mod.ALLOW_WRITES} allow_proxy={_mcp_mod.ALLOW_PROXY}", flush=True)

# Use streamable-HTTP transport in stateless mode — no session ID required,
# compatible with MCP clients that don't handle session management.
    from starlette.middleware.cors import CORSMiddleware as _CORSMiddleware
    import uvicorn as _uvicorn

    # Configure FastMCP settings for stateless HTTP (no session management)
    mcp.settings.streamable_http_path = "/sse"
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True  # Return responses in HTTP body, not just SSE

    # Get the streamable HTTP app
    http_app = mcp.streamable_http_app()

    # Add CORS middleware for browser access
    http_app.add_middleware(
        _CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    print(f"[mcp] CORS enabled (stateless HTTP mode)", flush=True)
    config = _uvicorn.Config(
        http_app,
        host=host,
        port=args.port,
        log_level="info",
    )
    server = _uvicorn.Server(config)
    import anyio as _anyio
    _anyio.run(server.serve)
