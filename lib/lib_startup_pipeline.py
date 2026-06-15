"""quickrobot startup pipeline — full initialization sequence.

This module contains the complete startup routine extracted from quickrobot.py:
  - parse_args()             : CLI argument parsing
  - phase0-7                 : Individual initialization phases
  - run_startup()            : Orchestrator that runs all phases in order

All functions reference _CONFIG from quickrobot.py (same mutable dict object,
not a copy). No circular imports — this module only reads from quickrobot.

Dependencies:
  - lib.lib_startup      : load_system_engine_config, backup_database, import_seed_file
  - lib.qr_engine_ids    : engine constants and port defaults (single source of truth)
  - lib.lib_constants    : legacy re-exports (backward compat)
  - lib.lib_system_engine: load_env_config, pre_validate_seed_checksum
  - db.*                 : DB adapters and pool
  - engine               : Engine discovery and registration
  - quickrobot           : _CONFIG dict, verify_playbook_integrity
"""

import argparse
import os
import sys
import socket
import threading

from quickrobot import _CONFIG, _project_root  # Same mutable dict, not a copy
from lib.qr_engine_ids import QR_FORBIDDEN_HOSTS  # Forbidden bind hosts for system engines

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def parse_args():
    """Parse command-line arguments.

    Returns:
        Namespace with parsed arguments.
    """
    parser = argparse.ArgumentParser(description="quickrobot LAN Controller API")
    parser.add_argument("--port", type=int, default=None,
                        help="API server port (default: from .quickrobot.env)")
    parser.add_argument("--db-path", default="./data/quickrobot.db",
                        help="Path to SQLite database")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--init", action="store_true",
                        help="Initialize fresh database")
    parser.add_argument("--replace", action="store_true",
                        help="Replace existing instance (kill old PID)")
    parser.add_argument("--webui-detach", action="store_true",
                        help="WebUI survives API death")
    parser.add_argument("--no-webui", action="store_true",
                        help="Disable WebUI auto-start")
    parser.add_argument("--mode", default="prod",
                        choices=["dev", "prod", "dev-update"],
                        help="Operation mode (default: prod)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Strict .env validation for system engines
# ---------------------------------------------------------------------------


def _validate_system_engine_env(env_cfg, engine_name, required_keys):
    """Validate that required .env keys exist and are non-empty.

    Args:
        env_cfg: Dict from parsed .quickrobot.env
        engine_name: Human-readable engine name (for error messages)
        required_keys: List of key names that must be present and non-empty

    Raises:
        SystemExit on missing/empty required keys.
    """
    for key in required_keys:
        value = env_cfg.get(key)
        if not value or (isinstance(value, str) and value.strip() == ""):
            print(f"[qr] FATAL: {engine_name} requires '{key}' in .quickrobot.env")
            print(f"[qr]   Current value: '{value}'" if value else f"[qr]   Key is missing or empty")
            sys.exit(1)


def _validate_system_engine_bind(env_cfg, engine_name):
    """Validate that system engine bind address is not 0.0.0.0.

    Args:
        env_cfg: Dict from parsed .quickrobot.env
        engine_name: Human-readable engine name

    Raises:
        SystemExit if host is 0.0.0.0, ::, or ::0.
    """
    host_key = {
        "API": "QUICKROBOT_API_HOST",
        "WebUI": "QUICKROBOT_WEBUI_HOST",
        "MCP": "QUICKROBOT_MCP_HOST",
    }.get(engine_name, "")

    if not host_key:
        return  # Unknown engine name — skip bind check

    host = env_cfg.get(host_key, "")

    if host in QR_FORBIDDEN_HOSTS:
        print(f"[qr] FATAL: {engine_name} bind host is '{host}' — must be a specific address")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Phase 0 — Mode flags
# ---------------------------------------------------------------------------


def phase0_mode_flags(args):
    """Set pb_mode based on CLI --mode argument.

    Single source of truth: _CONFIG["pb_mode"] (never use _DEV_MODE).
    """
    if args.mode == "prod":
        _CONFIG["pb_mode"] = "prod"
        print("[qr] Production mode: auto-playbook import disabled, strict integrity")
    elif args.mode == "dev-update":
        _CONFIG["pb_mode"] = "dev-update"
        print("[qr] Dev-update mode: auto-import enabled, sync checksums on mismatch")
    elif args.mode == "dev":
        _CONFIG["pb_mode"] = "dev"
        print("[qr] Dev mode: auto-import enabled, alert integrity changes")
    else:  # unexpected value (argparse choices should prevent this)
        _CONFIG["pb_mode"] = "prod"
        print(f"[qr] WARNING: unexpected --mode={args.mode}, defaulting to prod")


# ---------------------------------------------------------------------------
# Phase 1 — Config resolution
# ---------------------------------------------------------------------------


def phase1_config(args):
    """Resolve database path, API port, and host from CLI + .env.

    Returns:
        str: resolved db_path
    """
    _CONFIG["db_path"] = os.path.join(_project_root, args.db_path)

    # Use CLI port if explicitly provided, else fall back to .env file value
    _cli_port = getattr(args, "port", None)
    if not _cli_port:
        try:
            from lib.lib_system_engine import load_env_config as _load_env
            _env_cfg = _load_env(os.getcwd())
            _cli_port = _env_cfg.get("QUICKROBOT_API_PORT")
        except FileNotFoundError:
            from lib.qr_engine_ids import get_port_default
            _cli_port = get_port_default("quickrobot-api")
    _CONFIG["_last_port"] = int(_cli_port) if _cli_port else (get_port_default("quickrobot-api") if '_cli_port' in dir() else 8040)
    _CONFIG["api_port"] = _CONFIG["_last_port"]
    _CONFIG["host"] = args.host

    # Pass bind host and API port to subprocesses via env var
    os.environ["MCP_BIND_HOST"] = args.host
    os.environ["MCP_API_PORT"] = str(_CONFIG["api_port"])

    # Create data directory if it does not exist
    db_dir = os.path.dirname(_CONFIG["db_path"])
    if db_dir and not os.path.isdir(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    return _CONFIG["db_path"]


# ---------------------------------------------------------------------------
# Phase 2 — Pre-flight checks (mode-branching)
# ---------------------------------------------------------------------------


def phase2_preflight(args, env_cfg):
    """Run pre-flight checks based on mode.

    For --init: validates seed checksum, hard exit on mismatch.
    For non-init: stores env config for later use by _init_app.

    Args:
        args: CLI arguments namespace.
        env_cfg: Pre-loaded env config (None = not yet loaded).

    Returns:
        dict: loaded env configuration.
    """
    from lib.lib_system_engine import load_env_config as _load_env_cfg
    from lib.lib_startup import pre_validate_seed_checksum as _pre_val_seed
    from lib.qr_engine_ids import get_port_default as _get_api_port

    try:
        _env_cfg = _load_env_cfg(os.getcwd())
    except FileNotFoundError as exc:
        print(f"[qr] FATAL: {exc}")
        sys.exit(1)
    except SystemExit:
        # _validate_env_config already printed error and exited
        raise

    if args.init:
        _pre_val_seed(_env_cfg, init_mode=True)
        _CONFIG["init_mode"] = True

    return _env_cfg


# ---------------------------------------------------------------------------
# Phase 3 — Database handling
# ---------------------------------------------------------------------------


def phase3_db_handling(args):
    """Handle --init backup+delete or verify DB existence.

    Args:
        args: CLI arguments namespace.
    """
    import shutil
    from datetime import datetime

    db_path_exists = os.path.isfile(_CONFIG["db_path"])

    if args.init:
        if db_path_exists:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{_CONFIG['db_path']}_backup_{ts}.db"
            shutil.copy2(_CONFIG["db_path"], backup_name)
            print(f"[qr] Backed up existing DB to {backup_name}")
            os.remove(_CONFIG["db_path"])
            print("[qr] Removed old DB — fresh init in progress")
    elif not db_path_exists:
        print(f"[qr] Database not found at {_CONFIG['db_path']}")
        print("[qr] Start with --init to create a new database, or copy an existing DB file into place.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Phase 4 — PID + port checks
# ---------------------------------------------------------------------------


def phase4_pid_port():
    """Manage PID file and verify port availability."""
    from quickrobot import _check_pid_file, _write_pid_file, _kill_existing

    if _CONFIG.get("replace"):
        _kill_existing(port=_CONFIG["api_port"])
    else:
        pid, msg = _check_pid_file()
        if pid:
            print(msg)
            sys.exit(1)

    _write_pid_file()

    # Pre-check port availability before heavy init work
    try:
        _check_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _check_sock.bind((_CONFIG.get("host", "0.0.0.0"), _CONFIG["api_port"]))
        _check_sock.close()
    except OSError:
        _check_sock.close()
        print(f"FATAL: Port {_CONFIG['api_port']} is already in use by another process. Exiting.",
              file=sys.stderr)
        sys.exit(1)

    # Pass CLI flags to _CONFIG so _init_app() can respect them
    # (re-read from args since they're not persisted)


# ---------------------------------------------------------------------------
# Phase 5 — Init: sys.modules alias + full _init_app() body
# ---------------------------------------------------------------------------

# These are the functions moved from quickrobot.py. They reference _CONFIG
# (shared mutable dict) and import from db/adapters/engine at runtime.


def _auto_provision_system_instances():
    """Auto-create system-managed engine instances on startup.

    Checks for quickrobot-api and quickrobot-webui instances in the DB.
    Creates them if they don't exist. Always uses node_id=1 (localhost).
    Uses hardcoded instance IDs via _SYSTEM_INSTANCE_ID_MAP to prevent drift.
    """
    from db.adapters.engine_types import get_engine_type, list_engine_types as _let
    from db.adapters.instances import create_instance, assign_port, get_instance as _gi
    from db.adapters.configs import set_engine_config as _sec, get_engine_config as _gec
    from lib.qr_engine_ids import (
        QR_ENGINE_API,
        QR_FORBIDDEN_HOSTS,
    )
    from quickrobot import _SYSTEM_INSTANCE_ID_MAP

    db_path = _CONFIG["db_path"]

    # Ensure node 1 (localhost) exists — system-managed instances always use this
    try:
        from db.adapters.nodes import list_nodes as _ln, get_node as _gn, add_node as _an
        existing = _gn(db_path, 1)
        if existing is None:
            # Resolve real hostname instead of hardcoding "localhost"
            from lib.lib_local_inventory import gather_local_hostname
            actual_host = gather_local_hostname()
            node_id = _an(db_path, name=actual_host, hostname=actual_host,
                          transport="ansible")["id"]
        else:
            node_id = 1
            # Update stale "localhost" name/hostname with real hostname
            from lib.lib_local_inventory import gather_local_hostname as _glh
            actual_host = _glh()
            if not existing.get("name") or not existing.get("hostname") or existing.get("name") == "localhost" or existing.get("hostname") == "localhost":
                from db.adapters.nodes import update_node as _un
                _un(db_path, 1, name=actual_host, hostname=actual_host)
                print(f"[qr] localhost node renamed to '{actual_host}'", flush=True)

        # Auto-discover localhost hardware on startup
        try:
            from lib.lib_local_inventory import gather_local_inventory
            from db.adapters.nodes import update_local_host_inventory as _ulhi
            inv = gather_local_inventory()
            if all(v is not None for v in [inv.get("cpu_cores"), inv.get("ram_mb"),
                                           inv.get("os")]):
                _ulhi(db_path, node_id, inv)
                print(f"[qr] localhost: {inv['cpu_cores']} CPU, {inv['ram_mb']}MB RAM, "
                      f"{inv['os']}, {inv.get('fs_free_gb', '?')}GB free", flush=True)
            else:
                missing = [k for k in ("cpu_cores", "ram_mb", "os") if inv.get(k) is None]
                print(f"[qr] localhost partial inventory (missing: {','.join(missing)})",
                      flush=True)
        except Exception as exc:
            print(f"[qr] localhost inventory gather failed: {exc}", flush=True)
    except Exception:
        node_id = 1

    engine_names = ["quickrobot-api", "quickrobot-webui", "quickrobot-mcp"]

    for eng_name in engine_names:
        # Ensure engine type exists in DB
        et_row = None
        for et in _let(db_path, enabled_only=False):
            if et["name"] == eng_name:
                et_row = et
                break

        if et_row is None:
            # Auto-register the engine type
            from db.adapters.engine_types import add_engine_type as _ae
            capabilities_str = '{"max_instances": 1, "supports_models": false, "supports_presets": false}'
            try:
                et_id = _ae(db_path, name=eng_name, display_name=eng_name.replace("-", " ").title(),
                            module_path=f"engine.{eng_name}", capabilities=capabilities_str)["id"]
            except Exception:
                continue
        else:
            et_id = et_row["id"]

        # Check if instance already exists
        existing = _gi(db_path, 0)  # placeholder
        # List instances for this engine type to find system-managed ones
        from db.adapters.instances import list_instances as _li
        existing_insts = _li(db_path, engine_type_id=et_id)
        sys_managed = [i for i in existing_insts if i.get("system_managed", 0)]

        # Update transport + restore running state on existing system-managed instances
        from db.adapters.instances import update_instance as _ui
        changed = False
        for inst in sys_managed:
            if inst.get("transport") != "ssh":
                _ui(db_path, inst["id"], transport="ssh")
                changed = True
                print(f"Updated system-managed instance {inst['name']} transport to 'ssh'")
            # Restore running state on startup — respect autostart config for MCP
            if inst.get("state") not in ("running", "starting"):
                # For MCP: read from .env (default true), DB override if explicitly set via API
                if eng_name == "quickrobot-mcp":
                    qr_env = _CONFIG.get("qr_env_config", {})
                    auto_start_val = str(qr_env.get("QUICKROBOT_MCP_AUTOSTART", "true")).lower() in ("true", "1")
                    db_as = _gec(db_path, et_id, "mcp_autostart") or {}
                    if db_as:
                        auto_start_val = str(db_as.get("value", "false")).lower() in ("true", "1")
                    if not auto_start_val:
                        # Don't force MCP to running state — let user start manually
                        continue
                _ui(db_path, inst["id"], state="running")
                changed = True
                print(f"Restored system-managed instance {inst['name']} state to 'running'")
        if changed:
            print(f"Updated system-managed instance {inst['name']} (ID {inst['id']})")

        if not sys_managed:
            inst_name = f"system-{eng_name}"
            # Use hardcoded ID from map to ensure consistent numbering (1=api, 2=webui, 3=mcp)
            target_id = _SYSTEM_INSTANCE_ID_MAP.get(eng_name)
            try:
                if target_id:
                    # Insert with explicit ID to prevent drift from deleted user instances
                    from db.sqlite import pool as _pool
                    with _pool(db_path) as conn:
                        # Delete any existing row at this ID to avoid constraint error
                        conn.execute("DELETE FROM instances WHERE id = ?", (target_id,))
                        cursor = conn.execute(
                            """INSERT INTO instances (id, name, engine_type_id, node_id,
                               config_override, system_managed, start_on_boot, start_after_deploy, created_at)
                               VALUES (?, ?, ?, ?, ?, 1, 'false', 0, ?)""",
                            (target_id, inst_name, et_id, node_id, "{}",
                             __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')),
                        )
                    inst = {"id": target_id, "name": inst_name}
                else:
                    inst = create_instance(db_path, name=inst_name, engine_type_id=et_id,
                                   node_id=node_id, system_managed=1, start_on_boot="false")
                print(f"Auto-provisioned system-managed instance: {inst_name} (ID {inst['id']})")
            except Exception as exc:
                print(f"Warning: Failed to create system-managed instance {inst_name}: {exc}")
                inst = None

        # Register engine-level config defaults — runs on every startup for existing or new instances
        # Read env config (set by _load_system_engine_config) for env-file values
        qr_env = _CONFIG.get("qr_env_config", {})
        webui_host = qr_env.get("QUICKROBOT_WEBUI_HOST") or _CONFIG["host"]
        mcp_api_host = qr_env.get("QUICKROBOT_MCP_HOST") or _CONFIG["host"]
        if eng_name == "quickrobot-webui":
            _validate_system_engine_env(qr_env, "WebUI", ["QUICKROBOT_WEBUI_HOST", "QUICKROBOT_WEBUI_PORT"])
            _validate_system_engine_bind(qr_env, "WebUI")

            from db.adapters.instances import update_instance as _ui_webui
            try:
                web_port = int(qr_env["QUICKROBOT_WEBUI_PORT"])
            except (KeyError, ValueError):
                raise KeyError("QUICKROBOT_WEBUI_PORT not in .quickrobot.env")
            # Find instance if not in current scope
            inst_to_update = inst
            if not inst_to_update and sys_managed:
                for i in sys_managed:
                    if i.get("name", "").endswith("webui"):
                        inst_to_update = i
                        break
            if inst_to_update:
                _ui_webui(db_path, inst_to_update["id"], port_assigned=web_port)
            _sec(db_path, et_id, "web_ui_host", webui_host, "Web UI bind address")
            _sec(db_path, et_id, "web_ui_port", str(web_port), "Default Web UI bind port")

        if eng_name == "quickrobot-mcp":
            _validate_system_engine_bind(qr_env, "MCP")
            from db.adapters.instances import update_instance as _ui_mcp
            try:
                mcp_port = int(qr_env["QUICKROBOT_MCP_PORT"])
            except (KeyError, ValueError):
                raise KeyError("QUICKROBOT_MCP_PORT not in .quickrobot.env")
            inst_to_update = inst
            if not inst_to_update and sys_managed:
                for i in sys_managed:
                    if i.get("name", "").endswith("mcp"):
                        inst_to_update = i
                        break
            if inst_to_update:
                _ui_mcp(db_path, inst_to_update["id"], port_assigned=mcp_port)
            _sec(db_path, et_id, "mcp_port", str(mcp_port), "Default MCP SSE bind port")
            _sec(db_path, et_id, "mcp_api_host", mcp_api_host, "API host for MCP tool calls (read from .quickrobot.env QUICKROBOT_MCP_HOST)")
            _sec(db_path, et_id, "mcp_allow_reads", "true", "Expose read-only tools (list_instances, list_nodes, etc.)")
            _sec(db_path, et_id, "mcp_allow_writes", "true", "Expose write tools (create_instance, deploy, start, stop)")
            _sec(db_path, et_id, "mcp_allow_proxy", "true", "Expose raw API proxy tool")
            _sec(db_path, et_id, "mcp_python_interpreter", "", "Python interpreter binary for MCP server subprocess (empty=auto-detect pipx venv then system python)")

        # Binding check (HDIR-G): warn if system engines or main process
        # are bound to 0.0.0.0 or IPv6 wildcard (::)
        from db.adapters.configs import get_engine_config as _gec
        for eng_name_check in engine_names:
            et_row_check = None
            for et in _let(db_path, enabled_only=False):
                if et["name"] == eng_name_check:
                    et_row_check = et
                    break
            if et_row_check is None:
                continue
            # MCP uses mcp_api_host; other engines use host
            host_key = "mcp_api_host" if eng_name_check == "quickrobot-mcp" else "host"
            host_cfg = _gec(db_path, et_row_check["id"], host_key)
            if host_cfg:
                h = str(host_cfg.get("value", ""))
                if h in QR_FORBIDDEN_HOSTS:
                    print(f"WARNING: {eng_name_check} bound to {h} (LAN-exposed). "
                          f"Verify this is intentional.")
        # Main process binding check
        main_host = _CONFIG.get("host", "127.0.0.1")
        if main_host in QR_FORBIDDEN_HOSTS:
            print(f"WARNING: API server bound to {main_host} (LAN-exposed). "
                  f"Verify this is intentional.")

    # Reserve IDs 1-99 for system instances — advance sequence to start user instances at 100
    try:
        from db.sqlite import pool as _pool
        with _pool(db_path) as conn:
            max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM instances").fetchone()[0]
            if max_id < 100:
                conn.execute("UPDATE sqlite_sequence SET seq = 99 WHERE name = 'instances'")
                # Force SQLite to use the new sequence on next INSERT
                conn.execute("INSERT INTO instances (id, name, engine_type_id, node_id) VALUES (100, '__temp__', ?, 1)", (QR_ENGINE_API,))
                conn.execute("DELETE FROM instances WHERE id = 100")
                print("[qr] Instance ID range: system=1-99, user>=100")
    except Exception:
        pass


def recover_stale_instances():
    """Recover instances stuck in configuring/deploying after server restart.

    When the server crashes or restarts while a background build is running,
    instances may be left in 'configuring' or 'deploying' state. This function
    scans for such instances and transitions them to appropriate error states.

    If the target node is still active, the instance is marked as 'build_error'
    so the user can retry the deploy. If the node is unreachable, it's marked
    as 'error'.
    """
    from db.sqlite import pool as _pool
    from db.adapters.instances import transition_state, list_instances as _li2
    from db.adapters.nodes import get_node as _gn
    import os as _os

    db_path = _CONFIG["db_path"]

    try:
        with _pool(db_path) as conn:
            rows = conn.execute(
                "SELECT id, node_id FROM instances WHERE state IN ('configuring', 'deploying')"
            ).fetchall()
    except Exception as _e:
        print(f"[qr] WARNING: recover_stale_instances query failed: {_e}")
        return

    print(f"[qr] DEBUG: recover_stale found {len(rows)} instances")
    be_count = 0
    for row in rows:
        inst_id, node_id = row
        try:
            nd = _gn(db_path, node_id)
        except Exception:
            nd = None

        if not nd or nd.get("status") != "active":
            try:
                transition_state(db_path, inst_id, "error")
            except Exception:
                pass
            continue

        # Node is active — was this mid-build? Check if cmake cache exists.
        # For safety, mark as build_error (user can retry deploy).
        node_short = (nd.get("hostname") or nd.get("name", "")).split(".")[0]
        cmake_cache = "/opt/quickrobot/llama.cpp/build/CMakeCache.txt"
        cache_exists = _os.path.exists(cmake_cache)

        try:
            transition_state(db_path, inst_id, "build_error")
            be_count += 1
        except Exception:
            pass

    if be_count:
        print(f"[qr] Recovered {be_count} stale instance(s) -> build_error")


def recover_subprocess_instances(db_path):
    """Recover subprocess instances with start_on_boot=true after API restart.

    Scans all subprocess instances, checks if their PID is still alive via psutil.
    If the process is dead and start_on_boot=true, auto-restarts the instance.
    If the process is alive but state != running, restores "running" state.
    If the process is dead and start_on_boot=false, sets state to "stopped".
    """
    from db.sqlite import pool as _pool
    from db.adapters.instances import list_instances as _li2, transition_state as _ts
    from quickrobot import QR_ENGINE_SUBPROCESS

    try:
        with _pool(db_path) as conn:
            rows = conn.execute(
                "SELECT id, engine_type_id, pid_last_known, start_on_boot, state "
                "FROM instances WHERE engine_type_id = ? AND pid_last_known IS NOT NULL",
                (QR_ENGINE_SUBPROCESS,),
            ).fetchall()
    except Exception as _e:
        print(f"[qr] WARNING: subprocess recovery query failed: {_e}")
        return
    recovered = 0
    for row in rows:
        inst_id, engine_type_id, pid, start_on_boot, state = row
        if not pid:
            continue

        # Check if process is alive
        try:
            import psutil as _psutil
            alive = False
            try:
                proc = _psutil.Process(pid)
                if proc.status() != "zombie":
                    alive = True
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass
        except ImportError:
            # psutil not available — assume alive (conservative)
            continue

        if alive:
            # Process alive — restore running state if needed
            # Subprocess engine: error→starting→running (no direct error→running)
            if state != "running":
                try:
                    _ts(db_path, inst_id, "starting")
                except Exception:
                    pass
                try:
                    _ts(db_path, inst_id, "running")
                    print(f"[qr] Recovered subprocess {inst_id} (PID {pid} alive, restored to 'running')")
                except Exception:
                    pass
        else:
            # Process dead — check autostart setting
            sob_val = start_on_boot or "false"
            if isinstance(sob_val, str):
                sob_bool = sob_val.lower() in ("true", "1", "yes")
            elif isinstance(sob_val, (int, float)):
                sob_bool = bool(sob_val)
            else:
                sob_bool = False

            if sob_bool:
                # Auto-restart — transition to valid state then start
                try:
                    # Try "stopped" first (works for deployed/running states)
                    _ts(db_path, inst_id, "stopped")
                except Exception:
                    pass  # State machine may not allow this from current state — execute() handles it
                try:
                    from engine.subprocess import QrSubprocessEngine
                    QrSubprocessEngine().execute(inst_id, "start", db_path)
                    recovered += 1
                    print(f"[qr] Auto-restarted subprocess {inst_id} (PID {pid})")
                except Exception as exc:
                    print(f"[qr] Failed to auto-restart subprocess {inst_id}: {exc}")
            else:
                # Just mark as stopped
                try:
                    _ts(db_path, inst_id, "stopped")
                    print(f"[qr] Subprocess {inst_id} (PID {pid}) dead, start_on_boot=false → 'stopped'")
                except Exception:
                    pass

    print(f"recovered_subprocess={recovered}")



def _start_system_engine(db_path, engine_name):
    """Start a system-managed engine subprocess (webui or mcp).

    Unified entry point that delegates to the engine module's execute() method.
    Reads .quickrobot.env internally for host/port/token configuration.

    Args:
        db_path: Path to the SQLite database.
        engine_name: Short alias ("webui" or "mcp").
    """
    from db.adapters.instances import list_instances as _li, get_instance as _gi
    from engine import get_engine
    from lib.qr_engine_ids import get_port_default, QR_ENGINE_WEBUI_NAME, QR_ENGINE_MCP_NAME
    from lib.lib_system_engine import load_env_config as _load_env_cfg

    # Map short alias to canonical engine type name (SOT constants, not hardcoded strings)
    name_map = {"webui": QR_ENGINE_WEBUI_NAME, "mcp": QR_ENGINE_MCP_NAME}
    if engine_name not in name_map:
        print(f"[qr] Unknown system engine: {engine_name}")
        return

    engine_type_name = name_map[engine_name]
    # Instance ID from QR_ENGINE_* constants (SOT, not hardcoded integers)
    from lib.qr_engine_ids import QR_ENGINE_QUICKROBOT_WEBUI, QR_ENGINE_QUICKROBOT_MCP
    inst_id_map = {QR_ENGINE_WEBUI_NAME: QR_ENGINE_QUICKROBOT_WEBUI, QR_ENGINE_MCP_NAME: QR_ENGINE_QUICKROBOT_MCP}
    inst_id = inst_id_map.get(engine_type_name)

    # Find existing system-managed instance (should always exist after provisioning)
    inst = None
    for i in _li(db_path):
        if i.get("engine_type_name") == engine_type_name and i.get("system_managed"):
            inst = i
            break

    if inst is None:
        print(f"[qr] System instance for {engine_type_name} not found — skipping start")
        return

    # Delegate to engine module's execute() (handles PID check, env config, subprocess spawn)
    engine = get_engine(engine_type_name)
    if engine is None:
        print(f"Warning: {engine_type_name} engine not loaded, cannot start")
        return

    result = engine.execute(inst["id"], "start", db_path)
    if result.get("error"):
        print(f"[qr] {engine_type_name.replace('-', ' ').title()} start failed: {result['error']}")
        return

    if result.get("status") == "existing_process_alive":
        pid = result.get('pid', '?')
        port = result.get('port', '?')
        print(f"[qr] {engine_type_name.replace('-', ' ').title()} already running (pid={pid}, port={port}), skipping start")
        try:
            from db.adapters.instances import transition_state
            current = _gi(db_path, inst["id"])
            if current and current.get("state") == "unconfigured":
                transition_state(db_path, inst["id"], "deployed")
        except Exception:
            pass
        return

    port = result.get("port") or get_port_default("quickrobot-webui")
    pid = result.get("pid", "?")
    # Load env config for dynamic host/port values (not hardcoded)
    try:
        _qr_env = _load_env_cfg(os.getcwd())
    except FileNotFoundError:
        _qr_env = {}
    # Build engine-specific URL from actual env config (not hardcoded)
    api_host = _CONFIG.get("host", "?")
    api_port = _CONFIG.get("api_port", "?")
    if engine_name == "webui":  # short alias, OK for user-facing param
        webui_host = _qr_env.get("QUICKROBOT_WEBUI_HOST") or api_host
        webui_port = _qr_env.get("QUICKROBOT_WEBUI_PORT", str(port))
        url_path = f"http://{webui_host}:{webui_port}/webui/"
    elif engine_name == "mcp":  # short alias, OK for user-facing param
        mcp_host = _qr_env.get("QUICKROBOT_MCP_HOST") or api_host
        mcp_port = _qr_env.get("QUICKROBOT_MCP_PORT", str(port))
        url_path = f"http://{mcp_host}:{mcp_port}"  # MCP SSE endpoint
    else:
        url_path = f"http://{api_host}:{port}/"
    print(f"[qr] [{engine_name.upper()}] auto-start: {engine_type_name.replace('-', ' ').title()} at {url_path}  pid={pid}  api={api_host}:{api_port}")


def _start_ping_thread(db_path):
    """Start the background ping reachability checker thread.

    Reads ping_command and ping_interval from .quickrobot.env (engine_type_id=1).
    If ping_command is None: prints message and returns (disabled).
    If ping_interval is None or < 60: enforces minimum of 60 seconds.
    The thread runs in a loop, pinging each node's hostname and updating
    the ping_state column (online/offline/disabled) for active hosts only.

    Args:
        db_path: Path to the SQLite database.
    """
    import subprocess as _subp
    import time as _time
    from threading import Lock

    # Read config: env file provides defaults, DB overrides at runtime
    qr_env = _CONFIG.get("qr_env_config", {})
    ping_cmd = qr_env.get("QUICKROBOT_API_PING_COMMAND")
    if not ping_cmd:
        ping_cmd = "ping -c1 -W2 {host}"  # hardcoded fallback

    if not ping_cmd or ping_cmd.strip() == "":
        print("[qr] host ping: disabled (no ping_command set)")
        return

    interval_str = qr_env.get("QUICKROBOT_API_PING_INTERVAL")
    try:
        interval = max(int(interval_str), 60) if interval_str else 60
    except (ValueError, TypeError):
        interval = 60

    # DB override for ping_interval (runtime-editable)
    try:
        from db.adapters.configs import get_engine_config as _gec_ping
        from quickrobot import _let_config
        et_id = None
        for _et in _let_config():
            if _et["name"] == "quickrobot-api":
                et_id = _et["id"]
                break
        if et_id:
            row = _gec_ping(db_path, et_id, "ping_interval")
            if row and row.get("value"):
                try:
                    db_interval = int(row["value"])
                    if db_interval > 0:
                        interval = max(db_interval, 60)
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass  # DB read failure — keep env-based interval

    # First run: populate ping_state for all active hosts
    try:
        from db.adapters.nodes import list_nodes as _ln, update_ping_state as _ups
        nodes = _ln(db_path)
        for n in nodes:
            nid = n.get("id")
            host = n.get("hostname", "")
            if not host or nid == 1 or not n.get("is_active", 1):
                continue
            try:
                result = _subp.run(
                    ping_cmd.replace("{host}", host),
                    shell=True, timeout=5,
                    stdout=_subp.DEVNULL, stderr=_subp.DEVNULL,
                )
                if result.returncode == 0:
                    _ups(db_path, nid, "online")
                else:
                    _ups(db_path, nid, "offline")
            except Exception:
                _ups(db_path, nid, "offline")
    except Exception:
        pass  # non-critical at startup

    def _ping_loop():
        """Background loop: ping each active node every interval seconds."""
        from db.adapters.nodes import list_nodes as _ln2, update_ping_state as _ups2
        while True:
            _time.sleep(interval)
            try:
                _nodes = _ln2(db_path)
                for n in _nodes:
                    nid = n.get("id")
                    host = n.get("hostname", "")
                    if not host or nid == 1 or not n.get("is_active", 1):
                        continue
                    try:
                        result = _subp.run(
                            ping_cmd.replace("{host}", host),
                            shell=True, timeout=5,
                            stdout=_subp.DEVNULL, stderr=_subp.DEVNULL,
                        )
                        if result.returncode == 0:
                            _ups2(db_path, nid, "online")
                        else:
                            _ups2(db_path, nid, "offline")
                    except Exception:
                        _ups2(db_path, nid, "offline")
            except Exception:
                pass  # non-critical

    global _ping_thread
    _ping_thread = threading.Thread(target=_ping_loop, daemon=True)
    _ping_thread.start()
    print(f"[qr] host ping started (interval={interval}s)")


def phase5_init():
    """Run the full initialization sequence: sys.modules alias + _init_app().

    This is the core of the startup pipeline. It runs migrations, discovers
    engines, imports seed data, provisions system instances, and starts
    WebUI/MCP subprocesses.
    """
    global _APP_INITIALIZED
    if _APP_INITIALIZED := globals().get("_APP_INITIALIZED", False):
        return
    _APP_INITIALIZED = True

    # Load system engine config from env file (before any engine operations)
    # Returns (qr_env_dict, console_debug_level, ansible_log_level)
    from lib.lib_startup import load_system_engine_config as _load_system_engine_config
    _env_result = _load_system_engine_config()

    qr_env = _env_result[0] if isinstance(_env_result, tuple) else (_CONFIG.get("qr_env_config", {}) if "_CONFIG" in dir() else {})
    # Set qr_env_config and logging config keys in _CONFIG (now defined)
    _CONFIG["qr_env_config"] = qr_env
    if isinstance(_env_result, tuple) and len(_env_result) >= 3:
        _CONFIG["console_debug_level"] = _env_result[1]
        _CONFIG["ansible_log_level"] = _env_result[2]
    # Strict validation for API engine
    _validate_system_engine_env(qr_env, "API", ["QUICKROBOT_API_HOST", "QUICKROBOT_API_PORT"])
    _validate_system_engine_bind(qr_env, "API")

    # Override API host/port from .quickrobot.env if present (env > constant defaults)
    if "QUICKROBOT_API_HOST" in qr_env:
        _CONFIG["host"] = qr_env["QUICKROBOT_API_HOST"]
    if "QUICKROBOT_API_PORT" in qr_env:
        try:
            _CONFIG["api_port"] = int(qr_env["QUICKROBOT_API_PORT"])
        except ValueError:
            pass
    # max_backups from env (overrides hardcoded default in _CONFIG)
    if "QUICKROBOT_MAX_BACKUPS" in qr_env:
        try:
            _CONFIG["max_backups"] = int(qr_env["QUICKROBOT_MAX_BACKUPS"])
        except ValueError:
            pass
    # Pre-flight: .quickrobot.env must exist and define required keys before any DB operations
    if not qr_env:
        from lib.lib_system_engine import load_env_config
        try:
            load_env_config(os.getcwd())
        except FileNotFoundError as exc:
            print(f"[qr] FATAL: {exc}")
            sys.exit(1)
    from db.sqlite import pool as _pool
    from db.adapters.configs import set_engine_config as _set_ec
    from db.adapters.configs import get_engine_config as _gec  # FIX: was missing, caused silent failure on MCP DB override

    # 1) Backup database before any operations (skip if --init, first backup already done in main)
    from lib.lib_startup import backup_database as _backup_database
    _backup_database(_CONFIG["db_path"], skip_if_init=_CONFIG.get("init_mode", False))

    db_path = _CONFIG["db_path"]

    # 2) Run migrations (dev: execute; prod: check + alert if pending; --init: always execute)
    # Must happen early — creates all tables including engine_configs, playbook_registry, etc.
    from db.migration import run_migrations
    _init_mode = _CONFIG.get("init_mode", False)
    # DEV mode only controls mismatch behavior (warning vs fatal); init_mode controls DB flow
    applied_or_warning = run_migrations(
        db_path, os.path.join(_project_root, "db", "migrations"), dev_mode=_init_mode
    )
    if _init_mode:
        applied_count = len(applied_or_warning[0]) if isinstance(applied_or_warning, tuple) else 0
        print(f"[qr] Migrations applied: {applied_count}")
    elif isinstance(applied_or_warning, tuple) and applied_or_warning[1]:
        # Prod mode — pending migrations detected
        print(f"[qr] WARNING: {applied_or_warning[1]}")

    # 3b) Purge stale ansible_actions (NULL instance_id, older than 24h)
    try:
        with _pool(db_path) as conn:
            count = conn.execute(
                "DELETE FROM ansible_actions WHERE instance_id IS NULL AND created_at < datetime('now', '-24 hours')").rowcount
        if count:
            print(f"[qr] Purged {count} stale ansible_action(s) older than 24h (NULL instance_id)")
    except Exception as exc:
        print(f"[qr] WARNING: stale action purge failed: {exc}")

    # 3.5) Seed LLAMA_ARG_SEED for existing DBs (fresh DB gets it from seed file)
    try:
        from db.adapters.configs import update_engine_config as _uec
        _uec(db_path, 4, "LLAMA_ARG_SEED", "1337", "Random seed for sampling (LLAMA_ARG_SEED)")
    except Exception:
        pass

    # Load engines and auto-register in DB
    from engine import load_engines as _load_engines, _auto_register_engines
    _load_engines()
    _auto_register_engines(db_path)

    # Load engine type registry from DB for runtime lookups
    try:
        from lib.qr_engine_registry import load_engine_registry
        load_engine_registry(db_path)
        print("[qr] Engine registry loaded")
    except Exception as exc:
        print(f"[qr] WARNING: engine registry load failed: {exc}")

    # Fix: seed iperf3 engine_configs if missing (old seed used wrong engine_type_id)
    try:
        from db.adapters.configs import update_engine_config as _uec
        from db.sqlite import pool as _pool
        with _pool(db_path) as conn:
            iperf3_et = conn.execute(
                "SELECT id FROM engine_types WHERE name='iperf3' LIMIT 1"
            ).fetchone()
            if iperf3_et:
                iperf3_id = iperf3_et["id"]
                # Check if any configs exist for iperf3
                has_configs = conn.execute(
                    "SELECT COUNT(*) FROM engine_configs WHERE engine_type_id=?", (iperf3_id,)
                ).fetchone()[0]
                if has_configs == 0:
                    _uec(db_path, iperf3_id, "base_port", "9900", "Base port for iperf3 instance allocation")
                    _uec(db_path, iperf3_id, "restart_policy", "no", "Systemd restart policy")
                    _uec(db_path, iperf3_id, "start_on_boot", "false", "Enable on boot")
                    _uec(db_path, iperf3_id, "polling_interval_local_sec", "10", "Local polling interval (sec)")
                    _uec(db_path, iperf3_id, "polling_interval_remote_sec", "600", "Remote polling interval (sec)")
                    print("[qr] Seeded iperf3 engine_configs")
    except Exception as exc:
        print(f"[qr] WARNING: iperf3 config seeding failed: {exc}")

    # Seed start_on_boot for universal and subprocess engines (if not already present)
    try:
        with _pool(db_path) as conn:
            for et_name, sob_desc in [("universal", "Enable systemd unit on boot (true/false)"),
                                      ("subprocess", "Enable on boot via subprocess recovery (true/false)")]:
                row = conn.execute("SELECT id FROM engine_types WHERE name=? LIMIT 1", (et_name,)).fetchone()
                if row:
                    has_key = conn.execute(
                        "SELECT COUNT(*) FROM engine_configs WHERE engine_type_id=? AND key='start_on_boot'", (row["id"],)
                    ).fetchone()[0]
                    if has_key == 0:
                        _uec(db_path, row["id"], "start_on_boot", "false", sob_desc)
                        print(f"[qr] Seeded {et_name} start_on_boot")
    except Exception as exc:
        print(f"[qr] WARNING: universal/subprocess config seeding failed: {exc}")

    # Seed presets now that engine_types are registered (FK constraint)
    try:
        from quickrobot import _seed_presets
        with _pool(db_path) as conn:
            preset_count = conn.execute("SELECT COUNT(*) FROM engine_presets").fetchone()[0]
            if preset_count == 0:
                _seed_presets(conn)
                print("[qr] Seeded default presets")
    except Exception as exc:
        print(f"[qr] WARNING: preset seeding failed: {exc}")

    # Import seed file (checksum-verified, manifest-playbook registration always runs)
    from lib.lib_startup import import_seed_file as _import_seed_file
    _import_seed_file(db_path)

    # Backfill playbook_id for any existing rows that have NULL/empty IDs
    try:
        from db.adapters.playbooks import backfill_playbook_ids as _backfill_ids
        filled = _backfill_ids(db_path)
        if filled:
            print(f"[qr] Backfilled {filled} playbook(s) with stable IDs")
    except Exception as exc:
        print(f"[qr] WARNING: playbook ID backfill failed: {exc}")

    # System instances must exist for autostart to work (both dev and prod modes)
    _auto_provision_system_instances()
    # _auto_create_default_presets(db_path)  -- deactivated, presets managed manually

    # System engine autostart — env file is source of truth (read directly, no DB fallback)
    from lib.lib_system_engine import load_env_config as _load_env
    try:
        qr_env = _load_env()
    except FileNotFoundError:
        qr_env = {}
    # WebUI
    webui_autostart = str(qr_env.get("QUICKROBOT_WEBUI_AUTOSTART", "true")).lower() in ("true", "1")
    if _CONFIG.get("no_webui"):
        webui_autostart = False
    if webui_autostart:
        _start_system_engine(db_path, "webui")
    else:
        print("[qr] [WEBUI] autostart=disabled (set QUICKROBOT_WEBUI_AUTOSTART=true or use /instances/2/start)")

    # MCP
    # Read from .env (default false), DB override if explicitly set via API
    mcp_autostart = str(qr_env.get("QUICKROBOT_MCP_AUTOSTART", "false")).lower() in ("true", "1")
    try:
        db_ma = _gec(db_path, 3, "mcp_autostart") or {}  # Now works correctly (import fixed above)
        if db_ma:
            mcp_autostart = str(db_ma.get("value", "false")).lower() in ("true", "1")
    except Exception:
        pass  # DB not ready yet, use env default
    if mcp_autostart:
        _start_system_engine(db_path, "mcp")
    else:
        print("[qr] [MCP] autostart=disabled (set QUICKROBOT_MCP_AUTOSTART=true or use /instances/3/start)")
        # Verify actual MCP process state — clear stale "running" if not alive
        try:
            from db.adapters.instances import get_instance as _gi_mcp, update_instance as _ui_mcp
            mcp_inst = _gi_mcp(db_path, 3)
            if mcp_inst and mcp_inst.get("state") == "running":
                try:
                    os.kill(mcp_inst.get("pid_last_known", 0), 0)
                    # Process alive — leave as running
                except OSError:
                    # Process dead, clear stale state
                    _ui_mcp(db_path, 3, state="stopped", pid_last_known=None)
                    print("[qr] MCP state cleared (process not alive, autostart=false)")
        except Exception:
            pass

    # Start global host reachability tracking (HOST-PING)
    try:
        _start_ping_thread(db_path)
    except Exception:
        pass  # non-critical — ping thread is optional
    # Recover stale instances (mid-build after crash)
    try:
        recover_stale_instances()
    except Exception:
        pass

    # Recover subprocess instances with start_on_boot=true
    try:
        recover_subprocess_instances(db_path)
    except Exception as _exc:
        print(f"[qr] WARNING: subprocess recovery failed: {_exc}")

    # Ensure system-managed instances have correct state at startup.
    # Since we reached here, the API is running and subprocesses are started — set to running.
    try:
        from db.adapters.instances import transition_state, list_instances as _li2
        for i in _li2(db_path):
            if i.get("system_managed") and i.get("state") == "unconfigured":
                try:
                    transition_state(db_path, i["id"], "running")
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 6 — CLI overrides apply to DB
# ---------------------------------------------------------------------------


def phase6_cli_overrides(args):
    """Apply --webui-detach and --no-webui overrides to DB engine_configs."""
    from db.adapters.configs import update_engine_config as _uec

    if args.webui_detach:
        _uec(_CONFIG["db_path"], 2, "webui_detach", "true",
             "WebUI detach mode (set via CLI)")
        print("[qr] WebUI detach mode enabled (survives API death)")

    if args.no_webui:
        _uec(_CONFIG["db_path"], 2, "webui_autostart", "false",
             "WebUI auto-start disabled (set via CLI)")
        print("[qr] WebUI auto-start disabled via --no-webui")


# ---------------------------------------------------------------------------
# Phase 7 — Final playbook integrity verification
# ---------------------------------------------------------------------------


def phase7_verify_playbooks():
    """Verify playbook checksums. Mode-dependent: prod=exit on mismatch,
    dev=warn, dev-update=sync+exit."""
    from db.adapters.playbooks import verify_playbook_integrity as _verify_pbi
    pb_mode = _CONFIG.get("pb_mode", "dev")
    _verify_pbi(_CONFIG["db_path"], _project_root, pb_mode,
                exit_on_update=(pb_mode == "dev-update"))



# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_startup():
    """Run the full startup pipeline in order.

    This is the single entry point called from quickrobot.py __main__.
    After completion, the Flask app is ready to serve requests via app.run().

    Returns:
        dict: _CONFIG with all resolved settings.
    """
    args = parse_args()
    phase0_mode_flags(args)
    phase1_config(args)
    phase2_preflight(args, None)
    phase3_db_handling(args)
    phase4_pid_port()
    phase5_init()
    phase6_cli_overrides(args)
    phase7_verify_playbooks()
    return _CONFIG
