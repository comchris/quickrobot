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
import subprocess
import sys
import time

from lib.qr_engine_ids import get_port_default, get_name_by_id, QR_FORBIDDEN_HOSTS

# MCP flag fallback defaults — must be explicitly set in .quickrobot.env
_MCP_READ_FALLBACK = "true"
_MCP_WRITE_FALLBACK = "false"
_MCP_FULLPROXY_FALLBACK = "false"


def _mcp_binary_exists():
    """Check if the MCP server binary exists on disk.

    The MCP binary path is configured in engine_configs table for engine
    type 'quickrobot-mcp' with key 'binary_path'. If not set, falls back
    to known default paths.

    Returns:
        True if binary exists, False otherwise.
    """
    # Try known default paths
    candidates = [
        "/opt/quickrobot/build/bin/mcp-server",
        "/usr/local/bin/mcp-server",
    ]
    # Also check if there's a config_override or engine_config pointing to it
    try:
        from db.sqlite import pool as _pool
        from quickrobot import _CONFIG
        with _pool(_CONFIG.get("db_path", "data/quickrobot.db")) as conn:
            row = conn.execute(
                "SELECT value FROM engine_configs WHERE engine_type_id = (SELECT id FROM engine_types WHERE name='quickrobot-mcp') AND key = 'binary_path'"
            ).fetchone()
            if row and row["value"]:
                candidates.insert(0, row["value"])
    except Exception:
        pass  # DB not ready yet, use known defaults

    for path in candidates:
        if os.path.isfile(path):
            return True
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
            f"QUICKROBOT_WEBUI_BEARER_TOKEN, QUICKROBOT_MCP_HOST, "
            f"QUICKROBOT_MCP_PORT, QUICKROBOT_MCP_BEARER_TOKEN"
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

    # Required int keys (ports)
    port_keys = {
        "QUICKROBOT_API_PORT": 8040,
        "QUICKROBOT_WEBUI_PORT": 8041,
        "QUICKROBOT_MCP_PORT": 8042,
    }
    for key, default in port_keys.items():
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
        return ("127.0.0.1", False)

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
        token = env_config.get("QUICKROBOT_WEBUI_BEARER_TOKEN", "")

        webui_path = os.path.join(os.getcwd(), "quickrobot_webui.py")

        cmd = [
            sys.executable, webui_path,
            "--host", host,
            "--port", str(port),
            "--api-host", api_host,
            "--api-port", str(api_port),
        ]
        if token:
            cmd.extend(["--api-token", token])
        return cmd

    elif engine_name == "mcp":
        host = env_config["QUICKROBOT_MCP_HOST"]
        port = env_config.get("QUICKROBOT_MCP_PORT") or str(get_port_default("quickrobot-mcp"))
        # Strict host check — must not be a forbidden wildcard
        if host in QR_FORBIDDEN_HOSTS:
            print(f"[qr] FATAL: MCP bind host is '{host}' — {QR_FORBIDDEN_HOSTS}")
            sys.exit(1)
        token = env_config.get("QUICKROBOT_MCP_BEARER_TOKEN", "")

        mcp_server_path = os.path.join(os.getcwd(), "engine", "qr_mcp_server.py")

        cmd = [
            sys.executable, mcp_server_path,
            "--host", host,
            "--port", str(port),
            "--api-host", api_host,
            "--api-port", str(api_port),
        ]
        if token:
            cmd.extend(["--api-token", token])
        if extra_flags:
            cmd.extend(extra_flags)
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
    from datetime import datetime as _dt

    timestamp = _dt.now().strftime("%Y-%m-%dT%H:%M:%S")
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
    except Exception:
        pass  # Non-critical — logging should not break the runner

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
    inst_id = None
    if engine_name == "webui":
        inst_id = 2  # Hardcoded system engine ID
    elif engine_name == "mcp":
        inst_id = 3

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
    else:
        port = None

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

    popen_kwargs = {
        "stdout": None,  # Let subprocess handle output (or DEVNULL if preferred)
        "stderr": None,
        "env": env,
        "cwd": os.getcwd(),
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
    except OSError as exc:
        _log_lifecycle(engine_name, "start", {"error": str(exc)})
        return {"error": f"Failed to start {engine_name}: {exc}", "action": "start", "engine": engine_name}

    new_pid = proc.pid
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
    inst_id = None
    if engine_name == "webui":
        inst_id = 2
    elif engine_name == "mcp":
        inst_id = 3

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
    inst_id = None
    if engine_name == "webui":
        inst_id = 2
    elif engine_name == "mcp":
        inst_id = 3

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


def get_system_engine_pid(engine_name, env_config):
    """Lookup PID for a system engine.

    Reads pid_last_known from DB instance record. Verifies process exists via psutil.
    Returns None if PID not found or process dead.

    Args:
        engine_name: "webui" or "mcp"
        env_config: Dict from load_env_config()

    Returns:
        PID (int) or None
    """
    from db.adapters.instances import get_instance

    # Determine instance ID
    inst_id = None
    if engine_name == "webui":
        inst_id = 2
    elif engine_name == "mcp":
        inst_id = 3

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
    return None


def build_subprocess_env(engine_name, env_config, api_host, api_port, instance_config=None, is_system_managed=True):
    """Build a whitelisted subprocess environment dict for system engines.

    Consolidated builder replaces three independent inline dicts in:
      - lib_system_engine.py::start_system_engine() (L537-553)
      - engine/quickrobot_webui/__init__.py::execute() (L266-278)
      - engine/quickrobot_mcp/__init__.py::execute() (L432-457)

    Layer 1: Base whitelist — always merged (PATH, HOME, LANG, LC_ALL, API_HOST, API_PORT, API_TOKEN)
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
        QR_ENV_MCP_HOST, QR_ENV_MCP_PORT, QR_ENV_MCP_ALLOWED_HOSTS,
        QR_ENV_MCP_DISABLE_DNS_REBINDING, QR_ENV_MCP_ALLOW_READS,
        QR_ENV_MCP_ALLOW_WRITES, QR_ENV_MCP_ALLOW_PROXY,
    )

    env = {}

    # === LAYER 1: Base whitelist (always present) ===
    env[QR_ENV_PATH] = os.environ.get(QR_ENV_PATH, "")
    env[QR_ENV_HOME] = os.environ.get(QR_ENV_HOME, "")
    env[QR_ENV_LANG] = os.environ.get(QR_ENV_LANG, "en_US.UTF-8")
    env[QR_ENV_LC_ALL] = os.environ.get(QR_ENV_LC_ALL, "en_US.UTF-8")
    env[QR_ENV_API_BEARER_TOKEN] = env_config.get("QUICKROBOT_API_BEARER_TOKEN", "")
    env[QR_ENV_API_HOST] = str(api_host)
    env[QR_ENV_API_PORT] = str(api_port)

    # === LAYER 2: Engine-specific extras ===
    if engine_name == "webui":
        env[QR_ENV_WEBUI_HOST] = env_config.get("QUICKROBOT_WEBUI_HOST", str(api_host))
        env[QR_ENV_WEBUI_PORT] = str(env_config.get("QUICKROBOT_WEBUI_PORT", api_port))

    elif engine_name == "mcp":
        env[QR_ENV_PYTHONPATH] = os.getcwd()
        env[QR_ENV_MCP_HOST] = env_config["QUICKROBOT_MCP_HOST"]
        env[QR_ENV_MCP_PORT] = str(env_config.get("QUICKROBOT_MCP_PORT", ""))

        def _mcp_flag(key, fallback):
             """Resolve MCP flag: engine_configs (runtime) > .quickrobot.env (system default)."""
             try:
                 from db.adapters.configs import get_engine_config as _gec
                 et_id = None
                 if engine_name == "mcp":
                     from lib.qr_engine_ids import QR_ENGINE_QUICKROBOT_MCP
                     et_id = QR_ENGINE_QUICKROBOT_MCP
                 if et_id:
                     db_path = _CONFIG.get("db_path") if "_CONFIG" in globals() else os.path.join(os.getcwd(), "data", "quickrobot.db")
                     row = _gec(db_path, et_id, key)
                     if row and row.get("value"):
                         return str(row["value"])
             except Exception:
                 pass
             return env_config.get(f"QUICKROBOT_MCP_{key.upper()}", fallback)

        env[QR_ENV_MCP_ALLOW_READS] = _mcp_flag("allow_reads", _MCP_READ_FALLBACK)
        env[QR_ENV_MCP_ALLOW_WRITES] = _mcp_flag("allow_writes", _MCP_WRITE_FALLBACK)
        env[QR_ENV_MCP_ALLOW_PROXY] = _mcp_flag("allow_proxy", _MCP_FULLPROXY_FALLBACK)

        allowed_hosts = env_config.get("QUICKROBOT_MCP_ALLOWED_HOSTS", "")
        if allowed_hosts:
            env[QR_ENV_MCP_ALLOWED_HOSTS] = allowed_hosts
        disable_dns = env_config.get("QUICKROBOT_MCP_DISABLE_DNS_REBINDING", "")
        if disable_dns:
            env[QR_ENV_MCP_DISABLE_DNS_REBINDING] = disable_dns

    # === LAYER 3: Per-instance env_vars (subprocess engine only) ===
    if not is_system_managed and instance_config:
        co = instance_config if isinstance(instance_config, dict) else {}
        user_env_vars = co.get("env_vars", {})
        if isinstance(user_env_vars, dict):
            env.update(user_env_vars)

    return env


# Import _CONFIG at module level for DB path access
try:
    from quickrobot import _CONFIG
except ImportError:
    _CONFIG = {"db_path": os.path.join(os.getcwd(), "data", "quickrobot.db")}
