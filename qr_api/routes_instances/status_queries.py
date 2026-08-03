import json
import logging as _logging
import os as _os

from flask import request

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from db.sqlite import pool as db_pool
from qr_api.routes_instances.instance_health import api_instance_status
from qr_api.lib_instances import (_execute_playbook, _check_node_active, _get_keep_shared_build, _get_deploy_lock)
from lib.lib_qr_actions import log_qr_action

logger = _logging.getLogger(__name__)
from lib.qr_engine_ids import (
    QR_ENGINE_API_NAME, QR_ENGINE_WEBUI_NAME, QR_ENGINE_MCP_NAME,
    QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_IPERF3_NAME, QR_ENGINE_UNIVERSAL_NAME,
    QR_ENGINE_SUBPROCESS_NAME, QR_ENGINE_SCHEDULER_NAME,
    QR_ENGINE_TIMESTAMP_PROXY_NAME,
    QR_ENGINE_API, QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC,
    QR_ENGINE_SUBPROCESS, QR_ENGINE_UNIVERSAL,
    QR_JOB_DEPLOY, QR_JOB_DEPLOY_FAST, QR_JOB_DEPLOY_BINARY, QR_JOB_RECONFIGURE,
)
from lib.lib_engine_states import (
    QR_STATE_RUNNING, QR_STATE_STOPPED, QR_STATE_STARTING,
    QR_STATE_STOPPING, QR_STATE_ERROR, QR_STATE_UNCONFIGURED,
    QR_STATE_DEPLOYED, QR_STATE_DEPLOYING, QR_STATE_CONFIGURING,
    QR_STATE_LOADING, QR_STATE_UPDATING, QR_STATE_BUILD_ERROR,
    QR_STATE_COMPILING,
)


def _health_probe_instance(inst_id, hostname, engine_type_name=None, node_id=None):
    """Probe remote systemd service health via instance_health_check playbook.

    Used by start/stop/restart for DESIGN-2 health-first semantics.
    Returns dict with keys: service_state, error (None=healthy), main_pid.

    Args:
        inst_id: Instance ID (for logging).
        hostname: Remote node hostname/IP.
        engine_type_name: Engine type name for correct unit_name suffix.
        node_id: Node ID (for sudo detection on localhost deployments).

    Returns:
        Dict: {"service_state": "...", "error": None|str, "main_pid": int|None}
    """
    # Build correct unit_name from engine type (llama_rpc, llama_server, iperf3)
    unit_suffix = engine_type_name or "llama_server"  # llama_server is most common default
    try:
        r = _execute_playbook("instance_health_check", resolver_type="playbook_id",
                              limit=hostname,
                              extra_vars={"inventory_host": hostname,
                                          "unit_name": f"qr-{inst_id}-{unit_suffix}",
                                          "node_id": node_id},
                              action_type="health_check")

        if r.get("error"):
            return {"service_state": "unknown", "error": r["error"], "main_pid": None}

        # Parse JSON from playbook output (same logic as engine._check_remote_service)
        svc_result = r.get("result", {})
        json_str = ""
        for play in svc_result.get("results", {}).get("plays", []):
            for task in play.get("tasks", []):
                if "Output health check result" in task.get("task", {}).get("name", ""):
                    entry = task.get("results", [{}])[0]
                    json_str = entry.get("msg", "")

        if not json_str:
            return {"service_state": "unknown", "error": "no playbook output", "main_pid": None}

        try:
            data = json.loads(json_str)
            main_pid = int(data["main_pid"]) if data.get("main_pid") and data["main_pid"] not in ("0",) else None
            return {"service_state": data.get("service_state", "unknown"),
                    "error": None, "main_pid": main_pid}
        except _json.JSONDecodeError:
            return {"service_state": "unknown", "error": f"parse error: {json_str!r}", "main_pid": None}

    except Exception as exc:
        return {"service_state": "unknown", "error": str(exc), "main_pid": None}


def _engine_get_instance_status(db_path, instance_id):
    """Dispatch get_instance_status() to the correct engine class.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Instance primary key.

    Returns:
        Status dict (STATUS-1 format) or None if instance not found.
    """
    from db.sqlite import pool
    from lib.qr_engine_ids import QR_SYSTEM_IDS as _sys_ids

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT i.engine_type_id, i.system_managed FROM instances i WHERE i.id = ?",
            (instance_id,),
        ).fetchone()
        if not row:
            return None
        is_system_managed = row["system_managed"] == 1
        eng_name = conn.execute(
            "SELECT name FROM engine_types WHERE id = ?", (row["engine_type_id"],)
        ).fetchone()
        if not eng_name:
            return None
        engine_name = eng_name["name"]

    # Dispatch to engine class
    result = None
    if engine_name == QR_ENGINE_LLAMA_SERVER_NAME:
        from engine.llama_server import LlamaServerEngine
        result = LlamaServerEngine.get_instance_status(db_path, instance_id)
    elif engine_name == QR_ENGINE_LLAMA_RPC_NAME:
        from engine.llama_rpc import RpcEngine
        result = RpcEngine.get_instance_status(db_path, instance_id)
    elif engine_name == QR_ENGINE_IPERF3_NAME:
        import importlib as _il
        mod = _il.import_module("engine.iperf3")
        cls = getattr(mod, "Iperf3Engine")
        result = cls.get_instance_status(db_path, instance_id)
    elif engine_name == QR_ENGINE_UNIVERSAL_NAME:
        from engine.universal import UniversalEngine
        result = UniversalEngine.get_instance_status(db_path, instance_id)
    elif engine_name == QR_ENGINE_SUBPROCESS_NAME:
        from engine.subprocess import QrSubprocessEngine
        result = QrSubprocessEngine.get_instance_status(db_path, instance_id)
    elif engine_name == QR_ENGINE_SCHEDULER_NAME:
        from engine.quickrobot_scheduler import SchedulerEngine
        result = SchedulerEngine.get_instance_status(db_path, instance_id)
    elif engine_name == QR_ENGINE_API_NAME:
        # API instance (ID 1) — the running process itself. No restart action.
        from db.adapters.instances import get_instance as _gi_api
        from lib.lib_system_engine import get_system_engine_pid as _gep_api
        inst = _gi_api(db_path, instance_id)
        if not inst:
            return None
        pid = _gep_api(db_path, inst["id"])
        running = False
        uptime_seconds = 0
        rss_bytes = 0
        if pid and isinstance(pid, int):
            try:
                import psutil as _psutil
                proc = _psutil.Process(pid)
                if proc.status() != "zombie":
                    running = True
                    uptime_seconds = int(__import__("time").time() - proc.create_time())
                    rss_bytes = proc.memory_info().rss
            except Exception as _e:
                logger.debug("_engine_get_instance_status inst=%d: psutil process check failed: %s", inst["id"], _e)
        result = {
            "id": inst["id"],
            "state": "running" if running else inst.get("state", "stopped"),
            "engine_type_name": engine_name,
            "engine_data": {
                "pid": pid if running else None,
                "uptime_seconds": uptime_seconds,
                "rss_bytes": rss_bytes,
            },
            "actions": [],  # API is the running process — no restart needed
            "warnings": [{"message": "API server is the running process; restart requires stopping this session"}],
            "_meta": {"valid_next_states": [], "is_transitioning": False},
        }
    elif engine_name in (QR_ENGINE_WEBUI_NAME, QR_ENGINE_MCP_NAME, QR_ENGINE_SCHEDULER_NAME):
        # WebUI/MCP: check process health + restart action
        from db.adapters.instances import get_instance as _gi
        from lib.lib_system_engine import get_system_engine_pid
        
        inst = _gi(db_path, instance_id)
        if not inst:
            return None
        
        # Map full engine name to short name for get_system_engine_pid
        _engine_short_map = {
            QR_ENGINE_MCP_NAME: "mcp",
            QR_ENGINE_WEBUI_NAME: "webui",
        }
        short_engine_name = _engine_short_map.get(engine_name, engine_name)
        
        # Load minimal env_config for get_system_engine_pid (needed for restart logic)
        from lib.lib_system_engine import load_env_config as _load_env
        try:
            env_config = _load_env(_os.getcwd())
        except FileNotFoundError:
            env_config = {}
        
        pid = get_system_engine_pid(short_engine_name, env_config)
        running = False
        uptime_seconds = 0
        rss_bytes = 0
        if pid and isinstance(pid, int):
            try:
                import psutil as _psutil
                proc = _psutil.Process(pid)
                if proc.status() != "zombie":
                    running = True
                    uptime_seconds = int(__import__("time").time() - proc.create_time())
                    rss_bytes = proc.memory_info().rss
            except Exception as _e:
                logger.debug("_engine_get_instance_status inst=%d: psutil process check failed: %s", inst["id"], _e)
        result = {
            "id": inst["id"],
            "state": "running" if running else inst.get("state", "stopped"),
            "engine_type_name": engine_name,
            "engine_data": {
                "pid": pid if running else None,
                "uptime_seconds": uptime_seconds,
                "rss_bytes": rss_bytes,
            },
            "actions": [{"name": "restart", "label": "Restart"}],
            "warnings": [],
            "_meta": {"valid_next_states": ["stopping", "starting"], "is_transitioning": False},
        }
    else:
        # Default: minimal status for unknown engines
        with pool(db_path) as conn:
            inst = conn.execute(
                "SELECT id, state FROM instances WHERE id = ?",
                (instance_id,),
            ).fetchone()
        if not inst:
            return None
        result = {
            "id": inst["id"],
            "state": inst["state"],
            "engine_type_name": engine_name,
            "engine_data": {},
            "actions": [],
            "warnings": [],
            "_meta": {"valid_next_states": [], "is_transitioning": False},
        }

    # Delete action is now managed per-state in each engine's _get_available_actions()
    # (llama_server, llama_rpc) — no global post-processing needed.
    # This prevents delete from appearing in states where it shouldn't (e.g., running).

    # Add system_managed flag to response for WebUI status badges
    if result:
        result["system_managed"] = is_system_managed

    return result


