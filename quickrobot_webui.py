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

#!/usr/bin/env python3
"""Quickrobot v0.02 -- Minimal Web UI Server.

Flask app providing a lightweight web interface for the quickrobot LAN controller.
Binds to 0.0.0.0 (LAN accessible) for remote access from managed nodes.

Routes:
    /webui/             Main dashboard with nav sidebar
    /webui/hosts        List of all managed hosts/nodes
    /webui/engines      List of all engine types
    /webui/instances    List of all instances
    /webui/instances/<id>  Instance detail page
    /webui/nodes/<id>      Node detail page
    /api/<path>           Reverse proxy to quickrobot API server

Usage:
    python3 webui_server.py --port 8041
"""

import os
import argparse
import json
import socket
import sys
from datetime import datetime

from lib.qr_engine_ids import (
    QR_DEFAULT_LOCALHOST,
    QR_ENGINE_API_NAME, QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC,
    QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_MCP_NAME, QR_ENGINE_SCHEDULER_NAME, QR_ENGINE_SUBPROCESS_NAME,
    QR_ENGINE_UNIVERSAL_NAME, QR_ENGINE_WEBUI, QR_ENGINE_WEBUI_NAME,
    QR_FORBIDDEN_HOSTS,
    _QR_NAV_DISPLAY_NAMES, _QR_NAV_LLAMA_NAMES, _QR_NAV_NO_CONFIG,
    _QR_NAV_SHORT_ALIASES, _QR_NAV_SECTION_MAP, _QR_SYSTEM_NAMES,
    _QR_EMPTY,
    get_id_by_name, is_llamacpp_engine,
)

# Root guard — same as main process, refuse to run as root
if os.getuid() == 0:
    print("this robot won't run as root", file=sys.stderr)
    sys.exit(1)

from flask import Flask, request, Response, jsonify, redirect, url_for, render_template, send_from_directory
from markupsafe import Markup

from lib.lib_constants import DEFAULT_ANSIBLE_USER, VERSION, DEFAULT_TIMEZONE
from lib.qr_engine_registry import is_system_engine, get_engine_by_name, get_display_name
from qr_api.lib_nodes import find_system_instance as _find_sys_inst
from db.sqlite import pool
from db.adapters.configs import get_polling_intervals
import math

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

app = Flask(__name__, template_folder='webui')

# Register Jinja2 template filters for badge rendering
def _format_bytes_py(bytes_val):
    """Format bytes to human-readable string (Jinja2 global)."""
    if bytes_val is None or bytes_val == '':
        return '\u2014'
    try:
        b = int(bytes_val)
    except (ValueError, TypeError):
        return '\u2014'
    if b == 0:
        return '0 B'
    k = 1024
    sizes = ['B', 'KB', 'MB']
    i = min(int(math.log(b) / math.log(k)), len(sizes) - 1)
    return '{:.1f} {}'.format(b / math.pow(k, i), sizes[i])


@app.context_processor
def utility_processor():
    """Make helper functions and global vars available in all templates."""
    from flask import g
    tz_name = getattr(g, "tz_name", DEFAULT_TIMEZONE)
    app_status = get_app_status()
    is_dev = app_status.get("data", {}).get("mode", "prod") == "dev"
    # Extract instance summary for global status indicator
    qr_data = app_status.get("data", {})
    return dict(
         status_badge=status_badge,
         node_status_badge=node_status_badge,
         system_badge=system_badge,
         gpu_device_badge=gpu_device_badge,
         version=VERSION,
         tz_name=tz_name,
         qr_is_dev=is_dev,
         qr_status_global=qr_data.get("global_state", "idle"),
         qr_status_rgb=qr_data.get("global_state_rgb", "rgb(255, 255, 255)"),
         qr_status_counts_json=json.dumps(qr_data.get("instance_counts", {})),
         qr_status_tooltip=qr_data.get("global_state_tooltip", ""),
         formatBytes=_format_bytes_py,
     )


@app.before_request
def _load_webui_timezone():
    """Read web_ui_timezone from engine_configs table directly (no API call).

    Skips /api/v1/webui/config itself to avoid recursion.
    Falls back to 'Europe/Berlin' if the DB is unavailable or the key is missing.
    """
    from flask import g
    if request.path == "/api/v1/webui/config":
        return
    if hasattr(g, "tz_name"):
        return
    # Engine ID for quickrobot-webui (system-managed)
    _WEBUI_ENGINE_ID = QR_ENGINE_WEBUI
    tz_name = DEFAULT_TIMEZONE
    try:
        with pool(os.path.join(os.getcwd(), "data", "quickrobot.db")) as conn:
            row = conn.execute(
                "SELECT value FROM engine_configs WHERE engine_type_id = ? AND key = ?",
                (_WEBUI_ENGINE_ID, "web_ui_timezone"),
            ).fetchone()
        if row and isinstance(row["value"], str) and row["value"].strip():
            tz_name = row["value"]
    except Exception:
        pass  # fallback stays as 'Europe/Berlin'
    g.tz_name = tz_name

app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True
import json as _json
def _fromjson(s):
    """Parse a JSON string to Python object (for Jinja2 templates)."""
    if isinstance(s, str):
        return _json.loads(s)
    return s
app.jinja_env.filters['fromjson'] = _fromjson

# Configuration — resolved at runtime from environment, no hardcoded defaults
def _resolve_api_base():
    """Resolve API base URL from environment or crash if not set."""
    custom = os.environ.get("QR_API_BASE")
    if custom:
        return custom
    host = os.environ.get("QUICKROBOT_API_HOST")
    port = os.environ.get("QUICKROBOT_API_PORT")
    if host and port:
        return f"http://{host}:{port}/api/v1"
    raise RuntimeError(
        "API base URL not set: set QR_API_BASE env var, or define "
        "QUICKROBOT_API_HOST + QUICKROBOT_API_PORT in .quickrobot.env"
    )

CONFIG = {
    "api_base": _resolve_api_base(),
}

# Load engine registry for is_system_engine() filtering
try:
    _db_path_wui = os.path.join(os.getcwd(), "data", "quickrobot.db")
    from lib.qr_engine_registry import load_and_verify_registry as _load_reg
    _load_reg(_db_path_wui)
except Exception:
    pass  # Non-critical — will fall back gracefully


# ---------------------------------------------------------------------------
# API client helper
# ---------------------------------------------------------------------------

def api_get(path, params=None):
    """Fetch JSON data from the quickrobot API server.

    Args:
        path: API path (e.g., 'instances' for /api/v1/instances).
        params: Optional query parameters dict.

    Returns:
        dict with API response, or None on error.
    """
    import urllib.request
    import urllib.error
    url = f"{CONFIG['api_base']}/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{qs}"

    try:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=60) as resp:
            import json
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def api_post(path, data=None):
    """POST JSON data to the quickrobot API server.

    Args:
        path: API path (e.g., 'instances' for /api/v1/instances).
        data: Dict of JSON body to send.

    Returns:
        dict with API response, or {"error": str} on failure.
    """
    import urllib.request
    import json as _json
    url = f"{CONFIG['api_base']}/{path}"
    body = _json.dumps(data).encode() if data else b'{}'
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return _json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def api_delete(path):
    """DELETE a resource via the quickrobot API server.

    Args:
        path: API path (e.g., 'instances/5' for DELETE /api/v1/instances/5).

    Returns:
        dict with API response, or {"error": str} on failure.
    """
    import urllib.request
    import json as _json
    url = f"{CONFIG['api_base']}/{path}"
    try:
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return _json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Jinja2 inline templates (rendered from strings)
# ---------------------------------------------------------------------------

