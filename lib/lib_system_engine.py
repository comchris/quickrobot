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

"""Quickrobot — System engine process management.

Single entry point for starting, stopping, and restarting
system-managed subprocesses (WebUI, MCP). Reads config from
.quickrobot.env in CWD.
"""

import os
import re
import signal as _signal
import subprocess
import sys
import threading as _threading
import time
import logging

logger = logging.getLogger(__name__)

from lib.qr_engine_ids import (
    QR_DEFAULT_LOCALHOST, get_port_default, get_name_by_id,
    QR_FORBIDDEN_HOSTS, QR_ENGINE_PORT_DEFAULTS, get_system_instance_id,
    QR_HEALTH_CHECK_SLEEP, QR_ENGINE_SUBPROCESS,
    QR_STATE_DEPLOYED,
    QR_WEBUI_RESTART_ADOPT, QR_MCP_RESTART_ADOPT, QR_SCHEDULER_RESTART_ADOPT,
)
from lib.lib_time import utcnow_str

# Build env-var name -> default from SOT (avoids duplicating port values)
_ENV_PORT_DEFAULTS = {
    "QUICKROBOT_API_PORT":   QR_ENGINE_PORT_DEFAULTS["quickrobot-api"],
    "QUICKROBOT_WEBUI_PORT": QR_ENGINE_PORT_DEFAULTS["quickrobot-webui"],
    "QUICKROBOT_MCP_PORT":   QR_ENGINE_PORT_DEFAULTS["quickrobot-mcp"],
}

# ── Log path helper — unified logging for all system engines ───────────
_LOG_DIR = "logs"


def get_engine_log_path(engine_name):
    """Get the log file path for a system engine.

    Args:
        engine_name: "webui", "mcp", or "scheduler"

    Returns:
        str: Absolute path to the engine's log file
    """
    return os.path.join(os.getcwd(), _LOG_DIR, f"{engine_name}.log")


# ── Log rotation (vC): truncate on startup if > MAX_LOG_SIZE ──────────
_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB


def rotate_log_if_needed(log_path, engine_name="engine"):
    """Rotate a log file: if size exceeds MAX_LOG_BYTES, truncate to 0 bytes.

    Called once per engine startup. Logs the action to stderr for visibility.
    Returns True if rotation occurred, False otherwise.
    """
    try:
        if not os.path.exists(log_path):
            return False
        size = os.path.getsize(log_path)
        if size > _MAX_LOG_BYTES:
            with open(log_path, "w") as f:
                pass  # truncate
            logger.info("[qr-system] %s log rotated (%dB → 0B)", engine_name, size)
            return True
    except OSError as _e:
        logger.warning("[qr-system] %s log rotation check failed: %s", engine_name, _e)
    return False


# ── Child PID tracking for process group + signal cleanup ─────────────
_CHILD_PIDS = set()
_CHILD_PID_LOCK = _threading.Lock()

def _register_child(pid):
    """Register a child PID for cleanup on shutdown."""
    with _CHILD_PID_LOCK:
        _CHILD_PIDS.add(pid)

