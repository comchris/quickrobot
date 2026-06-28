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

from lib.qr_engine_ids import (
    QR_DEFAULT_LOCALHOST, get_port_default, get_name_by_id,
    QR_FORBIDDEN_HOSTS, QR_ENGINE_PORT_DEFAULTS, get_system_instance_id,
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
            print(
                f"[qr] {engine_name} log rotated ({size:,}B → 0B)",
                file=sys.stderr, flush=True,
            )
            return True
    except OSError as _e:
        print(f"[qr] {engine_name} log rotation check failed: {_e}", file=sys.stderr)
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
                            print(
                                f"[qr] WARNING: Port {port} used by stale "
                                f"{service_name} process (pid={stale_pid}, "
                                f"PPID={ppid}). Killing."
                            )
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
                                    print(f"[qr] WARNING: Orphan kill loop error (pid={stale_pid}): {_e}")
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
        print(f"[qr] WARN: MCP binary path lookup failed (using defaults): {_e}")

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
            except Exception:
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
                print(f"[qr] WARN: .quickrobot.env line {line_no}: no '=' found, skipping: {raw_line.rstrip()}")
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
            print(f"[qr] ERROR: {key} is required but missing (line {line})")
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
                print(f"[qr] ERROR: {key}={val} — expected integer 1-65535, got {n} (line {line})")
                sys.exit(1)
        except ValueError:
            print(f"[qr] ERROR: {key}={val} — expected integer, got \"{val}\" (line {line})")
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
                print(f"[qr] ERROR: {key}={val} — expected >= {min_val}, got {n} (line {line})")
                sys.exit(1)
        except ValueError:
            print(f"[qr] ERROR: {key}={val} — expected integer, got \"{val}\" (line {line})")
            sys.exit(1)

    # Optional string keys with allowed values
    ansible_level = config.get("QUICKROBOT_ANSIBLE_LOG_LEVEL")
    if ansible_level is not None:
        line = key_line_map.get("QUICKROBOT_ANSIBLE_LOG_LEVEL", "?")
        if ansible_level not in ("errors", "warnings", "all"):
            print(f"[qr] ERROR: QUICKROBOT_ANSIBLE_LOG_LEVEL={ansible_level} — expected one of: errors, warnings, all (line {line})")
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
            print(f"[qr] ERROR: {key}={val} — expected true/false/yes/no/1/0 (line {line})")
            sys.exit(1)
        config[key] = normalized

    # Seed checksum keys — required for --init mode integrity
    for key in ("QUICKROBOT_SEED_CHECKSUM", "QUICKROBOT_SEED_FILESIZE"):
        if key not in config or not config[key]:
            line = key_line_map.get(key, "?")
            print(f"[qr] ERROR: {key} is required but missing (line {line})")
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
            print(f"[qr] FATAL: MCP bind host is '{host}' — {QR_FORBIDDEN_HOSTS}")
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
        print(f"[qr] LOG WRITE FAILED: {_e}")

    # Also print to stdout for tmux visibility
    print(f"[qr] {log_line}")


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
        print(f"[qr] WARN: stale scheduler scan failed ({exc})")
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
        except Exception:
            pass

    # Determine port from env config (use structured defaults for non-env values)
    if engine_name == "webui":
        port = int(env_config.get("QUICKROBOT_WEBUI_PORT") or str(get_port_default("quickrobot-webui")))
    elif engine_name == "mcp":
        port = int(env_config.get("QUICKROBOT_MCP_PORT") or str(get_port_default("quickrobot-mcp")))
    elif engine_name == "scheduler":
        port = 0  # Scheduler doesn't bind a port
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
                print(f"[qr] scheduler: found stale process (pid={spid}, ppid={ppid}), killing")
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
            print(f"[qr] {engine_name}: orphaned process detected (pid={old_pid}), killing and restarting")
            _kill_orphaned_process(old_pid, engine_name)
            try:
                update_instance(db_path, inst_id, pid_last_known=None)
            except Exception:
                pass
        else:
            # True existing child — skip
            try:
                transition_state(db_path, inst_id, "deployed")
            except Exception:
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
        except Exception:
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
        print(f"[qr] ENV WHITELIST OK: {_test_key}={env_config[_test_key]} NOT in child env (whitelist working)")
    elif _test_key in env:
        print(f"[qr] ENV WHITELIST FAIL: {_test_key} leaked into child env")
    # Log env var count for comparison
    os_env_count = len(os.environ)
    child_env_count = len(env)
    if child_env_count < os_env_count:
        print(f"[qr] ENV: subprocess env reduced {os_env_count} → {child_env_count} keys (was copy, now whitelist)")

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
            transition_state(db_path, inst_id, "deployed")
        except Exception:
            pass

    _log_lifecycle(engine_name, "start", {"pid": new_pid, "port": port, "api_host": api_host, "api_port": api_port})
    return {"action": "start", "port": port, "pid": new_pid, "status": "started", "engine": engine_name}


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
        except Exception:
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
        except Exception:
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
        except Exception:
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
        print(f"[qr] {engine_name} restart: old PID {old_pid} didn't exit within {timeout}s, force killing")
        try:
            import psutil
            if _get_pid_status(old_pid):
                psutil.Process(old_pid).kill()
                time.sleep(1)
        except Exception:
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
    except Exception:
        pass

    pid = inst.get("pid_last_known") if inst else None
    if pid and _get_pid_status(pid):
        return pid

    # Stale PID — clear cached value and auto-restart (only once to avoid storm)
    if not _retried and pid:
        try:
            update_instance(db_path, inst_id, pid_last_known=None)
            print(f"[qr] Stale PID for {engine_name} ({pid}) cleared, restarting...")
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
            print(f"[qr] WARNING: EIO prevention restart failed for {engine_name}: {exc}")

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
    env["QUICKROBOT_API_HOST"] = str(api_host)
    env["QUICKROBOT_API_PORT"] = str(api_port)
    # Operational mode — ensures subprocess always knows its mode regardless of _CONFIG import timing
    env["QUICKROBOT_PB_MODE"] = os.environ.get("QUICKROBOT_PB_MODE", "prod")
    # Debug/logging — each subprocess reads its own level from env
    env["QUICKROBOT_CONSOLE_DEBUG_LEVEL"] = env_config.get("QUICKROBOT_CONSOLE_DEBUG_LEVEL", "")
    env["QUICKROBOT_ANSIBLE_LOG_LEVEL"] = env_config.get("QUICKROBOT_ANSIBLE_LOG_LEVEL", "errors")
    # Log path — used by health check for FATAL exit logging
    env["QUICKROBOT_LOG_PATH"] = get_engine_log_path(engine_name)

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
            except Exception:
                pass
            return env_config.get(env_key, "false")

        # Set env vars for subprocess — names must match qr_mcp_server.py expectations
        env["QUICKROBOT_MCP_READ"] = _resolve_mcp_flag("mcp_allow_reads", "QUICKROBOT_MCP_READ")
        env["QUICKROBOT_MCP_WRITE"] = _resolve_mcp_flag("mcp_allow_writes", "QUICKROBOT_MCP_WRITE")
        env["QUICKROBOT_MCP_FULLPROXY"] = _resolve_mcp_flag("mcp_allow_proxy", "QUICKROBOT_MCP_FULLPROXY")

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

    print(f"[qr] Health check starting: {_api_url} (interval={check_interval}s, retries={max_retries}, retry_delay={retry_delay}s)", flush=True)
    # Startup grace period: give Flask time to bind and start accepting connections.
    import time as _time_mod
    _time_mod.sleep(10)

    while True:
        try:
            _resp = _requests_lib.get(_api_url, timeout=10)
            if _resp.status_code == 200 and _resp.json().get("status") == "ok":
                if _consecutive_failures > 0:
                    print(f"[qr] Health check recovered after {_consecutive_failures} failure(s)", flush=True)
                _consecutive_failures = 0
                _wait = check_interval  # Normal interval after recovery
            else:
                _consecutive_failures += 1
                print(f"[qr] Health check failed (attempt {_consecutive_failures}): HTTP {_resp.status_code}", flush=True)
                _wait = retry_delay  # Short delay between retries

        except _requests_lib.ConnectionError as _e:
            _consecutive_failures += 1
            print(f"[qr] Health check connection error (attempt {_consecutive_failures}): {_e}", flush=True)
            _wait = retry_delay
        except _requests_lib.Timeout as _e:
            _consecutive_failures += 1
            print(f"[qr] Health check timeout (attempt {_consecutive_failures}): {_e}", flush=True)
            _wait = retry_delay
        except Exception as _e:
            _consecutive_failures += 1
            print(f"[qr] Health check error (attempt {_consecutive_failures}): {_e}", flush=True)
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
            except Exception:
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


# Import _CONFIG at module level for DB path access
try:
    from qr_api import _CONFIG
except ImportError:
    _CONFIG = {"db_path": os.path.join(os.getcwd(), "data", "quickrobot.db")}
