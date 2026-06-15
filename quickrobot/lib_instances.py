"""Shared instance business logic for quickrobot route handlers.

Contains: deploy_instance, _execute_playbook, build coordination,
system-managed instance lifecycle, iperf3 client, UUID verification.

Imported by route modules (routes_instances.py) and used by quickrobot.py root.
"""

import os
import json
import threading
import time as _time
import socket as _socket
import urllib.request as _urllib
import weakref
from threading import Lock

# Module-level globals
_INSTANCE_DEPLOY_LOCKS = weakref.WeakValueDictionary()  # {instance_id: Lock()}
_NODE_BUILD_LOCK = threading.Lock()
BUILD_STATES = ("configuring", "deploying", "updating")
# Project root — resolved from parent package's _project_root (defined in __init__.py line 21, before this module imports)
PROJECT_ROOT = __import__("quickrobot")._project_root

# Internal imports — avoid circular dependency with __init__.py
from db.sqlite import pool as db_pool
from db.adapters.instances import (
    get_instance, update_instance, list_instances, create_instance,
    assign_port, transition_state as _ts, log_action as _log_action,
    merge_configs, check_system_managed,
)
from db.adapters.playbooks import (
    resolve_playbook_by_id, get_playbook_by_path,
    increment_usage_counter, increment_error_counter,
    verify_playbook_integrity, register_all_core_playbooks,
)
from db.adapters.nodes import get_node
from db.adapters.configs import get_engine_config as _gec
from lib.lib_ansible_runner import run_playbook, log_ansible_action
from lib.lib_cluster_env_builder import build_llama_server_env, build_rpc_server_env, get_cluster_summary
from lib.lib_constants import (
    DEFAULT_ANSIBLE_USER, GRACE_PERIOD_RUNNING, QR_DEFAULT_BIND_HOST,
)
from lib.qr_engine_ids import (
    QR_ENGINE_API_NAME, QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_WEBUI_NAME, QR_ENGINE_MCP_NAME,
)
from lib.lib_qr_actions import log_qr_override
from quickrobot.lib_responses import error_response as _error_response, success_single as _success_single


# Resolve _CONFIG from the root quickrobot module (available after package is loaded)
# Lazy resolution to avoid circular imports
def _get_config():
    """Get the global _CONFIG dict from the quickrobot package."""
    import quickrobot as _qr_mod
    return _qr_mod._CONFIG if hasattr(_qr_mod, '_CONFIG') else {}

def _get_pb_mode():
    """Get the operational mode from _CONFIG (set by phase0_mode_flags)."""
    return _get_config().get("pb_mode", "dev")


def _check_node_active(db_path, node_id):
    """Check node exists and is active for operations.

    Checks is_active (admin toggle) — the single gate for all actions on a node.
    ping_state and status are display/reporting only; they do not affect action gating.

    Returns:
        Node dict on success.
        error_response tuple on failure (caller should return immediately).
    """
    nd = get_node(db_path, node_id) if node_id else None
    if nd is None:
        return _error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    if not nd.get("is_active", 1):
        return _error_response("NODE_INACTIVE",
            f"Node '{nd['name']}' (id:{node_id}) is admin-disabled (is_active=0). "
            "Operation aborted.")

    return nd


def override_system_instance_states(instances, config):
    """Override state for system-managed instances based on real process health.

    System-managed instances (IDs 1-3) have their DB state overridden to
    reflect actual process health via psutil checks. This must be called
    before returning instance data to avoid stale/error states from DB.

    Args:
        instances: List of instance dicts from list_instances().
        config: _CONFIG dict with 'db_path'.

    Returns:
        instances (mutated in place).
    """
    try:
        import psutil as _psutil  # lazy import to avoid hard dependency
    except ImportError:
        return instances

    now_ts = _time.time()
    qr_mod = __import__("quickrobot")
    start_time = getattr(qr_mod, '_START_TIME', now_ts)

    for inst in instances:
        if not check_system_managed(config["db_path"], inst["id"]):
            continue

        engine_type_name = inst.get("engine_type_name", "")

        if engine_type_name == QR_ENGINE_API_NAME:
            # This IS the API process — always running if we're serving requests
            inst["state"] = "running"
            inst["process_age_seconds"] = int(now_ts - start_time)
        elif engine_type_name in (QR_ENGINE_WEBUI_NAME, QR_ENGINE_MCP_NAME):
            # Check psutil: if PID is alive, instance is running
            pid = inst.get("pid_last_known") or inst.get("pid")
            if pid and isinstance(pid, int):
                try:
                    p = _psutil.Process(pid)
                    if p.is_running():
                        inst["state"] = "running"
                        inst["process_age_seconds"] = int(now_ts - p.create_time())
                    else:
                        # PID died — keep DB state (error/stopped), no uptime
                        pass
                except Exception:
                    pass
            # Fallback: if no valid PID, use API start time
            if not inst.get("process_age_seconds") and engine_type_name == QR_ENGINE_API_NAME:
                inst["process_age_seconds"] = int(now_ts - start_time)

    return instances


# Logger for playbook resolution warnings
import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deploy lock (SM-3 deduplication)
# ---------------------------------------------------------------------------

def _get_deploy_lock(inst_id):
    """Get or create a per-instance deploy lock (SM-3 dedup)."""
    if inst_id not in _INSTANCE_DEPLOY_LOCKS:
        lock = Lock()
        _INSTANCE_DEPLOY_LOCKS[inst_id] = lock
    return _INSTANCE_DEPLOY_LOCKS[inst_id]


# ---------------------------------------------------------------------------
# Playbook resolution helpers
# ---------------------------------------------------------------------------

def _resolve_playbook_by_file_path(file_path):
    """Resolve a playbook by exact file path.

    Used by the universal engine's resolver_type="file_path". Checks if the
    file exists at the given path and returns the absolute path. No registry
    lookup — purely file-existence based for user-defined playbooks.

    Args:
        file_path: Exact file path (relative to project root or absolute).

    Returns:
        str — full absolute path, or None if not found.
    """
    root = PROJECT_ROOT
    if os.path.isabs(file_path):
        candidate = file_path
    else:
        candidate = os.path.join(root, file_path)
    if os.path.exists(candidate):
        return candidate
    logger.warning("Playbook file not found: %s", file_path)
    return None


def _resolve_engine_playbook_id(action, engine_type_name):
    """Map (action, engine_type) → stable playbook_id string.

    Args:
        action: Action string, e.g. "deploy", "undeploy".
        engine_type_name: Engine type name, e.g. "llama_server", "llama_rpc", "iperf3".

    Returns:
        str — stable playbook_id (e.g. "DEPLOY_LLAMA_SERVER_V1"), or None.
    """
    _MAP = {
        ("deploy", "llama_server"): "DEPLOY_LLAMA_SERVER_V1",
        ("deploy", "llama_rpc"): "DEPLOY_LLAMA_RPC_V1",
        ("deploy", "iperf3"): "DEPLOY_IPERF3_V1",
        ("deploy", "universal"): "DEPLOY_UNIVERSAL_V1",
        ("undeploy", "llama_server"): "UNDEPLOY_LLAMA_SERVER_V1",
        ("undeploy", "llama_rpc"): "UNDEPLOY_LLAMA_RPC_V1",
        ("undeploy", "iperf3"): "UNDEPLOY_IPERF3_V1",
        ("undeploy", "universal"): "UNDEPLOY_UNIVERSAL_V1",
    }
    return _MAP.get((action, engine_type_name))


def _track_playbook_usage(ref):
    """Increment usage counter for a playbook after successful execution.

    Accepts either a file_path ("playbooks/deploy_rpc.yml") or playbook_id
    ("DEPLOY_LLAMA_RPC_V1"). Resolves to the DB row and increments.

    Args:
        ref: File path or stable playbook ID string.
    """
    try:
        config = _get_config()
        db_path = config.get("db_path", "")
        # Try playbook_id first (e.g., "APT_UPDATE_V1")
        pb = resolve_playbook_by_id(db_path, ref)
        if not pb:
            # Fall back to file_path lookup for backward compat
            pb = get_playbook_by_path(db_path, ref)
        if pb and pb.get("id"):
            increment_usage_counter(db_path, pb["id"])
    except Exception:
        pass  # Non-critical — don't break playbook flow