def _cleanup_children():
    """Kill all tracked child processes in their own process groups."""
    with _CHILD_PID_LOCK:
        pids = list(_CHILD_PIDS)
    for pid in pids:
        try:
            os.killpg(pid, _signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

def _install_signal_handlers():
    """Install signal handlers to clean up child processes on shutdown."""
    try:
        _signal.signal(_signal.SIGTERM, lambda s, f: (_cleanup_children(), sys.exit(0)))
        _signal.signal(_signal.SIGINT, lambda s, f: (_cleanup_children(), sys.exit(0)))
    except (OSError, ValueError):
        # Signal handling might fail in non-main thread or Windows
        pass

# ── Port conflict safety check ────────────────────────────────────────

def check_and_free_port(port, service_name):
    """Check if port is in use by a stale process. Kill it if found.

    Uses `ss -tlnp` to find processes listening on the given port.
    If found and identified as orphaned (PPID=1 or zombie), kills it.

    Args:
        port: Integer port number to check.
        service_name: Display name for logging (e.g., "webui", "mcp").

    Returns:
        True if port is free (or was successfully freed).
        Returns False if port is in use and couldn't be killed.
    """
    if port is None or port == 0:
        return True  # Scheduler doesn't bind a port

    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5
        )
        lines = [
            l for l in result.stdout.splitlines()
            if f":{port}" in l and "LISTEN" in l
        ]
        if lines:
            for line in lines:
                pid_match = re.search(r"pid=(\d+)", line)
                if pid_match:
                    stale_pid = int(pid_match.group(1))
                    try:
                        import psutil
                        proc = psutil.Process(stale_pid)
                        ppid = proc.ppid()
                        if ppid == 1 or proc.status() in ("zombie",):
                            logger.warning("[qr-system] Port %d used by stale %s process (pid=%d, PPID=%d). Killing.",
                                          port, service_name, stale_pid, ppid)
                            proc.terminate()
                            # Wait briefly, force kill if needed
                            for _ in range(10):
                                try:
                                    if not psutil.pid_exists(stale_pid) \
                                            or psutil.Process(
                                                stale_pid
                                            ).status() == "zombie":
                                        break
                                except Exception as _e:
                                    logger.warning("[qr-system] Orphan kill loop error (pid=%d): %s", stale_pid, _e)
                                    break
                                time.sleep(0.1)
                            if psutil.pid_exists(stale_pid):
                                proc.kill()
                            # Deregister if it was our tracked child
                            with _CHILD_PID_LOCK:
                                _CHILD_PIDS.discard(stale_pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
    except FileNotFoundError:
        # ss not available, skip check
        pass
    return True  # Best effort — don't block startup


# ---------------------------------------------------------------------------
# Port + process pre-flight scanner (designed for startup "force respawn")
# ---------------------------------------------------------------------------

_ENGINE_SCAN_PATTERNS = {
    "api": {"port": QR_ENGINE_PORT_DEFAULTS["quickrobot-api"], "patterns": ["quickrobot.py"]},
    "webui": {"port": QR_ENGINE_PORT_DEFAULTS["quickrobot-webui"], "patterns": ["quickrobot_webui.py"]},
    "mcp": {"port": QR_ENGINE_PORT_DEFAULTS["quickrobot-mcp"], "patterns": ["qr_mcp_server.py"]},
    "scheduler": {"port": None, "patterns": ["quickrobot_scheduler", "engine.quickrobot_scheduler"]},
}


def check_port_and_process_free(engine_name, port=None):
    """Pre-flight check: verify port is free AND no stale process exists.

    Used during API startup to detect any existing system engine processes
    before attempting a fresh start. Reports all findings and exits.

    Args:
        engine_name: "webui", "mcp", or "scheduler"
        port: Optional explicit port (falls back to _ENGINE_SCAN_PATTERNS)

    Returns:
        {"free": bool, "issues": list[str]}
        free=False means at least one conflict detected.
    """
    # Resolve port from scan patterns if not provided
    if port is None and engine_name in _ENGINE_SCAN_PATTERNS:
        port = _ENGINE_SCAN_PATTERNS[engine_name].get("port")

    issues = []
    import subprocess as _subp

    # Skip pre-flight for engines with RESTART_ADOPT=true — the engine's
    # execute() method will handle orphan adoption instead.
    adopt_env = os.getenv(f"QUICKROBOT_{engine_name.upper()}_RESTART_ADOPT")
    if adopt_env is None:
        _adopt_map = {"webui": QR_WEBUI_RESTART_ADOPT,
                      "mcp": QR_MCP_RESTART_ADOPT,
                      "scheduler": QR_SCHEDULER_RESTART_ADOPT}
        adopt_env = _adopt_map.get(engine_name, "false")
    if adopt_env.lower() in ("true", "1"):
        return {"free": True, "issues": []}

    # 1. Port check (skip for scheduler — no port)
    if port is not None and port > 0:
        try:
            result = _subp.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTEN" in line:
                    pid_match = re.search(r"pid=(\d+)", line)
                    comm_match = re.search(r'"([^"]+)"', line)
                    pid_str = f" pid={pid_match.group(1)}" if pid_match else ""
                    comm_str = comm_match.group(1) if comm_match else "(unknown)"
                    issues.append(f"Port {port} occupied by {comm_str}{pid_str}")
        except FileNotFoundError:
            pass  # ss not available, skip port check

    # 2. Process scan via ps aux (grep for known patterns)
    if engine_name in _ENGINE_SCAN_PATTERNS:
        patterns = _ENGINE_SCAN_PATTERNS[engine_name].get("patterns", [])
        try:
            result = _subp.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
            my_pid = os.getpid()
            for line in result.stdout.splitlines():
                # Skip the ps aux command itself and this function's grep subprocess
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    line_pid = int(parts[1])
                except ValueError:
                    continue
                if line_pid == my_pid:
                    continue
                for pattern in patterns:
                    if pattern in line and "ps aux" not in line.split()[:3]:
                        # Format: USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND...
                        cmd = " ".join(parts[10:]) if len(parts) > 10 else line
                        issues.append(f"Stale process found: pid={line_pid} cmd={cmd!r}")
                        break
        except FileNotFoundError:
            pass

    return {"free": len(issues) == 0, "issues": issues}


def _mcp_binary_exists():
    """Check if the MCP pipx venv exists and can import fastmcp.

    The MCP server runs via a pipx-installed Python environment. This function
    verifies the pipx venv exists and has the mcp SDK (with fastmcp submodule)
    available. Returns True if all checks pass.

    Checks (in priority order):
    1) engine_configs 'binary_path' for engine_type_id=3 (explicit override)
    2) engine_configs 'mcp_python_interpreter' (pipx venv python path)
    3) Default pipx MCP venv: ~/.local/share/pipx/venvs/mcp/bin/python

    Returns:
        True if the MCP runtime environment is available, False otherwise.
    """
    candidates = []
    try:
        from db.sqlite import pool as _pool
        from qr_api import _CONFIG
        with _pool(_CONFIG.get("db_path", "data/quickrobot.db")) as conn:
            # Check binary_path override
            row = conn.execute(
                "SELECT value FROM engine_configs WHERE engine_type_id = (SELECT id FROM engine_types WHERE name='quickrobot-mcp') AND key = 'binary_path'"
            ).fetchone()
            if row and row["value"]:
                candidates.insert(0, row["value"])
            # Check mcp_python_interpreter (pipx venv python path)
            row2 = conn.execute(
                "SELECT value FROM engine_configs WHERE engine_type_id = (SELECT id FROM engine_types WHERE name='quickrobot-mcp') AND key = 'mcp_python_interpreter'"
            ).fetchone()
            if row2 and row2["value"]:
                interp = str(row2["value"]).strip()
                candidates.insert(0, interp)
    except Exception as _e:
        logger.warning("[qr-system] MCP binary path lookup failed (using defaults): %s", _e)

    # Default pipx MCP venv python
    default_python = os.path.expanduser("~/.local/share/pipx/venvs/mcp/bin/python")
    candidates.append(default_python)

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            # Verify it can import the fastmcp module
            try:
                import subprocess as _subp
                result = _subp.run([path, "-c", "import mcp.server.fastmcp"],
                                   capture_output=True, timeout=5)
                if result.returncode == 0:
                    return True
            except Exception as _e:
                logger.debug("python3 interpreter verify failed for %s: %s", path, _e)
                pass
    return False


def load_env_config(cwd=None):
    """Parse .quickrobot.env from cwd. Returns dict of all keys.

    Args:
        cwd: Working directory to look for env file (default: os.getcwd()).

    Returns:
        Dict with all parsed key=value pairs.

    Raises:
        FileNotFoundError: If .quickrobot.env doesn't exist.
    """
    if cwd is None:
        cwd = os.getcwd()

    env_path = os.path.join(cwd, ".quickrobot.env")

    if not os.path.isfile(env_path):
        raise FileNotFoundError(
            f".quickrobot.env not found in {cwd}. "
            f"Create a .quickrobot.env file with keys: QUICKROBOT_API_HOST, "
            f"QUICKROBOT_API_PORT, QUICKROBOT_WEBUI_HOST, QUICKROBOT_WEBUI_PORT, "
            f"QUICKROBOT_MCP_HOST, QUICKROBOT_MCP_PORT"
        )

    # Track line numbers for error reporting
    _key_line_map = {}
    config = {}
    with open(env_path, "r") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            # Split on first '='
            if "=" not in line:
                logger.warning("[qr-system] .quickrobot.env line %d: no '=' found, skipping: %s", line_no, raw_line.rstrip())
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
                value = value[1:-1]
            config[key] = value
            _key_line_map[key] = line_no

    _validate_env_config(config, _key_line_map)
    return config


def _normalize_bool(value):
    """Normalize string to 'true'/'false'. Returns None if unrecognizable.

    Args:
        value: String value from env config.

    Returns:
        "true" or "false" if recognizable, None otherwise.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("true", "yes", "1"):
        return "true"
    if v in ("false", "no", "0"):
        return "false"
    return None


def _validate_env_config(config, key_line_map):
    """Validate all QUICKROBOT_* keys in config. Exits on error.

    Required keys must exist and be non-empty. Ports must be integers 1-65535.
    Boolean keys are normalized to "true"/"false". Unknown keys pass through.

    Args:
        config: Dict from load_env_config()
        key_line_map: Dict mapping keys to their line numbers in .quickrobot.env

    Raises:
        SystemExit on validation failure (with explicit error message).
    """
    # Required string keys
    for key in ("QUICKROBOT_API_HOST", "QUICKROBOT_WEBUI_HOST", "QUICKROBOT_MCP_HOST"):
        if key not in config or not config[key]:
            line = key_line_map.get(key, "?")
            logger.error("[qr-system] %s is required but missing (line %s)", key, line)
            sys.exit(1)

    # Required int keys (ports) — defaults from SOT QR_ENGINE_PORT_DEFAULTS
    for key, default in _ENV_PORT_DEFAULTS.items():
        if key not in config:
            continue  # Will use default at runtime
        val = config[key]
        line = key_line_map.get(key, "?")
        try:
            n = int(val)
            if n < 1 or n > 65535:
                logger.error("[qr-system] %s=%s — expected integer 1-65535, got %d (line %s)", key, val, n, line)
                sys.exit(1)
        except ValueError:
            logger.error("[qr-system] %s=%s — expected integer, got '%s' (line %s)", key, val, val, line)
            sys.exit(1)

    # Optional int keys
    int_keys = {
        "QUICKROBOT_CONSOLE_DEBUG_LEVEL": 0,     # min=0
        "QUICKROBOT_API_PING_INTERVAL": 1,       # min=1
        "QUICKROBOT_SERVER_SPAWN_TIMEOUT": 1,    # min=1
    }
    for key, min_val in int_keys.items():
        if key not in config:
            continue
        val = config[key]
        line = key_line_map.get(key, "?")
        try:
            n = int(val)
            if n < min_val:
                logger.error("[qr-system] %s=%s — expected >= %d, got %d (line %s)", key, val, min_val, n, line)
                sys.exit(1)
        except ValueError:
            logger.error("[qr-system] %s=%s — expected integer, got '%s' (line %s)", key, val, val, line)
            sys.exit(1)

    # Optional string keys with allowed values
    ansible_level = config.get("QUICKROBOT_ANSIBLE_LOG_LEVEL")
    if ansible_level is not None:
        line = key_line_map.get("QUICKROBOT_ANSIBLE_LOG_LEVEL", "?")
        if ansible_level not in ("errors", "warnings", "all"):
            logger.error("[qr-system] QUICKROBOT_ANSIBLE_LOG_LEVEL=%s — expected one of: errors, warnings, all (line %s)", ansible_level, line)
            sys.exit(1)

    # Optional bool keys — normalize in-place
    bool_keys = [
        "QUICKROBOT_WEBUI_AUTOSTART",
        "QUICKROBOT_MCP_AUTOSTART",
        "QUICKROBOT_MCP_READ",
        "QUICKROBOT_MCP_WRITE",
        "QUICKROBOT_MCP_FULLPROXY",
        "QUICKROBOT_MCP_DISABLE_DNS_REBINDING",
    ]
    for key in bool_keys:
        if key not in config:
            continue
        val = config[key]
        line = key_line_map.get(key, "?")
        normalized = _normalize_bool(val)
        if normalized is None:
            logger.error("[qr-system] %s=%s — expected true/false/yes/no/1/0 (line %s)", key, val, line)
            sys.exit(1)
        config[key] = normalized

    # Seed checksum keys — required for --init mode integrity
    for key in ("QUICKROBOT_SEED_CHECKSUM", "QUICKROBOT_SEED_FILESIZE"):
        if key not in config or not config[key]:
            line = key_line_map.get(key, "?")
            logger.error("[qr-system] %s is required but missing (line %s)", key, line)
            sys.exit(1)


def _parse_ipv6_host(host_str):
    """Parse an IPv4 or IPv6 host string.

    Handles bracket notation for IPv6 addresses: [::1], [fe80::1%eth0].

    Args:
        host_str: Host string, may include brackets for IPv6.

    Returns:
            Tuple (host, is_ipv6) where is_ipv6 indicates whether the host
            uses IPv6 (has brackets).
    """
    if not host_str:
        return (QR_DEFAULT_LOCALHOST, False)

    host_str = host_str.strip()

    # Check for IPv6 bracket notation
    if host_str.startswith("["):
        end_bracket = host_str.find("]")
        if end_bracket > 0:
            return (host_str[1:end_bracket], True)
        # Malformed — return as-is without brackets
        return (host_str.strip("[]"), False)

    return (host_str, False)


def _build_command(engine_name, env_config, api_host, api_port, extra_flags=None):
    """Build the subprocess command line for a system engine.

    Args:
        engine_name: "webui" or "mcp"
        env_config: Dict from load_env_config()
        api_host: API server bind host (from _CONFIG.host)
        api_port: API server bind port (from _CONFIG["api_port"])
        extra_flags: Optional list of engine-specific CLI flags

    Returns:
        List of command arguments suitable for subprocess.Popen()
    """
    if engine_name == "webui":
        host = env_config["QUICKROBOT_WEBUI_HOST"]
        port = env_config.get("QUICKROBOT_WEBUI_PORT") or str(get_port_default("quickrobot-webui"))

        webui_path = os.path.join(os.getcwd(), "quickrobot_webui.py")

        # --host and --port are required by WebUI startup validation (_check_webui_args)
        cmd = [
            sys.executable, webui_path,
            "--host", host,
            "--port", str(port),
        ]
        return cmd

    elif engine_name == "mcp":
        host = env_config["QUICKROBOT_MCP_HOST"]
        port = env_config.get("QUICKROBOT_MCP_PORT") or str(get_port_default("quickrobot-mcp"))
        # Strict host check — must not be a forbidden wildcard
        if host in QR_FORBIDDEN_HOSTS:
            logger.error("[qr-system] FATAL: MCP bind host is '%s' — %s", host, QR_FORBIDDEN_HOSTS)
            sys.exit(1)

        mcp_server_path = os.path.join(os.getcwd(), "engine", "qr_mcp_server.py")

        # No CLI args needed — MCP reads everything from env (QUICKROBOT_MCP_* / QUICKROBOT_API_*)
        cmd = [
            sys.executable, mcp_server_path,
        ]
        if extra_flags:
            cmd.extend(extra_flags)
        return cmd

    elif engine_name == "scheduler":
        # API-spawned: no --interval flag; scheduler reads poll_interval from DB config.
        # Standalone usage: pass --interval CLI arg to override.
        cmd = [
            sys.executable, "-m", "engine.quickrobot_scheduler",
            "--db", os.path.join(os.getcwd(), "data", "quickrobot.db"),
        ]
        return cmd

    raise ValueError(f"Unknown engine_name: {engine_name}")


def _log_lifecycle(engine_name, action, details=None):
    """Log a lifecycle event to logs/system_engine.log.

    Also writes to ansible_actions table for audit trail.

    Args:
        engine_name: "webui" or "mcp"
        action: "start", "stop", "restart"
        details: Dict with extra info (pid, port, status, etc.)
    """
    timestamp = utcnow_str()
    detail_str = ""
    if details:
        parts = [f"{k}={v}" for k, v in details.items()]
        detail_str = " ".join(parts)

    log_line = f"[{timestamp}] [{engine_name}] {action}: {detail_str}"

    # Write to logs/system_engine.log
    try:
        log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "system_engine.log")
        with open(log_path, "a") as f:
            f.write(log_line + "\n")
    except Exception as _e:
        # File write failure is non-critical but worth noting
        logger.warning("[qr-system] LOG WRITE FAILED: %s", _e)

    # Also log via module logger for tmux/consistency
    logger.info("[qr-system] %s", log_line)


def _get_pid_status(pid):
    """Check if a process with given PID is running.

    Args:
        pid: Process ID to check.

    Returns:
        True if process exists and is not zombie, False otherwise.
    """
    if not pid:
        return False
    try:
        import psutil
        proc = psutil.Process(pid)
        if proc.status() != "zombie":
            return True
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _is_process_orphaned(pid):
    """Check if a process is orphaned (parent API has died).

    An orphaned process has PPID=1 (re-parented to init) or its parent
    process no longer exists. This means the API that spawned it died,
    but the subprocess survived — we should kill it and start fresh.

    Args:
        pid: Process ID to check.

    Returns:
        True if process is orphaned, False otherwise.
    """
    if not pid:
        return False
    try:
        import psutil
        proc = psutil.Process(pid)
        ppid = proc.ppid()
        # Re-parented to init (PPID=1) means original parent died
        if ppid == 1:
            return True
        # Parent process doesn't exist — also orphaned
        try:
            parent = psutil.Process(ppid)
            parent_status = parent.status()
            if parent_status in ("zombie",):
                return True
        except psutil.NoSuchProcess:
            return True
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _kill_orphaned_process(pid, name="process"):
    """Gracefully kill an orphaned process.

    Args:
        pid: Process ID to kill.
        name: Display name for logging.

    Returns:
        True if successfully killed, False otherwise.
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        proc.terminate()  # SIGTERM — graceful
        # Wait briefly for exit
        for _ in range(10):
            if not _get_pid_status(pid):
                return True
            time.sleep(0.1)
        # Force kill if still alive
        if _get_pid_status(pid):
            proc.kill()
            time.sleep(0.5)
        return not _get_pid_status(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _find_stale_schedulers():
    """Find scheduler processes by command name scanning, independent of PID tracking.

    Used as a coexistence guard: scans all running processes for quickrobot_scheduler.__main__
    and returns PIDs of any found. This catches stale schedulers that PID-in-DB tracking misses,
    including cases where the API restarts rapidly and the old scheduler survives prctl(PDEATHSIG).

    Returns:
        List of PIDs (ints) of running scheduler processes. Empty if none found.
    """
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
        pids = []
        for line in result.stdout.splitlines():
            # Match the scheduler entry; skip ps aux itself and grep
            if "quickrobot_scheduler" in line or "engine.quickrobot_scheduler" in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1])
                        pids.append(pid)
                    except ValueError:
                        pass
        return pids
    except Exception as exc:
        logger.warning("[qr-system] stale scheduler scan failed: %s", exc)
        return []