def api_create_instance():
    """Create a new engine instance."""
    import sys as _sys; print(f"[PRINT] api_create_instance called for instance", file=_sys.stderr, flush=True)
    from db.adapters.instances import create_instance, merge_configs, assign_port
    from db.adapters.engine_types import get_engine_type
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    name = body.get("name")
    engine_type_id = body.get("engine_type_id")
    node_id = body.get("node_id")

    if not all([name, engine_type_id, node_id]):
        return error_response("VALIDATION_ERROR", "name, engine_type_id, and node_id are required")

    # Verify node exists and is active before creating instance
    node = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(node, tuple):
        return node  # error_response — return immediately

    # Subprocess engine is localhost-only — enforce node_id==1
    if engine_type_id == QR_ENGINE_SUBPROCESS and node_id != 1:
        return error_response("INVALID_NODE",
            "Subprocess instances must be on localhost (node_id=1). Subprocess runs local to API host.")

    # Enforce max_instances per engine type
    et_info = get_engine_type(_CONFIG["db_path"], engine_type_id)
    if et_info:
        cap = et_info.get("capabilities", {})
        if isinstance(cap, str):
            try:
                cap = json.loads(cap)
            except Exception as _e:
                logger.debug("api_create_instance: capability JSON parse failed, using empty cap: %s", _e)
                cap = {}
        max_inst = cap.get("max_instances")
        if max_inst is not None and max_inst > 0:
            from db.adapters.instances import list_instances as _lie
            existing = _lie(_CONFIG["db_path"], engine_type_id=engine_type_id)
            if len(existing) >= max_inst:
                return error_response("MAX_INSTANCES_REACHED",
                    f"Engine '{et_info.get('name', '')}' already has {max_inst} instance(s) (limit: {max_inst})")

    preset_id = body.get("preset_id")
    config_override = body.get("config_override", {})

    # Check preset ↔ engine_type mismatch (e.g., llama_server preset on llama_rpc instance)
    _preset_engine_mismatch = None
    if preset_id is not None:
        try:
            from db.sqlite import pool as _pool
            with _pool(_CONFIG["db_path"]) as conn:
                preset_row = conn.execute(
                    "SELECT engine_type_id FROM engine_presets WHERE id = ?", (preset_id,)
                ).fetchone()
                if preset_row and preset_row[0] != engine_type_id:
                    preset_et_name = conn.execute(
                        "SELECT name FROM engine_types WHERE id = ?", (preset_row[0],)
                    ).fetchone()
                    _preset_et_name = preset_et_name[0] if preset_et_name else "unknown"
                    _instance_et_name = get_engine_type(_CONFIG["db_path"], engine_type_id)
                    _inst_name = _instance_et_name.get("name", "unknown") if _instance_et_name else "unknown"
                    _preset_engine_mismatch = (
                        "Preset engine_type_id={} ({}) does not match instance engine_type_id={} ({})".format(
                            preset_row[0], _preset_et_name, engine_type_id, _inst_name
                        )
                    )
        except Exception as _e:
            logger.debug("preset mismatch check failed: %s", _e)
    port_override = body.get("port_override")
    gpu_device = body.get("gpu_device")

    # skip_build: from body → engine_config → default False
    _skip_build_from_body = body.get("skip_build")  # None, True/False, or not present
    # Resolve skip_build value (same logic as deploy_instance)
    _skip_build = None
    if _skip_build_from_body is not None:
        if isinstance(_skip_build_from_body, bool):
            _skip_build = _skip_build_from_body
        elif isinstance(_skip_build_from_body, str):
            _skip_build = _skip_build_from_body.lower() in ("true", "1")
        elif isinstance(_skip_build_from_body, (int, float)):
            _skip_build = bool(_skip_build_from_body)
    if _skip_build is None and engine_type_id in (QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC):
        try:
            from db.adapters.configs import get_engine_config as _gec
            ec = _gec(_CONFIG["db_path"], engine_type_id) or {}
            sv_raw = ec.get("skip_build")
            sv = sv_raw["value"] if isinstance(sv_raw, dict) and "value" in sv_raw else str(sv_raw) if sv_raw else ""
            if str(sv).lower() in ("true", "1"):
                _skip_build = True
        except Exception as _e:
            logger.debug("api_create_instance: skip_build engine_config lookup failed: %s", _e)
    # Cluster binding fields (llama_server only)
    rpc_bind_ids = body.get("rpc_bind_ids")  # explicit binding from request
    split_mode = body.get("split_mode")       # explicit split mode from request
    split_val = config_override.pop("split", None) if isinstance(config_override, dict) else None
    # Also check top-level "split" key
    if "split" in body:
        split_val = body.pop("split")

    # Default split value: 100 for both llama_server and llama_rpc (full tensor split)
    if split_val is None:
        split_val = 100 if engine_type_id in (QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC) else 0

    # Engine-type-specific start_on_boot default — read from engine_configs (DB)
    if "start_on_boot" not in body:
        from db.adapters.configs import get_engine_config as _gec
        _cfg = _gec(_CONFIG["db_path"], engine_type_id, "start_on_boot")
        default_sob = _cfg.get("value", "false") if _cfg else "false"
    else:
        default_sob = body.get("start_on_boot")

    # Normalize start_on_boot to "true"/"false" string
    if isinstance(default_sob, bool):
        default_sob = "true" if default_sob else "false"
    elif isinstance(default_sob, str):
        default_sob = "true" if default_sob.lower() in ("true", "1", "yes") else "false"
    elif isinstance(default_sob, int):
        default_sob = "true" if default_sob else "false"

    # start_after_deploy: default False for all engines (explicit opt-in)
    start_after_deploy = body.get("start_after_deploy", False)

    # Resolve cluster binding fields from preset if not explicitly provided (llama_server)
    if engine_type_id == QR_ENGINE_LLAMA_SERVER and rpc_bind_ids is None and preset_id is not None:
        # Load preset to check for rpc_bind_ids
        from db.sqlite import pool
        with pool(_CONFIG["db_path"]) as conn:
            preset_row = conn.execute(
                "SELECT config_template FROM engine_presets WHERE id = ?", (preset_id,)
            ).fetchone()
        if preset_row and preset_row[0]:
            try:
                pt = json.loads(preset_row[0])
                if isinstance(pt, dict):
                    if rpc_bind_ids is None and "rpc_bind_ids" in pt:
                        rpc_bind_ids = pt["rpc_bind_ids"]
                    if split_mode is None and "split_mode" in pt:
                        split_mode = pt["split_mode"]
            except (json.JSONDecodeError, TypeError):
                pass

    # Defaults for cluster binding fields
    if rpc_bind_ids is None:
        rpc_bind_ids = []
    if split_mode is None:
        split_mode = "layer"

    try:
        instance = create_instance(_CONFIG["db_path"], name, engine_type_id, node_id,
                                preset_id=preset_id, config_override=config_override,
                                start_on_boot=default_sob, start_after_deploy=start_after_deploy,
                                gpu_device=gpu_device)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    # Auto-generate auth_token for llama_server / llama_rpc instances (configurable via engine_config)
    if engine_type_id in (QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC):
        try:
            from db.adapters.configs import get_engine_config as _get_cfg
            auto_auth = _get_cfg(_CONFIG["db_path"], engine_type_id, "auto_auth_token")
            if auto_auth and str(auto_auth.get("value", "true")).lower() in ("true", "1", "yes"):
                from db.sqlite import pool as _pool
                from lib.lib_token import generate_api_key as _gen_key
                token = _gen_key()
                with _pool(_CONFIG["db_path"]) as conn:
                    conn.execute(
                        "UPDATE instances SET auth_token = ? WHERE id = ?",
                        (token, instance["id"]),
                    )
                instance["auth_token"] = token
        except Exception as _e:
            logger.debug("api_create_instance: token generation failed (non-critical): %s", _e)

    # Allocate port
    try:
        # Pass port_override to assign_port so it respects user intent
        port = assign_port(_CONFIG["db_path"], node_id, engine_type_id,
                            exclude_instance_id=instance["id"],
                            port_override=port_override if port_override else None)
        from db.adapters.instances import update_instance as _ui
        update_kwargs = {"port_assigned": port, "config_override": config_override}
        if port_override:
            update_kwargs["port_override"] = port_override
        if gpu_device:
            update_kwargs["gpu_device"] = gpu_device
        instance = _ui(_CONFIG["db_path"], instance["id"], **update_kwargs)
    except Exception as _e:
        logger.debug("api_create_instance: port allocation failed (best-effort): %s", _e)

    # Merge configs and store
    try:
        merged = merge_configs(_CONFIG["db_path"], instance["id"])
        from db.adapters.instances import update_instance as _ui2
        update_kwargs = {"ansible_vars": merged}
        # Write cluster binding fields to instance (top-level columns)
        if engine_type_id == QR_ENGINE_LLAMA_SERVER:
            update_kwargs["rpc_bind_ids"] = json.dumps(rpc_bind_ids) if isinstance(rpc_bind_ids, list) else rpc_bind_ids
            update_kwargs["split_mode"] = split_mode
        if split_val is not None:
            config_override["split"] = split_val
            update_kwargs["split"] = int(split_val)
        _ui2(_CONFIG["db_path"], instance["id"], **update_kwargs)
    except Exception as _e:
        logger.debug("api_create_instance: config merge failed (best-effort): %s", _e)

    # Populate node_hostname from node record (required for playbook limit/extra_vars)
    try:
        from db.adapters.nodes import get_node as _gn_node
        from db.adapters.instances import update_instance as _ui_nh
        nd = _gn_node(_CONFIG["db_path"], node_id) if node_id else None
        if nd:
            nh = nd.get("ansible_inventory_host") or nd.get("hostname", "")
            nn = nd.get("name", "")
            if nh != instance.get("node_hostname"):
                _ui_nh(_CONFIG["db_path"], instance["id"], node_hostname=nh, node_name=nn)
                instance["node_hostname"] = nh
                instance["node_name"] = nn
    except Exception as _e:
        logger.debug("api_create_instance: node_hostname update failed (non-critical): %s", _e)

    # BINARY-DL: Extract binary_template_id from request body (orthogonal to preset)
    _binary_template_id = None
    if "binary_template_id" in body:
        try:
            _binary_template_id = int(body.get("binary_template_id"))
        except (ValueError, TypeError):
            pass

    # Template persistence (Issue C): resolve template and merge metadata + ID into config_override.
    # binary_template_id is always stored (for rebuild chain detection).
    # Metadata (if any) is merged on top for subprocess/universal engines.
    if _binary_template_id is not None:
        try:
            from db.sqlite import pool as _pool_bin
            with _pool_bin(_CONFIG["db_path"]) as _conn_bin:
                _bin_row = _conn_bin.execute(
                    "SELECT metadata FROM engine_binaries WHERE id=? AND is_active=1",
                    (_binary_template_id,),
                ).fetchone()
            # Always persist binary_template_id reference for rebuild detection
            config_override["binary_template_id"] = _binary_template_id
            if _bin_row and _bin_row.get("metadata"):
                try:
                    _bin_meta = json.loads(_bin_row["metadata"])
                    if isinstance(_bin_meta, dict):
                        for _k, _v in _bin_meta.items():
                            config_override[_k] = _v
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception as _e:
            logger.debug("api_create_instance inst=%d: template lookup failed: %s", instance.get("id", "??") if instance else "?", _e)

    # Persist updated config_override (includes merged template metadata) to DB
    try:
        from db.adapters.instances import update_instance as _ui_co
        _ui_co(_CONFIG["db_path"], instance["id"], config_override=config_override)
    except Exception as _e:
        logger.debug("api_create_instance inst=%d: config_override persistence failed: %s", instance.get("id", "??") if instance else "?", _e)

    # Auto-deploy if enabled and deploy_requested flag not explicitly false
    auto_deploy = _CONFIG.get("create_and_autodeploy", True)
    deploy_flag = body.get("deploy", True)
    do_deploy = auto_deploy and (isinstance(deploy_flag, bool) and deploy_flag or str(deploy_flag).lower() != "false")
    logger.warning("[qr] api_create_instance: auto_deploy=%s deploy_flag=%s do_deploy=%s _binary_template_id=%s _skip_build=%s", auto_deploy, deploy_flag, do_deploy, _binary_template_id, _skip_build)
    # Cleanup orphaned records on create failure (QUICKROBOT_CLEANUP_ON_CREATE_FAIL)
    _qr_env = _CONFIG.get("qr_env_config", {})
    cleanup_fail = _qr_env.get("QUICKROBOT_CLEANUP_ON_CREATE_FAIL", "true").lower() == "true"
    if do_deploy:
        try:
            # Use RUNNER-1 staged chain for consistent job/task tracking
            from lib.lib_runner import PlaybookRunner
            _job_type = QR_JOB_DEPLOY_FAST if _skip_build else QR_JOB_DEPLOY
            # Apply binary template chain selection (§3.2A)
            if _binary_template_id is not None:
                _job_type = QR_JOB_DEPLOY_BINARY
            logger.warning("[qr] api_create_instance: entering deploy block job_type=%s binary_template_id=%s", _job_type, _binary_template_id)
            runner = PlaybookRunner(_CONFIG["db_path"])
            result = runner.chain(instance["id"], job_type=_job_type,
                                  actor="api", skip_build=_skip_build, async_mode=True,
                                  binary_template_id=_binary_template_id)
            if not result.get("success", False):
                err_msg = result.get("message", "deploy failed")
                # Cleanup orphaned instance on deploy failure
                if cleanup_fail:
                    try:
                        from db.adapters.instances import delete_instance as _di
                        _di(_CONFIG["db_path"], instance["id"])
                        log_qr_action(_CONFIG["db_path"], "instance_create_cleanup_orphan",
                                      instance["id"], actor="api",
                                      details={"name": name, "engine_type_id": engine_type_id,
                                               "node_id": node_id,
                                               "reason": err_msg})
                    except Exception as _ce:
                        log_qr_action(_CONFIG["db_path"], "instance_create_cleanup_failed",
                                      instance["id"], actor="api",
                                      details={"name": name, "cleanup_error": str(_ce)})
                return error_response("DEPLOY_FAILED", f"Deploy preflight failed: {err_msg}")
        except Exception as exc:
            # Log the actual error so we can diagnose auto-deploy failures
            import traceback as _tb
            logger.error("[qr] api_create_instance(%d): auto-deploy failed (binary_template_id=%s): %s\n%s",
                         instance["id"], _binary_template_id, exc, _tb.format_exc())

    # Attach preset/engine mismatch warning to response for agents
    if _preset_engine_mismatch:
        instance["_warnings"] = [instance.get("_warnings")] + [_preset_engine_mismatch] if instance.get("_warnings") else [_preset_engine_mismatch]

    return success_single(instance)