BASE_LAYOUT = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quickrobot v0.02 -- {title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          display: flex; min-height: 100vh; background: #f5f5f5; color: #333; }}

  /* Nav sidebar */
   nav {{ width: 220px; background: #1a1a2e; color: #eee; padding: 0; flex-shrink: 0; }}
   nav .nav-header {{ padding: 20px 16px; border-bottom: 1px solid #333; font-size: 1.1em; font-weight: bold; }}
   nav .nav-header span {{ color: #4fc3f7; }}
   nav ul {{ list-style: none; padding: 8px 0; }}
   nav li a {{ display: block; padding: 10px 16px; color: #bbb; text-decoration: none;
               border-left: 3px solid transparent; transition: all 0.2s; }}
   nav li a:hover {{ background: #16213e; color: #fff; }}
   nav li a.active {{ background: #16213e; color: #4fc3f7; border-left-color: #4fc3f7; }}
   nav .nav-section-header {{ padding: 12px 16px 4px; font-size: 0.75em; text-transform: uppercase;
               color: #666; letter-spacing: 1px; font-weight: 600; }}

  /* Main content */
  main {{ flex: 1; padding: 24px 32px; overflow-y: auto; }}
  main h1 {{ font-size: 1.5em; margin-bottom: 16px; color: #1a1a2e; }}
  main h2 {{ font-size: 1.2em; margin: 16px 0 8px; color: #333; }}

  /* Tables */
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 16px; }}
  th {{ text-align: left; padding: 10px 12px; background: #f8f9fa;
        border-bottom: 2px solid #dee2e6; font-size: 0.85em; color: #666;
        text-transform: uppercase; letter-spacing: 0.5px; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 0.9em; }}
  tr:hover td {{ background: #f0f7ff; }}
  a.row-link {{ color: #4fc3f7; text-decoration: none; cursor: pointer; }}
  a.row-link:hover {{ text-decoration: underline; }}

  /* Status badges */
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
            font-size: 0.8em; font-weight: 600; }}
  .badge-running {{ background: #d4edda; color: #155724; }}
  .badge-stopped {{ background: #f8d7da; color: #721c24; }}
  .badge-error {{ background: #fff3cd; color: #856404; }}
  .badge-other {{ background: #e2e3e5; color: #383d41; }}
  .badge-system {{ background: #cce5ff; color: #004085; }}
  .badge-active {{ background: #d4edda; color: #155724; }}
  .badge-unknown {{ background: #fff3cd; color: #856404; }}
  .badge-failed {{ background: #f8d7da; color: #721c24; }}

  /* Detail cards */
  .detail-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; margin-bottom: 16px; }}
  .detail-card {{ background: #fff; border-radius: 6px; padding: 16px;
                  box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .detail-card label {{ font-size: 0.8em; color: #888; text-transform: uppercase; display: block; margin-bottom: 4px; }}
  .detail-card value {{ font-size: 1.1em; font-weight: 600; color: #1a1a2e; }}

  /* Log output */
  .log-output {{ background: #1a1a2e; color: #d4d4d4; padding: 12px; border-radius: 6px;
                 font-family: 'Consolas', 'Monaco', monospace; font-size: 0.85em;
                 max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }}
  .log-line {{ line-height: 1.6; }}
  .log-success {{ color: #4caf50; }}
  .log-failed {{ color: #f44336; }}
  .log-processing {{ color: #ff9800; }}
  .log-received {{ color: #9e9e9e; }}
  .ansible-log-deploy {{ color: #4caf50; }}
  .ansible-log-failed {{ color: #f44336; }}
  .ansible-log-validate {{ color: #2196f3; }}
  .ansible-log-scan {{ color: #ff9800; }}

  /* Action buttons */
  .actions {{ margin: 12px 0; }}
  .btn {{ display: inline-block; padding: 6px 16px; border: none; border-radius: 4px;
          cursor: pointer; font-size: 0.85em; text-decoration: none; color: #fff; }}
  .btn-primary {{ background: #4fc3f7; color: #1a1a2e; }}
  .btn-danger {{ background: #f44336; }}
  .btn-success {{ background: #4caf50; }}
  .btn:hover {{ opacity: 0.85; }}

  /* Create instance form */
  .form-group {{ margin-bottom: 14px; }}
  .form-group label {{ display: block; font-size: 0.85em; color: #666; margin-bottom: 4px; font-weight: 500; }}
  .form-group input[type="text"], .form-group input[type="number"], .form-group select {{ width: 100%; padding: 7px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9em; }}
  .form-group select:focus, .form-group input:focus {{ outline: none; border-color: #4fc3f7; box-shadow: 0 0 0 2px rgba(79,195,247,0.15); }}
  .form-group small {{ display: block; color: #888; font-size: 0.8em; margin-top: 3px; }}
  .form-row {{ display: flex; gap: 16px; }}
  .form-row .form-group {{ flex: 1; }}
  .form-actions {{ margin-top: 16px; display: flex; gap: 8px; }}
  .engine-card {{ border: 2px solid #e0e0e0; border-radius: 6px; padding: 14px; cursor: pointer; transition: all 0.2s; margin-bottom: 8px; }}
  .engine-card:hover {{ border-color: #4fc3f7; background: #f8fbff; }}
  .engine-card.selected {{ border-color: #4fc3f7; background: #e8f4fd; }}
  .engine-card-name {{ font-weight: 600; color: #1a1a2e; }}
  .engine-card-desc {{ font-size: 0.85em; color: #666; margin-top: 2px; }}
  .preview-panel {{ background: #1a1a2e; color: #d4d4d4; padding: 12px; border-radius: 6px; font-family: 'Consolas', 'Monaco', monospace; font-size: 0.85em; max-height: 300px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }}
  .engine-fields {{ display: none; }}
  .engine-fields.visible {{ display: block; }}

  /* Back link */
  .back-link {{ display: inline-block; margin-bottom: 16px; color: #4fc3f7;
                text-decoration: none; font-size: 0.9em; }}
  .back-link:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<nav>
  <div class="nav-header"><span>Quickrobot</span> v0.02</div>
  <ul>
    <li><a href="/webui/" {dashboard}>Dashboard</a></li>
    <li><a href="/webui/hosts" {hosts}>Hosts</a></li>
    {engines_nav}
    <li><a href="/webui/instances" {instances}>Instances</a></li>
    <li><a href="/webui/ansible-logs" {logs}>Ansible Logs</a></li>
    <li><a href="{{ tasks_href }}" {tasks}>Running Tasks</a></li>
  </ul>
</nav>
<main>
{content}
</main>
</body>
</html>
"""

DASHBOARD_CONTENT = """\
<h1>Quickrobot v0.02 -- Dashboard</h1>
<div class="detail-grid">
<div class="detail-card">
  <label>Total Nodes</label><value>{total_nodes}</value>
</div>
<div class="detail-card">
  <label>Active Nodes</label><value>{active_nodes}</value>
</div>
<div class="detail-card">
  <label>Total Instances</label><value>{total_instances}</value>
</div>
<div class="detail-card">
  <label>Running Instances</label><value>{running_instances}</value>
</div>
<div class="detail-card">
  <label>Engine Types</label><value>{engine_types_count}</value>
</div>
</div>
"""

TABLE_HEADER = """\
<table>
<thead><tr>{headers}</tr></thead>
<tbody>{rows}</tbody>
</table>
"""

# Engine config metadata: descriptions and input types per engine type
# Used by the generic engine config page renderer
ENGINE_CONFIG_META = {
    "llama_rpc": {
        "display_title": "LLAMA.cpp RPC Server Global Engine Config",
        "fields": {
            "base_port": {"description": "Default port for first instance on the remote Host"},
            "binary_path": {"description": "Absolute path to ggml-rpc-server binary on remote host (e.g. /opt/quickrobot/llama.cpp/build/bin/ggml-rpc-server)"},
            "default_timeout": {"description": "Default runtime to be used in seconds for the benchmark"},
            "restart_policy": {"description": "Systemd unit restart policy (always/on-failure/no)"},
            "start_on_boot": {"description": "Enable systemd unit on boot (true/false)"},
            "skip_build": {"description": "Skip cmake rebuild during deploy if binary already exists on remote host"},
            "polling_interval_local_sec": {"description": "Action log polling interval for local instances in seconds (minimum 10)"},
            "polling_interval_remote_sec": {"description": "Action log polling interval for remote nodes in seconds (minimum 10)"},
            "playbook_dir": {"description": "Subdirectory under playbooks/ for custom deploy/undeploy scripts (e.g. 'custom', 'llama')"},
        },
        "dropdowns": ["restart_policy", "start_on_boot", "skip_build"],
    },
   "iperf3": {
          "display_title": "Iperf3 Global Engine Config",
        "fields": {
               "base_port": {"description": "Base port for iperf3 server instance allocation (range 9900-9904)"},
               "restart_policy": {"description": "Systemd restart policy"},
               "start_on_boot": {"description": "Enable systemd unit on boot (true/false)"},
               "target_host": {"description": "Server hostname/IP for client mode (used as target_host in CLI)"},
               "target_port": {"description": "Server port for client mode (used as target_port in CLI)"},
                  "polling_interval_local_sec": {"description": "Action log polling interval for local instances in seconds (minimum 10)"},
                   "polling_interval_remote_sec": {"description": "Action log polling interval for remote nodes in seconds (minimum 10)"},
               },
          "dropdowns": ["restart_policy", "start_on_boot"],
      },
"llama_server": {
             "display_title": "LLAMA.cpp Server Global Engine Config",
             "fields": {
                 "base_port": {"description": "Default port for first instance on the remote Host"},
                 "binary_path": {"description": "Path to llama-server binary on remote Host"},
                 "model_root_path": {"description": "Root path searched by model scan playbook (default: /mnt/llama/gguf/models)"},
                 "restart_policy": {"description": "Systemd unit restart policy"},
                 "start_on_boot": {"description": "Enable systemd unit on boot"},
                 "skip_build": {"description": "Skip cmake rebuild if binary already exists"},
                 "polling_interval_local_sec": {"description": "Action log polling interval for local instances in seconds (minimum 10)"},
                 "polling_interval_remote_sec": {"description": "Action log polling interval for remote nodes in seconds (minimum 10)"},
                 "llama_seed": {"description": "Random seed value passed as LLAMA_ARG_SEED to llama-server (default: 1337)"},
                 "playbook_dir": {"description": "Subdirectory under playbooks/ for custom deploy/undeploy scripts (e.g. 'custom', 'llama')"},
             },
           "dropdowns": ["restart_policy", "start_on_boot", "skip_build"],
        },
  "quickrobot-api": {
         "display_title": "Quickrobot API Service Global Config",
         "fields": {
             "db_path": {"description": "SQLite database file path", "input_type": "text", "editable": True},
             "api_host": {"description": "API server bind address (from .quickrobot.env QUICKROBOT_API_HOST)", "input_type": "text", "editable": False},
             "api_port": {"description": "API server port (from .quickrobot.env QUICKROBOT_API_PORT)", "input_type": "number", "editable": False},
             "ansible_user": {"description": "SSH user for ansible (from .quickrobot.env QUICKROBOT_API_ANSIBLE_SSHUSER)", "input_type": "text", "editable": False},
             "ansible_key_path": {"description": "Path to SSH private key for ansible (from .quickrobot.env QUICKROBOT_API_ANSIBLE_SSHKEY; empty=ssh-agent)", "input_type": "text", "editable": False},
             "playbook_root_dir": {"description": "Playbook root directory (from .quickrobot.env QUICKROBOT_API_PLAYBOOKDIR)", "input_type": "text", "editable": False},
             "ping_command": {"description": "Host reachability ping command template (from .quickrobot.env QUICKROBOT_API_PING_COMMAND)", "input_type": "text", "editable": False},
             "ping_interval": {"description": "Host reachability check interval seconds (min 10, overrides .env QUICKROBOT_API_PING_INTERVAL)", "input_type": "number", "editable": True},
             "polling_interval_local_sec": {"description": "Action log polling interval for local instances (sec, min 10)", "input_type": "number", "editable": True},
             "polling_interval_remote_sec": {"description": "Action log polling interval for remote nodes (sec, min 10)", "input_type": "number", "editable": True},
             "refresh_interval_default_sec": {"description": "Default auto-refresh interval for instance status polling (sec, min 10)", "input_type": "number", "editable": True},
         },
        "dropdowns": [],
        "save_endpoint": "/api/v1/engine/quickrobot-api/config",
    },
   "quickrobot-mcp": {
               "display_title": "Quickrobot MCP Server Global Engine Config",
               "fields": {
                   "mcp_port": {"description": "MCP SSE server bind port"},
                   "mcp_autostart": {"description": "Auto-start MCP on API boot (false=manual start only)"},
                   "mcp_python_interpreter": {"description": "Python interpreter binary for MCP server subprocess (e.g. pipx venv python path; empty=auto-detect)", "input_type": "text", "editable": True},
                   "mcp_allow_reads": {"description": "Expose read-only tools (list_instances, list_nodes, etc.)"},
                   "mcp_allow_writes": {"description": "Expose write tools (create_instance, deploy, start, stop)"},
                   "mcp_allow_proxy": {"description": "Expose raw API proxy tool for direct path access"},
                   "mcp_detach": {"description": "Run MCP in detached process group (survives API death; false=attached, dies with API)"},
                   "polling_interval_local_sec": {"description": "Action log polling interval for local instances in seconds (minimum 10)"},
                   "polling_interval_remote_sec": {"description": "Action log polling interval for remote nodes in seconds (minimum 10)"},
                   "allow_reads": {"description": "Expose read-only tools (list_instances, list_nodes, etc.)", "input_type": "dropdown", "editable": True},
                   "allow_writes": {"description": "Expose write tools (create_instance, deploy, start, stop)", "input_type": "dropdown", "editable": True},
                   "allow_proxy": {"description": "Expose raw API proxy tool for direct path access", "input_type": "dropdown", "editable": True},
               },
             "dropdowns": ["mcp_autostart", "mcp_allow_reads", "mcp_allow_writes", "mcp_allow_proxy", "mcp_detach", "allow_reads", "allow_writes", "allow_proxy"],
             "save_endpoint": "/api/v1/engines/quickrobot-mcp/settings",
         },
    "quickrobot-webui": {
        "display_title": "Dry Nose Ape Control Interface",
        "fields": {
            "web_ui_timezone": {"description": "Display timezone (IANA TZ name)", "input_type": "select", "editable": True},
            "webui_autostart": {"description": "Auto-start WebUI on API start (true/false)", "input_type": "dropdown", "editable": True},
            "webui_detach": {"description": "Run WebUI in detached process group (survives API death)", "input_type": "dropdown", "editable": True},
            "polling_interval_local_sec": {"description": "Polling interval for local instances (sec, min 10)", "input_type": "number", "editable": True},
            "polling_interval_remote_sec": {"description": "Polling interval for remote nodes (sec, min 10)", "input_type": "number", "editable": True},
        },
        "dropdowns": ["webui_autostart", "webui_detach"],
        "save_endpoint": "/api/v1/engines/quickrobot-webui/settings",
    },
 }

def get_app_status():
    """Fetch app-level status from main API (dev mode, version)."""
    try:
        return api_get("app/status")
    except Exception:
        return {}


def render_nav(active, engine_types=None):
    """Return nav dict and structured engines section data.

    Args:
        active: Current page identifier (dashboard/hosts/instances/logs).
        engine_types: Optional list of engine type dicts from the API.

    Returns:
        tuple of (nav_dict, engines_nav_data) where engines_nav_data is a
        list of section dicts {header, items} for use in Jinja2 templates.
    """
    pages = ["dashboard", "hosts", "instances", "models", "logs", "tasks", "engines"]
    nav = {p: ' class="active"' if p == active else "" for p in pages}
    # Preserve query params on Tasks nav link so filters persist across navigation
    _qs = request.query_string.decode() if request.query_string else ""
    nav["tasks_href"] = "/webui/qr-tasks" + ("?" + _qs if _qs else "")

    # Build engine sections: 3 groups (LLAMA.cpp, Misc, System)
    llama_section = {"header": "LLAMA.cpp", "items": []}
    misc_section = {"header": "Misc", "items": []}
    system_section = {"header": "System", "items": []}
    section_objs = {"llama": llama_section, "misc": misc_section, "system": system_section}

    if engine_types and len(engine_types) > 0:
        for et in engine_types:
            et_name = et.get("name", "?")
            # Skip short-name aliases — they'll render via their canonical long-name route
            if et_name in _QR_NAV_SHORT_ALIASES:
                continue
            # Skip engines without a config nav item (from SSOT)
            if et_name in _QR_NAV_NO_CONFIG:
                continue
            # Display name + suffix: LLaMA overrides first (explicit None check for empty string),
            # then display-name dict, then registry fallback.
            _llama_data = _QR_NAV_LLAMA_NAMES.get(et_name)
            if _llama_data is not None:
                _raw_display, _raw_suffix = _llama_data
                et_display = "" if _raw_display == _QR_EMPTY else _raw_display
                suffix = "" if _raw_suffix == _QR_EMPTY else " Config"
            else:
                et_display = _QR_NAV_DISPLAY_NAMES.get(et_name) or get_display_name(et_name)
                suffix = "" if et_name in _QR_SYSTEM_NAMES or et_name == QR_ENGINE_SUBPROCESS_NAME else " Config"
            caps = et.get("capabilities", {})
            if isinstance(caps, str):
                try:
                    import json as _j
                    caps = _j.loads(caps)
                except Exception:
                    caps = {}
            _section_key = _QR_NAV_SECTION_MAP.get(et_name, "system")
            section = section_objs[_section_key]
            has_presets = caps.get("supports_presets") if isinstance(caps, dict) else False
            item = f'<li><a href="/webui/engine/{et_name}/config">{et_display}{suffix}</a></li>'
            section["items"].append(item)
            if has_presets:
                preset_label = "Presets" if not et_display else f"{et_display} Presets"
                section["items"].append(f'<li><a href="/webui/engine/{et_name}/presets">{preset_label}</a></li>')
# Models moved to top-level nav (no longer in a section)
            if et_name == QR_ENGINE_LLAMA_SERVER_NAME:
                  misc_section["items"].append('<li><a href="/webui/benchmarks">Bench</a></li>')
                  llama_section["items"].append('<li><a href="/webui/rpccluster">Herd</a></li>')

    # Static misc nav items
    if "iperf3" in [e.get("name") for e in engine_types or []]:
        misc_section["items"].append('<li><a href="/webui/iperf3">iperf;3</a></li>')

   # Static LLAMA.cpp section items (merged pages)
    if "llama_rpc" in [e.get("name") for e in engine_types or []]:
        llama_section["items"].append('<li><a href="/webui/rpc">RPC</a></li>')

    # Static system nav items (Tasks before Playbooks)
    _tasks_qs = request.query_string.decode() if request.query_string else ""
    _tasks_href = "/webui/qr-tasks" + ("?" + _tasks_qs if _tasks_qs else "")
    system_section["items"].append(f'<li><a href="{_tasks_href}">Tasks</a></li>')
    if "quickrobot-api" in [e.get("name") for e in engine_types or []] or \
       "quickrobot-webui" in [e.get("name") for e in engine_types or []] or \
       "quickrobot-mcp" in [e.get("name") for e in engine_types or []]:
        system_section["items"].append('<li><a href="/webui/playbooks">Playbooks</a></li>')

    engines_nav_data = []
    for s in [llama_section, misc_section, system_section]:
        if s["items"]:
            engines_nav_data.append(s)

    return nav, engines_nav_data


def get_engine_types():
    """Fetch engine types from the API for navigation display.

    Returns:
        list of engine type dicts, or empty list on error.
    """
    data = api_get("engines")
    if "error" in data:
        return []
    return data.get("items", [])


def status_badge(state):
    """Render a CSS badge for instance/node state.

    Returns Markup-wrapped HTML so Jinja2 does not auto-escape it.
    """
    # SSOT state -> (css_class, display_label) mapping
    # All states covered here must also have a --badge-* CSS variable in base.html :root
    _STATE_MAP = {
        "running":      ("badge-running",   "running"),
        "stopped":      ("badge-stopped",   "stopped"),
        "error":        ("badge-error",     "error"),
        # Transition states — all map to loading (blue) for visual consistency
        "deploying":    ("badge-loading",   "deploying"),
        "configuring":  ("badge-loading",   "configuring"),
        "loading":      ("badge-loading",   "loading"),
        "updating":     ("badge-loading",   "updating"),
        "compiling":    ("badge-loading",   "compiling"),
        "starting":     ("badge-loading",   "starting"),
        "stopping":     ("badge-loading",   "stopping"),
        # Terminal/specific states
        "deployed":     ("badge-success",   "deployed"),
        "build_error":  ("badge-error",     "build_error"),
        "timeout":      ("badge-other",     "timeout"),
    }
    entry = _STATE_MAP.get(state)
    if entry is not None:
        cls, label = entry
        return Markup(f'<span class="badge {cls}">{label}</span>')
    # Unknown state — fall back to generic badge
    return Markup(f'<span class="badge badge-other">{state}</span>')


def gpu_device_badge(device):
    """Render a color-coded badge for GPU device value."""
    if not device or device == "none":
        return Markup('<span style="color:#888;font-size:0.85em;">&#9472;</span>')
    elif "Vulkan" in str(device):
        return Markup(f'<span class="badge badge-loading" style="font-size:0.8em;">{device}</span>')
    elif "CUDA" in str(device):
        return Markup(f'<span class="badge badge-running" style="font-size:0.8em;">{device}</span>')
    else:
        return Markup(f'<span style="color:#888;font-size:0.85em;">{device}</span>')


def node_status_badge(status):
    """Render a CSS badge for node status.

    Returns Markup-wrapped HTML so Jinja2 does not auto-escape it.
    """
    if status == "active":
        return Markup('<span class="badge badge-active">active</span>')
    elif status == "unknown":
        return Markup('<span class="badge badge-unknown">unknown</span>')
    elif status == "failed":
        return Markup('<span class="badge badge-failed">failed</span>')
    else:
        return Markup(f'<span class="badge badge-other">{status or "unknown"}</span>')


def system_badge(is_system):
    """Render a system-managed indicator.

    Returns Markup-wrapped HTML so Jinja2 does not auto-escape it.
    """
    if is_system:
        return Markup(' <span class="badge badge-system">system</span>')
    return ""


def make_html(title, nav_state, content, engine_types=None):
    """Wrap content in the base layout using Jinja2 template.

    Args:
        title: Page title string.
        nav_state: Navigation state identifier (dashboard/hosts/instances/logs).
        content: HTML content string (will be marked safe for render_template).
        engine_types: Optional list of engine type dicts for nav display.

    Returns:
        Complete HTML page string.
    """
    nav, engines_nav_data = render_nav(nav_state, engine_types)
    return render_template('base.html',
        title=title,
        dashboard=nav["dashboard"],
        hosts=nav["hosts"],
        instances=nav["instances"],
        logs=nav.get("logs", ""),
        tasks=nav.get("tasks", ""),
        engines_nav=engines_nav_data,
        content=Markup(content),
    )


# ---------------------------------------------------------------------------
# Cache control — prevent stale HTML on page requests
# ---------------------------------------------------------------------------

@app.after_request
def _set_page_headers(response):
    """Add no-cache headers to HTML page responses (not API/static)."""
    if response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ---------------------------------------------------------------------------
# Static files (JS, CSS)
# ---------------------------------------------------------------------------

@app.route("/_static/<path:filename>")
def webui_static(filename):
    """Serve static files from the webui/ directory."""
    return send_from_directory(_project_root + "/webui", filename)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def webui_root():
    """Root redirect to dashboard for health checks and direct visits."""
    return redirect("/webui/", code=302)


@app.route("/webui/")
def webui_dashboard():
    """Main dashboard showing overview stats."""
    data = api_get("")
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        eng_types = get_engine_types()
        return make_html("Dashboard", "dashboard", content, engine_types=eng_types)
    d = data.get("data", {})
    content = render_template('dashboard.html',
        total_nodes=d.get("total_nodes", 0),
        active_nodes=d.get("active_nodes", 0),
        total_instances=d.get("total_instances", 0),
        running_instances=d.get("running_instances", 0),
        engine_types_count=d.get("engine_types_count", 0),
    )
    eng_types = get_engine_types()
    return make_html("Dashboard", "dashboard", content, engine_types=eng_types)


@app.route("/webui/hosts")
def webui_hosts():
    """List all managed nodes/hosts with client-side column sorting and actions."""
    # Check URL param first, then cookie (set by JS on filter toggle), default to false
    include_inactive = request.args.get("include_inactive", "false").lower() == "true"
    if not include_inactive:
        cookie_val = request.cookies.get("qr_hosts_show_inactive", "")
        include_inactive = cookie_val.lower() == "true"
    data = api_get("nodes?include_inactive=" + str(include_inactive).lower())
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        return make_html("Hosts", "hosts", content, engine_types=get_engine_types())

    nodes = data.get("items", [])

    # Count instances per node from all instances
    inst_data = api_get("instances")
    inst_counts = {}
    compiling_instances = {}  # node_id -> list of compiling instance names
    if "error" not in inst_data:
        for inst in inst_data.get("items", []):
            nid = inst.get("node_id")
            if nid:
                inst_counts[nid] = inst_counts.get(nid, 0) + 1
                # Track compiling instances per node for Build column
                if inst.get("state") == "compiling" and nid:
                    if nid not in compiling_instances:
                        compiling_instances[nid] = []
                    compiling_instances[nid].append(inst.get("name", str(inst.get("id"))))

    # Build state map from nodes data (node_build_state column)
    build_states = {}
    for n in nodes:
        nid = n.get("id")
        if nid:
            build_states[nid] = {
                "state": n.get("node_build_state", "idle"),
                "compiling": compiling_instances.get(nid, []),
            }

    eng_types = get_engine_types()
    nav, engines_nav = render_nav("hosts", eng_types)
    content = render_template('hosts.html', nodes=nodes, instance_counts=inst_counts, build_states=build_states)
    return render_template('base.html', title="Hosts", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/nodes/new", methods=["GET", "POST"])
def webui_nodes_new():
    """Add a new node page with form and validation."""
    if request.method == "POST":
        body, is_err = require_json() if False else (request.form, False)
        name = body.get("name", "").strip()
        hostname = body.get("hostname", "").strip()
        ansible_user = body.get("ansible_user", "").strip() or DEFAULT_ANSIBLE_USER
        ansible_key_path = body.get("ansible_key_path", "").strip() or ""
        ssh_port = int(body.get("ssh_port", 22))

        if not name or not hostname:
            content = '<div style="color:#f44336;padding:12px;">Name and hostname are required.</div>'
            return make_html("Add Node", "hosts", content, engine_types=get_engine_types())

        # Step 1: Create node
        create_data = {"name": name, "hostname": hostname, "ssh_port": ssh_port}
        if ansible_user:
            create_data["ansible_user"] = ansible_user
        if ansible_key_path:
            create_data["ansible_key_path"] = ansible_key_path

        result = api_post("nodes", create_data)
        if "error" in result:
            content = f"""
<div style="color:#f44336;padding:12px;margin-bottom:12px;">Failed to create node: {result["error"]}</div>
<a href="/webui/nodes/new" style="color:#007bff;">Retry</a>"""
            return make_html("Add Node", "hosts", content, engine_types=get_engine_types())

        node_id = result.get("data", {}).get("id") or result.get("data", {}).get("node_id")
        if not node_id:
            content = f'<div style="color:#f44336;">Node created but ID unknown. <a href="/webui/hosts">Go to hosts</a></div>'
            return make_html("Add Node", "hosts", content, engine_types=get_engine_types())

        # Check for stale qr files on remote node
        stale = result.get("data", {}).get("stale_files", {})
        stale_warning = ""
        if stale and stale.get("has_stale"):
            total = stale.get("total", 0)
            svc_list = "<br>".join(f"  - {f}" for f in stale.get("service_files", []))
            env_list = "<br>".join(f"  - {f}" for f in stale.get("env_files", []))
            stale_warning = f"""<div style="color:#856404;padding:12px;background:#fff3cd;border-radius:4px;margin-bottom:12px;">
<strong>⚠ {total} pre-existing qr file(s) found on this node:</strong><br>{svc_list}{env_list}
</div>"""

        # Step 2: Run discovery (validation)
        val_result = api_post(f"nodes/{node_id}/discover")
        if "error" in val_result:
            # Discover endpoint error — delete the node (not added)
            api_delete(f"nodes/{node_id}")
            content = f"""
<h1>Add Node</h1>
<p>Failed to reach <strong>{name}</strong> at {hostname}:</p>
<div style="color:#f44336;padding:12px;background:#fff3cd;border-radius:4px;margin-bottom:12px;">{val_result["error"]}</div>
<a href="/webui/nodes/new" style="color:#007bff;">Try again</a>"""
            return make_html("Add Node", "hosts", content, engine_types=get_engine_types())

        # Step 3: Check if discovery succeeded (active = reachable)
        node_status = val_result.get("data", {}).get("status", "") or \
                      (api_get(f"nodes/{node_id}/status").get("data", {}).get("node_status", "") if isinstance(api_get(f"nodes/{node_id}/status"), dict) else "")
        if node_status == "active":
            return make_html("Add Node", "hosts",
                f'{stale_warning}<p style="color:#28a745;padding:12px;">Node <strong>{name}</strong> added and validated successfully! <a href="/webui/hosts">Go to hosts</a></p>',
                engine_types=get_engine_types())

        # Discovery didn't succeed — delete the node (it's not reachable)
        api_delete(f"nodes/{node_id}")
        status_detail = val_result.get("data", {}).get("result", {}).get("plays", [{}])[0].get("play", {}).get("name", "") if isinstance(val_result.get("data", {}), dict) else ""
        content = f"""
<h1>Add Node</h1>
<p><strong>{name}</strong> at {hostname} is not reachable.</p>
<div style="color:#f44336;padding:12px;background:#fff3cd;border-radius:4px;margin-bottom:12px;">
Cannot connect via SSH — the host was not added.<br>
Check that the hostname resolves and SSH (port {{ ssh_port }}) is accessible.
</div>
<a href="/webui/nodes/new" style="color:#007bff;">Try again with different hostname</a>"""
        return make_html("Add Node", "hosts", content, engine_types=get_engine_types())

    # GET: render form from template
    nav, engines_nav = render_nav("hosts", get_engine_types())
    return render_template('base.html', title="Add Node", engines_nav=engines_nav, **nav, content=Markup(render_template('nodes_new.html')))


@app.route("/webui/engines")
def webui_engines():
    """List all engine types."""
    data = api_get("engines")
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        return make_html("Engines", "engines", content, engine_types=get_engine_types())

    engines = data.get("items", [])
    nav, engines_nav = render_nav("engines", get_engine_types())
    content = render_template('engines.html', engines=engines)
    return render_template('base.html', title="Engines", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/iperf3")
def webui_iperf3():
    """Merged iperf3 page: engine config + presets list on one page."""
    data = api_get("engines")
    eng_types = get_engine_types() if "error" not in data else []
    nav, engines_nav = render_nav("instances", eng_types)
    content = render_template('iperf3.html', **nav)
    return render_template('base.html', title="Iperf3", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/rpc")
def webui_rpc():
    """Merged RPC page: engine config + presets list on one page."""
    data = api_get("engines")
    eng_types = get_engine_types() if "error" not in data else []
    nav, engines_nav = render_nav("instances", eng_types)
    content = render_template('rpc.html', **nav)
    return render_template('base.html', title="RPC Server", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/models")
def webui_models():
    """Unified models page — single hub for all engine models."""
    from flask import request as _request
    params = {}
    q = _request.args.get("q", "").strip()
    if q:
        params["q"] = q
    engine = _request.args.get("engine", "").strip()
    if engine:
        params["engine"] = engine
    data = api_get("models", params if params else None)
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        nav, engines_nav = render_nav("engines", get_engine_types())
        return render_template('base.html', title="Models",
                               engines_nav=engines_nav, **nav, content=Markup(content))

    models = data.get("items", [])
    # Pre-format size strings for template
    for m in models:
        size = m.get("size_bytes", 0)
        if size and isinstance(size, (int, float)):
            if size >= 1024**3:
                m["size_str"] = f"{size / (1024**3):.1f} GB"
            elif size >= 1024**2:
                m["size_str"] = f"{size / (1024**2):.1f} MB"
            else:
                m["size_str"] = f"{size} B"
        else:
            m["size_str"] = "N/A"

    nodes_data = api_get("nodes")
    nodes = nodes_data.get("items", []) if "error" not in nodes_data else []

    nav, engines_nav = render_nav("engines", get_engine_types())
    content = render_template('models.html', models=models, nodes=nodes)
    return render_template('base.html', title="Models", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/models/<int:model_id>/edit")
def webui_model_edit(model_id):
    """Edit a model entry (global, not per-engine)."""
    data = api_get(f"models/{model_id}")
    if "error" in data or not data.get("data"):
        content = f'<p style="color:#f44336;">Model {model_id} not found</p>'
        nav, engines_nav = render_nav("engines", get_engine_types())
        return render_template('base.html', title=f"Edit Model -- {model_id}",
                               engines_nav=engines_nav, **nav, content=Markup(content))

    m = data["data"]
    # Collect existing categories for datalist suggestions
    all_models = api_get("models").get("items", [])
    cats_set = set()
    for other_m in all_models:
        c = other_m.get("category")
        if c:
            cats_set.add(str(c))
    categories = sorted(cats_set)
    nav, engines_nav = render_nav("engines", get_engine_types())
    content = render_template('models_edit.html', model=m, model_id=model_id, categories=categories)
    return render_template('base.html', title=f"Edit Model -- {m.get('name', model_id)}",
                           engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/models/create", methods=["GET"])
def webui_model_create():
    """Create a new global model entry."""
    nav, engines_nav = render_nav("engines", get_engine_types())
    # Collect existing categories for datalist suggestions
    all_models = api_get("models").get("items", [])
    cats_set = set()
    for m in all_models:
        c = m.get("category")
        if c:
            cats_set.add(str(c))
    categories = sorted(cats_set)
    content = render_template('models_new.html', categories=categories)
    return render_template('base.html', title="Create Model",
                           engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/instances")
def webui_instances():
    """List all instances with filter controls."""
    filter_host = request.args.get("host") or request.args.get("filter_host") or ""
    # Determine include_inactive based on host filter selection
    if filter_host == "active_only":
        include_inactive = False
    elif filter_host == "all" or not filter_host:
        include_inactive = True
    else:
        # Specific host selected — show all instances for that host
        include_inactive = True

    data = api_get("instances?include_inactive=" + str(include_inactive).lower())
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        return make_html("Instances", "instances", content, engine_types=get_engine_types())

    all_instances = data.get("items", [])
    filter_engine = request.args.get("engine") or request.args.get("filter_engine") or ""
    filter_state = request.args.get("state") or request.args.get("filter_state") or ""

    # Build filter options from all data
    all_engines = sorted(set(
        inst.get("engine_display_name") or inst.get("engine_type_name", "unknown")
        for inst in all_instances
    ))
    all_states = sorted(set(inst.get("state", "unknown") for inst in all_instances))

    # Build host filter options with instance counts
    host_counts = {}
    for inst in all_instances:
        hn = inst.get("node_hostname") or inst.get("node_name") or "-"
        if not hn:
            hn = "-"
        host_counts[hn] = host_counts.get(hn, 0) + 1
    all_hosts = sorted(set(host_counts.keys()))

    # Apply filters — handle host/inactive combined logic
    # filter_host options: "active_only" = show only active hosts, "show_all" = include inactive hosts, other values = specific host
    if filter_host in ("active_only", "show_all", "all"):
        # Show all instances but rely on API include_inactive param to control inactive host display
        pass  # let API handle the filtering via include_inactive
    else:
        # Filter by specific host — also ensure we respect inactive setting
        if filter_host and filter_host != "":
            all_instances = [inst for inst in all_instances if inst.get("node_hostname") == filter_host or inst.get("node_name") == filter_host]

    # Apply remaining filters
    filtered = [
        inst for inst in all_instances
        if (not filter_engine or
            (filter_engine == 'system_only' and inst.get("system_managed")) or
            (filter_engine == 'exclude_system' and not inst.get("system_managed"))
            or (inst.get("engine_display_name") or inst.get("engine_type_name", "")) == filter_engine)
        and (not filter_state or inst.get("state", "unknown") == filter_state)
   ]

    # Enrich instances with node ping_state and is_active for UI indicators
    # Build node lookup map by hostname (more reliable than node_id since some nodes may be deleted)
    all_nodes_data = api_get("nodes")
    node_map = {}
    if "error" not in all_nodes_data:
        for n in all_nodes_data.get("items", []):
            hn = n.get("hostname", "") or n.get("name", "")
            if hn:
                node_map[hn] = {"ping_state": n.get("ping_state", "unknown"), "is_active": n.get("is_active", 1)}

    for inst in filtered:
        hn = inst.get("node_hostname") or inst.get("node_name") or ""
        nm = node_map.get(hn, {})
        inst["_node_ping_state"] = nm.get("ping_state", "unknown") if hn else None
        inst["_node_is_active"] = nm.get("is_active", 1) if hn else 1

    # Pre-format RSS strings for template
    for inst in filtered:
        rss = inst.get("rss_bytes", 0)
        if rss and isinstance(rss, (int, float)) and rss > 0:
            inst["_rss_str"] = f"{rss / (1024*1024):.1f} MB"
        else:
            inst["_rss_str"] = ""

    # Fetch llama_server instances for RPC cluster binding dropdown + reverse map
    ls_data = api_get(f"instances?engine_type_id={QR_ENGINE_LLAMA_SERVER}")
    llama_servers = []
    rpc_to_llama = {}  # rpc_id -> {id, name, hostname}
    if "error" not in ls_data:
        for ls in ls_data.get("items", []):
            if ls.get("state") in ("running", "deployed"):
                llama_servers.append({
                    "id": ls["id"],
                    "name": ls["name"],
                    "node_hostname": ls.get("node_hostname", "?"),
                })
                # Parse rpc_bind_ids — list_instances returns raw JSON string
                raw_rbi = ls.get("rpc_bind_ids")
                if isinstance(raw_rbi, str):
                    try:
                        rbi = json.loads(raw_rbi) if raw_rbi.strip() else []
                    except (json.JSONDecodeError, TypeError):
                        rbi = []
                elif isinstance(raw_rbi, list):
                    rbi = raw_rbi
                else:
                    rbi = []
                # Build reverse map: rpc_id -> llama_server
                for rid in rbi:
                    if isinstance(rid, int):
                        rpc_to_llama[rid] = {
                            "id": ls["id"],
                            "name": ls["name"],
                            "node_hostname": ls.get("node_hostname", "?"),
                        }

   # RPC binding state warnings for llama_server instances
    # WebUI runs as subprocess.Popen (separate process) — _CONFIG not available
    _db_path = os.path.join(os.getcwd(), "data", "quickrobot.db")
    for inst in filtered:
        if inst.get("engine_type_name") == QR_ENGINE_LLAMA_SERVER_NAME and inst.get("rpc_bind_ids"):
            try:
                from lib.lib_cluster_env_builder import rpc_binding_warnings as _rbw
                inst["_rpc_warnings"] = _rbw(_db_path, inst["id"])
            except Exception:
                inst["_rpc_warnings"] = []
        else:
            inst["_rpc_warnings"] = []

    nav, engines_nav = render_nav("instances", get_engine_types())
    content = render_template('instances.html',
        instances=filtered,
        all_engines=all_engines,
        all_states=all_states,
        filter_engine=filter_engine,
        filter_state=filter_state,
        filter_host=filter_host,
        all_hosts=all_hosts,
        host_counts=host_counts,
        filter_limit=100,
        llama_servers=llama_servers,
        rpc_to_llama=rpc_to_llama,
    )
    return render_template('base.html', title="Instances", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/instances/new", methods=["GET"])
def webui_instances_new():
    """Create new instance page with engine-specific configuration.

    Step 1: Select engine type, instance name, and target node.
    Step 2: Engine-specific fields (presets for llama_server, simple form for rpc).
    Config preview updates live when preset is selected.
    """
    engines_data = api_get("engines")
    nodes_data = api_get("nodes")
    if "error" in engines_data or "error" in nodes_data:
        content = '<p style="color:#f44336;">API unavailable</p>'
        return make_html("Create Instance", "instances", content,
                          engine_types=get_engine_types())

    engines = [e for e in engines_data.get("items", [])
               if not is_system_engine(e.get("name", ""))]
    # Active nodes — all engines see these as baseline
    active_nodes = [n for n in nodes_data.get("items", [])
                    if n.get("status") == "active" and n.get("is_active", 1)]
    # User-instance nodes (excludes node 1 which hosts system-managed instances)
    user_nodes = [n for n in active_nodes
                  if n.get("available_for_instances", True)]
    # Subprocess node — only localhost (id=1), must be is_active
    subprocess_nodes = [n for n in active_nodes if n.get("id") == 1]

    # Engine type options — use engine_type name as value for JS comparison, ID in data attr for submit
    engine_options = ""
    for e in engines:
        eid = e.get("id", "?")
        ename = e.get("name", "")
        edisplay = e.get("display_name", e.get("name", "?"))
        engine_options += f'<option value="{ename}" data-id="{eid}">{edisplay}</option>\n'

    # Node options — default for all engines (user nodes, excludes node 1)
    node_options = ""
    for n in user_nodes:
        nid = n.get("id", "?")
        nname = n.get("name", "?")
        nhost = n.get("hostname", "")
        node_options += f'<option value="{nid}">{nname} ({nhost})</option>\n'

    if not user_nodes:
        node_options = '<option value="">No active nodes available</option>'

    # Subprocess-only node options (node 1 only) — single option, no trailing newline
    subprocess_node_options = ""
    for n in subprocess_nodes:
        nid = n.get("id", "?")
        nname = n.get("name", "?")
        nhost = n.get("hostname", "")
        subprocess_node_options += f'<option value="{nid}" selected>{nname} ({nhost})</option>'

    if not subprocess_nodes:
        subprocess_node_options = '<option value="1" selected>localhost (127.0.0.1)</option>'

    # Engine capabilities map (for JS) — use proper JSON to avoid trailing comma bug
    engine_caps = json.dumps({e.get("name", ""): e.get("capabilities", {}) for e in engines})

    nav, engines_nav = render_nav("instances", get_engine_types())
    content = render_template('instances_new.html',
                              engine_options=engine_options,
                              node_options=node_options,
                              subprocess_node_options=subprocess_node_options,
                              engine_caps=engine_caps)
    return render_template('base.html', title="Create Instance",
                          engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/nodes/<int:node_id>")
def webui_node_detail(node_id):
    """Show detail page for a single node."""
    data = api_get(f"nodes/{node_id}")
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        return make_html(f"Node {node_id}", "hosts", content, engine_types=get_engine_types())

    node = data.get("data", {})
    # Build simple key-value pairs (skip nested dicts/lists)
    node_simple = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}

    inst_data = api_get("instances", {"node_id": node_id})
    node_instances = inst_data.get("items", []) if "error" not in inst_data else []

    # Fetch recent ansible_actions for this node
    actions_data = api_get("ansible_actions", {"node_id": node_id, "limit": 20})
    node_actions = actions_data.get("items", []) if "error" not in actions_data else []

    nav, engines_nav = render_nav("hosts", get_engine_types())
    content = render_template('node_detail.html',
        node=node, node_simple=node_simple, instances=node_instances,
        actions=node_actions)
    return render_template('base.html', title=f"Node -- {node.get('name', str(node_id))}", engines_nav=engines_nav, **nav, content=Markup(content))



# ---------------------------------------------------------------------------
# Live log polling endpoint (serves log JSON for the JS client)
# ---------------------------------------------------------------------------

@app.route("/webui/instances/<int:inst_id>/logs")
def webui_instance_logs(inst_id):
    """Proxy endpoint: fetch instance logs from the API and serve as HTML."""
    data = api_get(f"instances/{inst_id}/logs")
    if "error" in data:
        return make_html("Logs", "instances", f'<p style="color:#f44336;">{data["error"]}</p>', engine_types=get_engine_types())

    items = data.get("items", [])
    # Format detail for template (convert dict to string)
    log_entries = []
    for entry in items:
        d = dict(entry)
        if isinstance(d.get("detail"), dict):
            d["detail"] = str(d["detail"])
        log_entries.append(d)

    nav, engines_nav = render_nav("instances", get_engine_types())
    content = render_template('instance_logs.html', inst_id=inst_id, log_entries=log_entries, log_data=data)
    return render_template('base.html', title="Logs", engines_nav=engines_nav, **nav, content=Markup(content))


# ---------------------------------------------------------------------------
# Phase 3: Engine config, presets, models pages
# ---------------------------------------------------------------------------

@app.route("/webui/engine/<engine_type>/config")
def webui_engine_config(engine_type):
    """Show engine config page with editable key-value table.

    For system engines (quickrobot-api, quickrobot-webui), uses dedicated
    settings/metrics endpoints. For regular engines, uses generic config API.

    Args:
        engine_type: Engine type name string (e.g., 'rpc', 'llama_server').
    """
    # Normalize short names to canonical long names for system engines
    _NAME_ALIAS = {"qr_api": "quickrobot-api", "qr_webui": "quickrobot-webui",
                   "qr_mcp": "quickrobot-mcp"}
    if engine_type in _NAME_ALIAS:
        engine_type = _NAME_ALIAS[engine_type]

    # Determine if this is a system-managed engine
    is_system = engine_type in ("quickrobot-api", "quickrobot-webui", "quickrobot-mcp")

    # Determine page title — standard format: "Config -- {display_name}"
    display_names = {
        "quickrobot-webui": "Dry Nose Ape Control Interface",
        "quickrobot-api": "API Service Settings",
        "quickrobot-scheduler": "Scheduler",
        "llama_server": "LLAMA.cpp Server",
        "llama_rpc": "LLAMA.RPC Server",
        "quickrobot-mcp": "MCP Service Settings",
    }
    display_name = display_names.get(engine_type, get_display_name(engine_type))
    page_title = f"Config -- {display_name}"

    if is_system:
        # Use system-engine specific endpoints
        config_html, save_js = _render_system_engine_config(engine_type)
    else:
        # Use generic engine config API
        data = api_get(f"engine/{engine_type}/config")
        if "error" in data:
            content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
            nav, engines_nav = render_nav("engines", get_engine_types())
            title = f"Config -- {engine_type}"
            return render_template('base.html', title=title,
                                   engines_nav=engines_nav, **nav, content=Markup(content))

        configs = data.get("data", {})
        if not isinstance(configs, dict):
            configs = {}

        # Get engine metadata (descriptions, dropdown options, etc.)
        meta = ENGINE_CONFIG_META.get(engine_type, {})
        field_meta = meta.get("fields", {})
        dropdown_fields = set(meta.get("dropdowns", []))

        # Systemd restart policy options
        restart_options = '<option value="no">no</option>' \
                          '<option value="always">always</option>' \
                          '<option value="on-success">on-success</option>' \
                          '<option value="on-failure">on-failure</option>' \
                          '<option value="on-abnormal">on-abnormal</option>' \
                          '<option value="on-watchdog">on-watchdog</option>' \
                          '<option value="on-failure:N">on-failure:N (max restarts)</option>'

        rows = ""
        for key, config_entry in configs.items():
            if isinstance(config_entry, dict):
                display_value = config_entry.get("value", "")
                db_desc = config_entry.get("description", "")
            else:
                display_value = config_entry or ""
                db_desc = ""

            # Use metadata description as fallback when DB description is empty
            desc = db_desc if db_desc else field_meta.get(key, {}).get("description", "")

          # Build input element (text or dropdown for restart_policy / start_on_boot / skip_build / booleans)
            if key.startswith("start_on_boot"):
                dv = str(display_value).lower()
                options_html = '<option value="true"' + (' selected' if dv == "true" else '') + '>true</option>' \
                               '<option value="false"' + (' selected' if dv == "false" else '') + '>false</option>'
                input_html = f'<select class="config-value" data-key="{key}" style="width:100%;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">{options_html}</select>'
            elif key == "skip_build":
                dv = str(display_value).lower()
                options_html = '<option value="true"' + (' selected' if dv == "true" else '') + '>true</option>' \
                               '<option value="false"' + (' selected' if dv == "false" else '') + '>false</option>'
                input_html = f'<select class="config-value" data-key="{key}" style="width:100%;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">{options_html}</select>'
            elif key in dropdown_fields and key in ("restart_policy",):
                options_html = ""
                for opt_val in ["no", "always", "on-success", "on-failure", "on-abnormal", "on-watchdog", "on-failure:N"]:
                    sel = "selected" if str(display_value) == opt_val else ""
                    options_html += f'<option value="{opt_val}" {sel}>{opt_val}</option>'
                input_html = f'<select class="config-value" data-key="{key}" style="width:100%;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">{options_html}</select>'
            elif key in dropdown_fields and str(display_value).lower() in ("true", "false"):
                # Boolean dropdown fields (allow_reads, allow_writes, allow_proxy, mcp_detach, etc.)
                dv = str(display_value).lower()
                options_html = '<option value="true"' + (' selected' if dv == "true" else '') + '>true</option>' \
                               '<option value="false"' + (' selected' if dv == "false" else '') + '>false</option>'
                input_html = f'<select class="config-value" data-key="{key}" style="width:100%;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">{options_html}</select>'
            elif key in dropdown_fields:
                options_html = ""
                for opt_val in ["no", "always", "on-success", "on-failure", "on-abnormal", "on-watchdog", "on-failure:N"]:
                    sel = "selected" if str(display_value) == opt_val else ""
                    options_html += f'<option value="{opt_val}" {sel}>{opt_val}</option>'
                input_html = f'<select class="config-value" data-key="{key}" style="width:100%;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">{options_html}</select>'
            else:
                input_html = f'<input type="text" class="config-value" data-key="{key}" value="{display_value}" style="width:100%;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">'

            rows += f"""<tr>
   <td style="white-space:nowrap;min-width:200px;">{key}</td>
   <td style="color:#888;font-size:0.85em;min-width:250px;">{desc}</td>
   <td>{input_html}</td>
 </tr>\n"""

        # Build original-values map for delta tracking
        orig_map = "{"
        first = True
        for key, config_entry in configs.items():
            if not first:
                orig_map += ", "
            first = False
            display_val = config_entry.get("value", "") if isinstance(config_entry, dict) else (config_entry or "")
            orig_map += f'"{key}": {json.dumps(str(display_val))}'
        orig_map += "}"

        if not configs:
            rows = "<tr><td colspan='3' style='text-align:center;color:#888;'>No config keys set</td></tr>"

        table = TABLE_HEADER.format(
            headers="<th>Key</th><th>Description</th><th>Value</th>", rows=rows)

        config_html = f"<form id='config-form'>{table}</form>"
        save_js = f"""
<script>
(function() {{
  // Store original values for delta tracking
  var originals = {orig_map};
  // Known field types for validation
  var numeric_fields = {{}};

  document.querySelector('#config-form')
    .insertAdjacentHTML('beforeend',
      '<div class="actions"><button class="btn btn-success" id="save-all-btn">Save All Changes</button></div>');

  document.getElementById('save-all-btn').addEventListener('click', function(e) {{
    var inputs = document.querySelectorAll('.config-value');
    var changes = {{}};
    var has_error = false;
    inputs.forEach(function(input) {{
      var key = input.getAttribute('data-key');
      var val = input.value;
      // Only send field if it actually changed from original
      var orig = originals[key];
      if (val !== orig) changes[key] = val;
      // Validate numeric fields
      if (key === "base_port") {{
        var n = parseInt(val, 10);
        if (isNaN(n) || n < 1024 || n > 65535) {{
          alert('Port must be between 1024 and 65535');
          has_error = true;
        }}
      }} else if (key === "default_timeout") {{
        var n = parseInt(val, 10);
        if (isNaN(n) || n < 1) {{
          alert('Timeout must be a positive integer');
          has_error = true;
        }}
      }} else if (key === "binary_path") {{
        if (!val.trim()) {{
          alert('Binary path cannot be empty');
          has_error = true;
        }}
      }}
    }});
    if (has_error) return;
    if (Object.keys(changes).length === 0) {{ return; }}

    // Show status message inline
    var saveBtn = document.getElementById('save-all-btn');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';

    fetch('/api/v1/engine/{engine_type}/config/batch?_cb=' + Date.now(), {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{configs: changes}})
    }}).then(function(r) {{ return r.json(); }})
      .then(function(data) {{
         if (data.status === 'ok') {{
           // Update changed values in DOM (no reload needed)
           var keysSaved = data.data && data.data.saved_keys ? data.data.saved_keys : 0;
           for (var k in changes) {{
             var el = document.querySelector('.config-value[data-key="' + k + '"]');
             if (el) {{ el.value = changes[k]; }}
           }}
           saveBtn.textContent = 'Saved ' + keysSaved + ' keys';
           setTimeout(function() {{ saveBtn.textContent = 'Save All Changes'; saveBtn.disabled = false; }}, 2000);
         }} else {{
          saveBtn.textContent = 'Error';
          setTimeout(function() {{ saveBtn.textContent = 'Save All Changes'; saveBtn.disabled = false; }}, 3000);
        }}
      }}).catch(function(e) {{
        saveBtn.textContent = 'Error';
        setTimeout(function() {{ saveBtn.textContent = 'Save All Changes'; saveBtn.disabled = false; }}, 3000);
      }});
   }});
}})();
</script>"""

    content = f"<h1 style='margin:0;font-size:1.3em;'>{page_title}</h1>\n"
    content += config_html + save_js

    # Add instance detail button for scheduler
    if engine_type == "quickrobot-scheduler":
        _sched_inst = _find_sys_inst(os.path.join(os.getcwd(), "data", "quickrobot.db"), QR_ENGINE_SCHEDULER_NAME)
        sched_id = _sched_inst.get("id") if _sched_inst else None
        if sched_id:
            content += f'<div class="actions" style="margin-top:12px;"><a href="/webui/instances/{sched_id}" class="btn btn-sm btn-success">Open Instance Detail</a></div>'

    nav, engines_nav = render_nav("engines", get_engine_types())
    return render_template('base.html', title=page_title,
                           engines_nav=engines_nav, **nav, content=Markup(content))


def _render_system_engine_config(engine_type):
    """Render config page for system-managed engines with proper endpoints.

    For quickrobot-webui: editable settings (timezone, autostart, detach, polling) — host/port migrated to .quickrobot.env.
    For quickrobot-api: editable settings (db_path, polling, refresh) — ansible/ping/playbook/max_backups fields migrated to .quickrobot.env.

    Args:
        engine_type: System engine name (quickrobot-api or quickrobot-webui).

    Returns:
        tuple of (config_html, save_js) strings.
    """
    if engine_type == QR_ENGINE_WEBUI_NAME:
        # Fetch actual settings from dedicated endpoint
        data = api_get(f"engines/{engine_type}/settings")
        if "error" in data or not data.get("data"):
            rows = "<tr><td colspan='2' style='text-align:center;color:#888;'>Settings unavailable</td></tr>"
            js = ""
        else:
            settings = data["data"]
            # Find the quickrobot-webui instance ID for link to detail page
            _web_inst = _find_sys_inst(os.path.join(os.getcwd(), "data", "quickrobot.db"), QR_ENGINE_WEBUI_NAME)
            web_id = _web_inst.get("id") if _web_inst else None

            # Fields from .quickrobot.env — show as informational only (read-only)
            webui_env_migrated = ("web_ui_host", "web_ui_port", "webui_autostart", "webui_detach")
            # Build editable config table
            rows = ""
            # Common IANA timezones — ~30 options covering major regions
            TIMEZONES = [
                "UTC", "Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Moscow",
                "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul",
                "Australia/Sydney", "Pacific/Auckland",
                "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
                "America/Vancouver", "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
                "Africa/Cairo", "Africa/Lagos", "Africa/Johannesburg",
            ]
            webui_field_meta = {
                "web_ui_timezone": {"description": "Display timezone (IANA TZ name)", "input_type": "select", "editable": True},
                "webui_autostart": {"description": "Auto-start WebUI on API start (true/false)", "input_type": "dropdown", "editable": True},
                "webui_detach": {"description": "Run WebUI in detached process group (survives API death)", "input_type": "dropdown", "editable": True},
                "polling_interval_local_sec": {"description": "Polling interval for local instances (sec, min 10)", "input_type": "number", "editable": True},
                "polling_interval_remote_sec": {"description": "Polling interval for remote nodes (sec, min 10)", "input_type": "number", "editable": True},
            }
            for key, value in settings.items():
                if key in webui_env_migrated:
                    continue  # Already shown in bottom info table
                meta = webui_field_meta.get(key, {"description": "", "input_type": "text"})
                input_type = meta.get("input_type", "text")
                desc = meta.get("description", "")
                default_val = value if isinstance(value, str) else str(value)
                if key in ("webui_autostart", "webui_detach") and input_type == "dropdown":
                    options = '<option value="true"' + (' selected' if default_val.lower() == "true" else '') + '>true</option>' \
                              '<option value="false"' + (' selected' if default_val.lower() == "false" else '') + '>false</option>'
                    rows += f"""<tr>
  <td>{key}</td>
  <td style="color:#888;font-size:0.85em;">{desc}</td>
  <td>
    <div style="display:flex;gap:4px;align-items:center;">
      <select class="config-value" data-key="{key}" style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">{options}</select>
      <button class="btn-save-line" data-key="{key}" title="Save this setting" style="padding:3px 8px;font-size:0.8em;background:#4caf50;color:#fff;border:none;border-radius:3px;cursor:pointer;">&#10003;</button>
    </div>
  </td>
</tr>\n"""
                elif key == "web_ui_timezone" and input_type == "select":
                    opt_parts = []
                    for tz in TIMEZONES:
                        sel = ' selected' if tz == default_val else ''
                        opt_parts.append(f'<option value="{tz}"{sel}>{tz}</option>')
                    # Add current value if not in standard list
                    if default_val not in TIMEZONES:
                        opt_parts.append(f'<option value="{default_val}" selected>{default_val}</option>')
                    options = "".join(opt_parts)
                    rows += f"""<tr>
  <td>{key}</td>
  <td style="color:#888;font-size:0.85em;">{desc}</td>
  <td>
    <div style="display:flex;gap:4px;align-items:center;">
      <select class="config-value" data-key="{key}" style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">{options}</select>
      <button class="btn-save-line" data-key="{key}" title="Save this setting" style="padding:3px 8px;font-size:0.8em;background:#4caf50;color:#fff;border:none;border-radius:3px;cursor:pointer;">&#10003;</button>
    </div>
  </td>
</tr>\n"""
                else:
                    placeholder = ''
                    if key == "web_ui_timezone":
                        placeholder = ' placeholder="Europe/Berlin"'
                    rows += f"""<tr>
  <td>{key}</td>
  <td style="color:#888;font-size:0.85em;">{desc}</td>
  <td>
    <div style="display:flex;gap:4px;align-items:center;">
      <input type="{input_type}" class="config-value" data-key="{key}"
             value="{default_val}"{placeholder} style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:3px;">
      <button class="btn-save-line" data-key="{key}" title="Save this setting" style="padding:3px 8px;font-size:0.8em;background:#4caf50;color:#fff;border:none;border-radius:3px;cursor:pointer;">&#10003;</button>
    </div>
  </td>
</tr>\n"""

            # Info table for env-migrated fields (web_ui_host, web_ui_port)
            info_rows = ""
            for key, value in sorted(settings.items()):
                if key not in webui_env_migrated:
                    continue
                default_val = str(value)
                desc = webui_field_meta.get(key, {}).get("description", "")
                info_rows += f"""<tr>
  <td>{key}</td>
  <td style="color:#888;font-size:0.85em;">{desc} (from .quickrobot.env)</td>
  <td style="font-family:monospace;font-size:0.9em;color:#666;">{default_val}</td>
</tr>\n"""

            edit_table = TABLE_HEADER.format(
                headers="<th>Setting</th><th>Description</th><th>Value</th>", rows=rows)

            info_table_html = ""
            if info_rows:
                info_table_html = f"<h3 style='margin-top:20px;font-size:1em;color:#666;'>from .quickrobot.env</h3>{TABLE_HEADER.format(headers='<th>Setting</th><th>Description</th><th>Value</th>', rows=info_rows)}"

            rows += "<tr><td colspan='3' style='text-align:center;color:#666;font-size:0.8em;'>Changes require restart to take effect</td></tr>"

            # Build original-values map for delta tracking (editable keys only)
            orig_map = "{"
            first = True
            for key, value in sorted(settings.items()):
                if key in webui_env_migrated:
                    continue
                if not first:
                    orig_map += ", "
                first = False
                escaped_val = json.dumps(str(value))
                orig_map += f'"{key}": {escaped_val}'
            orig_map += "}"

            # Add instance detail link button
            instance_btn = ""
            if web_id:
               instance_btn = f'<a href="/webui/instances/{web_id}" class="btn btn-sm btn-success" style="margin-left:8px;">Open Instance Detail</a>'

            config_html = f"<form id='config-form'>{edit_table}</form>{info_table_html}"
            _save_ep = '/api/v1/engines/' + engine_type + '/settings'
            js = f"""
<script>
(function() {{
  var originals = {orig_map};
  var saveEndpoint = '{_save_ep}';

  // Per-line save handlers — each .btn-save-line button saves just its field
   document.querySelectorAll('.btn-save-line').forEach(function(btn) {{
     btn.addEventListener('click', function() {{
       var key = this.getAttribute('data-key');
       var input = document.querySelector('.config-value[data-key="' + key + '"]');
       if (!input) return;
       var val = input.value;
       var orig = originals[key];
       if (JSON.stringify(val) === JSON.stringify(orig)) return;

       fetch(saveEndpoint + '/' + key, {{
          method: 'PUT',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{value: val}})
        }}).then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            if (data.status === 'ok') {{
              // Visual feedback on the button
              var originalText = this.textContent;
              btn.textContent = '\u2714';
              btn.style.background = '#28a745';
              // Update originals map so future saves compare against new value
              originals[key] = val;
              setTimeout(function() {{
                btn.textContent = originalText;
                btn.style.background = '#4caf50';
              }}, 1500);
            }} else {{
              btn.textContent = '\u2718';
              btn.style.background = '#dc3545';
              setTimeout(function() {{ btn.textContent = '\u2713'; btn.style.background = '#4caf50'; }}, 2000);
            }}
          }}.bind(this)).catch(function(e) {{
            btn.textContent = '\u2718';
            btn.style.background = '#dc3545';
            setTimeout(function() {{ btn.textContent = '\u2713'; btn.style.background = '#4caf50'; }}, 2000);
          }});
      }});
    }});
}})();
</script>"""
            config_html += f'<div class="actions" style="margin-top:12px;">{instance_btn}</div>'
            return config_html, js

    elif engine_type == QR_ENGINE_API_NAME:
        # quickrobot-api: fetch runtime metrics + editable settings
        data = api_get(f"engines/{engine_type}/status")
        metrics = {}
        if "error" not in data and data.get("data"):
            metrics = data["data"]

        # Find the quickrobot-api instance ID for link to detail page
        _api_inst = _find_sys_inst(os.path.join(os.getcwd(), "data", "quickrobot.db"), QR_ENGINE_API_NAME)
        svc_id = _api_inst.get("id") if _api_inst else None

        # Build editable config section (shared settings from engine_configs table)
        config_data = api_get("engine/quickrobot-api/config")
        configs = {}
        if "error" not in config_data:
            raw = config_data.get("data", {})
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        configs[k] = v.get("value", "")
                    else:
                        configs[k] = v or ""

        # Fields migrated to .quickrobot.env — show as informational only
        env_migrated_fields = ("ansible_key_path", "ansible_user", "playbook_root_dir",
                               "ping_command", "ping_interval")
        # Known field metadata for quickrobot-api config
        api_field_meta = {
            "db_path": {"description": "SQLite database file path", "editable": True},
            "api_host": {"description": "API server bind address", "editable": True},
            "api_port": {"description": "API server port", "editable": True},
            "polling_interval_local_sec": {"description": "Action log polling interval for local instances (sec, min 10)", "editable": True},
             "polling_interval_remote_sec": {"description": "Action log polling interval for remote nodes (sec, min 10)", "editable": True},
            "ansible_user": {"description": "SSH user for ansible playbook calls — from .quickrobot.env"},
            "ansible_key_path": {"description": "Path to SSH private key for ansible authentication — from .quickrobot.env"},
            "ping_command": {"description": "Shell command template for host reachability checks — from .quickrobot.env"},
            "ping_interval": {"description": "Ping check interval in seconds — from .quickrobot.env"},
            "playbook_root_dir": {"description": "Base directory containing playbook YAML files — from .quickrobot.env"},
        }

        # Editable config table rows (fields NOT migrated to env file)
        editable_rows = ""
        for key in sorted(configs.keys()):
            if key in env_migrated_fields:
                continue
            val = str(configs[key])
            desc = api_field_meta.get(key, {}).get("description", "")
            input_type = "number" if key == "max_backups" else "text"
            editable_rows += f"""<tr>
  <td>{key}</td>
  <td style="color:#888;font-size:0.85em;">{desc}</td>
  <td><div style="display:flex;gap:4px;align-items:center;"><input type="{input_type}" class="config-value" data-key="{key}" value="{val}" style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:3px;"><button class="btn-save-line" data-key="{key}" title="Save this setting" style="padding:3px 8px;font-size:0.8em;background:#4caf50;color:#fff;border:none;border-radius:3px;cursor:pointer;">&#10003;</button></div></td>
</tr>"""

        # Informational rows for env-migrated fields (read-only)
        info_rows = ""
        for key in sorted(configs.keys()):
            if key not in env_migrated_fields:
                continue
            val = str(configs[key])
            desc = api_field_meta.get(key, {}).get("description", "")
            info_rows += f"""<tr>
  <td>{key}</td>
  <td style="color:#888;font-size:0.85em;">{desc} (from .quickrobot.env)</td>
  <td style="font-family:monospace;font-size:0.9em;color:#666;">{val}</td>
</tr>"""

        editable_table = TABLE_HEADER.format(
            headers="<th>Setting</th><th>Description</th><th>Value</th>",
            rows=editable_rows or "<tr><td colspan='3' style='text-align:center;color:#888;'>No editable settings</td></tr>")

        info_table_html = ""
        if info_rows:
            info_table_html = f"<h3 style='margin-top:20px;font-size:1em;color:#666;'>from .quickrobot.env</h3>{TABLE_HEADER.format(headers='<th>Setting</th><th>Description</th><th>Value</th>', rows=info_rows)}"

        # Runtime metrics table (read-only)
        metric_desc = {
            "flask_version": "Flask framework version",
            "pid": "Process ID of the QR service",
            "python_version": "Python interpreter version",
            "rss_bytes": "Resident set size (actual RAM used by process)",
            "db_size": "SQLite database file size on disk",
            "state": "Current operational state",
            "uptime_seconds": "Time since the service started",
        }
        metrics_rows = ""
        for key, value in sorted(metrics.items()):
            desc = metric_desc.get(key, "")
            display_val = value
            if isinstance(value, (int, float)):
                if key == "rss_bytes":
                    display_val = f"{value / (1024*1024):.1f} MB"
                elif key == "uptime_seconds":
                    hrs = value // 3600
                    mins = (value % 3600) // 60
                    secs = value % 60
                    display_val = f"{hrs}h {mins}m {secs}s"
            elif key == "pid":
                display_val = f"<code>{value}</code>"
            metrics_rows += f"<tr><td>{key}</td><td>{display_val}</td><td style='color:#888;font-size:0.85em;'>{desc}</td></tr>\n"

        metrics_table = TABLE_HEADER.format(
            headers="<th>Metric</th><th>Value</th><th>Description</th>",
            rows=metrics_rows or "<tr><td colspan='3' style='text-align:center;color:#888;'>Metrics unavailable</td></tr>")

        # Build original values map for delta tracking (editable keys only)
        orig_map = "{"
        first = True
        for key in sorted(configs.keys()):
            if key in env_migrated_fields:
                continue
            val = configs[key]
            if not first:
                orig_map += ", "
            first = False
            orig_map += f'"{key}": {json.dumps(str(val))}'
        orig_map += "}"

        instance_btn = ""
        if svc_id:
            instance_btn = f'<a href="/webui/instances/{svc_id}" class="btn btn-sm btn-success" style="margin-left:8px;">Open Instance Detail</a>'

        config_html = (f"<form id='config-form'>{editable_table}</form>"
                       f"{info_table_html}"
                       f"<h3 style='margin-top:20px;font-size:1em;'>Runtime Metrics (read-only)</h3>"
                       f"{metrics_table}")
        _save_ep_api = '/api/v1/engine/quickrobot-api/config'
        js = f"""
<script>
(function() {{
  var originals = {orig_map};
  var saveEndpoint = '{_save_ep_api}';

  // Per-line save handlers — each .btn-save-line button saves just its field
   document.querySelectorAll('.btn-save-line').forEach(function(btn) {{
     btn.addEventListener('click', function() {{
       var key = this.getAttribute('data-key');
       var input = document.querySelector('.config-value[data-key="' + key + '"]');
       if (!input) return;
       var val = input.value;
       var orig = originals[key];
       if (JSON.stringify(val) === JSON.stringify(orig)) return;

       fetch(saveEndpoint + '/' + key, {{
          method: 'PUT',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{value: val}})
        }}).then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            if (data.status === 'ok') {{
              var originalText = this.textContent;
              btn.textContent = '\u2714';
              btn.style.background = '#28a745';
              originals[key] = val;
              setTimeout(function() {{
                btn.textContent = originalText;
                btn.style.background = '#4caf50';
              }}, 1500);
            }} else {{
              btn.textContent = '\u2718';
              btn.style.background = '#dc3545';
              setTimeout(function() {{ btn.textContent = '\u2713'; btn.style.background = '#4caf50'; }}, 2000);
            }}
          }}.bind(this)).catch(function(e) {{
            btn.textContent = '\u2718';
            btn.style.background = '#dc3545';
            setTimeout(function() {{ btn.textContent = '\u2713'; btn.style.background = '#4caf50'; }}, 2000);
          }});
      }});
    }});
 }})();
 </script>"""
        config_html += f"<div class='actions' style='margin-top:16px;'>{instance_btn}"
        f"<span style='margin-left:8px;font-size:0.85em;color:#666;'>Metrics require restart to reflect config changes</span></div>"
        return config_html, js

    elif engine_type == QR_ENGINE_MCP_NAME:
        data = api_get(f"engines/{engine_type}/settings")
        if "error" in data or not data.get("data"):
            rows = "<tr><td colspan='2' style='text-align:center;color:#888;'>Settings unavailable</td></tr>"
            js = ""
        else:
            settings = data["data"]
            # Remove deprecated/hidden fields entirely from display
            for _k in ("mcp_detach", "mcp_autostart"):
                settings.pop(_k, None)
            _mcp_inst = _find_sys_inst(os.path.join(os.getcwd(), "data", "quickrobot.db"), QR_ENGINE_MCP_NAME)
            mcp_id = _mcp_inst.get("id") if _mcp_inst else None

            # Fields migrated to .quickrobot.env - show as informational only
            mcp_env_migrated = ("mcp_host", "mcp_port")
            rows = ""
            api_settings = api_get("engines/quickrobot-api/status").get("data", {})
            api_bind_host = api_settings.get("bind_host") or QR_DEFAULT_LOCALHOST

            mcp_field_meta = {
                "allow_reads": {"description": "Expose read-only tools (list_instances, list_nodes, etc.)", "input_type": "dropdown", "editable": True},
                "allow_writes": {"description": "Expose write tools (create_instance, deploy, start, stop)", "input_type": "dropdown", "editable": True},
                "allow_proxy": {"description": "Expose raw API proxy tool for direct path access", "input_type": "dropdown", "editable": True},
                "mcp_python_interpreter": {"description": "Python interpreter binary for MCP server (empty=auto-detect)", "input_type": "text", "editable": True},
                "mcp_host": {"description": "MCP listen address - configured in .quickrobot.env", "input_type": "text"},
                "mcp_port": {"description": "MCP listen port - configured in .quickrobot.env", "input_type": "text"},
            }

            # Info rows for env-migrated fields
            mcp_info_rows = ""
            for key, value in sorted(settings.items()):
                if key not in mcp_env_migrated:
                    continue
                default_val = str(value)
                desc = mcp_field_meta.get(key, {}).get("description", "")
                suffix = " - from .quickrobot.env" if key in mcp_env_migrated else ""
                mcp_info_rows += f"<tr><td>{key}</td><td style='color:#888;font-size:0.85em;'>{desc}{suffix}</td><td style='font-family:monospace;font-size:0.9em;color:#666;'>{default_val}</td></tr>\n"

           # Editable settings rows
            for key, value in settings.items():
                if key in mcp_env_migrated:
                    continue  # Shown in info table below
                meta = mcp_field_meta.get(key, {"description": "", "input_type": "text"})
                input_type = meta.get("input_type", "text")
                desc = meta.get("description", "")
                default_val = value if isinstance(value, bool) else str(value if value else "")

                if key == "mcp_api_host":
                    rows += f"<tr><td>{key}</td><td style='color:#888;font-size:0.85em;'>{desc} (derived from quickrobot-api config)</td><td><code style='font-family:monospace;font-size:0.85em;'>{api_bind_host}</code></td></tr>\n"
                elif key == "mcp_api_base":
                    rows += f"<tr><td>{key}</td><td style='color:#888;font-size:0.85em;'>{desc} (auto-generated from API bind address)</td><td><code style='font-family:monospace;font-size:0.85em;'>{settings.get('mcp_api_base')}</code></td></tr>\n"
                elif key in ("allow_reads", "allow_writes", "allow_proxy"):
                    sel_true = ' selected' if str(default_val).lower() == "true" else ""
                    sel_false = ' selected' if str(default_val).lower() != "true" else ""
                    options = f'<option value="true"{sel_true}>true</option><option value="false"{sel_false}>false</option>'
                    rows += f"<tr><td>{key}</td><td style='color:#888;font-size:0.85em;'>{desc}</td><td><div style='display:flex;gap:4px;align-items:center;'><select class='config-value' data-key='{key}' style='flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:3px;'>{options}</select><button class='btn-save-line' data-key='{key}' title='Save this setting' style='padding:3px 8px;font-size:0.8em;background:#4caf50;color:#fff;border:none;border-radius:3px;cursor:pointer;'>&#10003;</button></div></td></tr>\n"
                else:
                    rows += f"<tr><td>{key}</td><td style='color:#888;font-size:0.85em;'>{desc}</td><td><div style='display:flex;gap:4px;align-items:center;'><input type='{input_type}' class='config-value' data-key='{key}' value='{default_val}' style='flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:3px;'><button class='btn-save-line' data-key='{key}' title='Save this setting' style='padding:3px 8px;font-size:0.8em;background:#4caf50;color:#fff;border:none;border-radius:3px;cursor:pointer;'>&#10003;</button></div></td></tr>\n"

            # SSE URL display row
            mcp_listen_host = settings.get("mcp_host") or api_bind_host
            mcp_port_val = settings.get("mcp_port") or os.getenv("QUICKROBOT_MCP_PORT", "")
            sse_url = f"http://{mcp_listen_host}:{mcp_port_val}/sse"
            rows += f"<tr><td><strong>SSE URL</strong></td><td style='color:#888;font-size:0.85em;'>MCP SSE endpoint (uses mcp_host + port)</td><td><code style='font-family:monospace;font-size:0.85em;'>{sse_url}</code></td></tr>\n"
            rows += "<tr><td><strong>Interpreter</strong></td><td style='color:#888;font-size:0.85em;' id='mcp-interpreter-desc'>Loading...</td><td><code id='mcp-interpreter-path' style='font-family:monospace;font-size:0.85em;'>—</code></td></tr>\n"
            rows += "<tr><td colspan='3' style='text-align:center;color:#666;font-size:0.8em;'>Flag and port changes take effect on restart</td></tr>"
            edit_table = TABLE_HEADER.format(headers="<th>Setting</th><th>Description</th><th>Value</th>", rows=rows)

            info_table_html = ""
            if mcp_info_rows:
                info_table_html = f"<h3 style='margin-top:20px;font-size:1em;color:#666;'>from .quickrobot.env</h3>{TABLE_HEADER.format(headers='<th>Setting</th><th>Description</th><th>Value</th>', rows=mcp_info_rows)}"

            orig_map = "{"
            first = True
            for key, value in sorted(settings.items()):
                if key in mcp_env_migrated:
                    continue
                if not first:
                    orig_map += ", "
                first = False
                escaped_val = json.dumps(str(value))
                orig_map += f'"{key}": {escaped_val}'
            orig_map += "}"

            instance_btn = ""
            if mcp_id:
                instance_btn = f'<a href="/webui/instances/{mcp_id}" class="btn btn-sm btn-success" style="margin-left:8px;">Open Instance Detail</a>'

            config_html = f"<form id='config-form'>{edit_table}</form>{info_table_html}"
            _save_ep_mcp = '/api/v1/engines/' + engine_type + '/settings'
            js = f"""
<script>
(function() {{
  var originals = {orig_map};
  var saveEndpoint = '{_save_ep_mcp}';

 // Per-line save handlers — each .btn-save-line button saves just its field
   document.querySelectorAll('.btn-save-line').forEach(function(btn) {{
     btn.addEventListener('click', function() {{
       var key = this.getAttribute('data-key');
       var input = document.querySelector('.config-value[data-key="' + key + '"]');
       if (!input) return;
       var val = input.value;
       var orig = originals[key];
       if (JSON.stringify(val) === JSON.stringify(orig)) return;

     fetch(saveEndpoint + '/' + key, {{
          method: 'PUT',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{value: val}})
        }}).then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            if (data.status === 'ok') {{
              var originalText = this.textContent;
              btn.textContent = '\u2714';
              btn.style.background = '#28a745';
              originals[key] = val;
              setTimeout(function() {{
                btn.textContent = originalText;
                btn.style.background = '#4caf50';
              }}, 1500);
            }} else {{
              btn.textContent = '\u2718';
              btn.style.background = '#dc3545';
              setTimeout(function() {{ btn.textContent = '\u2713'; btn.style.background = '#4caf50'; }}, 2000);
            }}
          }}.bind(this)).catch(function(e) {{
            btn.textContent = '\u2718';
            btn.style.background = '#dc3545';
            setTimeout(function() {{ btn.textContent = '\u2713'; btn.style.background = '#4caf50'; }}, 2000);
          }});
      }});
    }});

     // Load MCP interpreter status from engine status endpoint
    fetch('/api/v1/engines/{engine_type}/status')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.status !== 'ok') return;
        var interp = data.data.interpreter_path || '';
        var avail = data.data.mcp_available;
        var descEl = document.getElementById('mcp-interpreter-desc');
        var pathEl = document.getElementById('mcp-interpreter-path');
        if (!descEl || !pathEl) return;
        if (avail) {{
          descEl.innerHTML = avail ? '<span style=\"color:#28a745;\">&#10003; MCP Python package available</span>' : '<span style=\"color:#e67e22;\">&#9888; MCP package not installed</span>';
          pathEl.textContent = interp || '(auto-detecting...)';
        }} else {{
          descEl.innerHTML = '<span style=\"color:#e67e22;\">&#9888; MCP Python package not found</span>';
          pathEl.textContent = 'none — cannot start MCP server';
        }}
       }}).catch(function() {{}});
}})();
            </script>"""
            config_html += f'<div class="actions">{instance_btn}</div>'
            return config_html, js

        return config_html, js

    # Fallback for unknown engine types
    return "<p>Unknown system engine: " + engine_type + "</p>", ""


@app.route("/webui/engine/<engine_type>/presets")
def webui_engine_presets(engine_type):
    """List presets for an engine type."""
    from flask import request as _request
    import traceback
    try:
        q = _request.args.get("q", "").strip()
        filter_model = _request.args.get("model", "").strip() or ""
        params = ""
        if q:
            from urllib.parse import urlencode as _urlencode
            params = "?" + _urlencode({"q": q})
        data = api_get(f"engine/{engine_type}/presets{params}")

        if "error" in data:
            content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
            return make_html(f"Presets -- {engine_type}", "engines", content, engine_types=get_engine_types())

        presets = data.get("items", [])
        print(f"[qr] DEBUG presets after get: len={len(presets)} first_name={presets[0].get('name','?') if presets else 'empty'}", file=sys.stderr)
        preset_count = len(presets)

        # Engine display name for header
        engine_display_name = {"llama_server": "llama.cpp", "llama_rpc": "LLAMA.RPC"}.get(engine_type, engine_type)

        # Count presets per model and build model filter options
        model_counts = {}
        for p in presets:
            mid = p.get("model_id")
            mname = p.get("model_name", "None") or "None"
            key = f"{mid}|||{mname}" if mid else "None"
            model_counts[key] = model_counts.get(key, 0) + 1

        all_models = sorted(set(
            (p.get("model_id"), p.get("model_name") or "None")
            for p in presets
        ), key=lambda x: (x[1] if x[1] != "None" else ""))

        # Apply model filter
        if filter_model:
            presets = [p for p in presets if (str(p.get("model_id")) == filter_model or str(p.get("model_id") or "None") == filter_model)]

        nav, engines_nav = render_nav("engines", get_engine_types())
        content = render_template('engine_presets.html',
            engine_type=engine_type, presets=presets, search_q=q,
            all_models=all_models, model_counts=model_counts,
            filter_model=filter_model, preset_count=preset_count,
            engine_display_name=engine_display_name,
        )
        return render_template('base.html', title=f"Presets -- {engine_type}", engines_nav=engines_nav, **nav, content=Markup(content))
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[qr] ERROR webui_engine_presets: {tb}", file=sys.stderr)
        return make_html(f"Presets -- {engine_type}", "engines", f'<p style="color:#f44336;">Error: {exc}</p>', engine_types=get_engine_types())


@app.route("/webui/engine/<engine_type>/presets/create")
def webui_engine_presets_create(engine_type):
    """Create a new preset for an engine type."""
    nav, engines_nav = render_nav("engines", get_engine_types())
    content = render_template('engine_presets_create.html', engine_type=engine_type)
    return render_template('base.html', title=f"Create Preset -- {engine_type}", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/engine/<engine_type>/presets/<int:preset_id>", methods=["GET"])
def webui_engine_preset_edit(engine_type, preset_id):
    """Edit an existing preset with affected instances display."""
    data = api_get(f"engine/{engine_type}/presets/{preset_id}")
    if "error" in data or not data.get("data"):
        pdata = api_get(f"presets/{preset_id}")
        if "error" in pdata:
            content = f'<p style="color:#f44336;">Preset {preset_id} not found</p>'
            return make_html(f"Edit Preset -- {engine_type}", "engines", content, engine_types=get_engine_types())
        preset = pdata.get("data", {})
    else:
        preset = data.get("data", {})

    # Format config for template pre-fill (raw dicts for JS tojson filter)
    pconfig = preset.get("config_template", {}) or {}
    if isinstance(pconfig, str):
        try:
            import json as _json
            pconfig = _json.loads(pconfig)
        except (ValueError, TypeError):
            pconfig = {}
    env_data = pconfig.get("env", {}) or {}
    cli_data = pconfig.get("cli_opts", []) or []
    model_data = pconfig.get("model", {}) or {}
    current_model_path = (pconfig.get("model") or {}).get("LLAMA_ARG_MODEL", "")

    affected_instances = preset.get("affected_instances") or []
    affected_count = len(affected_instances)

    # Pass model_id and gpu_device for the new edit form fields
    preset["model_id"] = preset.get("model_id")
    preset["gpu_device"] = preset.get("gpu_device")
    preset["env_data"] = env_data
    preset["cli_data"] = cli_data
    preset["model_data"] = model_data
    preset["current_model_path"] = current_model_path

    nav, engines_nav = render_nav("engines", get_engine_types())
    content = render_template('engine_presets_edit.html',
        engine_type=engine_type, preset=preset, preset_id=preset_id,
        env_data=env_data, cli_data=cli_data, model_data=model_data,
        current_model_path=current_model_path,
        affected_instances=affected_instances, affected_count=affected_count)
    return render_template('base.html', title=f"Edit Preset -- {engine_type}", engines_nav=engines_nav, **nav, content=Markup(content))


# Per-engine model pages deactivated — using global /webui/models instead
#@app.route("/webui/engine/<engine_type>/models")
#def webui_engine_models(engine_type):
#    """List models for an engine type."""
#    data = api_get(f"engine/{engine_type}/models")
#    if "error" in data:
#        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
#        return make_html(f"Models -- {engine_type}", "engines", content, engine_types=get_engine_types())
#
#    models = data.get("items", [])
#    # Pre-format size strings for template
#    for m in models:
#        size = m.get("size_bytes", 0)
#        if size and isinstance(size, (int, float)):
#            if size >= 1024**3: m["size_str"] = f"{size / (1024**3):.1f} GB"
#            elif size >= 1024**2: m["size_str"] = f"{size / (1024**2):.1f} MB"
#            else: m["size_str"] = f"{size} B"
#        else: m["size_str"] = "N/A"
#
#    nav, engines_nav = render_nav("engines", get_engine_types())
#    # Get active nodes for scan dropdown
#    nodes_data = api_get("nodes")
#    nodes = nodes_data.get("items", []) if "error" not in nodes_data else []



@app.route("/webui/ansible-logs")
def webui_ansible_logs():
    """Ansible actions log viewer with sort and limit controls."""
    action_type = request.args.get("action_type") or None
    status = request.args.get("status") or None
    limit_val = int(request.args.get("limit", "100")) or 100

    api_params = {"limit": limit_val}
    if action_type:
        api_params["action_type"] = action_type
    if status:
        api_params["status"] = status

    data = api_get("ansible_actions", api_params)
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        return make_html("Logs", "logs", content, engine_types=get_engine_types())

    items = data.get("items", [])

    # Collect unique action types for filter dropdown
    action_types_sorted = sorted(set(e.get("action_type", "") for e in items if e.get("action_type")))

    # Pre-format entries for template (extract detail strings, format duration)
    ansible_entries = []
    for entry in items:
        try:
            e = dict(entry)
            ts = e.get("created_at") or e.get("started_at") or "?"
            e["ts"] = ts
            action_type_val = e.get("action_type", "?")
            status_val = e.get("status", "?")
            duration = e.get("duration_ms", 0)
            if isinstance(duration, (int, float)) and duration >= 0:
                if duration < 1000:
                    e["dur_str"] = f"{int(duration)}ms"
                else:
                    secs = int(duration) // 1000
                    mins, secs = divmod(secs, 60)
                    e["dur_str"] = f"{mins}m {secs}s" if mins else f"{secs}s"
            else:
                e["dur_str"] = "N/A"

            # Build details string
            detail_str = ""
            stdout_val = e.get("stdout", "")
            stderr_val = e.get("stderr", "")
            results = e.get("results_json")

            if status_val == "failed":
                # Priority: stderr > stdout (error prefix) > details JSON error > exit code
                if stderr_val:
                    detail_str = stderr_val[:80]
                elif stdout_val.startswith("[error]"):
                    # log_ansible_action prepends [error] message when no Ansible output
                    detail_str = stdout_val.strip()[:200]
                else:
                    detail_str = "Exit code: " + str(e.get("exit_code", "?"))
                    # Try extracting error from details JSON (timeout, exec exceptions)
                    try:
                        ts_raw = e.get("task_summary", [])
                        if isinstance(ts_raw, str):
                            task_summary = json.loads(ts_raw)
                        else:
                            task_summary = ts_raw or []
                        if isinstance(task_summary, list):
                            for ts_item in task_summary:
                                err = ts_item.get("error", "")
                                if err and "FAIL:" not in err:
                                    detail_str = "ERROR: " + err[:120]
                                    break
                    except (json.JSONDecodeError, TypeError):
                        pass
            elif action_type_val == "validate_node" and stdout_val:
                try:
                    msg_data = json.loads(stdout_val)
                    if isinstance(msg_data, dict):
                        parts = []
                        if msg_data.get("cpu_cores"): parts.append(f"CPU: {msg_data['cpu_cores']}c")
                        if msg_data.get("ram_mb"): parts.append(f"RAM: {msg_data['ram_mb']//1024}GB")
                        if msg_data.get("os"): parts.append(f"OS: {msg_data['os']}")
                        detail_str = ", ".join(parts) if parts else stdout_val[:80]
                except (json.JSONDecodeError, TypeError):
                    detail_str = stdout_val[:80]
            elif action_type_val == "deploy" and stdout_val:
                detail_str = stdout_val[:80]
            elif results and isinstance(results, dict):
                for play in results.get("plays", []):
                    for task in play.get("tasks", []):
                        name = task.get("task", {}).get("name", "")
                        if task.get("failed"): detail_str += "FAIL:" + name + " "
                        elif task.get("changed"): detail_str += "CHG:" + name + " "
                    break
            e["detail_str"] = detail_str or "N/A"
            ansible_entries.append(e)
        except Exception:
            # Malformed entry — keep safe defaults, don't crash the page
            e = dict(entry)
            e.setdefault("ts", "?")
            e.setdefault("dur_str", "N/A")
            e.setdefault("detail_str", "N/A")
            ansible_entries.append(e)

    nav, engines_nav = render_nav("logs", get_engine_types())
    content = render_template('ansible_logs.html',
        ansible_entries=ansible_entries,
        action_types=action_types_sorted,
        filter_action=action_type,
        filter_status=status,
        filter_limit=limit_val,
    )
    return render_template('base.html', title="Ansible Logs", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/qr-tasks")
def webui_qr_tasks():
     """Running tasks viewer with auto-refresh for in-flight operations.

     Tab 1 (Job Log): RUNNER-1 job/task pipeline via /api/v1/jobs + /tasks
     Tab 2 (Task Log): Legacy qr_actions framework-level operation log
     """
     status_filter = request.args.get("status") or None
     limit_val = int(request.args.get("limit", "50")) or 50

     # Fetch legacy qr_actions for Operation Log tab
     api_params = {"limit": limit_val}
     if status_filter:
         api_params["status"] = status_filter

     data = api_get("qr_actions", api_params)
     if "error" in data:
         content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
         return make_html("Running Tasks", "tasks", content, engine_types=get_engine_types())

     items = data.get("items", [])
     running_count = sum(1 for i in items if i.get("status") == "running")
     failed_count = sum(1 for i in items if i.get("status") in ("failed", "stuck"))

     # Fetch RUNNER-1 jobs for Staged Deploys tab
     jobs_data = api_get("jobs")
     jobs_running = 0
     if jobs_data.get("status") == "ok":
         jobs_running = sum(1 for j in jobs_data.get("items", []) if j.get("status") == "running")

     nav, engines_nav = render_nav("tasks", get_engine_types())
     content = render_template('qr_tasks.html',
         qr_entries=items,
         filter_status=status_filter,
         filter_limit=limit_val,
         running_count=running_count,
         failed_count=failed_count,
         jobs_running=jobs_running,
     )
     return render_template('base.html', title="Running Tasks", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/benchmarks")
def webui_benchmarks():
    """Benchmark page with instance selector, prompt editor, preset change, and results."""
    instances_data = api_get("instances")
    prompts_data = api_get("benchmarks/prompts")
    # Fetch llama_server presets for the preset selector dropdown
    presets_data = api_get("engine/llama_server/presets")
    nav, engines_nav = render_nav("benchmarks", get_engine_types())
    content = render_template('benchmark.html',
        instances=instances_data.get("items", []),
        prompts=prompts_data.get("items", []),
        llama_presets=presets_data.get("items", [])
    )
    return render_template('base.html', title="Benchmarks", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/playbooks")
def webui_playbooks():
    """Playbook registry listing page."""
    file_type = request.args.get("file_type") or ""
    search = request.args.get("search") or ""

    pb_params = {}
    if file_type:
        pb_params["file_type"] = file_type
    if search:
        pb_params["search"] = search

    playbooks_data = api_get("playbooks", pb_params)
    playbooks = playbooks_data.get("items", [])

    nav, engines_nav = render_nav("playbooks", get_engine_types())
    content = render_template('playbooks.html',
        playbooks=playbooks,
        filter_type=file_type,
        filter_search=search,
    )
    return render_template('base.html', title="Playbooks", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/playbooks/<int:pb_id>")
def webui_playbook_content(pb_id):
    """Display a single playbook's YAML content as HTML with syntax highlighting."""
    data = api_get(f"playbooks/{pb_id}/content")
    if "error" in data or data.get("status") != "ok":
        content = f'<p style="color:#f44336;">Playbook not found or error: {data.get("message", data.get("error", ""))}</p>'
        nav, engines_nav = render_nav("playbooks", get_engine_types())
        return render_template('base.html', title="Playbook — Error", engines_nav=engines_nav, **nav, content=Markup(content))

    pb = data.get("data", {})
    playbook_id = pb.get("playbook_id", pb_id)
    filename = pb.get("playbook_name", f"playbook_{pb_id}.yml")
    db_checksum = pb.get("checksum_sha256", "N/A")
    raw_content = pb.get("content", "")
    content_html = Markup(raw_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    nav, engines_nav = render_nav("playbooks", get_engine_types())
    return render_template('base.html', title=f"Playbook: {filename}", engines_nav=engines_nav, **nav, content=Markup(f'''
<div style="padding:16px;">
  <a href="/webui/playbooks" style="color:#4fc3f7;text-decoration:none;">&larr; Back to playbooks</a>
  <h2 style="margin-top:16px;">{filename}</h2>
  <table style="margin-bottom:16px;font-size:13px;border-collapse:collapse;">
    <tr><td style="padding:4px 12px 4px 0;color:#888;">ID</td><td>{playbook_id}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#888;">DB Checksum</td><td style="font-family:monospace;font-size:12px;word-break:break-all;">{db_checksum[:64] if db_checksum and db_checksum != "N/A" else "N/A"}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#888;">Checksum Now</td><td id="current-chk" style="font-family:monospace;font-size:12px;word-break:break-all;"><span style="color:#999;">computing...</span></td></tr>
  </table>
  <pre style="background:#1e1e1e;color:#d4d4d4;padding:16px;border-radius:4px;overflow-x:auto;font-size:13px;line-height:1.5;"><code id="yaml-content">{content_html}</code></pre>
</div>
<script>
(function() {{
  function simpleHash(str) {{
    var hash = 0;
    for (var i = 0; i < str.length; i++) {{
      var ch = str.charCodeAt(i);
      hash = ((hash << 5) - hash) + ch;
      hash = hash & hash; // Convert to 32bit integer
    }}
    // Expand to a longer hex string by running multiple passes
    var base = Math.abs(hash).toString(16).padStart(8, "0");
    // Use multiple character positions for better distribution
    var h1 = "", h2 = "";
    for (var i = 0; i < str.length; i += 3) {{ h1 += str.charCodeAt(i % str.length).toString(16).padStart(2, "0"); }}
    for (var i = str.length - 1; i >= 0; i -= 3) {{ h2 += str.charCodeAt(i % str.length).toString(16).padStart(2, "0"); }}
    return (base + h1 + h2).substring(0, 64).toLowerCase();
  }}

  // Try crypto.subtle first, fall back to simple hash
  var el = document.getElementById("current-chk");
  if (!el) return;
  var rawContent = document.getElementById("yaml-content").textContent;
 if (!rawContent) {{
    el.innerHTML = '<span style="color:#f44336;">no content</span>';
    return;
  }}

  if (typeof crypto !== 'undefined' && crypto.subtle && typeof crypto.subtle.digest === 'function') {{
    var bytes = new TextEncoder().encode(rawContent);
    crypto.subtle.digest("SHA-256", bytes).then(function(hashBuffer) {{
      var hashArray = Array.from(new Uint8Array(hashBuffer));
      var hash = hashArray.map(function(b) {{ return b.toString(16).padStart(2, "0"); }}).join("");
      var dbCk = "{db_checksum[:64]}";
      if (dbCk && dbCk !== "N/A" && dbCk.toLowerCase() === hash.toLowerCase()) {{
        el.innerHTML = '<span style="color:#4caf50;" title="Matches DB checksum">' + hash + '</span> <span style="color:#4caf50;font-size:11px;">\\u2713</span>';
      }} else {{
        el.innerHTML = '<span style="color:#f44336;" title="Does not match DB checksum">' + hash + '</span> <span style="color:#f44336;font-size:11px;">\\u26A0</span>';
      }}
    }}).catch(function(e) {{
      el.innerHTML = '<span style="color:#f44336;">crypto error: ' + e.message + '</span>';
    }});
  }} else {{
    // Fallback: use a deterministic hash (not crypto-grade but consistent)
    var hash = simpleHash(rawContent);
    el.innerHTML = '<span style="color:#999;font-size:10px;">(compat mode)</span> ' + hash;
  }}
}})();
</script>
'''))


@app.route("/webui/rpccluster")
def webui_rpccluster():
    """Herd page — RPC cluster management for llama-server + RPC instances."""
    nav, engines_nav = render_nav("rpccluster", get_engine_types())
    content = render_template('rpccluster.html')
    return render_template('base.html', title="Herd — RPC Cluster Management", engines_nav=engines_nav, **nav, content=Markup(content))


@app.route("/webui/proxy/<int:inst_id>")
def webui_instance_proxy(inst_id):
    """Proxy page for remote instance WebUI.

    Loads the remote instance's web interface in an iframe, served through
    qr's reverse proxy so it stays inside quickrobot's frame with navigation.

    Args:
        inst_id: Instance primary key.

    Returns:
        HTML page with iframe loading proxied remote content.
    """
    data = api_get(f"instances/{inst_id}")
    if "error" in data:
        content = f'<p style="color:#f44336;">Instance not found</p>'
        return make_html("Proxy", "instances", content, engine_types=get_engine_types())

    inst = data.get("data", {})
    node_hostname = inst.get("node_hostname", "") or inst.get("node_name", QR_DEFAULT_LOCALHOST)
    port = inst.get("port_assigned", 8080)
    inst_name = inst.get("name", str(inst_id))
    nav, engines_nav = render_nav("instances", get_engine_types())
    return render_template('base.html', title=f"Proxy: {inst_name}", engines_nav=engines_nav, **nav,
                           content=Markup(f'''
<div style="height:calc(100vh - 60px);">
  <div style="padding:8px 16px;background:#f8f9fa;border-bottom:1px solid #dee2e6;">
    <a href="/webui/instances" style="color:#4fc3f7;text-decoration:none;">← Back to instances</a>
    &nbsp;—&nbsp;
    <strong>{inst_name}</strong>
    &nbsp;({node_hostname}:{port})
  </div>
  <iframe src="/api/v1/proxy/{inst_id}/" style="width:100%;height:calc(100vh - 100px);border:none;" sandbox="allow-same-origin allow-scripts allow-forms allow-popups"></iframe>
</div>
'''))


@app.route("/webui/instances/<int:inst_id>", methods=["GET"])
def webui_instance_detail_v2(inst_id):
    """Enhanced instance detail page with state badge, actions, merged config, logs."""
    data = api_get(f"instances/{inst_id}")
    if "error" in data:
        content = f'<p style="color:#f44336;">API unavailable: {data["error"]}</p>'
        return make_html(f"Instance {inst_id}", "instances", content, engine_types=get_engine_types())

    inst = data.get("data", {})
    state = inst.get("state", "unknown")
    name = inst.get("name", "unknown")
    engine_name = inst.get("engine_type_name", "?")
    engine_display = inst.get("engine_display_name") or engine_name
    node_name = inst.get("node_name", "?")
    port_assigned_val = inst.get("port_assigned", "N/A")
    transport = inst.get("transport", "N/A")
    instance_uuid_val = inst.get("instance_uuid", "N/A")
    created_at = inst.get("created_at", "?")
    last_change = inst.get("last_state_change", "?")
    start_on_boot = inst.get("start_on_boot", True)
    merged_config = inst.get("merged_config") or {}
    if isinstance(merged_config, str):
        try: import json as _j; merged_config = _j.loads(merged_config)
        except Exception: merged_config = {}

    # Unified port resolver — determines the actual port in use after the full merge chain.
    # llama_server/rpc: port_assigned is authoritative (from port allocator); LLAMA_ARG_PORT is fallback.
    # subprocess/universal: port comes from config_override → merged_config.env.port.
    merged_env = merged_config.get("env", {}) if isinstance(merged_config, dict) else {}
    if engine_name in ("llama_server", "llama_rpc"):
        actual_port = port_assigned_val or merged_env.get("LLAMA_ARG_PORT") or "N/A"
    else:
        actual_port = merged_env.get("port") or port_assigned_val or "N/A"

    # Parse config_override for build-related fields
    co_raw = inst.get("config_override", {})
    co_dict = {} if not isinstance(co_raw, dict) else co_raw
    co_git_pull = co_dict.get("git_pull_cmd", "")
    co_build_threads = co_dict.get("build_threads", None)
    build_number = inst.get("build_number", "") or ""

    # Polling intervals from DB config with fallback to code defaults
    engine_type_id = inst.get("engine_type_id") or 0
    is_local = (inst.get("node_id", -1) == 1)
    try:
        from db.sqlite import pool as _pool
        if '_CONFIG' in dir():
            _db_path = _CONFIG["db_path"]
        else:
            _db_path = os.path.join(os.getcwd(), "data", "quickrobot.db")
        polling_local_sec = get_polling_intervals(_db_path, engine_type_id, is_local=True)
        polling_remote_sec = get_polling_intervals(_db_path, engine_type_id, is_local=False)
        polling_interval_sec = polling_local_sec if is_local else polling_remote_sec
    except Exception:
        from lib.lib_constants import POLLING_INTERVAL_LOCAL_SEC, POLLING_INTERVAL_REMOTE_SEC
        polling_interval_sec = POLLING_INTERVAL_LOCAL_SEC if is_local else POLLING_INTERVAL_REMOTE_SEC

    # Universal engine config_override fields
    is_universal = (engine_name == QR_ENGINE_UNIVERSAL_NAME)
    univ_co = {}
    if is_universal:
        try: import json as _j2
        except Exception: _j2 = None
        if isinstance(co_raw, str):
            try: univ_co = _j2.loads(co_raw) if _j2 else {}
            except Exception: univ_co = {}
        elif isinstance(co_raw, dict):
            univ_co = co_raw
        else:
            univ_co = {}
    univ_playbook_dir = univ_co.get("playbook_dir", "custom")
    univ_deploy_pb = univ_co.get("deploy_playbook", "")
    univ_undeploy_pb = univ_co.get("undeploy_playbook", "")
    univ_binary_path = univ_co.get("binary_path", "")
    univ_start_cmd = univ_co.get("start_command", "")
    univ_stop_cmd = univ_co.get("stop_command", "")
    univ_restart_cmd = univ_co.get("restart_command", "")
    univ_base_port = univ_co.get("base_port", 0)
    univ_env_vars = univ_co.get("env_vars", {}) or {}
    univ_cli_args = univ_co.get("cli_args", []) or []
    univ_instant_fb = bool(univ_co.get("instant_feedback", False))
    univ_fb_timeout = int(univ_co.get("feedback_timeout", 30))

    # Subprocess engine env_passthrough info (for UI banner)
    is_subprocess = (engine_name == QR_ENGINE_SUBPROCESS_NAME)
    _raw_ep = co_dict.get("env_passthrough") if is_subprocess else None
    if isinstance(_raw_ep, str):
        subprocess_env_passthrough = _raw_ep.lower() == "true"
    elif _raw_ep is not None:
        subprocess_env_passthrough = bool(_raw_ep)
    else:
        subprocess_env_passthrough = True  # default when unset
    subprocess_user_env_vars_count = len(co_dict.get("env_vars", {})) if is_subprocess and not subprocess_env_passthrough else 0

    # Extract preset info (only for engine types that support presets)
    preset_id = inst.get("preset_id")
    preset_name = inst.get("preset_name") or "No preset"
    presets_list = []
    if engine_name in ("llama_server", "llama_rpc"):
        _presets_data = api_get(f"engine/{engine_name}/presets")
        if "error" not in _presets_data:
            presets_list = _presets_data.get("items", [])

    # Model info for current preset (llama.cpp only) — size, mmproj, draft model
    model_info = None
    if engine_name in ("llama_server", "llama_rpc") and preset_id:
        # First get the preset to find its model_id
        _preset_data = api_get(f"engine/{engine_name}/presets/{preset_id}")
        _model_id = None
        if "error" not in _preset_data:
            _preset_data = _preset_data.get("data", {})
            _model_id = _preset_data.get("model_id")
        # Now fetch model info using the preset's model_id
        if _model_id:
            _model_data = api_get(f"models/{_model_id}")
        else:
            _model_data = {"error": "no_model"}
        if "error" not in _model_data:
            m = _model_data.get("data", {})
            size_bytes = m.get("size_bytes", 0)
            # Human-readable size display
            if size_bytes >= 1073741824:
                size_display = "{:.1f} GB".format(size_bytes / 1073741824)
            elif size_bytes >= 1048576:
                size_display = "{:.0f} MB".format(size_bytes / 1048576)
            else:
                size_display = "{:.0f} KB".format(size_bytes / 1024)
            model_info = {
                "name": m.get("name", "?"),
                "size_display": size_display,
                "mmproj_path": m.get("mmproj_path"),
                "draft_model_path": m.get("draft_model_path"),
            }

    # STATUS-1: Unified status with engine-specific actions (single source of truth for badges)
    status_api = api_get(f"instances/{inst_id}/status")
    health_alive = False
    health_latency = None
    health_error = ""
    if "error" not in status_api and status_api.get("data"):
        d = status_api["data"]
        health_alive = True
        health_latency = d.get("latency_ms")
        health_error = d.get("error", "")

    system_status_data = api_get(f"instances/{inst_id}/system-status")
    is_llama = (engine_name == QR_ENGINE_LLAMA_SERVER_NAME)
    is_system = inst.get("system_managed", 0)
    is_mcp = (engine_name == QR_ENGINE_MCP_NAME)
    mcp_flags = {}
    if is_system and "error" not in system_status_data:
        sdata = system_status_data.get("data", {})
        mcp_flags = {
            "allow_reads": sdata.get("allow_reads", True),
            "allow_writes": sdata.get("allow_writes", True),
            "allow_proxy": sdata.get("allow_proxy", True),
        }

    # Build detail_items list for template
    # Use STATUS-1 data as single source of truth for state/health badges.
    # override_system_instance_states() already updated DB state based on process health,
    # so we trust the instance's state field directly.
    state_badge_html = status_badge(state)
    health_badge = ""
    if is_system and "error" not in status_api:
        sd = status_api.get("data", {}) or {}
        ed = sd.get("engine_data", {})
        pid = ed.get("pid")
        uptime = ed.get("uptime_seconds", 0)
        rss = ed.get("rss_bytes", 0)
        if pid:
            # System engine with active PID — running
            health_badge = Markup(f'<span class="badge badge-running">running</span>')
            if uptime and uptime > 0:
                hrs = uptime // 3600; mins = (uptime % 3600) // 60
                health_badge += Markup(f' <small style="color:#888;">{hrs}h {mins}m</small>')
            if rss and rss > 0:
                health_badge += Markup(f' <small style="color:#888;">RSS {rss // (1024*1024)}MB</small>')
        elif sd.get("engine_type_name") in (QR_ENGINE_WEBUI_NAME, QR_ENGINE_MCP_NAME):
            # System engine with stale/dead PID — show error state
            health_badge = Markup('<span class="badge badge-error">dead</span>')
        elif sd.get("engine_type_name") == QR_ENGINE_API_NAME:
            # API instance always alive when serving requests
            health_badge = Markup('<span class="badge badge-running">running</span>')
    elif health_alive:
        health_badge = Markup(f'<span class="badge badge-running">alive</span>')
        if health_latency is not None: health_badge += Markup(f' <small style="color:#666;">{health_latency:.0f}ms</small>')
    elif health_error:
        health_badge = Markup(f'<span class="badge badge-other">unknown</span> <small style="color:#888;">{health_error[:50]}</small>')

    detail_items = [
        ("Instance ID", str(inst_id)), ("UUID", instance_uuid_val or "N/A"),
        ("State", state_badge_html), ("Health", health_badge),
        ("Engine Type", engine_display),
    ]
    if is_system:
        local_hostname = "localhost"
        try: local_hostname = socket.gethostname()
        except Exception: pass
        detail_items.append(("Node", f"{local_hostname} (local)"))
        sys_data = system_status_data.get("data", {}) if "error" not in system_status_data else {}
        # Use engine-specific fields for port/IP
        if engine_name == QR_ENGINE_WEBUI_NAME:
            detail_items.append(("Port", str(sys_data.get("web_ui_port") or sys_data.get("port") or actual_port or "N/A")))
            detail_items.append(("Host", str(sys_data.get("web_ui_host") or sys_data.get("ip") or QR_DEFAULT_LOCALHOST)))
        elif engine_name == QR_ENGINE_API_NAME:
            detail_items.append(("Port", str(sys_data.get("port") or actual_port or "N/A")))
            detail_items.append(("Host", str(sys_data.get("ip") or QR_DEFAULT_LOCALHOST)))
        else:
            detail_items.append(("Port", str(sys_data.get("port") or actual_port or "N/A")))
            if sys_data.get("ip"): detail_items.append(("IP", sys_data["ip"]))
    else:
        detail_items.append(("Node", node_name))
        detail_items.append(("Port", str(actual_port)))
    detail_items.extend([("Transport", "local" if is_system else transport), ("Created", str(created_at)), ("Last State Change", str(last_change))])
    if is_llama: detail_items.append(("GPU Device", inst.get("gpu_device", "") or "not set"))
    # Use STATUS-1 engine_data for system instance details (RSS, uptime) — single source of truth
    if is_system and "error" not in status_api:
        sd = status_api.get("data", {}) or {}
        ed = sd.get("engine_data", {})
        if "rss_bytes" in ed and ed["rss_bytes"]:
            detail_items.append(("RSS Memory", f"{ed['rss_bytes'] / (1024*1024):.1f} MB"))
        if ed.get("uptime_seconds", 0) > 0:
            hrs = ed["uptime_seconds"] // 3600; mins = (ed["uptime_seconds"] % 3600) // 60
            detail_items.append(("Uptime", f"{hrs}h {mins}m"))

    status_actions = []
    status_warnings = []
    status_engine_data = {}
    if "error" not in status_api:
        status_data = status_api.get("data", {}) or {}
        status_actions = status_data.get("actions", [])
        status_warnings = status_data.get("warnings", [])
        status_engine_data = status_data.get("engine_data", {})

    # For system-managed instances, use STATUS-1 state as single source of truth.
    # This ensures the detail page shows real-time process health, not stale DB state.
    if is_system and "error" not in status_api:
        sd = status_api.get("data", {}) or {}
        real_state = sd.get("state")
        if real_state:
            state = real_state

    # Convert STATUS-1 actions to template tuple format: (name, label, endpoint, enabled)
    actions = []
    for sa in status_actions:
        aname = sa.get("name", "")
        alabel = sa.get("label", aname)
        # STATUS-1 action names now match API endpoints directly (post redesign)
        endpoint = aname
        enabled = not sa.get("disabled", False)
        actions.append((aname, alabel, endpoint, enabled))

   # Prepare config data for template
    env_data = {}; cli_data = []; model_data = {}
    config_json = "No merged config"
    is_build_engine = engine_type_id in (QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC)
    if is_build_engine and merged_config:
        env_data = merged_config.get("env", {}) or {}
        cli_data = merged_config.get("cli_opts", []) or []
        model_data = merged_config.get("model", {}) or {}
        config_json = json.dumps(merged_config, indent=2)

    # Journal logs
    journal_data_raw = api_get(f"instances/{inst_id}/journal", {"lines": 100})
    journal_logs_text = "No journal logs available"
    if "error" not in journal_data_raw and journal_data_raw.get("logs"):
        journal_logs_text = journal_data_raw["logs"].replace("&", "&amp;").replace("<", "&lt;")

    # Ansible entries for template
    ansible_raw = api_get("ansible_actions", {"instance_id": inst_id, "limit": 10})
    ansible_entries = []
    if "error" not in ansible_raw:
        for entry in ansible_raw.get("items", []):
            e = dict(entry)
            e["ts"] = e.get("created_at") or e.get("started_at") or "?"
            e["dur_str"] = f"{int(e.get('duration_ms',0))}ms" if isinstance(e.get("duration_ms"), (int,float)) and e.get("duration_ms") else "N/A"
            status_val = e.get("status", "?")
            css_map = {"deploy":"ansible-log-deploy","validate":"ansible-log-validate","scan":"ansible-log-scan"}
            e["css_class"] = "ansible-log-failed" if status_val == "failed" else css_map.get(e.get("action_type",""), "ansible-log-validate")
            ansible_entries.append(e)

    # Action log entries for template
    log_raw = api_get(f"instances/{inst_id}/logs", {"limit": 50})
    log_entries = []
    if "error" not in log_raw:
        for entry in log_raw.get("items", []):
            e = dict(entry)
            if isinstance(e.get("detail"), dict): e["detail"] = json.dumps(e["detail"])
            log_entries.append(e)

    nav, engines_nav = render_nav("instances", get_engine_types())
    content = render_template('instance_detail.html',
        inst_id=inst_id, name=name, state=state, engine_name=engine_name,
        engine_display=engine_display, node_name=node_name, port=actual_port,
        transport=transport, instance_uuid_val=instance_uuid_val,
        created_at=created_at, last_change=last_change, start_on_boot=start_on_boot,
        is_llama=is_llama, is_system=is_system, is_universal=is_universal, is_mcp=is_mcp, is_subprocess=is_subprocess,
        mcp_flags=mcp_flags, subprocess_env_passthrough=subprocess_env_passthrough,
        subprocess_user_env_vars_count=subprocess_user_env_vars_count,
        merged_config=merged_config,
        env_data=env_data, cli_data=cli_data, model_data=model_data,
        config_json=config_json, detail_items=detail_items, actions=actions,
        status_warnings=status_warnings, journal_logs_text=journal_logs_text, ansible_entries=ansible_entries,
        log_entries=log_entries, log_data=nav,
        has_benchmark_btn=(engine_name == 'iperf3' and state in ('deployed','running','stopped')),
         preset_id=preset_id, preset_name=preset_name, presets_list=presets_list,
         model_info=model_info,
        build_number=build_number, co_git_pull=co_git_pull, co_build_threads=co_build_threads,
        # Polling intervals
          polling_interval_sec=polling_interval_sec, polling_local_sec=polling_local_sec,
          polling_remote_sec=polling_remote_sec, is_local=is_local,
         # Universal engine fields
        univ_co=univ_co, univ_playbook_dir=univ_playbook_dir,
        univ_deploy_pb=univ_deploy_pb, univ_undeploy_pb=univ_undeploy_pb,
        univ_binary_path=univ_binary_path, univ_start_cmd=univ_start_cmd,
        univ_stop_cmd=univ_stop_cmd, univ_restart_cmd=univ_restart_cmd,
        univ_base_port=univ_base_port, univ_env_vars=univ_env_vars,
        univ_cli_args=univ_cli_args, univ_instant_fb=univ_instant_fb,
        univ_fb_timeout=univ_fb_timeout,
    )
    return render_template('base.html', title=f"Instance -- {name}", engines_nav=engines_nav, **nav, content=Markup(content))


# ---------------------------------------------------------------------------
# WebUI Config endpoint
# ---------------------------------------------------------------------------

@app.route("/api/v1/webui/config", methods=["GET"])
def webui_config():
    """Return WebUI configuration including timezone settings.

    Reads the web_ui_timezone from engine_configs and returns
    timezone info for JS consumption.

    Returns:
        JSON: {"timezone": str, "utc_offset_hours": float, "local_tz_name": str}
    """
    from lib.lib_time import parse_tz_offset, get_local_tz_name

    tz_name = DEFAULT_TIMEZONE
    try:
        data = api_get("engines/quickrobot-webui/settings")
        if "error" not in data and data.get("data"):
            settings = data["data"]
            if "web_ui_timezone" in settings:
                tz_name = settings["web_ui_timezone"]
    except Exception:
        pass

    offset_hours = parse_tz_offset(tz_name)

    return jsonify({
        "timezone": tz_name,
        "utc_offset_hours": offset_hours,
        "local_tz_name": get_local_tz_name(),
    })


# ---------------------------------------------------------------------------
# API Proxy — forwards browser requests to the quickrobot API server
# ---------------------------------------------------------------------------

@app.route("/api/v1/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_proxy(subpath):
    """Proxy request from browser to the quickrobot API server.

    This allows the WebUI to serve on its own port/protocol (e.g. HTTPS) while
    forwarding API calls internally to the quickrobot server process.
    Eliminates hardcoded API URLs in JavaScript.

    Args:
        subpath: Remaining path after /api/v1/ (e.g., 'engines/rpc/config')
    """
    import urllib.request
    import urllib.error
    import urllib.parse as _urllib_parse

    # Build target URL from CONFIG['api_base'] (resolved at import time from env)
    base = CONFIG["api_base"]
    url = f"{base}/{subpath}"
    if request.query_string:
        url += "?" + request.query_string.decode()

    # Prepare request data
    data = request.get_data() if request.method in ("POST", "PUT", "PATCH") else None

    headers = {}
    for key, value in request.headers:
        # Skip hop-by-hop headers that shouldn't be forwarded
        if key.lower() not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value

    try:
        from lib.lib_proxy_reader import proxy_request as _proxy_req
        body, status_code, resp_headers = _proxy_req(
            url, data=data, headers=headers,
            method=request.method, timeout=3600)
        # Ensure proper charset on all responses — Firefox needs explicit charset
        ct_header = None
        clean_headers = []
        for k, v in resp_headers.items():
            kl = k.lower()
            if kl == "content-type" and "charset" not in v.lower():
                ct_header = f"{v}; charset=utf-8"
            elif kl == "content-length":
                continue  # let Flask set it
            else:
                clean_headers.append((k, v))
        if ct_header:
            clean_headers.append(("Content-Type", ct_header))
        return Response(body, status=status_code, headers=dict(clean_headers))

    except urllib.error.HTTPError as e:
        # Read error body — fall back to safe JSON if read fails (e.g. empty body,
        # HTML error from an unhandled exception upstream)
        try:
            raw_body = e.read()
        except Exception:
            raw_body = b""
        # Ensure Content-Type is always application/json for Firefox compatibility
        clean_headers = [("Content-Type", "application/json; charset=utf-8")]
        for k, v in e.headers.items():
            kl = k.lower()
            if kl == "content-length":
                continue  # let Flask set it
            elif kl != "content-type":  # skip original CT, we set it above
                clean_headers.append((k, v))
        body = raw_body if raw_body else b'{"status":"error","code":"UPSTREAM_ERROR","message":"Upstream returned status ' + str(e.code).encode() + b'"}'
        return Response(body, status=e.code, headers=dict(clean_headers))

    except Exception as exc:
        from lib.lib_proxy_reader import ProxyConnectionError as _PCE
        if isinstance(exc, _PCE):
            error_msg = str(exc)
        else:
            error_msg = f"Proxy error: {exc}"
        error_body = f"{{\"status\":\"error\",\"code\":\"PROXY_ERROR\",\"message\":\"{error_msg}\"}}".encode()
        return Response(error_body, status=502, headers={"Content-Type": "application/json; charset=utf-8"})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _check_webui_args():
    """Validate that quickrobot_webui.py was started with explicit --host and --port.

    Without these, the WebUI binds to 0.0.0.0:8041 which may surprise users.
    The API server passes these args when auto-starting the WebUI subprocess.

    Returns:
        True if valid, exits with error if not.
    """
    import sys as _sys_mod
    # Check sys.argv for explicit --host and --port flags
    has_host = any(a.startswith("--host=") or a == "--host" for a in sys.argv[1:])
    has_port = any(a.startswith("--port=") or a == "--port" for a in sys.argv[1:])
    if not has_host or not has_port:
        missing = []
        if not has_host:
            missing.append("--host")
        if not has_port:
            missing.append("--port")
        print(f"FATAL: quickrobot_webui.py requires explicit {', '.join(missing)} argument(s). "
              f"Example: python3 quickrobot_webui.py --host 127.0.0.1 --port 8041\n"
              f"Tip: normally auto-started by quickrobot.py — no manual start needed.",
              file=_sys_mod.stderr)
        _sys_mod.exit(1)
    return True


def parse_args():
    """Parse CLI arguments for the web UI server."""
    parser = argparse.ArgumentParser(
        description="quickrobot Web UI — frontend for the LAN Controller API.",
        epilog="Tip: normally auto-started by quickrobot.py. To run standalone:\n"
               "  python3 quickrobot_webui.py --host 127.0.0.1 --port 8041\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=None,
                         help="Port to listen on (overrides QUICKROBOT_WEBUI_PORT)")
    parser.add_argument("--host", default=None,
                        help="Host to bind to (overrides QUICKROBOT_WEBUI_HOST)")
    parser.add_argument("--api-host", default=None,
                        help="Quickrobot API server host (overrides QUICKROBOT_API_HOST)")
    parser.add_argument("--api-port", type=int, default=None,
                        help="Quickrobot API server port (overrides QUICKROBOT_API_PORT)")
    parser.add_argument("--api-token", default=None,
                        help="Bearer token for API auth (overrides QUICKROBOT_API_BEARER_TOKEN)")
    parser.add_argument("--api-base", default=None,
                        help="Base URL for the quickrobot API (shortcut, overrides all API_* envs)")
    args = parser.parse_args()
    # Priority: CLI --api-base > CLI --api-host+--api-port > env QUICKROBOT_API_HOST:PORT > default
    if args.api_base:
        api_base = args.api_base
    elif args.api_host and args.api_port:
        api_base = f"http://{args.api_host}:{args.api_port}/api/v1"
    else:
        _host = os.getenv("QUICKROBOT_API_HOST")
        _port = os.getenv("QUICKROBOT_API_PORT")
        if _host and _port:
            api_base = f"http://{_host}:{_port}/api/v1"
        else:
            raise RuntimeError(
                "API base URL not set: define QUICKROBOT_API_HOST + QUICKROBOT_API_PORT in .quickrobot.env"
            )
    # Backfill args.host and args.port with env values if not set
    args.host = args.host or os.getenv("QUICKROBOT_WEBUI_HOST")
    _env_port = os.getenv("QUICKROBOT_WEBUI_PORT")
    args.port = args.port if args.port is not None else (int(_env_port) if _env_port else None)
    return args, api_base


if __name__ == "__main__":
    args, api_base = parse_args()
    CONFIG["api_base"] = api_base
    # Validate that --host and --port were explicitly provided
    _check_webui_args()
    # Validate bind host against QR_FORBIDDEN_HOSTS
    if args.host in QR_FORBIDDEN_HOSTS:
        print(f"[qr] FATAL: Web UI bind host is '{args.host}' — {QR_FORBIDDEN_HOSTS}", file=sys.stderr)
        sys.exit(1)
    # Log rotation (vC): truncate oversized log files on startup
    from lib.lib_system_engine import get_engine_log_path as _log_path, rotate_log_if_needed as _rotate_log
    _rotate_log(_log_path("webui"), "webui")
    # Structured startup log — single line with all config info minus tokens
    _pid = os.getpid()
    _log_path = os.getenv("QUICKROBOT_LOG_PATH", "")
    _log_suffix = f" log_path={_log_path}" if _log_path else ""
    _api_h = args.api_host or os.getenv("QUICKROBOT_API_HOST", "?")
    _api_p = args.api_port or os.getenv("QUICKROBOT_API_PORT", "?")
    print(f"[qr] STARTUP: pid={_pid} host={args.host} port={args.port} api={_api_h}:{_api_p}{_log_suffix}")
    try:
        # === Start periodic health check thread ===
        from lib.lib_system_engine import start_health_check_thread as _start_health
        api_host = args.api_host or os.getenv("QUICKROBOT_API_HOST", "?")
        api_port = int(args.api_port) if args.api_port else int(os.getenv("QUICKROBOT_API_PORT", "?"))
        _health_thread = _start_health(
            api_host=api_host,
            api_port=api_port,
            max_retries=3,
            retry_delay=5,
            check_interval=10
        )
        print(f"[qr] Health check thread started (interval=10s, kill=10s)", flush=True)
        
        app.run(host=args.host, port=args.port, debug=False)
    except OSError as exc:
        if "Address already in use" in str(exc) or "Errno 98" in str(exc):
            print(f"FATAL: Port {args.port} is already in use. Another Web UI instance is running. Exiting.", file=_sys_mod.stderr)
        else:
            print(f"FATAL: {exc}", file=_sys_mod.stderr)
        _sys_mod.exit(1)
