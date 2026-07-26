"""System health, logs, and app status endpoints for quickrobot.

Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
import logging
from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_API_NAME, QR_ENGINE_WEBUI_NAME, get_system_instance_id
from db.sqlite import pool as db_pool
from lib.lib_constants import DEFAULT_ANSIBLE_USER, VERSION
from engine import get_engine, get_engine_capabilities


def _fsi(db_path, engine_name):
    """Find system-managed instance by engine type name.

    Returns the instance dict (with 'id', 'state', etc.) or None if not found.
    Uses SSOT constant get_system_instance_id() for reliable lookup.
    """
    from db.adapters.instances import get_instance as _gi
    inst_id = get_system_instance_id(engine_name)
    if inst_id is None:
        return None
    return _gi(db_path, inst_id)

logger = logging.getLogger(__name__)

def api_app_status():
    """Return app-level status + instance summary for WebUI consumption.

    Adds global_state (computed from active-host instances) and
    global_state_rgb (server-computed color string).
    Also returns instance_counts (by state) and total_instances.
    """
    from db.adapters.instances import list_instances as _li
    from db.adapters.nodes import list_nodes as _ln
    from lib.qr_engine_ids import QR_STATUS_COLORS as _qrsc

    active_node_ids = {n["id"] for n in _ln(_CONFIG["db_path"]) if n.get("status") == "active"}
    all_instances = _li(_CONFIG["db_path"])
    # Apply system-managed state override so DB errors on live processes don't affect global indicator
    from qr_api.lib_instances import override_system_instance_states as _osis
    _osis(all_instances, _CONFIG)

    # Counts for tooltip (all instances) and global state (active hosts only)
    counts = {"running": 0, "error": 0, "build_error": 0,
              "stopped": 0, "not_running": 0, "other": 0}
    ac = {"running": 0, "error": 0, "build_error": 0,
          "stopped": 0, "not_running": 0, "other": 0}

    for inst in all_instances:
        s = inst.get("state", "other")
        if s == "running": counts["running"] += 1
        elif s in ("error", "build_error"): counts["error"] += 1
        elif s == "stopped": counts["stopped"] += 1
        else: counts["not_running"] += 1  # deploying, starting, compiling, etc.

        if inst.get("node_id") in active_node_ids:
            if s == "running": ac["running"] += 1
            elif s in ("error", "build_error"): ac["error"] += 1
            elif s == "stopped": ac["stopped"] += 1
            else: ac["not_running"] += 1

    total = sum(counts.values())
    act_total = sum(ac.values())

    # Priority: error > stopped/not_running > running > idle
    if act_total == 0:
        state_key = "idle"
    elif ac["error"] > 0:
        state_key = "error"
    elif ac["stopped"] > 0 or ac["not_running"] > 0:
        state_key = "stopped"
    else:
        state_key = "running"

    r, g, b = _qrsc[state_key]
    rgb_str = f"rgb({r}, {g}, {b})"

    # Tooltip: per-state counts from active hosts
    parts = []
    if ac.get("running"): parts.append(f"{ac['running']} running")
    err_count = ac.get("error", 0) + ac.get("build_error", 0)
    if err_count: parts.append(f"{err_count} error")
    if ac.get("stopped"): parts.append(f"{ac['stopped']} stopped")
    if ac.get("not_running"): parts.append(f"{ac['not_running']} in-progress")
    tooltip = f"Instances: {total} total, active hosts only — " + (", ".join(parts) if parts else "none")

    return success_single({
        "version": VERSION,
        "mode": _CONFIG.get("pb_mode", "prod"),
        "bind_host": _CONFIG.get("host", "127.0.0.1"),
        "bind_port": _CONFIG.get("api_port", 8039),
        "global_state": state_key,
        "global_state_rgb": rgb_str,
        "instance_counts": counts,
        "total_instances": total,
        "global_state_tooltip": tooltip,
    })


# ---------------------------------------------------------------------------
# Log Management
# ---------------------------------------------------------------------------

def api_cleanup_null_logs():
    """Remove orphaned log entries with NULL FK references.

    After migration 010 changed FK constraints from ON DELETE CASCADE to
    ON DELETE SET NULL, deleted nodes/instances leave behind log rows
    with NULL node_id or instance_id. This endpoint removes those
    orphaned entries on demand.

    Returns:
        { status: "ok", data: { instance_logs_deleted, ansible_actions_deleted } }
    """
    from db.adapters.logs import cleanup_null_log_entries
    deleted = cleanup_null_log_entries(_CONFIG["db_path"])
    return success_single(deleted)


def api_log_entries():
    """List unified log entries with optional filters.

    Queries the unified log_entries table (consolidated from jobs, tasks,
    ansible_actions, qr_actions, and instance_logs).

    Query params:
        status: Filter by status ('running', 'completed', 'failed', etc.)
        job_type: Filter by job type ('deploy', 'restart', 'reconfigure', etc.)
        instance_id: Filter by instance (int)
        node_id: Filter by node (int)
        parent_id: Filter by parent job ID (for task sub-rows)
        limit: Max results (default 50)
        offset: Pagination offset (default 0)

    Returns:
        { status: "ok", total: N, items: [...], offset: N }
    """
    from db.sqlite import pool
    import datetime as _dt

    status_filter = request.args.get("status")
    job_type = request.args.get("job_type")
    instance_id = request.args.get("instance_id", type=int)
    node_id = request.args.get("node_id", type=int)
    parent_id = request.args.get("parent_id", type=int)
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)

    query = ("SELECT le.id, le.parent_id, le.job_type, le.engine_type_name, "
             "le.instance_id, le.node_id, le.status, le.actor, "
             "le.error_message, le.created_at, le.started_at, "
             "le.finished_at, le.duration_ms, le.task_stage, "
             "le.stage_playbook, le.retry_count, le.max_retries, "
             "le.details_json, le.results_json, "
             "le.playbook_registry_id, le.playbook_version, "
             "n.name as node_name, i.name as instance_name "
             "FROM log_entries le "
             "LEFT JOIN nodes n ON le.node_id = n.id "
             "LEFT JOIN instances i ON le.instance_id = i.id "
             "WHERE 1=1")
    params = []

    if status_filter:
        query += " AND le.status = ?"
        params.append(status_filter)
    if job_type:
        query += " AND le.job_type = ?"
        params.append(job_type)
    if instance_id:
        query += " AND le.instance_id = ?"
        params.append(instance_id)
    if node_id:
        query += " AND le.node_id = ?"
        params.append(node_id)
    if parent_id is not None:
        query += " AND le.parent_id = ?"
        params.append(parent_id)

    # Job headers first (parent_id IS NULL), then task sub-rows
    query += " ORDER BY CASE WHEN le.parent_id IS NULL THEN 0 ELSE 1 END, le.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with pool(_CONFIG["db_path"]) as conn:
        rows = conn.execute(query, params).fetchall()

    items = []
    for row in rows:
        d = {k: row[k] for k in row.keys()}
        # Parse JSON fields if present
        for json_field in ("details_json", "results_json"):
            if d.get(json_field):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass
        items.append(d)

    # Get total count (excluding offset)
    count_query = ("SELECT COUNT(*) FROM log_entries le "
                   "LEFT JOIN nodes n ON le.node_id = n.id "
                   "LEFT JOIN instances i ON le.instance_id = i.id "
                   "WHERE 1=1")
    count_params = []
    if status_filter:
        count_query += " AND le.status = ?"
        count_params.append(status_filter)
    if job_type:
        count_query += " AND le.job_type = ?"
        count_params.append(job_type)
    if instance_id:
        count_query += " AND le.instance_id = ?"
        count_params.append(instance_id)
    if node_id:
        count_query += " AND le.node_id = ?"
        count_params.append(node_id)
    if parent_id is not None:
        count_query += " AND le.parent_id = ?"
        count_params.append(parent_id)

    with pool(_CONFIG["db_path"]) as conn:
        total = conn.execute(count_query, count_params).fetchone()[0]

    return success_list(items, total=total, meta={"offset": offset, "limit": limit})


def api_cleanup_log_entries():
    """Bulk delete log entries with optional filters.

    POST /api/v1/log_entries/cleanup with JSON body:
        { older_than_minutes: N }       — delete entries older than N minutes
        { older_than_minutes: N, status: "completed" } — only completed entries

    Returns:
        { status: "ok", data: { deleted_count } }
    """
    from db.sqlite import pool
    import datetime as _dt

    try:
        body = request.get_json(silent=True) or {}
        older_than = body.get("older_than_minutes", 4320)  # 3 days default
        status_filter = body.get("status")  # optional: only delete specific status
    except Exception as _e:
        logger.debug("body parse failed (clear_ansible_actions): %s", _e)
        return error_response("VALIDATION_ERROR", "Invalid JSON body")

    try:
        older_than = int(older_than)
    except (ValueError, TypeError):
        return error_response("VALIDATION_ERROR", "older_than_minutes must be an integer")

    if older_than < 1:
        older_than = 1

    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(minutes=older_than)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Protect running/recent jobs: never delete entries with status 'running', 'received', or 'queued'
    # unless the user explicitly filters by that status
    if not status_filter:
        query = "DELETE FROM log_entries WHERE created_at < ? AND status NOT IN ('running','received','queued')"
    else:
        query = "DELETE FROM log_entries WHERE created_at < ?"
    params = [cutoff]

    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)

    with pool(_CONFIG["db_path"]) as conn:
        cursor = conn.execute(query, params)
        deleted = cursor.rowcount
        conn.commit()

    return success_single({"deleted_count": deleted})


def api_health_check():
    """POST /api/v1/health/check — placeholder, see instance_health submodule for full health check."""
    return success_single({"status": "ok", "message": "Health check endpoint active"})

def api_list_system_engines():
    """List system-managed engine types (quickrobot-api, quickrobot-webui)."""
    from db.adapters.engine_types import list_engine_types as _let
    engine_types = _let(_CONFIG["db_path"], enabled_only=True)

    result = []
    for et in engine_types:
        # Check if this is a system-managed engine type
        cap = get_engine_capabilities(et["name"])
        is_system = (et["name"] in (QR_ENGINE_API_NAME, QR_ENGINE_WEBUI_NAME))
        if cap and cap.get("supports_models") is False and cap.get("supports_presets") is False:
            is_system = True

        et_entry = dict(et)
        et_entry["system_managed"] = is_system
        result.append(et_entry)

    return success_list(result)


def api_register_system_engine():
    """Register a new system-managed engine type."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import add_engine_type as _ae
    name = body.get("name")
    display_name = body.get("display_name", name)
    capabilities = body.get("capabilities", {})
    try:
        et = _ae(_CONFIG["db_path"], name=name, display_name=display_name,
                    module_path=f"engine.{name}", capabilities=capabilities)
        return success_single(et)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))