def api_list_instances():
    """List instances with optional filters."""
    from db.adapters.instances import list_instances, check_system_managed as _csm
    from db.adapters.nodes import get_node as _gn
    et = request.args.get("engine_type_id")
    nid = request.args.get("node_id")
    st = request.args.get("state")
    orphan = request.args.get("orphan", "").lower() == "true"
    show_inactive = request.args.get("include_inactive", "false").lower() == "true"

    params = {}
    if et:
        params["engine_type_id"] = int(et)
    if nid:
        params["node_id"] = int(nid)
    if st:
        params["state"] = st
    if orphan:
        params["orphan"] = True

    instances = list_instances(_CONFIG["db_path"], **params)
    # Filter out instances on inactive nodes by default
    if not show_inactive:
        filtered = []
        for inst in instances:
            node_id = inst.get("node_id")
            if node_id and node_id != 1:  # localhost skip
                try:
                    node = _gn(_CONFIG["db_path"], node_id)
                    if node and not node.get("is_active", 1):
                        continue
                except Exception as _e:
                    logger.debug("api_list_instances: node active check failed for nid=%s, including instance: %s", node_id, _e)
            filtered.append(inst)
        instances = filtered
    # Enrich instances with _host_inactive flag (for WebUI/MCP)
    for inst in instances:
        nid = inst.get("node_id")
        if nid and nid != 1:  # localhost always active
            try:
                node = _gn(_CONFIG["db_path"], nid)
                inst["_host_inactive"] = bool(node and not node.get("is_active", 1))
            except Exception as _e:
                logger.debug("api_list_instances: node lookup for _host_inactive failed: %s", _e)
                inst["_host_inactive"] = False
        else:
            inst["_host_inactive"] = False
    # Add relative age for each instance
    import time as _time
    from lib.lib_utils import relative_age
    now_ts = _time.time()
    for inst in instances:
        inst["age_created"] = relative_age(inst.get("created_at"))
        # Compute per-instance config override indicator (for WebUI OVER badge)
        co_raw = inst.get("config_override") or "{}"
        if isinstance(co_raw, str):
            try:
                co_dict = json.loads(co_raw) if co_raw not in ("{}",) else {}
            except (json.JSONDecodeError, ValueError):
                co_dict = {}
        else:
            co_dict = co_raw or {}
        inst["has_custom_config"] = len(co_dict) > 0
        # System-managed instances: populate bind info and process uptime
        if _csm(_CONFIG["db_path"], inst["id"]):
            engine_type_name = inst.get("engine_type_name", "")
            co_raw = inst.get("config_override") or {}
            if isinstance(co_raw, str):
                try:
                    co_raw = json.loads(co_raw) if co_raw not in ("{}",) else {}
                except (json.JSONDecodeError, ValueError):
                    co_raw = {}
            co = dict(co_raw) if isinstance(co_raw, dict) else {}
            # Set node_hostname from config_override host (LAN IP)
            lan_host = co.get("host", "")
            if lan_host and lan_host != "0.0.0.0":
                inst["node_hostname"] = lan_host
            elif engine_type_name == QR_ENGINE_API_NAME:
                inst["node_hostname"] = _CONFIG.get("host", "127.0.0.1")
            else:
                # WebUI/MCP: read own host from .quickrobot.env (not API bind address)
                try:
                    from lib.lib_system_engine import load_env_config as _lec
                    env_cfg = _lec(_os.getcwd())
                    if engine_type_name == QR_ENGINE_WEBUI_NAME:
                        inst["node_hostname"] = env_cfg["QUICKROBOT_WEBUI_HOST"]
                    elif engine_type_name == QR_ENGINE_MCP_NAME:
                        inst["node_hostname"] = env_cfg["QUICKROBOT_MCP_HOST"]
                    else:
                        inst["node_hostname"] = _CONFIG.get("host", "127.0.0.1")
                except FileNotFoundError:
                    inst["node_hostname"] = _CONFIG.get("host", "127.0.0.1")
            # Set port_assigned and config_override for Remote column display
            if engine_type_name == QR_ENGINE_API_NAME:
                inst["port_assigned"] = _CONFIG.get("api_port", 8039)
                co["host"] = inst["node_hostname"]
                co["port"] = str(inst["port_assigned"])
            elif engine_type_name == QR_ENGINE_WEBUI_NAME:
                if not inst.get("port_assigned"):
                    # Read from engine_configs (seeded on DB init)
                    from db.adapters.configs import get_engine_config as _gec
                    port_cfg = _gec(_CONFIG["db_path"], 2, "web_ui_port")
                    if port_cfg and port_cfg.get("value"):
                        inst["port_assigned"] = int(port_cfg["value"])
                co["web_ui_host"] = co.get("web_ui_host", co.get("host", inst["node_hostname"]))
                co["web_ui_port"] = str(inst["port_assigned"]) if inst.get("port_assigned") else ""
            # Update config_override with computed values
            if co != co_raw:
                inst["config_override"] = co
            # MCP engine: add tool permission flags to instance data (BEFORE any continue)
            if engine_type_name == QR_ENGINE_MCP_NAME:
                try:
                    from db.adapters.configs import get_engine_config as _gec
                    et_id = inst.get("engine_type_id")
                    if et_id:
                        rr = _gec(_CONFIG["db_path"], et_id, "mcp_allow_reads") or {}
                        wr = _gec(_CONFIG["db_path"], et_id, "mcp_allow_writes") or {}
                        pr = _gec(_CONFIG["db_path"], et_id, "mcp_allow_proxy") or {}
                        inst["mcp_allow_reads"] = str(rr.get("value", "true")).lower() in ("true", "1", "yes")
                        inst["mcp_allow_writes"] = str(wr.get("value", "true")).lower() in ("true", "1", "yes")
                        inst["mcp_allow_proxy"] = str(pr.get("value", "true")).lower() in ("true", "1", "yes")
                except Exception as _e:
                    logger.debug("api_list_instances inst=%d: MCP engine config lookup failed: %s", inst["id"], _e)
                    inst["mcp_allow_reads"] = True
                    inst["mcp_allow_writes"] = True
                    inst["mcp_allow_proxy"] = True
                # MCP availability: check engine status for interpreter/package info
                try:
                    from engine import get_engine as _ge
                    mcp_eng = _ge(QR_ENGINE_MCP_NAME)
                    if mcp_eng:
                        _st = mcp_eng.get_status(inst["id"], _CONFIG["db_path"])
                        inst["_mcp_available"] = bool(_st.get("mcp_available", False))
                except Exception as _e:
                    logger.debug("api_list_instances inst=%d: MCP availability check failed: %s", inst["id"], _e)
                    inst["_mcp_available"] = True  # default optimistic
            # Warn that system-managed engines do not accept per-instance overrides.
            # Config comes from .quickrobot.env (L1) + engine_configs table (L2).
            if "warnings" not in inst:
                inst["warnings"] = []
            inst["warnings"].append(
                "System-managed engine: per-instance config_override is ignored at runtime. "
                "Use .quickrobot.env for host/port; use PUT /engines/<name>/settings for engine-level config."
            )
    # Apply shared system-managed state override (process health check via psutil)
    from qr_api.lib_instances import override_system_instance_states as _osis
    _osis(instances, _CONFIG)
    # Compute process uptime for all PID-tracked instances (system-managed + subprocess)
    import time as _time; now_ts = _time.time()
    for inst in instances:
        if inst.get("system_managed") or inst.get("engine_type_name") == QR_ENGINE_SUBPROCESS_NAME:
            pid = inst.get("pid_last_known") or inst.get("pid")
            if pid and isinstance(pid, int):
                try:
                    import psutil as _psutil
                    p = _psutil.Process(pid)
                    if p.is_running():
                        inst["process_age_seconds"] = int(now_ts - p.create_time())
                except Exception as _e:
                    logger.debug("api_list_instances inst=%d: process uptime check failed: %s", inst["id"], _e)
    # Debug: show engine_type_name for instance 3 (disable after debugging)
    for i in instances:
        if i.get('id') == 3:
            logger.debug("api_list_instances instance 3: engine_type_name=%s mcp_keys=%s", i.get('engine_type_name'), [k for k in i.keys() if 'mcp' in k.lower()])
    # Enrich with active job details (queued + running) — returns dict per instance
    try:
        with db_pool(_CONFIG["db_path"]) as conn:
            job_rows = conn.execute(
                "SELECT instance_id, MIN(id) as job_id, job_type, task_stage, stage_playbook, "
                "created_at, COUNT(*) as active_count FROM log_entries "
                "WHERE parent_id IS NULL AND status IN ('queued','running') GROUP BY instance_id"
            ).fetchall()
        job_map = {r["instance_id"]: r for r in job_rows}
        for inst in instances:
            ji = job_map.get(inst["id"])
            if ji:
                inst["active_jobs"] = {
                    "job_id": ji["job_id"], "job_type": ji["job_type"] or "",
                    "stage": ji["task_stage"] or "", "playbook": ji["stage_playbook"] or "",
                    "created_at": ji["created_at"] or "", "count": ji["active_count"],
                }
            else:
                inst["active_jobs"] = None
    except Exception as _e:
        logger.debug("api_list_instances: active job query failed: %s", _e)
        for inst in instances:
            inst["active_jobs"] = None

    # Pre-compute available actions per instance (state + engine_type derived).
    # Actions are deterministic — no AJAX needed client-side.
    _LLAMA_ACTIONS = {
        QR_STATE_UNCONFIGURED:   [{"name": "deploy", "label": "Deploy"}, {"name": "delete", "label": "Delete"}],
        QR_STATE_CONFIGURING:    [{"name": "stop", "label": "Stop"}],
        QR_STATE_DEPLOYING:      [{"name": "stop", "label": "Stop"}],
        QR_STATE_DEPLOYED:       [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
        QR_STATE_STARTING:       [{"name": "stop", "label": "Stop"}],
        QR_STATE_LOADING:        [{"name": "stop", "label": "Stop"}],
        QR_STATE_RUNNING:        [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "stop", "label": "Stop"}],
        QR_STATE_STOPPING:       [{"name": "start", "label": "Start"}],
        QR_STATE_STOPPED:        [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}],
        QR_STATE_ERROR:          [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
        QR_STATE_UPDATING:       [],
        QR_STATE_COMPILING:      [],
         QR_STATE_BUILD_ERROR:    [{"name": "deploy", "label": "Deploy"}, {"name": "start", "label": "Start"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
         "timeout":        [{"name": "deploy", "label": "Deploy"}],


    }
    _SUBPROCESS_ACTIONS = {
        QR_STATE_UNCONFIGURED:   [{"name": "deploy", "label": "Deploy"}, {"name": "delete", "label": "Delete"}],
        QR_STATE_CONFIGURING:    [{"name": "stop", "label": "Stop"}],
        QR_STATE_DEPLOYED:       [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
        QR_STATE_STARTING:       [{"name": "stop", "label": "Stop"}],
        QR_STATE_RUNNING:        [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "stop", "label": "Stop"}],
        QR_STATE_STOPPING:       [{"name": "start", "label": "Start"}],
        QR_STATE_STOPPED:        [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}],
        QR_STATE_ERROR:          [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
         QR_STATE_BUILD_ERROR:    [{"name": "deploy", "label": "Deploy"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
         "timeout":        [{"name": "deploy", "label": "Deploy"}],
     }
    # System-managed engines: use restart_system endpoint instead of standard stop/start
    _SYSTEM_ACTIONS = {
        QR_STATE_RUNNING:        [{"name": "restart_system", "label": "Restart"}],
        QR_STATE_STOPPED:        [{"name": "start", "label": "Start"}],
        QR_STATE_ERROR:          [{"name": "restart_system", "label": "Restart"}],
    }

    for inst in instances:
        engine = inst.get("engine_type_name", "")
        state = inst.get("state", "unknown")
        if engine in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME,
                      QR_ENGINE_IPERF3_NAME, QR_ENGINE_UNIVERSAL_NAME):
            action_map = _LLAMA_ACTIONS
        elif engine == QR_ENGINE_SUBPROCESS_NAME:
            action_map = _SUBPROCESS_ACTIONS
        elif engine in (QR_ENGINE_API_NAME, QR_ENGINE_WEBUI_NAME,
                        QR_ENGINE_MCP_NAME, QR_ENGINE_SCHEDULER_NAME):
            action_map = _SYSTEM_ACTIONS
        elif engine == QR_ENGINE_TIMESTAMP_PROXY_NAME:
            from lib.lib_engine_actions import _ACTION_MAPS as _epm
            action_map = _epm.get("timestamp_proxy", {})
        else:
            action_map = {}
        actions = action_map.get(state, [])
        # Hide delete for system-managed instances
        if inst.get("system_managed"):
            actions = [a for a in actions if a["name"] != "delete"]
        inst["_actions"] = actions

    return success_list(instances)


def api_get_instance(inst_id):
    """Get instance details with merged config."""
    from db.adapters.instances import get_instance, merge_configs, check_system_managed as _csm
    instance = get_instance(_CONFIG["db_path"], inst_id)
    if instance is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # Merge and attach config — use cluster builder for llama_server/rpc so
    # WebUI/GET API shows the same data that actually gets deployed
    engine_type_name = instance.get("engine_type_name", "")
    is_cluster = engine_type_name in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME)
    merged = {}
    if is_cluster:
        try:
            from lib.lib_cluster_env_builder import build_llama_server_env, build_rpc_server_env
            if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
                cluster_result = build_llama_server_env(_CONFIG["db_path"], inst_id)
            elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                cluster_result = build_rpc_server_env(_CONFIG["db_path"], inst_id)
            # Resolve restart_policy and start_on_boot from config_override chain
            try:
                from lib.lib_config_merge import _resolve_metadata, _parse_config_override
                from db.sqlite import pool as _pool
                with _pool(_CONFIG["db_path"]) as _conn:
                    rp, sob_default = _resolve_metadata(
                        _conn, instance.get("engine_type_id"),
                        instance.get("preset_id"),
                        instance.get("config_override")
                    )
                # Resolve start_on_boot override from config_override (top-level or nested env)
                sob = sob_default
                co_raw = _parse_config_override(instance.get("config_override") or "{}")
                if isinstance(co_raw, dict):
                    sob_raw = co_raw.get("start_on_boot")
                    if sob_raw is None and "env" in co_raw and isinstance(co_raw.get("env"), dict):
                        sob_raw = co_raw["env"].get("start_on_boot")
                    if sob_raw is not None:
                        if isinstance(sob_raw, bool):
                            sob = sob_raw
                        elif isinstance(sob_raw, str):
                            sob = sob_raw.lower() in ("true", "1", "yes")
                        else:
                            sob = bool(int(sob_raw))
            except Exception as _e:
                logger.debug("api_get_instance inst=%d: restart_policy/start_on_boot config parse fallback: %s", inst_id, _e)
                rp, sob = "no", False
            merged = {"env": cluster_result["env"], "cli_opts": [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else [], "model": {}, "restart_policy": rp or "no", "start_on_boot": sob}
        except Exception as exc:
            merged = {"_merge_error": str(exc)}
    else:
        try:
            merged = merge_configs(_CONFIG["db_path"], inst_id)
        except Exception as exc:
            merged = {"_merge_error": str(exc)}
    instance["merged_config"] = merged

    # Active overrides: parsed config_override (what user actually set, not preset/engine defaults)
    try:
        from lib.lib_config_merge import _parse_config_override as _pcov
        co_raw = instance.get("config_override") or {}
        instance["active_overrides"] = _pcov(co_raw)
    except Exception as _e:
        logger.debug("api_get_instance inst=%d: active_overrides parse failed: %s", inst_id, _e)
        instance["active_overrides"] = {}

    # System-managed instances: use config_override.host (LAN IP) instead of "localhost"
    if _csm(_CONFIG["db_path"], inst_id):
        engine_type_name = instance.get("engine_type_name", "")
        co_raw = instance.get("config_override") or {}
        if isinstance(co_raw, str):
            try:
                co_raw = json.loads(co_raw)
            except Exception as _e:
                logger.debug("api_get_instance inst=%d: config_override JSON parse failed: %s", inst_id, _e)
                co_raw = {}
        if isinstance(co_raw, dict):
            lan_host = co_raw.get("host", "")
            if lan_host and lan_host != "0.0.0.0":
                instance["node_hostname"] = lan_host
            elif engine_type_name == QR_ENGINE_API_NAME:
                # quickrobot-api IS the API — always use the configured host
                instance["node_hostname"] = _CONFIG.get("host", "127.0.0.1")
            else:
                # Read from .quickrobot.env for WebUI/MCP (not API bind address)
                try:
                    from lib.lib_system_engine import load_env_config as _lec
                    env_cfg = _lec(_os.getcwd())
                    if engine_type_name == QR_ENGINE_WEBUI_NAME:
                        instance["node_hostname"] = env_cfg.get("QUICKROBOT_WEBUI_HOST", "127.0.0.1")
                    elif engine_type_name == QR_ENGINE_MCP_NAME:
                        instance["node_hostname"] = env_cfg.get("QUICKROBOT_MCP_HOST", "127.0.0.1")
                    else:
                        instance["node_hostname"] = _CONFIG.get("host", "127.0.0.1")
                except FileNotFoundError:
                    instance["node_hostname"] = _CONFIG.get("host", "127.0.0.1")
        # System-managed engines do not accept per-instance config overrides.
        # Config comes from .quickrobot.env (L1) + engine_configs table (L2).
        # The config_override column is a legacy artifact; changes there are ignored at runtime.
        instance["has_custom_config"] = len(co_raw) > 0
        # Warn that system-managed engines do not accept per-instance overrides.
        # Config comes from .quickrobot.env (L1) + engine_configs table (L2).
        if "warnings" not in instance:
            instance["warnings"] = []
        instance["warnings"].append(
            "System-managed engine: per-instance config_override is ignored at runtime. "
            "Use .quickrobot.env for host/port; use PUT /engines/<name>/settings for engine-level config."
        )

    # Add cluster binding metadata for llama_server instances
    if instance.get("engine_type_name") == QR_ENGINE_LLAMA_SERVER_NAME:
        try:
            raw = instance.get("rpc_bind_ids") or "[]"
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            instance["rpc_bind_ids"] = parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            instance["rpc_bind_ids"] = []
        instance["split_mode"] = instance.get("split_mode") or "layer"
        instance["tensor_split"] = instance.get("tensor_split")

    return success_single(instance)


def api_update_instance(inst_id):
    """Update instance settings with automatic redeploy on config change.

    Detects if port_override, config_override, or preset_id changed vs
    previous DB values. If changed and instance is running/stopped/unconfigured,
    triggers a full redeploy lifecycle:
        - Running → stopping → verified stopped → deploy → started → running
        - Stopped → deploy → stays stopped (no auto-start)
    Each state transition is logged for WebUI polling visibility.
    """
    from db.adapters.instances import update_instance as _ui, get_instance, check_system_managed as _csm_update
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    # Check instance exists first
    existing = get_instance(_CONFIG["db_path"], inst_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # System-managed instances: reject config override changes (use engine config page)
    if _csm_update(_CONFIG["db_path"], inst_id):
        body_copy = dict(body)
        body_copy.pop("config_override", None)
        body_copy.pop("preset_id", None)
        body_copy.pop("port_override", None)
        body_copy.pop("port_assigned", None)
        # Only allow state-related changes (start_on_boot etc.) on system instances
        if body:
            return error_response("SYSTEM_MANAGED_INSTANCE",
                                    f"Instance {inst_id} is system-managed. "
                                    "Use the engine config page for settings changes.", 409)

    # Detect config changes before updating
    config_update_needed = False
    change_fields = []
    old_config = existing.get("config_override", {}) or {}
    old_preset = existing.get("preset_id")
    old_port = existing.get("port_assigned")

    # Merge incoming config_override with existing (partial PUT semantics)
    # Empty string "" means "delete key" — supports clearing fields like qr_cluster_gpu_override
    new_override = dict(old_config)
    if body.get("config_override"):
        co_in = body["config_override"]
        if isinstance(co_in, dict):
            for k, v in co_in.items():
                if v == "":
                    # Empty string = delete this key from config_override
                    new_override.pop(k, None)
                else:
                    new_override[k] = v
            # Remove any old keys that are not in the incoming request
            # (handles user unchecking/removing overrides via UI or API)
            for k in list(new_override.keys()):
                if k in old_config and k not in co_in:
                    del new_override[k]
        elif isinstance(co_in, str):
            try:
                co_in = json.loads(co_in)
                if isinstance(co_in, dict):
                    for k, v in co_in.items():
                        if v == "":
                            new_override.pop(k, None)
                        else:
                            new_override[k] = v
                    # Same removal logic for JSON-string payloads
                    for k in list(new_override.keys()):
                        if k in old_config and k not in co_in:
                            del new_override[k]
            except Exception as _e:
                logger.debug("api_update_instance inst=%d: config_override JSON parse fallback: %s", inst_id, _e)
    new_preset = body.get("preset_id", old_preset)
    new_port = body.get("port_override", body.get("port_assigned", old_port))

    if new_override != old_config:
        config_update_needed = True
        change_fields.append("config_override")
    if new_preset != old_preset:
        config_update_needed = True
        change_fields.append("preset_id")
    if isinstance(new_port, int) and new_port > 0 and new_port != old_port:
        config_update_needed = True
        change_fields.append("port")

    # Fast path: preset-only change. Skip generic merge chain (read→compare→merge→chain).
    # Just update preset_id in DB, then trigger reconfigure. Eliminates spurious error logs
    # from the intermediate merge_configs() call and avoids redundant DB reads/writes.
    # Only applies when skip_build is not explicitly False (which signals full deploy).
    explicit_no_skip = body.get("skip_build") is not None and body["skip_build"] == False
    preset_only = (new_preset != old_preset
                   and "config_override" not in change_fields
                   and "port" not in change_fields
                   and not explicit_no_skip)

    # Normalize start_on_boot: accept "true"/"false", 0/1, True/False → store as string
    if "start_on_boot" in body:
        sob = body["start_on_boot"]
        if isinstance(sob, bool):
            body["start_on_boot"] = "true" if sob else "false"
        elif isinstance(sob, str):
            body["start_on_boot"] = "true" if sob.lower() in ("true", "1", "yes") else "false"
        elif isinstance(sob, int):
            body["start_on_boot"] = "true" if sob else "false"

    # Also extract start_on_boot from config_override.env and write to DB column
    # so recover_subprocess_instances() sees it (reads instances.start_on_boot directly)
    if "config_override" in body and body["config_override"]:
        co_in = body["config_override"]
        if isinstance(co_in, dict):
            env = co_in.get("env", {})
            sob_env = env.get("start_on_boot")
            if sob_env is not None:
                if isinstance(sob_env, bool):
                    body["start_on_boot"] = "true" if sob_env else "false"
                elif isinstance(sob_env, str):
                    body["start_on_boot"] = "true" if sob_env.lower() in ("true", "1", "yes") else "false"
                elif isinstance(sob_env, int):
                    body["start_on_boot"] = "true" if sob_env else "false"

    # Update instance fields (handle config_override separately)
    try:
        update_fields = {k: v for k, v in body.items() if k != "config_override"}
        if body.get("config_override") is not None:
            update_fields["config_override"] = new_override  # merged, not raw body value
        instance = _ui(_CONFIG["db_path"], inst_id, **update_fields)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    # Fast path for preset-only changes: skip merge_configs + generic flow.
    # The reconfigure chain reads the new preset from DB and regenerates config.
    # Eliminates spurious error logs from intermediate merge failures.
    if preset_only:
        from db.adapters.instances import transition_state as _ts, log_action as _log
        current_state = instance.get("state", "")
        if current_state in (QR_STATE_RUNNING, QR_STATE_STOPPED, QR_STATE_ERROR):
            lock = _get_deploy_lock(inst_id)
            if not lock.acquire(blocking=False):
                return error_response("BUSY", f"Config update already in progress for instance {inst_id}")
            try:
                from lib.lib_runner import PlaybookRunner as _PR
                runner = _PR(_CONFIG["db_path"])
                result = runner.chain(inst_id, job_type=QR_JOB_RECONFIGURE, actor="api", async_mode=True)
                if result.get("success"):
                    instance["config_update_triggered"] = True
                    instance["change_fields"] = change_fields
                else:
                    _log(_CONFIG["db_path"], inst_id, "preset_change", "failed",
                         detail={"error": result.get("message", "")})
            except Exception as exc:
                try:
                    _ts(_CONFIG["db_path"], inst_id, "running")
                except Exception as _e:
                    logger.debug("api_update_instance inst=%d: preset change exception handler fallback: %s", inst_id, _e)
            finally:
                lock.release()
    else:
        # Re-merge config after update (generic path for mixed changes)
        try:
            from db.adapters.instances import merge_configs
            merged = merge_configs(_CONFIG["db_path"], inst_id)
            _ui(_CONFIG["db_path"], inst_id, ansible_vars=merged)
            instance["merged_config"] = merged
        except Exception as _e:
            logger.debug("api_update_instance inst=%d: re-merge config after update failed: %s", inst_id, _e)

        # Trigger config update with proper state lifecycle if config changed
        if config_update_needed:
            from db.adapters.instances import get_instance as _gi, transition_state as _ts, log_action as _log
            inst = _gi(_CONFIG["db_path"], inst_id)
            engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME) if inst else QR_ENGINE_LLAMA_RPC_NAME
            node_id = inst.get("node_id") if inst else None
            current_state = instance.get("state", "")

            if engine_type_name in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME, QR_ENGINE_IPERF3_NAME):
                # BC-1: Config-only update via RUNNER-1 staged chain
                # Uses deploy_config_env + service_start playbooks through job/task system.
                # Creates proper job+task records so SSE progress bar and task log work.
                # No git clone/pull, no cmake build. Works identically regardless of running or stopped state.
                if current_state in (QR_STATE_RUNNING, QR_STATE_STOPPED, QR_STATE_ERROR) and config_update_needed:
                    lock = _get_deploy_lock(inst_id)
                    if not lock.acquire(blocking=False):
                        return error_response("BUSY", f"Config update already in progress for instance {inst_id}")
                    try:
                        from lib.lib_runner import PlaybookRunner as _PR
                        runner = _PR(_CONFIG["db_path"])
                        # Reconfigure chain (config_env + service_start) runs async — returns
                        # instantly. Scheduler picks up tasks; instance transitions deploying→running.
                        # The start stage handles stop→start internally; no separate restart needed.
                        result = runner.chain(inst_id, job_type=QR_JOB_RECONFIGURE, actor="api", async_mode=True)
                        if result.get("success"):
                            # chain(async_mode=True) already sets initial state via STAGE_STATE_MAP
                            # (first task stage determines display state: deploying→running).
                            instance["config_update_triggered"] = True
                            instance["change_fields"] = change_fields
                        else:
                            _log(_CONFIG["db_path"], inst_id, "preset_change", "failed", detail={"error": result.get("message", "")})
                    except Exception as exc:
                        try:
                            _log(_CONFIG["db_path"], inst_id, "preset_change", "exception", detail={"error": str(exc)})
                            _ts(_CONFIG["db_path"], inst_id, "running")
                        except Exception as _e:
                            logger.debug("api_update_instance inst=%d: reconfigure chain exception handler: %s", inst_id, _e)
                    finally:
                        lock.release()
            else:
                # Standard flow: stopped/unconfigured/error/deployed → redeploy
                was_running = current_state == QR_STATE_RUNNING
                if current_state in (QR_STATE_RUNNING, QR_STATE_STOPPED, QR_STATE_UNCONFIGURED, QR_STATE_ERROR, QR_STATE_DEPLOYED):
                    try:
                        # Step 1: Stop if running, verify stopped
                        if was_running:
                            try:
                                _ts(_CONFIG["db_path"], inst_id, "stopping")
                                _log(_CONFIG["db_path"], inst_id, QR_JOB_STOP, "received")
                            except Exception as _e:
                                logger.debug("api_update_instance inst=%d: stopping state transition before deploy: %s", inst_id, _e)

                            stop_result = _run_manage_action(inst_id, engine_type_name, node_id, "stop")
                            if stop_result.get("success"):
                                _log(_CONFIG["db_path"], inst_id, "stop", "success", detail={"remote": stop_result})
                            else:
                                _log(_CONFIG["db_path"], inst_id, "stop", "failed", detail={"remote": stop_result})

                            _wait_for_stop_status(_CONFIG["db_path"], inst_id, max_wait=30)

                        # Step 2: Deploy with new config
                        deploy_result = deploy_instance(_CONFIG["db_path"], inst_id, skip_build=body.get("skip_build", False))
                        instance["deploy_result"] = deploy_result
                        instance["deploy_triggered"] = True
                        instance["change_fields"] = change_fields

                        # Step 3: Restart if was previously running
                        if was_running and engine_type_name in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME, QR_ENGINE_IPERF3_NAME):
                            try:
                                _ts(_CONFIG["db_path"], inst_id, "starting")
                                _log(_CONFIG["db_path"], inst_id, "start", "received")

                                remote_result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
                                if remote_result.get("success"):
                                    _ts(_CONFIG["db_path"], inst_id, "running")
                                    _log(_CONFIG["db_path"], inst_id, "start", "success", detail={"remote": remote_result})
                                else:
                                    _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"remote": remote_result})
                                    _ts(_CONFIG["db_path"], inst_id, "error")
                            except Exception as exc:
                                _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"error": str(exc)})
                    except Exception as exc:
                        instance["deploy_result"] = {"success": False, "message": str(exc)}
                        instance["deploy_triggered"] = True

    return success_single(instance)