def start_system_engine(engine_name, env_config, api_host, api_port, python_exe=None):
    """Start a system engine subprocess.

    Validates env config, builds command, checks for existing process,
    spawns subprocess, updates DB with PID, transitions state.

    Args:
        engine_name: "webui" or "mcp"
        env_config: Dict from load_env_config()
        api_host: API server bind host
        api_port: API server bind port
        python_exe: Optional explicit Python interpreter path (MCP uses pipx venv)

    Returns:
        {"action": "start", "pid": <int>, "port": <int>,
         "status": "started"|"existing_process_alive", "engine": engine_name}
    """
    from db.adapters.instances import get_instance, update_instance, transition_state
    from db.adapters.configs import get_engine_config as _gec

    # Determine instance ID from env config or DB lookup
    inst_id = get_system_instance_id(engine_name)

    db_path = _CONFIG.get("db_path") if "_CONFIG" in globals() else os.path.join(os.getcwd(), "data", "quickrobot.db")

    inst = None
    if inst_id:
        try:
            inst = get_instance(db_path, inst_id)
        except Exception as _e:
            logger.debug("get_instance failed for %s (id=%d): %s", engine_name, inst_id, _e)
            pass

    # Determine port from env config (use structured defaults for non-env values)
    if engine_name == "webui":
        port = int(env_config.get("QUICKROBOT_WEBUI_PORT") or str(get_port_default("quickrobot-webui")))
    elif engine_name == "mcp":
        port = int(env_config.get("QUICKROBOT_MCP_PORT") or str(get_port_default("quickrobot-mcp")))
    elif engine_name == "scheduler":
        port = None

    # Port conflict safety: check + free if stale process holds it
    check_and_free_port(port, engine_name)

    # REG-03-F1 Part 1: Stale scheduler coexistence guard.
    # Scheduler has no port so check_and_free_port() skips it.
    # Scan by command name to catch stale schedulers that PID tracking misses.
    if engine_name == "scheduler":
        stale = _find_stale_schedulers()
        import psutil as _psutil  # local import — not at module level
        for spid in stale:
            try:
                proc = _psutil.Process(spid)
                ppid = proc.ppid()
                # Skip if this is our own process group (ppid matches our PID)
                my_pid = os.getpid()
                if ppid == my_pid:
                    continue
                logger.warning("[qr-system] scheduler: found stale process (pid=%d, ppid=%d), killing", spid, ppid)
                try:
                    proc.kill()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass

    # Check for existing live process via stored PID
    old_pid = inst.get("pid_last_known") if inst else None
    if old_pid and _get_pid_status(old_pid):
        # Is this a true child of this API, or an orphan from a dead API?
        if _is_process_orphaned(old_pid):
            # Check per-engine restart adopt flag (from .env, default=QR_XXX_RESTART_ADOPT)
            adopt_env = os.getenv(f"QUICKROBOT_{engine_name.upper()}_RESTART_ADOPT")
            if adopt_env is None:
                _adopt_map = {"webui": QR_WEBUI_RESTART_ADOPT,
                              "mcp": QR_MCP_RESTART_ADOPT,
                              "scheduler": QR_SCHEDULER_RESTART_ADOPT}
                adopt_env = _adopt_map.get(engine_name, "false")
            adopt = adopt_env.lower() in ("true", "1")
            if adopt:
                logger.info("[qr-system] %s: orphaned process (pid=%d) — adopting (RESTART_ADOPT=true)", engine_name, old_pid)
                try:
                    transition_state(db_path, inst_id, QR_STATE_DEPLOYED)
                except Exception as _e:
                    logger.debug("transition_state deploy failed for %s (id=%d): %s", engine_name, inst_id, _e)
                    pass
                return {"action": "start", "port": port, "pid": old_pid,
                        "status": "existing_process_alive", "engine": engine_name}
            logger.info("[qr-system] %s: orphaned process detected (pid=%d), killing and restarting", engine_name, old_pid)
            _kill_orphaned_process(old_pid, engine_name)
            try:
                update_instance(db_path, inst_id, pid_last_known=None)
            except Exception as _e:
                logger.debug("update_instance pid_clear failed for %s (id=%d): %s", engine_name, inst_id, _e)
                pass
        else:
            # True existing child — skip
            try:
                transition_state(db_path, inst_id, QR_STATE_DEPLOYED)
            except Exception as _e:
                logger.debug("transition_state deployed (existing child) failed for %s (id=%d): %s", engine_name, inst_id, _e)
                pass
            return {"action": "start", "port": port, "pid": old_pid,
                    "status": "existing_process_alive", "engine": engine_name}

    # Build command
    try:
        cmd = _build_command(engine_name, env_config, api_host, api_port)
    except Exception as exc:
        _log_lifecycle(engine_name, "start", {"error": str(exc)})
        return {"error": f"Failed to build command: {exc}", "action": "start", "engine": engine_name}

    # Resolve python executable (for MCP, use configured interpreter)
    if python_exe and os.path.isfile(python_exe):
        exe_path = python_exe
    elif engine_name == "mcp" and inst:
        # Try to resolve MCP interpreter from config
        try:
            et_id = inst.get("engine_type_id")
            row = _gec(db_path, et_id, "mcp_python_interpreter") if et_id else None
            if row and row.get("value"):
                mp = str(row["value"]).strip()
                if os.path.isfile(mp) and os.access(mp, os.X_OK):
                    exe_path = mp
                else:
                    exe_path = sys.executable
            else:
                # Try pipx auto-detect
                pipx_py = os.path.expanduser("~/.local/share/pipx/venvs/mcp/bin/python")
                if os.path.isfile(pipx_py):
                    exe_path = pipx_py
                else:
                    exe_path = sys.executable
        except Exception as _e:
            logger.debug("pipx exe auto-detect failed, using sys.executable: %s", _e)
            exe_path = sys.executable
    else:
        exe_path = sys.executable

    # Build explicit env whitelist via consolidated builder
    env = build_subprocess_env(engine_name, env_config, api_host, api_port, is_system_managed=True)

    # Unified log file for all system engines
    log_path = get_engine_log_path(engine_name)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    _logf = open(log_path, "a")  # Keep handle open for subprocess lifetime
    popen_kwargs = {
        "stdout": _logf,  # All output to engine log file
        "stderr": _logf,
        "env": env,
        "cwd": os.getcwd(),
        "start_new_session": True,  # Isolates child in its own process group
    }

    # Whitelist verification: ensure test vars from env file don't leak
    _test_key = "QUICKROBOT_TEST_VAR"
    if _test_key in env_config and _test_key not in env:
        logger.debug("[qr-system] ENV WHITELIST OK: %s=%s NOT in child env (whitelist working)", _test_key, env_config[_test_key])
    elif _test_key in env:
        logger.warning("[qr-system] ENV WHITELIST FAIL: %s leaked into child env", _test_key)
    # Log env var count for comparison
    os_env_count = len(os.environ)
    child_env_count = len(env)
    if child_env_count < os_env_count:
        logger.info("[qr-system] ENV: subprocess env reduced %d → %d keys (was copy, now whitelist)", os_env_count, child_env_count)

    try:
        proc = subprocess.Popen([exe_path] + cmd[1:], **popen_kwargs)
        # C5-REG: Auto-terminate on parent death (survives SIGKILL, not just SIGTERM)
        import ctypes as _ctypes
        _ctypes.CDLL("libc.so.6").prctl(1, 15)  # PR_SET_PDEATHSIG=1, SIGTERM=15
    except OSError as exc:
        _log_lifecycle(engine_name, "start", {"error": str(exc)})
        return {"error": f"Failed to start {engine_name}: {exc}", "action": "start", "engine": engine_name}

    new_pid = proc.pid
    # Register child PID for cleanup on shutdown
    _register_child(new_pid)
    if inst:
        try:
            update_instance(db_path, inst_id, pid_last_known=new_pid)
            transition_state(db_path, inst_id, QR_STATE_DEPLOYED)
        except Exception as _e:
            logger.debug("post-spawn update+transition failed for %s (id=%d): %s", engine_name, inst_id, _e)
            pass

    _log_lifecycle(engine_name, "start", {"pid": new_pid, "port": port, "api_host": api_host, "api_port": api_port})
    return {"action": "start", "port": port, "pid": new_pid, "status": "started", "engine": engine_name}


