import json

from flask import request, jsonify
import logging as _logging

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from qr_api.routes_instances.status_queries import _health_probe_instance
from qr_api.lib_instances import (
    _restart_system_managed, _stop_system_managed, _start_system_managed, _execute_playbook,
    _resolve_engine_playbook_id, _get_keep_shared_build, deploy_instance,
    _check_node_active, _get_deploy_lock,
)
from lib.lib_qr_actions import log_qr_override
from lib.qr_engine_ids import (
    QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_IPERF3_NAME, QR_ENGINE_UNIVERSAL_NAME, QR_ENGINE_SUBPROCESS_NAME,
    QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC,
    QR_JOB_DEPLOY, QR_JOB_DEPLOY_FAST, QR_JOB_DEPLOY_BINARY, QR_JOB_RECONFIGURE,
    QR_JOB_RESTART, QR_JOB_START, QR_JOB_STOP, QR_JOB_UNDEPLOY, QR_JOB_HEALTH_CHECK,
)
from lib.lib_engine_states import (
    VALID_INSTANCE_STATES, VALID_UNDEPLOY_STATES, IDEMPOTENT_START_STATES,
    AUTO_DEPLOY_STATES, RESTART_FROM_NONRUNNING, SUBPROCESS_CYCLE_STATES,
    UNDEPLOY_TRANSITIONAL_STATES, STOP_ALLOWED_STATES, RECONFIGURE_ALLOWED_STATES,
    QR_STATE_STARTING, QR_STATE_RUNNING, QR_STATE_STOPPING,
    QR_STATE_STOPPED, QR_STATE_ERROR, QR_STATE_UNCONFIGURED, QR_STATE_DEPLOYED,
)

logger = _logging.getLogger(__name__)


