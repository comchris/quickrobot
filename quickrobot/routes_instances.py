"""Instance route handlers for quickrobot.

All functions accept the same signatures as the originals from quickrobot.py.
Route registration is handled by __init__.py via app.add_url_rule().
"""

import json
import os
from flask import request, jsonify, Response
from quickrobot.lib_responses import success_single, success_list, error_response, require_json
from quickrobot import _CONFIG, _START_TIME, _project_root
from lib.qr_engine_ids import (
    QR_ENGINE_API_NAME, QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC,
    QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_MCP_NAME, QR_ENGINE_SUBPROCESS, QR_ENGINE_UNIVERSAL,
    QR_ENGINE_WEBUI_NAME,
    get_id_by_name, is_llamacpp_engine, is_system_engine,
)
from lib.lib_constants import DEFAULT_ANSIBLE_USER
from quickrobot.lib_instances import (
     deploy_instance, _execute_playbook, check_remote_uuids,
     _start_async_build, _run_manage_action, _NODE_BUILD_LOCK,
     _resolve_engine_playbook_id, _get_keep_shared_build, _get_deploy_lock,
     GRACE_PERIOD_RUNNING, _wait_for_stop_status, _track_playbook_error,
     _start_system_managed, _stop_system_managed, _restart_system_managed,
     _check_node_active,
   )
from lib.lib_qr_actions import log_qr_action, log_qr_override
from quickrobot.lib_nodes import _get_node_build_state
from db.sqlite import pool as db_pool


