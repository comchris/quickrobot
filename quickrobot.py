#!/usr/bin/env python3
"""quickrobot — Quickrobot REST API Server (entry point).

Delegates to lib/lib_startup_pipeline.run_startup() which handles:
  - CLI parsing, .env loading, seed checksum validation
  - DB creation/backup, migrations, seed import, engine discovery
  - Playbook integrity verification
  - PID management and port binding

After startup, launches the Flask app registered in ./quickrobot/ package.
"""

import sys
import os
import re
import subprocess as _subp
import atexit

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Source .quickrobot.env into os.environ so this process (and all its children)
# inherit env vars like PYTHONPYCACHEPREFIX, CONSOLE_DEBUG_LEVEL, etc.
# This prevents local __pycache__ dirs when run directly from the shell.
_env_file = os.path.join(_project_root, ".quickrobot.env")
if os.path.isfile(_env_file):
    with open(_env_file, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip()
            if len(_v) >= 2 and (( _v[0] == '"' and _v[-1] == '"') or (_v[0] == "'" and _v[-1] == "'")):
                _v = _v[1:-1]
            os.environ[_k] = _v
del _f, _line, _k, _v, _env_file  # clean up loop vars


def _pid_file_path():
    """Get the path to the PID file."""
    # Read db_path from .quickrobot.env or use default
    try:
        from lib.lib_system_engine import load_env_config as _lec
        env_cfg = _lec(os.getcwd())
        db_dir = os.path.dirname(env_cfg.get("QUICKROBOT_DB_PATH", "data/quickrobot.db"))
    except (FileNotFoundError, KeyError):
        db_dir = "data"
    return os.path.join(db_dir, "quickrobot.pid")


def _remove_pid_file():
    """Remove PID file on exit."""
    try:
        os.remove(_pid_file_path())
    except OSError:
        pass


atexit.register(_remove_pid_file)


if __name__ == "__main__":
    # Root guard — refuse to run as root (non-interactive HTTP server)
    if os.getuid() == 0:
        print("this robot won't run as root", file=sys.stderr)
        sys.exit(1)

    # Pre-flight: detect stale quickrobot.py processes from prior sessions.
    # A leftover --mode dev-update (or any other long-running mode) would still
    # hold the API port and serve requests — a fresh start would conflict.
    # Skip this check for exit mode (no port binding).
    _exit_mode = "--mode" in sys.argv and "exit" in sys.argv
    if not _exit_mode:
        try:
            _api_port = int(os.environ.get("QUICKROBOT_API_PORT", "8039"))
        except (ValueError, TypeError):
            _api_port = 8039

        _stale_found = []
        try:
            _ps_out = _subp.run(["ps", "aux"], capture_output=True, text=True, timeout=5).stdout
            _my_pid = os.getpid()
            for _line in _ps_out.splitlines():
                _parts = _line.split()
                if len(_parts) < 2:
                    continue
                try:
                    _lp = int(_parts[1])
                except ValueError:
                    continue
                if _lp == _my_pid:
                    continue
                if "quickrobot.py" not in _line:
                    continue
                # Verify the stale process actually holds the API port
                try:
                    _ss_out = _subp.run(
                        ["ss", "-tlnp"], capture_output=True, text=True, timeout=5
                    ).stdout
                    for _sline in _ss_out.splitlines():
                        if f":{_api_port}" in _sline and "LISTEN" in _sline:
                            _pid_match = re.search(r"pid=(\d+)", _sline)
                            if _pid_match and int(_pid_match.group(1)) == _lp:
                                _cmd = " ".join(_parts[10:]) if len(_parts) > 10 else _line
                                _stale_found.append(
                                    f"  pid={_lp} cmd={_cmd!r}"
                                )
                except FileNotFoundError:
                    # ss unavailable — trust the ps match
                    _stale_found.append(
                        f"  pid={_lp} cmd={(_parts[10:] if len(_parts) > 10 else [])!r}"
                    )
        except FileNotFoundError:
            pass  # ps/ss not available, skip pre-flight

        if _stale_found:
            print("[qr] FATAL: Stale quickrobot.py process(es) detected:")
            for _s in _stale_found:
                print(f"[qr]{_s}")
            print(
                "[qr] A previous session may not have exited cleanly. "
                "Kill the stale process(es) and restart."
            )
            sys.exit(1)

    # Import app/register_routes BEFORE run_startup() so quickrobot resolves to PACKAGE
    # (After split, quickrobot/__init__.py owns Flask app + route registration)
    from qr_api import app, register_routes
    from lib.lib_constants import VERSION
    
    from lib.lib_startup_pipeline import run_startup
    
    # Print early banner so API startup appears before WebUI/MCP subprocess starts
    from lib.qr_engine_ids import QUICKROBOT_VERSION as _VERSION
    print(f"[qr] {_VERSION} — Quickrobot API server starting...", flush=True)
    
    # Run the full startup pipeline (populates _CONFIG via package-level reference)
    config = run_startup()
    
    # Register routes (idempotent — already registered at package import time)
    register_routes(app)
    
    print(f"[qr] quickrobot API server starting on {config['host']}:{config['api_port']}")
    print(f"[qr] version={VERSION} mode={config.get('pb_mode', 'prod')}")
    
    # Exit mode: system engines already started by run_startup(), skip Flask loop
    if config.get("exit_mode"):
        sys.exit(0)
    
    # Start system engine subprocesses in a daemon thread AFTER Flask binds.
    # This ensures the API port is listening before subprocesses try to connect.
    from lib.lib_startup_pipeline import deferred_start_system_engines as _dsse
    import threading as _threading
    _db_path = config.get("deferred_db_path")
    _qr_env = config.get("deferred_qr_env", {})
    _webui_as = config.get("deferred_webui_autostart", True)
    _mcp_as = config.get("deferred_mcp_autostart", False)
    if _db_path and (_webui_as or _mcp_as or True):  # scheduler always runs
        _threading.Thread(
            target=_dsse,
            args=(_db_path, _qr_env, _webui_as, _mcp_as),
            daemon=True,
            name="system-engines-start",
        ).start()
    
    app.run(
        host=config["host"],
        port=config["api_port"],
        debug=False,
    )