def api_start_instance(inst_id):
    """Start an instance: deployed/stopped -> starting -> running (or error).

    System-managed instances are routed to subprocess-based start path.
    """
    from db.adapters.instances import transition_state, log_action, get_instance as _gi, \
        check_system_managed as _csm
    import os as _os

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    node_id = inst.get("node_id")
    engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME)

    # Guard: block operations on inactive hosts
    if node_id and node_id != 1 and engine_type_name != QR_ENGINE_SUBPROCESS_NAME:
        from db.adapters.nodes import get_node as _gn_i
        nd_check = _gn_i(_CONFIG["db_path"], node_id)
        if nd_check and not nd_check.get("is_active", 1):
            return error_response("HOST_INACTIVE", f"Instance {inst_id} is on an inactive host — operations blocked. Activate the host first.")

    # DESIGN-2: Health-first probe for remote systemd services
    health = None
    hostname = None
    if not _csm(_CONFIG["db_path"], inst_id) and engine_type_name != QR_ENGINE_SUBPROCESS_NAME:
        try:
            from db.adapters.nodes import get_node as _gn
            nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
            hostname = (nd.get("ansible_inventory_host") or
                        nd.get("hostname")) if nd else None
            if hostname:
                health = _health_probe_instance(inst_id, hostname, engine_type_name, node_id)
        except Exception as _e:
            logger.debug("api_start_instance inst=%d: health probe failed, proceeding without: %s", inst_id, _e)

    # Route system-managed instances to subprocess-based start path
    if _csm(_CONFIG["db_path"], inst_id):
        return _start_system_managed(inst_id, engine_type_name, log_action)

    # Subprocess engine: always try execute (local process, no playbook state lock)
    if engine_type_name == QR_ENGINE_SUBPROCESS_NAME:
        from engine import get_engine as _ge
        engine = _ge(QR_ENGINE_SUBPROCESS_NAME)
        if engine is None:
            return error_response("DEPLOYMENT_FAILED", "subprocess engine not loaded")
        # Check if already running via engine status
        status = engine.get_status(inst_id, _CONFIG["db_path"])
        if status.get("running"):
            # Already running — ensure state is running
            try:
                transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STARTING)
            except Exception as _e:
                logger.debug("api_start_instance inst=%d: starting state transition (idempotent): %s", inst_id, _e)
            try:
                transition_state(_CONFIG["db_path"], inst_id, QR_STATE_RUNNING)
            except Exception as _e:
                logger.debug("api_start_instance inst=%d: running state transition (idempotent): %s", inst_id, _e)
            return success_single({"action": "start", "instance_id": inst_id,
                                    "state": "running", "idempotent": True})
        result = engine.execute(inst_id, QR_JOB_START, _CONFIG["db_path"])
        if result.get("error"):
            try:
                transition_state(_CONFIG["db_path"], inst_id, QR_STATE_ERROR)
            except Exception as _e:
                logger.debug("api_start_instance inst=%d: error state transition on execute failure: %s", inst_id, _e)
            return error_response("DEPLOYMENT_FAILED", result["error"])
        # execute() already transitions state (starting → running)
        # Just confirm the PID is alive for safety
        if result.get("pid"):
            try:
                import psutil as _psutil
                p = _psutil.Process(result["pid"])
                if p.status() != "zombie":
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STARTING)
                    except Exception as _e:
                        logger.debug("api_start_instance inst=%d: starting state transition (pid check): %s", inst_id, _e)
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, QR_STATE_RUNNING)
                    except Exception as _e:
                        logger.debug("api_start_instance inst=%d: running state transition (pid check): %s", inst_id, _e)
                else:
                    # Zombie process — clear PID and go back to deployed
                    _ui = __import__("db.adapters.instances", fromlist=["update_instance"]).update_instance
                    _ui(_CONFIG["db_path"], inst_id, pid_last_known=None)
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass
        log_action(_CONFIG["db_path"], inst_id, QR_JOB_START, "success", detail={"subprocess": result})
        return success_single({"action": "start", "instance_id": inst_id,
                                "state": "running", "pid": result.get("pid")})

   # RPC binding warnings for llama_server instances (engine_type_id=21)
    if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME and inst.get("rpc_bind_ids"):
        try:
            from lib.lib_cluster_env_builder import rpc_binding_warnings as _rbw
            rp_warnings = _rbw(_CONFIG["db_path"], inst_id)
        except Exception as _e:
            logger.debug("api_start_instance inst=%d: RPC binding warnings check failed: %s", inst_id, _e)
            rp_warnings = []
    else:
        rp_warnings = []

    allowed = VALID_INSTANCE_STATES

    # DESIGN-2: Health-first idempotency — probe remote, then decide
    if health is not None:
        if health["error"]:
            # Probe failed — transition to error state per DESIGN-2
            try:
                 transition_state(_CONFIG["db_path"], inst_id, QR_STATE_ERROR,
                                   state_reason=f"Health probe failed: {health['error']}")
            except Exception as _e:
                logger.debug("api_start_instance inst=%d: error state transition (health probe fail): %s", inst_id, _e)
                pass
            return error_response("HEALTH_CHECK_FAILED",
                                  f"Remote health probe failed: {health['error']}", 503)
        if health["service_state"] in ("running", "active"):
            # Service confirmed running on remote — idempotent success
            try:
                transition_state(_CONFIG["db_path"], inst_id, QR_STATE_RUNNING)
            except Exception as _e:
                logger.debug("api_start_instance inst=%d: running state transition (idempotent): %s", inst_id, _e)
                pass
            resp = {"action": "start", "instance_id": inst_id,
                    "state": QR_STATE_RUNNING, "idempotent": True,
                    "note": "Already running on remote node"}
            if rp_warnings:
                resp["warnings"] = rp_warnings
            return success_single(resp)
    elif inst["state"] in IDEMPOTENT_START_STATES:
        # No probe available — fall back to DB-state idempotency
        resp = {"action": "start", "instance_id": inst_id,
                "state": inst["state"], "idempotent": True}
        if rp_warnings:
            resp["warnings"] = rp_warnings
        return success_single(resp)

    if inst["state"] not in allowed:
        return error_response("INVALID_STATE",
                                 f"Cannot start instance in '{inst['state']}' state (allowed: {allowed})")

    # llama_server with RPC bindings → RUNNER-1 job with health checks
    if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME and inst.get("rpc_bind_ids"):
        try:
            from lib.lib_runner import PlaybookRunner
            runner = PlaybookRunner(_CONFIG["db_path"])
            job, tasks = runner.create_deploy_job(inst_id, job_type=QR_JOB_START, actor="api")
            return success_single({
                "action": "start",
                "instance_id": inst_id,
                "job_id": job["id"],
                "tasks_created": len(tasks),
            })
        except Exception as exc:
            from db.adapters.instances import update_log_status, log_action as _la
            _tmp_id = _la(_CONFIG["db_path"], inst_id, QR_JOB_START, "failed")
            update_log_status(_CONFIG["db_path"], _tmp_id, "failed", detail={"error": str(exc)})
            return error_response("DEPLOYMENT_FAILED", f"Start job creation failed: {exc}")

    # Auto-deploy if unconfigured or deploying (stuck)
    if inst["state"] in AUTO_DEPLOY_STATES:
        deploy_result = deploy_instance(_CONFIG["db_path"], inst_id)
        if not deploy_result.get("success"):
            return error_response("DEPLOYMENT_FAILED",
                                  f"Auto-deploy failed: {deploy_result.get('message', 'unknown')}")

    # Universal engine: require start_command or binary_path
    if engine_type_name == QR_ENGINE_UNIVERSAL_NAME:
        co = inst.get("config_override") or {}
        if isinstance(co, str):
            try:
                co_merged = json.loads(co) or {}
            except Exception as _e:
                logger.debug("api_start_instance inst=%d: config_override JSON parse failed: %s", inst_id, _e)
                co_merged = {}
        elif isinstance(co, dict):
            co_merged = co
        else:
            co_merged = {}
        has_start_cmd = bool(co_merged.get("start_command", ""))
        has_binary = bool(co_merged.get("binary_path", ""))
        if not has_start_cmd and not has_binary:
            return error_response("START_CONFIG_MISSING",
                                  "No start_command or binary_path defined for this universal instance")

    # RUNNER-1: Start via staged chain (service_start playbook)
    from lib.lib_runner import PlaybookRunner as _PR
    runner = _PR(_CONFIG["db_path"])
    result = runner.chain(inst_id, job_type=QR_JOB_START, actor="api", async_mode=True)
    if not result.get("job_id"):
        from db.adapters.instances import update_log_status, log_action as _la
        _tmp_id = _la(_CONFIG["db_path"], inst_id, QR_JOB_START, "failed")
        update_log_status(_CONFIG["db_path"], _tmp_id, "failed",
                          detail={"chain": result})
        return error_response("DEPLOYMENT_FAILED",
                              f"Start job creation failed: {result.get('message', 'unknown')}")

    from db.adapters.instances import update_log_status
    update_log_status(_CONFIG["db_path"], result["job_id"], "success", detail={"job_id": result["job_id"]})

    resp = {"action": "start", "instance_id": inst_id,
                "job_id": result["job_id"],
                "state": QR_STATE_STARTING}
    if rp_warnings:
        resp["warnings"] = rp_warnings
    return success_single(resp)