def api_create_instance():
    """Create a new engine instance."""
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
                import json as _json_cap
                cap = _json_cap.loads(cap)
            except Exception:
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
        except Exception:
            pass  # Will default below

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
    except Exception:
        pass  # Port allocation is best-effort in Phase 1

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
    except Exception:
        pass  # Config merge is best-effort

    # Auto-deploy if enabled and deploy_requested flag not explicitly false
    auto_deploy = _CONFIG.get("create_and_autodeploy", True)
    deploy_flag = body.get("deploy", True)
    do_deploy = auto_deploy and (isinstance(deploy_flag, bool) and deploy_flag or str(deploy_flag).lower() != "false")
    # Cleanup orphaned records on create failure (QUICKROBOT_CLEANUP_ON_CREATE_FAIL)
    _qr_env = _CONFIG.get("qr_env_config", {})
    cleanup_fail = _qr_env.get("QUICKROBOT_CLEANUP_ON_CREATE_FAIL", "true").lower() == "true"
    if do_deploy:
        try:
            # Sync preflight only (git clone/pull), then start async build
            result = deploy_instance(_CONFIG["db_path"], instance["id"], async_mode=True, skip_build=_skip_build)
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

            # Start async build thread
            _start_async_build(_CONFIG["db_path"], instance["id"])
        except Exception as exc:
            # Best-effort auto-deploy — keep instance for async build tracking
            pass

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
                except Exception:
                    pass  # If we can't check the node, include the instance
            filtered.append(inst)
        instances = filtered
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
                inst["node_hostname"] = _CONFIG["host"]
            else:
                # WebUI/MCP: read own host from .quickrobot.env (not API bind address)
                try:
                    from lib.lib_system_engine import load_env_config as _lec
                    env_cfg = _lec(os.getcwd())
                    if engine_type_name == QR_ENGINE_WEBUI_NAME:
                        inst["node_hostname"] = env_cfg["QUICKROBOT_WEBUI_HOST"]
                    elif engine_type_name == QR_ENGINE_MCP_NAME:
                        inst["node_hostname"] = env_cfg["QUICKROBOT_MCP_HOST"]
                    else:
                        inst["node_hostname"] = _CONFIG["host"]
                except FileNotFoundError:
                    inst["node_hostname"] = _CONFIG["host"]
            # Set port_assigned and config_override for Remote column display
            if engine_type_name == QR_ENGINE_API_NAME:
                inst["port_assigned"] = _CONFIG["api_port"]
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
                except Exception:
                    inst["mcp_allow_reads"] = True
                    inst["mcp_allow_writes"] = True
                    inst["mcp_allow_proxy"] = True
            # Process uptime computed by shared helper (called after loop below)
            if engine_type_name == QR_ENGINE_MCP_NAME:
                try:
                    from db.adapters.configs import get_engine_config as _gec
                    et_id = inst.get("engine_type_id")
                    open("/tmp/mcp_debug.log", "a").write(f"mcp check: engine={engine_type_name} et_id={et_id}\n")
                    if et_id:
                        rr = _gec(_CONFIG["db_path"], et_id, "mcp_allow_reads") or {}
                        wr = _gec(_CONFIG["db_path"], et_id, "mcp_allow_writes") or {}
                        pr = _gec(_CONFIG["db_path"], et_id, "mcp_allow_proxy") or {}
                        open("/tmp/mcp_debug.log", "a").write(f"  _gec: reads={rr.get('value')} writes={wr.get('value')} proxy={pr.get('value')}\n")
                        inst["mcp_allow_reads"] = str(rr.get("value", "true")).lower() in ("true", "1", "yes")
                        inst["mcp_allow_writes"] = str(wr.get("value", "true")).lower() in ("true", "1", "yes")
                        inst["mcp_allow_proxy"] = str(pr.get("value", "true")).lower() in ("true", "1", "yes")
                        open("/tmp/mcp_debug.log", "a").write(f"  set: reads={inst['mcp_allow_reads']} writes={inst['mcp_allow_writes']} proxy={inst['mcp_allow_proxy']}\n")
                except Exception as _exc:
                    open("/tmp/mcp_debug.log", "a").write(f"  exception: {_exc}\n")
                    inst["mcp_allow_reads"] = True
                    inst["mcp_allow_writes"] = True
                    inst["mcp_allow_proxy"] = True
                # MCP availability: check engine status for interpreter/package info
                try:
                    from engine import get_engine as _ge
                    mcp_eng = _ge("quickrobot-mcp")
                    if mcp_eng:
                        _st = mcp_eng.get_status(inst["id"], _CONFIG["db_path"])
                        inst["_mcp_available"] = bool(_st.get("mcp_available", False))
                except Exception:
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
    from quickrobot.lib_instances import override_system_instance_states as _osis
    _osis(instances, _CONFIG)
    # Compute process uptime for subprocess instances (similar to system-managed engines)
    import time as _time; now_ts = _time.time()
    for inst in instances:
        if inst.get("engine_type_name") == "subprocess":
            try:
                import psutil as _psutil
                pid = inst.get("pid_last_known")
                if pid and isinstance(pid, int):
                    p = _psutil.Process(pid)
                    if p.is_running():
                        inst["process_age_seconds"] = int(now_ts - p.create_time())
            except Exception:
                pass
    # Debug: show engine_type_name for all instances
    for i in instances:
        if i.get('id') == 3:
            print(f"[qr] DEBUG api_list_instances instance 3: engine_type_name={i.get('engine_type_name')} mcp_keys={[k for k in i.keys() if 'mcp' in k.lower()]}")
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
    is_cluster = engine_type_name in ("llama_server", "llama_rpc")
    merged = {}
    if is_cluster:
        try:
            from lib.lib_cluster_env_builder import build_llama_server_env, build_rpc_server_env
            if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
                cluster_result = build_llama_server_env(_CONFIG["db_path"], inst_id)
            elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                cluster_result = build_rpc_server_env(_CONFIG["db_path"], inst_id)
            merged = {"env": cluster_result["env"], "cli_opts": [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else [], "model": {}}
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
    except Exception:
        instance["active_overrides"] = {}

    # System-managed instances: use config_override.host (LAN IP) instead of "localhost"
    if _csm(_CONFIG["db_path"], inst_id):
        engine_type_name = instance.get("engine_type_name", "")
        co_raw = instance.get("config_override") or {}
        if isinstance(co_raw, str):
            try:
                import json as _jc2
                co_raw = _jc2.loads(co_raw)
            except Exception:
                co_raw = {}
        if isinstance(co_raw, dict):
            lan_host = co_raw.get("host", "")
            if lan_host and lan_host != "0.0.0.0":
                instance["node_hostname"] = lan_host
            elif engine_type_name == QR_ENGINE_API_NAME:
                # quickrobot-api IS the API — always use the configured host
                instance["node_hostname"] = _CONFIG["host"]
            else:
                # Read from .quickrobot.env for WebUI/MCP (not API bind address)
                try:
                    from lib.lib_system_engine import load_env_config as _lec
                    env_cfg = _lec(os.getcwd())
                    if engine_type_name == QR_ENGINE_WEBUI_NAME:
                        instance["node_hostname"] = env_cfg["QUICKROBOT_WEBUI_HOST"]
                    elif engine_type_name == QR_ENGINE_MCP_NAME:
                        instance["node_hostname"] = env_cfg["QUICKROBOT_MCP_HOST"]
                    else:
                        instance["node_hostname"] = _CONFIG["host"]
                except FileNotFoundError:
                    instance["node_hostname"] = _CONFIG["host"]
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
    deploy_needed = False
    change_fields = []
    old_config = existing.get("config_override", {}) or {}
    old_preset = existing.get("preset_id")
    old_port = existing.get("port_assigned")

    # Merge incoming config_override with existing (partial PUT semantics)
    # Empty string "" means "delete key" — supports clearing fields like LLAMA_ARG_DEVICE
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
        elif isinstance(co_in, str):
            try:
                import json as _json
                co_in = _json.loads(co_in)
                if isinstance(co_in, dict):
                    for k, v in co_in.items():
                        if v == "":
                            new_override.pop(k, None)
                        else:
                            new_override[k] = v
            except Exception:
                pass
    new_preset = body.get("preset_id", old_preset)
    new_port = body.get("port_override", body.get("port_assigned", old_port))

    if new_override != old_config:
        deploy_needed = True
        change_fields.append("config_override")
    if new_preset != old_preset:
        deploy_needed = True
        change_fields.append("preset_id")
    if isinstance(new_port, int) and new_port > 0 and new_port != old_port:
        deploy_needed = True
        change_fields.append("port")

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

    # Re-merge config after update
    try:
        from db.adapters.instances import merge_configs
        merged = merge_configs(_CONFIG["db_path"], inst_id)
        _ui(_CONFIG["db_path"], inst_id, ansible_vars=merged)
        instance["merged_config"] = merged
    except Exception:
        pass

    # Trigger redeploy with proper state lifecycle if config changed
    if deploy_needed:
        from db.adapters.instances import get_instance as _gi, transition_state as _ts, log_action as _log
        inst = _gi(_CONFIG["db_path"], inst_id)
        engine_type_name = inst.get("engine_type_name", "llama_rpc") if inst else "llama_rpc"
        node_id = inst.get("node_id") if inst else None
        current_state = instance.get("state", "")

        if engine_type_name in ("llama_server", "llama_rpc", "iperf3"):
            # BC-1: Config-only update for running/stopped instances (no stop/restart needed)
            # Uses UPDATE_CONFIG_V1 playbook — writes env file + systemd unit, reloads daemon, restarts service.
            # No git clone/pull, no cmake build. Works identically regardless of running or stopped state.
            if current_state in ("running", "stopped") and deploy_needed:
                lock = _get_deploy_lock(inst_id)
                if not lock.acquire(blocking=False):
                    return error_response("BUSY", f"Config update already in progress for instance {inst_id}")
                try:
                    try:
                        _ts(_CONFIG["db_path"], inst_id, "updating")
                        _log(_CONFIG["db_path"], inst_id, "preset_change", "received")
                    except Exception:
                        pass

                    # Build cluster-aware config via Python builder
                    from lib.lib_cluster_env_builder import build_llama_server_env, build_rpc_server_env
                    if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
                        cluster_result = build_llama_server_env(_CONFIG["db_path"], inst_id)
                    elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                        cluster_result = build_rpc_server_env(_CONFIG["db_path"], inst_id)
                    else:
                        cluster_result = {"env": {}, "cli_args": ""}

                    try:
                        from db.adapters.nodes import get_node as _gn
                        nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
                        hostname = (nd.get("hostname") or nd.get("name")) if nd else "localhost"
                    except Exception:
                        hostname = "localhost"

                    extra_vars = {
                        "inventory_host": hostname,
                        "instance_id": inst["id"],
                        "instance_name": inst.get("name", ""),
                        "engine_type": engine_type_name,
                        "instance_port": inst.get("port_assigned", 0),
                        "binary_path": cluster_result["env"].get("binary_path", "") if isinstance(cluster_result.get("env"), dict) else "",
                        "remote_node_user": (nd.get("ansible_user") if nd else None) or DEFAULT_ANSIBLE_USER,
                        "merged_env": cluster_result["env"] if isinstance(cluster_result.get("env"), dict) else {},
                        "merged_cli_opts": [s for s in cluster_result["cli_args"].split()] if isinstance(cluster_result.get("cli_args"), str) and cluster_result["cli_args"] else [],
                        "tensor_split_value": cluster_result.get("tensor_split_str", ""),
                        "split_mode_value": cluster_result.get("split_mode", "layer"),
                        "rpc_bind_ids": json.loads(inst.get("rpc_bind_ids") or "[]") if isinstance(inst.get("rpc_bind_ids"), str) else (inst.get("rpc_bind_ids") or []),
                        "instance_uuid": inst.get("instance_uuid", ""),
                    }

                    r = _execute_playbook("UPDATE_CONFIG_V1", resolver_type="playbook_id",
                                            limit=hostname, extra_vars=extra_vars, timeout=300,
                                            action_type="config_change", node_id=node_id, instance_id=inst_id)
                    if r["error"]:
                        _ts(_CONFIG["db_path"], inst_id, "build_error")
                        _log(_CONFIG["db_path"], inst_id, "preset_change", "update_failed", detail={"error": r["error"]})
                    elif r.get("failed"):
                        _ts(_CONFIG["db_path"], inst_id, "deployed")
                        _log(_CONFIG["db_path"], inst_id, "preset_change", "update_success")
                        # Restart service via manage_instance
                        try:
                            _ts(_CONFIG["db_path"], inst_id, "starting")
                            _log(_CONFIG["db_path"], inst_id, "start", "received")
                            start_result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
                            if start_result.get("success"):
                                _ts(_CONFIG["db_path"], inst_id, "running")
                                _log(_CONFIG["db_path"], inst_id, "start", "success")
                            else:
                                _ts(_CONFIG["db_path"], inst_id, "error")
                                _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"rpc": start_result})
                        except Exception as exc:
                            _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"error": str(exc)})
                            # Ensure we don't leave instance stuck in "starting" — transition to error
                            try:
                                _ts(_CONFIG["db_path"], inst_id, "error")
                            except Exception:
                                pass
                    else:
                        # Config update succeeded — no failure, transition back to running
                        _log(_CONFIG["db_path"], inst_id, "preset_change", "update_success")
                        _ts(_CONFIG["db_path"], inst_id, "running")

                    instance["deploy_triggered"] = True
                    instance["change_fields"] = change_fields
                except Exception as exc:
                    # Catch-all: any unhandled exception in BC-1 block — recover to running
                    try:
                        _log(_CONFIG["db_path"], inst_id, "preset_change", "update_exception", detail={"error": str(exc)})
                        _ts(_CONFIG["db_path"], inst_id, "running")
                        _log(_CONFIG["db_path"], inst_id, "preset_change", "recovered_to_running")
                    except Exception:
                        pass
                finally:
                    lock.release()
        else:
            # Standard flow: stopped/unconfigured/error/deployed → redeploy
            was_running = current_state == "running"
            if current_state in ("running", "stopped", "unconfigured", "error", "deployed"):
                try:
                    # Step 1: Stop if running, verify stopped
                    if was_running:
                        try:
                            _ts(_CONFIG["db_path"], inst_id, "stopping")
                            _log(_CONFIG["db_path"], inst_id, "stop", "received")
                        except Exception:
                            pass

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
                    if was_running and engine_type_name in ("llama_server", "llama_rpc", "iperf3"):
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
    """Delete an instance with remote undeploy and verification.

    Before deleting from DB, runs the engine-specific undeploy playbook on the
    target node to stop the service, remove unit files and env files, and
    reload systemd. Verifies success by checking no qr-<id>* units remain
    on the remote host. Only proceeds with DB deletion after undeploy succeeds.
    """
    from db.adapters.instances import delete_instance, log_action, get_instance as _gi, \
        check_system_managed as _csm
    import os as _os

    # Check if system-managed before deleting
    if _csm(_CONFIG["db_path"], inst_id):
        return error_response("SYSTEM_MANAGED_INSTANCE",
                                f"Instance {inst_id} is a system-managed engine and cannot be deleted. "
                                "Use the engine config page to modify settings, or restart/undeploy via that page.", 409)

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # Extract values early (needed for override logging and undeploy logic)
    engine_type_name = inst.get("engine_type_name", "llama_rpc")
    node_id = inst.get("node_id")

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    # Log override — deleting instance bypasses normal undeploy-first workflow
    log_qr_override(_CONFIG["db_path"], "instance_delete_override",
                        node_id=node_id, instance_id=inst_id, actor="api",
                        details={"instance_name": inst.get("name"),
                                "state": inst.get("state"),
                                "engine": engine_type_name})

    # Undeploy from remote node before DB deletion (if deployed)
    undeploy_result = None
    if node_id is not None and inst.get("state") not in ("unconfigured",):
        from db.adapters.nodes import get_node as _gn

        nd = _gn(_CONFIG["db_path"], node_id)
        hostname = (nd.get("ansible_inventory_host") or
                    nd.get("hostname") or
                    nd.get("name")) if nd else None
        if hostname:
            # Dynamic inventory — no file generated (DI-7)
            play_name = f"undeploy_{engine_type_name}.yml"
            # Resolve from registry first via stable ID, fallback to dynamic path
            pb_id = _resolve_engine_playbook_id("undeploy", engine_type_name)
            if pb_id:
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
            else:
                play_path = os.path.join(_project_root, "playbooks", play_name)
                if _os.path.exists(play_path):
                    try:
                        r = _execute_playbook(play_path, resolver_type="file_path",
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
                    except Exception as exc:
                        undeploy_result = {"failed": True, "error": str(exc)}
                    if undeploy_result and not undeploy_result.get("failed", False):
                        _track_playbook_error(play_name)

    # Verify undeploy removed all files on remote node (non-critical)
    if not (undeploy_result.get("failed", False) if undeploy_result else True) and hostname:
        try:
            r = _execute_playbook("CHECK_UNDEPLOY_V1", resolver_type="playbook_id",
                                    limit=hostname,
                                    extra_vars={
                                        "inventory_host": hostname,
                                        "instance_id": inst_id,
                                        "engine_type": engine_type_name,
                                    },
                                    action_type="undeploy_instance")
        except Exception:
            pass  # Non-critical — proceed with DB deletion regardless

    # Log the delete action BEFORE removing from DB (FK constraint on instance_logs)
    ud_success = not (undeploy_result.get("failed", False) if undeploy_result else True)

    # Delete from DB after undeploy verification and pre-delete logging
    deleted = delete_instance(_CONFIG["db_path"], inst_id)
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # Post-delete log may fail FK (instance already gone) — non-critical
    try:
        log_action(_CONFIG["db_path"], inst_id, "delete", "success",
                    detail={"remote_undeploy": ud_success})
    except Exception:
        pass

    # Check if shared build should be cleaned up (last llama_server/llama_rpc on node)
    cleanup_done = None
    if engine_type_name in ("llama_server", "llama_rpc") and _get_keep_shared_build():
        try:
            from db.adapters.instances import list_instances as _list_all
            remaining = [i for i in _list_all(_CONFIG["db_path"], node_id=node_id)
                            if i.get("engine_type_name") in ("llama_server", "llama_rpc")
                            and i.get("state") not in ("unconfigured",)]
            if len(remaining) == 0:
                # Last instance on this node — trigger shared build cleanup
                if hostname:
                    r = _execute_playbook("CLEAN_SHARED_LLAMACPP_BUILD_V1", resolver_type="playbook_id",
                                            limit=hostname,
                                            extra_vars={
                                                "inventory_host": hostname,
                                                "engine_type": engine_type_name,
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
    return success_single({"instance_id": inst_id, "deleted": True,
                            "remote_undeploy": ud_success})


def api_bind_rpc(inst_id):
    """Bind RPC instances to a llama-server instance.

    Updates rpc_bind_ids, recomputes tensor_split from resolved RPC split values.
    If instance is running: rejects with INVALID_STATE (must stop first — RPC bindings
    require config reload). If deployed/stopped: uses update_config.yml for config-only
    update + service restart.

    Args:
        inst_id: Integer primary key of the llama-server instance.

    Returns:
        JSON with updated instance data and bind result.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui, \
        transition_state as _ts, log_action as _log
    from lib.lib_ansible_runner import run_playbook

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
        return error_response("INVALID_ENGINE", "bind-rpc only works for llama_server instances")

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    rpc_ids = body.get("rpc_ids", [])
    split_mode = body.get("split_mode", inst.get("split_mode") or "layer")

    # Validate all RPC IDs exist and are rpc engine type
    for rid in rpc_ids:
        ri = _gi(_CONFIG["db_path"], int(rid))
        if ri is None:
            return error_response("RESOURCE_NOT_FOUND", f"RPC instance {rid} not found")
        if ri.get("engine_type_name") != QR_ENGINE_LLAMA_RPC_NAME:
            return error_response("INVALID_ENGINE", f"Instance {rid} is {ri.get('engine_type_name')}, not llama_rpc")

    # Update rpc_bind_ids and split_mode
    try:
        _ui(_CONFIG["db_path"], inst_id,
            rpc_bind_ids=json.dumps(rpc_ids),
            split_mode=split_mode)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update bindings: {exc}")

    # Re-deploy if instance is deployed/stopped (skip_build since binding only changes env vars)
    # If running: reject — must stop first for RPC binding changes
    current_state = inst.get("state", "")
    result = {"instance_id": inst_id, "bound_rpc_ids": rpc_ids,
                "split_mode": split_mode}

    if current_state == "running":
        return error_response("INVALID_STATE", "Must stop instance first to change RPC bindings")

    if current_state in ("deployed", "stopped"):
        lock = _get_deploy_lock(inst_id)
        if not lock.acquire(blocking=False):
            return error_response("BUSY", f"Config update already in progress for instance {inst_id}")
        try:
            from db.adapters.instances import get_instance as _gi2
            inst_after = _gi2(_CONFIG["db_path"], inst_id)
            # Transition to updating (BC-1: valid from deployed/stopped)
            try:
                _ts(_CONFIG["db_path"], inst_id, "updating")
                _log(_CONFIG["db_path"], inst_id, "bind_rpc", "received")
            except Exception:
                pass

           # Build extra_vars for config-only update (cluster-aware)
            from lib.lib_cluster_env_builder import build_llama_server_env as _cls2
            cluster_result = _cls2(_CONFIG["db_path"], inst_id)

            try:
                from db.adapters.nodes import get_node as _gn
                nd = _gn(_CONFIG["db_path"], inst.get("node_id"))
                hostname = (nd.get("hostname") or nd.get("name")) if nd else "localhost"
            except Exception:
                hostname = "localhost"

            extra_vars = {
                "inventory_host": hostname,
                "instance_id": inst_after["id"],
                "instance_name": inst_after.get("name", ""),
                "engine_type": "llama_server",
                "instance_port": inst_after.get("port_assigned", 0),
                "binary_path": cluster_result["env"].get("binary_path", ""),
                "remote_node_user": (nd.get("ansible_user") if nd else None) or DEFAULT_ANSIBLE_USER,
                "merged_env": cluster_result["env"],
                "merged_cli_opts": [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else [],
                "tensor_split_value": cluster_result.get("tensor_split_str"),
                "split_mode_value": cluster_result.get("split_mode"),
                "rpc_bind_ids": rpc_ids,
                "instance_uuid": inst_after.get("instance_uuid", ""),
            }

            r = _execute_playbook("UPDATE_CONFIG_V1", resolver_type="playbook_id",
                                    limit=hostname, extra_vars=extra_vars, timeout=300,
                                    action_type="config_change")
            if r["error"]:
                _ts(_CONFIG["db_path"], inst_id, "build_error")
                _log(_CONFIG["db_path"], inst_id, "bind_rpc", "update_failed", detail={"error": r["error"]})
                result["update_success"] = False
            elif r.get("failed"):
                _ts(_CONFIG["db_path"], inst_id, "deployed")
                _log(_CONFIG["db_path"], inst_id, "bind_rpc", "update_success")
                # Restart service via manage_instance playbook
                try:
                    _ts(_CONFIG["db_path"], inst_id, "starting")
                    _log(_CONFIG["db_path"], inst_id, "start", "received")
                    start_result = _run_manage_action(inst_id, "llama_server", inst.get("node_id"), "start")
                    if start_result.get("success"):
                        _ts(_CONFIG["db_path"], inst_id, "running")
                        _log(_CONFIG["db_path"], inst_id, "start", "success")
                        result["update_success"] = True
                    else:
                        _ts(_CONFIG["db_path"], inst_id, "error")
                        _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"rpc": start_result})
                        result["update_success"] = False
                except Exception as exc:
                    _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"error": str(exc)})
                    result["update_success"] = False

            result["redeploy_triggered"] = True
        except Exception as exc:
            result["update_error"] = str(exc)
            result["redeploy_triggered"] = True
        finally:
            lock.release()

    return success_single(result)


def api_unbind_rpc(inst_id, rpc_id):
    """Remove a single RPC binding from a llama-server instance.

    Args:
        inst_id: Integer primary key of the llama-server instance.
        rpc_id: Integer primary key of the RPC instance to unbind.

    Returns:
        JSON with updated bindings list and result.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
        return error_response("INVALID_ENGINE", "unbind-rpc only works for llama_server instances")

    try:
        raw = inst.get("rpc_bind_ids") or "[]"
        current_ids = json.loads(raw) if isinstance(raw, str) else list(raw)
        if rpc_id not in current_ids:
            return error_response("NOT_FOUND", f"RPC instance {rpc_id} not in bindings")
        current_ids.remove(rpc_id)

        _ui(_CONFIG["db_path"], inst_id, rpc_bind_ids=json.dumps(current_ids))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update bindings: {exc}")

    # Re-deploy if instance is deployed/stopped (skip_build since binding only changes env vars)
    current_state = inst.get("state", "")
    result = {"instance_id": inst_id, "remaining_rpc_ids": current_ids}

    if current_state == "running":
        return error_response("INVALID_STATE", "Must stop instance first to unbind RPC instances")

    if current_state in ("deployed", "stopped"):
        lock = _get_deploy_lock(inst_id)
        if not lock.acquire(blocking=False):
            return error_response("BUSY", f"Config update already in progress for instance {inst_id}")
        try:
            from db.adapters.instances import get_instance as _gi2, transition_state as _ts, log_action as _log
            inst_after = _gi2(_CONFIG["db_path"], inst_id)

            try:
                _ts(_CONFIG["db_path"], inst_id, "updating")
                _log(_CONFIG["db_path"], inst_id, "unbind_rpc", "received")
            except Exception:
                pass

            from lib.lib_cluster_env_builder import build_llama_server_env as _cls2
            cluster_result = _cls2(_CONFIG["db_path"], inst_id)

            try:
                from db.adapters.nodes import get_node as _gn
                nd = _gn(_CONFIG["db_path"], inst.get("node_id"))
                hostname = (nd.get("hostname") or nd.get("name")) if nd else "localhost"
            except Exception:
                hostname = "localhost"

            extra_vars = {
                "inventory_host": hostname,
                "instance_id": inst_after["id"],
                "instance_name": inst_after.get("name", ""),
                "engine_type": "llama_server",
                "instance_port": inst_after.get("port_assigned", 0),
                "binary_path": cluster_result["env"].get("binary_path", ""),
                "remote_node_user": (nd.get("ansible_user") if nd else None) or DEFAULT_ANSIBLE_USER,
                "merged_env": cluster_result["env"],
                "merged_cli_opts": [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else [],
                "tensor_split_value": cluster_result["tensor_split_str"],
                "split_mode_value": cluster_result["split_mode"],
                "rpc_bind_ids": current_ids,
                "instance_uuid": inst_after.get("instance_uuid", ""),
            }

            r = _execute_playbook("UPDATE_CONFIG_V1", resolver_type="playbook_id",
                                    limit=hostname, extra_vars=extra_vars, timeout=300,
                                    action_type="config_change")
            if r["error"]:
                _ts(_CONFIG["db_path"], inst_id, "build_error")
                _log(_CONFIG["db_path"], inst_id, "unbind_rpc", "update_failed", detail={"error": r["error"]})
                result["update_success"] = False
            elif r.get("failed"):
                _ts(_CONFIG["db_path"], inst_id, "deployed")
                _log(_CONFIG["db_path"], inst_id, "unbind_rpc", "update_success")
                try:
                    _ts(_CONFIG["db_path"], inst_id, "starting")
                    _log(_CONFIG["db_path"], inst_id, "start", "received")
                    start_result = _run_manage_action(inst_id, "llama_server", inst.get("node_id"), "start")
                    if start_result.get("success"):
                        _ts(_CONFIG["db_path"], inst_id, "running")
                        _log(_CONFIG["db_path"], inst_id, "start", "success")
                        result["update_success"] = True
                    else:
                        _ts(_CONFIG["db_path"], inst_id, "error")
                        _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"rpc": start_result})
                        result["update_success"] = False
                except Exception as exc:
                    _log(_CONFIG["db_path"], inst_id, "start", "failed", detail={"error": str(exc)})
                    result["update_success"] = False

            result["redeploy_triggered"] = True
        except Exception as exc:
            result["update_error"] = str(exc)
            result["redeploy_triggered"] = True
        finally:
            lock.release()

    return success_single(result)


def api_list_rpc_bindings():
    """List all RPC instances bound to a specific llama-server.

    Query param: llama_id — the llama-server instance ID.
    Returns: list of RPC instance metadata (id, name, hostname, port, split).
    """
    llama_id = request.args.get("llama_id")
    if not llama_id:
        return error_response("VALIDATION_ERROR", "llama_id query param required")

    from db.adapters.instances import get_instance as _gi, list_instances as _list_all
    from db.sqlite import pool

    # Get the llama-server instance
    llama_inst = _gi(_CONFIG["db_path"], int(llama_id))
    if not llama_inst:
        return error_response("RESOURCE_NOT_FOUND", f"Llama-server {llama_id} not found")

    try:
        raw = llama_inst.get("rpc_bind_ids") or "[]"
        bind_ids = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (json.JSONDecodeError, TypeError):
        bind_ids = []

    bindings = []
    for rid in bind_ids:
        ri = _gi(_CONFIG["db_path"], int(rid))
        if ri:
            bindings.append({
                "id": ri["id"],
                "name": ri["name"],
                "node_hostname": ri.get("node_hostname") or "",
                "port_assigned": ri.get("port_assigned"),
                "split": ri.get("split") or 0,
                "state": ri.get("state"),
            })

    return success_single({"llama_id": llama_id, "bindings": bindings})


def api_cluster_bind(inst_id):
    """Bind an RPC instance to a llama-server (or unbind).

    Sets rpc_bind_ids to [llama_id] for the target llama-server.
    This is a 1:1 convenience endpoint — the underlying DB supports N:1.

    Args:
        inst_id: Integer primary key of the RPC instance.

    Returns:
        JSON with bind result.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_RPC_NAME:
        return error_response("INVALID_ENGINE", "cluster-bind only works for llama_rpc instances")

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    llama_id = body.get("llama_id")  # null or 0 = unbind
    ls = None  # Will be set below
    is_bind = llama_id is not None  # True = bind, False = unbind
    if is_bind:
        try:
            llama_id = int(llama_id)
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "llama_id must be integer or null")
        # Validate the llama-server exists and is llama_server type
        ls = _gi(_CONFIG["db_path"], llama_id)
        if not ls or ls.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
            return error_response("RESOURCE_NOT_FOUND", f"Llama-server {llama_id} not found")
    else:
        # Unbind: find which llama-server this RPC is bound to
        from db.adapters.instances import list_instances as _list_all
        all_ls = _list_all(_CONFIG["db_path"], engine_type_id=QR_ENGINE_LLAMA_SERVER)
        for lsi in all_ls:
            try:
                raw = lsi.get("rpc_bind_ids") or "[]"
                ids = json.loads(raw) if isinstance(raw, str) else list(raw or [])
                if inst_id in ids:
                    ls = lsi
                    llama_id = lsi["id"]
                    break
            except (json.JSONDecodeError, TypeError):
                pass

    try:
        if is_bind and ls:
            # Bind: append RPC instance ID to target's list
            current_ids = []
            if ls.get("rpc_bind_ids"):
                try:
                    current_ids = json.loads(ls["rpc_bind_ids"]) if isinstance(ls["rpc_bind_ids"], str) else list(ls["rpc_bind_ids"] or [])
                except (json.JSONDecodeError, TypeError):
                    current_ids = []
            if inst_id not in current_ids:
                current_ids.append(inst_id)
            _ui(_CONFIG["db_path"], llama_id, rpc_bind_ids=json.dumps(current_ids))
        elif not is_bind and ls:
            # Unbind: remove RPC instance ID from target's list
            if ls.get("rpc_bind_ids"):
                try:
                    current_ids = json.loads(ls["rpc_bind_ids"]) if isinstance(ls["rpc_bind_ids"], str) else list(ls["rpc_bind_ids"] or [])
                except (json.JSONDecodeError, TypeError):
                    current_ids = []
                current_ids = [x for x in current_ids if x != inst_id]
                _ui(_CONFIG["db_path"], ls["id"], rpc_bind_ids=json.dumps(current_ids)) if current_ids else None

        return success_single({"rpc_id": inst_id, "llama_id": llama_id, "bound": is_bind and bool(llama_id)})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Bind failed: {exc}")


def api_rpccluster_summary():
    """List all llama-server instances with resolved cluster info.

    Returns: list of llama-servers with rpc_bindings and computed tensor_split.

    Returns:
        JSON with status and data.llama_servers array.
    """
    from db.adapters.instances import list_instances as _list_all
    from lib.lib_cluster_env_builder import get_cluster_summary as _get_summary
    from db.sqlite import pool

    try:
        all_ls = _list_all(_CONFIG["db_path"], engine_type_id=QR_ENGINE_LLAMA_SERVER)
        servers = []
        for lsi in all_ls:
            try:
                summary = _get_summary(_CONFIG["db_path"], lsi["id"])
                servers.append(summary)
            except Exception:
                pass  # Skip instances that fail to summarize
        return success_single({"llama_servers": servers})
    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_rpccluster_bind(llama_id):
    """Bind RPC instance(s) to a llama-server.

    Args:
        llama_id: Integer primary key of the llama-server instance.
        Body: {"rpc_ids": [132, 133]}

    Returns:
        JSON with updated cluster summary.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui
    from lib.lib_cluster_env_builder import get_cluster_summary as _get_summary

    try:
        llama_inst = _gi(_CONFIG["db_path"], llama_id)
        if not llama_inst:
            return error_response("RESOURCE_NOT_FOUND", f"Llama-server {llama_id} not found")
        if llama_inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
            return error_response("INVALID_ENGINE", f"Instance {llama_id} is not a llama-server")

        body, is_err = require_json()
        if is_err:
            return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

        rpc_ids = body.get("rpc_ids", [])
        if not isinstance(rpc_ids, list):
            return error_response("VALIDATION_ERROR", "rpc_ids must be a list")

        # Validate each RPC ID (must exist and be rpc type), build clean list
        valid_ids = []
        for rid in rpc_ids:
            try:
                rid = int(rid)
            except (ValueError, TypeError):
                continue
            # Validate RPC instance exists and is llama_rpc type
            rpc_inst = _gi(_CONFIG["db_path"], rid)
            if not rpc_inst or rpc_inst.get("engine_type_name") != QR_ENGINE_LLAMA_RPC_NAME:
                continue
            valid_ids.append(rid)

        _ui(_CONFIG["db_path"], llama_id, rpc_bind_ids=json.dumps(valid_ids))

        # Return updated cluster summary
        summary = _get_summary(_CONFIG["db_path"], llama_id)
        return success_single({"llama_server": summary})

    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Bind failed: {exc}")


def api_rpccluster_unbind(llama_id, rpc_id):
    """Unbind a single RPC from a llama-server.

    Args:
        llama_id: Integer primary key of the llama-server instance.
        rpc_id: Integer primary key of the RPC instance to unbind.

    Returns:
        JSON with updated cluster summary.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui
    from lib.lib_cluster_env_builder import get_cluster_summary as _get_summary

    try:
        llama_inst = _gi(_CONFIG["db_path"], llama_id)
        if not llama_inst:
            return error_response("RESOURCE_NOT_FOUND", f"Llama-server {llama_id} not found")

        # Get current bindings
        current_ids = []
        if llama_inst.get("rpc_bind_ids"):
            try:
                current_ids = json.loads(llama_inst["rpc_bind_ids"]) if isinstance(llama_inst["rpc_bind_ids"], str) else list(llama_inst["rpc_bind_ids"] or [])
            except (json.JSONDecodeError, TypeError):
                current_ids = []

        # Remove RPC ID
        if rpc_id in current_ids:
            current_ids = [x for x in current_ids if x != rpc_id]
            _ui(_CONFIG["db_path"], llama_id, rpc_bind_ids=json.dumps(current_ids))

        # Return updated cluster summary
        summary = _get_summary(_CONFIG["db_path"], llama_id)
        return success_single({"llama_server": summary})

    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Unbind failed: {exc}")


def api_deploy_preview(inst_id):
    """Return the computed deployment config (env + CLI args) for a llama-server instance.

    Uses build_llama_server_env() to compute the full merged environment and CLI string
    that would be used on deploy. Useful for previewing RPC bindings, tensor_split, etc.

    Args:
        inst_id: Integer primary key of the llama-server instance.

    Returns:
        JSON with env dict, cli_args, tensor_split, split_mode, rpc_bindings.
    """
    try:
        from lib.lib_cluster_env_builder import build_llama_server_env as _build_env
        result = _build_env(_CONFIG["db_path"], inst_id)
        # Add cli_flags and draft_devices from instance data
        with db_pool(_CONFIG["db_path"]) as conn:
            row = conn.execute(
                "SELECT cli_flags FROM instances WHERE id = ?", (inst_id,)
            ).fetchone()
        raw_flags = row["cli_flags"] if row else None
        try:
            parsed_flags = json.loads(raw_flags) if isinstance(raw_flags, str) else []
            if not isinstance(parsed_flags, list):
                parsed_flags = []
        except (json.JSONDecodeError, TypeError):
            parsed_flags = []
        # Compute draft devices from rpc_bindings
        draft_devices = []
        for idx, b in enumerate(result.get("rpc_bindings", [])):
            d = b.get("draft", 0)
            if isinstance(d, int) and d > 0:
                draft_devices.append(f"RPC{idx}")
        result["cli_flags"] = parsed_flags
        result["draft_devices"] = draft_devices
        # Wrap in success response format
        return success_single(result)
    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_merged_config(inst_id):
    """Return the complete merged configuration with source annotations for each key.

    Shows the 6-layer merge trace: engine defaults → preset → model → cluster binding →
    instance override → metadata. Each key is annotated with its source layer.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        JSON with env/cli_opts/model sections, each key annotated with source_layer,
        plus layer_summary showing keys contributed per layer.
    """
    try:
        from db.adapters.instances import merge_configs as _merge
        result = _merge(_CONFIG["db_path"], inst_id)

        # Build annotated response format for WebUI
        env_annotated = {}
        model_annotated = {}
        cli_annotated = []

        # Layer source annotation from _layers metadata
        layers = result.get("_layers", {})

        # Annotate env keys
        for key, val in result.get("env", {}).items():
            if key.startswith("_"):
                continue  # Skip internal metadata keys
            # Find which layer contributed this key
            source = "unknown"
            for layer_name, layer_data in layers.items():
                if isinstance(layer_data, dict):
                    env_keys = layer_data.get("env_keys", [])
                    if key in env_keys:
                        source = layer_name
                        break
            env_annotated[key] = {"value": val, "source_layer": source}

        # Annotate model keys
        for key, val in result.get("model", {}).items():
            source = "unknown"
            for layer_name, layer_data in layers.items():
                if isinstance(layer_data, dict):
                    model_keys = layer_data.get("model_keys", [])
                    if key in model_keys:
                        source = layer_name
                        break
            model_annotated[key] = {"value": val, "source_layer": source}

        # Annotate CLI opts (source from preset or instance_override cli_opts_count)
        cli_opts = result.get("cli_opts", [])
        for opt in cli_opts:
            cli_annotated.append({"value": opt, "source_layer": "preset"})

        # Build layer summary
        layer_summary_layers = []
        layer_name_map = {
            "engine_default": "Engine default configs",
            "preset": "Preset config template",
            "model": "Model definition",
            "cluster_binding": "Cluster/RPC binding",
            "instance_override": "Per-instance override",
            "metadata": "Metadata injection",
        }
        for layer_key, count_info in layers.items():
             if isinstance(count_info, dict):
                 env_keys = count_info.get("env_keys", [])
                 model_keys = count_info.get("model_keys", [])
                 total = len(env_keys) + len(model_keys) + int(count_info.get("cli_opts_count", 0))
             else:
                 total = 0
             layer_summary_layers.append({
                  "name": layer_name_map.get(layer_key, layer_key),
                  "keys_contribution": total if isinstance(total, (int, float)) else 0,
             })

        # Extract actual overrides from instance_override layer
        instance_ov = layers.get("instance_override", {})
        actual_overrides = {}
        if isinstance(instance_ov, dict):
            ov_env_keys = instance_ov.get("env_keys", [])
            ov_model_keys = instance_ov.get("model_keys", [])
            # env_annotated and model_annotated contain the actual values with source_layer
            for key in ov_env_keys:
                if key in env_annotated:
                    actual_overrides[key] = env_annotated[key]["value"]
            for key in ov_model_keys:
                if key in model_annotated:
                    actual_overrides[key] = model_annotated[key]["value"]

        return jsonify({
            "status": "ok",
            "data": {
                "env": env_annotated,
                "cli_opts": cli_annotated,
                "model": model_annotated,
                "actual_overrides": actual_overrides,
                "start_on_boot": result.get("start_on_boot"),
            },
            "layer_summary": {"layers": layer_summary_layers},
        }), 200

    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_cycle_split_mode(inst_id):
    """Cycle split_mode on a llama-server instance: layer → row → tensor → layer.

    Args:
        inst_id: Integer primary key of the llama-server instance.

    Returns:
        JSON with new split_mode value.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
        return error_response("INVALID_ENGINE", "split-mode cycle only works for llama_server instances")

    modes = ["layer", "row", "tensor"]
    current = inst.get("split_mode") or "layer"
    idx = modes.index(current) if current in modes else 0
    new_mode = modes[(idx + 1) % len(modes)]

    try:
        _ui(_CONFIG["db_path"], inst_id, split_mode=new_mode)
        return success_single({"instance_id": inst_id, "split_mode": new_mode})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update split_mode: {exc}")


def api_set_split(inst_id):
    """Set the split value for an instance (RPC or llama_server).

    Args:
        inst_id: Integer primary key of the instance.
        Body: {"split": <int>}

    Returns:
        JSON with updated split value.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    split_val = body.get("split")
    # None/null → clear split (CPU-only mode), otherwise validate as integer 0-100
    if split_val is not None:
        try:
            split_val = int(split_val)
            if split_val < 0 or split_val > 100:
                return error_response("VALIDATION_ERROR", "split must be between 0 and 100")
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "split must be an integer 0-100")

    try:
        _ui(_CONFIG["db_path"], inst_id, split=split_val)
        return success_single({"instance_id": inst_id, "split": split_val})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update split: {exc}")


def api_set_experts(inst_id):
    """Set the experts value for an instance."""
    from db.adapters.instances import get_instance as _gi, update_instance as _ui
    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))
    experts_val = body.get("experts")
    try:
        experts_val = int(experts_val)
        if experts_val < 0 or experts_val > 100:
            return error_response("VALIDATION_ERROR", "experts must be between 0 and 100")
    except (ValueError, TypeError):
        return error_response("VALIDATION_ERROR", "experts must be an integer 0-100")
    try:
        _ui(_CONFIG["db_path"], inst_id, experts=experts_val)
        return success_single({"instance_id": inst_id, "experts": experts_val})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update experts: {exc}")


def api_set_draft(inst_id):
    """Set the draft value for an instance."""
    from db.adapters.instances import get_instance as _gi, update_instance as _ui
    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))
    draft_val = body.get("draft")
    try:
        draft_val = int(draft_val)
        if draft_val < 0 or draft_val > 100:
            return error_response("VALIDATION_ERROR", "draft must be between 0 and 100")
    except (ValueError, TypeError):
        return error_response("VALIDATION_ERROR", "draft must be an integer 0-100")
    try:
        _ui(_CONFIG["db_path"], inst_id, draft=draft_val)
        return success_single({"instance_id": inst_id, "draft": draft_val})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update draft: {exc}")


def api_set_cli_flags(inst_id):
    """Set CLI flags for an instance (Herd cluster setup).

    Args:
        inst_id: Integer primary key of the instance.
        Body: {"flags": ["--no-mmproj-offload", "--mlock", "--flash-attn", "on"]}

    Returns:
        JSON with updated cli_flags list.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    flags = body.get("flags")
    if flags is None:
        flags = []
    if not isinstance(flags, list):
        return error_response("VALIDATION_ERROR", "flags must be a JSON array")
    # Validate each flag is a non-empty string
    for f in flags:
        if not isinstance(f, str) or not f.strip():
            return error_response("VALIDATION_ERROR", f"Each flag must be a non-empty string, got: {f!r}")

    try:
        _ui(_CONFIG["db_path"], inst_id, cli_flags=json.dumps(flags))
        return success_single({"instance_id": inst_id, "flags": flags})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update cli_flags: {exc}")


def api_get_cli_flags(inst_id):
    """Get current CLI flags for an instance (Herd cluster setup)."""
    from db.adapters.instances import get_instance as _gi

    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    raw = inst.get("cli_flags") or "[]"
    try:
        flags = json.loads(raw) if isinstance(raw, str) else []
        if not isinstance(flags, list):
            flags = []
    except (json.JSONDecodeError, TypeError):
        flags = []
    return success_single({"instance_id": inst_id, "flags": flags})


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
    engine_type_name = inst.get("engine_type_name", "llama_rpc")

   # Route system-managed instances to subprocess-based start path
    if _csm(_CONFIG["db_path"], inst_id):
        return _start_system_managed(inst_id, engine_type_name, log_action)

    # Subprocess engine: always try execute (local process, no playbook state lock)
    if engine_type_name == "subprocess":
        from engine import get_engine as _ge
        engine = _ge("subprocess")
        if engine is None:
            return error_response("DEPLOYMENT_FAILED", "subprocess engine not loaded")
        # Check if already running via engine status
        status = engine.get_status(inst_id, _CONFIG["db_path"])
        if status.get("running"):
            # Already running — ensure state is running
            try:
                transition_state(_CONFIG["db_path"], inst_id, "starting")
            except Exception:
                pass
            try:
                transition_state(_CONFIG["db_path"], inst_id, "running")
            except Exception:
                pass
            return success_single({"action": "start", "instance_id": inst_id,
                                    "state": "running", "idempotent": True})
        result = engine.execute(inst_id, "start", _CONFIG["db_path"])
        if result.get("error"):
            try:
                transition_state(_CONFIG["db_path"], inst_id, "error")
            except Exception:
                pass
            return error_response("DEPLOYMENT_FAILED", result["error"])
        # execute() already transitions state (starting → running)
        # Just confirm the PID is alive for safety
        if result.get("pid"):
            try:
                import psutil as _psutil
                p = _psutil.Process(result["pid"])
                if p.status() != "zombie":
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, "starting")
                    except Exception:
                        pass
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, "running")
                    except Exception:
                        pass
                else:
                    # Zombie process — clear PID and go back to deployed
                    _ui = __import__("db.adapters.instances", fromlist=["update_instance"]).update_instance
                    _ui(_CONFIG["db_path"], inst_id, pid_last_known=None)
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass
        log_action(_CONFIG["db_path"], inst_id, "start", "success", detail={"subprocess": result})
        return success_single({"action": "start", "instance_id": inst_id,
                                "state": "running", "pid": result.get("pid")})

   # RPC binding warnings for llama_server instances (engine_type_id=21)
    if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME and inst.get("rpc_bind_ids"):
        try:
            from lib.lib_cluster_env_builder import rpc_binding_warnings as _rbw
            rp_warnings = _rbw(_CONFIG["db_path"], inst_id)
        except Exception:
            rp_warnings = []
    else:
        rp_warnings = []

    allowed = ["deployed", "stopped", "stopping", "error", "unconfigured", "deploying", "build_error", "configuring"]
    # Idempotency: if already running or starting, consider it a success
    if inst["state"] in ("running", "starting"):
        resp = {"action": "start", "instance_id": inst_id,
                "state": inst["state"], "idempotent": True}
        if rp_warnings:
            resp["warnings"] = rp_warnings
        return success_single(resp)
    if inst["state"] not in allowed:
        return error_response("INVALID_STATE",
                                 f"Cannot start instance in '{inst['state']}' state (allowed: {allowed})")

    log_action(_CONFIG["db_path"], inst_id, "start", "received")

    # Auto-deploy if unconfigured or deploying (stuck)
    if inst["state"] in ("unconfigured", "deploying"):
        deploy_result = deploy_instance(_CONFIG["db_path"], inst_id)
        if not deploy_result.get("success"):
            log_action(_CONFIG["db_path"], inst_id, "start", "failed",
                        detail={"auto_deploy": deploy_result})
            return error_response("DEPLOYMENT_FAILED",
                                f"Auto-deploy failed: {deploy_result.get('message', 'unknown')}")

    # Universal engine: require start_command or binary_path
    if engine_type_name == "universal":
        co = inst.get("config_override") or {}
        if isinstance(co, str):
            try:
                import json as _json
                co_merged = _json.loads(co) or {}
            except Exception:
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

    # Transition to starting
    try:
        transition_state(_CONFIG["db_path"], inst_id, "starting")
    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "start", "failed", detail={"error": str(exc)})
        return error_response("DEPLOYMENT_FAILED", str(exc))

    # Run universal start playbook on remote node
    remote_result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
    if remote_result.get("success"):
        try:
            transition_state(_CONFIG["db_path"], inst_id, "running")
        except Exception:
            pass  # best-effort — remote succeeded
        log_action(_CONFIG["db_path"], inst_id, "start", "success",
                    detail={"remote": remote_result})
    else:
        log_action(_CONFIG["db_path"], inst_id, "start", "failed",
                    detail={"remote": remote_result})
        try:
            transition_state(_CONFIG["db_path"], inst_id, "error")
        except Exception:
            pass  # best-effort — already logged
        error_msg = (remote_result.get("playbook_msg") or
                     remote_result.get("svc_status") or
                     str(remote_result.get("results", ""))[:200] or
                     "no details available")
        return error_response("DEPLOYMENT_FAILED",
                                f"Remote start failed: {error_msg}")

    resp = {"action": "start", "instance_id": inst_id,
                "state": "running"}
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
    engine_type_name = inst.get("engine_type_name", "llama_rpc")

    # Route system-managed instances to subprocess-based stop path
    if _csm(_CONFIG["db_path"], inst_id):
        return _stop_system_managed(inst_id, engine_type_name, log_action)

    # Subprocess engine: use local subprocess management
    if engine_type_name == "subprocess":
        from engine import get_engine as _ge
        engine = _ge("subprocess")
        if engine is None:
            return error_response("DEPLOYMENT_FAILED", "subprocess engine not loaded")
        try:
            transition_state(_CONFIG["db_path"], inst_id, "stopping")
        except Exception:
            pass
        result = engine.execute(inst_id, "stop", _CONFIG["db_path"])
        # Always transition to stopped regardless of execute() result
        # (process may already be dead with stale PID)
        try:
            transition_state(_CONFIG["db_path"], inst_id, "stopped")
        except Exception:
            pass
        if result.get("error"):
            log_action(_CONFIG["db_path"], inst_id, "stop", "failed", detail={"subprocess": result})
            return success_single({"action": "stop", "instance_id": inst_id, "state": "stopped", "note": result["error"]})
        log_action(_CONFIG["db_path"], inst_id, "stop", "success", detail={"subprocess": result})
        return success_single({"action": "stop", "instance_id": inst_id, "state": "stopped"})

    allowed = ["running", "starting", "stopping", "configuring", "deploying", "error", "updating", "deployed", "build_error"]
    # Idempotency: if already stopped, consider it a success
    if inst["state"] in ("stopped", "stopping"):
        return success_single({"action": "stop", "instance_id": inst_id,
                                "state": inst["state"], "idempotent": True})
    if inst["state"] not in allowed:
        return error_response("INVALID_STATE",
                                f"Cannot stop instance in '{inst['state']}' state (allowed: {allowed})")

    log_action(_CONFIG["db_path"], inst_id, "stop", "received")

    # Transition states locally first
    try:
        transition_state(_CONFIG["db_path"], inst_id, "stopping")
    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "stop", "failed", detail={"error": str(exc)})
        return error_response("DEPLOYMENT_FAILED", str(exc))

    # Run universal stop playbook on remote node
    remote_result = _run_manage_action(inst_id, engine_type_name, node_id, "stop")
    if remote_result.get("success"):
        transition_state(_CONFIG["db_path"], inst_id, "stopped")
        log_action(_CONFIG["db_path"], inst_id, "stop", "success",
                    detail={"remote": remote_result})
    else:
        # For stuck states (starting/deploying/error), stop failure is acceptable
        # — the unit may not exist yet or already stopped. Transition to stopped anyway.
        log_action(_CONFIG["db_path"], inst_id, "stop", "failed",
                    detail={"remote": remote_result, "recovery": "forced_stopped"})
        transition_state(_CONFIG["db_path"], inst_id, "stopped")

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
    engine_type_name = inst.get("engine_type_name", "llama_rpc")

    # Route system-managed instances to subprocess-based restart path
    if _csm(_CONFIG["db_path"], inst_id):
        return _restart_system_managed(inst_id, engine_type_name, log_action)

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], inst.get("node_id"))
    if isinstance(nd, tuple):
        return nd

    log_action(_CONFIG["db_path"], inst_id, "restart", "received")

    # Idempotency: if already starting (mid-start), consider it success
    if inst["state"] == "starting":
        return success_single({"action": "restart", "instance_id": inst_id,
                                "state": inst["state"], "idempotent": True})

    # Log override if restarting from non-running state (deployed/stopped)
    if inst.get("state") in ("deployed", "stopped"):
        log_qr_override(_CONFIG["db_path"], "restart_from_deployed",
                            node_id=inst.get("node_id"), instance_id=inst_id,
                            actor="api",
                            details={"from_state": inst["state"]})

    # Subprocess engine: skip ansible playbooks entirely — runs locally via Popen, not systemd
    if engine_type_name == "subprocess":
        from engine import get_engine as _ge
        engine = _ge("subprocess")
        if engine is None:
            return error_response("DEPLOYMENT_FAILED", "subprocess engine not loaded")
        # For running/stopping states, do a proper stop→start cycle
        if inst["state"] in ("running", "stopping"):
            try:
                transition_state(_CONFIG["db_path"], inst_id, "stopping")
            except Exception as exc:
                log_action(_CONFIG["db_path"], inst_id, "restart", "failed", detail={"phase": "stopping", "error": str(exc)})
                return error_response("DEPLOYMENT_FAILED", str(exc))
            # Stop the process
            stop_result = engine.execute(inst_id, "stop", _CONFIG["db_path"])
            try:
                transition_state(_CONFIG["db_path"], inst_id, "stopped")
            except Exception:
                pass
        # Then start (handles stopped/deployed/error states directly)
        try:
            transition_state(_CONFIG["db_path"], inst_id, "starting")
        except Exception as exc:
            log_action(_CONFIG["db_path"], inst_id, "restart", "failed", detail={"phase": "starting", "error": str(exc)})
            return error_response("DEPLOYMENT_FAILED", str(exc))
        start_result = engine.execute(inst_id, "start", _CONFIG["db_path"])
        if start_result.get("error"):
            try:
                transition_state(_CONFIG["db_path"], inst_id, "error")
            except Exception:
                pass
            return error_response("DEPLOYMENT_FAILED", start_result["error"])
        # If process is alive, transition to running
        if start_result.get("pid"):
            try:
                import psutil as _psutil
                p = _psutil.Process(start_result["pid"])
                if p.status() != "zombie":
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, "starting")
                    except Exception:
                        pass
                    try:
                        transition_state(_CONFIG["db_path"], inst_id, "running")
                    except Exception:
                        pass
                else:
                    # Zombie process — clear PID
                    _ui = __import__("db.adapters.instances", fromlist=["update_instance"]).update_instance
                    _ui(_CONFIG["db_path"], inst_id, pid_last_known=None)
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                pass
        log_action(_CONFIG["db_path"], inst_id, "restart", "success", detail={"subprocess": start_result})
        return success_single({"action": "restart", "instance_id": inst_id, "state": "running"})

    # Step 1: stopping
    try:
        transition_state(_CONFIG["db_path"], inst_id, "stopping")
    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "restart", "failed", detail={"phase": "stopping", "error": str(exc)})
        return error_response("DEPLOYMENT_FAILED", str(exc))

    # Step 2: run stop on remote
    stop_result = _run_manage_action(inst_id, engine_type_name, node_id, "stop")
    if not stop_result.get("success"):
        log_action(_CONFIG["db_path"], inst_id, "restart", "failed",
                    detail={"phase": "stop", "remote": stop_result})
        return error_response("DEPLOYMENT_FAILED",
                                f"Restart stop failed: {stop_result.get('error', 'unknown')}")

    # Step 3: stopped
    try:
        transition_state(_CONFIG["db_path"], inst_id, "stopped")
    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "restart", "failed", detail={"phase": "stopped", "error": str(exc)})
        return error_response("DEPLOYMENT_FAILED", str(exc))

    # Step 4: starting
    try:
        transition_state(_CONFIG["db_path"], inst_id, "starting")
    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "restart", "failed", detail={"phase": "starting", "error": str(exc)})
        return error_response("DEPLOYMENT_FAILED", str(exc))

    # Step 5: run start on remote
    start_result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
    if not start_result.get("success"):
        log_action(_CONFIG["db_path"], inst_id, "restart", "failed",
                    detail={"phase": "start", "remote": start_result})
        # Log to ansible_actions for full audit trail
        try:
            from lib.lib_ansible_runner import log_ansible_action as _laa
            _laa(_CONFIG["db_path"], "restart_instance", node_id, inst_id,
                    "manage_instance.yml (restart)", {"action": "restart", "engine_type": engine_type_name},
                    {"changed": start_result.get("changed", False), "failed": True,
                    "results": start_result.get("results", {})})
        except Exception:
            pass  # non-critical
        try:
            transition_state(_CONFIG["db_path"], inst_id, "error")
        except Exception:
            pass
        return error_response("DEPLOYMENT_FAILED",
                                f"Restart start failed: {start_result.get('playbook_msg', start_result.get('error', 'unknown'))}")

    # Step 6: running
    try:
        transition_state(_CONFIG["db_path"], inst_id, "running")
    except Exception:
        pass  # best-effort

    log_action(_CONFIG["db_path"], inst_id, "restart", "success",
                detail={"remote": start_result})
    # Log to ansible_actions for full audit trail
    try:
        from lib.lib_ansible_runner import log_ansible_action as _laa
        _laa(_CONFIG["db_path"], "restart_instance", node_id, inst_id,
                "manage_instance.yml (restart)", {"action": "restart", "engine_type": engine_type_name},
                {"changed": start_result.get("changed", False), "failed": not start_result.get("success"),
                "results": start_result.get("results", {})})
    except Exception:
        pass  # non-critical

    return success_single({"action": "restart", "instance_id": inst_id, "state": "running"})


