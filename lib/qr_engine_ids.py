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
QUICKROBOT_VERSION = "v0.07"

# ── Default bind host (localhost loopback) ────────────────────────────
# SSOT for "127.0.0.1" fallbacks across the codebase.
# Use this constant instead of hardcoding "127.0.0.1" in any fallback.
QR_DEFAULT_LOCALHOST = "127.0.0.1"

# ── Forbidden bind hosts (system engines reject these) ───────────────
# If a system engine (.env or startup) binds to any of these, it fails.
# 0.0.0.0 is NOT a default — it means "all interfaces" and opens the
# service to the entire network. Must be explicit config, never fallback.
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
    (4,  "quickrobot-scheduler", "system"),
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
QR_ENGINE_API       = QR_ENGINE_QUICKROBOT_API
QR_ENGINE_WEBUI     = QR_ENGINE_QUICKROBOT_WEBUI
QR_ENGINE_MCP       = QR_ENGINE_QUICKROBOT_MCP
QR_ENGINE_SCHEDULER = QR_ENGINE_QUICKROBOT_SCHEDULER

# -- String names (for engine_type_name DB comparisons) ---------------------
QR_ENGINE_API_NAME                 = "quickrobot-api"
QR_ENGINE_WEBUI_NAME               = "quickrobot-webui"
QR_ENGINE_MCP_NAME                 = "quickrobot-mcp"
QR_ENGINE_LLAMA_SERVER_NAME        = "llama_server"
QR_ENGINE_LLAMA_RPC_NAME           = "llama_rpc"
QR_ENGINE_UNIVERSAL_NAME           = "universal"
QR_ENGINE_IPERF3_NAME              = "iperf3"
QR_ENGINE_SUBPROCESS_NAME          = "subprocess"
QR_ENGINE_SCHEDULER_NAME           = "quickrobot-scheduler"

# -- Auto-generated maps ----------------------------------------------------
# id -> name (e.g., 21 -> "llama_server", 22 -> "llama_rpc")
QR_ENGINE_NAME_MAP = {_id: name for _id, name, _cat in _QR_ENGINES}
# name -> id (e.g., "llama_server" -> 21)
QR_ENGINE_ID_MAP   = {name: _id for _id, name, _cat in _QR_ENGINES}
# id -> category
QR_ENGINE_CAT_MAP  = {_id: cat  for _id, name, cat in _QR_ENGINES}

# Category membership tuples (for fast "in" checks without function call)
QR_SYSTEM_IDS = tuple(_id for _id, _, c in _QR_ENGINES if c == "system")           # (1, 2, 3, 4)
QR_LLAMACPP_IDS = tuple(_id for _id, _, c in _QR_ENGINES if c == "llamacpp")      # (21, 22)
QR_INFRA_IDS = tuple(_id for _id, _, c in _QR_ENGINES if c == "infra")            # (11, 12, 31)

# System engine name tuples (for suffix logic, etc.)
_QR_SYSTEM_NAMES = tuple(name for _, name, cat in _QR_ENGINES if cat == "system")  # ("quickrobot-api", ...)

# Engine name aliases: map non-canonical names to the canonical hyphen form.
# The ID_MAP uses the hyphen form as keys, so aliases must resolve to it.
_QR_NAME_ALIASES = {}
for _name in (_n for _, _n, _ in _QR_ENGINES):
    # Map underscore variant -> canonical hyphen name
    _underscore = _name.replace("-", "_")
    if _underscore != _name:
        _QR_NAME_ALIASES[_underscore] = _name

# ── WebUI Nav Menu Constants (SSOT for nav rendering) ────────────────
# Short alias names to skip in nav (redirects to canonical route).
_QR_NAV_SHORT_ALIASES = ("qr_api", "qr_webui", "qr_mcp", "rpc")

# Nav display name overrides: engine_name → human-readable label.
# Add entries here when engine display should differ from DB display_name.
_QR_NAV_DISPLAY_NAMES = {
    "quickrobot-api":  "API Service",
    "quickrobot-webui": "Web UI Service",
    "quickrobot-scheduler": "Scheduler",
    "quickrobot-mcp":   "MCP Service",
    "subprocess":        "Subprocess",
}

# Short display names for instance list VIEW ENGINE column.
# Only used in WebUI instance table — not nav, not DB, not API status.
_QR_INST_LIST_SHORT_NAMES = {
    "quickrobot-api":   "QR-API",
    "quickrobot-webui": "QR-WebUI",
    "quickrobot-scheduler": "QR-Sched",
    "quickrobot-mcp":   "QR-MCP",
}