def api_stop_instance(inst_id):
    """Stop an instance via Ansible playbook or subprocess for system-managed."""
    from db.adapters.instances import transition_state, log_action, get_instance as _gi, \
        check_system_managed as _csm

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    node_id = inst.get("node_id")
    engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME)

    # Route system-managed instances to subprocess-based stop path
    if _csm(_CONFIG["db_path"], inst_id):
        return _stop_system_managed(inst_id, engine_type_name, log_action)

    # Subprocess engine: use local subprocess management
    if engine_type_name == QR_ENGINE_SUBPROCESS_NAME:
        from engine import get_engine as _ge
        engine = _ge(QR_ENGINE_SUBPROCESS_NAME)
        if engine is None:
            return error_response("DEPLOYMENT_FAILED", "subprocess engine not loaded")
        try:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPING)
        except Exception as _e:
            logger.debug("api_stop_instance inst=%d: subprocess stopping state transition: %s", inst_id, _e)
            pass
        result = engine.execute(inst_id, QR_JOB_STOP, _CONFIG["db_path"])
        # Always transition to stopped regardless of execute() result
        # (process may already be dead with stale PID)
        try:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPED)
        except Exception as _e:
            logger.debug("api_stop_instance inst=%d: subprocess stopped state transition: %s", inst_id, _e)
        if result.get("error"):
            return success_single({"action": "stop", "instance_id": inst_id, "state": "stopped", "note": result["error"]})
        log_action(_CONFIG["db_path"], inst_id, QR_JOB_STOP, "success", detail={"subprocess": result})
        return success_single({"action": "stop", "instance_id": inst_id, "state": "stopped"})

    if inst["state"] not in STOP_ALLOWED_STATES:
        return error_response("INVALID_STATE",
                                f"Cannot stop instance in '{inst['state']}' state")

    # RUNNER-1: Stop via staged chain (service_stop playbook)
    # State transitions are handled by _run_stage via STAGE_STATE_MAP.
    # If chain() fails (e.g., job_type CHECK violation), instance stays in original state.
    from lib.lib_runner import PlaybookRunner as _PR
    runner = _PR(_CONFIG["db_path"])
    result = runner.chain(inst_id, job_type=QR_JOB_STOP, actor="api")
    if result.get("success"):
        # _finalize_job sets state via raw SQL for stop jobs.
        # Ensure final state is "stopped" (not "deployed").
        try:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPED)
        except Exception as _e:
            logger.debug("api_stop_instance inst=%d: final stopped state transition (may be redundant): %s", inst_id, _e)
        from db.adapters.instances import update_log_status
        update_log_status(_CONFIG["db_path"], result["job_id"], "success",
                          detail={"chain": result})
    else:
        # Chain failed — instance stays in whatever state _run_stage left it.
        # Log failure for visibility; user can retry or investigate.
        from db.adapters.instances import update_log_status, log_action as _la
        _tmp_id = _la(_CONFIG["db_path"], inst_id, QR_JOB_STOP, "failed")
        update_log_status(_CONFIG["db_path"], _tmp_id, "failed",
                          detail={"chain": result})

    return success_single({"action": "stop", "instance_id": inst_id, "state": "stopped"})


def api_restart_instance(inst_id):
    """Restart an instance: running -> stopping -> stopped -> starting -> running (or error).

    For system-managed instances (quickrobot-api, quickrobot-webui, quickrobot-mcp), uses
    the subprocess-based restart path via engine.execute() instead of Ansible playbooks.
    """
    from db.adapters.instances import transition_state, log_action, get_instance as _gi, \
        check_system_managed as _csm

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    node_id = inst.get("node_id")
    engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME)

    # Route system-managed instances to subprocess-based restart path
    if _csm(_CONFIG["db_path"], inst_id):
        return _restart_system_managed(inst_id, engine_type_name, log_action)

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], inst.get("node_id"))
    if isinstance(nd, tuple):
        return nd

    # Subprocess engine: uses its own log tracking (no chain())
    is_subprocess_restart = (engine_type_name == QR_ENGINE_SUBPROCESS_NAME)

    # Log override if restarting from non-running state (deployed/stopped)
    if inst.get("state") in RESTART_FROM_NONRUNNING:
        log_qr_override(_CONFIG["db_path"], "restart_from_deployed",
                            node_id=inst.get("node_id"), instance_id=inst_id,
                            actor="api",
                            details={"from_state": inst["state"]})

    # Subprocess engine: skip ansible playbooks entirely — runs locally via Popen, not systemd
    if is_subprocess_restart:
        from db.adapters.instances import log_action as _ra
        log_id = _ra(_CONFIG["db_path"], inst_id, QR_JOB_RESTART, "received")
        from engine import get_engine as _ge
        engine = _ge(QR_ENGINE_SUBPROCESS_NAME)
        if engine is None:
            return error_response("DEPLOYMENT_FAILED", "subprocess engine not loaded")
        # For running/stopping states, do a proper stop→start cycle
        if inst["state"] in SUBPROCESS_CYCLE_STATES:
            try:
                transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPING)
            except Exception as exc:
                from db.adapters.instances import update_log_status
                update_log_status(_CONFIG["db_path"], log_id, "failed", detail={"phase": "stopping", "error": str(exc)})
                return error_response("DEPLOYMENT_FAILED", str(exc))
            # Stop the process
            stop_result = engine.execute(inst_id, QR_JOB_STOP, _CONFIG["db_path"])
            try:
                transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPED)
            except Exception as _e:
                logger.debug("api_restart_instance inst=%d: stopped state transition after subprocess stop: %s", inst_id, _e)
        # Then start (handles stopped/deployed/error states directly)
        try:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STARTING)
        except Exception as exc:
            from db.adapters.instances import update_log_status
            update_log_status(_CONFIG["db_path"], log_id, "failed", detail={"phase": "starting", "error": str(exc)})
            return error_response("DEPLOYMENT_FAILED", str(exc))
        start_result = engine.execute(inst_id, QR_JOB_START, _CONFIG["db_path"])
        if start_result.get("error"):
            try:
                transition_state(_CONFIG["db_path"], inst_id, QR_STATE_ERROR)
            except Exception as _e:
                logger.debug("api_restart_instance inst=%d: error state transition on start failure: %s", inst_id, _e)
            from db.adapters.instances import update_log_status
            update_log_status(_CONFIG["db_path"], log_id, "failed", detail={"phase": "start", "error": start_result["error"]})
            return error_response("DEPLOYMENT_FAILED", start_result["error"])
        # If process is alive, transition to running
        if start_result.get("pid"):
            try:
                import psutil as _psutil
                p = _psutil.Process(start_result["pid"])
                if p.status() != "zombie":
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STARTING)
                    except Exception as _e:
                        logger.debug("api_restart_instance inst=%d: starting state transition (pid check): %s", inst_id, _e)
                        pass
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, QR_STATE_RUNNING)
                    except Exception as _e:
                        logger.debug("api_restart_instance inst=%d: running state transition (pid check): %s", inst_id, _e)
                        pass
                else:
                    # Zombie process — clear PID
                    _ui = __import__("db.adapters.instances", fromlist=["update_instance"]).update_instance
                    _ui(_CONFIG["db_path"], inst_id, pid_last_known=None)
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass
        from db.adapters.instances import update_log_status
        update_log_status(_CONFIG["db_path"], log_id, "success", detail={"subprocess": start_result})
        return success_single({"action": "restart", "instance_id": inst_id, "state": "running"})

    # RUNNER-1: Restart via staged chain (service_stop → service_start + RPC health_probe)
    from lib.lib_runner import PlaybookRunner as _PR
    runner = _PR(_CONFIG["db_path"])
    result = runner.chain(inst_id, job_type=QR_JOB_RESTART, actor="api")
    if result.get("success"):
        try:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_RUNNING)
        except Exception as _e:
            logger.debug("api_restart_instance inst=%d: running state transition post-chain: %s", inst_id, _e)
        from db.adapters.instances import update_log_status
        update_log_status(_CONFIG["db_path"], result["job_id"], "success", detail={"chain": result})
    else:
        from db.adapters.instances import update_log_status, log_action as _la
        _tmp_id = _la(_CONFIG["db_path"], inst_id, QR_JOB_RESTART, "failed")
        update_log_status(_CONFIG["db_path"], _tmp_id, "failed",
                          detail={"chain": result})
        try:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_ERROR)
        except Exception as _e:
            logger.debug("api_restart_instance inst=%d: error state transition (chain failure): %s", inst_id, _e)
            pass
        return error_response("DEPLOYMENT_FAILED",
                                f"Restart failed: {result.get('message', 'unknown')}")

    return success_single({"action": "restart", "instance_id": inst_id, "state": "running"})


