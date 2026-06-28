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
import atexit

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


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
