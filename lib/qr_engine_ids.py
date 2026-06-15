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

"""Engine type definitions — single source of truth.

All engine types are defined here as tuples in _QR_ENGINES.
Constants, maps, and helper functions are auto-generated from this list.
No imports needed — pure data file (can be imported at any point).

Usage:
    from lib.qr_engine_ids import QR_ENGINE_LLAMA_SERVER, is_llamacpp_engine, get_name_by_id
    
    if is_llamacpp_engine(instance["engine_type_id"]):
        # llama.cpp family engine
        pass
    
    name = get_name_by_id(21)  # -> "llama_server"
"""

# ── Version ───────────────────────────────────────────────────────────
QUICKROBOT_VERSION = "v0.06"

# ── Forbidden bind hosts (system engines reject these) ───────────────
# If a system engine (.env or startup) binds to any of these, it fails.
QR_FORBIDDEN_HOSTS = ("0.0.0.0", "::", "::0")

# ── SSH connection defaults (overridable via .quickrobot.env) ─────────
# QUICKROBOT_SSH_STRICT_HOST_KEY_CHECKING — yes|accept-new|no
# QUICKROBOT_SSH_CONNECT_TIMEOUT          — seconds
# If not set in .env, use these fallbacks.
_QR_SSH_STRICT_HOST_KEY_CHECKING_FALLBACK = "accept-new"
_QR_SSH_CONNECT_TIMEOUT_FALLBACK = 5

# ── General defaults ─────────────────────────────────────────────────
# QR_DEFAULT_BIND_HOST moved to lib/lib_constants.py (RPC host fallback).
# Host/port for API/WebUI/MCP binding: .env file → FATAL exit on missing.
# Missing = FATAL exit, no silent fallbacks (except RPC playbook defaults).

# Engine definitions: (id, name, category)
# Categories: "system" = managed by API lifecycle
#             "llamacpp" = llama.cpp family (server + rpc)
#             "infra" = infrastructure tools
_QR_ENGINES = [
    # System engines (managed by API server lifecycle)
    (1,  "quickrobot-api",  "system"),
    (2,  "quickrobot-webui","system"),
    (3,  "quickrobot-mcp",  "system"),
    # llama.cpp engine family
    (21, "llama_server",    "llamacpp"),
    (22, "llama_rpc",       "llamacpp"),
    # Infrastructure engines
    (11, "universal",       "infra"),
    (12, "subprocess",      "infra"),
    (31, "iperf3",          "infra"),
]

# -- Auto-generated constants -----------------------------------------------
# QR_ENGINE_<UPPERCASE_NAME> = ID  (e.g., QR_ENGINE_LLAMA_SERVER = 21)
for _id, _name, _cat in _QR_ENGINES:
    globals()[f"QR_ENGINE_{_name.upper().replace('-','_')}"] = _id

# Short aliases for system engines (backward compat + convenience)
QR_ENGINE_API     = QR_ENGINE_QUICKROBOT_API
QR_ENGINE_WEBUI   = QR_ENGINE_QUICKROBOT_WEBUI
QR_ENGINE_MCP     = QR_ENGINE_QUICKROBOT_MCP

# -- String names (for engine_type_name DB comparisons) ---------------------
QR_ENGINE_API_NAME         = "quickrobot-api"
QR_ENGINE_WEBUI_NAME       = "quickrobot-webui"
QR_ENGINE_MCP_NAME         = "quickrobot-mcp"
QR_ENGINE_LLAMA_SERVER_NAME = "llama_server"
QR_ENGINE_LLAMA_RPC_NAME   = "llama_rpc"

# -- Auto-generated maps ----------------------------------------------------
# id -> name (e.g., 21 -> "llama_server", 22 -> "llama_rpc")
QR_ENGINE_NAME_MAP = {_id: name for _id, name, _cat in _QR_ENGINES}
# name -> id (e.g., "llama_server" -> 21)
QR_ENGINE_ID_MAP   = {name: _id for _id, name, _cat in _QR_ENGINES}
# id -> category
QR_ENGINE_CAT_MAP  = {_id: cat  for _id, name, cat in _QR_ENGINES}

# Category membership tuples (for fast "in" checks without function call)
QR_SYSTEM_IDS = tuple(_id for _id, _, c in _QR_ENGINES if c == "system")           # (1, 2, 3)
QR_LLAMACPP_IDS = tuple(_id for _id, _, c in _QR_ENGINES if c == "llamacpp")      # (21, 22)
QR_INFRA_IDS = tuple(_id for _id, _, c in _QR_ENGINES if c == "infra")            # (11, 12, 31)

# Engine name aliases: map non-canonical names to the canonical hyphen form.
# The ID_MAP uses the hyphen form as keys, so aliases must resolve to it.
_QR_NAME_ALIASES = {}
for _name in (_n for _, _n, _ in _QR_ENGINES):
    # Map underscore variant -> canonical hyphen name
    _underscore = _name.replace("-", "_")
    if _underscore != _name:
        _QR_NAME_ALIASES[_underscore] = _name