def api_deploy_instance(inst_id):
    """Deploy/redeploy an instance to its target node via staged playbook chain.

    Uses PlaybookRunner.chain() for staged execution with per-stage progress.
    Returns structured result matching current format for WebUI compatibility.
    """
    from db.adapters.instances import get_instance as _gi, check_system_managed as _csm_deploy
    try:
        inst = _gi(_CONFIG["db_path"], inst_id)
        if inst is None:
            return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

        # System-managed instances don't support deploy (use engine config page instead)
        if _csm_deploy(_CONFIG["db_path"], inst_id):
            return error_response("SYSTEM_MANAGED_INSTANCE",
                                    f"Instance {inst_id} is system-managed. "
                                    "Restart via the engine settings page.", 409)

        # Check node is active (admin toggle)
        nd = _check_node_active(_CONFIG["db_path"], inst.get("node_id"))
        if isinstance(nd, tuple):
            return nd

        # Guard: prevent multiple overlapping jobs for same instance
        from db.sqlite import pool as _pool
        with _pool(_CONFIG["db_path"]) as conn:
            existing = conn.execute(
                "SELECT id, job_type, status FROM log_entries WHERE instance_id=? AND parent_id IS NULL AND status IN ('queued','running') AND job_type IN ('deploy','reconfigure','deploy_fast') LIMIT 1",
                (inst_id,)
            ).fetchone()
        if existing:
            return error_response("DEPLOY_IN_PROGRESS",
                                    "Instance %d already has an active %s job (id=%d, status=%s)"
                                    % (inst_id, existing[1], existing[0], existing[2]), 409)

        # RPC binding warnings for llama_server instances before deploy
        rp_warnings = []
        if inst and inst.get("engine_type_name") == QR_ENGINE_LLAMA_SERVER_NAME and inst.get("rpc_bind_ids"):
            try:
                from lib.lib_cluster_env_builder import rpc_binding_warnings as _rbw
                rp_warnings = _rbw(_CONFIG["db_path"], inst_id)
            except Exception as _e:
                logger.debug("api_deploy_instance inst=%d: RPC binding warnings check failed: %s", inst_id, _e)
                pass

        # Read skip_build from request body (Herd page sends this)
        # skip_build=True → deploy_fast chain (config_svc + config_env + start, no source/compile)
        _deploy_skip = None
        _body = request.get_json(force=True, silent=True)
        if _body:
            _sb = _body.get("skip_build")
            if isinstance(_sb, bool):
                _deploy_skip = _sb
            elif isinstance(_sb, str):
                _deploy_skip = _sb.lower() in ("true", "1")
            elif isinstance(_sb, (int, float)):
                _deploy_skip = bool(_sb)

        # BINARY-DL: Extract binary_template_id from request body (orthogonal to preset).
        # Chain selection priority (§3.2A of binary-download-template.md):
        #   1. Explicit binary_template_id → QR_JOB_DEPLOY_BINARY
        #   2. Preset config_template.binary_id (backward compat) → QR_JOB_DEPLOY_BINARY
        #   3. None → QR_JOB_DEPLOY (git_build default)
        _binary_template_id = None
        if _body:
            _btid = _body.get("binary_template_id")
            if _btid is not None:
                try:
                    _binary_template_id = int(_btid)
                except (ValueError, TypeError):
                    pass

        # Template persistence (Issue C+D): on re-deploy with binary_template_id,
        # always store the ID (for rebuild chain detection), merge metadata if present.
        if _binary_template_id is not None:
            try:
                from db.sqlite import pool as _pool_db
                with _pool_db(_CONFIG["db_path"]) as _cdb:
                    _bin_row = _cdb.execute(
                        "SELECT metadata FROM engine_binaries WHERE id=? AND is_active=1",
                        (_binary_template_id,),
                    ).fetchone()
                # Persist binary_template_id always (for rebuild detection)
                _co_merged = inst.get("config_override") or {}
                if isinstance(_co_merged, str):
                    try:
                        _co_merged = json.loads(_co_merged)
                    except (json.JSONDecodeError, TypeError):
                        _co_merged = {}
                _co_merged["binary_template_id"] = _binary_template_id
                if _bin_row and _bin_row.get("metadata"):
                    try:
                        _new_meta = json.loads(_bin_row["metadata"])
                        # Merge fresh template metadata into existing config_override
                        if isinstance(_new_meta, dict):
                            for _k, _v in _new_meta.items():
                                _co_merged[_k] = _v
                    except (json.JSONDecodeError, TypeError):
                        pass
                # Persist updated config_override to DB
                from db.adapters.instances import update_instance as _upd_inst
                _upd_inst(_CONFIG["db_path"], inst_id, config_override=_co_merged)
            except Exception as _e:
                logger.debug("api_deploy_instance(%d): template refresh failed: %s", inst_id, _e)

        # Check for binary_id in preset config_template (backward compat fallback)
        _has_binary_preset = False
        if inst and inst.get("preset_id") and _binary_template_id is None:
            try:
                from db.sqlite import pool as _pool2
                with _pool2(_CONFIG["db_path"]) as _c2:
                    _pr2 = _c2.execute(
                        "SELECT config_template FROM engine_presets WHERE id=?",
                        (inst["preset_id"],),
                    ).fetchone()
                    if _pr2 and _pr2["config_template"]:
                        _ct2 = json.loads(_pr2["config_template"])
                        _has_binary_preset = bool(_ct2.get("binary_id"))
            except Exception:
                pass

        # Route to correct job type based on skip_build / binary_template_id / preset
        _job_type = QR_JOB_DEPLOY_FAST if _deploy_skip else QR_JOB_DEPLOY
        if _binary_template_id is not None:
            _job_type = QR_JOB_DEPLOY_BINARY
        elif not _deploy_skip and _has_binary_preset and _job_type == QR_JOB_DEPLOY:
            # Auto-route to binary download when preset has binary_id (backward compat)
            _job_type = QR_JOB_DEPLOY_BINARY

        # DEBUG: trace deploy call
        logger.debug("api_deploy_instance(%d) _job_type=%s _binary_template_id=%s",
                     inst_id, _job_type, _binary_template_id)
        # Execute staged chain via PlaybookRunner (async — returns immediately)
        from lib.lib_runner import PlaybookRunner
        runner = PlaybookRunner(_CONFIG["db_path"])
        result = runner.chain(inst_id, job_type=_job_type,
                              actor="api", skip_build=_deploy_skip, async_mode=True,
                              binary_template_id=_binary_template_id)

        # Map chain() result to api_deploy_instance response format
        response = {"action": "deploy", "instance_id": inst_id,
                    "success": result.get("success"),
                    "message": result.get("message", "")}
        if result.get("job_id"):
            response["job_id"] = result["job_id"]
        if result.get("tasks_created"):
            response["tasks_created"] = result["tasks_created"]
        if result.get("uuid_mismatches"):
            response["uuid_mismatches"] = result["uuid_mismatches"]
        if rp_warnings:
            response["warnings"] = rp_warnings
        return success_single(response)
    except Exception as exc:
        import traceback; traceback.print_exc()
        return error_response("DEPLOYMENT_FAILED", str(exc))