def api_delete_instance(inst_id):
    """Delete an instance with remote undeploy and verification via RUNNER-1 chain.

    Before deleting from DB, runs the engine-specific undeploy chain (stop →
    engine-undeploy → verify) on the target node. Only proceeds with DB
    deletion after the chain succeeds. Shared build cleanup runs post-undeploy
    when this is the last llama.cpp instance on the node.
    """
    from db.adapters.instances import delete_instance, log_action, get_instance as _gi, \
        check_system_managed as _csm

    # Check if system-managed before deleting
    if _csm(_CONFIG["db_path"], inst_id):
        return error_response("SYSTEM_MANAGED_INSTANCE",
                                f"Instance {inst_id} is a system-managed engine and cannot be deleted. "
                                "Use the engine config page to modify settings, or restart/undeploy via that page.", 409)

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # Extract values early (needed for logging and undeploy logic)
    engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME)
    node_id = inst.get("node_id")

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    # Log override — deleting instance bypasses normal undeploy-first workflow
    # Use log_qr_task (not log_qr_override) so qr_actions entry self-cleans after delete
    from lib.lib_qr_actions import log_qr_task as _lqt, update_qr_task as _uqt

    _delete_task_id = _lqt(_CONFIG["db_path"], "instance_delete_override",
                           node_id=node_id, instance_id=inst_id, actor="api",
                           extra_details={"instance_name": inst.get("name"),
                                          "state": inst.get("state"),
                                          "engine": engine_type_name})

    # Run engine-specific undeploy chain via RUNNER-1 (if deployed with a node)
    chain_result = {"success": True, "message": "skipped"}
    if node_id is not None and inst.get("state") not in (QR_STATE_UNCONFIGURED,):
        from lib.lib_runner import PlaybookRunner
        runner = PlaybookRunner(_CONFIG["db_path"])
        chain_result = runner.chain(inst_id, job_type="undeploy", actor="api")

    ud_success = chain_result.get("success", False) if chain_result else True
    # If no node or unconfigured state, undeploy was skipped — consider success
    if node_id is None or inst.get("state") == QR_STATE_UNCONFIGURED:
        ud_success = True

    # DESIGN-5: Atomic delete — only remove from DB if remote undeploy succeeded.
    # On failure, transition to error state so user can investigate stale remote files.
    if ud_success:
        # Delete from DB after successful undeploy and pre-delete logging
        deleted = delete_instance(_CONFIG["db_path"], inst_id)
        if not deleted:
            return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

        # Post-delete log may fail FK (instance already gone) — non-critical
        try:
            log_action(_CONFIG["db_path"], inst_id, "undeploy", "success",
                        detail={"remote_undeploy": True, "deleted": True})
        except Exception as _e:
            logger.debug("api_delete_instance inst=%d: post-delete logging failed (FK may be gone): %s", inst_id, _e)

        # Check if shared build should be cleaned up (last llama_server/llama_rpc on node)
        cleanup_done = None
        if engine_type_name in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME) and _get_keep_shared_build():
            try:
                from db.adapters.instances import list_instances as _list_all
                remaining = [i for i in _list_all(_CONFIG["db_path"], node_id=node_id)
                                if i.get("engine_type_name") in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME)
                                and i.get("state") not in (QR_STATE_UNCONFIGURED,)]
                if len(remaining) == 0:
                    # Last instance on this node — trigger shared build cleanup
                    nd = _gi(_CONFIG["db_path"], inst_id) or {}
                    hostname = (nd.get("node_hostname") or
                                nd.get("ipv4_address", "") or "")
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
                        log_action(_CONFIG["db_path"], inst_id, "state_transition",
                                "success" if cleanup_done else "failed",
                                detail={"node_id": node_id, "hostname": hostname})
            except Exception as exc:
                cleanup_done = False
                log_action(_CONFIG["db_path"], inst_id, "state_transition",
                            "failed", detail={"error": str(exc)})
        # Mark override task completed after successful delete
        if _delete_task_id:
            _uqt(_CONFIG["db_path"], _delete_task_id, "completed")
        return success_single({"instance_id": inst_id, "deleted": True,
                                "remote_undeploy": True})
    else:
        # Undeploy failed — transition to error state with note about stale files.
        # Instance stays in DB so user can retry delete or investigate.
        if _delete_task_id:
            _uqt(_CONFIG["db_path"], _delete_task_id, "failed")
        try:
            from db.adapters.instances import update_instance as _ui
            chain_err = chain_result.get("message", "unknown error")
            _ui(_CONFIG["db_path"], inst_id,
                state="error",
                state_reason=f"Remote undeploy failed: {chain_err}. Files may remain on remote node. Delete instance to retry.")
            log_action(_CONFIG["db_path"], inst_id, "undeploy", "undeploy",
                        detail={"remote_undeploy": False, "error": chain_err})
        except Exception as exc:
            log_action(_CONFIG["db_path"], inst_id, "undeploy", "failed",
                        detail={"remote_undeploy": False, "error": str(exc)})
        return error_response("UNDEPLOY_FAILED",
                               f"Remote undeploy failed for instance {inst_id}. "
                               "Instance kept in DB with error state. Files may remain on remote node.", 409)