def api_deploy_instance(inst_id):
    """Deploy/redeploy an instance to its target node.

    Returns structured result with task_summary and duration_ms on failure,
    providing actionable feedback instead of generic 'playbook reported failures'.
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

        # RPC binding warnings for llama_server instances before deploy
        rp_warnings = []
        if inst and inst.get("engine_type_name") == QR_ENGINE_LLAMA_SERVER_NAME and inst.get("rpc_bind_ids"):
            try:
                from lib.lib_cluster_env_builder import rpc_binding_warnings as _rbw
                rp_warnings = _rbw(_CONFIG["db_path"], inst_id)
            except Exception:
                pass
        # Read skip_build from request body (Herd page sends this)
        _deploy_skip = None
        if request.json:
            _sb = request.json.get("skip_build")
            if isinstance(_sb, bool):
                _deploy_skip = _sb
            elif isinstance(_sb, str):
                _deploy_skip = _sb.lower() in ("true", "1")
            elif isinstance(_sb, (int, float)):
                _deploy_skip = bool(_sb)
        result = deploy_instance(_CONFIG["db_path"], inst_id, skip_build=_deploy_skip)
        # Handle both legacy dict return and new error_response tuple format
        if isinstance(result, tuple) and len(result) >= 2:
            return result  # Already an error_response (code, message) from interlock
        response = {"action": "deploy", "instance_id": inst_id,
                        "success": result.get("success"),
                        "message": result.get("message", "")}
        if "task_summary" in result:
            response["task_summary"] = result["task_summary"]
        if "duration_ms" in result:
            response["duration_ms"] = result["duration_ms"]
        if "uuid_mismatches" in result:
            response["uuid_mismatches"] = result["uuid_mismatches"]
        if "uuid_stray" in result:
            response["uuid_stray"] = result["uuid_stray"]
        if rp_warnings:
            response["warnings"] = rp_warnings
        return success_single(response)
    except Exception as exc:
        import traceback; traceback.print_exc()
        return error_response("DEPLOYMENT_FAILED", str(exc))


def api_reconfigure_instance(inst_id):
    """Reconfigure an instance: update env file + systemd unit, reload daemon, restart service.

    Uses UPDATE_CONFIG_V1 playbook — no git clone/pull, no cmake build.
    Works for running and stopped llama_server/rpc/iperf3 instances.
    Transitions: running/stopped → updating → [running|error].

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

    if engine_type_name not in ("llama_server", "llama_rpc", "iperf3", "subprocess"):
        return error_response("UNSUPPORTED_ENGINE",
                f"Reconfigure only supported for llama_server/rpc/iperf3/subprocess (got {engine_type_name})")

    if current_state not in ("running", "stopped", "error"):
        return error_response("INVALID_STATE",
                f"Cannot reconfigure instance in '{current_state}' state (only running/stopped/error)")

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
        from lib.lib_cluster_env_builder import build_llama_server_env, build_rpc_server_env
        from db.adapters.nodes import get_node as _gn

        # Build cluster-aware config
        if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
            cluster_result = build_llama_server_env(_CONFIG["db_path"], inst_id)
        elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
            cluster_result = build_rpc_server_env(_CONFIG["db_path"], inst_id)
        else:
            cluster_result = {"env": {}, "cli_args": ""}

        # Subprocess: restart via engine.execute() instead of Ansible playbook
        if engine_type_name == "subprocess":
            from engine import get_engine as _ge_sub
            _ts(_CONFIG["db_path"], inst_id, "updating")
            _log(_CONFIG["db_path"], inst_id, "config_change", "received")
            engine = _ge_sub("subprocess")
            if engine is None:
                _ts(_CONFIG["db_path"], inst_id, "error")
                return error_response("RECONFIGURE_FAILED", "Subprocess engine not loaded")
            result = engine.execute(inst_id, "restart", _CONFIG["db_path"])
            if isinstance(result, dict) and result.get("error"):
                _ts(_CONFIG["db_path"], inst_id, "build_error")
                return error_response("RECONFIGURE_FAILED", f"Reconfigure failed: {result['error']}")
            _ts(_CONFIG["db_path"], inst_id, "running")
            _log(_CONFIG["db_path"], inst_id, "config_change", "success")
            return success_single({"action": "reconfigure", "instance_id": inst_id,
                                    "state": "running", "message": "Subprocess restarted"})

        try:
            nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
            hostname = (nd.get("hostname") or nd.get("name")) if nd else "localhost"
        except Exception:
            hostname = "localhost"

        extra_vars = {
            "inventory_host": hostname,
            "instance_id": inst["id"],
            "instance_name": inst.get("name", ""),
            "engine_type": engine_type_name,
            "instance_port": inst.get("port_assigned", 0),
            "binary_path": cluster_result["env"].get("binary_path", "") if isinstance(cluster_result.get("env"), dict) else "",
            "remote_node_user": (nd.get("ansible_user") if nd else None) or DEFAULT_ANSIBLE_USER,
            "merged_env": cluster_result["env"] if isinstance(cluster_result.get("env"), dict) else {},
            "merged_cli_opts": [s for s in cluster_result["cli_args"].split()] if isinstance(cluster_result.get("cli_args"), str) and cluster_result["cli_args"] else [],
            "tensor_split_value": cluster_result.get("tensor_split_str", ""),
            "split_mode_value": cluster_result.get("split_mode", "layer"),
            "rpc_bind_ids": json.loads(inst.get("rpc_bind_ids") or "[]") if isinstance(inst.get("rpc_bind_ids"), str) else (inst.get("rpc_bind_ids") or []),
            "instance_uuid": inst.get("instance_uuid", ""),
        }

        _ts(_CONFIG["db_path"], inst_id, "updating")
        _log(_CONFIG["db_path"], inst_id, "config_change", "received")

        r = _execute_playbook("UPDATE_CONFIG_V1", resolver_type="playbook_id",
                                limit=hostname, extra_vars=extra_vars, timeout=300,
                                action_type="config_change", node_id=node_id, instance_id=inst_id)

        if r["error"]:
            _ts(_CONFIG["db_path"], inst_id, "build_error")
            _log(_CONFIG["db_path"], inst_id, "config_change", "failed", detail={"error": r["error"]})
            return error_response("RECONFIGURE_FAILED", f"Reconfigure failed: {r['error']}")

        _ts(_CONFIG["db_path"], inst_id, "running")
        _log(_CONFIG["db_path"], inst_id, "config_change", "success")
        return success_single({"action": "reconfigure", "instance_id": inst_id,
                                "state": "running", "message": "Reconfigured and service restarted"})

    except Exception as exc:
        try:
            _ts(_CONFIG["db_path"], inst_id, "error")
            _log(_CONFIG["db_path"], inst_id, "config_change", "exception", detail={"error": str(exc)})
        except Exception:
            pass
        return error_response("RECONFIGURE_ERROR", str(exc))
    finally:
        lock.release()