def api_reconfigure_instance(inst_id):
    """Reconfigure an instance: update env file via RUNNER-1 staged chain, then restart.

    Uses RUNNER-1 chain with QR_JOB_RECONFIGURE (config_env + start stages).
    No git clone/pull, no cmake build. Works for running and stopped instances.
    Transitions: running/stopped → deploying → [running|error].

    Args:
        inst_id: Instance primary key.

    Returns:
        JSON response with action status and instance details.
    """
    from db.adapters.instances import get_instance, check_system_managed as _csm_reconf
    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # System-managed instances use engine config page
    if _csm_reconf(_CONFIG["db_path"], inst_id):
        return error_response("SYSTEM_MANAGED_INSTANCE",
                f"Instance {inst_id} is system-managed. Use the engine config page.", 409)

    engine_type_name = inst.get("engine_type_name", "")
    node_id = inst.get("node_id")
    current_state = inst.get("state", "")

    if engine_type_name not in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME, QR_ENGINE_IPERF3_NAME, QR_ENGINE_SUBPROCESS_NAME):
        return error_response("UNSUPPORTED_ENGINE",
                f"Reconfigure only supported for llama_server/rpc/iperf3/subprocess (got {engine_type_name})")

    if current_state not in RECONFIGURE_ALLOWED_STATES:
        return error_response("INVALID_STATE",
                f"Cannot reconfigure instance in '{current_state}' state (only running/stopped/error/deployed)")

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    # Check deploy lock
    lock = _get_deploy_lock(inst_id)
    if not lock.acquire(blocking=False):
        return error_response("BUSY", f"Config update already in progress for instance {inst_id}")

    try:
        from db.adapters.instances import transition_state as _ts, log_action as _log
        from lib.lib_runner import PlaybookRunner

        # Subprocess: restart via engine.execute() instead of RUNNER-1 chain
        if engine_type_name == QR_ENGINE_SUBPROCESS_NAME:
            from engine import get_engine as _ge_sub
            _ts(_CONFIG["db_path"], inst_id, "updating")
            _log(_CONFIG["db_path"], inst_id, "config_change", "received")
            engine = _ge_sub(QR_ENGINE_SUBPROCESS_NAME)
            if engine is None:
                _ts(_CONFIG["db_path"], inst_id, "error")
                return error_response("RECONFIGURE_FAILED", "Subprocess engine not loaded")
            result = engine.execute(inst_id, QR_JOB_RESTART, _CONFIG["db_path"])
            if isinstance(result, dict) and result.get("error"):
                _ts(_CONFIG["db_path"], inst_id, "build_error")
                return error_response("RECONFIGURE_FAILED", f"Reconfigure failed: {result['error']}")
            _ts(_CONFIG["db_path"], inst_id, "running")
            _log(_CONFIG["db_path"], inst_id, "config_change", "success")
            return success_single({"action": "reconfigure", "instance_id": inst_id,
                                    "state": "running", "message": "Subprocess restarted"})

        # Use RUNNER-1 staged chain (QR_JOB_RECONFIGURE = config_env + start)
        runner = PlaybookRunner(_CONFIG["db_path"])
        result = runner.chain(inst_id, job_type=QR_JOB_RECONFIGURE, actor="api")

        if not result.get("success"):
            _ts(_CONFIG["db_path"], inst_id, "error")
            return error_response("RECONFIGURE_FAILED", result.get("message", "Reconfigure failed"))

        # Success — ensure running state
        try:
            _ts(_CONFIG["db_path"], inst_id, "running")
        except Exception as _e:
            logger.debug("api_reconfigure_instance inst=%d: running state transition after reconfigure: %s", inst_id, _e)
            pass

        return success_single({"action": "reconfigure", "instance_id": inst_id,
                                "state": "running", "message": result.get("message", "Reconfigured and service restarted"),
                                "job_id": result.get("job_id"),
                                "task_ids": result.get("task_ids", [])})

    except Exception as exc:
        try:
            _ts(_CONFIG["db_path"], inst_id, "error")
            _log(_CONFIG["db_path"], inst_id, "config_change", "exception", detail={"error": str(exc)})
        except Exception as _e:
            logger.debug("api_reconfigure_instance inst=%d: error state transition in exception handler: %s", inst_id, _e)
            pass
        return error_response("RECONFIGURE_ERROR", str(exc))
    finally:
        lock.release()


