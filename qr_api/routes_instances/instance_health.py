"""Instance health and log query endpoints for quickrobot instances API.

Includes: logs, journal, status (remote), health check, system instance status,
and general query status. These are supplementary to the main CRUD/status_queries modules.
"""

import json
import logging
from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response
from qr_api import _CONFIG, _START_TIME
from lib.qr_engine_ids import (
    QR_DEFAULT_LOCALHOST, QR_ENGINE_API_NAME, QR_ENGINE_WEBUI_NAME,
    QR_ENGINE_MCP_NAME, QR_ENGINE_SCHEDULER_NAME,
    QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_PORT_DEFAULTS,
)
from lib.lib_constants import DEFAULT_ANSIBLE_USER
from db.sqlite import pool as db_pool
from lib.lib_engine_states import HEALTH_CHECK_STATES

logger = logging.getLogger(__name__)

def api_instance_logs(inst_id):
    """Get paginated action logs for an instance."""
    from db.adapters.instances import get_instance as _gi
    from db.adapters.logs import get_instance_logs_paginated
    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    logs = get_instance_logs_paginated(_CONFIG["db_path"], inst_id, limit=limit, offset=offset)
    return jsonify({"status": "ok", "total": logs["total"], "limit": limit,
                    "offset": offset, "items": logs["items"]})


def api_instance_journal(inst_id):
    """Get journalctl logs for a deployed instance's systemd service.

    Queries journalctl on the remote node for the instance's service unit
    (qr-{instance_name}) and returns recent log entries.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        JSON with instance_name, node_name, logs (journalctl output string),
        and error if any.
    """
    from db.adapters.instances import get_instance as _gi
    from lib.lib_ansible_runner import get_instance_logs as _gil
    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    lines = min(int(request.args.get("lines", 100)), 500)
    result = _gil(_CONFIG["db_path"], inst_id, lines=lines)

    if result.get("error"):
        return error_response("JOURNAL_ERROR", result["error"])

    return jsonify({
        "status": "ok",
        "instance_name": result.get("instance_name", ""),
        "node_name": result.get("node_name", ""),
        "lines": lines,
        "logs": result.get("logs", ""),
    })


