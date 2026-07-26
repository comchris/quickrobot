import json

from flask import request, jsonify
import logging as _logging

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import is_llamacpp_engine, get_id_by_name
from qr_api.lib_instances import _check_node_active, _execute_playbook, _resolve_engine_playbook_id
from qr_api.lib_nodes import _scan_orphaned_units

logger = _logging.getLogger(__name__)


def api_instance_rebuild(inst_id):
    """Trigger a git pull + cmake recompile for a llama.cpp instance (async).

    Uses RUNNER-1 staged chain for consistent job/task tracking.
    job_type="rebuild" runs: source → compile → start stages.

    Supports both llama_server and rpc engine types. Uses node-level build
    coordination so only one build runs per host at a time.

    Returns immediately with job info. The build runs in the scheduler with a
    30-minute timeout. State transitions: updating → deployed (success)
    or updating → error (failure) or updating → timeout (30 min exceeded).
    """
    from db.adapters.instances import get_instance as _gi

    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    engine = inst.get("engine_type_name", "")
    if not is_llamacpp_engine(get_id_by_name(engine)):
        return error_response("UNSUPPORTED_ENGINE",
                                f"Update build supported for llama_server/llama_rpc, got: {engine}")

    node_id = inst.get("node_id")
    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = (nd.get("hostname") or nd.get("name"))
    if not hostname:
        return error_response("RESOURCE_NOT_FOUND", f"Host for node {node_id} not found")

    # Check if already updating (idempotent)
    current_state = inst.get("state", "")
    if current_state == "updating":
        return success_single({"action": "rebuild", "instance_id": inst_id,
                                "status": "already_updating", "node": hostname})

    # Use RUNNER-1 staged chain (job_type="rebuild" → source → compile → start)
    from lib.lib_runner import PlaybookRunner
    runner = PlaybookRunner(_CONFIG["db_path"])
    result = runner.chain(inst_id, job_type="rebuild",
                          actor="api", skip_build=False, async_mode=True)

    # Map chain() result to response format
    response = {"action": "rebuild", "instance_id": inst_id,
                "success": result.get("success"),
                "message": result.get("message", "")}
    if result.get("job_id"):
        response["job_id"] = result["job_id"]
    if result.get("tasks_created"):
        response["tasks_created"] = result["tasks_created"]
    return success_single(response)


def api_orphans():
    """List orphaned systemd units across all nodes.

    Cross-references remote qr-*.service files against DB instances.
    Returns list of {node_name, orphan_units: [{unit_key, uuid}]}.
    """
    result = _scan_orphaned_units(_CONFIG["db_path"])
    return success_single(result)