def api_undeploy_instance(inst_id):
    """Remove deployed files from remote node, transition to unconfigured.

    System-managed instances (quickrobot-api, quickrobot-webui) cannot be
    undeployed — they run locally and have no remote artifacts.
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

    allowed = ["running", "stopped", "starting", "stopping", "configuring", "deploying", "error", "deployed", "updating", "build_error"]
    if inst["state"] not in allowed:
        return error_response("INVALID_STATE",
                                f"Cannot undeploy instance in '{inst['state']}' state (allowed: {allowed})")

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], inst.get("node_id"))
    if isinstance(nd, tuple):
        return nd

    log_action(_CONFIG["db_path"], inst_id, "undeploy", "received")

    # Run remote undeploy playbook if instance is deployed and has a node
    engine_type_name = inst.get("engine_type_name", "llama_rpc")
    node_id = inst.get("node_id")
    remote_undeploy_ok = None
    
    if node_id is not None and inst["state"] != "unconfigured":
        # Universal engine: use built-in undeploy_universal.yml
        if engine_type_name == "universal":
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
                    "instance_id": inst_id,
                    "instance_name": instance_name,
                    "install_dir": install_dir,
                    "clean_source_dir": bool(co_merged.get("clean_source_dir", False)),
                    "clean_venv": bool(co_merged.get("clean_venv", False)),
                }

                pb_id = _resolve_engine_playbook_id("undeploy", "universal")
                if not pb_id:
                    log_action(_CONFIG["db_path"], inst_id, "undeploy", "partial",
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
                        log_action(_CONFIG["db_path"], inst_id, "undeploy", "partial",
                                    detail={"error": str(undeploy_result.get("error", "unknown"))})
                else:
                    log_action(_CONFIG["db_path"], inst_id, "undeploy", "partial",
                                detail={"message": "No hostname for node"})
                    remote_undeploy_ok = True
            except Exception as exc:
                log_action(_CONFIG["db_path"], inst_id, "undeploy", "partial",
                            detail={"error": str(exc)})
        # Standard engine (non-universal): use built-in undeploy playbook
        try:
            from db.adapters.nodes import get_node as _gn
            from lib.lib_ansible_runner import run_playbook
            nd = _gn(_CONFIG["db_path"], node_id) if node_id else None
            hostname = (nd.get("ansible_inventory_host") or
                         nd.get("hostname") or
                         nd.get("name")) if nd else None
            # Dynamic inventory — no file generated (DI-7)
            pb_id = _resolve_engine_playbook_id("undeploy", engine_type_name)
            if not pb_id:
                log_action(_CONFIG["db_path"], inst_id, "undeploy", "partial",
                            detail={"message": f"undeploy playbook not in registry for {engine_type_name}"})
            elif hostname:
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
                remote_undeploy_ok = not undeploy_result.get("failed", False)
                if not remote_undeploy_ok:
                    log_action(_CONFIG["db_path"], inst_id, "undeploy", "partial",
                                detail={"error": str(undeploy_result.get("error", "unknown"))})
        except Exception as exc:
            log_action(_CONFIG["db_path"], inst_id, "undeploy", "partial",
                        detail={"error": str(exc)})

    try:
        # Transition path depends on current state
        if inst["state"] == "running":
            transition_state(_CONFIG["db_path"], inst_id, "stopping")
            transition_state(_CONFIG["db_path"], inst_id, "stopped")
        elif inst["state"] in ("starting", "stopping", "deploying"):
            transition_state(_CONFIG["db_path"], inst_id, "stopping")
            transition_state(_CONFIG["db_path"], inst_id, "stopped")
        # error and stopped states: direct to unconfigured
        updated = transition_state(_CONFIG["db_path"], inst_id, "unconfigured")
    except Exception as exc:
        log_action(_CONFIG["db_path"], inst_id, "undeploy", "failed", detail={"error": str(exc)})
        return error_response("DEPLOYMENT_FAILED", str(exc))

    log_action(_CONFIG["db_path"], inst_id, "undeploy", "success" if remote_undeploy_ok else "partial",
                detail={"remote_undeploy": remote_undeploy_ok})

    # Check if shared build should be cleaned up (last llama_server/llama_rpc on node)
    cleanup_done = None
    if engine_type_name in ("llama_server", "llama_rpc") and _get_keep_shared_build():
        try:
            from db.adapters.instances import list_instances as _list_all
            remaining = [i for i in _list_all(_CONFIG["db_path"], node_id=node_id)
                            if i.get("engine_type_name") in ("llama_server", "llama_rpc")]
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

    return success_single(inst)


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

    engine_type_name = inst.get("engine_type_name", "llama_rpc")
    
    if engine_type_name == "universal":
        # Use universal engine's execute method
        cmd = body.get("command", "")
        timeout = body.get("timeout", 30)
        
        eng = get_engine("universal")
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
    if engine_type_name != "iperf3":
        return error_response("WRONG_ENGINE_TYPE",
                                f"Endpoint requires iperf3 engine, got '{engine_type_name}'")

    node_id = inst.get("node_id")
    instance_name = inst.get("name", "")
    port = inst.get("port_assigned")

    # Determine preset mode: server or client
    merged = {}
    try:
        merged = _mc(_CONFIG["db_path"], inst_id)
    except Exception:
        merged = {"env": {}, "cli_opts": [], "model": {}}

    cli_opts = merged.get("cli_opts", []) if isinstance(merged, dict) else []
    is_client = any("-c" in str(o) for o in cli_opts)
    is_server = any("-s" in str(o) for o in cli_opts)

    # BUG-IPERF3: Resolve target_host/target_port from config_override for client mode
    co_raw = inst.get("config_override") or {}
    if isinstance(co_raw, str):
        try:
            import json as _json2
            co_raw = _json2.loads(co_raw)
        except Exception:
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
        except Exception:
            pass
    if not inv_hostname:
        return error_response("NO_HOSTNAME", f"No hostname resolved for node {node_id}")

  # Step 1: Deploy if needed (install iperf3, create systemd unit)
    sob = inst.get("start_on_boot") or False
    env = merged.get("env", {}) if isinstance(merged, dict) else {}
    binary_path = env.get("binary_path", "/usr/bin/iperf3")
    device = inst.get("gpu_device", "") or "CPU"

    extra_vars = {
        "inventory_host": inv_hostname,
        "instance_id": inst["id"],
        "instance_name": instance_name,
        "engine_type": engine_type_name,
        "instance_port": port or 0,
        "binary_path": binary_path,
        "device": device,
        "start_on_boot": False,  # Do not auto-start; we control lifecycle
        "restart_policy": env.get("restart_policy", "no"),
        "rpc_host": "0.0.0.0",
        "instance_env_vars": [],
        "gpu_device": device,
        "merged_env": env,
        "merged_cli_opts": cli_opts,
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
            transition_state(_CONFIG["db_path"], inst_id, "starting")
        except Exception:
            pass
        try:
            result = _run_manage_action(inst_id, engine_type_name, node_id, "start")
            if result.get("success"):
                transition_state(_CONFIG["db_path"], inst_id, "running")
            else:
                log_action(_CONFIG["db_path"], inst_id, "start", "failed",
                            detail={"remote": result})
                try:
                    transition_state(_CONFIG["db_path"], inst_id, "error")
                except Exception:
                    pass
                return error_response("DEPLOYMENT_FAILED",
                                f"Server start failed: {result.get('error', 'unknown')}")
        except Exception as exc:
            return error_response("DEPLOYMENT_FAILED", str(exc))

        log_action(_CONFIG["db_path"], inst_id, "start", "success")
        return success_single({"action": "run_client", "instance_id": inst_id,
                                "state": "running", "message": "Server started"})
    else:
        # Default: treat as server
        log_action(_CONFIG["db_path"], inst_id, "run_client", "success",
                    detail={"mode": "server_default"})
        return success_single({"action": "run_client", "instance_id": inst_id,
                                "state": "running", "message": "Started as server (no explicit -s/-c)"})


def api_toggle_test_mode(inst_id):
    """Toggle test mode on/off for an instance."""
    from db.adapters.instances import transition_state, log_action, get_instance
    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    if inst["state"] == "test_mode":
        new_state = inst.get("_original_state", "stopped")
        log_action(_CONFIG["db_path"], inst_id, "state_transition", "success",
                    detail={"action": "exit_test_mode"})
        updated = transition_state(_CONFIG["db_path"], inst_id, new_state)
    else:
        log_action(_CONFIG["db_path"], inst_id, "state_transition", "received",
                    detail={"action": "enter_test_mode"})
        try:
            updated = transition_state(_CONFIG["db_path"], inst_id, "test_mode")
        except Exception as exc:
            return error_response("INVALID_STATE", str(exc))

    return success_single({"action": "test_mode", "instance_id": inst_id,
                            "state": updated["state"]})


def api_update_log_level(inst_id):
    """Update the qr-log-level for an instance.

    This changes log forwarding without triggering a remote restart.
    Accepts level string: debug, info, warn, error.

    Args:
        inst_id: Integer instance ID.

    Returns:
        JSON response with updated config_override.
    """
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "Invalid JSON"))

    level = body.get("level")
    valid_levels = ("debug", "info", "warn", "error")
    if level not in valid_levels:
        return error_response("VALIDATION_ERROR",
                                f"Level must be one of: {', '.join(valid_levels)}")

    from db.adapters.instances import get_instance, update_instance as _ui
    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    current_override = inst.get("config_override", {}) or {}
    env_dict = current_override.get("env", {})
    env_dict["qr-log-level"] = level

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
        inv_script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                "..", "lib", "qr_dynamic_inventory.py")
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
    except Exception:
        return "idle"


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
        skip_build: If True, skip git clone/pull and cmake configure/build steps.
                    If None (default), auto-detect: check if compiled binary exists
                    on remote node. For llama_server/rpc: default to skipping build
                    when binary already present. For other engines: default False.

    Returns:
        dict with deployment result keys: success (bool), message (str). instance.
        playbook: Optional explicit playbook filename. If None, auto-detected
                    from engine_type_name (e.g., "deploy_llama_server.yml").
        async_mode: If True, run only sync preflight + git, then return.
                    The caller is responsible for starting async build thread.

    Returns:
        dict with deployment result keys: success (bool), message (str).
    """
    import os as _os
    from db.adapters.instances import get_instance as _gi
    from db.adapters.instances import transition_state, log_action as _log
    from lib.lib_ansible_runner import run_playbook
    import os as _os
    import re as _re

    inst = _gi(db_path, instance_id)
    if inst is None:
        return {"success": False, "message": f"Instance {instance_id} not found"}

    # SM-3: deploy deduplication — reject concurrent deploys of same instance
    cur_state = inst.get("state", "unknown")
    in_progress_states = ("configuring", "deploying", "compiling", "starting", "loading")
    if cur_state in in_progress_states:
        print(f"DEBUG GUARD: state={cur_state} blocked", flush=True)
        return {"success": False,
                "message": f"Instance already {cur_state} — deploy skipped (concurrent deploy in progress)"}

    engine_type_name = inst.get("engine_type_name", "llama_rpc")
    node_id = inst.get("node_id")
    node_name = inst.get("node_name", "")
    instance_name = inst.get("name", "")
    # Engine-specific default for start_on_boot — read from engine_configs (DB)
    sob_raw = inst.get("start_on_boot")
    if sob_raw is None or sob_raw == 0:
        from db.adapters.configs import get_engine_config as _gec
        _cfg = _gec(db_path, inst.get("engine_type_id"), "start_on_boot")
        _default_sob = _cfg.get("value", "false") if _cfg else "false"
        start_on_boot = _default_sob.lower() in ("true", "1", "yes")
    elif sob_raw == 1:
        start_on_boot = True
    else:
        start_on_boot = bool(sob_raw)

    # Build interlock: check node build state before deploying llama_server/rpc
    with _NODE_BUILD_LOCK:
        if engine_type_name in ("llama_server", "llama_rpc"):
            try:
                from db.sqlite import pool as _pool
                nd_state = "idle"
                with _pool(db_path) as _check_conn:
                    _row = _check_conn.execute(
                        "SELECT node_build_state FROM nodes WHERE id = ?", (node_id,)
                    ).fetchone()
                    if _row:
                        nd_state = _row[0] or "idle"
                if nd_state == "running":
                    return {"success": False,
                            "message": f"Node {node_name} has an active build (state=running). Deploy skipped."}
            except Exception:
                pass  # Non-critical — proceed even if check fails

    # Resolve inventory hostname (ansible_inventory_host > hostname > node_name)
    inv_hostname = None
    if node_id:
        try:
            from db.adapters.nodes import get_node as _gn
            nd = _gn(db_path, node_id)
            if nd:
                inv_hostname = (nd.get("ansible_inventory_host") or
                                nd.get("hostname") or
                                nd.get("name"))
        except Exception:
            pass
    if not inv_hostname:
        inv_hostname = node_name
    # --- Dynamic playbook selection ---
    engine_name = inst.get("engine_type_name", "llama_rpc")
    
    # Subprocess engine: no playbook needed — manages local process directly
    if inst.get("engine_type_id") == QR_ENGINE_SUBPROCESS:
        _log(db_path, instance_id, "deploy", "success")
        try:
            transition_state(db_path, instance_id, "configuring")
            transition_state(db_path, instance_id, "deploying")
            transition_state(db_path, instance_id, "deployed")
        except Exception:
            pass
        _log(db_path, instance_id, "deploy", "success")
        return {"success": True, "message": "Subprocess engine deployed (local process)",
                "task_summary": [], "duration_ms": 0}
    
    # Universal engine: use built-in deploy_universal.yml
    if engine_name == QR_ENGINE_UNIVERSAL:
        co = inst.get("config_override", {}) or {}
        co_merged = co if isinstance(co, dict) else {}

        # Resolve node hostname and IP for extra_vars
        inv_hostname = None
        if node_id:
            try:
                from db.adapters.nodes import get_node as _gn
                nd = _gn(db_path, node_id)
                if nd:
                    inv_hostname = (nd.get("ansible_inventory_host") or
                                    nd.get("hostname") or
                                    nd.get("name"))
            except Exception:
                pass

        # Build extra_vars from config_override
        instance_name = inst.get("name", f"universal-{instance_id}")
        install_dir = co_merged.get("install_dir") or _os.path.join("/opt/quickrobot", instance_name)
        venv_python = _os.path.join(install_dir, "venv/bin/python")
        extra_vars = {
            "inventory_host": inv_hostname,
            "instance_id": instance_id,
            "instance_name": instance_name,
            "install_dir": install_dir,
            "git_url": co_merged.get("git_url", ""),
            "requirements_file": co_merged.get("requirements_file", "requirements.txt"),
            "start_command": co_merged.get("start_command", ""),
            "binary_path": co_merged.get("binary_path") or venv_python,
            "cli_args": co_merged.get("cli_args") or [],
            "env_vars": co_merged.get("env_vars") or {},
            "user": co_merged.get("user") or (nd.get("ansible_user") if nd else None) or DEFAULT_ANSIBLE_USER,
            "start_after_deploy": bool(co_merged.get("start_after_deploy", False)),
            "restart_policy": co_merged.get("restart_policy", "no"),
            "start_on_boot": bool(co_merged.get("start_on_boot", False)),
            "instance_uuid": inst.get("instance_uuid", ""),
            "deploy_port": co_merged.get("deploy_port") or inst.get("port_assigned") or 0,
        }

        # Resolve host/port for {host}/{port} template substitution
        try:
            if node_id:
                from db.adapters.nodes import get_node as _gn
                nd = _gn(db_path, node_id)
                if nd:
                    ipv4 = nd.get("ipv4_address")
                    extra_vars["host"] = (ipv4.strip() if isinstance(ipv4, str) else "127.0.0.1") if ipv4 else "127.0.0.1"
                extra_vars["port"] = co_merged.get("deploy_port") or inst.get("port_assigned") or 0
        except Exception:
            extra_vars["host"] = "127.0.0.1"
            extra_vars["port"] = 0

        # Resolve DEPLOY_UNIVERSAL_V1 playbook
        pb_id = _resolve_engine_playbook_id("deploy", "universal")
        if not pb_id:
            return {"success": False,
                    "message": "Deploy playbook not found in registry for universal engine"}

        # Transition states and run the deploy playbook
        try:
            transition_state(db_path, instance_id, "configuring")
            transition_state(db_path, instance_id, "deploying")
        except Exception as exc:
            _log(db_path, instance_id, "deploy", "failed", detail={"error": str(exc)})
            return {"success": False, "message": f"State transition failed: {exc}"}

        try:
            r = _execute_playbook(pb_id, resolver_type="playbook_id",
                                  limit=inv_hostname or node_name,
                                  extra_vars=extra_vars,
                                  action_type="deploy_instance")
            if r["error"]:
                try:
                    transition_state(db_path, instance_id, "error")
                except Exception:
                    pass
                return {"success": False,
                        "message": f"Deploy failed: {r['error']}"}

            # Deploy succeeded — check if service should be running
            start_after = extra_vars.get("start_after_deploy", False)
            if start_after:
                try:
                    transition_state(db_path, instance_id, "starting")
                except Exception:
                    pass
                try:
                    transition_state(db_path, instance_id, "running")
                except Exception:
                    pass
            else:
                try:
                    transition_state(db_path, instance_id, "deployed")
                except Exception:
                    pass

            _log(db_path, instance_id, "deploy", "success")
            return {"success": True, "message": "Universal engine deployed successfully",
                    "task_summary": [], "duration_ms": 0}
        except Exception as exc:
            try:
                transition_state(db_path, instance_id, "error")
            except Exception:
                pass
            _log(db_path, instance_id, "deploy", "failed", detail={"error": str(exc)})
            return {"success": False, "message": f"Deploy error: {exc}"}
    else:
       if playbook is None:
           pb_id = _resolve_engine_playbook_id("deploy", engine_name)
           if pb_id:
               playbook_path = pb_id
           else:
               return {"success": False,
                       "message": f"Playbook not found in registry for deploy+{engine_name}"}
       else:
            playbook_path = _os.path.join(_project_root, "playbooks", playbook)
            if not _os.path.exists(playbook_path):
                return {"success": False,
                        "message": f"Explicit playbook not found: {playbook_path}"}

    # --- Parse merged config (3-section format: env/cli_opts/model) ---
    # Use cluster env builder for llama_server/rpc (computes tensor_split, resolves RPC bindings)
    # Fall back to legacy merge_configs for other engines
    is_cluster_engine = engine_type_name in ("llama_server", "llama_rpc")
    if is_cluster_engine:
        try:
            from lib.lib_cluster_env_builder import build_llama_server_env, build_rpc_server_env
            if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
                cluster_result = build_llama_server_env(db_path, instance_id)
            elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                cluster_result = build_rpc_server_env(db_path, instance_id)
            env = cluster_result["env"]
            # Use cluster builder result for engine-specific values
            if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
                # cli_args from builder already includes RPC refs (e.g., -dev none,RPC0 --rpc ...)
                cli_opts = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []
            elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                # RPC engine: builder returns rpc-specific cli_args
                cli_opts = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []
        except Exception as exc:
            # Fallback to legacy merge_configs
            try:
                from db.adapters.instances import merge_configs as _mc_fallback
                merged = _mc_fallback(db_path, instance_id)
                env = merged.get("env", {}) if isinstance(merged, dict) else {}
                cli_opts = list(merged.get("cli_opts", [])) if isinstance(merged, dict) else []
            except Exception:
                env = {}
                cli_opts = []
    else:
        try:
            from db.adapters.instances import merge_configs as _mc_legacy
            merged = _mc_legacy(db_path, instance_id)
        except Exception as exc:
            merged = {"env": {}, "cli_opts": [], "model": {}}
        env = merged.get("env", {}) if isinstance(merged, dict) else {}
        cli_opts = list(merged.get("cli_opts", [])) if isinstance(merged, dict) else []

    # --- Inject iperf3 server listen port into cli_opts (only for server mode) ---
    if engine_type_name == "iperf3":
        has_server_mode = any(str(o) in ("-s", "--server") for o in cli_opts)
        port = inst.get("port_assigned")
        if has_server_mode and port:
            filtered = []
            skip_next = False
            for o in cli_opts:
                if skip_next:
                    skip_next = False
                    continue
                if str(o) == "-p":
                    skip_next = True
                    continue
                filtered.append(o)
            cli_opts = filtered + ["-p", str(port)]

    # --- Override LLAMA_ARG_PORT with port_override or port_assigned ---
    port = inst.get("port_override") or inst.get("port_assigned")
    try:
        port = int(port) if port else 0
        if port > 0 and env:
            env["LLAMA_ARG_PORT"] = str(port)
    except (ValueError, TypeError):
        pass

    # --- Validate port ---
    is_universal = engine_name == QR_ENGINE_UNIVERSAL
    if not is_universal:
        try:
            port = int(port) if port else 0
            if port < 1 or port > 65535:
                return {"success": False, "message": f"Invalid port: {port}"}
        except (ValueError, TypeError):
            return {"success": False, "message": f"Invalid port value: {port}"}

    # --- Resolve rpc_host (IPv4-first, IPv6 fallback) ---
    co = inst.get("config_override", {}) or {}
    co_merged_env = co.get("env", co) if isinstance(co, dict) else (co if isinstance(co, dict) else {})
    rpc_host = env.get("LLAMA_ARG_HOST") or co_merged_env.get("LLAMA_ARG_HOST") or "0.0.0.0"
    if rpc_host == "0.0.0.0" and node_id:
        try:
            from db.adapters.nodes import get_node as _gn
            nd = _gn(db_path, node_id)
            if nd:
                # Prefer explicit columns set by migration 040
                ipv4_from_col = nd.get("ipv4_address")
                ipv6_from_col = nd.get("ipv6_address")
                if ipv4_from_col:
                    rpc_host = ipv4_from_col.strip() if isinstance(ipv4_from_col, str) else ipv4_from_col
                elif ipv6_from_col:
                    rpc_host = ipv6_from_col.strip() if isinstance(ipv6_from_col, str) else ipv6_from_col
                else:
                    # Fallback: parse from available_devices (legacy, IPv4-only)
                    devices_raw = nd.get("available_devices", []) or []
                    if isinstance(devices_raw, str):
                        import json as _j
                        devices_raw = _j.loads(devices_raw) if devices_raw else []
                    device_text = "\n".join(str(d) for d in devices_raw)
                    # Try IPv4 first, then IPv6
                    ip_v4_matches = _re.findall(r'inet\s+(\d{1,3}(\.\d{1,3}){3})/', device_text)
                    ip_v6_matches = _re.findall(r'inet6\s+([0-9a-fA-F:]+)/', device_text)
                    if ip_v4_matches:
                        rpc_host = ip_v4_matches[0][0]
                    elif ip_v6_matches:
                        rpc_host = ip_v6_matches[0][0]
        except Exception:
            pass
    if rpc_host != "0.0.0.0" and rpc_host:
        _host_str = str(rpc_host)
        # Accept both IPv4 and IPv6 addresses
        if not (_re.match(r'^\d{1,3}(\.\d{1,3}){3}$', _host_str) or
                _re.match(r'^[0-9a-fA-F:]+$', _host_str)):
            return {"success": False, "message": f"Invalid host: {rpc_host}"}

    # --- Extract per-instance env vars ---
    instance_env_vars = co.get("env_vars", []) if isinstance(co, dict) else []
    if not isinstance(instance_env_vars, list):
        instance_env_vars = [str(instance_env_vars)] if instance_env_vars else []

    # Resolve engine-level build vars
    try:
        from db.adapters.configs import get_engine_config as _gec
        gc = _gec(db_path, inst.get("engine_type_id")) or {}
        node_git_pull_cmd = gc.get("node_git_pull_cmd", {}).get("value")
        node_build_set_cmd = gc.get("node_build_set_cmd", {}).get("value")
        node_build_run_cmd = gc.get("node_build_run_cmd", {}).get("value")
    except Exception:
        node_git_pull_cmd = None
        node_build_set_cmd = None
        node_build_run_cmd = None

   # Wrap IPv6 in brackets for llama.cpp -H flag (RFC 3986)
    # Only applies to pure IPv6 (contains colons, no dots) — preserves IPv4 and hostnames as-is
    if rpc_host != "0.0.0.0" and rpc_host:
        _host_check = str(rpc_host)
        if ":" in _host_check and "." not in _host_check:
            rpc_host = f"[{_host_check}]"
            # Update the merged env so the env file gets the bracket-wrapped value
            if isinstance(env, dict) and "LLAMA_ARG_HOST" in env:
                env["LLAMA_ARG_HOST"] = rpc_host

    # Build extra_vars
    # Instance-level restart_policy overrides engine config default
    restart_policy_val = inst.get("restart_policy") or env.get("restart_policy", "no")

    # Extract model path from merged env for preflight check (llama_server only)
    model_path = env.get("LLAMA_ARG_MODEL", "") if isinstance(env, dict) else ""

    extra_vars = {
        "inventory_host": inv_hostname,
        "instance_id": inst["id"],
        "instance_name": instance_name,
        "engine_type": engine_type_name,
        "instance_port": port,
        "binary_path": env.get("binary_path", ""),
        "start_on_boot": bool(start_on_boot),
       "start_after_deploy": bool(inst.get("start_after_deploy", 0)),
        "restart_policy": restart_policy_val,
        "rpc_host": rpc_host,
        "instance_env_vars": instance_env_vars,
        "merged_env": env,
        "merged_cli_opts": cli_opts,
        "model_path": model_path,
        "node_git_pull_cmd": node_git_pull_cmd or "git pull origin master",
        "git_clone_url": gc.get("git_clone_url", {}).get("value") or "https://github.com/ggml-org/llama.cpp.git",
        "node_build_set_cmd": node_build_set_cmd or "cmake -B build -DGGML_RPC=ON -DGGML_NATIVE=ON -DGGML_CPU=ON -DLLAMA_OPENSSL=ON -DGGML_AVX2=ON -DGGML_VULKAN=ON",
        "node_build_run_cmd": node_build_run_cmd or "cmake --build build --config Release -j 2",
        "node_build_state": _get_node_build_state(db_path, node_id),
        "instance_uuid": inst.get("instance_uuid", ""),
    }
     # --- Cluster binding: use builder results for llama_server/rpc ---
    if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
        try:
            from lib.lib_cluster_env_builder import build_llama_server_env as _cls
            cluster_result = _cls(db_path, instance_id)
            extra_vars["merged_env"] = cluster_result["env"]
            extra_vars["merged_cli_opts"] = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []
            extra_vars["tensor_split_value"] = cluster_result["tensor_split_str"]
            extra_vars["split_mode_value"] = cluster_result["split_mode"]
            extra_vars["rpc_bind_ids"] = [b["id"] for b in cluster_result["rpc_bindings"]]
            extra_vars["rpc_instances_by_id"] = {
                b["id"]: {"hostname": b["hostname"], "port": b["port_assigned"], "split": b["split"]}
                for b in cluster_result["rpc_bindings"]
            }
        except Exception as _e:
            print(f"DEBUG: builder failed for instance {instance_id}: {_e}", flush=True)
            extra_vars["merged_env"] = {}
            extra_vars["merged_cli_opts"] = []
            extra_vars["tensor_split_value"] = str(inst.get("split") or 100)
            extra_vars["split_mode_value"] = inst.get("split_mode") or "layer"
            extra_vars["rpc_bind_ids"] = []
            extra_vars["rpc_instances_by_id"] = {}
    elif engine_type_name == QR_ENGINE_LLAMA_RPC_NAME and is_cluster_engine:
        try:
            from lib.lib_cluster_env_builder import build_rpc_server_env as _cls_rpc
            cluster_result = _cls_rpc(db_path, instance_id)
            extra_vars["merged_env"] = cluster_result["env"]
            extra_vars["merged_cli_opts"] = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []
        except Exception:
            pass
    # Validate host format if non-default (accepts IPv4 + IPv6 + bracket-wrapped IPv6)
    if rpc_host != "0.0.0.0" and rpc_host:
        _host_str = str(rpc_host)
        # Accept plain IPv4, plain IPv6, or bracket-wrapped IPv6
        _is_valid = (_re.match(r'^\d{1,3}(\.\d{1,3}){3}$', _host_str) or
                     _re.match(r'^[0-9a-fA-F:]+$', _host_str) or
                     _re.match(r'^\[([0-9a-fA-F:]+)\]$', _host_str))
        if not _is_valid:
            return {"success": False, "message": f"Invalid host: {rpc_host}"}

    # --- Extract per-instance env vars from config_override ---
    instance_env_vars = co.get("env_vars", []) if isinstance(co, dict) else []
    if not isinstance(instance_env_vars, list):
        instance_env_vars = [str(instance_env_vars)] if instance_env_vars else []

    # Resolve engine-level build vars (git_pull, cmake set, cmake run, paths)
    try:
        from db.adapters.configs import get_engine_config as _gec
        gc = _gec(db_path, inst.get("engine_type_id")) or {}
        node_git_pull_cmd = gc.get("node_git_pull_cmd", {}).get("value")
        node_build_set_cmd = gc.get("node_build_set_cmd", {}).get("value")
        node_build_run_cmd = gc.get("node_build_run_cmd", {}).get("value")
        node_src_dir = gc.get("node_src_dir", {}).get("value")
        node_build_dir = gc.get("node_build_dir", {}).get("value")
    except Exception:
        node_git_pull_cmd = None
        node_build_set_cmd = None
        node_build_run_cmd = None
        node_src_dir = None
        node_build_dir = None

    # Build extra_vars with merged config for ansible
    device = co.get("device", "") or inst.get("gpu_device", "") or "CPU"
    # Per-engine restart policy (from engine_configs via merge chain)
    restart_policy_val = env.get("restart_policy", "no")

    extra_vars = {
        "inventory_host": inv_hostname,
        "instance_id": inst["id"],
        "instance_name": instance_name,
        "engine_type": engine_type_name,
        "instance_port": port,
        "binary_path": env.get("binary_path", ""),
        "device": device,
        "start_on_boot": bool(start_on_boot),
        "start_after_deploy": bool(inst.get("start_after_deploy", 0)),
        # Per-engine restart policy (from engine_configs via merge chain)
        "restart_policy": restart_policy_val,
        # RPC host resolution (merged_env > node available_devices > default)
        "rpc_host": rpc_host,
        # Per-instance systemd env vars
        "instance_env_vars": instance_env_vars,
        # New vars from merged config (3-section format)
        "gpu_device": device,
        "merged_env": env,
        "merged_cli_opts": cli_opts,
        # Node-level build commands (from engine_configs)
        "node_git_pull_cmd": node_git_pull_cmd or "git pull origin master",
         "git_clone_url": gc.get("git_clone_url", {}).get("value") or "https://github.com/ggml-org/llama.cpp.git",
         "node_build_set_cmd": node_build_set_cmd or "cmake -B build -DGGML_RPC=ON -DGGML_NATIVE=ON -DGGML_CPU=ON -DLLAMA_OPENSSL=ON -DGGML_AVX2=ON -DGGML_VULKAN=ON",
         # Per-instance override: config_override.env.node_build_run_cmd takes priority over engine default
         "node_build_run_cmd": (co.get("env", {}).get("node_build_run_cmd") if isinstance(co, dict) else None) or node_build_run_cmd or "cmake --build build --config Release -j 2",
         # Node-level build paths (from engine_configs)
         "node_src_dir": node_src_dir or "/opt/quickrobot/llama.cpp",
         "node_build_dir": node_build_dir or "/opt/quickrobot/llama.cpp/build",
        # Node-level build state (from nodes table)
        "node_build_state": _get_node_build_state(db_path, node_id),
        # UUID for collision prevention (AGENTS.md §9)
        "instance_uuid": inst.get("instance_uuid", ""),
        # Remote node user (from node ansible_user — ensures git/cmake run as user, not root)
         "remote_node_user": nd.get("ansible_user") or DEFAULT_ANSIBLE_USER,
         # Cluster binding fields (llama_server)
         "tensor_split_value": "",
         "split_mode_value": "layer",
     }

    # --- Cluster binding: resolve RPC instances and tensor split ---
    if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
        try:
            from db.adapters.instances import get_instance as _gi_rpc
            from lib.lib_cluster_env_builder import build_llama_server_env as _cls2

            cluster_result = _cls2(db_path, instance_id)
            extra_vars["merged_env"] = cluster_result["env"]
            extra_vars["merged_cli_opts"] = [s for s in cluster_result["cli_args"].split()] if cluster_result["cli_args"] else []
            extra_vars["tensor_split_value"] = cluster_result["tensor_split_str"]
            extra_vars["split_mode_value"] = cluster_result["split_mode"]

            raw_bind = inst.get("rpc_bind_ids") or "[]"
            bind_ids = json.loads(raw_bind) if isinstance(raw_bind, str) else list(raw_bind or [])

            # Resolve each RPC instance's metadata including split value
            rpc_map = {}
            for rid in bind_ids:
                ri = _gi_rpc(db_path, int(rid))
                if ri:
                    rpc_map[int(rid)] = {
                        "hostname": ri.get("node_hostname") or "",
                        "port": ri.get("port_assigned") or 0,
                        "split": ri.get("split") or 0,
                    }

            extra_vars["rpc_bind_ids"] = bind_ids
            extra_vars["split_value"] = inst.get("split") or 100
            extra_vars["rpc_instances_by_id"] = rpc_map
        except Exception:
            # Non-critical — deploy proceeds without RPC bindings
            extra_vars["rpc_bind_ids"] = []
            extra_vars["split_mode_value"] = "layer"
            extra_vars["split_value"] = 0
            extra_vars["tensor_split_value"] = str(inst.get("split") or 100)


    # Dynamic inventory — no file generated (DI-7)
    try:
        _script_dir = _os.path.dirname(_os.path.abspath(__file__))
        _inv_script = _os.path.join(_script_dir, "lib", "qr_dynamic_inventory.py")
    except Exception as exc:
        return {"success": False, "message": f"Inventory setup failed: {exc}"}

    # Preflight UUID check before deploy
    uuid_result = check_remote_uuids(db_path, instance_id)
    for w in uuid_result.get("warnings", []):
        _log(db_path, instance_id, "uuid_check", "warning", detail={"message": w})

    # llama_server/rpc: read skip_build from engine_config, fallback to playbook auto-detect
    # Other engines: default to building unless explicitly told otherwise
    if skip_build is None and engine_type_name in ("llama_server", "llama_rpc"):
        try:
            from db.adapters.configs import get_engine_config as _gec
            ec = _gec(db_path, inst.get("engine_type_id")) or {}
            sv_raw = ec.get("skip_build")
            sv = sv_raw["value"] if isinstance(sv_raw, dict) and "value" in sv_raw else str(sv_raw) if sv_raw else ""
            if str(sv).lower() in ("true", "1"):
                skip_build = True
        except Exception:
            pass  # Non-critical — playbook will auto-detect
    if skip_build is None and engine_type_name not in ("llama_server", "llama_rpc"):
        skip_build = False

    # Ensure extra_vars has the (possibly auto-detected) skip_build value
    if skip_build is not None:
        extra_vars["skip_build"] = bool(skip_build)

    # Ensure nd is available for sudo error message
    if node_id and not isinstance(nd, dict):
        try:
            from db.adapters.nodes import get_node as _gn
            nd = _gn(db_path, node_id)
        except Exception:
            nd = None
    sudo_user = (nd.get("ansible_user") if isinstance(nd, dict) and nd.get("ansible_user") else None) or DEFAULT_ANSIBLE_USER

    # Preflight sudoers check — verify the ansible user can run systemctl daemon-reload as root
    import subprocess as _sub
    try:
        sudo_test = _sub.run(
            ["ansible", inv_hostname, "-i", _inv_script,
                "-m", "shell",
                "-a", "sudo systemctl daemon-reload 2>&1; echo \"exit=$?\"",
                "-b"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"},
        )
        # Debug: write preflight result to file for inspection
        with open("/tmp/preflight_result.txt", "w") as _f:
            _f.write(f"inv_hostname={inv_hostname}\n")
            _f.write(f"_inv_script={_inv_script}\n")
            _f.write(f"rc={sudo_test.returncode}\n")
            _f.write(f"stdout={sudo_test.stdout!r}\n")
            _f.write(f"stderr={sudo_test.stderr!r}\n")
        if sudo_test.returncode != 0:
            # For localhost (ansible_connection=local), sudo may not be configured but
            # the machine is the same — warn instead of failing
            err = (sudo_test.stderr or sudo_test.stdout).strip()
            if node_id == 1 or inv_hostname == "localhost":
                _log(db_path, instance_id, "preflight", "warning",
                        detail={"message": f"Sudo not configured on localhost: {err}"})
            else:
                _log(db_path, instance_id, "preflight", "error",
                        detail={"message": f"Sudo not configured on {inv_hostname}: {err}"})
                return {"success": False,
                            "message": f"Sudo access check failed on {inv_hostname}. "
                                f"Ensure passwordless sudo is configured for the SSH user ({sudo_user})."}
    except _sub.TimeoutExpired:
        return {"success": False, "message": f"Preflight sudo test timed out on {inv_hostname}"}
    except Exception as exc:
        _log(db_path, instance_id, "preflight", "warning",
                detail={"message": f"Sudo test skipped: {exc}"})

    # --- Git clone/pull (sync phase — fast for existing repo) ---
    # Only needed for llama_server and rpc engines using shared build dirs.
    # When skip_build=True and binary exists → skip git/build in Python (playbook handles)
    # When skip_build=True but binary missing → force full build here
    # When skip_build=False → always do full build here.
    if engine_type_name in ("llama_server", "llama_rpc"):
        _force_build = False
        if skip_build:
            # Check if prebuilt binary exists on remote node
            import subprocess as _sub_bin
            try:
                bin_check = _sub_bin.run(
                    ["ansible", inv_hostname, "-i", _inv_script,
                        "-m", "shell", "-a", f"test -f /opt/quickrobot/llama.cpp/build/bin/llama-server 2>/dev/null || test -f /opt/quickrobot/llama.cpp/build/bin/rpc-server 2>/dev/null && echo YES || echo NO"],
                    capture_output=True, text=True, timeout=10,
                )
                _force_build = "NO" in (bin_check.stdout or "")
            except Exception:
                _force_build = True  # Can't check — assume build needed
        if _force_build or not skip_build:
            _log(db_path, instance_id, "preflight", "info",
                    detail={"message": f"Binary check: force_build={_force_build} (git clone/pull handled by playbook)"})

    # Async mode: preflight only, return immediately
    if async_mode:
        _log(db_path, instance_id, "deploy", "preflight_ok")
        try:
            transition_state(db_path, instance_id, "configuring")
        except Exception:
            pass
        return {"success": True, "message": "Preflight passed (async mode)",
                "status": "configuring"}

    # Set node build state to running (prevents concurrent builds on same node)
    with _NODE_BUILD_LOCK:
        if engine_type_name in ("llama_server", "llama_rpc"):
            try:
                from db.sqlite import pool as _pool
                with _pool(db_path) as conn:
                    conn.execute("UPDATE nodes SET node_build_state = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (node_id,))
            except Exception:
                pass  # Non-critical — deploy proceeds even if state update fails

    # Transition to configuring BEFORE running the (potentially long) playbook
    try:
        transition_state(db_path, instance_id, "configuring")
    except Exception:
        pass  # Non-critical — deploy proceeds even if state update fails

    # Run deploy playbook
    try:
        import traceback as _tb
        if extra_vars is None:
            print("ERROR: extra_vars is None!", flush=True)
        pb_id = _resolve_engine_playbook_id("deploy", engine_type_name)

        r = _execute_playbook(pb_id, resolver_type="playbook_id",
                               limit=inv_hostname, extra_vars=extra_vars,
                               instance_id=instance_id, node_id=node_id,
                               action_type="deploy_instance")
        if r["error"]:
            return {"success": False, "message": f"Deploy failed: {r['error']}"}
        result = r.get("result") or {}

        # Universal task-level result extraction (works for ALL engine types)
        def _extract_task_summary(result_data):
            """Extract per-task status from parsed Ansible output."""
            tasks = []
            plays = result_data.get("results", {}).get("plays", [])
            start_time = None
            end_time = None

            for play in plays:
                # Capture play duration if available
                play_duration = play.get("play", {}).get("duration")
                if isinstance(play_duration, dict):
                    secs = int(play_duration.get("seconds", 0))
                    if start_time is None:
                        end_time = secs
                    else:
                        end_time += secs

                for task in play.get("tasks", []):
                    task_info = task.get("task", {})
                    task_name = task_info.get("name", "unknown")
                    # Check per-host results (Ansible 2.10+ format)
                    failed = False
                    changed = False
                    for entry in task.get("results", []):
                        if entry.get("changed", False):
                            changed = True
                        if entry.get("failed", False):
                            failed = True
                    status = "failed" if failed else ("changed" if changed else "ok")

                    # Extract error message for failed tasks
                    error_msg = ""
                    for entry in task.get("results", []):
                        msg = entry.get("msg", "")
                        if isinstance(msg, dict):
                            error_msg = json.dumps(msg)
                        elif isinstance(msg, str) and msg.strip():
                            error_msg = msg
                        if error_msg:
                            break

                    tasks.append({
                        "name": task_name,
                        "status": status,
                        "error": error_msg,
                    })

            return tasks, end_time or 0

        try:
            task_summary, duration_ms = _extract_task_summary(result)
        except RecursionError:
            # Jinja2 recursion during template rendering — config was likely applied successfully
            task_summary = [{"name": "task (recursion prevented summary)", "status": "ok", "error": ""}]
            duration_ms = 0
        except Exception as _e:
            task_summary = [{"name": "unknown task", "status": "ok", "error": str(_e)}]
            duration_ms = 0

        # Compute relative playbook path for counter tracking (needed in both success and error paths)
        pb_rel = f"playbooks/{playbook}" if playbook and "/" not in playbook else playbook

        failed = result.get("failed", False)
        r_err = r.get("error") or "N/A"
        if failed:
            _log(db_path, instance_id, "deploy", "failed")
            # Transition to error state
            try:
                transition_state(db_path, instance_id, "error")
            except Exception:
                pass
            # Reset node build state to idle on failure
            if engine_type_name in ("llama_server", "llama_rpc"):
                try:
                    from db.sqlite import pool as _pool
                    with _pool(db_path) as conn:
                        conn.execute("UPDATE nodes SET node_build_state = 'idle', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (node_id,))
                except Exception:
                    pass

            # Find first failed task for actionable error message
            first_error = ""
            for t in task_summary:
                if t["status"] == "failed":
                    first_error = f"{t['name']}: {t['error']}"
                    break
            return {"success": False,
                    "message": first_error or "Deploy playbook reported failures",
                    "task_summary": task_summary,
                    "duration_ms": duration_ms}

        # Transition through configuring → deploying → deployed (state machine)
        try:
            transition_state(db_path, instance_id, "configuring")
        except Exception:
            pass
        try:
            transition_state(db_path, instance_id, "deploying")
        except Exception:
            pass
        try:
            transition_state(db_path, instance_id, "deployed")
        except Exception:
            pass

        # llama_server/rpc post-deploy: leave in "deployed" state.
        # The health check cycle will detect the running service and transition to "running".
        # If start_after_deploy is set, the systemd unit starts the service via playbook
        # (start_after_deploy task in deploy_llama_server.yml), and the health check
        # picks it up. No auto-transition here — avoids confusing UI state flips.

        # Post-deploy state transition: handle start_after_deploy
        if inst.get("start_after_deploy", 0):
            try:
                transition_state(db_path, instance_id, "starting")
            except Exception:
                pass
            # Verify service started by checking systemd status on remote
            try:
                import subprocess as _sub3
                # Dynamic inventory — no file generated (DI-7)
                _inv_script2 = _os.path.join(_script_dir, "lib", "qr_dynamic_inventory.py")
                svc_check = _sub3.run(
                    ["ansible", inv_hostname, "-i", _inv_script2,
                        "-b", "-m", "shell",
                        f"-a", f"systemctl is-active {{unit_name}}"],
                    capture_output=True, text=True, timeout=10,
                )
                if "active" in (svc_check.stdout or "").lower():
                    try:
                        transition_state(db_path, instance_id, "running")
                    except Exception:
                        pass
                else:
                    _log(db_path, instance_id, "deploy", "warning",
                            detail={"message": f"start_after_deploy=true but service not active"})
            except Exception as exc:
                _log(db_path, instance_id, "deploy", "warning",
                        detail={"message": f"Service check failed: {exc}"})

        _log(db_path, instance_id, "deploy", "success")

        # Extract and record build commit hash from playbook task results
        try:
            commit_hash = None
            for play in result.get("results", {}).get("plays", []):
                for task in play.get("tasks", []):
                    hosts = task.get("hosts", {})
                    for host_data in hosts.values():
                         msg = host_data.get("msg", "") or ""
                         if "BUILD_COMMIT=" in msg:
                             commit_hash = msg.split("BUILD_COMMIT=")[1].split("|")[0].strip()
                             break
                    if commit_hash:
                        break
                if commit_hash:
                    break
            if commit_hash:
                from db.sqlite import pool as _pool
                with _pool(db_path) as conn:
                    conn.execute("UPDATE instances SET build_number=? WHERE id=?",
                                (commit_hash, instance_id))
        except Exception:
            pass  # Non-critical — build number tracking failure doesn't break deploy

   # _execute_playbook handles all logging (starting/success/error) — single logging point

        # Reset node build state to idle
        if engine_type_name in ("llama_server", "llama_rpc"):
            try:
                from db.sqlite import pool as _pool
                with _pool(db_path) as conn:
                    conn.execute("UPDATE nodes SET node_build_state = 'idle', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (node_id,))
            except Exception:
                pass

        return {"success": True, "message": "Deploy succeeded",
                "task_summary": task_summary,
                "duration_ms": duration_ms,
                "uuid_mismatches": uuid_result.get("mismatches", []),
                "uuid_stray": uuid_result.get("stray_units", [])}

    except Exception as exc:
        _log(db_path, instance_id, "deploy", "failed", detail={"error": str(exc)})
        # _execute_playbook already logs error case — no duplicate needed
        return {"success": False, "message": str(exc)}


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


def api_instance_status(inst_id):
    """Lightweight status check (state, port, uptime)."""
    from db.adapters.instances import get_instance
    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    return success_single({
        "instance_id": inst_id,
        "state": inst["state"],
        "port_assigned": inst.get("port_assigned"),
        "uptime_seconds": inst.get("uptime_seconds", 0),
        "last_state_change": inst.get("last_state_change"),
    })


def api_instance_health(inst_id):
    """Health probe (checks if instance endpoint is reachable)."""
    from db.adapters.instances import get_instance
    inst = get_instance(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    port = inst.get("port_assigned")
    health = {"instance_id": inst_id, "reachable": False, "latency_ms": 0}

    if port and inst["state"] in ("running",):
        try:
            import urllib.request
            url = f"http://127.0.0.1:{port}/health"
            start = __import__("time").time()
            req = urllib.request.urlopen(url, timeout=3)
            latency = (__import__("time").time() - start) * 1000
            health["reachable"] = True
            health["latency_ms"] = round(latency, 2)
        except Exception:
            health["reachable"] = False

    return success_single(health)


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
        except Exception:
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
            result = s.connect_ex(("127.0.0.1", info["port"]))
            if result == 0:
                pass  # Port is open, confirmed
            else:
                # Try common fallback ports
                for fallback_port in [8040, 8080, 5000]:
                    if fallback_port != info["port"]:
                        s2 = _sock2.socket(_sock2.AF_INET, _sock2.SOCK_STREAM)
                        r2 = s2.connect_ex(("127.0.0.1", fallback_port))
                        if r2 == 0:
                            info["port"] = fallback_port
                            break
                        s2.close()
        except Exception:
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
        except Exception:
            pass
        return success_single(status)

    elif engine_type == QR_ENGINE_MCP_NAME:
        from engine import get_engine as _ge
        mcp_engine = _ge("quickrobot-mcp")
        if mcp_engine:
            try:
                status_data = mcp_engine.get_status(inst_id, _CONFIG["db_path"])
            except Exception:
                status_data = {"engine_type": "quickrobot-mcp", "info": {}}
        else:
            status_data = {"engine_type": "quickrobot-mcp", "info": {}}
        return success_single(status_data)

    return success_single({"engine_type": engine_type, "info": {}})

def api_query_status(inst_id):
    """Remote status query for a single instance.

    Calls the engine's query_status() method to check if the remote
    instance is reachable and alive. Updates the DB state from
    remote results.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        JSON with keys: alive (bool), latency_ms (float|None),
        error (str|None), new_state (str|None).
    """
    from db.adapters.instances import get_instance as _gi
    from engine import get_engine, load_engines

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    engine_type = inst.get("engine_type_name", "")

    # Try direct lookup first (e.g., "quickrobot-api", "qr_api")
    engine = get_engine(engine_type)
    if engine is None:
        alt_name = engine_type.replace("-", "_")
        engine = get_engine(alt_name)
    if engine is None:
        alt_name = engine_type.replace("_", "-")
        engine = get_engine(alt_name)

    if engine:
        result = engine.query_status(inst_id, _CONFIG["db_path"])
    else:
        result = {"alive": False, "latency_ms": None, "error": f"Engine '{engine_type}' not loaded"}

    from db.adapters.instances import update_instance as _ui, transition_state as _ts
    from datetime import datetime as _dt, timezone as _tz

    cur_state = inst.get("state", "unknown")
    new_state = None

    # Model loading detection: transition starting → loading when health check sees loading message.
    if result.get("model_loading") and cur_state not in ("loading",):
        try:
            _ts(_CONFIG["db_path"], inst_id, "loading")
            new_state = "loading"
            result["new_state"] = "loading"
        except Exception:
            pass

    # Loading complete: transition loading → running when model is ready.
    elif result.get("alive") and not result.get("model_loading") and cur_state == "loading":
        try:
            _ts(_CONFIG["db_path"], inst_id, "running")
            new_state = "running"
            result["new_state"] = "running"
        except Exception:
            pass

    # Startup complete: transition starting → running when health check confirms alive (model loaded).
    elif result.get("alive") and not result.get("model_loading") and cur_state == "starting":
        try:
            _ts(_CONFIG["db_path"], inst_id, "running")
            new_state = "running"
            result["new_state"] = "running"
        except Exception:
            pass

    # Deployed/stopped auto-detect: transition to running when health check confirms alive.
    # Covers instances deployed with start_after_deploy=false that were started externally
    # or manually, and stopped instances that were restarted outside the API flow.
    elif result.get("alive") and not result.get("model_loading") and cur_state in ("deployed", "stopped"):
        try:
            _ts(_CONFIG["db_path"], inst_id, "running")
            new_state = "running"
            result["new_state"] = "running"
        except Exception:
            pass

    # Recovery from updating/build_error: transition to running when health check confirms alive.
    # Prevents instances from staying stuck in transient states indefinitely.
    elif result.get("alive") and not result.get("model_loading") and cur_state in ("updating", "build_error"):
        try:
            _ts(_CONFIG["db_path"], inst_id, "running")
            new_state = "running"
            result["new_state"] = "running"
        except Exception:
            pass

    # Recovery from error/build_error: transition to running when health check confirms alive.
    # Also recovers from build_error after a failed deploy if the service is actually running.
    elif result.get("alive") and not result.get("model_loading") and cur_state in ("error", "build_error"):
        try:
            _ts(_CONFIG["db_path"], inst_id, "running")
            new_state = "running"
            result["new_state"] = "running"
        except Exception:
            pass

    # Crash detection: running → error, updating/build_error → error, or loading → error when health check fails.
    # Grace period: skip crash detection if instance just changed state (prevents false errors
    # during large model loading when server doesn't respond to health checks yet).
    elif not result.get("alive") and cur_state in ("running", "updating", "build_error", "stopping", "loading"):
        lsc = inst.get("last_state_change", "")
        # Build error reason message for logging
        _error_reason = (result.get("error", "") or
                         f"Health check failed: {cur_state} → error")[:500]
        if lsc:
            try:
                from datetime import timezone as _tz_dt
                lsc_dt = _dt.fromisoformat(lsc.replace("Z", "+00:00"))
                since = (_dt.now(_tz_dt) - lsc_dt).total_seconds()
                # 5-min grace for running/loading, 30-min for updating/build_error (long builds need it)
                grace_period = GRACE_PERIOD_RUNNING if cur_state in ("running", "loading") else 1800
                if since < grace_period:  # skip crash-detect during recovery
                    pass
                else:
                    try:
                        _ts(_CONFIG["db_path"], inst_id, "error")
                        new_state = "error"
                        result["new_state"] = "error"
                        # Log crash detection to qr_actions with reason
                        try:
                            from db.sqlite import pool
                            with pool(_CONFIG["db_path"]) as _crash_conn:
                                _crash_conn.execute(
                                    "INSERT INTO qr_actions (action_type, instance_id, actor, details, status, created_at) "
                                    "VALUES (?, ?, 'system', ?, 'failed', ?)",
                                    ("crash_detect", inst_id,
                                     json.dumps({"state_from": cur_state, "reason": _error_reason}),
                                     _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                                )
                        except Exception:
                            pass  # Non-critical
                    except Exception:
                        pass
            except (ValueError, TypeError):
                try:
                    _ts(_CONFIG["db_path"], inst_id, "error")
                    new_state = "error"
                    result["new_state"] = "error"
                    # Log crash detection to qr_actions with reason
                    try:
                        from db.sqlite import pool
                        with pool(_CONFIG["db_path"]) as _crash_conn:
                            _crash_conn.execute(
                                "INSERT INTO qr_actions (action_type, instance_id, actor, details, status, created_at) "
                                "VALUES (?, ?, 'system', ?, 'failed', ?)",
                                ("crash_detect", inst_id,
                                 json.dumps({"state_from": cur_state, "reason": _error_reason}),
                                 _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                            )
                    except Exception:
                        pass
                except Exception:
                    pass
        else:
            try:
                _ts(_CONFIG["db_path"], inst_id, "error")
                new_state = "error"
                result["new_state"] = "error"
                # Log crash detection to qr_actions with reason
                try:
                    from db.sqlite import pool
                    with pool(_CONFIG["db_path"]) as _crash_conn:
                        _crash_conn.execute(
                            "INSERT INTO qr_actions (action_type, instance_id, actor, details, status, created_at) "
                            "VALUES (?, ?, 'system', ?, 'failed', ?)",
                            ("crash_detect", inst_id,
                             json.dumps({"state_from": cur_state, "reason": _error_reason}),
                             _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                        )
                except Exception:
                    pass
            except Exception:
                pass

    # Update last_state_change timestamp on every explicit query (WebUI refresh or API call).
    # Keeps "Last Info" column current for all instances, not just alive ones.
    try:
        _ui(_CONFIG["db_path"], inst_id, last_state_change=_dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        pass  # Best effort

    if new_state:
        result["new_state"] = new_state

    return success_single(result)


def api_proxy_remote(subpath):
    """Reverse proxy to a remote instance's web UI.

    Forwards requests to the remote instance (identified by node + port)
    and returns the response with CORS headers so it can be embedded in
    an iframe inside qr's WebUI.

    Args:
        subpath: Instance ID followed by path, e.g., '123/health' or '123/'.

    Returns:
        Proxied response with CORS headers.
    """
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return Response("", status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        })

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

    node_hostname = inst.get("node_hostname", "") or inst.get("node_name", "127.0.0.1")
    port = inst.get("port_assigned", 8080)

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

        # Add CORS headers for iframe embedding (always, on all responses)
        cors_headers = [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            ("Access-Control-Allow-Headers", "*"),
        ]
        clean_headers = []
        for k, v in resp_headers.items():
            kl = k.lower()
            if kl == "content-type":
                clean_headers.append((k, f"{v}; charset=utf-8"))
            elif kl == "content-length":
                continue  # let Flask set it
            else:
                clean_headers.append((k, v))
        clean_headers.extend(cors_headers)

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
        clean_headers.extend([
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            ("Access-Control-Allow-Headers", "*"),
        ])
        return Response(body, status=e.code, headers=dict(clean_headers))

    except Exception as exc:
        # Handle ProxyConnectionError with its descriptive message
        from lib.lib_proxy_reader import ProxyConnectionError as _PCE
        if isinstance(exc, _PCE):
            error_msg = str(exc)
        else:
            error_msg = f"Proxy error: {exc}"
        error_body = f'{{"status":"error","code":"PROXY_ERROR","message":"{error_msg}"}}'.encode()
        return Response(error_body, status=502, headers={
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        })


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
    import os as _os
    from db.adapters.nodes import get_node as _gn

    if node_id is None:
        return {"success": False, "error": "No target node"}

    nd = _gn(_CONFIG["db_path"], node_id)
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

  # _execute_playbook handles all logging (starting/success/error) — single logging point

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
        except Exception:
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

            except Exception:
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
        except Exception:
            client_log = "(unable to retrieve log)"

        # Parse iperf3 log output for throughput results
        parsed = _parse_iperf3_log(client_log)

        # Transition to deployed (client ran once and finished)
        try:
            transition_state(_CONFIG["db_path"], inst_id, "deployed")
        except Exception:
            pass

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