# -- Port defaults (engine-specific, NOT generic QR_DEFAULT_* names) --------
QR_ENGINE_PORT_DEFAULTS = {
    "quickrobot-api":  8040,
    "quickrobot-webui": 8041,
    "quickrobot-mcp":   8042,
}

# -- Helper functions -------------------------------------------------------

def is_system_engine(tid):
    """Check if engine ID is system-managed (id < 10).
    
    Args:
        tid: Integer engine type ID.
    
    Returns:
        True if system-managed, False otherwise.
    """
    return tid in QR_SYSTEM_IDS


def is_llamacpp_engine(tid):
    """Check if engine ID belongs to the llama.cpp family.
    
    Args:
        tid: Integer engine type ID.
    
    Returns:
        True if llama_server or llama_rpc, False otherwise.
    """
    return tid in QR_LLAMACPP_IDS


def is_infra_engine(tid):
    """Check if engine ID is an infrastructure engine.
    
    Args:
        tid: Integer engine type ID.
    
    Returns:
        True if universal/subprocess/iperf3, False otherwise.
    """
    return tid in QR_INFRA_IDS


def get_name_by_id(tid):
    """Get engine name from ID.
    
    Args:
        tid: Integer engine type ID.
    
    Returns:
        Engine name string (e.g., "llama_server"), or None if not found.
    """
    return QR_ENGINE_NAME_MAP.get(tid)


def get_id_by_name(name):
    """Get engine ID from name. Accepts hyphen or underscore variants.
    
    Args:
        name: Engine type name string (e.g., "llama_rpc" or "llama-rpc").
    
    Returns:
        Integer engine type ID, or None if not found.
    """
    # Direct lookup first
    direct = QR_ENGINE_ID_MAP.get(name)
    if direct is not None:
        return direct
    # Alias lookup (quickrobot_api -> quickrobot-api)
    canonical = _QR_NAME_ALIASES.get(name)
    if canonical is not None:
        return QR_ENGINE_ID_MAP.get(canonical)
    return None


def get_port_default(engine_name):
    """Get default port for an engine type.
    
    Args:
        engine_name: Engine type name string.
    
    Returns:
        Integer port number, or None if no default defined.
    """
    return QR_ENGINE_PORT_DEFAULTS.get(engine_name)


# ── Environment variable name constants (ENV whitelist SSOT) ──────────
# Used by build_subprocess_env() in lib/lib_system_engine.py for
# WebUI/MCP/subprocess engine env dicts. All 17 names match the keys
# used across engine/quickrobot_webui/, engine/quickrobot_mcp/, and
# lib/lib_system_engine.py — eliminating hardcoded string drift.
QR_ENV_PATH = "PATH"
QR_ENV_HOME = "HOME"
QR_ENV_LANG = "LANG"
QR_ENV_LC_ALL = "LC_ALL"
QR_ENV_PYTHONPATH = "PYTHONPATH"
QR_ENV_API_BEARER_TOKEN = "QUICKROBOT_API_BEARER_TOKEN"
QR_ENV_API_HOST = "QUICKROBOT_API_HOST"
QR_ENV_API_PORT = "QUICKROBOT_API_PORT"
QR_ENV_WEBUI_HOST = "QUICKROBOT_WEBUI_HOST"
QR_ENV_WEBUI_PORT = "QUICKROBOT_WEBUI_PORT"
QR_ENV_MCP_HOST = "QUICKROBOT_MCP_HOST"
QR_ENV_MCP_PORT = "QUICKROBOT_MCP_PORT"
QR_ENV_MCP_ALLOWED_HOSTS = "QUICKROBOT_MCP_ALLOWED_HOSTS"
QR_ENV_MCP_DISABLE_DNS_REBINDING = "QUICKROBOT_MCP_DISABLE_DNS_REBINDING"
QR_ENV_MCP_ALLOW_READS = "MCP_ALLOW_READS"
QR_ENV_MCP_ALLOW_WRITES = "MCP_ALLOW_WRITES"
QR_ENV_MCP_ALLOW_PROXY = "MCP_ALLOW_PROXY"

# ── Global status indicator colors (RGB tuples) ──────────────────────
# Used by api_app_status() to compute the nav indicator color.
# Edit these to adjust colors without touching JS code.
# Future: host-to-color mapping, user override via DB/seed.
QR_STATUS_COLORS = {
    "idle":      (255, 255, 255),   # white — no instances on active hosts
    "running":   (0,   230, 118),   # cyber-green — all running cleanly
    "error":     (244, 67,  54),    # red — any error state
    "stopped":   (255, 193, 7),     # yellow/amber — stopped or in-progress
}