def api_undeploy_instance(inst_id):
    """Remove deployed files from remote node, transition to unconfigured.

    System-managed instances (quickrobot-api, quickrobot-webui) cannot be
    undeployed — they run locally and have no remote artifacts.
    Uses RUNNER-1 staged chain for standard engines; universal engine
    uses direct playbook execution (custom extra_vars).
    """
    import os as _os
    from db.adapters.instances import transition_state, log_action, get_instance, \
        check_system_managed as _csm
    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # System-managed instances cannot be undeployed
    if _csm(_CONFIG["db_path"], inst_id):
        return error_response("INVALID_STATE",
                                f"Cannot undeploy system-managed instance '{inst.get('name', inst_id)}'")

    if inst["state"] not in VALID_UNDEPLOY_STATES:
        return error_response("INVALID_STATE",
                                f"Cannot undeploy instance in '{inst['state']}' state (allowed: {VALID_UNDEPLOY_STATES})")

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], inst.get("node_id"))
    if isinstance(nd, tuple):
        return nd

    # Run remote undeploy chain if instance is deployed and has a node
    engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME)
    node_id = inst.get("node_id")
    remote_undeploy_ok = None

    if node_id is not None and inst["state"] != "unconfigured":
        # Universal engine: custom extra_vars, use direct playbook execution
        if engine_type_name == QR_ENGINE_UNIVERSAL_NAME:
            co = inst.get("config_override", {}) or {}
            co_merged = co if isinstance(co, dict) else {}

            try:
                from db.adapters.nodes import get_node as _gn
                nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
                hostname = (nd.get("ansible_inventory_host") or
                            nd.get("hostname") or
                            nd.get("name")) if nd else None

                instance_name = inst.get("name", f"universal-{inst_id}")
                install_dir = co_merged.get("install_dir") or _os.path.join("/opt/quickrobot", instance_name)
                extra_vars = {
                    "inventory_host": hostname,
                    "node_id": node_id,
                    "instance_id": inst_id,
                    "instance_name": instance_name,
                    "install_dir": install_dir,
                    "clean_source_dir": bool(co_merged.get("clean_source_dir", False)),
                    "clean_venv": bool(co_merged.get("clean_venv", False)),
                }

                pb_id = _resolve_engine_playbook_id(QR_JOB_UNDEPLOY, QR_ENGINE_UNIVERSAL_NAME)
                if not pb_id:
                    log_action(_CONFIG["db_path"], inst_id, QR_JOB_UNDEPLOY, "partial",
                                detail={"message": "Undeploy playbook not found in registry for universal engine"})
                    remote_undeploy_ok = True  # considered ok — best effort
                elif hostname:
                    r = _execute_playbook(pb_id, resolver_type="playbook_id",
                                          limit=hostname,
                                          extra_vars=extra_vars,
                                          action_type="undeploy_instance")
                    if r["error"]:
                        undeploy_result = {"failed": True, "error": r["error"]}
                    else:
                        undeploy_result = r.get("result") or {}
                    remote_undeploy_ok = not undeploy_result.get("failed", False)
                    if not remote_undeploy_ok:
                        log_action(_CONFIG["db_path"], inst_id, QR_JOB_UNDEPLOY, "partial",
                                    detail={"error": str(undeploy_result.get("error", "unknown"))})
                else:
                    log_action(_CONFIG["db_path"], inst_id, QR_JOB_UNDEPLOY, "partial",
                                detail={"message": "No hostname for node"})
                    remote_undeploy_ok = True
            except Exception as exc:
                log_action(_CONFIG["db_path"], inst_id, QR_JOB_UNDEPLOY, "partial",
                            detail={"error": str(exc)})
        # Standard engines (llama_server, llama_rpc, iperf3): use RUNNER-1 chain
        else:
            from lib.lib_runner import PlaybookRunner
            runner = PlaybookRunner(_CONFIG["db_path"])
            # async_mode=True — SSH-heavy undeploy can exceed 30s HTTP timeout.
            # Creates job+tasks, transitions state, returns immediately.
            # Scheduler picks up tasks; client polls via GET /instances/<id> or /jobs.
            chain_result = runner.chain(inst_id, job_type="undeploy", actor="api",
                                        async_mode=True)
            remote_undeploy_ok = None  # undetermined — await chain completion

    try:
        # Transition path depends on current state
        if inst["state"] == QR_STATE_RUNNING:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPING)
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPED)
        elif inst["state"] in UNDEPLOY_TRANSITIONAL_STATES:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPING)
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STOPPED)
        # error and stopped states: direct to unconfigured
        updated = transition_state(_CONFIG["db_path"], inst_id, QR_STATE_UNCONFIGURED)
    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, QR_JOB_UNDEPLOY, "failed", detail={"error": str(exc)})
        return error_response("DEPLOYMENT_FAILED", str(exc))

    log_action(_CONFIG["db_path"], inst_id, QR_JOB_UNDEPLOY, "success" if remote_undeploy_ok else "partial",
                detail={"remote_undeploy": remote_undeploy_ok})

    # Check if shared build should be cleaned up (last llama_server/llama_rpc on node)
    cleanup_done = None
    if engine_type_name in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME) and _get_keep_shared_build():
        try:
            from db.adapters.instances import list_instances as _list_all
            remaining = [i for i in _list_all(_CONFIG["db_path"], node_id=node_id)
                            if i.get("engine_type_name") in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME)]
            # Note: instance just transitioned to unconfigured, so it won't be in remaining anymore
            if len(remaining) == 0:
                # Last instance on this node — trigger shared build cleanup
                from db.adapters.nodes import get_node as _gn
                nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
                hostname = (nd.get("ansible_inventory_host") or
                            nd.get("hostname") or
                            nd.get("name")) if nd else None
                if hostname:
                    r = _execute_playbook("CLEAN_SHARED_LLAMACPP_BUILD_V1", resolver_type="playbook_id",
                                             limit=hostname,
                                             extra_vars={
                                                 "inventory_host": hostname,
                                                 "engine_type": engine_type_name,
                                                 "node_id": node_id,
                                             },
                                             action_type="undeploy_instance")
                    cleanup_done = not r.get("failed", False) if r.get("result") else False
                    log_action(_CONFIG["db_path"], inst_id, "shared_cleanup",
                            "success" if cleanup_done else "failed",
                            detail={"node_id": node_id, "hostname": hostname})
        except Exception as exc:
                cleanup_done = False
                log_action(_CONFIG["db_path"], inst_id, "shared_cleanup",
                        "failed", detail={"error": str(exc)})

    # Build async response with job_id for progress tracking
    response = {"action": "undeploy", "instance_id": inst_id,
                "success": remote_undeploy_ok if remote_undeploy_ok is not None else True,
                "message": "Undeploy queued — scheduler will execute chain"}
    if isinstance(chain_result, dict) and chain_result.get("job_id"):
        response["job_id"] = chain_result["job_id"]
        if chain_result.get("tasks_created"):
            response["tasks_created"] = chain_result["tasks_created"]
    return success_single(response)


