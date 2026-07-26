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

"""Runtime configuration defaults — unrelated to engine definitions.

Engine IDs, names, ports, and bind host are defined in lib/qr_engine_ids.py.
This module contains only operational defaults used throughout the codebase.
"""

# Base directory for playbook files (relative to project root)
# Used as fallback when QUICKROBOT_API_PLAYBOOKDIR not set in .quickrobot.env
UE_PLAYBOOK_ROOT_DIR = "playbooks/"

# Default polling intervals (seconds) — used when engine_configs not available
POLLING_INTERVAL_LOCAL_SEC = 10      # 10s minimum for localhost instances
POLLING_INTERVAL_REMOTE_SEC = 600    # 10m for remote instances

# Console debug level — numeric, 0 = off, >= 10 = full debug output
# (legacy single value; replaced by per-engine QUICKROBOT_<ENGINE>_LOG_LEVEL)
QUICKROBOT_CONSOLE_DEBUG_LEVEL = 10

# Per-engine log level defaults — LOG-CONSOLIDATE Phase 2
# Numeric: >= 10 = DEBUG, < 10 = WARNING (quiet production).
# Each engine reads its own key; falls back to QUICKROBOT_CONSOLE_DEBUG_LEVEL if absent.
QUICKROBOT_SCHEDULER_LOG_LEVEL = 10    # debug level for scheduler
QUICKROBOT_MCP_LOG_LEVEL = 0           # quiet production for MCP
QUICKROBOT_WEBUI_LOG_LEVEL = 0         # quiet production for WebUI
QUICKROBOT_API_LOG_LEVEL = 10          # debug level for API server

# Ansible action log level — controls what gets persisted to ansible_actions table.
# "errors" = only failures/timeouts (default, keeps table lean).
# "warnings" = errors + non-failed changes (apt updates, config-only deploys).
# "all" = every action logged.
QUICKROBOT_ANSIBLE_LOG_LEVEL = "errors"

# Minimum log level per action type — used as fallback when env not configured.
# If QUICKROBOT_ANSIBLE_LOG_LEVEL="warnings", only actions with level <= "warnings" are logged.
ANSIBLE_LOG_LEVELS = {
    # Critical operations — always log
    "validate_node": "errors",
    "discover_node": "errors",
    "scan_models": "errors",
    "apt_update": "warnings",
    "apt_upgrade": "warnings",
    "apt_update_upgrade": "warnings",
    "reboot_node": "all",
    "shutdown_node": "all",
    # Instance lifecycle
    "deploy_instance": "warnings",
    "undeploy_instance": "warnings",
    "rebuild": "warnings",
    "update_and_compile": "warnings",
    "restart_instance": "errors",
    "stop_instance": "errors",
    # Config/infra
    "ansible_execute": "errors",
    "config_change": "warnings",
    "get_logs": "errors",
}

# Debug level for ansible_runner — deprecated, use QUICKROBOT_CONSOLE_DEBUG_LEVEL instead
QUICKROBOT_DEBUG_LEVEL = 10

# Default playbook execution timeout in seconds (3600s = 1 hour).
# Can be overridden per-playbook via # @timeout: N comment at top of YAML file.
QUICKROBOT_PLAYBOOK_TIMEOUT = 3600

# Default timeout for short local shell commands (e.g., hardware info gathering).
QUICKROBOT_LOCAL_CMD_TIMEOUT = 10

# ── SSH connection settings — flow: .env → qr_engine_ids fallbacks ────
# Real SOT: .quickrobot.env keys (QUICKROBOT_SSH_STRICT_HOST_KEY_CHECKING / QUICKROBOT_SSH_CONNECT_TIMEOUT)
# Fallback defaults (from qr_engine_ids): "accept-new", 5s
from lib.qr_engine_ids import (
    _QR_SSH_STRICT_HOST_KEY_CHECKING_FALLBACK,
    _QR_SSH_CONNECT_TIMEOUT_FALLBACK,
)
SSH_STRICT_HOST_KEY_CHECKING = _QR_SSH_STRICT_HOST_KEY_CHECKING_FALLBACK
SSH_CONNECT_TIMEOUT = _QR_SSH_CONNECT_TIMEOUT_FALLBACK

# ── Re-exports from qr_engine_ids.py (backward-compat import paths) ────
import getpass as _getpass
from lib.qr_engine_ids import QR_FORBIDDEN_HOSTS, QR_DEFAULT_LOCALHOST

# Default bind address for services that need a localhost fallback
# Real SOT: QR_DEFAULT_LOCALHOST in lib/qr_engine_ids.py
QR_DEFAULT_BIND_HOST = QR_DEFAULT_LOCALHOST

# Default timezone when DB config is unavailable
DEFAULT_TIMEZONE = "Europe/Berlin"

DEFAULT_ANSIBLE_USER = _getpass.getuser()

# Backward-compat alias — original name for existing imports across the codebase.
# Real SOT: QUICKROBOT_VERSION in lib/qr_engine_ids.py
VERSION = __import__("lib.qr_engine_ids", fromlist=["QUICKROBOT_VERSION"]).QUICKROBOT_VERSION