def api_force_delete_instance(inst_id):
    """Force-delete an instance from DB, optionally cleaning remote artifacts.

    Args:
        inst_id: Integer primary key of the instance to force-delete.
        Body (optional): {"clean_remote": true} — also undeploy on remote node.

    Returns:
        JSON with deleted instance info and cleanup results.
    """
    import os as _os
    from db.adapters.instances import get_instance, delete_instance, transition_state, log_action as _la

    from db.adapters.instances import check_system_managed as _csm_force

    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # Force-delete also blocks system-managed instances
    if _csm_force(_CONFIG["db_path"], inst_id):
        return error_response("SYSTEM_MANAGED_INSTANCE",
                                f"Instance {inst_id} is a system-managed engine and cannot be force-deleted.", 409)

    body = request.get_json(silent=True) or {}
    clean_remote = body.get("clean_remote", False)

    node_id = inst.get("node_id")
    engine_type_name = inst.get("engine_type_name", "unknown")

    # If cleaning remote, run undeploy first (same as normal undeploy)
    cleanup_result = {"remote_cleaned": False, "error": None}
    if clean_remote and node_id and node_id != 1:
        try:
            from lib.lib_ansible_runner import run_playbook
            from db.adapters.nodes import get_node as _gn

            nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
            hostname = (nd.get("ansible_inventory_host") or
                        nd.get("hostname") or
                        nd.get("name")) if nd else None

            pb_id = _resolve_engine_playbook_id("undeploy", engine_type_name)
            if pb_id and hostname:
                r = _execute_playbook(pb_id, resolver_type="playbook_id",
                                      limit=hostname,
                                      extra_vars={
                                          "inventory_host": hostname,
                                          "instance_id": inst_id,
                                          "engine_type": engine_type_name,
                                      },
                                      action_type="undeploy_instance")
                if r["error"]:
                    undeploy_result = {"failed": True, "error": r["error"]}
                else:
                    undeploy_result = r.get("result") or {}
                cleanup_result["remote_cleaned"] = not undeploy_result.get("failed", False)
                if undeploy_result.get("failed"):
                    cleanup_result["error"] = undeploy_result.get("error", "unknown")
            else:
                cleanup_result["remote_cleaned"] = False
        except Exception as exc:
            cleanup_result["error"] = str(exc)

    # Delete from DB
    _la(_CONFIG["db_path"], inst_id, "force_delete", "started",
        {"clean_remote": clean_remote, "cleanup": cleanup_result})
    delete_instance(_CONFIG["db_path"], inst_id)

    return success_single({
        "action": "force_delete",
        "instance_id": inst_id,
        "name": inst.get("name"),
        "clean_remote": clean_remote,
        "cleanup_result": cleanup_result,
    })