def shutdown_subprocesses(env_config, timeout=10):
    """Gracefully shut down running system engine subprocesses (WebUI, MCP, Scheduler).

    Checks each engine's PID in DB, terminates if process is alive. Skips dead/stopped ones.
    Used during API restart to cleanly stop child engines before execv replacement.

    Args:
        env_config: Dict from load_env_config() — read AUTOSTART flags but always stop running ones.
        timeout: Seconds to wait for each process to exit (default 10).

    Returns:
        list of dicts: [{"engine": "webui", "pid": 123, "status": "terminated|already_dead|skipped"}]
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    results = []
    engines = [("webui", 2), ("mcp", 3), ("scheduler", 4)]

    # Read DB path — prefer _CONFIG if available, fallback to default location
    db_path = None
    if "_CONFIG" in globals():
        db_path = _CONFIG.get("db_path")
    if not db_path:
        try:
            qr_env = load_env_config(os.getcwd())
            db_file_raw = qr_env.get("QUICKROBOT_DB_PATH", "data/quickrobot.db")
            db_dir = os.path.dirname(db_file_raw)
        except FileNotFoundError:
            db_dir = "data"
    else:
        db_dir = os.path.dirname(db_path) if "/" in db_path else "data"

    db_file = os.path.join(os.getcwd(), "quickrobot.db") if not os.path.exists(db_path) else db_path

    for engine_name, inst_id in engines:
        result = {"engine": engine_name}

        # Look up PID from DB
        try:
            target_db = db_path if db_path and os.path.exists(db_path) else os.path.join(os.getcwd(), "data", "quickrobot.db")
            inst = _gi(target_db, inst_id)
        except Exception as _e:
            logger.debug("shutdown_subprocesses: get_instance failed for %s (id=%d): %s", engine_name, inst_id, _e)
            result["status"] = "skipped"
            results.append(result)
            continue

        pid = inst.get("pid_last_known") if inst else None
        result["pid"] = pid

        if not pid:
            result["status"] = "already_dead"
            results.append(result)
            continue

        if not _get_pid_status(pid):
            # Process already dead — clear PID from DB
            try:
                _ui(target_db, inst_id, pid_last_known=None)
            except Exception as _e:
                logger.debug("shutdown_subprocesses: pid_clear failed for %s (pid=%d): %s", engine_name, pid, _e)
            result["status"] = "already_dead"
            results.append(result)
            continue

        # Process alive — terminate it
        try:
            import psutil
            psutil.Process(pid).terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            result["status"] = "already_dead"
            results.append(result)
            continue

        logger.info("shutdown_subprocesses: terminated %s (pid=%d)", engine_name, pid)

        # Wait for process to exit
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _get_pid_status(pid):
                # Successfully terminated — clear PID from DB
                try:
                    _ui(target_db, inst_id, pid_last_known=None)
                except Exception as _e:
                    logger.debug("shutdown_subprocesses: pid_clear after terminate failed for %s (pid=%d): %s", engine_name, pid, _e)
                result["status"] = "terminated"
                results.append(result)
                continue  # Continue with remaining engines instead of returning early
            time.sleep(0.5)

        # Timeout — force kill
        try:
            import psutil
            if _get_pid_status(pid):
                psutil.Process(pid).kill()
                time.sleep(0.5)
        except Exception as _e:
            logger.debug("shutdown_subprocesses: force kill failed for %s (pid=%d): %s", engine_name, pid, _e)

        try:
            _ui(target_db, inst_id, pid_last_known=None)
        except Exception as _e:
            logger.debug("shutdown_subprocesses: pid_clear after force kill failed for %s (pid=%d): %s", engine_name, pid, _e)

        result["status"] = "force_killed"
        results.append(result)

    return results


def stop_system_engine(engine_name, env_config):
    """Stop a system engine subprocess.

    Looks up PID via DB (pid_last_known), terminates process, clears PID in DB.

    Args:
        engine_name: "webui" or "mcp"
        env_config: Dict from load_env_config()

    Returns:
        {"action": "stop", "pid": <int or None>, "engine": engine_name}
    """
    from db.adapters.instances import get_instance, update_instance
    from db.adapters.configs import get_engine_config as _gec

    # Determine instance ID
    inst_id = get_system_instance_id(engine_name)

    db_path = _CONFIG.get("db_path") if "_CONFIG" in globals() else os.path.join(os.getcwd(), "data", "quickrobot.db")

    inst = None
    if inst_id:
        try:
            inst = get_instance(db_path, inst_id)
        except Exception as _e:
            logger.debug("get_instance failed for %s (id=%d): %s", engine_name, inst_id, _e)
            pass

    pid = inst.get("pid_last_known") if inst else None
    if pid and _get_pid_status(pid):
        try:
            import psutil
            psutil.Process(pid).terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass  # Best-effort termination

    # Deregister child PID from tracking
    with _CHILD_PID_LOCK:
        _CHILD_PIDS.discard(pid) if pid else None

    if inst:
        try:
            update_instance(db_path, inst_id, pid_last_known=None)
        except Exception as _e:
            logger.debug("update_instance pid_clear on stop failed for %s (id=%d): %s", engine_name, inst_id, _e)
            pass

    _log_lifecycle(engine_name, "stop", {"pid": pid})
    return {"action": "stop", "pid": pid, "engine": engine_name}


def restart_system_engine(engine_name, env_config, api_host, api_port, timeout=None, python_exe=None):
    """Full lifecycle restart with dead-check.

    Sequence:
    1. Stop engine (terminate process)
    2. Wait up to `timeout` seconds for process to fully exit
    3. If process still alive after timeout: log warning, kill -9
    4. Verify old PID is gone (psutil check)
    5. Start new process

    Args:
        engine_name: "webui" or "mcp"
        env_config: Dict from load_env_config()
        api_host: API server bind host
        api_port: API server bind port
        timeout: Seconds to wait for dead check (default from env: QUICKROBOT_SERVER_SPAWN_TIMEOUT)
        python_exe: Optional explicit Python interpreter path

    Returns:
        {"action": "restart", "pid": <new_pid>, "port": <int>,
         "old_pid": <old_pid or None>, "dead_verified": True/False,
         "status": "restart_success"|"restart_timeout"}
    """
    import subprocess

    # Determine timeout
    if timeout is None:
        timeout = int(env_config.get("QUICKROBOT_SERVER_SPAWN_TIMEOUT", 5))

    db_path = _CONFIG.get("db_path") if "_CONFIG" in globals() else os.path.join(os.getcwd(), "data", "quickrobot.db")

    # Determine instance ID
    inst_id = get_system_instance_id(engine_name)

    inst = None
    if inst_id:
        try:
            from db.adapters.instances import get_instance
            inst = get_instance(db_path, inst_id)
        except Exception as _e:
            logger.debug("get_instance failed for %s (id=%d): %s", engine_name, inst_id, _e)
            pass

    old_pid = inst.get("pid_last_known") if inst else None

    # Step 1: Terminate existing process
    if old_pid and _get_pid_status(old_pid):
        try:
            import psutil
            psutil.Process(old_pid).terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    _log_lifecycle(engine_name, "restart", {"old_pid": old_pid, "timeout": timeout})

    # Step 2-3: Wait for process to die
    dead_verified = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _get_pid_status(old_pid):
            dead_verified = True
            break
        time.sleep(0.5)

    if not dead_verified:
        # Force kill
        logger.warning("[qr-system] %s restart: old PID %d didn't exit within %ds, force killing", engine_name, old_pid, timeout)
        try:
            import psutil
            if _get_pid_status(old_pid):
                psutil.Process(old_pid).kill()
                time.sleep(1)
        except Exception as _e:
            logger.debug("force kill failed for %s (pid=%d): %s", engine_name, old_pid, _e)
            pass

    # Step 4-5: Start new process
    result = start_system_engine(engine_name, env_config, api_host, api_port, python_exe)

    if result.get("status") == "started":
        _log_lifecycle(engine_name, "restart", {
            "old_pid": old_pid,
            "new_pid": result.get("pid"),
            "dead_verified": dead_verified,
            "timeout": timeout
        })
        return {
            "action": "restart",
            "pid": result.get("pid"),
            "port": result.get("port"),
            "old_pid": old_pid,
            "dead_verified": dead_verified,
            "status": "restart_success",
            "engine": engine_name
        }

    _log_lifecycle(engine_name, "restart", {"error": result.get("error")})
    return {
        "action": "restart",
        "old_pid": old_pid,
        "dead_verified": dead_verified,
        "status": "restart_failed",
        "engine": engine_name,
        "error": result.get("error")
    }


def get_system_engine_pid(engine_name, env_config, _retried=False):
    """Lookup PID for a system engine.

    Reads pid_last_known from DB instance record. Verifies process exists via psutil.
    If PID is stale (dead process), resets pid_last_known to NULL and auto-restarts.
    Returns None if PID not found or process dead (after one retry).

    Args:
        engine_name: "webui", "mcp", or "scheduler"
        env_config: Dict from load_env_config()
        _retried: Internal — prevents infinite recursion on stale PID restart.

    Returns:
        PID (int) or None
    """
    from db.adapters.instances import get_instance, update_instance

    # Determine instance ID
    inst_id = get_system_instance_id(engine_name)

    if not inst_id:
        return None

    db_path = _CONFIG.get("db_path") if "_CONFIG" in globals() else os.path.join(os.getcwd(), "data", "quickrobot.db")

    inst = None
    try:
        inst = get_instance(db_path, inst_id)
    except Exception as _e:
        logger.debug("get_instance failed for %s (id=%d): %s", engine_name, inst_id, _e)
        pass

    pid = inst.get("pid_last_known") if inst else None
    if pid and _get_pid_status(pid):
        return pid

    # Stale PID — clear cached value and auto-restart (only once to avoid storm)
    if not _retried and pid:
        try:
            update_instance(db_path, inst_id, pid_last_known=None)
            logger.info("[qr-system] Stale PID for %s (%d) cleared, restarting...", engine_name, pid)
            # Extract api_host/api_port from env_config for start_system_engine
            api_host = env_config.get("QUICKROBOT_API_HOST", "127.0.0.1") if isinstance(env_config, dict) else "127.0.0.1"
            api_port_raw = env_config.get("QUICKROBOT_API_PORT", str(QR_ENGINE_PORT_DEFAULTS["quickrobot-api"])) if isinstance(env_config, dict) else str(QR_ENGINE_PORT_DEFAULTS["quickrobot-api"])
            try:
                api_port = int(api_port_raw)
            except (ValueError, TypeError):
                api_port = QR_ENGINE_PORT_DEFAULTS["quickrobot-api"]
            start_system_engine(engine_name, env_config, api_host, api_port)
            # Recursive call to get the NEW PID after restart
            return get_system_engine_pid(engine_name, env_config, _retried=True)
        except Exception as exc:
            logger.warning("[qr-system] EIO prevention restart failed for %s: %s", engine_name, exc)

    return None


def build_subprocess_env(engine_name, env_config, api_host, api_port, instance_config=None, is_system_managed=True):
    """Build a whitelisted subprocess environment dict for system engines.

    Consolidated builder replaces three independent inline dicts in:
      - lib_system_engine.py::start_system_engine() (L541-557)
      - engine/quickrobot_webui/__init__.py::execute() (L266-278)
      - engine/quickrobot_mcp/__init__.py::execute() (L432-457)

    Layer 1: Base whitelist — always merged (PATH, HOME, LANG, LC_ALL, API_HOST, API_PORT,
             API_BEARER_TOKEN, CONSOLE_DEBUG_LEVEL, ANSIBLE_LOG_LEVEL)
    Layer 2: Engine extras — engine-specific (WEBUI_HOST/PORT, MCP_HOST/PORT/PYTHONPATH/FLAGS)
    Layer 3: Per-instance env_vars — subprocess engine only (config_override.env_vars)

    Args:
        engine_name: "webui", "mcp", or "subprocess"
        env_config: Dict from load_env_config()
        api_host: API server bind host
        api_port: API server bind port
        instance_config: Per-instance config_override dict (Layer 3, subprocess only)
        is_system_managed: True for system engines, False for user subprocess

    Returns:
        env_dict: Environment variable dict ready for subprocess.Popen
    """
    from lib.qr_engine_ids import (
        QR_ENV_PATH, QR_ENV_HOME, QR_ENV_LANG, QR_ENV_LC_ALL, QR_ENV_PYTHONPATH,
        QR_ENV_API_BEARER_TOKEN, QR_ENV_API_HOST, QR_ENV_API_PORT,
        QR_ENV_WEBUI_HOST, QR_ENV_WEBUI_PORT,
        QR_ENV_MCP_HOST, QR_ENV_MCP_PORT,
        QR_ENV_MCP_DISABLE_DNS_REBINDING, QR_ENV_MCP_CORS_ORIGINS,
    )

    env = {}

    # === LAYER 1: Base whitelist (always present) ===
    env[QR_ENV_PATH] = os.environ.get(QR_ENV_PATH, "")
    env[QR_ENV_HOME] = os.environ.get(QR_ENV_HOME, "")
    env[QR_ENV_LANG] = os.environ.get(QR_ENV_LANG, "en_US.UTF-8")
    env[QR_ENV_LC_ALL] = os.environ.get(QR_ENV_LC_ALL, "en_US.UTF-8")
    # Python bytecode cache — redirect all __pycache__ to single location (PYTHONPYCACHEPREFIX)
    _pycache_prefix = env_config.get("PYTHONPYCACHEPREFIX", "")
    if _pycache_prefix:
        env["PYTHONPYCACHEPREFIX"] = _pycache_prefix
    env["QUICKROBOT_API_BEARER_TOKEN"] = env_config.get("QUICKROBOT_API_BEARER_TOKEN", "")
    # Auth token — passed to all system engine subprocesses for API authentication
    env["QUICKROBOT_API_KEY"] = env_config.get("QUICKROBOT_API_KEY", "")
    env["QUICKROBOT_API_HOST"] = str(api_host)
    env["QUICKROBOT_API_PORT"] = str(api_port)
    # Operational mode — ensures subprocess always knows its mode regardless of _CONFIG import timing
    env["QUICKROBOT_PB_MODE"] = os.environ.get("QUICKROBOT_PB_MODE", "prod")
    # Debug/logging — each subprocess reads its own level from env
    env["QUICKROBOT_CONSOLE_DEBUG_LEVEL"] = env_config.get("QUICKROBOT_CONSOLE_DEBUG_LEVEL", "")
    env["QUICKROBOT_ANSIBLE_LOG_LEVEL"] = env_config.get("QUICKROBOT_ANSIBLE_LOG_LEVEL", "errors")
    # Log path — used by health check for FATAL exit logging
    env["QUICKROBOT_LOG_PATH"] = get_engine_log_path(engine_name)
    # Unified system engine health check retry count (all subprocesses)
    _sys_retries = os.environ.get("QUICKROBOT_SYSTEM_RETRIES", "")
    if _sys_retries:
        try:
            v = int(_sys_retries)
            if 1 <= v <= 10:
                env["QUICKROBOT_SYSTEM_RETRIES"] = str(v)
        except (ValueError, TypeError):
            pass

    # === LAYER 2: Engine-specific extras ===
    if engine_name == "webui":
        env[QR_ENV_WEBUI_HOST] = env_config.get("QUICKROBOT_WEBUI_HOST", str(api_host))
        env[QR_ENV_WEBUI_PORT] = str(env_config.get("QUICKROBOT_WEBUI_PORT", api_port))

    elif engine_name == "mcp":
        env[QR_ENV_PYTHONPATH] = os.getcwd()
        env[QR_ENV_MCP_HOST] = env_config["QUICKROBOT_MCP_HOST"]
        env[QR_ENV_MCP_PORT] = str(env_config.get("QUICKROBOT_MCP_PORT", ""))

        db_path = os.path.join(os.getcwd(), "data", "quickrobot.db")

        def _resolve_mcp_flag(db_key, env_key):
            """Resolve MCP flag from engine_configs (runtime) or .quickrobot.env."""
            try:
                from db.adapters.configs import get_engine_config as _gec
                if engine_name == "mcp":
                    from lib.qr_engine_ids import QR_ENGINE_MCP
                    row = _gec(db_path, QR_ENGINE_MCP, db_key)
                    if row and row.get("value"):
                        return str(row["value"])
            except Exception as _e:
                logger.debug("MCP flag resolution failed for %s/%s: %s", engine_name, db_key, _e)
                pass
            return env_config.get(env_key, "false")

        # Set env vars for subprocess — names must match qr_mcp_server.py expectations
        env["QUICKROBOT_MCP_READ"] = _resolve_mcp_flag("mcp_allow_reads", "QUICKROBOT_MCP_READ")
        env["QUICKROBOT_MCP_WRITE"] = _resolve_mcp_flag("mcp_allow_writes", "QUICKROBOT_MCP_WRITE")
        env["QUICKROBOT_MCP_FULLPROXY"] = _resolve_mcp_flag("mcp_allow_proxy", "QUICKROBOT_MCP_FULLPROXY")
        # MCP SSE auth token (MCP-DUAL-TOKEN) — passed only to MCP subprocess
        env["QUICKROBOT_MCP_TOKEN"] = env_config.get("QUICKROBOT_MCP_TOKEN", "")
        # MCP key disabled flag — controls whether SSE endpoint requires auth
        mcp_key_disabled = env_config.get("QUICKROBOT_MCP_KEY_DISABLED", "false")
        if mcp_key_disabled:
            env["QUICKROBOT_MCP_KEY_DISABLED"] = mcp_key_disabled

        disable_dns = env_config.get("QUICKROBOT_MCP_DISABLE_DNS_REBINDING", "")
        if disable_dns:
            env[QR_ENV_MCP_DISABLE_DNS_REBINDING] = disable_dns

        cors_origins = env_config.get("QUICKROBOT_MCP_CORS_ORIGINS", "")
        if cors_origins:
            env[QR_ENV_MCP_CORS_ORIGINS] = cors_origins

    # === LAYER 3: Per-instance env_vars (subprocess engine only) ===
    if not is_system_managed and instance_config:
        co = instance_config if isinstance(instance_config, dict) else {}
        user_env_vars = co.get("env_vars", {})
        if isinstance(user_env_vars, dict):
            env.update(user_env_vars)

    return env


def api_health_check_loop(api_host, api_port, max_retries=3, retry_delay=3, check_interval=60):
    """Periodic health check for system subprocesses.

    Checks API connectivity every check_interval seconds. Exits with error if
    API unreachable after max_retries consecutive failures. Prevents zombies
    by ensuring clean exit when parent API dies.

    Args:
        api_host: API server host (e.g., "127.0.0.1")
        api_port: API server port (e.g., 8039)
        max_retries: Number of consecutive failures before exit
        retry_delay: Seconds between retry attempts (default: 3 for fast failure)
        check_interval: Seconds between health checks after recovery (default: 60)

    Returns:
        None — exits process on failure
    """
    import requests as _requests_lib

    _api_url = f"http://{api_host}:{api_port}/api/v1/app/status"
    _consecutive_failures = 0

    logger.info("[qr-system] Health check starting: %s (interval=%ds, retries=%d, retry_delay=%ds)",
                _api_url, check_interval, max_retries, retry_delay)
    # Startup grace period: give Flask time to bind and start accepting connections.
    import time as _time_mod
    _time_mod.sleep(QR_HEALTH_CHECK_SLEEP)

    while True:
        try:
            _resp = _requests_lib.get(_api_url, timeout=10)
            if _resp.status_code == 200 and _resp.json().get("status") == "ok":
                if _consecutive_failures > 0:
                    logger.info("[qr-system] Health check recovered after %d failure(s)", _consecutive_failures)
                _consecutive_failures = 0
                _wait = check_interval  # Normal interval after recovery
            else:
                _consecutive_failures += 1
                logger.warning("[qr-system] Health check failed (attempt %d): HTTP %d", _consecutive_failures, _resp.status_code)
                _wait = retry_delay  # Short delay between retries

        except _requests_lib.ConnectionError as _e:
            _consecutive_failures += 1
            logger.warning("[qr-system] Health check connection error (attempt %d): %s", _consecutive_failures, _e)
            _wait = retry_delay
        except _requests_lib.Timeout as _e:
            _consecutive_failures += 1
            logger.warning("[qr-system] Health check timeout (attempt %d): %s", _consecutive_failures, _e)
            _wait = retry_delay
        except Exception as _e:
            _consecutive_failures += 1
            logger.warning("[qr-system] Health check error (attempt %d): %s", _consecutive_failures, _e)
            _wait = retry_delay

        # Exit if too many consecutive failures
        if _consecutive_failures >= max_retries:
            # Write FATAL to log file directly (stdout may not flush on os._exit)
            _fatal_msg = f"[qr] FATAL: API unreachable after {_consecutive_failures} attempts. Exiting."
            try:
                import datetime
                _log_path = os.environ.get("QUICKROBOT_LOG_PATH", "")
                if _log_path:
                    # Extract engine name from log path (e.g., "logs/scheduler.log" → "scheduler")
                    _engine_name = os.path.basename(_log_path).replace(".log", "")
                    with open(_log_path, "a") as _lf:
                        _lf.write(f"{datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} {_engine_name}: {_fatal_msg}\n")
            except Exception as _e:
                logger.debug("FATAL log file write failed: %s", _e)
                pass
            print(_fatal_msg, flush=True)
            os._exit(1)

        # Wait before next check
        time.sleep(_wait)


def start_health_check_thread(api_host, api_port, max_retries=3, retry_delay=10, check_interval=60):
    """Start health check as a daemon thread.

    Args:
        api_host: API server host
        api_port: API server port
        max_retries: Number of consecutive failures before exit
        retry_delay: Seconds between retry attempts
        check_interval: Seconds between health checks (default: 60)

    Returns:
        Thread object (daemon=True)
    """
    import threading as _threading
    _thread = _threading.Thread(
        target=api_health_check_loop,
        args=(api_host, api_port, max_retries, retry_delay, check_interval),
        daemon=True,
        name="api-health-check"
    )
    _thread.start()
    return _thread


# ── System + Subprocess Engine Health Loop (HC-SYSTEM) ──────────────────
# Monitors all system-managed and subprocess instances for dead PIDs.
# Auto-restarts dead processes, logs to logs/system_health.log.

_SYSTEM_HEALTH_INTERVAL = 30  # seconds between health checks
_SYSTEM_HEALTH_LOG_NAME = "system_health.log"
_SYSTEM_HEALTH_STARTED = False  # Guard: only start once


def _get_system_health_log_path():
    """Get the log file path for system health checks."""
    return os.path.join(os.getcwd(), _LOG_DIR, _SYSTEM_HEALTH_LOG_NAME)


def _system_health_check_one(db_path, inst_id, pid, engine_name):
    """Check a single instance's PID and auto-restart if dead.

    Uses os.kill(pid, 0) for fast existence check (no psutil import overhead).
    Falls back to psutil if os.kill fails (e.g., permission issues).

    Args:
        db_path: Path to the SQLite database.
        inst_id: Instance ID to check.
        pid: Process ID to verify.
        engine_name: Human-readable engine name for logging.

    Returns:
        str: "alive", "dead_restarted", or "error"
    """
    import psutil as _psutil

    alive = False
    try:
        proc = _psutil.Process(pid)
        if proc.status() != "zombie":
            alive = True
    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
        pass

    if alive:
        return "alive"

    # Process is dead — attempt auto-restart
    try:
        from db.adapters.instances import update_instance, get_instance

        # Get instance info to determine restart method
        inst = get_instance(db_path, inst_id)
        if not inst:
            return "error"

        engine_type_id = inst.get("engine_type_id")
        is_subprocess = (engine_type_id == QR_ENGINE_SUBPROCESS)

        # For system engines (IDs 1-4), use start_system_engine path
        # For subprocess engines (ID=12), use QrSubprocessEngine execute path
        if is_subprocess:
            from engine.subprocess import QrSubprocessEngine
            try:
                QrSubprocessEngine().execute(inst_id, "start", db_path)
                _log_system_health(
                    engine_name, f"restarted_subprocess",
                    {"inst_id": inst_id, "old_pid": pid}
                )
                return "dead_restarted"
            except Exception as _exc:
                _log_system_health(
                    engine_name, f"restart_failed_subprocess",
                    {"inst_id": inst_id, "old_pid": pid, "error": str(_exc)}
                )
                return "error"
        else:
            # System-managed instance — clear stale PID and let
            # start_system_engine handle restart on next cycle
            try:
                update_instance(db_path, inst_id, pid_last_known=None)
                _log_system_health(
                    engine_name, f"dead_system_engine",
                    {"inst_id": inst_id, "pid": pid}
                )
                return "dead_restarted"
            except Exception as _exc:
                _log_system_health(
                    engine_name, f"dead_system_engine_error",
                    {"inst_id": inst_id, "pid": pid, "error": str(_exc)}
                )
                return "error"

    except Exception as _exc:
        _log_system_health(
            engine_name, f"check_error",
            {"inst_id": inst_id, "pid": pid, "error": str(_exc)}
        )
        return "error"


def _system_health_loop():
    """Periodic health check loop for system-managed and subprocess instances.

    Runs every _SYSTEM_HEALTH_INTERVAL seconds:
    1. Queries instances with system_managed=1 or engine_type_id=12
    2. For each with a non-NULL PID, checks if process is alive
    3. Dead PIDs → auto-restart (subprocess) or clear PID (system engine)
    4. Logs all results to logs/system_health.log

    Designed for use as a daemon thread target — no exceptions escape.
    """
    import threading as _threading
    import time as _time_mod

    db_path = _CONFIG.get("db_path", os.path.join(os.getcwd(), "data", "quickrobot.db"))
    from db.sqlite import pool
    from lib.qr_engine_ids import QR_ENGINE_SUBPROCESS

    while True:
        try:
            with pool(db_path) as conn:
                # Query system-managed + subprocess instances with PIDs
                rows = conn.execute(
                    """SELECT id, pid_last_known, engine_type_id, name
                       FROM instances
                       WHERE (system_managed = 1 OR engine_type_id = ?)
                         AND pid_last_known IS NOT NULL
                         AND state != 'stopped'""",
                    (QR_ENGINE_SUBPROCESS,),
                ).fetchall()

                # Also query system-managed instances without PIDs (scheduler).
                # These need process-table scanning to detect alive/dead.
                no_pid_rows = conn.execute(
                    """SELECT id, engine_type_id, name
                       FROM instances
                       WHERE system_managed = 1
                         AND pid_last_known IS NULL
                         AND state != 'stopped'""",
                ).fetchall()

            if not rows and not no_pid_rows:
                _time_mod.sleep(_SYSTEM_HEALTH_INTERVAL)
                continue

            alive_count = 0
            dead_count = 0
            error_count = 0

            for row in rows:
                inst_id = row["id"]
                pid = row["pid_last_known"]
                engine_type_id = row["engine_type_id"]
                engine_name = row["name"] if row["name"] else f"instance-{inst_id}"

                # Always update last_state_change so WebUI/API shows freshness
                # Direct SQL — transition_state fails for running→running (not in state machine)
                try:
                    from lib.lib_time import utcnow_str as _hc_now
                    now_ts = _hc_now()
                    from db.sqlite import pool as _hc_pool
                    with _hc_pool(db_path) as _hc_conn:
                        _hc_conn.execute(
                            "UPDATE instances SET last_state_change=? WHERE id=?",
                            (now_ts, inst_id),
                        )
                except Exception as _e:
                    logger.debug("last_state_change update failed for instance %d: %s", inst_id, _e)
                    pass  # Non-critical — stale timestamp is acceptable

                result = _system_health_check_one(
                    db_path, inst_id, pid, engine_name
                )

                if result == "alive":
                    alive_count += 1
                elif result == "dead_restarted":
                    dead_count += 1
                else:
                    error_count += 1

            # Handle system-managed instances without PIDs (scheduler).
            # Use process table scanning to detect alive/dead state.
            for row in no_pid_rows:
                inst_id = row["id"]
                engine_type_id = row["engine_type_id"]
                engine_name = row["name"] if row["name"] else f"instance-{inst_id}"

                # Update last_state_change for freshness
                try:
                    from lib.lib_time import utcnow_str as _hc_now
                    now_ts = _hc_now()
                    from db.sqlite import pool as _hc_pool
                    with _hc_pool(db_path) as _hc_conn:
                        _hc_conn.execute(
                            "UPDATE instances SET last_state_change=? WHERE id=?",
                            (now_ts, inst_id),
                        )
                except Exception as _e:
                    logger.debug("last_state_change update (no-pid row) failed for instance %d: %s", inst_id, _e)
                    pass

                # Check if this is the scheduler engine (ID=4 by convention)
                is_scheduler = False
                try:
                    from lib.qr_engine_ids import QR_ENGINE_SCHEDULER
                    et_row = conn.execute(
                        "SELECT id FROM engine_types WHERE name=?",
                        ("quickrobot-scheduler",),
                    ).fetchone()
                    is_scheduler = (et_row and et_row["id"] == QR_ENGINE_SCHEDULER) or engine_type_id == QR_ENGINE_SCHEDULER
                except Exception as _e:
                    logger.debug("scheduler engine type check failed for instance %d: %s", inst_id, _e)
                    pass

                if is_scheduler:
                    try:
                        stale = _find_stale_schedulers()
                        if len(stale) > 0:
                            alive_count += 1
                        else:
                            # No scheduler process found — update state to error
                            try:
                                from db.adapters.instances import update_instance
                                with pool(db_path) as _hc_pool:
                                    _hc_pool.execute(
                                        "UPDATE instances SET state='error' WHERE id=? AND state != 'error'",
                                        (inst_id,),
                                    )
                                dead_count += 1
                                _log_system_health(engine_name, "dead_system_engine_scan", {"inst_id": inst_id})
                            except Exception as _e:
                                logger.debug("dead state update failed for instance %d: %s", inst_id, _e)
                                error_count += 1
                    except Exception as _exc:
                        _log_system_health("hc-loop", "scheduler_scan_error", {"error": str(_exc)})
                        error_count += 1

            total_checked = len(rows) + len(no_pid_rows)
            _log_system_health(
                "hc-loop", "check_summary",
                {"total": total_checked, "alive": alive_count,
                 "dead_restarted": dead_count, "errors": error_count}
            )

        except Exception as _exc:
            # Non-fatal: loop continues on next interval
            _log_system_health(
                "hc-loop", "loop_error",
                {"error": str(_exc)}
            )

        _time_mod.sleep(_SYSTEM_HEALTH_INTERVAL)


def start_system_health_thread():
    """Start the system health check daemon thread.

    Idempotent: if already started, returns without creating a duplicate thread.

    Returns:
        Thread object (daemon=True), or None if already running.
    """
    global _SYSTEM_HEALTH_STARTED
    if _SYSTEM_HEALTH_STARTED:
        return None

    from threading import Thread as _Thread
    _thread = _Thread(
        target=_system_health_loop,
        daemon=True,
        name="system-health-check"
    )
    _thread.start()
    _SYSTEM_HEALTH_STARTED = True
    logger.info("[qr-system] system health check started (interval=%ds)", _SYSTEM_HEALTH_INTERVAL)
    return _thread


def _log_system_health(engine_name, action, details=None):
    """Log a system health event to logs/system_health.log.

    Args:
        engine_name: "hc-loop" for loop-level events, or instance name
        action: Short action descriptor (e.g., "dead_restarted", "alive")
        details: Optional dict with extra context
    """
    from lib.lib_time import utcnow_str

    timestamp = utcnow_str()
    detail_str = ""
    if details:
        parts = [f"{k}={v}" for k, v in sorted(details.items())]
        detail_str = " ".join(parts)

    log_line = f"[{timestamp}] [{engine_name}] {action}: {detail_str}"

    try:
        log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, _SYSTEM_HEALTH_LOG_NAME)
        with open(log_path, "a") as f:
            f.write(log_line + "\n")
    except Exception as _e:
        logger.debug("system health log write failed: %s", _e)
        pass  # Non-critical — don't let log failure break the health loop

    # Also print to stdout for tmux visibility (low frequency)
    if action in ("check_summary", "dead_restarted", "loop_error"):
        logger.info("[qr-system] %s", log_line)


# Import _CONFIG at module level for DB path access
try:
    from qr_api import _CONFIG
except ImportError:
    _CONFIG = {"db_path": os.path.join(os.getcwd(), "data", "quickrobot.db")}