def api_quickrobot_api_status():
    """Get runtime status of the quickrobot API service (PID, RSS, uptime)."""
    svc_inst = _fsi(_CONFIG["db_path"], QR_ENGINE_API_NAME)
    if svc_inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Quickrobot API instance not found")

    engine = get_engine(QR_ENGINE_API_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Quickrobot API engine not loaded")

    status = engine.get_status(svc_inst["id"], _CONFIG["db_path"])
    return success_single(status)


def api_quickrobot_api_metrics():
    """Get detailed system metrics for the quickrobot API service."""
    svc_inst = _fsi(_CONFIG["db_path"], QR_ENGINE_API_NAME)
    if svc_inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Quickrobot API instance not found")

    engine = get_engine(QR_ENGINE_API_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Quickrobot API engine not loaded")

    metrics = engine.execute(svc_inst["id"], "metrics", _CONFIG["db_path"])
    return success_single(metrics)


def _get_webui_settings_from_engine_config(db_path, inst):
    """Retrieve WebUI settings from engine config table.

    Args:
        db_path: Path to the SQLite database.
        inst: Instance dict (system-managed webui instance).

    Returns:
        Dict mapping config key -> {value, description}, or empty dict on error.
    """
    try:
        from db.adapters.configs import get_engine_config as _gec
        engine_type_id = inst.get("engine_type_id", 0)
        if not engine_type_id:
            return {}
        configs = _gec(db_path, engine_type_id)
        if isinstance(configs, dict):
            return configs
    except Exception as e:
        logger.debug("_get_webui_settings_from_engine_config failed: %s", e)
    return {}


def api_api_settings():
    """Get current API server settings from engine config table."""
    try:
        from db.adapters.configs import get_engine_config as _gec
        from lib.qr_engine_ids import QR_ENGINE_API
        configs = _gec(_CONFIG["db_path"], QR_ENGINE_API)
        if isinstance(configs, dict):
            return success_single(configs)
        return success_single({})
    except Exception as e:
        logger.debug("api_api_settings failed: %s", e)
        return success_single({})