def api_ansible_actions():
    """List ansible action logs with optional filters.

    Joins nodes and instances tables to include names.
    Maps exit_code to string status and computes duration_ms.

    Query params:
        node_id: Filter by node (int).
        instance_id: Filter by instance (int).
        action_type: Filter by action type string.
        status: Filter by 'success' or 'failed'.
        limit: Max results (default 50).
    """
    from db.sqlite import pool
    import datetime as _dt

    node_id = request.args.get("node_id", type=int)
    instance_id = request.args.get("instance_id", type=int)
    action_type = request.args.get("action_type")
    status_filter = request.args.get("status")  # 'success' or 'failed'
    limit = request.args.get("limit", 50, type=int)

    # Build the base query with JOINs for node/instance/playbook names
    # log_entries: job_type=action_type, stage_playbook=playbook_name
    query = ("SELECT le.*, n.name as node_name, i.name as instance_name, "
                 "p.file_path as playbook_file, p.version as playbook_version "
                 "FROM log_entries le "
                 "LEFT JOIN nodes n ON le.node_id = n.id "
                 "LEFT JOIN instances i ON le.instance_id = i.id "
                 "LEFT JOIN playbook_registry p ON le.playbook_registry_id = p.id "
                 "WHERE 1=1 AND le.parent_id IS NULL")
    params = []

    if node_id:
        query += " AND le.node_id = ?"
        params.append(node_id)
    if instance_id:
        query += " AND le.instance_id = ?"
        params.append(instance_id)
    if action_type:
        query += " AND le.job_type = ?"
        params.append(action_type)

    query += " ORDER BY le.started_at DESC LIMIT ?"
    params.append(limit)

    with pool(_CONFIG["db_path"]) as conn:
        rows = conn.execute(query, params).fetchall()

    items = []
    for row in rows:
        d = {k: row[k] for k in row.keys()}
        # Parse results_json if present
        if d.get("results_json"):
            try:
                d["results_json"] = json.loads(d["results_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Use stored status (fall back to exit_code for legacy rows)
        if "status" not in d or not d["status"]:
            exit_code = d.get("exit_code", 1)
            d["status"] = "success" if exit_code == 0 else "failed"

        # Use started_at as created_at for WebUI compatibility
        d["created_at"] = d.get("started_at", "")

        # Calculate duration_ms from started_at/finished_at
        duration_ms_val = "N/A"
        started = d.get("started_at")
        finished = d.get("finished_at")
        if started and finished:
            try:
                start_dt = _dt.datetime.fromisoformat(started)
                end_dt = _dt.datetime.fromisoformat(finished)
                diff_seconds = (end_dt - start_dt).total_seconds()
                duration_ms_val = int(diff_seconds * 1000)
            except (ValueError, TypeError):
                duration_ms_val = "N/A"
        d["duration_ms"] = duration_ms_val

        # Apply status filter if provided (post-query filter)
        if status_filter and d["status"] != status_filter:
            continue

        items.append(d)

    return success_list(items, total=len(items))


def api_qr_actions():
    """List qr_actions entries with optional filters for running-task visibility.

    Query params:
        status: Filter by 'running', 'completed', 'failed', 'timeout', 'stuck'.
                Default (no filter) returns all entries.
        node_id: Filter by node (int).
        instance_id: Filter by instance (int).
        action_type: Filter by action type string.
        limit: Max results (default 50).

    Returns running tasks for real-time monitoring; completed tasks for audit trail.
    """
    from db.sqlite import pool
    import datetime as _dt

    status_filter = request.args.get("status")  # 'running', 'completed', etc.
    node_id = request.args.get("node_id", type=int)
    instance_id = request.args.get("instance_id", type=int)
    action_type = request.args.get("action_type")
    limit = request.args.get("limit", 50, type=int)

    query = ("SELECT le.*, n.name as node_name, i.name as instance_name "
             "FROM log_entries le "
             "LEFT JOIN nodes n ON le.node_id = n.id "
             "LEFT JOIN instances i ON le.instance_id = i.id "
             "WHERE 1=1 AND le.parent_id IS NULL")
    params = []

    if status_filter:
        query += " AND le.status = ?"
        params.append(status_filter)
    if node_id:
        query += " AND le.node_id = ?"
        params.append(node_id)
    if instance_id:
        query += " AND le.instance_id = ?"
        params.append(instance_id)
    if action_type:
        query += " AND le.job_type = ?"
        params.append(action_type)

    # Running tasks first, then newest
    query += " ORDER BY CASE WHEN le.status='running' THEN 0 ELSE 1 END, le.created_at DESC LIMIT ?"
    params.append(limit)

    with pool(_CONFIG["db_path"]) as conn:
        rows = conn.execute(query, params).fetchall()

    items = []
    for row in rows:
        d = {k: row[k] for k in row.keys()}
        # Compute duration_ms from timestamps if available
        duration_ms_val = "N/A"
        started = d.get("started_at")
        finished = d.get("finished_at")
        if started and finished:
            try:
                start_dt = _dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
                end_dt = _dt.datetime.fromisoformat(finished.replace("Z", "+00:00"))
                diff_seconds = (end_dt - start_dt).total_seconds()
                duration_ms_val = int(diff_seconds * 1000)
            except (ValueError, TypeError):
                pass
        elif started and d.get("status") == "running":
            # Compute live elapsed for running tasks
            try:
                start_dt = _dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
                elapsed = (_dt.datetime.now(_dt.timezone.utc) - start_dt).total_seconds()
                duration_ms_val = int(elapsed * 1000)
            except (ValueError, TypeError):
                pass
        d["duration_ms"] = duration_ms_val

        # Include playbook info if available
        pb_id = d.get("playbook_registry_id")
        if pb_id:
            try:
                from db.adapters.playbooks import resolve_playbook_by_id
                pb_rec = resolve_playbook_by_id(_CONFIG["db_path"], pb_id)
                if pb_rec:
                    d["playbook_file"] = pb_rec.get("file_path", "")
                    d["playbook_version"] = pb_rec.get("version", "")
            except Exception as _e:
                logger.debug("playbook resolve by ID failed (pb_id=%s): %s", pb_id, _e)

        items.append(d)

    return success_list(items, total=len(items))


def api_clear_old_ansible_actions():
    """Clear ansible action logs older than N days.

    JSON body: {"days": <int>} — delete all entries older than this many days.
    Returns: { status: "ok", data: { deleted_count } }
    """
    from db.sqlite import pool
    import datetime as _dt

    body = request.get_json(silent=True) or {}
    days = int(body.get("days", 7)) if body.get("days") is not None else 7
    if days == 0:
        # Clear all entries
        query = "DELETE FROM log_entries"
        with pool(_CONFIG["db_path"]) as conn:
            cur = conn.execute(query)
            deleted = cur.rowcount
            conn.commit()
        return success_single({"deleted_count": deleted, "clear_all": True})

    if days < 1:
        return error_response("days must be >= 0 (0=clear all)", code="invalid_params")

    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    query = "DELETE FROM log_entries WHERE started_at < ?"
    with pool(_CONFIG["db_path"]) as conn:
        cur = conn.execute(query, (cutoff,))
        deleted = cur.rowcount
        conn.commit()

    return success_single({"deleted_count": deleted})


def api_clear_old_qr_actions():
    """Clear qr action logs older than N days.

    JSON body: {"days": <int>} — delete all entries older than this many days.
    Returns: { status: "ok", data: { deleted_count } }
    """
    from db.sqlite import pool
    import datetime as _dt

    body = request.get_json(silent=True) or {}
    days = int(body.get("days", 7)) if body.get("days") is not None else 7
    if days == 0:
        # Clear all entries
        query = "DELETE FROM log_entries"
        with pool(_CONFIG["db_path"]) as conn:
            cur = conn.execute(query)
            deleted = cur.rowcount
            conn.commit()
        return success_single({"deleted_count": deleted, "clear_all": True})

    if days < 1:
        return error_response("days must be >= 0 (0=clear all)", code="invalid_params")

    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    query = "DELETE FROM log_entries WHERE started_at < ?"
    with pool(_CONFIG["db_path"]) as conn:
        cur = conn.execute(query, (cutoff,))
        deleted = cur.rowcount
        conn.commit()

    return success_single({"deleted_count": deleted})


# ---------------------------------------------------------------------------
# WebUI Settings (centralized browser settings store)
# ---------------------------------------------------------------------------

def api_home():
    """Overview dashboard data."""
    from db.adapters.nodes import list_nodes as _ln
    from db.adapters.engine_types import list_engine_types as _let
    from db.adapters.instances import list_instances as _li

    nodes = _ln(_CONFIG["db_path"])
    engine_types = _let(_CONFIG["db_path"], enabled_only=True)
    instances = _li(_CONFIG["db_path"])
    running = [i for i in instances if i["state"] == "running"]

    return success_single({
        "total_nodes": len(nodes),
        "active_nodes": len([n for n in nodes if n["status"] == "active"]),
        "total_instances": len(instances),
        "running_instances": len(running),
        "engine_types_count": len(engine_types),
        "recent_activity": [],
    })


# ---------------------------------------------------------------------------
# Ansible action log endpoints
# ---------------------------------------------------------------------------

def api_get_config():
    """Get all global config keys."""
    from db.adapters.configs import get_all_global_config as _ggc
    configs = _ggc(_CONFIG["db_path"])
    return success_single(configs)


def api_set_config(key):
    """Set/update a global config key."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.configs import set_global_config as _sgc
    value = body.get("value", "")
    description = body.get("description", "")
    _sgc(_CONFIG["db_path"], key, value, description)
    return success_single({"key": key, "value": value})


def api_delete_config(key):
    """Remove a global config key."""
    from db.sqlite import pool as _pool
    with _pool(_CONFIG["db_path"]) as conn:
        cursor = conn.execute(
            "DELETE FROM config_global WHERE key = ?", (key,)
        )
        deleted = cursor.rowcount > 0

    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Config key '{key}' not found")

    return success_single({"key": key, "deleted": True})


# ---------------------------------------------------------------------------
# Home / Dashboard endpoint
# ---------------------------------------------------------------------------