def api_execute_instance(inst_id):
    """Execute a command on an instance via the engine.

    For universal engine: supports both sync (instant feedback) and async modes
    based on config_override.instant_feedback setting. Sync mode waits for
    completion up to feedback_timeout seconds and returns full output.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        JSON with execution result including success, exit_code, stdout, stderr,
        duration_ms, and mode (sync/async).
    """
    from db.adapters.instances import get_instance as _gi
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME)
    
    if engine_type_name == QR_ENGINE_UNIVERSAL_NAME:
        # Use universal engine's execute method
        cmd = body.get("command", "")
        timeout = body.get("timeout", 30)
        
        eng = get_engine(QR_ENGINE_UNIVERSAL_NAME)
        if eng is None:
            return error_response("ENGINE_NOT_FOUND", "Universal engine not loaded")
        
        # Pass node_id and config_override to avoid internal DB query
        co_raw = inst.get("config_override", {})
        co_dict = {} if not isinstance(co_raw, dict) else co_raw
        result = eng.execute(inst_id, cmd, db_path=_CONFIG["db_path"],
                                node_id=inst.get("node_id"),
                                config_override=co_dict, timeout=timeout)
        
        if result.get("error"):
            return error_response("EXECUTION_FAILED", result["error"])
        
        return success_single({
            "action": "execute",
            "instance_id": inst_id,
            "engine": "universal",
            **{k: v for k, v in result.items() if k not in ("engine", "instance_id")},
        })
    
    # Fallback: use generic execute via manage_instance.yml
    cmd = body.get("command", "")
    log_action(_CONFIG["db_path"], inst_id, "execute", "received",
                detail={"command": cmd})
    
    return success_single({
        "action": "execute",
        "instance_id": inst_id,
        "engine": engine_type_name,
        "mode": "async",
        "success": True,
        "message": "Execute submitted (async)",
    })