# LLaMA section overrides: engine_name → (display_name, suffix).
# "__EMPTY__" sentinel: empty-string display/suffix (avoids OR-chain falsy skip).
_QR_EMPTY = "__EMPTY__"
_QR_NAV_LLAMA_NAMES = {
    "llama_server": (_QR_EMPTY, "Config"),
    "llama_rpc":    ("RPC", _QR_EMPTY),
}

# Engines without a per-instance config nav item.
# Merged pages (rpccluster, iperf3) or per-instance only (subprocess).
_QR_NAV_NO_CONFIG = {"subprocess", "universal", "iperf3", "llama_rpc"}

# Nav section assignment: engine_name → section key.
# Engines not in this map go to "System" (default_section).
_QR_NAV_SECTION_MAP = {
    "llama_server": "llama",
    "llama_rpc":    "llama",  # rpc alias handled separately
    "iperf3":        "misc",
    "subprocess":    "misc",
}

# -- Port defaults (engine-specific, NOT generic QR_DEFAULT_* names) --------
QR_ENGINE_PORT_DEFAULTS = {
    "quickrobot-api":    8040,
    "quickrobot-webui":  8041,
    "quickrobot-mcp":    8042,
    "llama_server":      8080,
    "llama_rpc":         9000,
    "iperf3":            9900,
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


# ── System instance ID map (short alias → DB instance ID) ─────────────
# Maps engine short names (webui/mcp/scheduler/api) to their system-managed
# instance IDs. Used by lifecycle functions in lib_system_engine.py.
_SYSTEM_INST_IDS = {
    "api": QR_ENGINE_API,            # 1
    "webui": QR_ENGINE_WEBUI,        # 2
    "mcp": QR_ENGINE_MCP,            # 3
    "scheduler": QR_ENGINE_SCHEDULER, # 4
}


def get_system_instance_id(engine_name):
    """Get system-managed instance DB ID from engine name.
    
    Accepts BOTH short names ("mcp", "webui") AND long names ("quickrobot-mcp", "quickrobot-webui").
    
    Args:
        engine_name: Engine name (short or long form).
    
    Returns:
        Integer instance DB ID, or None if not a known system engine.
    """
    # Try direct lookup first (handles short names)
    result = _SYSTEM_INST_IDS.get(engine_name)
    if result is not None:
        return result
    
    # Handle long names by extracting short form
    # QR_ENGINE_*_NAME constants are like "quickrobot-api", "quickrobot-webui", etc.
    _short_prefix = "quickrobot-"
    if engine_name.startswith(_short_prefix):
        short_name = engine_name[len(_short_prefix):]
        return _SYSTEM_INST_IDS.get(short_name)
    
    return None


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
QR_ENV_MCP_ALLOWED_HOSTS = "QUICKROBOT_MCP_ALLOWED_HOSTS"  # deprecated — ALLOWED_HOSTS collapsed to static defaults
QR_ENV_MCP_DISABLE_DNS_REBINDING = "QUICKROBOT_MCP_DISABLE_DNS_REBINDING"
QR_ENV_MCP_CORS_ORIGINS = "QUICKROBOT_MCP_CORS_ORIGINS"

# MCP subprocess defaults (SSOT — used by health check, startup pipeline)
QR_MCP_DEFAULT_READS = "false"
QR_MCP_DEFAULT_WRITES = "false"
QR_MCP_DEFAULT_PROXY = "false"
QR_MCP_DEFAULT_AUTOSTART = "false"

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

# ── Staged Chain Constants (SSOT for lib_runner.py) ──────────────────
# All job types, stage names, stage→state mappings, and skipable stages
# are defined here so they're never duplicated across the codebase.

# Known job types — used in jobs.job_type CHECK constraint + scheduler
_QR_JOB_TYPES = (
    "deploy", "rebuild", "reconfigure", "deploy_fast", "undeploy",
    "bind", "unbind", "start", "restart", "stop", "reboot",
    "apt_update", "apt_upgrade", "apt_update_upgrade",
)

# Job type constants for comparison (same strings, named constants)
QR_JOB_DEPLOY      = "deploy"
QR_JOB_REBUILD     = "rebuild"
QR_JOB_RECONFIGURE = "reconfigure"
QR_JOB_DEPLOY_FAST = "deploy_fast"
QR_JOB_UNDEPLOY    = "undeploy"
QR_JOB_BIND        = "bind"
QR_JOB_UNBIND      = "unbind"
QR_JOB_START       = "start"
QR_JOB_RESTART     = "restart"
QR_JOB_STOP        = "stop"
QR_JOB_REBOOT      = "reboot"
QR_JOB_APT_UPDATE  = "apt_update"
QR_JOB_APT_UPGRADE = "apt_upgrade"
QR_JOB_APT_ALL     = "apt_update_upgrade"


# Stage name constants
QR_STAGE_PREFLIGHT = "preflight"
QR_STAGE_DEPS      = "deps"
QR_STAGE_SOURCE    = "source"
QR_STAGE_COMPILE   = "compile"
QR_STAGE_CONFIG_SVC = "config_svc"
QR_STAGE_CONFIG_ENV = "config_env"
QR_STAGE_START     = "start"
QR_STAGE_STOP      = "stop"
QR_STAGE_HEALTH_PROBE = "health_probe"

# Stage → instance state mapping (what instances.state becomes while a task runs).
# During deploy/rebuild chains, instance.state stays "deploying" through all stages
# except start (→"running") and stop (→"stopped"). This prevents the misleading
# transition from "deploying" back to "configuring" during config_svc/config_env.
_QR_STAGE_STATES = {
    QR_STAGE_PREFLIGHT:  "deploying",
    QR_STAGE_DEPS:       "deploying",
    QR_STAGE_SOURCE:     "deploying",
    QR_STAGE_COMPILE:    "deploying",
    QR_STAGE_CONFIG_SVC: "deploying",
    QR_STAGE_CONFIG_ENV: "deploying",
    QR_STAGE_STOP:       "stopped",
    QR_STAGE_START:      "running",
}

# Stages that can be skipped when binary already exists (source, compile)
_QR_SKIPABLE_STAGES = {QR_STAGE_SOURCE, QR_STAGE_COMPILE}

# Job type → final instance state mapping (SSOT for _finalize_job).
# bind/unbind are NOT in this dict — they preserve pre-operation state.
_QR_JOB_FINAL_STATES = {
    "deploy":        "running",          # chain includes start stage, full deploy
    "rebuild":       "running",          # chain includes start stage, post-compile
    "stop":          "stopped",
    "undeploy":      "unconfigured",
    "start":         "loading",          # triggers SSE model-load progress bar
    "restart":       "loading",          # triggers SSE model-load progress bar
    "reconfigure":   "running",          # config-only, no model reload needed
    "deploy_fast":   "running",          # config_svc + config_env + start, no source/compile
    "apt_update":    "running",          # node-level apt update
    "apt_upgrade":   "running",          # node-level apt upgrade
    "apt_update_upgrade": "running",     # combined apt update + upgrade chain
}


# ── Undeploy stage chains (per engine type) ─────────────────────────
# Engine-specific undeploy chains used by runner.chain(job_type="undeploy").
# Each chain stops the service, runs engine-specific cleanup, then verifies.
_QR_UNDEPLOY_CHAINS = {
    QR_ENGINE_LLAMA_SERVER_NAME: [
        {"stage": "stop",          "playbook": "service_stop"},
        {"stage": "undeploy",      "playbook": "undeploy_llama_server"},
        {"stage": "verify",        "playbook": "check_undeploy"},
    ],
    QR_ENGINE_LLAMA_RPC_NAME: [
        {"stage": "stop",          "playbook": "service_stop"},
        {"stage": "undeploy",      "playbook": "undeploy_rpc"},
        {"stage": "verify",        "playbook": "check_undeploy"},
    ],
    QR_ENGINE_IPERF3_NAME: [
        {"stage": "stop",          "playbook": "service_stop"},
        {"stage": "undeploy",      "playbook": "undeploy_iperf3"},
        {"stage": "verify",        "playbook": "check_undeploy"},
    ],
}


# ── Stage timeout defaults (SSOT for playbook header fallbacks) ─────
# Playbook headers (# @timeout:) override these. These are the fallback
# defaults when a playbook has no timeout header.
QR_TIMEOUT_COMPILE    = 1800  # 30 min for cmake build
QR_TIMEOUT_SOURCE     = 600   # 10 min for git clone
QR_TIMEOUT_DEFAULT    = 300   # 5 min default for other stages
QR_SSH_PORT_DEFAULT   = 22    # Default SSH port for node connections

# Model path placeholder values treated as empty/omitted in merge chain
_QR_MODEL_PATH_PLACEHOLDERS = frozenset(("none", "yes", "no", "true", "false"))

# ── Backward-compatible exports ─────────────────────────────────────
# These mirror the old lib_runner.py constant names for import compatibility.
STAGE_STATE_MAP = _QR_STAGE_STATES
SKIPABLE_STAGES = _QR_SKIPABLE_STAGES
JOB_FINAL_STATES = _QR_JOB_FINAL_STATES