def api_instance_status(inst_id, remote=False):
    """Unified instance status endpoint (EP-CONSOLIDATE P1).

    Default: returns STATUS-1 engine-specific data with actions and warnings.
    With remote=True: adds health check result + state transition logic from
    the former api_query_status(), enabling crash detection and auto-recovery.

    This replaces three separate endpoints:
      - api_get_instance_status (STATUS-1, line 825) → kept as thin wrapper
      - api_instance_status (lightweight, line 4480) → merged here
      - api_query_status (remote health probe, line 4633) → logic added when remote=True

    Args:
        inst_id: Instance primary key.
        remote: If True, also run engine.query_status() + state transitions.

    Returns:
        JSON with engine-specific status data; when remote=True includes
        alive, latency_ms, model_loading, and new_state fields.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui, transition_state as _ts
    from db.sqlite import pool
    from engine import get_engine as _ge

    try:
        # Phase 1: Get STATUS-1 engine-specific status data
        # Lazy import to avoid circular dependency with status_queries
        from qr_api.routes_instances.status_queries import _engine_get_instance_status as _egis
        status = _egis(_CONFIG["db_path"], inst_id)
        if status is None:
            return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

        # INSTANCE-META: attach preset_name and model_name from LEFT JOIN query
        with pool(_CONFIG["db_path"]) as conn:
            preset_row = conn.execute(
                """SELECT ep.name as preset_name, em.name as model_name
                   FROM instances i
                   LEFT JOIN engine_presets ep ON i.preset_id = ep.id
                   LEFT JOIN engine_models em ON ep.model_id = em.id
                   WHERE i.id = ?""", (inst_id,),
            ).fetchone()
        # Convert sqlite3.Row to dict — .get() not available in this Python version
        if preset_row:
            pr = dict(preset_row)
            if pr.get("preset_name"):
                status["preset_name"] = pr["preset_name"]
            if pr.get("model_name"):
                status["model_name"] = pr["model_name"]

        # Phase 2: If remote=True, run health probe + crash detection
        if remote:
            from engine import get_engine, load_engines
            import json as _json
            from datetime import datetime as _dt, timezone as _tz

            inst = _gi(_CONFIG["db_path"], inst_id)
            if inst is None:
                return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

            engine_type = inst.get("engine_type_name", "")
            engine = _ge(engine_type)
            if engine is None:
                alt_name = engine_type.replace("-", "_")
                engine = _ge(alt_name)
            if engine is None:
                alt_name = engine_type.replace("_", "-")
                engine = _ge(alt_name)

            if engine:
                result = engine.query_status(inst_id, _CONFIG["db_path"])
            else:
                result = {"alive": False, "latency_ms": None, "error": f"Engine '{engine_type}' not loaded"}

            cur_state = inst.get("state", "unknown")
            new_state = None
            _active_jobs = False
            _recently_completed = False

            try:
                from db.sqlite import pool as _jobs_pool
                with _jobs_pool(_CONFIG["db_path"]) as _jconn:
                    _active_jobs = bool(_jconn.execute(
                        "SELECT 1 FROM log_entries WHERE instance_id=? AND parent_id IS NULL AND status IN ('queued','running') LIMIT 1",
                        (inst_id,),
                    ).fetchone())
                    _rc = _jconn.execute(
                        "SELECT 1 FROM log_entries WHERE instance_id=? AND parent_id IS NULL AND status='completed' "
                        "AND datetime(finished_at) > datetime('now', '-60 seconds') LIMIT 1",
                        (inst_id,),
                    )
                    _recently_completed = bool(_rc.fetchone())
            except Exception as _e:
                logger.debug("api_instance_status inst=%d: recently completed health check query: %s", inst_id, _e)
                pass

            # State transition logic from former api_query_status()
            if result.get("model_loading"):
                result["model_loading"] = True
            elif result.get("alive") and not result.get("model_loading") and cur_state == "starting":
                try:
                    _ts(_CONFIG["db_path"], inst_id, "running")
                    new_state = "running"
                    result["new_state"] = "running"
                except Exception as _e:
                    logger.debug("health check state transition failed: inst=%d cur_state=%s → running", inst_id, cur_state, _e)
            elif result.get("alive") and not result.get("model_loading") and cur_state == "loading":
                try:
                    _ts(_CONFIG["db_path"], inst_id, "running")
                    new_state = "running"
                    result["new_state"] = "running"
                except Exception as _e:
                    logger.debug("health check state transition failed: inst=%d cur_state=%s → running", inst_id, cur_state, _e)
            elif result.get("alive") and not result.get("model_loading") and cur_state in ("deployed", "stopped"):
                try:
                    _ts(_CONFIG["db_path"], inst_id, "running")
                    new_state = "running"
                    result["new_state"] = "running"
                except Exception as _e:
                    logger.debug("health check state transition failed: inst=%d cur_state=%s → running", inst_id, cur_state, _e)
            elif result.get("alive") and not result.get("model_loading") and cur_state in ("updating", "build_error"):
                try:
                    _ts(_CONFIG["db_path"], inst_id, "running")
                    new_state = "running"
                    result["new_state"] = "running"
                except Exception as _e:
                    logger.debug("health check state transition failed: inst=%d cur_state=%s → running", inst_id, cur_state, _e)
            elif result.get("alive") and not result.get("model_loading") and cur_state in ("deploying", "configuring"):
                try:
                    _ts(_CONFIG["db_path"], inst_id, "running")
                    new_state = "running"
                    result["new_state"] = "running"
                except Exception as _e:
                    logger.debug("health check state transition failed: inst=%d cur_state=%s → running", inst_id, cur_state, _e)
            elif result.get("alive") and not result.get("model_loading") and cur_state in ("error", "build_error"):
                try:
                    _ts(_CONFIG["db_path"], inst_id, "running")
                    new_state = "running"
                    result["new_state"] = "running"
                except Exception as _e:
                    logger.debug("health check state transition failed: inst=%d cur_state=%s → running", inst_id, cur_state, _e)
            elif not result.get("alive") and cur_state in ("running", "updating", "build_error", "stopping"):
                if not _active_jobs and not _recently_completed:
                    _error_reason = (result.get("error", "") or f"Health check failed: {cur_state} → error")[:500]
                    try:
                        _ts(_CONFIG["db_path"], inst_id, "error")
                        new_state = "error"
                        result["new_state"] = "error"
                        try:
                            from db.sqlite import pool as _pool2
                            with _pool2(_CONFIG["db_path"]) as _crash_conn:
                                _crash_conn.execute(
                                    "INSERT INTO log_entries (parent_id, job_type, engine_type_name, instance_id, status, actor, details_json, created_at, task_stage, stage_playbook, retry_count, max_retries) "
                                    "VALUES (?, ?, 'system', ?, 'failed', ?)",
                                    ("crash_detect", inst_id,
                                     _json.dumps({"state_from": cur_state, "reason": _error_reason}),
                                     _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                                 )
                        except Exception as _e:
                            logger.debug("crash detect log entry insert failed: inst=%d", inst_id, _e)
                    except Exception as _e:
                        logger.debug("crash detect state transition failed: inst=%d cur_state=%s → error", inst_id, cur_state, _e)

            # Update last_state_change timestamp
            try:
                _ui(_CONFIG["db_path"], inst_id, last_state_change=_dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            except Exception as _e:
                logger.debug("last_state_change update failed: inst=%d", inst_id, _e)

            if new_state:
                result["new_state"] = new_state

            # Merge health check into status response
            status["health"] = result
            status["remote_check"] = True

        return success_single(status)

    except Exception as exc:
        return error_response("INTERNAL_ERROR", f"Status query failed: {exc}")


# ── BG-HEALTH-1: Scheduled Periodic Health Checks ───────────────────────

def api_health_check_all():
    """BG-HEALTH-1: Queue health checks for all eligible instances.

    Creates one-shot health_check jobs via the RUNNER-1 infrastructure.
    The scheduler picks up these tasks and executes them asynchronously.
    Returns immediately after queuing.

    Query params:
        state_filter: comma-separated list of states to check
                      (default: all checked states)

    Returns:
        JSON with queued_count and per-instance details.
    """
    from db.sqlite import pool
    from lib.lib_runner import PlaybookRunner
    from datetime import datetime as _dt, timezone as _tz

    # Parse optional state filter
    state_filter = request.args.get("state_filter", "")
    if state_filter:
        checked_states = tuple(s.strip() for s in state_filter.split(","))
    else:
        checked_states = tuple(HEALTH_CHECK_STATES)

    try:
        with pool(_CONFIG["db_path"]) as conn:
            rows = conn.execute(
                """SELECT i.id, i.name, i.state
                   FROM instances i
                   JOIN nodes n ON i.node_id = n.id
                   WHERE i.system_managed = 0
                     AND i.health_check_enabled = 1
                     AND n.is_active = 1
                     AND i.state IN (""" + ",".join("?" for _ in checked_states) + """)""",
                list(checked_states),
            ).fetchall()

        queued_count = 0
        instances = []
        runner = PlaybookRunner(_CONFIG["db_path"], "playbooks/")

        for row in rows:
            result = runner.create_periodic_health_check(row["id"])
            if result:
                queued_count += 1
            instances.append({
                "id": row["id"],
                "name": row["name"],
                "state": row["state"],
                "queued": result is not None,
            })

        return success_single({
            "queued_count": queued_count,
            "instances": instances,
        })

    except Exception as exc:
        return error_response("INTERNAL_ERROR", f"Health check queue failed: {exc}")


def api_system_instance_status(inst_id):
    """Get system instance status (uptime, port, health).

    For system-managed instances, returns real-time data:
    - quickrobot-api: RSS memory and self-uptime
    - quickrobot-webui: subprocess (PID-in-DB) + HTTP health check

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        JSON with engine-specific status data.
    """
    from db.adapters.instances import get_instance as _gi
    import psutil

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    if not inst.get("system_managed"):
        return error_response("NOT_SYSTEM_INSTANCE", "This endpoint is for system-managed instances only")

    engine_type = inst.get("engine_type_name", "")
    config_override = inst.get("config_override", {})
    if isinstance(config_override, str):
        try:
            import json as _j2
            config_override = _j2.loads(config_override)
        except Exception as _e:
            logger.debug("api_system_instance_status inst=%d: config_override JSON parse failed: %s", inst_id, _e)
            config_override = {}

    if engine_type == QR_ENGINE_API_NAME:
        import socket as _sock
        import time as _t2
        import psutil as _ps
        info = {
            "engine_type": "quickrobot-api",
            "alive": True,
            "rss_bytes": _ps.Process().memory_info().rss,
            "uptime_seconds": int(_t2.time() - _START_TIME),
            "port": _CONFIG["api_port"],
            "ip": _CONFIG["host"],
        }
        # Try to detect the actual listening port if config differs
        try:
            import socket as _sock2
            s = _sock2.socket(_sock2.AF_INET, _sock2.SOCK_STREAM)
            result = s.connect_ex((QR_DEFAULT_LOCALHOST, info["port"]))
            if result == 0:
                pass  # Port is open, confirmed
            else:
                # Try common fallback ports: SSOT default (historical), HTTP proxy/llama_server port, Flask dev default
                for fallback_port in [QR_ENGINE_PORT_DEFAULTS["quickrobot-api"], QR_ENGINE_PORT_DEFAULTS["llama_server"], 5000]:
                    if fallback_port != info["port"]:
                        s2 = _sock2.socket(_sock2.AF_INET, _sock2.SOCK_STREAM)
                        r2 = s2.connect_ex((QR_DEFAULT_LOCALHOST, fallback_port))
                        if r2 == 0:
                            info["port"] = fallback_port
                            break
                        s2.close()
        except Exception as _e:
            logger.debug("api_system_instance_status inst=%d: API port check failed: %s", inst_id, _e)
            pass
        return success_single(info)

    elif engine_type == QR_ENGINE_WEBUI_NAME:
        from db.adapters.configs import get_engine_config as _gec
        port_row = _gec(_CONFIG["db_path"], 2, "web_ui_port") or {}
        host_row = _gec(_CONFIG["db_path"], 2, "web_ui_host") or {}
        web_port = config_override.get("web_ui_port") or port_row.get("value", "")
        web_host = config_override.get("web_ui_host") or host_row.get("value", "")
        status = {
            "engine_type": "quickrobot-webui",
            "web_ui_port": web_port,
            "web_ui_host": web_host,
            "alive": False,
        }
        if not web_port:
            raise KeyError("web_ui_port not in config_override or engine_configs for quickrobot-webui")
        port = int(web_port) if web_port else 0
        import urllib.request as _ur
        try:
            resp = _ur.urlopen(f"http://{web_host}:{port}/", timeout=2)
            status["alive"] = True
            status["http_status"] = resp.getcode()
        except Exception as _e:
            logger.debug("api_system_instance_status inst=%d: WebUI HTTP health check failed: %s", inst_id, _e)
            pass
        return success_single(status)

    elif engine_type == QR_ENGINE_MCP_NAME:
        from engine import get_engine as _ge
        mcp_engine = _ge(QR_ENGINE_MCP_NAME)
        if mcp_engine:
            try:
                status_data = mcp_engine.get_status(inst_id, _CONFIG["db_path"])
            except Exception as _e:
                logger.debug("api_system_instance_status inst=%d: MCP engine status check failed: %s", inst_id, _e)
                status_data = {"engine_type": QR_ENGINE_MCP_NAME, "info": {}}
        else:
            status_data = {"engine_type": QR_ENGINE_MCP_NAME, "info": {}}
        return success_single(status_data)

    elif engine_type == QR_ENGINE_SCHEDULER_NAME:
        from engine.quickrobot_scheduler import SchedulerEngine
        sched_engine = SchedulerEngine()
        try:
            status_data = sched_engine.get_status(inst_id, _CONFIG["db_path"])
        except Exception as _exc:
            status_data = {"engine_type": "quickrobot-scheduler", "info": {}}
        return success_single(status_data)

    return success_single({"engine_type": engine_type, "info": {}})

def api_proxy_remote(subpath):
    """Reverse proxy to a remote instance's web UI.

    Forwards requests to the remote instance (identified by node + port).
    CORS headers added by global flask-cors middleware.

    Args:
        subpath: Instance ID followed by path, e.g., '123/health' or '123/'.

    Returns:
        Proxied response (CORS handled by middleware).
    """
    import urllib.request as _urq
    import urllib.error as _ure

    # Parse instance ID and target path from subpath
    parts = subpath.split("/", 1)
    if len(parts) < 2:
        return Response('{"status":"error","code":"BAD_REQUEST","message":"Usage: /api/v1/proxy/<instance_id>/<path>"}',
                            status=400, content_type="application/json; charset=utf-8")

    inst_id = int(parts[0])
    target_path = parts[1] or "/"

    # Verify instance exists and get its node + port
    from db.adapters.instances import get_instance as _gi
    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    node_hostname = inst.get("node_hostname") or inst.get("ipv4_address", QR_DEFAULT_LOCALHOST)
    engine_type = inst.get("engine_type_name", "")
    default_port = QR_ENGINE_PORT_DEFAULTS.get(engine_type, 8080)
    port = inst.get("port_assigned") or default_port

    # Build target URL — avoid double slashes
    if target_path == "":
        base_url = f"http://{node_hostname}:{port}/"
    elif target_path.startswith("/"):
        base_url = f"http://{node_hostname}:{port}{target_path}"
    else:
        base_url = f"http://{node_hostname}:{port}/{target_path}"
    if request.query_string:
        base_url += "?" + request.query_string.decode()

    # Forward the request — set Host to target, forward other headers
    headers = {"Host": f"{node_hostname}:{port}"}
    for key, value in request.headers:
        if key.lower() not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value

    try:
        data = request.get_data() if request.method in ("POST", "PUT") else None
        from lib.lib_proxy_reader import proxy_request as _proxy_req
        body, status_code, resp_headers = _proxy_req(
            base_url, data=data, headers=headers,
            method=request.method, timeout=60)

        # Clean proxied response headers — CORS added by middleware
        clean_headers = []
        for k, v in resp_headers.items():
            kl = k.lower()
            if kl == "content-type":
                clean_headers.append((k, f"{v}; charset=utf-8"))
            elif kl == "content-length":
                continue  # let Flask set it
            else:
                clean_headers.append((k, v))

        return Response(body, status=status_code, headers=dict(clean_headers))

    except _ure.HTTPError as e:
        body = e.read()
        resp_headers = list(e.headers.items()) if hasattr(e, 'headers') else []
        clean_headers = []
        for k, v in resp_headers:
            kl = k.lower()
            if kl == "content-type":
                clean_headers.append((k, f"{v}; charset=utf-8"))
            elif kl == "content-length":
                continue
            else:
                clean_headers.append((k, v))
        return Response(body, status=e.code, headers=dict(clean_headers))

    except Exception as exc:
        # Handle ProxyConnectionError with its descriptive message
        from lib.lib_proxy_reader import ProxyConnectionError as _PCE
        if isinstance(exc, _PCE):
            error_msg = str(exc)
        else:
            error_msg = f"Proxy error: {exc}"
        error_body = f'{{"status":"error","code":"PROXY_ERROR","message":"{error_msg}"}}'.encode()
        return Response(error_body, status=502, content_type="application/json; charset=utf-8")


def api_restart_system_instance(inst_id):
    """Restart a system-managed instance.

    Unified restart handler: delegates to api_restart_instance which auto-detects
    system-managed instances and routes to the correct subprocess path.
    This consolidates the restart logic into one endpoint instead of having
    separate /instances/<id>/restart and /instances/<id>/restart_system endpoints.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        Action result dict with status message.
    """
    return api_restart_instance(inst_id)


def api_regenerate_instance_token(inst_id):
    """Regenerate the auth token for a llama-server / llama_rpc instance.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        dict with status and new auth_token.
    """
    from db.sqlite import pool
    from lib.lib_token import generate_api_key as _gen_key
    from lib.qr_engine_ids import get_name_by_id

    try:
        with pool(_CONFIG["db_path"]) as conn:
            inst = conn.execute(
                "SELECT engine_type_id FROM instances WHERE id = ?", (inst_id,)
            ).fetchone()
            if not inst:
                return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

            engine = get_name_by_id(inst["engine_type_id"]) or ""
            new_token = _gen_key()
            conn.execute(
                "UPDATE instances SET auth_token = ? WHERE id = ?",
                (new_token, inst_id),
            )
        return jsonify({
            "status": "ok",
            "instance_id": inst_id,
            "engine_type": engine,
            "auth_token": new_token,
        })
    except Exception as exc:
        logger.error("api_regenerate_instance_token failed for %d: %s", inst_id, exc)
        return error_response("INTERNAL_ERROR", str(exc))


def api_disable_instance_token(inst_id):
    """Disable the auth token for a llama-server / llama_rpc instance.

    Sets auth_token to NULL in the DB. The instance detail page will show "(disabled)".
    DB-only update — no reconfig job triggered. User must click "Apply & Restart"
    to push changes to the remote server via the env file.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        dict with status and updated auth_token (None).
    """
    from db.sqlite import pool
    from lib.qr_engine_ids import get_name_by_id

    try:
        with pool(_CONFIG["db_path"]) as conn:
            inst = conn.execute(
                "SELECT engine_type_id FROM instances WHERE id = ?", (inst_id,)
            ).fetchone()
            if not inst:
                return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

            engine = get_name_by_id(inst["engine_type_id"]) or ""
            conn.execute(
                "UPDATE instances SET auth_token = NULL WHERE id = ?",
                (inst_id,),
            )
        return jsonify({
            "status": "ok",
            "instance_id": inst_id,
            "engine_type": engine,
            "auth_token": None,
        })
    except Exception as exc:
        logger.error("api_disable_instance_token failed for %d: %s", inst_id, exc)
        return error_response("INTERNAL_ERROR", str(exc))


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
    from db.adapters.instances import get_instance as _gi, transition_state, \
        log_action
    from lib.lib_ansible_runner import run_playbook

    try:
        # Start the client service (one-shot execution)
        start_result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
        if not start_result.get("success"):
            return error_response("START_FAILED",
                                f"Client start failed: {start_result.get('error', 'unknown')}")

        log_action(_CONFIG["db_path"], inst_id, "client_run", "started")

        # Transition to starting state while running
        try:
            transition_state(_CONFIG["db_path"], inst_id, "starting")
        except Exception as _e:
            logger.debug("_run_iperf3_client inst=%d: starting state transition for client: %s", inst_id, _e)
            pass

        # Poll until the service exits (one-shot run)
        import time as _time
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

            except Exception as _e:
                logger.debug("_run_iperf3_client inst=%d: status poll iteration failed, retrying: %s", inst_id, _e)
                continue  # Poll failure, try again

        if not exit_ok:
            # Timeout — stop and log
            _run_manage_action(inst_id, engine_type_name, node_id, "stop")
            return error_response("TIMEOUT",
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
        except Exception as _e:
            logger.debug("_run_iperf3_client inst=%d: log retrieval failed: %s", inst_id, _e)
            client_log = "(unable to retrieve log)"

        # Parse iperf3 log output for throughput results
        parsed = _parse_iperf3_log(client_log)

        # Transition to deployed (client ran once and finished)
        try:
            transition_state(_CONFIG["db_path"], inst_id, "deployed")
        except Exception as _e:
            logger.debug("_run_iperf3_client inst=%d: deployed state transition after client run: %s", inst_id, _e)

        log_action(_CONFIG["db_path"], inst_id, "client_run", "success",
                    detail={"sent_mbits": parsed.get("sent_mbits"),
                            "received_mbits": parsed.get("received_mbits")})

        return success_single({
            "action": "run_client",
            "instance_id": inst_id,
            "success": True,
            "log_file": f"/var/log/qr/iperf3-{inst_id}.log",
            "log_excerpt": client_log[:2000],
            "parsed_results": parsed,
        })

    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "client_run", "failed",
                    detail={"error": str(exc)})
        return error_response("CLIENT_RUN_ERROR", str(exc))


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