def _track_playbook_error(ref):
    """Increment error counter for a playbook after failed execution.

    Accepts either a file_path ("playbooks/deploy_rpc.yml") or playbook_id
    ("DEPLOY_LLAMA_RPC_V1"). Resolves to the DB row and increments.

    Args:
        ref: File path or stable playbook ID string.
    """
    try:
        config = _get_config()
        db_path = config.get("db_path", "")
        # Try playbook_id first (e.g., "APT_UPDATE_V1")
        pb = resolve_playbook_by_id(db_path, ref)
        if not pb:
            # Fall back to file_path lookup for backward compat
            pb = get_playbook_by_path(db_path, ref)
        if pb and pb.get("id"):
            increment_error_counter(db_path, pb["id"])
    except Exception:
        pass  # Non-critical — don't break playbook flow


def _execute_playbook(resolver_ref, resolver_type="playbook_id", limit=None, extra_vars=None,
                      timeout=3600, inventory_data=None, node_id=None, instance_id=None,
                      action_type="ansible_execute"):
    """Centralized playbook execution with pre-call checksum, usage tracking, and error handling.

    Resolves a playbook from the DB registry, verifies its checksum before execution,
    increments the usage counter BEFORE running, executes via run_playbook(), logs
    ansible_actions for audit trail, and tracks errors on failure or exception.

    Args:
        resolver_ref: Playbook reference — stable playbook_id string (for type="playbook_id")
                      or file path string (for type="file_path", used by universal engine).
        resolver_type: "playbook_id" (resolve by stable ID, default) or
                       "file_path" (resolve by file existence check).
        limit: Host limit for playbook execution (e.g., 'dllama6.lan').
        extra_vars: Dict of extra variables to pass to the playbook.
        timeout: Max seconds for playbook execution (default 3600).
        inventory_data: Optional inventory data for ansible.
        node_id: Foreign key to nodes table (for ansible_actions logging).
        instance_id: Foreign key to instances table (for ansible_actions logging).
        action_type: Action type string for ansible_actions (e.g., 'deploy', 'undeploy').

    Returns:
        dict with keys:
          - "success": bool — True if playbook ran without Ansible failure
          - "failed": bool — True if Ansible reported failures
          - "playbook_id": stable ID string or None
          - "file_path": resolved file path string
          - "rel_path": relative path for counter tracking
          - "checksum_status": "pass"/"mismatch"/"missing"/"unknown"
          - "result": the run_playbook() result dict (on success)
          - "error": error message string (on exception)
    """
    from db.sqlite import pool
    from db.adapters.playbooks import resolve_playbook_by_id as _rpbi, \
        get_playbook_by_path as _gpbp, \
        _compute_file_checksum as _chksum, \
        _parse_playbook_header as _pph
    from lib.lib_ansible_runner import _parse_playbook_timeout as _ppt

    config = _get_config()
    mode = _get_pb_mode()
    root = PROJECT_ROOT
    db_path = config.get("db_path", "")

    playbook_path = None
    if resolver_type == "file_path":
        playbook_path = _resolve_playbook_by_file_path(resolver_ref)
        # Fallback: if resolver_ref is already an absolute/full path and resolver failed, use it directly
        if not playbook_path and os.path.isabs(resolver_ref) and os.path.exists(resolver_ref):
            playbook_path = resolver_ref
    elif resolver_type == "playbook_id":
        # Resolve stable playbook ID → DB record → file path
        pb_record = resolve_playbook_by_id(db_path, resolver_ref)
        if pb_record and pb_record.get("file_path"):
            playbook_path = os.path.join(root, pb_record["file_path"])
    else:
        # resolver_type="file_path" — handled above; raw path fallback
        playbook_path = resolver_ref  # Assume it's already a full path

    if not playbook_path:
        return {"success": False, "failed": True, "playbook_id": None,
                "file_path": None, "rel_path": None,
                "checksum_status": "unknown", "result": None,
                "error": f"Playbook not resolved: resolver_type={resolver_type}, ref={resolver_ref}"}

    # Compute relative path for counter tracking
    rel_path = os.path.relpath(playbook_path, root)

    # Resolve DB record for checksum lookup and counter tracking
    pb_record = None
    if isinstance(resolver_ref, str):
        pb_record = resolve_playbook_by_id(db_path, resolver_ref)
        if not pb_record:
            pb_record = get_playbook_by_path(db_path, resolver_ref)

    playbook_id = None
    checksum_status = "unknown"
    expected_hash = None

    # Full verification: checksum + file_size + @playbook_id header match
    if pb_record and isinstance(pb_record, dict):
        playbook_id = pb_record.get("playbook_id")
        expected_hash = pb_record.get("checksum_sha256")
        expected_size = pb_record.get("file_size")
        expected_pb_id = pb_record.get("playbook_id", "")
        file_path = pb_record.get("file_path", "")
        full_path = os.path.join(root, file_path) if not os.path.isabs(file_path) else file_path

        if not os.path.exists(full_path):
            checksum_status = "missing"
            print(f"[qr] CHECKSUM MISSING: {file_path} (not found on disk)")
            if mode == "prod":
                print("[qr] FATAL: playbook missing in prod mode. Killing API process.")
                raise SystemExit(1)
        else:
            # Check checksum
            actual_hash = _chksum(full_path)
            hash_ok = (actual_hash == expected_hash) if expected_hash else True

            # Check file_size
            actual_size = os.path.getsize(full_path)
            size_ok = (actual_size == expected_size) if expected_size else True

            # Check @playbook_id header in YAML file
            actual_header = _pph(full_path)
            id_ok = (actual_header["playbook_id"] == expected_pb_id) if expected_pb_id else True

            # Aggregate status
            if hash_ok and size_ok and id_ok:
                checksum_status = "pass"
            else:
                checksum_status = "mismatch"
                issues = []
                if not hash_ok:
                    issues.append(f"checksum (expected={expected_hash[:16]}... actual={actual_hash[:16]}...)")
                if not size_ok:
                    issues.append(f"size (expected={expected_size}B actual={actual_size}B)")
                if not id_ok:
                    issues.append(f"playbook_id header (expected={expected_pb_id} actual={actual_header['playbook_id']})")
                print(f"[qr] PLAYBOOK VERIFY FAIL: {file_path} — {'; '.join(issues)}")
                if mode == "prod":
                    print("[qr] FATAL: playbook verification failed in prod mode. Killing API process.")
                    raise SystemExit(1)
    else:
        # No DB record — check file existence only
        if os.path.exists(playbook_path):
            checksum_status = "missing"  # known on disk but not in registry
        else:
            checksum_status = "missing"

    # Resolve per-playbook timeout from YAML header comment (# @timeout: N)
    _effective_timeout = timeout  # caller-provided (default 3600)
    if playbook_path and timeout == 3600:
        try:
            _effective_timeout = _ppt(playbook_path, default=timeout)
            from lib.lib_constants import QUICKROBOT_DEBUG_LEVEL
            if QUICKROBOT_DEBUG_LEVEL >= 10 and _effective_timeout != 3600:
                print(f"[qr] playbook timeout override: {playbook_path} -> {_effective_timeout}s", flush=True)
        except Exception:
            pass  # Non-critical — fall back to caller timeout

    # Increment usage counter BEFORE execution
    try:
        ref_for_tracking = playbook_id or rel_path
        _track_playbook_usage(ref_for_tracking)
    except Exception:
        pass  # Non-critical

    # Log starting action to ansible_actions (audit trail)
    if node_id is not None or instance_id is not None:
        try:
            log_ansible_action(db_path, action_type, node_id,
                               instance_id, playbook_path, extra_vars or {},
                               {"failed": False, "started": True})
        except Exception as _log_err:
            print(f"[qr] WARNING: ansible action start log failed: {_log_err}", flush=True)

    # Start qr_actions task tracking for running-task visibility (Phase A)
    _qr_task_id = 0
    if playbook_id and (node_id is not None or instance_id is not None):
        try:
            from lib.lib_qr_actions import log_qr_task as _lqt
            _qr_task_id = _lqt(db_path, action_type, node_id=node_id,
                               instance_id=instance_id,
                               playbook_registry_id=pb_record.get("id") if pb_record else None)
        except Exception:
            pass  # Non-critical — task tracking failure shouldn't break execution

    # Execute the playbook
    try:
        result = run_playbook(playbook_path, inventory_path=inventory_data,
                              limit=limit, extra_vars=extra_vars, timeout=_effective_timeout)
        failed = result.get("failed", False)
        # Extra guard: if playbook matched zero hosts, treat as failure
        if not result.get("hosts_matched", True):
            failed = True
            result["error"] = result.get("error") or "Playbook matched 0 hosts in inventory"
        if failed:
            _track_playbook_error(rel_path)
        # Log final result to ansible_actions
        if node_id is not None or instance_id is not None:
            try:
                log_ansible_action(db_path, action_type, node_id,
                                   instance_id, playbook_path, extra_vars or {},
                                   result)
            except Exception as _log_err:
                print(f"[qr] WARNING: ansible action result log failed: {_log_err}", flush=True)
        # Update qr_actions task to completed/failed (Phase A)
        if _qr_task_id:
            try:
                from lib.lib_qr_actions import update_qr_task as _uqt
                _uqt(db_path, _qr_task_id, "completed" if not failed else "failed")
            except Exception:
                pass  # Non-critical
        return {"success": not failed, "failed": failed,
                "playbook_id": playbook_id, "file_path": playbook_path,
                "rel_path": rel_path, "checksum_status": checksum_status,
                "result": result, "error": None}
    except Exception as exc:
        _track_playbook_error(rel_path)
        # Log exception to ansible_actions
        if node_id is not None or instance_id is not None:
            try:
                log_ansible_action(db_path, action_type, node_id,
                                   instance_id, playbook_path, extra_vars or {},
                                   {"failed": True, "error": str(exc)})
            except Exception as _log_err:
                print(f"[qr] WARNING: ansible action error log failed: {_log_err}", flush=True)
        # Update qr_actions task to failed (Phase A)
        if _qr_task_id:
            try:
                from lib.lib_qr_actions import update_qr_task as _uqt
                _uqt(db_path, _qr_task_id, "failed", finished_at=None)
            except Exception:
                pass  # Non-critical
        return {"success": False, "failed": True,
                "playbook_id": playbook_id, "file_path": playbook_path,
                "rel_path": rel_path, "checksum_status": checksum_status,
                "result": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Build coordination helpers
# ---------------------------------------------------------------------------

def _get_keep_shared_build():
    """Read clean_build_on_last_instance from quickrobot-api config_override.

    Looks up the system-managed instance by engine_type_name (QR_ENGINE_API)
    and system_managed=1 instead of hardcoded instance ID, so the lookup
    remains correct after DB reset or instance ID changes.
    """
    try:
        config = _get_config()
        with db_pool(config["db_path"]) as conn:
            row = conn.execute(
                "SELECT config_override FROM instances "
                "WHERE engine_type_name=? AND system_managed=1",
                (QR_ENGINE_API_NAME,),
            ).fetchone()
            if row and row[0]:
                co = json.loads(row[0])
                return bool(co.get("clean_build_on_last_instance", True))
    except Exception:
        pass
    return False


def _start_async_build(db_path, instance_id):
    """Start background build thread for an instance.

    Checks node build lock (per-node coordination for shared cmake build dir).
    If lock held by another instance, returns BUSY error immediately.
    If lock free, spawns daemon thread that runs the full playbook and handles
    state transitions.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.

    Returns:
        dict with action status, or error dict if node is busy.
    """
    from db.adapters.instances import get_instance as _gi, transition_state as _ts2, log_action as _log
    from db.sqlite import pool as _pool

    inst = _gi(db_path, instance_id)
    if not inst:
        return {"success": False, "message": f"Instance {instance_id} not found"}

    engine_type_name = inst.get("engine_type_name", "")
    node_id = inst.get("node_id")
    current_state = inst.get("state", "")

    # SM-3: Dedup check — reject only if a build thread is actually running
    if current_state in ("deploying", "compiling"):
        return {"success": False, "message": f"Instance already {current_state}"}

    # Resolve node hostname
    try:
        nd = get_node(db_path, node_id)
    except Exception:
        nd = None
    if not nd:
        return {"success": False, "message": f"Node for instance {instance_id} not found"}

    hostname = (nd.get("hostname") or nd.get("name"))
    node_short = hostname.split(".")[0] if hostname else "unknown"

    # Check node build coordination
    with _NODE_BUILD_LOCK:
        try:
            with _pool(db_path) as conn:
                nd_row = conn.execute(
                    "SELECT node_build_state FROM nodes WHERE id = ?", (node_id,)
                ).fetchone()
                nd_state_val = nd_row[0] if nd_row else "idle"
        except Exception:
            nd_state_val = "idle"

        if nd_state_val == "running":
            try:
                with _pool(db_path) as conn2:
                    other = conn2.execute(
                        f"SELECT id, name FROM instances WHERE node_id = ? AND state IN ({','.join(['?']*len(BUILD_STATES))}) AND id != ?",
                        (node_id,) + tuple(BUILD_STATES) + (instance_id,),
                    ).fetchone()
                    other_name = f"#{other[0]} ({other[1]})" if other else "unknown"
            except Exception:
                other_name = "unknown"
            from quickrobot.lib_responses import error_response
            return error_response("NODE_BUSY", f"Node {hostname} building (instance {other_name})")

        # Set node build state to running
        try:
            with _pool(db_path) as conn3:
                conn3.execute("UPDATE nodes SET node_build_state = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (node_id,))
        except Exception:
            pass

    # Transition state (SM-2 fix: use updating for running instances)
    if current_state == "running":
        try:
            _ts2(db_path, instance_id, "updating")
        except Exception:
            pass
    else:
        try:
            _ts2(db_path, instance_id, "configuring")
        except Exception:
           pass

    def _build_task():
        """Background thread: run deploy playbook with timeout."""
        try:
            # Build extra_vars
            merged = merge_configs(db_path, instance_id) if callable(merge_configs) else {"env": {}, "cli_opts": [], "model": {}}
            env = merged.get("env", {}) if isinstance(merged, dict) else {}
            cli_opts = list(merged.get("cli_opts", [])) if isinstance(merged, dict) else []

            # Resolve engine-level build vars
            try:
                gc = _gec(db_path, inst.get("engine_type_id")) or {}
                def _gc_val(key, default=None):
                    entry = gc.get(key) or {}
                    return entry.get("value") or default
            except Exception:
                gc = {}

            # Resolve restart_policy: instance-level > engine config > merged env > default 'no'
            restart_policy_val = inst.get("restart_policy") or (gc.get("restart_policy", {}).get("value") if isinstance(gc, dict) else None) or env.get("restart_policy", "no")
            extra_vars = {
                    "inventory_host": hostname,
                    "instance_id": inst["id"],
                    "instance_name": inst.get("name", ""),
                    "engine_type": engine_type_name,
                    "instance_port": inst.get("port_assigned", 0),
                    "binary_path": env.get("binary_path") or gc.get("binary_path", {}).get("value", ""),
                    "device": inst.get("gpu_device") or None,
                    "start_on_boot": bool(inst.get("start_on_boot", 0)),
                    "start_after_deploy": bool(inst.get("start_after_deploy", 0)),
                    "restart_policy": restart_policy_val,
                    "rpc_host": env.get("LLAMA_ARG_HOST") or QR_DEFAULT_BIND_HOST,
                    "merged_env": env,
                    "merged_cli_opts": cli_opts,
                    "instance_env_vars": [],
                    # Top-level vars required by playbooks (line 18-19 in deploy_*.yml)
                    "node_src_dir": _gc_val("node_src_dir", "/opt/quickrobot/llama.cpp"),
                    "node_build_dir": _gc_val("node_build_dir", "/opt/quickrobot/llama.cpp/build"),
                    "node_git_pull_cmd": _gc_val("node_git_pull_cmd", "git pull origin master"),
                    "git_clone_url": _gc_val("git_clone_url", "https://github.com/ggml-org/llama.cpp.git"),
                    "node_build_set_cmd": _gc_val("node_build_set_cmd"),
                    "node_build_run_cmd": _gc_val("node_build_run_cmd"),
                    "instance_uuid": inst.get("instance_uuid", ""),
                    "remote_node_user": nd.get("ansible_user") or DEFAULT_ANSIBLE_USER,
                }

            # --- Cluster binding: use builder results for llama_server/rpc ---
            if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
                cluster_result = build_llama_server_env(db_path, instance_id)
                extra_vars["merged_env"] = cluster_result["env"]
                extra_vars["merged_cli_opts"] = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []
                extra_vars["tensor_split_value"] = cluster_result["tensor_split_str"]
                extra_vars["split_mode_value"] = cluster_result["split_mode"]
                raw_bind = inst.get("rpc_bind_ids") or "[]"
                bind_ids = json.loads(raw_bind) if isinstance(raw_bind, str) else list(raw_bind or [])
                extra_vars["rpc_bind_ids"] = bind_ids
            elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                cluster_result = build_rpc_server_env(db_path, instance_id)
                extra_vars["merged_env"] = cluster_result["env"]
                extra_vars["merged_cli_opts"] = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []

            _log(db_path, instance_id, "async_build", "started",
                    {"node": hostname, "engine": engine_type_name})

            r = _execute_playbook(_resolve_engine_playbook_id("deploy", engine_type_name), resolver_type="playbook_id",
                                  limit=hostname, extra_vars=extra_vars, timeout=1800,
                                  node_id=inst.get("node_id"), instance_id=inst["id"],
                                  action_type="deploy_instance")
            if r["error"]:
                _log(db_path, instance_id, "async_build", "playbook_error", {"error": r["error"]})
                try:
                    _ts2(db_path, instance_id, "build_error")
                except Exception:
                    pass
                return

            result = r.get("result") or {}

            if result.get("failed"):
                _log(db_path, instance_id, "async_build", "playbook_failed",
                        {"error": str(result)})
                try:
                    _ts2(db_path, instance_id, "build_error")
                except Exception:
                    pass
                return

            # Extract commit hash from playbook results
            import re as _re
            new_build = None
            plays = result.get("results", {}).get("plays", [])
            for play in plays:
                if new_build:
                    break
                for task in play.get("tasks", []):
                    hosts = task.get("hosts", {})
                    for host_data in hosts.values():
                        msg = host_data.get("msg", "") or ""
                        bm = _re.search(r'BUILD_COMMIT=([a-f0-9]{7})', msg)
                        if bm:
                            new_build = bm.group(1)
                            break

            # Update build_number in DB
            if new_build:
                try:
                    with _pool(db_path) as conn4:
                        conn4.execute("UPDATE instances SET build_number=? WHERE id=?",
                                (new_build, instance_id))
                except Exception:
                    pass

            # Post-deploy state transitions (SM-2 fix)
            if current_state == "running":
                try:
                    _ts2(db_path, instance_id, "deployed")
                except Exception:
                    pass
            else:
                try:
                    _ts2(db_path, instance_id, "deploying")
                except Exception:
                    pass
                try:
                    _ts2(db_path, instance_id, "deployed")
                except Exception:
                    pass

            # RPC engine has special start path
            if engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                try:
                    _ts2(db_path, instance_id, "starting")
                except Exception:
                    pass
                rpc_remote = _run_manage_action(instance_id, engine_type_name, node_id, "start")
                if rpc_remote.get("success"):
                    try:
                        _ts2(db_path, instance_id, "running")
                    except Exception:
                        pass
                else:
                    _log(db_path, instance_id, "async_build", "failed_start",
                            {"rpc": rpc_remote})
                    try:
                        _ts2(db_path, instance_id, "build_error")
                    except Exception:
                        pass
            elif inst.get("start_after_deploy", 0):
                # Verify service started
                try:
                    _ts2(db_path, instance_id, "starting")
                except Exception:
                    pass
                port = inst.get("port_assigned", 0)
                svc_ok = False
                if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME and port:
                    try:
                        req = _urllib.Request(f"http://{hostname}:{port}/health")
                        with _urllib.urlopen(req, timeout=10) as resp:
                            if resp.status == 200:
                                svc_ok = True
                    except Exception:
                        pass
                else:
                    import subprocess as _sub3
                    unit_name = f"qr-{instance_id}-{engine_type_name}"
                    inv_script = os.path.join(PROJECT_ROOT, "lib", "qr_dynamic_inventory.py")
                    try:
                        svc_check = _sub3.run(
                            ["ansible", hostname, "-i", inv_script,
                                "-b", "-m", "shell",
                                f"-a", f"systemctl is-active {unit_name}"],
                            capture_output=True, text=True, timeout=15,
                        )
                        if "active" in (svc_check.stdout or "").lower():
                            svc_ok = True
                    except Exception:
                        pass
                if svc_ok:
                    _ts2(db_path, instance_id, "running")
                else:
                    _log(db_path, instance_id, "async_build", "warning",
                            {"message": "service not active after start"})

            _log(db_path, instance_id, "async_build", "success",
                    {"build": new_build, "node": hostname})

        except TimeoutError:
            _log(db_path, instance_id, "async_build", "playbook_timeout",
                    {"node": hostname, "timeout": 1800})
            try:
                _ts2(db_path, instance_id, "build_error")
            except Exception:
                pass
        except Exception as exc:
            _log(db_path, instance_id, "async_build", "exception", {"error": str(exc)})
            try:
                _ts2(db_path, instance_id, "build_error")
            except Exception:
                pass
        finally:
            # Always reset node build state on completion
            try:
                with _NODE_BUILD_LOCK:
                    with _pool(db_path) as conn5:
                        conn5.execute("UPDATE nodes SET node_build_state = 'idle', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (node_id,))
            except Exception:
                pass

    # Start background thread
    t = threading.Thread(target=_build_task, daemon=True,
                            name=f"async-build-{instance_id}")
    t.start()

    return {"success": True, "message": f"Build started in background (30 min timeout)",
            "status": "configuring", "node": hostname}


# ---------------------------------------------------------------------------
# Stop helpers
# ---------------------------------------------------------------------------

def _wait_for_stop_status(db_path, inst_id, max_wait=30):
    """Wait for instance to reach 'stopped' state by polling query-status.

    Args:
        db_path: Path to the SQLite database.
        inst_id: Integer instance ID.
        max_wait: Maximum seconds to wait (default 30).

    Returns:
        bool: True if stopped successfully, False if timed out.
    """
    from db.adapters.instances import transition_state as _ts2
    start = _time.time()
    while _time.time() - start < max_wait:
        try:
            result = query_status(db_path, inst_id)
            if result.get("status") == "ok":
                data = result.get("data", {})
                if data.get("state") == "stopped" or not data.get("alive", True):
                    try:
                        _ts2(db_path, inst_id, "stopped")
                        _log_action(db_path, inst_id, "stop", "success")
                    except Exception:
                        pass
                    return True
        except Exception:
            pass  # Best-effort polling
        _time.sleep(2)
    # Timeout — force transition to stopped anyway
    try:
        _ts2(db_path, inst_id, "stopped")
        _log_action(db_path, inst_id, "stop", "success", detail={"timeout": True})
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# System-managed instance lifecycle
# ---------------------------------------------------------------------------

def _stop_system_managed(inst_id, engine_type_name, log_action_fn):
    """Stop a system-managed instance (no Ansible).

    Uses PID-in-DB tracking via engine.execute(). No tmux.

    Args:
        inst_id: Integer primary key.
        engine_type_name: Engine type string.
        log_action_fn: Logging function reference.

    Returns:
        Success or error response dict.
    """
    from db.adapters.instances import get_instance as _gi, transition_state as _ts2

    config = _get_config()
    inst = _gi(config["db_path"], inst_id)
    if not inst:
        return _error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    node_id = inst.get("node_id")
    log_action_fn(config["db_path"], inst_id, "stop", "received", detail={"system_managed": True})

    try:
        _ts2(config["db_path"], inst_id, "stopping")
    except Exception:
        pass
    try:
        if engine_type_name == "quickrobot-webui":
            from engine.quickrobot_webui import QrWebuiEngine
            engine = QrWebuiEngine()
            result = engine.execute(inst_id, "stop", config["db_path"])
            log_action_fn(config["db_path"], inst_id, "stop", "success",
                            detail={"system_managed": True, "engine": engine_type_name})
            try:
                _ts2(config["db_path"], inst_id, "stopped")
            except Exception:
                pass
            try:
                log_ansible_action(config["db_path"], "stop_instance", node_id, inst_id,
                        "webui (quickrobot-webui)", {"action": "stop", "engine_type": engine_type_name,
                                "system_managed": True},
                        {"changed": True, "failed": False, "results": result})
            except Exception:
                pass
            return _success_single({"action": "stop", "instance_id": inst_id,
                                "state": "stopped", "system_managed": True})

        elif engine_type_name == "quickrobot-api":
            try:
                _ts2(config["db_path"], inst_id, "stopped")
            except Exception:
                pass
            log_action_fn(config["db_path"], inst_id, "stop", "success",
                            detail={"system_managed": True, "engine": engine_type_name,
                                "message": "Local service — state transition only"})
            try:
                log_ansible_action(config["db_path"], "stop_instance", node_id, inst_id,
                        "local (quickrobot-api)", {"action": "stop", "engine_type": engine_type_name,
                                "system_managed": True},
                        {"changed": False, "failed": False, "results": {"local_service": True}})
            except Exception:
                pass
            return _success_single({"action": "stop", "instance_id": inst_id,
                                "state": "stopped", "system_managed": True,
                                "message": "Local service — state transition only"})

        elif engine_type_name == "quickrobot-mcp":
            from engine.quickrobot_mcp import QrMcpEngine
            engine = QrMcpEngine()
            result = engine.execute(inst_id, "stop", config["db_path"])
            log_action_fn(config["db_path"], inst_id, "stop", "success",
                            detail={"system_managed": True, "engine": engine_type_name})
            try:
                _ts2(config["db_path"], inst_id, "stopped")
            except Exception:
                pass
            try:
                log_ansible_action(config["db_path"], "stop_instance", node_id, inst_id,
                        "mcp (quickrobot-mcp)", {"action": "stop", "engine_type": engine_type_name,
                                "system_managed": True},
                        {"changed": True, "failed": False, "results": result})
            except Exception:
                pass
            return _success_single({"action": "stop", "instance_id": inst_id,
                                "state": "stopped", "system_managed": True})

    except Exception as exc:
        log_action_fn(config["db_path"], inst_id, "stop", "failed",
                        detail={"error": str(exc), "system_managed": True})
        return _error_response("DEPLOYMENT_FAILED", f"System stop failed: {exc}")

    return _error_response("UNKNOWN_ENGINE", f"Unknown system engine: {engine_type_name}")


def _restart_system_managed(inst_id, engine_type_name, log_action_fn):
    """Restart a system-managed instance (no Ansible).

    Uses PID-in-DB tracking via engine.execute(). No tmux.

    Args:
        inst_id: Integer primary key.
        engine_type_name: Engine type string.
        log_action_fn: Logging function reference.

    Returns:
        Success or error response dict.
    """
    from db.adapters.instances import get_instance as _gi, transition_state as _ts2

    config = _get_config()
    inst = _gi(config["db_path"], inst_id)
    if not inst:
        return _error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    log_action_fn(config["db_path"], inst_id, "restart", "received", detail={"system_managed": True})
    node_id = inst.get("node_id")

    try:
        if engine_type_name == "quickrobot-webui":
            try:
                from engine.quickrobot_webui import QrWebuiEngine
                engine = QrWebuiEngine()
                result = engine.execute(inst_id, "restart", config["db_path"])

                log_action_fn(config["db_path"], inst_id, "restart", "success",
                                detail={"system_managed": True, "engine": engine_type_name})
                try:
                    _ts2(config["db_path"], inst_id, "starting")
                    _time.sleep(0.2)
                    _ts2(config["db_path"], inst_id, "running")
                except Exception:
                    pass
                try:
                    log_ansible_action(config["db_path"], "restart_instance", node_id, inst_id,
                            "webui (quickrobot-webui)", {"action": "restart", "engine_type": engine_type_name,
                                    "system_managed": True},
                            {"changed": True, "failed": False, "results": result})
                except Exception:
                    pass  # non-critical
                return _success_single({"action": "restart", "instance_id": inst_id,
                                    "state": "running", "system_managed": True,
                                    "          pid": result.get("pid")})
            except Exception as exc:
                log_action_fn(config["db_path"], inst_id, "restart", "failed",
                                detail={"error": str(exc), "system_managed": True})
                try:
                    log_ansible_action(config["db_path"], "restart_instance", node_id, inst_id,
                            "webui (quickrobot-webui)", {"action": "restart", "engine_type": engine_type_name,
                                    "system_managed": True},
                            {"changed": False, "failed": True, "results": {"error": str(exc)}})
                except Exception:
                    pass
            return _error_response("DEPLOYMENT_FAILED", f"Web UI restart failed: {exc}")

        elif engine_type_name == "quickrobot-api":
            try:
                _ts2(config["db_path"], inst_id, "running")
            except Exception:
                pass
            log_action_fn(config["db_path"], inst_id, "restart", "success",
                            detail={"system_managed": True, "engine": engine_type_name,
                                    "message": "quickrobot service reloads on next request"})
            try:
                log_ansible_action(config["db_path"], "restart_instance", node_id, inst_id,
                        "local (quickrobot-api)", {"action": "restart", "engine_type": engine_type_name,
                                    "system_managed": True},
                            {"changed": False, "failed": False, "results": {"reloads_on_next_request": True}})
            except Exception:
                pass  # non-critical
            return _success_single({"action": "restart", "instance_id": inst_id,
                                "state": "running", "system_managed": True,
                                "message": "quickrobot service will reload on next request"})

        elif engine_type_name == "quickrobot-mcp":
            from engine.quickrobot_mcp import QrMcpEngine
            engine = QrMcpEngine()
            result = engine.execute(inst_id, "restart", config["db_path"])
            if result.get("error"):
                log_action_fn(config["db_path"], inst_id, "restart", "failed",
                                detail={"system_managed": True, "engine": engine_type_name,
                                        "error": result["error"]})
                return _error_response("DEPLOYMENT_FAILED", f"MCP restart failed: {result['error']}")
            log_action_fn(config["db_path"], inst_id, "restart", "success",
                            detail={"system_managed": True, "engine": engine_type_name})
            try:
                _ts2(config["db_path"], inst_id, "starting")
                _time.sleep(0.2)
                _ts2(config["db_path"], inst_id, "running")
            except Exception:
                pass
            try:
                log_ansible_action(config["db_path"], "restart_instance", node_id, inst_id,
                        "mcp (quickrobot-mcp)", {"action": "restart", "engine_type": engine_type_name,
                            "system_managed": True},
                        {"changed": True, "failed": False, "results": result})
            except Exception:
                pass  # non-critical
            return _success_single({"action": "restart", "instance_id": inst_id,
                                "state": "running", "system_managed": True,
                                "pid": result.get("pid")})

    except Exception as exc:
        log_action_fn(config["db_path"], inst_id, "restart", "failed",
                        detail={"error": str(exc), "system_managed": True})
        return _error_response("DEPLOYMENT_FAILED", f"System restart failed: {exc}")

    return _error_response("UNKNOWN_ENGINE", f"Unknown system engine: {engine_type_name}")


def _start_system_managed(inst_id, engine_type_name, log_action_fn):
    """Start a system-managed instance (no Ansible).

    Uses PID-in-DB tracking via engine.execute(). No tmux.

    Args:
        inst_id: Integer primary key.
        engine_type_name: Engine type string.
        log_action_fn: Logging function reference.

    Returns:
        Success or error response dict.
    """
    from db.adapters.instances import get_instance as _gi, transition_state as _ts2

    config = _get_config()
    inst = _gi(config["db_path"], inst_id)
    if not inst:
        return _error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    node_id = inst.get("node_id")
    log_action_fn(config["db_path"], inst_id, "start", "received", detail={"system_managed": True})

    try:
        if engine_type_name == "quickrobot-webui":
            from engine.quickrobot_webui import QrWebuiEngine
            engine = QrWebuiEngine()
            result = engine.execute(inst_id, "start", config["db_path"])

            if result.get("error"):
                log_action_fn(config["db_path"], inst_id, "start", "failed",
                                detail={"system_managed": True, "engine": engine_type_name,
                                        "error": result["error"]})
                return _error_response("DEPLOYMENT_FAILED", f"Web UI start failed: {result['error']}")

            log_action_fn(config["db_path"], inst_id, "start", "success",
                            detail={"system_managed": True, "engine": engine_type_name})
            try:
                _ts2(config["db_path"], inst_id, "running")
            except Exception:
                pass
            try:
                log_ansible_action(config["db_path"], "start_instance", node_id, inst_id,
                        "webui (quickrobot-webui)", {"action": "start", "engine_type": engine_type_name,
                            "system_managed": True},
                        {"changed": True, "failed": False, "results": result})
            except Exception:
                pass
            return _success_single({"action": "start", "instance_id": inst_id,
                                "state": "running", "system_managed": True})

        elif engine_type_name == "quickrobot-api":
            try:
                _ts2(config["db_path"], inst_id, "running")
            except Exception:
                pass
            log_action_fn(config["db_path"], inst_id, "start", "success",
                            detail={"system_managed": True, "engine": engine_type_name,
                                "message": "Already running (API server itself)"})
            try:
                log_ansible_action(config["db_path"], "start_instance", node_id, inst_id,
                        "local (quickrobot-api)", {"action": "start", "engine_type": engine_type_name,
                                "system_managed": True},
                        {"changed": False, "failed": False, "results": {"already_running": True}})
            except Exception:
                pass
            return _success_single({"action": "start", "instance_id": inst_id,
                                "state": "running", "system_managed": True,
                                "message": "Already running (API server itself)"})

        elif engine_type_name == "quickrobot-mcp":
            from engine.quickrobot_mcp import QrMcpEngine
            engine = QrMcpEngine()
            result = engine.execute(inst_id, "start", config["db_path"])

            if result.get("error"):
                log_action_fn(config["db_path"], inst_id, "start", "failed",
                                detail={"system_managed": True, "engine": engine_type_name,
                                        "error": result["error"]})
                return _error_response("DEPLOYMENT_FAILED", f"MCP start failed: {result['error']}")

            log_action_fn(config["db_path"], inst_id, "start", "success",
                            detail={"system_managed": True, "engine": engine_type_name})
            try:
                _ts2(config["db_path"], inst_id, "running")
            except Exception:
                pass
            try:
                log_ansible_action(config["db_path"], "start_instance", node_id, inst_id,
                        "mcp (quickrobot-mcp)", {"action": "start", "engine_type": engine_type_name,
                            "system_managed": True},
                        {"changed": True, "failed": False, "results": result})
            except Exception:
                pass
            return _success_single({"action": "start", "instance_id": inst_id,
                                "state": "running", "system_managed": True})

    except Exception as exc:
        log_action_fn(config["db_path"], inst_id, "start", "failed",
                        detail={"error": str(exc), "system_managed": True})
        return _error_response("DEPLOYMENT_FAILED", f"System start failed: {exc}")

    return _error_response("UNKNOWN_ENGINE", f"Unknown system engine: {engine_type_name}")


# ---------------------------------------------------------------------------
# Generic manage action runner
# ---------------------------------------------------------------------------

def _run_manage_action(inst_id, engine_type_name, node_id, action):
    """Run the universal manage_instance playbook on the remote node.

    Args:
        inst_id: Integer instance ID.
        engine_type_name: Engine type (llama_server, rpc, etc.).
        node_id: Target node ID.
        action: One of 'start', 'stop', 'restart'.

    Returns:
        dict with 'success' bool and optional 'error' string.
    """
    config = _get_config()

    if node_id is None:
        return {"success": False, "error": "No target node"}

    nd = get_node(config["db_path"], node_id)
    hostname = (nd.get("ansible_inventory_host") or
                nd.get("hostname") or
                nd.get("name")) if nd else None
    if not hostname:
        return {"success": False, "error": "No hostname for node"}

    # Dynamic inventory — no file generated (DI-7)
    try:
        r = _execute_playbook("MANAGE_INSTANCE_V1", resolver_type="playbook_id",
                               limit=hostname,
                               extra_vars={
                                    "inventory_host": hostname,
                                    "instance_id": inst_id,
                                    "engine_type": engine_type_name,
                                    "action": action,
                                },
                               action_type="restart_instance")
        if r["error"]:
            return {"success": False, "error": r["error"]}
        result = r.get("result") or {}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    # Log ansible execution to persistent store
    try:
        log_ansible_action(config["db_path"], f"{action}_instance", node_id, inst_id,
                            "manage_instance.yml", {"action": action, "engine_type": engine_type_name}, result)
    except Exception:
        pass  # Non-critical — logging failure doesn't break manage action

    # Extract service status from playbook results for better error reporting
    svc_status = None
    playbook_error = ""
    plays = result.get("results", {}).get("plays", [])
    for play in plays:
        for task in play.get("tasks", []):
            tname = task.get("task", {}).get("name", "")
            for entry in task.get("results", []):
                if "Get service status" in tname:
                    svc_status = entry.get("stdout", "")
                if "Report result" in tname and entry.get("msg"):
                    playbook_error = entry.get("msg", "")

    return {"success": not result.get("failed", False),
            "changed": result.get("changed", False),
            "svc_status": svc_status,
            "playbook_msg": playbook_error,
            "results": result.get("results", {})}


# ---------------------------------------------------------------------------
# Iperf3 client helper
# ---------------------------------------------------------------------------

def _run_iperf3_client(inst_id, engine_type_name, node_id, inv_hostname):
    """Run an iperf3 client to completion and return benchmark results.

    Starts the client service via systemctl, polls until it exits (one-shot),
    then fetches the log file and parses throughput results.

    Args:
        inst_id: Integer instance ID.
        engine_type_name: Engine type string ("iperf3").
        node_id: Target node ID.
        inv_hostname: Resolved hostname for the target node.

    Returns:
        success_single dict with action, log content, parsed results, or error_response.
    """
    from db.adapters.instances import get_instance as _gi, transition_state as _ts2, \
        log_action as _log

    config = _get_config()

    try:
        # Start the client service (one-shot execution)
        start_result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
        if not start_result.get("success"):
            return _error_response("START_FAILED",
                                f"Client start failed: {start_result.get('error', 'unknown')}")

        _log(config["db_path"], inst_id, "client_run", "started")

        # Transition to starting state while running
        try:
            _ts2(config["db_path"], inst_id, "starting")
        except Exception:
            pass

        # Poll until the service exits (one-shot run)
        start_wait = _time.time()
        max_wait = 300  # 5 minutes max
        client_log = ""
        exit_ok = False

        while _time.time() - start_wait < max_wait:
            _time.sleep(3)
            try:
                status_result = _run_manage_action(inst_id, engine_type_name, node_id, "status")
                if not isinstance(status_result, dict):
                    status_result = {"failed": True, "error": str(status_result)}

                # Check if the service is still active
                plays = status_result.get("results", {}).get("plays", [])
                is_active = False
                for play in plays:
                    for task in play.get("tasks", []):
                        tname = task.get("task", {}).get("name", "")
                        for entry in task.get("results", []):
                            if "Get service status" in tname:
                                stdout_val = entry.get("stdout", "").strip()
                                if stdout_val == "active":
                                    is_active = True
                if not is_active:
                    exit_ok = True
                    break

            except Exception:
                continue  # Poll failure, try again

        if not exit_ok:
            # Timeout — stop and log
            _run_manage_action(inst_id, engine_type_name, node_id, "stop")
            return _error_response("TIMEOUT",
                                f"Client run exceeded {max_wait}s timeout")

        # Fetch the log file content from remote node
        import subprocess as _sub
        log_path = f"/var/log/qr/iperf3-{inst_id}.log"
        ssh_cmd = (
            f'ssh -o ConnectTimeout=10 {DEFAULT_ANSIBLE_USER}@{inv_hostname} '
            f"'tail -100 {log_path} 2>/dev/null || echo \"(no log found)\"'"
        )
        try:
            log_proc = _sub.run(ssh_cmd, capture_output=True, text=True, timeout=15)
            client_log = (log_proc.stdout or "(no log available)").strip()
        except Exception:
            client_log = "(unable to retrieve log)"

        # Parse iperf3 log output for throughput results
        parsed = _parse_iperf3_log(client_log)

        # Transition to deployed (client ran once and finished)
        try:
            _ts2(config["db_path"], inst_id, "deployed")
        except Exception:
            pass

        _log(config["db_path"], inst_id, "client_run", "success",
                    detail={"sent_mbits": parsed.get("sent_mbits"),
                            "received_mbits": parsed.get("received_mbits")})

        return _success_single({
            "action": "run_client",
            "instance_id": inst_id,
            "success": True,
            "log_file": f"/var/log/qr/iperf3-{inst_id}.log",
            "log_excerpt": client_log[:2000],
            "parsed_results": parsed,
        })

    except Exception as exc:
        _log(config["db_path"], inst_id, "client_run", "failed",
                    detail={"error": str(exc)})
        return _error_response("CLIENT_RUN_ERROR", str(exc))


def _parse_iperf3_log(log_text):
    """Parse iperf3 output for throughput results.

    Args:
        log_text: Raw iperf3 log output string.

    Returns:
        dict with keys: sent_mbits, received_mbits, duration_seconds,
            sender_loss_pct, receiver_loss_pct. All numeric values are None
            if not found in the log.
    """
    import re as _re
    result = {
        "sent_mbits": None,
        "received_mbits": None,
        "duration_seconds": None,
        "sender_loss_pct": None,
        "receiver_loss_pct": None,
    }

    if not log_text or "iperf" not in log_text.lower():
        return result

    # Match summary lines like: "[  5]   0.00-40.28  sec  75.3 GBytes  16.1 Gbits/sec    0            sender"
    pattern = (
        r'\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+'
        r'([\d.]+)\s+(Bytes|KBytes|MBytes|GBytes|TBytes)\s+'
        r'([\d.]+)\s+(bits/sec|Kbits/sec|Mbits/sec|Gbits/sec|Tbits/sec)'
    )
    matches = list(_re.finditer(pattern, log_text))
    if matches:
        last = matches[-1]
        # Duration from last interval
        try:
            result["duration_seconds"] = float(last.group(2)) - float(last.group(1))
        except (ValueError, TypeError):
            pass
        # Bandwidth from last interval
        bw_val = float(last.group(5))
        bw_unit = last.group(6).lower()
        if "tbits" in bw_unit:
            result["sent_mbits"] = bw_val * 1000000
        elif "gbits" in bw_unit:
            result["sent_mbits"] = bw_val * 1000
        elif "mbits" in bw_unit:
            result["sent_mbits"] = bw_val
        elif "kbits" in bw_unit:
            result["sent_mbits"] = bw_val / 1000

    # Find receiver line (contains both sender and receiver in summary)
    recv_pattern = (
        r'\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+'
        r'([\d.]+)\s+(Bytes|KBytes|MBytes|GBytes|TBytes)\s+'
        r'([\d.]+)\s+(bits/sec|Kbits/sec|Mbits/sec|Gbits/sec|Tbits/sec)\s+\d+\s+\S+\s+receiver'
    )
    recv_match = _re.search(recv_pattern, log_text, _re.IGNORECASE)
    if recv_match:
        bw_val = float(recv_match.group(5))
        bw_unit = recv_match.group(6).lower()
        if "tbits" in bw_unit:
            result["received_mbits"] = bw_val * 1000000
        elif "gbits" in bw_unit:
            result["received_mbits"] = bw_val * 1000
        elif "mbits" in bw_unit:
            result["received_mbits"] = bw_val
        elif "kbits" in bw_unit:
            result["received_mbits"] = bw_val / 1000

    # Find loss percentage: e.g., "0.00% loss"
    loss_match = _re.search(r'([\d.]+)%\s+loss', log_text)
    if loss_match:
        result["receiver_loss_pct"] = float(loss_match.group(1))

    return result


# ---------------------------------------------------------------------------
# UUID verification
# ---------------------------------------------------------------------------

def check_remote_uuids(db_path, instance_id):
    """Preflight check: verify remote systemd unit UUIDs match DB records.

    Scans the target node for qr-*.service files, parses QR_UUID
    from each, and compares against DB records for instances on that node.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Instance id being deployed (used to find node context).

    Returns:
        dict with keys:
            - mismatches: list of {unit_key, remote_uuid, expected_uuid}
            - stray_units: list of {unit_key, uuid} for units not in DB
            - warnings: list of strings for logging
    """
    import re as _re_match
    from db.adapters.instances import get_instance as _gi
    from db.sqlite import pool

    config = _get_config()
    results = {
        "mismatches": [],
        "stray_units": [],
        "warnings": [],
    }

    inst = _gi(db_path, instance_id)
    if inst is None:
        results["warnings"].append("Instance not found for UUID check")
        return results

    node_id = inst.get("node_id")
    node_name = inst.get("node_name", "")

    if not node_id or not node_name:
        results["warnings"].append("No node context for UUID check")
        return results

    # Get all DB instance UUIDs for this node, including engine type
    with pool(db_path) as conn:
        db_uuid_map = {}
        for row in conn.execute(
            "SELECT i.id, i.name, i.instance_uuid, e.name as engine_type_name "
            "FROM instances i JOIN engine_types e ON i.engine_type_id = e.id "
            "WHERE i.node_id = ?",
            (node_id,),
        ):
            db_uuid_map[row["name"]] = {
                "id": row["id"],
                "uuid": row["instance_uuid"],
                "engine_type_name": row["engine_type_name"],
            }

    if not db_uuid_map:
        results["warnings"].append("No instances in DB for this node")
        return results

    # Build full set of valid unit keys for this node
    valid_unit_keys = set()
    for db_name, db_info in db_uuid_map.items():
        eng = db_info["engine_type_name"]
        key = f"qr-{db_info['id']}-{eng}"
        valid_unit_keys.add(key)

    # Run Ansible ad-hoc to grep QR_UUID from systemd unit files
    import subprocess as _sub
    try:
        inv_script = os.path.join(PROJECT_ROOT, "lib", "qr_dynamic_inventory.py")
        result = _sub.run(
            [
                "ansible", node_name, "-i", inv_script,
                "-m", "shell",
                "-a", "grep -h 'QR_UUID' /etc/systemd/system/qr-*.service 2>/dev/null || true",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            results["warnings"].append(f"UUID check failed on node '{node_name}': {result.stderr.strip()}")
            return results

        # Parse UUIDs from output
        remote_uuid_map = {}
        for line in (result.stdout or "").strip().splitlines():
            line = line.strip()
            if "QR_UUID=" not in line:
                continue
            unit_file = line.split(":")[0].strip() if ":" in line else "unknown"
            uuid_val = line.split("QR_UUID=")[1].strip() if "QR_UUID=" in line else ""
            # Extract instance name from unit file path
            name_match = _re_match.search(r'qr-(\d+)-(\w+)\.service', unit_file)
            if name_match:
                unit_key = f"qr-{name_match.group(1)}-{name_match.group(2)}"
                remote_uuid_map[unit_key] = uuid_val

        # Compare DB vs remote — detect mismatches
        for db_name, db_info in db_uuid_map.items():
            eng = db_info["engine_type_name"]
            unit_key = f"qr-{db_info['id']}-{eng}"
            if unit_key not in valid_unit_keys:
                continue
            db_uuid = db_info["uuid"]
            remote_uuid = remote_uuid_map.get(unit_key)

            if remote_uuid is None:
                results["warnings"].append(
                    f"Missing service for '{db_name}' (id:{db_info['id']}, uuid:{db_uuid}) "
                    f"on node '{node_name}'"
                )
            elif remote_uuid != db_uuid:
                results["mismatches"].append({
                    "unit_key": unit_key,
                    "remote_uuid": remote_uuid,
                    "expected_uuid": db_uuid,
                    "instance_name": db_name,
                })

        # Check for stray units not in DB
        for remote_key, remote_uuid in remote_uuid_map.items():
            if remote_key not in valid_unit_keys:
                results["stray_units"].append({
                    "unit_key": remote_key,
                    "uuid": remote_uuid,
                })

    except Exception as exc:
        results["warnings"].append(f"UUID check exception on node '{node_name}': {exc}")

    return results


# ---------------------------------------------------------------------------
# Node build state
# ---------------------------------------------------------------------------

def _get_node_build_state(db_path, node_id):
    """Read the node_build_state from the nodes table.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.

    Returns:
        String state ('idle' or 'running'), defaults to 'idle'.
    """
    try:
        with db_pool(db_path) as conn:
            row = conn.execute(
                "SELECT node_build_state FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return row[0] if row and row[0] else "idle"
    except Exception:
        return "idle"


# ---------------------------------------------------------------------------
# Deploy instance — main deployment logic
# ---------------------------------------------------------------------------

def deploy_instance(db_path, instance_id, playbook=None, async_mode=False, skip_build=None):
    """Deploy/redeploy an instance to its target node via Ansible.

    Dynamically selects the playbook based on engine_type_name, validates
    port and host values from merged config, and passes extra_vars including
    the new merged_env/merged_cli_opts fields for llama_server deployment.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key of the instance.
        playbook: Optional explicit playbook filename. If None, auto-detected
                    from engine_type_name (e.g., "deploy_llama_server.yml").
        async_mode: If True, run only sync preflight + git, then return.
                    The caller is responsible for starting async build thread.

    Returns:
        dict with deployment result keys: success (bool), message (str).
    """
    import os as _os
    import re as _re
    from db.adapters.instances import get_instance as _gi, transition_state as _ts2, log_action as _log
    from lib.lib_ansible_runner import run_playbook

    config = _get_config()

    inst = _gi(db_path, instance_id)
    if inst is None:
        return {"success": False, "message": f"Instance {instance_id} not found"}

    engine_type_name = inst.get("engine_type_name", "")
    node_id = inst.get("node_id")
    current_state = inst.get("state", "")
    instance_name = inst.get("name", "")

    # Resolve node hostname
    nd = get_node(db_path, node_id) if node_id else None
    if not nd:
        return {"success": False, "message": f"Node {node_id} not found for instance {instance_id}"}

    hostname = (nd.get("ansible_inventory_host") or
                nd.get("hostname") or
                nd.get("name"))

    if not hostname:
        return {"success": False, "message": f"No hostname for node {node_id}"}

    # Resolve port
    merged = merge_configs(db_path, instance_id) if callable(merge_configs) else {}
    port = inst.get("port_assigned", 0)
    if not port:
        return {"success": False, "message": f"No port assigned for instance {instance_id}"}

    # Determine playbook to use
    if not playbook:
        playbook_map = {
            "llama_server": "DEPLOY_LLAMA_SERVER_V1",
            "llama_rpc": "DEPLOY_LLAMA_RPC_V1",
            "iperf3": "DEPLOY_IPERF3_V1",
            "universal": "DEPLOY_UNIVERSAL_V1",
        }
        playbook = playbook_map.get(engine_type_name)
        if not playbook:
            return {"success": False, "message": f"Unknown engine type: {engine_type_name}"}

    # Resolve extra_vars for the deploy playbook
    env = merged.get("env", {}) if isinstance(merged, dict) else {}
    cli_opts = list(merged.get("cli_opts", [])) if isinstance(merged, dict) else []

    # Build extra_vars
    extra_vars = {
        "inventory_host": hostname,
        "instance_id": instance_id,
        "instance_name": instance_name,
        "engine_type": engine_type_name,
        "instance_port": port,
        "binary_path": env.get("binary_path", ""),
        "device": inst.get("gpu_device") or None,
        "start_on_boot": bool(inst.get("start_on_boot", 0)),
        "start_after_deploy": bool(inst.get("start_after_deploy", 0)),
        "rpc_host": env.get("LLAMA_ARG_HOST") or QR_DEFAULT_BIND_HOST,
        "merged_env": env,
        "merged_cli_opts": cli_opts,
        "instance_env_vars": [],
        "remote_node_user": nd.get("ansible_user") or DEFAULT_ANSIBLE_USER,
    }

    # --- Cluster binding: use builder results for llama_server/rpc ---
    if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
        cluster_result = build_llama_server_env(db_path, instance_id)
        extra_vars["merged_env"] = cluster_result["env"]
        extra_vars["merged_cli_opts"] = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []
        extra_vars["tensor_split_value"] = cluster_result["tensor_split_str"]
        extra_vars["split_mode_value"] = cluster_result["split_mode"]
        raw_bind = inst.get("rpc_bind_ids") or "[]"
        bind_ids = json.loads(raw_bind) if isinstance(raw_bind, str) else list(raw_bind or [])
        extra_vars["rpc_bind_ids"] = bind_ids
    elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
        cluster_result = build_rpc_server_env(db_path, instance_id)
        extra_vars["merged_env"] = cluster_result["env"]
        extra_vars["merged_cli_opts"] = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []

    # Preflight: check_remote_uuids
    uuid_check = check_remote_uuids(db_path, instance_id)
    if uuid_check.get("mismatches"):
        msg = f"UUID mismatches on {hostname}: " + "; ".join(
            f"{m['unit_key']} remote={m['remote_uuid']} expected={m['expected_uuid']}"
            for m in uuid_check["mismatches"]
        )
        _log(db_path, instance_id, "deploy", "preflight_warning", {"uuid_check": uuid_check})

    # Run the deploy playbook
    _ts2(db_path, instance_id, "deploying") if current_state != "running" else _ts2(db_path, instance_id, "updating")
    r = _execute_playbook(playbook, resolver_type="playbook_id",
                          limit=hostname, extra_vars=extra_vars, timeout=3600,
                          node_id=node_id, instance_id=instance_id,
                          action_type="deploy_instance")

    if r["error"]:
        _log(db_path, instance_id, "deploy", "playbook_error", {"error": r["error"]})
        _ts2(db_path, instance_id, "error")
        return {"success": False, "message": f"Deploy playbook error: {r['error']}"}

    result = r.get("result") or {}
    if result.get("failed"):
        _log(db_path, instance_id, "deploy", "playbook_failed", {"error": str(result)})
        _ts2(db_path, instance_id, "error")
        return {"success": False, "message": "Deploy playbook reported failure"}

    # Post-deploy state transitions
    if current_state == "running":
        _ts2(db_path, instance_id, "deployed")
    else:
        _ts2(db_path, instance_id, "deploying")
        _ts2(db_path, instance_id, "deployed")

    # Handle start_after_deploy
    if inst.get("start_after_deploy", 0):
        _ts2(db_path, instance_id, "starting")
        # Start service via manage action
        start_result = _run_manage_action(instance_id, engine_type_name, node_id, "start")
        if start_result.get("success"):
            _ts2(db_path, instance_id, "running")
        else:
            _log(db_path, instance_id, "deploy", "start_failed", {"error": start_result})

    _log(db_path, instance_id, "deploy", "success", {"playbook_id": playbook})
    return {"success": True, "message": f"Instance {instance_id} deployed to {hostname}:{port}"}


# ---------------------------------------------------------------------------
# query_status wrapper — standalone function for _wait_for_stop_status
# ---------------------------------------------------------------------------

def query_status(db_path, inst_id):
    """Query status of an instance via its engine's query_status method.

    This is a standalone helper used by _wait_for_stop_status. The route
    handler api_query_status returns a full HTTP response; this function
    returns the raw result dict for programmatic use.

    Args:
        db_path: Path to SQLite database.
        inst_id: Instance primary key.

    Returns:
        dict with keys: status, data (dict), or error.
    """
    from db.adapters.instances import get_instance as _gi
    from engine import get_engine

    inst = _gi(db_path, inst_id)
    if inst is None:
        return {"status": "error", "data": {"error": f"Instance {inst_id} not found"}}

    engine_type = inst.get("engine_type_name", "")
    engine = get_engine(engine_type)
    if engine is None:
        alt_name = engine_type.replace("-", "_")
        engine = get_engine(alt_name)
    if engine is None:
        alt_name = engine_type.replace("_", "-")
        engine = get_engine(alt_name)

    if engine:
        raw_result = engine.query_status(inst_id, db_path)
    else:
        raw_result = {"alive": False, "latency_ms": None, "error": f"Engine '{engine_type}' not loaded"}

    return {"status": "ok", "data": raw_result}