def api_run_client(inst_id):
    """Run an iperf3 client instance to completion and return results.

    For client-mode instances: deploys (installs iperf3 if needed), starts
    the client service, polls until the process exits (one-shot run), then
    fetches the log output as the benchmark result.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        JSON with action, instance_id, success flag, log content, parsed
        throughput results (sent/received mbits), and error if any.
    """
    from db.adapters.instances import get_instance as _gi, \
        transition_state, log_action, merge_configs as _mc
    from lib.lib_ansible_runner import run_playbook
    import os as _os

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    engine_type_name = inst.get("engine_type_name", "")
    if engine_type_name != QR_ENGINE_IPERF3_NAME:
        return error_response("WRONG_ENGINE_TYPE",
                                f"Endpoint requires iperf3 engine, got '{engine_type_name}'")

    node_id = inst.get("node_id")
    instance_name = inst.get("name", "")
    port = inst.get("port_assigned")

    # Determine preset mode: server or client
    merged = {}
    try:
        merged = _mc(_CONFIG["db_path"], inst_id)
    except Exception as _e:
        logger.debug("api_run_client inst=%d: config merge failed, using defaults: %s", inst_id, _e)
        merged = {"env": {}, "cli_opts": [], "model": {}}

    cli_opts = merged.get("cli_opts", []) if isinstance(merged, dict) else []
    is_client = any("-c" in str(o) for o in cli_opts)
    is_server = any("-s" in str(o) for o in cli_opts)

    # BUG-IPERF3: Resolve target_host/target_port from config_override for client mode
    co_raw = inst.get("config_override") or {}
    if isinstance(co_raw, str):
        try:
            co_raw = json.loads(co_raw)
        except Exception as _e:
            logger.debug("api_run_client inst=%d: config_override JSON parse failed: %s", inst_id, _e)
            co_raw = {}

    if is_client:
        target_host = co_raw.get("target_host", "") or ""
        target_port = co_raw.get("target_port", "") or ""
        if target_host or target_port:
            # Resolve Jinja2 template vars in cli_opts from config_override
            resolved_cli_opts = []
            for opt in cli_opts:
                if "{{ target_host }}" in str(opt):
                    opt = str(opt).replace("{{ target_host }}", target_host)
                if "{{ target_port }}" in str(opt):
                    opt = str(opt).replace("{{ target_port }}", target_port)
                resolved_cli_opts.append(opt)
            cli_opts = resolved_cli_opts

    # DEBUG: log past all guards
    # Resolve inventory hostname
    inv_hostname = None
    if node_id:
        try:
            from db.adapters.nodes import get_node as _gn
            nd = _gn(_CONFIG["db_path"], node_id)
            if nd:
                inv_hostname = (nd.get("ansible_inventory_host") or
                                nd.get("hostname") or
                                nd.get("name"))
        except Exception as _e:
            logger.debug("api_run_client inst=%d: node lookup for inventory hostname failed: %s", inst_id, _e)
    if not inv_hostname:
        return error_response("NO_HOSTNAME", f"No hostname resolved for node {node_id}")

  # Step 1: Deploy if needed (install iperf3, create systemd unit)
    sob = inst.get("start_on_boot") or False
    env = merged.get("env", {}) if isinstance(merged, dict) else {}
    binary_path = env.get("binary_path", "/usr/bin/iperf3")
    # Default device from instance config; fallback to "CPU" is acceptable here
    # since this is the iperf3 client path where GPU mode doesn't apply
    device = inst.get("gpu_device", "") or ""

    extra_vars = {
        "inventory_host": inv_hostname,
        "node_id": node_id,
        "instance_id": inst["id"],
        "instance_name": instance_name,
        "engine_type": engine_type_name,
        "instance_port": port or 0,
        "binary_path": binary_path,
        "device": device,
        "start_on_boot": False,  # Do not auto-start; we control lifecycle
        "restart_policy": env.get("restart_policy", "no"),
        "rpc_host": QR_DEFAULT_LOCALHOST,
        "instance_env_vars": [],
        "gpu_device": device,
        "merged_env": env,
        "merged_cli_opts": " ".join(cli_opts) if isinstance(cli_opts, list) else str(cli_opts or ""),
        "target_host": co_raw.get("target_host", "") if isinstance(co_raw, dict) else "",
        "target_port": co_raw.get("target_port", "") if isinstance(co_raw, dict) else "",
        "instance_uuid": inst.get("instance_uuid", ""),
    }

    r = _execute_playbook("DEPLOY_IPERF3_V1", resolver_type="playbook_id",
                           limit=inv_hostname, extra_vars=extra_vars,
                           action_type="deploy_instance")
    if r["error"]:
        return error_response("DEPLOY_ERROR", r["error"])
    if r.get("failed"):
        return error_response("DEPLOY_FAILED",
                              f"Deploy failed: {r.get('result', {}).get('error', 'unknown')}")

    # Step 2: Run the appropriate action based on mode
    if is_client:
        return _run_iperf3_client(inst_id, engine_type_name, node_id, inv_hostname)
    elif is_server:
        # Server: just start and mark running
        try:
            transition_state(_CONFIG["db_path"], inst_id, QR_STATE_STARTING)
        except Exception as _e:
            logger.debug("api_run_client inst=%d: starting state transition (server): %s", inst_id, _e)
        try:
            result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
            if result.get("success"):
                try:
                    transition_state(_CONFIG["db_path"], inst_id, QR_STATE_RUNNING)
                except Exception as _e:
                    logger.debug("api_run_client inst=%d: running state transition (server): %s", inst_id, _e)
                log_action(_CONFIG["db_path"], inst_id, QR_JOB_START, "success")
                return success_single({"action": "run_client", "instance_id": inst_id,
                                       "state": QR_STATE_RUNNING, "message": "Server started"})
            else:
                log_action(_CONFIG["db_path"], inst_id, QR_JOB_START, "failed",
                            detail={"remote": result})
                try:
                    transition_state(_CONFIG["db_path"], inst_id, QR_STATE_ERROR)
                except Exception as _e:
                    logger.debug("api_run_client inst=%d: error state transition after server start failure: %s", inst_id, _e)
                return error_response("DEPLOYMENT_FAILED",
                                        f"Server start failed: {result.get('error', 'unknown')}")
        except Exception as exc:
            return error_response("DEPLOYMENT_FAILED", str(exc))
    else:
        # Default: treat as server
        log_action(_CONFIG["db_path"], inst_id, "run_client", "success",
                    detail={"mode": "server_default"})
        return success_single({"action": "run_client", "instance_id": inst_id,
                                "state": "running", "message": "Started as server (no explicit -s/-c)"})


def api_update_log_level(inst_id):
    """Update the log level for a llama-server instance.

    Sets LLAMA_ARG_LOG_LEVEL in config_override.env — takes effect on next deploy/restart.
    Accepts level string: off, info, debug (off = production default, no verbose output).

    Args:
        inst_id: Integer instance ID.

    Returns:
        JSON response with updated config_override.
    """
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "Invalid JSON"))

    level = body.get("level")
    valid_levels = ("off", "info", "debug")
    if level not in valid_levels:
        return error_response("VALIDATION_ERROR",
                                f"Level must be one of: {', '.join(valid_levels)}")

    from db.adapters.instances import get_instance, update_instance as _ui
    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    current_override = inst.get("config_override", {}) or {}
    env_dict = current_override.get("env", {})
    env_dict["LLAMA_ARG_LOG_LEVEL"] = level

    try:
        updated = _ui(_CONFIG["db_path"], inst_id, config_override=current_override)
        updated["log_level"] = level
        return success_single(updated)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))


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
    import os as _os
    from db.adapters.instances import get_instance as _gi
    from db.adapters.instances import list_instances as _li
    from db.sqlite import pool
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

    if not node_id:
        results["warnings"].append("No node context for UUID check")
        return results

    # Resolve target hostname from node record (not instance.node_name — name is display-only)
    nd = _gn(db_path, node_id)
    if not nd:
        results["warnings"].append(f"No node record found for node_id={node_id}")
        return results
    # SSOT: QR_DEFAULT_LOCALHOST from lib.qr_engine_ids (127.0.0.1)
    # Fail explicitly if no hostname — prevents silent localhost fallback in production
    target_host = nd.get("ansible_inventory_host") or nd.get("hostname")
    if not target_host:
        raise ValueError(f"No hostname resolved for node_id={node_id}")

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
        inv_script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                "..", "lib", "qr_dynamic_inventory.py")
        result = _sub.run(
            [
                "ansible", target_host, "-i", inv_script,
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
            # Extract instance name from unit file path (e.g., /etc/systemd/system/qr-2-llama_server.service)
            import re as _re_match
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


def _get_node_build_state(db_path, node_id):
    """Read the node_build_state from the nodes table.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.

    Returns:
        String state ('idle' or 'running'), defaults to 'idle'.
    """
    try:
        from db.sqlite import pool
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT node_build_state FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return row[0] if row and row[0] else "idle"
    except Exception as _e:
        logger.debug("_get_node_build_state node=%d: build state query failed: %s", node_id, _e)
        return "idle"


