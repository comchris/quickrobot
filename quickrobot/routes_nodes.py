"""Node route handlers for quickrobot.

All functions accept the same signatures as the originals from quickrobot.py.
Route registration is handled by __init__.py via app.add_url_rule().
"""

import json
import os
import threading
from flask import request, jsonify
from quickrobot.lib_responses import success_single, success_list, error_response, require_json
from quickrobot import _CONFIG, _project_root
from engine import get_engine, get_engine_capabilities, ENGINES
from lib.qr_engine_ids import (
    QR_ENGINE_API_NAME, QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC,
    QR_ENGINE_LLAMA_SERVER_NAME,
    QR_ENGINE_MCP_NAME, QR_ENGINE_SUBPROCESS, QR_ENGINE_UNIVERSAL,
    QR_ENGINE_WEBUI_NAME,
    is_llamacpp_engine, get_id_by_name,
)
from lib.lib_constants import VERSION, DEFAULT_ANSIBLE_USER, DEFAULT_TIMEZONE
from lib.lib_qr_actions import log_qr_action, log_qr_override
from quickrobot.lib_instances import (
     deploy_instance, _execute_playbook, check_remote_uuids,
     _start_async_build, _run_manage_action,
     BUILD_STATES, _check_node_active,
  )
from quickrobot.lib_nodes import _scan_orphaned_units, _get_node_build_state, find_system_instance as _fsi
from quickrobot.lib_instances import _NODE_BUILD_LOCK
from db.sqlite import pool as db_pool


def api_list_nodes():
    """List all nodes, optionally excluding inactive hosts."""
    from db.adapters.nodes import list_nodes as _ln
    from lib.lib_utils import relative_age
    from db.sqlite import pool
    show_inactive = request.args.get("include_inactive", "false").lower() == "true"
    nodes = _ln(_CONFIG["db_path"])
    # Filter out inactive hosts by default
    if not show_inactive:
        nodes = [n for n in nodes if n.get("is_active", 1)]
    # Add relative age and availability for user instances
    with pool(_CONFIG["db_path"]) as conn:
        sys_node_ids = {r[0] for r in conn.execute(
            "SELECT DISTINCT node_id FROM instances WHERE system_managed = 1"
        ).fetchall()}
    for node in nodes:
        # localhost (node_id=1) always active — it's the machine itself
        if node.get("id") == 1:
            node["status"] = "active"
        node["age_created"] = relative_age(node.get("created_at"))
        node["available_for_instances"] = node.get("id") not in sys_node_ids
    return success_list(nodes)


def api_create_node():
    """Create a new node entry."""
    from db.adapters.nodes import add_node, get_node as _gn
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    name = body.get("name")
    hostname = body.get("hostname")
    if not name or not hostname:
        return error_response("VALIDATION_ERROR", "name and hostname are required")

    model_base_path = body.get("model_base_path") or _CONFIG.get("qr_env_config", {}).get("QUICKROBOT_API_MODEL_BASE_PATH")
    try:
        node = add_node(_CONFIG["db_path"], name=name, hostname=hostname,
                        transport=body.get("transport", "ansible"),
                        ansible_user=(body.get("ansible_user")
                          or _CONFIG.get("qr_env_config", {}).get("QUICKROBOT_API_ANSIBLE_SSHUSER")
                          or DEFAULT_ANSIBLE_USER),
                        ansible_port=body.get("ansible_port", 22),
                        ansible_key_path=(body.get("ansible_key_path")
                           or (_CONFIG.get("qr_env_config", {}).get("QUICKROBOT_API_ANSIBLE_SSHKEY") or None)),
                        model_base_path=model_base_path)
    except Exception as exc:
        log_qr_action(_CONFIG["db_path"], "node_create_failed", actor="api",
                            details={"name": name, "hostname": hostname, "reason": str(exc)})
        return error_response("VALIDATION_ERROR", str(exc))

    from lib.lib_ansible_runner import validate_node as _vn
    disc_result = {"connected": False, "capabilities": {}, "error": "N/A"}
    try:
        disc_result = _vn(_CONFIG["db_path"], node["id"])
        if disc_result.get("connected"):
            from db.adapters.nodes import update_status as _us, update_capabilities as _uc
            _us(_CONFIG["db_path"], node["id"], "active")
            _uc(_CONFIG["db_path"], node["id"],
                disc_result.get("capabilities", {}),
                disc_result.get("available_devices", []))
    except Exception as _e:
        # Keep status as 'unknown' if discovery fails
        disc_result["error"] = str(_e)

    # Set host_type if provided (Docker/LXC/VM/baremetal)
    if "host_type" in body and node:
        ht = body["host_type"]
        if ht not in ("", "baremetal", "docker", "lxc", "vm"):
            ht = ""
        from db.adapters.nodes import update_node as _un
        _un(_CONFIG["db_path"], node["id"], host_type=ht)

    # Cleanup orphaned node on validation failure (QUICKROBOT_CLEANUP_ON_CREATE_FAIL)
    _qr_env = _CONFIG.get("qr_env_config", {})
    _cleanup_on_fail = _qr_env.get("QUICKROBOT_CLEANUP_ON_CREATE_FAIL", "true").lower() == "true"
    if not disc_result.get("connected") and _cleanup_on_fail:
        from db.adapters.nodes import delete_node as _dn
        try:
            _dn(_CONFIG["db_path"], node["id"])
            log_qr_action(_CONFIG["db_path"], "node_create_cleanup_orphan", node["id"],
                          actor="api", details={"name": name, "hostname": hostname,
                                                "reason": disc_result.get("error", "validation failed")})
        except Exception as _ce:
            log_qr_action(_CONFIG["db_path"], "node_create_cleanup_failed", node["id"],
                          actor="api", details={"name": name, "hostname": hostname,
                                                "cleanup_error": str(_ce)})

    if not disc_result.get("connected"):
        return error_response("NODE_UNREACHABLE",
            f"Node '{name}' ({hostname}) — {disc_result.get('error', 'validation failed')}")

    # Extract stale QR service info from validate_node output
    sqrs = disc_result.get("stale_qr_services", {})
    qr_units = sqrs.get("qr_units", {})
    stale = {
        "service_files": list(qr_units.keys()) if qr_units else [],
        "env_files": [],
        "total": len(qr_units) if qr_units else 0,
        "has_stale": len(qr_units) > 0 if qr_units else False,
    }

    log_qr_action(_CONFIG["db_path"], "node_create", node["id"], actor="api",
                         details={"name": name, "hostname": hostname,
                                 "discovered": disc_result.get("connected"),
                                 "capabilities": disc_result.get("capabilities"),
                                 "stale_files": stale})

    # Re-fetch node to return post-discovery state (not the pre-discovery snapshot)
    node = _gn(_CONFIG["db_path"], node["id"]) or node
    node["stale_files"] = stale
    return success_single(node)


def api_delete_node(node_id):
    """Delete a node with remote undeploy of all attached instances.

    Query params:
        stop_running: 'true' or 'false' — if true and nodes have running instances,
                        run remote undeploy before deleting the node.

    Flow:
        1. Get node and attached non-system instances
        2. For each instance, run engine-specific undeploy playbook on remote node
        3. Log each undeploy action to ansible_actions table
        4. Verify cleanup (non-critical, logged only)
        5. Delete node from DB (FK cascade removes instances)
        6. Log final node_delete action
    """
    import os as _os
    from db.adapters.nodes import delete_node as _dn, get_node as _gn, \
        NodeError
    from db.adapters.instances import list_instances as _list_inst
    from lib.lib_ansible_runner import run_playbook, log_ansible_action

    stop_running = request.args.get("stop_running", "false").lower() == "true"

    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    hostname = (node.get("ansible_inventory_host") or
                node.get("hostname") or
                node.get("name"))
    # Dynamic inventory — no file generated (DI-7)

    # Get all attached non-system instances
    all_instances = _list_inst(_CONFIG["db_path"], node_id=node_id)
    user_instances = [i for i in all_instances if not i.get("system_managed", 0)]

    undeploy_results = []
    # Always undeploy (clean up remote files) regardless of stop_running
    if user_instances:
        # Log override when also stopping running instances
        if stop_running:
            log_qr_override(_CONFIG["db_path"], "node_delete_override_stop_running",
                                node_id, actor="api",
                                details={"instance_count": len(user_instances),
                                "instances": [{"id": i["id"], "name": i["name"], "state": i.get("state")} for i in user_instances]})
        for inst in user_instances:
            inst_id = inst["id"]
            inst_name = inst["name"]
            engine_type_name = inst.get("engine_type_name", "llama_rpc")
            play_name = f"undeploy_{engine_type_name}.yml"
            state = inst.get("state", "unknown")

            # Skip if already unconfigured (no remote to undeploy)
            if state == "unconfigured":
                log_qr_action(_CONFIG["db_path"], "node_delete_undeploy_skip",
                                node_id, instance_id=inst_id, actor="api",
                                details={"reason": "already unconfigured"})
                continue

            # Run undeploy playbook
            pb_id = _resolve_engine_playbook_id("undeploy", engine_type_name)
            if not pb_id:
                log_qr_action(_CONFIG["db_path"], "node_delete_undeploy_skip",
                               node_id, instance_id=inst_id, actor="api",
                               details={"reason": f"no playbook found for {engine_type_name}"})
                undeploy_results.append({"instance_id": inst_id, "skipped": True,
                                "reason": "no playbook"})
                continue

            try:
                r = _execute_playbook(pb_id, resolver_type="playbook_id",
                                      limit=hostname,
                                      extra_vars={
                                          "inventory_host": hostname,
                                          "instance_id": inst_id,
                                          "engine_type": engine_type_name,
                                      },
                                      action_type="undeploy_instance")
                if r["error"]:
                    result = {"failed": True, "error": r["error"]}
                else:
                    result = r.get("result") or {}
                # _execute_playbook already logs starting + result — single logging point

                # Verify cleanup (non-critical)
                try:
                    check_r = _execute_playbook("CHECK_UNDEPLOY_V1", resolver_type="playbook_id",
                                                limit=hostname,
                                                extra_vars={
                                                    "inventory_host": hostname,
                                                    "instance_id": inst_id,
                                                    "engine_type": engine_type_name,
                                                },
                                                action_type="undeploy_instance")
                    check_result = check_r.get("result") or {} if not check_r["error"] else {"failed": True, "error": check_r["error"]}
                    undeploy_results.append({
                        "instance_id": inst_id, "instance_name": inst_name,
                        "success": not result.get("failed", False),
                        "verified": not check_result.get("failed", False),
                    })
                except Exception:
                    undeploy_results.append({
                        "instance_id": inst_id, "instance_name": inst_name,
                        "success": not result.get("failed", False),
                        "verified": None,
                    })
            except Exception as exc:
                # _execute_playbook already logs error case — no duplicate needed
                undeploy_results.append({
                    "instance_id": inst_id, "instance_name": inst_name,
                    "success": False, "error": str(exc),
                })

    # Delete node from DB (FK cascade removes instances and their logs)
    try:
        result = _dn(_CONFIG["db_path"], node_id, stop_running=stop_running)
    except NodeError as exc:
        log_qr_action(_CONFIG["db_path"], "node_delete_failed", node_id, actor="api",
                            details={"reason": str(exc), "stop_running": stop_running,
                                "undeploy_results": undeploy_results})
        return error_response("NODE_HAS_INSTANCES", str(exc))
    except Exception as exc:
        return error_response("RESOURCE_BUSY", str(exc))

    if not result:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    log_qr_action(_CONFIG["db_path"], "node_delete", node_id, actor="api",
                        details={"name": node.get("name"),
                                "stop_running": stop_running,
                                "undeploy_results": undeploy_results})
    return success_single({"node_id": node_id, "deleted": True,
                            "undeploy_results": undeploy_results})


def api_get_node(node_id):
    """Get node details with attached instances."""
    from db.adapters.nodes import get_node
    from db.adapters.instances import list_instances
    node = get_node(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    # Attach non-system-managed instances only
    all_instances = list_instances(_CONFIG["db_path"], node_id=node_id)
    node["instances"] = [i for i in all_instances if not i.get("system_managed", 0)]
    return success_single(node)


def api_update_node(node_id):
    """Update node settings."""
    from db.adapters.nodes import update_node as _un, get_node as _gn
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    existing = _gn(_CONFIG["db_path"], node_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    try:
        node = _un(_CONFIG["db_path"], node_id, **body)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(node)


def api_node_status(node_id):
    """Get node health and attached instances."""
    from db.adapters.nodes import get_node as _gn
    from db.adapters.instances import list_instances
    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    instances = list_instances(_CONFIG["db_path"], node_id=node_id)
    running = [i for i in instances if i["state"] == "running"]
    return success_single({
        "node_status": node["status"],
        "status_reason": node.get("status_reason", ""),
        "instances": instances,
        "running_count": len(running),
    })


def api_set_node_host_status(node_id):
    """Toggle a node's admin active/inactive state.

    This is a write endpoint — used to manually mark a host as inactive.
    Inactive hosts are excluded from ping checks and instance lists by default.
    Ping state (online/offline) is managed separately via POST /nodes/<id>/ping.
    """
    from db.adapters.nodes import get_node as _gn, toggle_host_active as _tha, update_ping_state as _ups
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    existing = _gn(_CONFIG["db_path"], node_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    # Handle admin active/inactive toggle
    if "is_active" in body:
        try:
            val = body["is_active"]
            if isinstance(val, str):
                val = 1 if val.lower() in ("true", "1", "active") else 0
            elif isinstance(val, bool):
                val = 1 if val else 0
            updated = _tha(_CONFIG["db_path"], node_id, int(val))
            return success_single({"is_active": updated.get("is_active"), "ping_state": updated.get("ping_state")})
        except Exception as exc:
            return error_response("VALIDATION_ERROR", str(exc))

    # Handle ping state update (one-shot ping result)
    if "ping_state" in body:
        new_ping = body["ping_state"]
        if new_ping not in ("online", "offline", "disabled"):
            return error_response("VALIDATION_ERROR", "ping_state must be one of: online, offline, disabled")
        try:
            updated = _ups(_CONFIG["db_path"], node_id, new_ping)
            return success_single({"is_active": updated.get("is_active"), "ping_state": updated.get("ping_state")})
        except Exception as exc:
            return error_response("VALIDATION_ERROR", str(exc))

    # Legacy: host_status field — map to appropriate new fields
    legacy = body.get("host_status")
    if legacy in ("active", "no_ping", "offline"):
        # Map legacy values to ping_state, keep is_active unchanged
        ps = {"active": "online", "no_ping": "offline", "offline": "offline"}.get(legacy, "offline")
        try:
            updated = _ups(_CONFIG["db_path"], node_id, ps)
            return success_single({"is_active": updated.get("is_active"), "ping_state": updated.get("ping_state")})
        except Exception as exc:
            return error_response("VALIDATION_ERROR", str(exc))
    if legacy == "inactive":
        # Legacy inactive → set is_active=0
        try:
            updated = _tha(_CONFIG["db_path"], node_id, 0)
            return success_single({"is_active": updated.get("is_active"), "ping_state": updated.get("ping_state")})
        except Exception as exc:
            return error_response("VALIDATION_ERROR", str(exc))

    return error_response("VALIDATION_ERROR", "Request must include is_active or ping_state")


def api_reset_node_build_state(node_id):
    """Reset a node's build state to idle (used when stale 'compiling' blocks new builds)."""
    from db.adapters.nodes import get_node as _gn
    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    try:
        from db.sqlite import pool
        with pool(_CONFIG["db_path"]) as conn:
            conn.execute(
                "UPDATE nodes SET node_build_state = 'idle', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (node_id,),
            )
        return success_single({"node_id": node_id, "node_build_state": "idle"})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))


def api_node_shutdown(node_id):
    """Graceful node shutdown via Ansible (Phase 1 placeholder)."""
    import os as _os
    from lib.lib_ansible_runner import log_ansible_action as _la

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = nd["hostname"]
    user = nd.get("ansible_user") or DEFAULT_ANSIBLE_USER
    port = nd.get("ansible_port", 22)
    key = nd.get("ansible_key_path")

    # _execute_playbook handles all logging (starting/success/error) — single logging point
    r = _execute_playbook("SHUTDOWN_NODE_V1", resolver_type="playbook_id",
                         limit=hostname, node_id=node_id,
                         extra_vars={"inventory_host": hostname,
                                     "node_user": user, "ansible_port": port},
                         action_type="shutdown_node")
    if r["error"]:
        return error_response("DEPLOYMENT_FAILED", r["error"])
    return success_single({"action": "shutdown", "node_id": node_id,
                            "result": r.get("result")})


def api_node_reboot(node_id):
    """Reboot node via Ansible (Phase 1 placeholder)."""
    import os as _os
    from lib.lib_ansible_runner import log_ansible_action as _la

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = nd["hostname"]
    user = nd.get("ansible_user") or DEFAULT_ANSIBLE_USER
    port = nd.get("ansible_port", 22)

    # _execute_playbook handles all logging (starting/success/error) — single logging point
    r = _execute_playbook("REBOOT_NODE_V1", resolver_type="playbook_id",
                         limit=hostname, node_id=node_id,
                         extra_vars={"inventory_host": hostname,
                                     "node_user": user, "ansible_port": port},
                         action_type="reboot_node")
    if r["error"]:
        return error_response("DEPLOYMENT_FAILED", r["error"])
    return success_single({"action": "reboot", "node_id": node_id,
                            "result": r.get("result")})


def api_node_apt_update(node_id):
    """Run apt update on a remote node."""
    from lib.lib_ansible_runner import log_ansible_action as _la

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = nd.get("hostname") or nd.get("name")

    # _execute_playbook handles all logging (starting/success/error) — single logging point
    r = _execute_playbook("APT_UPDATE_V1", resolver_type="playbook_id",
                         limit=hostname, node_id=node_id,
                         extra_vars={"inventory_host": hostname},
                         action_type="apt_update")
    if r["error"]:
        return error_response("DEPLOYMENT_FAILED", r["error"])
    return success_single({"action": "apt_update", "node_id": node_id,
                            "result": r.get("result")})


def api_node_ping(node_id):
    """One-shot ping reachability check for a node (used by WebUI ping dots)."""
    import subprocess as _subp

    from db.adapters.nodes import get_node as _gn, update_ping_state as _ups

    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    hostname = node.get("hostname") or node.get("name")
    if not hostname or node_id == 1:  # localhost skip
        return success_single({"ping_state": "disabled"})

    # Read ping_command from env file (migrated from engine_configs)
    qr_env = _CONFIG.get("qr_env_config", {})
    ping_cmd = qr_env.get("QUICKROBOT_API_PING_COMMAND") or "ping -c1 -W2 {host}"

    if not ping_cmd or ping_cmd.strip() == "":
        return success_single({"ping_state": "disabled", "message": "ping_command not configured"})

    try:
        result = _subp.run(
            ping_cmd.replace("{host}", hostname),
            shell=True, timeout=5,
            stdout=_subp.DEVNULL, stderr=_subp.DEVNULL,
        )
        ps = "online" if result.returncode == 0 else "offline"
    except Exception:
        ps = "offline"

    # Update DB
    try:
        _ups(_CONFIG["db_path"], node_id, ps)
    except Exception:
        pass  # non-critical

    return success_single({"ping_state": ps})


def api_node_apt_upgrade(node_id):
    """Run apt upgrade on a remote node."""
    from lib.lib_ansible_runner import log_ansible_action as _la

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = nd.get("hostname") or nd.get("name")

    # _execute_playbook handles all logging (starting/success/error) — single logging point
    r = _execute_playbook("APT_UPGRADE_V1", resolver_type="playbook_id",
                         limit=hostname, node_id=node_id,
                         extra_vars={"inventory_host": hostname},
                         action_type="apt_upgrade")
    if r["error"]:
        return error_response("DEPLOYMENT_FAILED", r["error"])
    return success_single({"action": "apt_upgrade", "node_id": node_id,
                            "result": r.get("result")})


def api_instance_update_build(inst_id):
    """Trigger a git pull + cmake recompile for a llama.cpp instance (async).

    Supports both llama_server and rpc engine types. Uses node-level build
    coordination so only one build runs per host at a time.

    Returns immediately with "updating" status. The build runs in a background
    thread with a 30-minute timeout. State transitions: updating → deployed (success)
    or updating → error (failure) or updating → timeout (30 min exceeded).
    """
    from db.adapters.instances import get_instance as _gi
    from db.adapters.instances import transition_state as _ts, log_action as _log
    from db.adapters.configs import get_engine_config as _gec
    from lib.lib_ansible_runner import run_playbook, log_ansible_action as _la
    import re as _re

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

    # Resolve build commands from engine_configs, with per-instance override
    try:
        et_id = inst.get("engine_type_id")
        gc = _gec(_CONFIG["db_path"], et_id) or {}
    except Exception:
        gc = {}

    co = inst.get("config_override", {}) or {}
    co_dict = co if isinstance(co, dict) else {}

    def _gc_val(key, default=None):
        entry = gc.get(key) or {}
        return entry.get("value") or default

    git_pull_cmd = co_dict.get("git_pull_cmd") or _gc_val("node_git_pull_cmd", "git pull origin main")
    build_threads = co_dict.get("build_threads", None)
    if build_threads is None:
        try:
            bt = _gc_val("build_threads")
            build_threads = int(bt) if bt else 2
        except Exception:
            build_threads = 2
    else:
        try:
            build_threads = int(build_threads)
        except (ValueError, TypeError):
            build_threads = 2

    extra_vars = {
        "inventory_host": hostname,
        "instance_id": inst_id,
        "engine_type": engine,
        "remote_node_user": nd.get("ansible_user") or DEFAULT_ANSIBLE_USER,
        # node_src_dir and cmake_cmd vars passed as extra_vars; play-level |default()
        # handles CLI usage. git_pull_cmd NOT passed (conflicts with play-level default).
        "node_src_dir": _gc_val("node_src_dir", "/opt/quickrobot/llama.cpp"),
        "node_build_dir": _gc_val("node_build_dir", "/opt/quickrobot/llama.cpp/build"),
        "node_build_set_cmd": _gc_val("node_build_set_cmd", "cmake -B build -DGGML_RPC=ON -DGGML_NATIVE=ON -DGGML_CPU=ON -DLLAMA_OPENSSL=ON -DGGML_AVX2=ON -DGGML_VULKAN=ON"),
        "node_build_run_cmd": _gc_val("node_build_run_cmd", "cmake --build build --config Release -j 2"),
    }

    # Check if already updating (idempotent)
    current_state = inst.get("state", "")
    if current_state == "updating":
        return success_single({"action": "update_build", "instance_id": inst_id,
                                "status": "already_updating", "node": hostname})

    # Log to ansible_actions for Ansible Logs page visibility
    _la(_CONFIG["db_path"], "update_build", node_id, inst_id,
        "playbooks/update_and_compile.yml", extra_vars, {"status": "started"})

    # Check node-level build coordination
    with _NODE_BUILD_LOCK:
        try:
            from db.sqlite import pool as _pool
            with _pool(_CONFIG["db_path"]) as conn:
                nd_state = conn.execute(
                    "SELECT node_build_state FROM nodes WHERE id = ?", (node_id,)
                ).fetchone()
                nd_state_val = nd_state[0] if nd_state else "idle"
        except Exception:
            nd_state_val = "idle"

        if nd_state_val == "running":
            # Another instance on this node is building — find it
            try:
                with _pool(_CONFIG["db_path"]) as conn2:
                    other = conn2.execute(
                        f"SELECT id, name FROM instances WHERE node_id = ? AND state IN ({','.join(['?']*len(BUILD_STATES))})",
                        (node_id,) + tuple(BUILD_STATES),
                    ).fetchone()
                    other_name = f"#{other[0]} ({other[1]})" if other else "unknown"
            except Exception:
                other_name = "unknown"
            return error_response("NODE_BUSY",
                                  f"Node {hostname} is building (instance {other_name})")

        # Set node build state to running
        try:
            with _pool(_CONFIG["db_path"]) as conn3:
                conn3.execute("UPDATE nodes SET node_build_state = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (node_id,))
        except Exception:
            pass

    # Transition to updating state
    _ts(_CONFIG["db_path"], inst_id, "updating")

    def _run_compile():
        """Background thread: run playbook with 30-min timeout."""
        try:
            _log(_CONFIG["db_path"], inst_id, "update_and_compile", "started",
                 {"node": hostname, "threads": build_threads})

            r = _execute_playbook("UPDATE_AND_COMPILE_V1", resolver_type="playbook_id",
                                  limit=hostname, extra_vars=extra_vars, timeout=1800,
                                  node_id=node_id, instance_id=inst_id,
                                  action_type="update_and_compile")
            if r["error"]:
                _log(_CONFIG["db_path"], inst_id, "update_and_compile", "failed", {"error": r["error"]})
                try:
                    _ts(_CONFIG["db_path"], inst_id, "error")
                except Exception:
                    pass
                return

            result = r.get("result") or {}
            _log(_CONFIG["db_path"], inst_id, "update_and_compile", "playbook_done",
                 {"changed": result.get("changed"), "failed": result.get("failed")})

            # Extract commit hash from playbook task results (msg field, not stdout)
            new_build = None
            try:
                plays_data = result.get("results", {}).get("plays", [])
                for play in plays_data:
                    for task in play.get("tasks", []):
                        for host_data in task.get("hosts", {}).values():
                            msg = host_data.get("msg", "") or ""
                            if "BUILD_COMMIT=" in msg:
                                new_build = msg.split("BUILD_COMMIT=")[1].split("|")[0].strip()
                                break
                        if new_build:
                            break
                    if new_build:
                        break
            except Exception:
                pass  # Non-critical

            # Update build_number in DB on success
            if not result.get("failed") and new_build:
                try:
                    from db.sqlite import pool as _pool2
                    with _pool2(_CONFIG["db_path"]) as conn4:
                        conn4.execute("UPDATE instances SET build_number=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (new_build, inst_id))
                except Exception:
                    pass

            # Transition to deployed (compile was standalone, service may still be running)
            try:
                _ts(_CONFIG["db_path"], inst_id, "deployed")
            except Exception:
                pass

            _log(_CONFIG["db_path"], inst_id, "update_and_compile", "success",
                    {"build": new_build, "node": hostname})

        except TimeoutError:
            try:
                _ts(_CONFIG["db_path"], inst_id, "timeout")
            except Exception:
                pass
            _log(_CONFIG["db_path"], inst_id, "update_and_compile", "timeout",
                    {"node": hostname})
        except Exception as exc:
            try:
                _ts(_CONFIG["db_path"], inst_id, "error")
            except Exception:
                pass
            _log(_CONFIG["db_path"], inst_id, "update_and_compile", "failed", str(exc))
        finally:
            # Always reset node build state on completion
            try:
                with _NODE_BUILD_LOCK:
                    from db.sqlite import pool as _pool3
                    with _pool3(_CONFIG["db_path"]) as conn5:
                        conn5.execute("UPDATE nodes SET node_build_state = 'idle', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (node_id,))
            except Exception:
                pass

    # Start background thread
    t = threading.Thread(target=_run_compile, daemon=True,
                            name=f"update-compile-{inst_id}")
    t.start()

    return success_single({"action": "update_and_compile", "instance_id": inst_id,
                            "status": "compiling", "node": hostname,
                            "message": f"Compile started in background (30 min timeout, {engine} engine)"})


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


def api_node_discover(node_id):
    """Re-validate a node by running validate.yml against it.

    Collects CPU/RAM/OS/capabilities from the remote node and updates
    the node record in the DB. Useful for refreshing stale data after
    hardware changes or network reconfiguration.
    """
    from db.adapters.nodes import get_node as _gn
    from lib.lib_ansible_runner import validate_node as _vn

    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    try:
        import ast as _ast
        result = _vn(_CONFIG["db_path"], node_id)
        connected = result.get("connected", False)
        error_msg = result.get("error")
        caps = result.get("capabilities", {})
        devices_raw = result.get("available_devices", [])
        # Fix: available_devices comes as Python str() repr of list from
        # Jinja2 YAML rendering — parse it back to a proper list
        if isinstance(devices_raw, str):
            stripped = devices_raw.strip()
            try:
                devices = _ast.literal_eval(stripped)
                if not isinstance(devices, list):
                    devices = [devices_raw]
            except (ValueError, SyntaxError):
                devices = [stripped]
        else:
              devices = devices_raw

        status = "active" if connected else "unknown"
        status_reason = "" if connected else (error_msg or "")

        # Build concise warnings list for agent feedback
        warnings = []
        if caps:
            kf = caps.get("keeper_files", [])
            if kf:
                warnings.append(f"stale keeper files: {', '.join(kf[:3])}")
            gpu_w = caps.get("gpu_perm_warn", "")
            if gpu_w and gpu_w != "ok":
                warnings.append(f"GPU perm: {gpu_w[:80]}")
            fs = caps.get("fs_free_gb")
            if fs is not None and fs < 10:
                warnings.append(f"low disk: {fs} GB free")
            bs = caps.get("binary_status", {})
            if bs.get("ls") == "MISSING":
                warnings.append("llama-server missing")
            if bs.get("rs") == "MISSING":
                warnings.append("rpc-server missing")

        from db.adapters.nodes import update_node
        update_node(_CONFIG["db_path"], node_id, status=status,
                    status_reason=status_reason,
                    capabilities=json.dumps(caps),
                  available_devices=json.dumps(devices))

        return success_single({
            "action": "discover", "node_id": node_id,
            "connected": connected,
            "capabilities": caps,
            "available_devices": devices,
            "status": status,
            "warnings": warnings if warnings else [],
        })
    except Exception as exc:
        from db.adapters.nodes import update_node
        update_node(_CONFIG["db_path"], node_id, status_reason=str(exc))
        return error_response("DISCOVERY_FAILED", str(exc))


def api_discover_local():
    """Discover and update localhost (node 1) hardware inventory.

    Runs the same hardware checks as validate.yml but locally without
    SSH or root. Updates the node record with CPU/RAM/disk/GPU/OS info.
    Returns partial data if some commands fail — no crash.
    """
    from db.adapters.nodes import get_node as _gn, update_local_host_inventory as _ulhi
    try:
        from lib.lib_local_inventory import gather_local_inventory, gather_local_hostname

        # Check if localhost node exists; create it if not (with real hostname)
        existing = _gn(_CONFIG["db_path"], 1)
        if existing is None:
            actual_host = gather_local_hostname()
            from db.adapters.nodes import add_node as _an
            existing = _an(_CONFIG["db_path"], name=actual_host, hostname=actual_host,
                           transport="ansible")

        # Gather hardware inventory
        inv = gather_local_inventory()

        # Update the node record
        _ulhi(_CONFIG["db_path"], 1, inv)

        return success_single({
            "action": "discover-local",
            "node_id": 1,
            "cpu_cores": inv.get("cpu_cores"),
            "ram_mb": inv.get("ram_mb"),
            "os": inv.get("os"),
            "fs_free_gb": inv.get("fs_free_gb"),
            "gpu_name": inv.get("gpu_name"),
            "gpu_type": inv.get("gpu_type"),
            "gpu_memory_mb": inv.get("gpu_memory_mb"),
            "available_devices": inv.get("available_devices", []),
        })
    except Exception as exc:
        return error_response("DISCOVERY_LOCAL_FAILED", str(exc))


def api_node_configs(node_id):
    """List all node config values (for all engine types)."""
    from db.adapters.nodes import get_node as _gn
    from db.adapters.engine_types import list_engine_types
    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    engine_types = list_engine_types(_CONFIG["db_path"], enabled_only=True)
    result = {}
    from db.adapters.configs import get_node_config as _gnc
    for et in engine_types:
        configs = _gnc(_CONFIG["db_path"], node_id, et["id"])
        if configs:
            result[et["name"]] = configs

    return success_single({"node_id": node_id, "configs": result})


def api_set_node_config(node_id, key):
    """Set/update a per-node config value for any engine type."""
    from db.adapters.nodes import get_node as _gn
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    engine_type_name = request.args.get("engine_type")
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type_name)
    et_id = et["id"] if et else None
    if et_id is None and engine_type_name:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type_name}' not found")

    value = body.get("value", "")
    from db.adapters.configs import set_node_config
    set_node_config(_CONFIG["db_path"], node_id, et_id, key, value)

    return success_single({"node_id": node_id, "key": key, "value": value})


def api_delete_node_config(node_id, key):
    """Remove a per-node config value."""
    from db.adapters.nodes import get_node as _gn
    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    engine_type_name = request.args.get("engine_type")
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type_name)
    et_id = et["id"] if et else None

    from db.adapters.configs import delete_node_config
    deleted = delete_node_config(_CONFIG["db_path"], node_id, et_id, key)
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Config {key} not found for node {node_id}")

    return success_single({"node_id": node_id, "key": key, "deleted": True})


# ---------------------------------------------------------------------------
# Engine type management endpoints
# ---------------------------------------------------------------------------

def api_list_engines():
    """List all engine types: DB entries + in-memory discovered engines not in DB."""
    from db.adapters.engine_types import list_engine_types as _let, get_engine_type as _get_et
    from db.adapters.instances import list_instances as _li
    engine_types = _let(_CONFIG["db_path"], enabled_only=False)

    result = []
    db_names = {et["name"] for et in engine_types}

    # Add DB engine types
    for et in engine_types:
        instances = _li(_CONFIG["db_path"], engine_type_id=et["id"])
        count = len(instances)
        loaded_cap = get_engine_capabilities(et["name"])
        if loaded_cap:
            et["capabilities"].update(loaded_cap)
        et["instance_count"] = count
        result.append(et)

    # Add in-memory engines not yet registered in DB (normalize names for comparison)
    for eng_name, cls, cap in ENGINES:
        cap_name = cap.get("name") if isinstance(cap, dict) else None
        db_name = eng_name.replace("_", "-")
        if db_name not in db_names and eng_name not in db_names and (cap_name is None or cap_name not in db_names):
            cap_dict = cap if isinstance(cap, dict) else {}
            result.append({
                "name": db_name if db_name != eng_name else eng_name,
                "display_name": cap_dict.get("display_name", eng_name.replace("_", " ").title()),
                "capabilities": cap_dict,
                "instance_count": 0,
                "enabled": 1,
            })

    return success_list(result)


def api_get_engine_config(engine_type):
    """List all config keys for an engine type."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.configs import get_engine_config as _gec
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    configs = _gec(_CONFIG["db_path"], et_id)
    return success_single(configs or {})


def api_set_engine_config(engine_type, key):
    """Set/update an engine config key."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.configs import update_engine_config as _sec
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    value = body.get("value", "")
    description = body.get("description", "")
    _sec(_CONFIG["db_path"], et_id, key, value, description)
    return success_single({"engine_type": engine_type, "key": key, "value": value})


def api_delete_engine_config(engine_type, key):
    """Remove an engine config key."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.configs import delete_engine_config as _dec
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    deleted = _dec(_CONFIG["db_path"], et_id, key)
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Config {key} not found")

    return success_single({"engine_type": engine_type, "key": key, "deleted": True})


def api_batch_set_engine_config(engine_type):
    """Set multiple config keys at once (batch update).

    Accepts a JSON body with {configs: {key1: value1, key2: value2}} and
    persists all values in a single request. Used by the WebUI batch save.

    Args:
        engine_type: Engine type name string.

    Returns:
        dict with saved_keys count and engine_type.
    """
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    configs = body.get("configs", {})
    if not isinstance(configs, dict):
        return error_response("VALIDATION_ERROR", "configs must be a dict")

    from db.adapters.configs import update_engine_config as _sec

    saved = 0
    for key, value in configs.items():
        _sec(_CONFIG["db_path"], et_id, key, str(value), "")
        saved += 1

    return success_single({"saved_keys": saved, "engine_type": engine_type})


def _let_config():
    """Cached engine type list for batch config lookups."""
    from db.adapters.engine_types import list_engine_types as _let
    return _let(_CONFIG["db_path"])


def api_api_server_update_setting(key):
    """Get or update a single quickrobot-api config key."""
    from db.adapters.configs import get_engine_config as _gec, update_engine_config as _uec

    editable_keys = ("db_path", "ping_interval",
                       "polling_interval_local_sec", "polling_interval_remote_sec",
                       "refresh_interval_default_sec")
    # env-sourced keys (read-only, set from .quickrobot.env):
    #   api_host, api_port, ansible_user, ansible_key_path,
    #   playbook_root_dir, ping_command
    if key not in editable_keys:
        return error_response("INVALID_KEY", f"Editable keys: {', '.join(editable_keys)}")

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], QR_ENGINE_API_NAME)
    et_id = et["id"] if et else None
    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", "Engine type quickrobot-api not found")

    if request.method == "GET":
        row = _gec(_CONFIG["db_path"], et_id, key) or {}
        value = row.get("value", "")
        return success_single({"engine_type": QR_ENGINE_API_NAME, "key": key, "value": value})

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid json"))

    value = body.get("value")
    if value is None:
        return error_response("VALIDATION_ERROR", '"value" field required')

    try:
        _uec(_CONFIG["db_path"], et_id, key, str(value))
    except Exception as exc:
        return error_response("WRITE_ERROR", str(exc))

    return success_single({"engine_type": QR_ENGINE_API_NAME, "key": key, "value": str(value)})


def api_list_presets(engine_type):
    """List presets for an engine type."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import list_presets as _lp
    from db.sqlite import pool as _pool
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    presets = _lp(_CONFIG["db_path"], engine_type_id=et_id)
    # Apply ?q= search filter (name, category, tags, model_name)
    q = request.args.get("q", "").strip()
    if q:
        presets = [p for p in presets if
                   q.lower() in p.get("name", "").lower() or
                   q.lower() in str(p.get("category") or "").lower() or
                   q.lower() in str(p.get("tags") or "").lower() or
                   q.lower() in str(p.get("model_name") or "").lower()]
    # Enrich with model_name and gpu_device from DB
    with _pool(_CONFIG["db_path"]) as conn:
        for p in presets:
            mid = p.get("model_id")
            if mid:
                mrow = conn.execute(
                    "SELECT name FROM engine_models WHERE id = ?", (mid,)
                ).fetchone()
                p["model_name"] = mrow["name"] if mrow else None
            else:
                p["model_name"] = None
            p["gpu_device"] = p.get("gpu_device")
    # Add relative age for each preset
    from lib.lib_utils import relative_age
    for p in presets:
        p["age_created"] = relative_age(p.get("created_at"))
    return success_list(presets)


def api_create_preset(engine_type):
    """Create a new preset for an engine type."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import add_preset as _ap
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    name = body.get("name")
    if not name:
        return error_response("VALIDATION_ERROR", "preset name is required")

    # Pre-check for duplicate name within engine_type_id
    from db.sqlite import pool as _pool
    with _pool(_CONFIG["db_path"]) as conn:
        existing = conn.execute(
            "SELECT id, name FROM engine_presets WHERE engine_type_id = ? AND name = ?",
            (et_id, name),
        ).fetchone()
    if existing is not None:
        return error_response("CONFLICT_ERROR",
                                f"Preset '{name}' already exists for this engine type (id={existing['id']})",
                                status_code=409)

    try:
        preset = _ap(_CONFIG["db_path"], et_id, name=name,
                        category=body.get("category", "default"),
                        config_template=body.get("config_template", {}),
                        model_path=body.get("model_path"),
                        tags=body.get("tags", []),
                        model_id=body.get("model_id"),
                        gpu_device=body.get("gpu_device"))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(preset)


def api_get_preset(engine_type, preset_id):
    """Get a single preset by id.

    Args:
        engine_type: Engine type name string.
        preset_id: Integer primary key of the preset.

    Returns:
        Single preset dict with affected_instances count.
    """
    from db.sqlite import pool as _pool
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import get_preset as _gp

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    preset = _gp(_CONFIG["db_path"], preset_id)
    if preset is None:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    # Enrich with model_name
    mid = preset.get("model_id")
    if mid:
        with _pool(_CONFIG["db_path"]) as conn:
            mrow = conn.execute(
                "SELECT name FROM engine_models WHERE id = ?", (mid,)
            ).fetchone()
            preset["model_name"] = mrow["name"] if mrow else None
    else:
        preset["model_name"] = None
    preset["gpu_device"] = preset.get("gpu_device")

    # Verify the preset belongs to this engine type
    if et_id is not None and preset.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                                f"Preset {preset_id} belongs to a different engine type")

    # Get instances using this preset
    affected = []
    if et_id is not None:
        from db.adapters.instances import list_instances_by_preset as _libp
        instances = _libp(_CONFIG["db_path"], preset_id)
        for inst in instances:
            affected.append({
                "id": inst["id"],
                "name": inst["name"],
                "node_name": inst.get("node_name", ""),
                "state": inst["state"],
            })

    return jsonify({"status": "ok", "data": preset,
                        "affected_instances": affected}), 200


def api_update_preset(engine_type, preset_id):
    """Update a preset with affected instances count."""
    from db.adapters.engine_types import list_engine_types as _let
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.presets import update_preset as _up
    try:
        preset = _up(_CONFIG["db_path"], preset_id, **body)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    if preset is None:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    # Find engine type id for instance lookup
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    # Get instances using this preset
    affected = []
    if et_id is not None:
        from db.adapters.instances import list_instances_by_preset as _libp
        instances = _libp(_CONFIG["db_path"], preset_id)
        for inst in instances:
            affected.append({
                "id": inst["id"],
                "name": inst["name"],
                "node_name": inst.get("node_name", ""),
                "state": inst["state"],
            })

    return jsonify({"status": "ok", "data": preset,
                        "affected_instances": affected}), 200


def api_preset_restart_all(engine_type, preset_id):
    """Restart all instances using a specific preset.

    Updates each instance via the update endpoint which triggers deploy
    if the preset config has changed.

    Args:
        engine_type: Engine type name string.
        preset_id: Integer primary key of the preset.

    Returns:
        dict with restart results per instance.
    """
    from db.adapters.presets import get_preset as _gp
    from db.adapters.engine_types import list_engine_types as _let

    preset = _gp(_CONFIG["db_path"], preset_id)
    if preset is None:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    # Get all instances using this preset
    from db.adapters.instances import list_instances_by_preset as _libp
    instances = _libp(_CONFIG["db_path"], preset_id)

    results = []
    for inst in instances:
        iid = inst["id"]
        result = {"instance_id": iid, "name": inst["name"]}
        try:
            # Stop if running
            if inst["state"] == "running":
                from db.adapters.instances import transition_state as _ts
                try:
                    _ts(_CONFIG["db_path"], iid, "stopping")
                    _ts(_CONFIG["db_path"], iid, "stopped")
                except Exception:
                    pass

            # Trigger deploy via the deploy_instance function (skip_build since preset restart is config-only)
            deploy_result = deploy_instance(_CONFIG["db_path"], iid, skip_build=True)
            result["deploy"] = deploy_result

            if deploy_result.get("success"):
                from db.adapters.instances import log_action as _log
                _log(_CONFIG["db_path"], iid, "preset_restart", "success")
        except Exception as exc:
            result["deploy"] = {"success": False, "message": str(exc)}
            try:
                from db.adapters.instances import log_action as _log
                _log(_CONFIG["db_path"], iid, "preset_restart", "failed",
                        detail={"error": str(exc)})
            except Exception:
                pass

        results.append(result)

    succeeded = sum(1 for r in results if r.get("deploy", {}).get("success"))
    return success_list(results, meta={"succeeded": succeeded}), 200


def api_delete_preset(engine_type, preset_id):
    """Delete a preset."""
    from db.adapters.presets import delete_preset as _dp
    try:
        deleted = _dp(_CONFIG["db_path"], preset_id)
    except Exception as exc:
        return error_response("BAD_REQUEST", str(exc))
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    return success_single({"preset_id": preset_id, "deleted": True})


def api_clone_preset(engine_type, preset_id):
    """Clone a preset 1:1 with unique name suffix."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import PresetError, clone_preset as _cp

    et = _get_et(_CONFIG["db_path"], engine_type)
    if not et:
        return error_response("ENGINE_NOT_FOUND", f"Engine '{engine_type}' not found")
    et_id = et["id"]

    try:
        new_preset = _cp(_CONFIG["db_path"], preset_id, et_id)
    except PresetError as exc:
        return error_response("BAD_REQUEST", str(exc))

    return success_single({"id": new_preset["id"], "name": new_preset["name"]})


def api_list_all_models():
    """List ALL models across all engines, with optional ?q= search filter."""
    from db.adapters.engine_types import list_engine_types as _let
    from db.adapters.models import list_models as _lm

    models = _lm(_CONFIG["db_path"], engine_type_id=None)

    # Enrich each model with engine type name and preset usage count
    et_map = {}
    from db.adapters.engine_types import list_engine_types as _let
    for et in _let(_CONFIG["db_path"]):
        et_map[et["id"]] = et["name"]
    for m in models:
        m["engine_type_name"] = et_map.get(m.get("engine_type_id"), "unknown")

    # Count preset usage per model (model_id FK in engine_presets)
    from db.sqlite import pool
    with pool(_CONFIG["db_path"]) as conn:
        preset_counts = {}
        for row in conn.execute(
            "SELECT model_id, COUNT(*) as cnt FROM engine_presets WHERE model_id IS NOT NULL GROUP BY model_id"
        ).fetchall():
            preset_counts[row["model_id"]] = row["cnt"]
    for m in models:
        m["preset_count"] = preset_counts.get(m["id"], 0)

    # Filter by engine_type (optional — e.g. from preset list nav)
    et_name = request.args.get("engine", "").strip()
    if et_name:
        from db.adapters.engine_types import get_engine_type_by_name as _get_et
        et = _get_et(_CONFIG["db_path"], et_name)
        et_id = et["id"] if et else None
        if et_id:
            models = [m for m in models if m.get("engine_type_id") == et_id]

    # Count active/inactive totals BEFORE any filtering (for counter display)
    total_active = sum(1 for m in models if m.get("is_active", 1))
    total_inactive = sum(1 for m in models if not m.get("is_active", 1))

    # Filter by is_active (default: show active only)
    show_inactive = request.args.get("include_inactive", "false").lower() == "true"
    if not show_inactive:
        models = [m for m in models if m.get("is_active", 1)]

    # Apply ?q= search filter (name, model_path, quantization)
    q = request.args.get("q", "").strip()
    if q:
        ql = q.lower()
        models = [m for m in models if
                  ql in str(m.get("name") or "").lower() or
                  ql in str(m.get("model_path") or "").lower() or
                  ql in str(m.get("quantization") or "").lower()]

    # Apply ?draft_filter=N — show only the model referenced as a draft
    df = request.args.get("draft_filter", "").strip()
    if df:
        try:
            df_id = int(df)
            models = [m for m in models if m.get("id") == df_id]
        except ValueError:
            pass

    # Add relative age for each model
    from lib.lib_utils import relative_age
    for m in models:
        m["age_created"] = relative_age(m.get("created_at"))
    return success_list(models, meta={"active_count": total_active, "inactive_count": total_inactive})


def api_get_model_global(model_id):
    """Get details for a single model by ID (global, no engine type check)."""
    from db.adapters.models import get_model as _gm

    model = _gm(_CONFIG["db_path"], model_id)
    if model is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")
    # Enrich with engine type name
    from db.adapters.engine_types import get_engine_type as _get_et_id
    et = _get_et_id(_CONFIG["db_path"], model.get("engine_type_id"))
    if et:
        model["engine_type_name"] = et["name"]
    else:
        model["engine_type_name"] = "unknown"
    return success_single(model)


def api_update_model_global(model_id):
    """Update an existing model by ID (global, no engine type check)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.models import get_model as _gm, update_model as _um

    existing = _gm(_CONFIG["db_path"], model_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    # Build allowed fields dynamically (same as per-engine handler)
    from db.adapters.models import ALLOWED_FIELDS
    update_fields = {}
    for key in ALLOWED_FIELDS:
        if key in body and body[key] is not None:
            update_fields[key] = body[key]
        elif key == "name" or key == "model_path":
            # Always require these
            pass

    try:
        _um(_CONFIG["db_path"], model_id, **update_fields)
    except Exception as exc:
        return error_response("UPDATE_ERROR", str(exc))

    updated = _gm(_CONFIG["db_path"], model_id)
    return success_single(updated)


def api_create_model_global():
    """Create a new global model entry."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.models import add_model as _am

    name = body.get("name")
    model_path = body.get("model_path")
    if not name or not model_path:
        return error_response("VALIDATION_ERROR", "name and model_path are required")

    engine_type_id = body.get("engine_type_id", 21)  # Default to llama_server

    try:
        model = _am(_CONFIG["db_path"], engine_type_id, name=name, model_path=model_path,
                    mmproj_path=body.get("mmproj_path") or None,
                    draft_model_path=body.get("draft_model_path") or None,
                    size_bytes=body.get("size_bytes"),
                    last_modified=body.get("last_modified"),
                    quantization=body.get("quantization"),
                    model_params=body.get("model_params"))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(model)


def api_list_models(engine_type):
    """List models for an engine type, with optional ?q= search filter."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import list_models as _lm
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    host_id = request.args.get("host_id")
    models = _lm(_CONFIG["db_path"], engine_type_id=et_id,
                    host_id=int(host_id) if host_id else None)

    # Filter by is_active (default: show active only)
    show_inactive = request.args.get("include_inactive", "false").lower() == "true"
    if not show_inactive:
        models = [m for m in models if m.get("is_active", 1)]

    # Apply ?q= search filter (name, model_path, quantization)
    q = request.args.get("q", "").strip()
    if q:
        ql = q.lower()
        models = [m for m in models if
                  ql in str(m.get("name") or "").lower() or
                  ql in str(m.get("model_path") or "").lower() or
                  ql in str(m.get("quantization") or "").lower()]

    # Add relative age for each model
    from lib.lib_utils import relative_age
    for m in models:
        m["age_created"] = relative_age(m.get("created_at"))
    return success_list(models)


def api_model_active(model_id):
    """Toggle model active/inactive state."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])
    val = body.get("is_active")
    if val is None:
        return error_response("VALIDATION_ERROR", "Request must include is_active (0 or 1)")
    if val not in (0, 1):
        return error_response("VALIDATION_ERROR", "is_active must be 0 or 1")
    from db.adapters.models import update_model as _um
    model = _um(_CONFIG["db_path"], model_id, is_active=val)
    if not model:
        return error_response("NOT_FOUND", f"Model {model_id} not found")
    return success_single({"is_active": model["is_active"]})


def api_create_model(engine_type):
    """Add a new model for an engine type."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import add_model as _am
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    name = body.get("name")
    model_path = body.get("model_path")
    if not name or not model_path:
        return error_response("VALIDATION_ERROR", "name and model_path are required")

    try:
        model = _am(_CONFIG["db_path"], et_id, name=name, model_path=model_path,
                    mmproj_path=body.get("mmproj_path") or None,
                    draft_model_path=body.get("draft_model_path") or None,
                    size_bytes=body.get("size_bytes"),
                    last_modified=body.get("last_modified"),
                    host_id=body.get("host_id"),
                    quantization=body.get("quantization"))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(model)


def api_delete_model(engine_type, model_id):
    """Remove a model."""
    from db.adapters.models import delete_model as _dm
    deleted = _dm(_CONFIG["db_path"], model_id)
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    return success_single({"model_id": model_id, "deleted": True})


def api_clear_all_models():
    """Delete all models from the database.

    POST body: {"engine_type": "llama_server"} (required) or omit for all engines.

    Args:
        engine_type: Engine type to clear. If omitted, clears all models across all engines.

    Returns:
        dict with count of deleted models.
    """
    from db.sqlite import pool as _pool
    from db.adapters.engine_types import list_engine_types as _let

    data = request.get_json() or {}
    engine_type = data.get("engine_type")  # optional; None clears all engines

    try:
        with _pool(_CONFIG["db_path"]) as conn:
            if engine_type:
                from db.adapters.engine_types import get_engine_type_by_name as _get_et
                et = _get_et(_CONFIG["db_path"], engine_type)
                et_id = et["id"] if et else None
                if et_id:
                    count = conn.execute("DELETE FROM engine_models WHERE engine_type_id = ?", (et_id,)).rowcount
                else:
                    count = 0
            else:
                count = conn.execute("DELETE FROM engine_models").rowcount
        return success_single({"deleted_count": count})
    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_get_model(engine_type, model_id):
    """Get details for a single model."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import get_model as _gm

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    model = _gm(_CONFIG["db_path"], model_id)
    if model is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    # Verify the model belongs to this engine type
    if model.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                                f"Model {model_id} belongs to a different engine type")

    return success_single(model)


def api_update_model(engine_type, model_id):
    """Update an existing model entry."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import update_model as _um
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    # Verify model belongs to this engine type
    existing = None
    from db.adapters.models import get_model as _gm
    existing = _gm(_CONFIG["db_path"], model_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")
    if existing.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                                f"Model {model_id} belongs to a different engine type")

    try:
        updated = _um(_CONFIG["db_path"], model_id,
                        name=body.get("name"),
                        model_path=body.get("model_path"),
                        mmproj_path=body.get("mmproj_path") or None,
                        draft_model_path=body.get("draft_model_path") or None,
                        size_bytes=body.get("size_bytes"),
                        last_modified=body.get("last_modified"),
                        host_id=body.get("host_id"),
                        model_params=body.get("model_params"),
                        is_active=int(body.get("is_active", 1)))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(updated)


def api_scan_models(engine_type):
    """Trigger a remote model scan via Ansible playbook.

    Scans all active nodes for GGUF model files using the scan_models playbook.
    Results are upserted into the engine_models table.

    Returns:
        dict with scan results including count of new/stale models.
    """
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.nodes import list_nodes as _ln
    from lib.lib_ansible_runner import scan_models as _scan
    from db.adapters.playbooks import resolve_playbook_by_id

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    # Optional node_id to target a specific node (default: scan all active)
    from db.adapters.nodes import list_nodes as _ln2
    target_node_id = request.args.get("node_id")
    nodes = _ln2(_CONFIG["db_path"])
    active_nodes = [n for n in nodes if n.get("status") == "active"]

    if not active_nodes:
        return error_response("NO_NODES", "No active nodes available for scanning")

    # Build host limit — single targeted node or all active
    if target_node_id:
        target = next((n for n in active_nodes if str(n.get("id")) == str(target_node_id)), None)
        if not target:
            return error_response("RESOURCE_NOT_FOUND",
                                f"Node {target_node_id} not found or not active")
        limit_str = target.get("hostname", target.get("name", ""))
    else:
        hostnames = [n.get("hostname", n.get("name", "")) for n in active_nodes]
        limit_str = ",".join(hostnames)

    pb = resolve_playbook_by_id(_CONFIG["db_path"], "NODE_SCAN_MODELS_V1")
    if not pb:
        return error_response("PLAYBOOK_MISSING", "node/scan_models.yml not found in playbook registry")
    playbook = os.path.join(_project_root, pb["file_path"])
    # Dynamic inventory — no file generated (DI-7)

    try:
        result = _scan(engine_type_id=et_id, limit=limit_str,
                       db_path=_CONFIG["db_path"])
        return success_single(result)
    except Exception as exc:
        return error_response("SCAN_FAILED", str(exc))


def api_scan_models_agnostic():
    """Engine-agnostic GGUF model discovery on a specific node.

    Required params:
        ?node=<node_id> -- node to scan (required, no default "scan all")
        ?compute_checksums=0|1 -- include SHA256 computation (default 0)

    Returns:
        JSON with new/existing/mismatch counts and discovered model details.
    """
    from db.adapters.engine_types import list_engine_types as _let
    from db.adapters.nodes import list_nodes as _ln
    from lib.lib_ansible_runner import scan_models as _scan

    # Required: node parameter
    target_node_id = request.args.get("node")
    if not target_node_id:
        return error_response("VALIDATION_ERROR", "?node=<id> is required")

    nodes = _ln(_CONFIG["db_path"])
    active_nodes = [n for n in nodes if n.get("status") == "active"]

    target = next((n for n in active_nodes if str(n.get("id")) == str(target_node_id)), None)
    if not target:
        return error_response("RESOURCE_NOT_FOUND",
                              f"Node {target_node_id} not found or not active")

    # compute_checksums default to 0 (fast scan)
    compute_checksums = request.args.get("compute_checksums", "0") == "1"

    # Use the llama_server engine_type_id for scanning
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], QR_ENGINE_LLAMA_SERVER_NAME)
    et_id = et["id"] if et else None
    if not et_id:
        return error_response("RESOURCE_NOT_FOUND", "llama_server engine type not found")

    limit_str = target.get("hostname", target.get("name", ""))

    try:
        result = _scan(engine_type_id=et_id, limit=limit_str,
                       db_path=_CONFIG["db_path"])
        # Add compute_checksums flag for future use
        result["compute_checksums"] = compute_checksums
        return success_single(result)
    except Exception as exc:
        return error_response("SCAN_FAILED", str(exc))


def api_verify_checksum(engine_type, model_id):
    """Async checksum verification for a model's files.

    POST returns immediately with {status: "accepted"}.
    WebUI polls model detail page for updated sha256_verified_at timestamps.

    Args:
        engine_type: Engine type name (e.g., llama_server).
        model_id: Integer primary key of the model.

    Returns:
        JSON with status "accepted".
    """
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import get_model as _gm

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    model = _gm(_CONFIG["db_path"], model_id)
    if model is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    if model.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                              f"Model {model_id} belongs to a different engine type")

    # For now, just mark that verification was requested.
    # Actual SHA256 computation would run via Ansible on the remote node.
    # The WebUI will poll for updated sha256_verified_at values.
    return jsonify({"status": "accepted", "model_id": model_id}), 202


def api_checksum_diff(engine_type):
    """List models where computed hash != expected hash or not yet verified.

    Args:
        engine_type: Engine type name.

    Returns:
        List of models with hash mismatch or missing verification.
    """
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import list_models as _lm

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    models = _lm(_CONFIG["db_path"], engine_type_id=et_id)
    diff_models = [m for m in models
                   if not m.get("sha256_model") or not m.get("sha256_verified_at_model")]

    return success_list(diff_models)


# ---------------------------------------------------------------------------
# Global config endpoints
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
    query = ("SELECT a.*, n.name as node_name, i.name as instance_name, "
                "p.file_path as playbook_file, p.version as playbook_version "
                "FROM ansible_actions a "
                "LEFT JOIN nodes n ON a.node_id = n.id "
                "LEFT JOIN instances i ON a.instance_id = i.id "
                "LEFT JOIN playbook_registry p ON a.playbook_registry_id = p.id "
                "WHERE 1=1")
    params = []

    if node_id:
        query += " AND a.node_id = ?"
        params.append(node_id)
    if instance_id:
        query += " AND a.instance_id = ?"
        params.append(instance_id)
    if action_type:
        query += " AND a.action_type = ?"
        params.append(action_type)

    query += " ORDER BY a.finished_at DESC LIMIT ?"
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

    query = ("SELECT a.*, n.name as node_name, i.name as instance_name "
             "FROM qr_actions a "
             "LEFT JOIN nodes n ON a.node_id = n.id "
             "LEFT JOIN instances i ON a.instance_id = i.id "
             "WHERE 1=1")
    params = []

    if status_filter:
        query += " AND a.status = ?"
        params.append(status_filter)
    if node_id:
        query += " AND a.node_id = ?"
        params.append(node_id)
    if instance_id:
        query += " AND a.instance_id = ?"
        params.append(instance_id)
    if action_type:
        query += " AND a.action_type = ?"
        params.append(action_type)

    # Running tasks first, then newest
    query += " ORDER BY CASE WHEN a.status='running' THEN 0 ELSE 1 END, a.created_at DESC LIMIT ?"
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
            except Exception:
                pass

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
        query = "DELETE FROM ansible_actions"
        with pool(_CONFIG["db_path"]) as conn:
            cur = conn.execute(query)
            deleted = cur.rowcount
            conn.commit()
        return success_single({"deleted_count": deleted, "clear_all": True})

    if days < 1:
        return error_response("days must be >= 0 (0=clear all)", code="invalid_params")

    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    query = "DELETE FROM ansible_actions WHERE started_at < ?"
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
        query = "DELETE FROM qr_actions"
        with pool(_CONFIG["db_path"]) as conn:
            cur = conn.execute(query)
            deleted = cur.rowcount
            conn.commit()
        return success_single({"deleted_count": deleted, "clear_all": True})

    if days < 1:
        return error_response("days must be >= 0 (0=clear all)", code="invalid_params")

    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    query = "DELETE FROM qr_actions WHERE started_at < ?"
    with pool(_CONFIG["db_path"]) as conn:
        cur = conn.execute(query, (cutoff,))
        deleted = cur.rowcount
        conn.commit()

    return success_single({"deleted_count": deleted})


# ---------------------------------------------------------------------------
# WebUI Settings (centralized browser settings store)
# ---------------------------------------------------------------------------

def api_get_webui_settings():
    """Get WebUI user settings from request header or defaults.
    
    Client sends settings via X-QR-Settings header as JSON string.
    Returns stored settings or empty object if none provided.
    """
    settings_raw = request.headers.get("X-QR-Settings", "")
    try:
        settings = json.loads(settings_raw) if settings_raw else {}
    except (json.JSONDecodeError, TypeError):
        settings = {}
    return success_single(settings)


def api_set_webui_settings():
    """Store WebUI user settings in DB (server-side backup).
    
    Body: {"page": "<page_name>", "settings": {key: value, ...}}
    Returns: { status: "ok", data: { saved: true } }
    """
    from db.sqlite import pool
    body = request.get_json(silent=True) or {}
    page = str(body.get("page", "")).strip()
    settings = body.get("settings", {})
    if not page or not settings:
        return error_response("VALIDATION_ERROR", "page and settings required")

    try:
        with pool(_CONFIG["db_path"]) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO config_global (key, value) VALUES ('webui_settings_" + str(page) + "', ?)",
                (json.dumps(settings),)
            )
            conn.commit()
    except Exception:
        pass  # Non-critical — client localStorage is the primary store
    return success_single({"saved": True})


# ---------------------------------------------------------------------------
# Benchmarks — prompt CRUD + run management
# ---------------------------------------------------------------------------

def api_list_prompts():
    """List all benchmark prompts sorted by created_at desc."""
    from lib.lib_benchmarks import list_prompts as _lp
    items = _lp(_CONFIG["db_path"])
    return success_list(items, total=len(items))


def api_get_prompt(prompt_id):
    """Get a single benchmark prompt by ID."""
    from lib.lib_benchmarks import get_prompt as _gp
    prompt = _gp(_CONFIG["db_path"], prompt_id)
    if not prompt:
        return error_response("NOT_FOUND", f"Prompt #{prompt_id} not found")
    return success_single(prompt)


def api_create_prompt():
    """Create a new benchmark prompt."""
    from lib.lib_benchmarks import create_prompt as _cp
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    max_tokens = body.get("max_tokens") or 20
    try:
        prompt = _cp(_CONFIG["db_path"], name, content, max_tokens=max_tokens)
        return success_single(prompt)
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "PROMPT_DUPLICATE":
            return error_response("DUPLICATE_NAME", f"Prompt '{name}' already exists")
        return error_response("CREATE_FAILED", msg)


def api_update_prompt(prompt_id):
    """Update an existing benchmark prompt."""
    from lib.lib_benchmarks import update_prompt as _up
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    name = body.get("name")
    content = body.get("content")
    max_tokens = body.get("max_tokens")
    try:
        prompt = _up(_CONFIG["db_path"], prompt_id, name=name, content=content, max_tokens=max_tokens)
        return success_single(prompt)
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "PROMPT_NOT_FOUND":
            return error_response("RESOURCE_NOT_FOUND", f"Prompt {prompt_id} not found")
        return error_response("UPDATE_FAILED", msg)


def api_delete_prompt(prompt_id):
    """Delete a benchmark prompt."""
    from lib.lib_benchmarks import delete_prompt as _dp
    try:
        deleted = _dp(_CONFIG["db_path"], prompt_id)
        if not deleted:
            return error_response("RESOURCE_NOT_FOUND", f"Prompt {prompt_id} not found")
        return success_single({"deleted": True})
    except RuntimeError as exc:
        return error_response("DELETE_FAILED", str(exc))


def api_start_benchmark():
    """Start a benchmark run on a llama.cpp instance.

    Fire-and-forget: returns immediately with a run_id.
    The actual benchmark runs in a background thread.
    Interlock: only one benchmark per instance at a time.
    Override flag skips MODEL_MISMATCH check.
    """
    import uuid as _uuid
    from lib.lib_benchmarks import check_interlock as _ci, start_benchmark as _sb
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    instance_id = body.get("instance_id")
    prompt_id = body.get("prompt_id")
    override = body.get("override", False)

    if instance_id is None or prompt_id is None:
        return error_response("VALIDATION_ERROR", "instance_id and prompt_id are required")

    try:
        instance_id = int(instance_id)
        prompt_id = int(prompt_id)
    except (ValueError, TypeError):
        return error_response("VALIDATION_ERROR", "instance_id and prompt_id must be integers")

    # Interlock check
    active_run_id, interlock_err = _ci(_CONFIG["db_path"], instance_id)
    if active_run_id and not override:
        return error_response(
            "BENCHMARK_RUNNING",
            f"Benchmark already running for instance {instance_id}. "
            "Use override=true to force.",
            status_code=409,
            detail={"active_run_id": active_run_id},
        )

    run_id = _uuid.uuid4().hex[:12]
    try:
        result = _sb(_CONFIG["db_path"], run_id, instance_id, prompt_id, override=override)
        return success_single(result)
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "INSTANCE_NOT_FOUND":
            return error_response("RESOURCE_NOT_FOUND", f"Instance {instance_id} not found")
        elif msg.startswith("INSTANCE_NOT_RUNNING:"):
            state = msg.split(":", 1)[1]
            return error_response(
                "INSTANCE_NOT_RUNNING",
                f"Instance {instance_id} is not running (state={state})",
                status_code=409,
            )
        elif msg == "INSTANCE_NO_PORT":
            return error_response(
                "INSTANCE_NO_PORT",
                f"Instance {instance_id} has no port assigned",
                status_code=409,
            )
        elif msg == "PROMPT_NOT_FOUND":
            return error_response("RESOURCE_NOT_FOUND", f"Prompt {prompt_id} not found")
        elif msg.startswith("MODEL_MISMATCH"):
            return error_response(
                "MODEL_MISMATCH",
                msg,
                status_code=409,
                detail={"override_hint": "Set override=true in request body to skip model verification"},
            )
        return error_response("BENCHMARK_START_FAILED", msg)


def api_list_results():
        """List benchmark results for an instance (or all instances).

        Query params:
        - instance_id: Integer instance ID, or "all" for all instances.
        - limit: Max rows to return (default 50).

        When instance_id is omitted or "all", returns results across all
        instances sorted by started_at DESC — useful for group execution
        verification and multi-node benchmark management.
        """
        from lib.lib_benchmarks import get_results as _gr, list_all_results as _lar
        instance_id = request.args.get("instance_id")
        limit = int(request.args.get("limit", 50))
        if instance_id is None or instance_id.strip() == "" or instance_id.lower() == "all":
            items = _lar(_CONFIG["db_path"], limit=limit)
            return success_list(items, total=len(items))
        try:
            instance_id_int = int(instance_id)
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "instance_id must be an integer or 'all'")
        items = _gr(_CONFIG["db_path"], instance_id_int, limit=limit)
        return success_list(items, total=len(items))


def api_get_result_detail(run_id):
    """Get full benchmark result detail including complete output."""
    from lib.lib_benchmarks import get_result_detail as _grd
    result = _grd(_CONFIG["db_path"], run_id)
    if result is None:
        return error_response("RESOURCE_NOT_FOUND", f"Run {run_id} not found")
    return success_single(result)


def api_get_progress(run_id):
    """Get current progress of a benchmark run (for polling from WebUI)."""
    from lib.lib_benchmarks import get_progress as _gp
    result = _gp(_CONFIG["db_path"], run_id)
    if result is None:
        return error_response("RESOURCE_NOT_FOUND", f"Run {run_id} not found")
    return success_single(result)


def api_clear_results():
    """Clear all benchmark results."""
    from lib.lib_benchmarks import clear_results as _cr
    count = _cr(_CONFIG["db_path"])
    return success_single({"deleted": count})


def api_delete_benchmark_run(run_id):
    """Delete a single benchmark result by run_id (for stale stuck runs)."""
    from lib.lib_benchmarks import delete_result as _dr
    count = _dr(_CONFIG["db_path"], run_id)
    if count == 0:
        return error_response("RESOURCE_NOT_FOUND", f"Benchmark run {run_id} not found")
    return success_single({"deleted": run_id})


# ---------------------------------------------------------------------------
# Phase 2: System-engine management endpoints
# ---------------------------------------------------------------------------

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
    """Build web UI settings dict from engine_configs table only.

    Used for system-managed instances (engine_id < 100) where
    per-instance config_override is not used.

    Args:
        db_path: Database path.
        inst: Instance dict with engine_type_id.

    Returns:
        dict with web_ui_host, web_ui_port, web_ui_timezone, webui_autostart, webui_detach.
    """
    from db.adapters.configs import get_engine_config as _gec_web, get_polling_intervals
    et_id = inst.get("engine_type_id")
    host_row = _gec_web(db_path, et_id, "web_ui_host") or {}
    port_row = _gec_web(db_path, et_id, "web_ui_port") or {}
    tz_row = _gec_web(db_path, et_id, "web_ui_timezone") or {}
    def_host = host_row.get("value", "") if host_row else ""
    def_port = int(port_row["value"]) if port_row and port_row.get("value") else 0
    def_tz = tz_row.get("value", DEFAULT_TIMEZONE) if tz_row else DEFAULT_TIMEZONE
    autostart_row = _gec_web(db_path, et_id, "webui_autostart") or {}
    def_autostart = autostart_row.get("value", "true") if autostart_row else "true"
    detach_row = _gec_web(db_path, et_id, "webui_detach") or {}
    def_detach = detach_row.get("value", "false") if detach_row else "false"
    return {
        "web_ui_host": def_host,
        "web_ui_port": str(inst.get("port_assigned") or def_port),
        "web_ui_timezone": def_tz,
        "webui_autostart": def_autostart,
        "webui_detach": def_detach,
    }


def api_web_server_settings():
    """Get current web server settings (port, host)."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    config = engine.get_config(inst["id"], _CONFIG["db_path"])
    # For system-managed instances (engine_id < 100), all settings come from engine_configs only.
    # Non-system instances may use per-instance config_override as an additional layer.
    is_system = inst.get("system_managed")
    if is_system:
        result = _get_webui_settings_from_engine_config(_CONFIG["db_path"], inst)
    else:
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                import json as _jc
                co = _jc.loads(co)
            except Exception:
                co = {}
        from db.adapters.configs import get_engine_config as _gec_web, get_polling_intervals
        et_id = inst.get("engine_type_id")
        host_row = _gec_web(_CONFIG["db_path"], et_id, "web_ui_host") or {}
        port_row = _gec_web(_CONFIG["db_path"], et_id, "web_ui_port") or {}
        tz_row = _gec_web(_CONFIG["db_path"], et_id, "web_ui_timezone") or {}
        def_host = host_row.get("value", "") if host_row else ""
        def_port = int(port_row["value"]) if port_row and port_row.get("value") else 0
        def_tz = tz_row.get("value", DEFAULT_TIMEZONE) if tz_row else DEFAULT_TIMEZONE
        autostart_row = _gec_web(_CONFIG["db_path"], et_id, "webui_autostart") or {}
        def_autostart = autostart_row.get("value", "true") if autostart_row else "true"
        detach_row = _gec_web(_CONFIG["db_path"], et_id, "webui_detach") or {}
        def_detach = detach_row.get("value", "false") if detach_row else "false"
        result = {
            "web_ui_host": co.get("web_ui_host") or def_host,
            "web_ui_port": str(co.get("web_ui_port") or inst.get("port_assigned") or def_port),
            "web_ui_timezone": co.get("web_ui_timezone") or def_tz,
            "webui_autostart": def_autostart,
            "webui_detach": co.get("webui_detach") or def_detach,
        }
    # Add polling intervals from engine_configs (not per-instance)
    try:
        from db.sqlite import pool as _pool
        with _pool(_CONFIG["db_path"]) as conn:
            first_engine = conn.execute("SELECT id FROM engine_types WHERE name='quickrobot-api' LIMIT 1").fetchone()
            if first_engine:
                api_et_id = first_engine[0]
                local_poll = get_polling_intervals(_CONFIG["db_path"], api_et_id, is_local=True) or "10000"
                remote_poll = get_polling_intervals(_CONFIG["db_path"], api_et_id, is_local=False) or "600000"
                result["polling_interval_local_sec"] = local_poll
                result["polling_interval_remote_sec"] = remote_poll
    except Exception:
        pass  # Non-critical — polling values are engine-level, not instance-level
    return success_single(result)


def api_web_server_update_settings():
    """Update web server settings (port, host)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    # Separate engine_configs (global) from instance config_override (per-instance)
    engine_cfg_keys = ("web_ui_port", "web_ui_host", "web_ui_timezone", "webui_autostart", "webui_detach")
    config = {k: v for k, v in body.items() if k in engine_cfg_keys}
    # Save engine_configs first (global settings)
    et_id = inst.get("engine_type_id")
    try:
        from db.adapters.configs import update_engine_config as _uec
        for k, v in config.items():
            if k in ("webui_autostart", "webui_detach") and isinstance(v, bool):
                v = "true" if v else "false"
            elif k in ("webui_autostart", "webui_detach") and isinstance(v, str):
                v = "true" if v.lower() in ("true", "1", "yes") else "false"
            _uec(_CONFIG["db_path"], et_id, k, str(v))
    except Exception as exc:
        print(f"[qr] WARNING: failed to update engine config: {exc}")
    # For system-managed instances (engine_id < 100), skip per-instance overrides
    # All settings are global via engine_configs — prevents divergence between
    # config_override and engine_configs that causes "settings revert" bugs.
    result = {}
    if not inst.get("system_managed"):
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                import json as _jc2
                co = _jc2.loads(co)
            except Exception:
                co = {}
        for k, v in config.items():
            co[k] = v
        result = engine.set_config(inst["id"], co, _CONFIG["db_path"])
    return success_single(result)


def api_web_server_update_setting(key):
    """Get or update a single web server setting by key."""
    from db.adapters.configs import get_engine_config as _gec, update_engine_config as _uec

    editable_keys = ("web_ui_port", "web_ui_host", "web_ui_timezone",
                     "webui_autostart", "webui_detach")
    if key not in editable_keys:
        return error_response("INVALID_KEY", f"Editable keys: {', '.join(editable_keys)}")

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    et_id = inst.get("engine_type_id")

    if request.method == "GET":
        row = _gec(_CONFIG["db_path"], et_id, key) or {}
        value = row.get("value", "")
        return success_single({"engine_type": QR_ENGINE_WEBUI_NAME, "key": key, "value": value})

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid json"))

    value = body.get("value")
    if value is None:
        return error_response("VALIDATION_ERROR", '"value" field required')

    try:
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, str):
            value = value.strip()
        _uec(_CONFIG["db_path"], et_id, key, str(value))
    except Exception as exc:
        return error_response("WRITE_ERROR", str(exc))

    return success_single({"engine_type": QR_ENGINE_WEBUI_NAME, "key": key, "value": str(value)})


def api_web_server_start():
    """Start the web UI server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    result = engine.execute(inst["id"], "start", _CONFIG["db_path"])
    return success_single(result)


def api_web_server_stop():
    """Stop the web UI server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    result = engine.execute(inst["id"], "stop", _CONFIG["db_path"])
    return success_single(result)


def api_web_server_restart():
    """Restart the web UI server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    result = engine.execute(inst["id"], "restart", _CONFIG["db_path"])
    return success_single(result)


def api_web_server_status():
    """Check if the web UI server is running."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    status = engine.get_status(inst["id"], _CONFIG["db_path"])
    return success_single(status)


def api_mcp_settings():
    """Get MCP server settings (port, flags)."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    # For system-managed instances (engine_id < 100), all settings come from engine_configs only.
    if inst.get("system_managed"):
        et_id = inst.get("engine_type_id")
        from db.adapters.configs import get_engine_config as _gec
        from lib.lib_system_engine import load_env_config as _lec
        port_row = _gec(_CONFIG["db_path"], et_id, "mcp_port") or {}
        api_host_row = _gec(_CONFIG["db_path"], et_id, "mcp_api_host") or {}
        python_interp_row = _gec(_CONFIG["db_path"], et_id, "mcp_python_interpreter") or {}
        reads_row = _gec(_CONFIG["db_path"], et_id, "mcp_allow_reads") or {}
        writes_row = _gec(_CONFIG["db_path"], et_id, "mcp_allow_writes") or {}
        proxy_row = _gec(_CONFIG["db_path"], et_id, "mcp_allow_proxy") or {}
        detach_row = _gec(_CONFIG["db_path"], et_id, "mcp_detach") or {}
        # MCP listen host and autostart: read from .quickrobot.env (not API bind address)
        # DB override applies only if user explicitly set mcp_autostart via API
        try:
            env_cfg = _lec(os.getcwd())
            mcp_host_val = env_cfg["QUICKROBOT_MCP_HOST"]
            mcp_autostart_val = str(env_cfg.get("QUICKROBOT_MCP_AUTOSTART", "true")).lower() in ("true", "1")
        except FileNotFoundError:
            mcp_host_val = _CONFIG["host"]
            mcp_autostart_val = True
        # DB override: if user explicitly set mcp_autostart via API, use DB value
        autostart_row = _gec(_CONFIG["db_path"], et_id, "mcp_autostart") or {}
        if autostart_row:
            mcp_autostart_val = str(autostart_row.get("value", "false")).lower() in ("true", "1")
        result = {
            "mcp_port": str(inst.get("port_assigned") or port_row.get("value", "")),
            "mcp_host": mcp_host_val,
            "mcp_api_base": f"http://{_CONFIG['host']}:{_CONFIG['api_port']}/api/v1",
            "mcp_python_interpreter": python_interp_row.get("value", "") if python_interp_row else "",
            "mcp_autostart": str(mcp_autostart_val),
            "mcp_detach": str(detach_row.get("value", "false")),
            "allow_reads": "true" if (str(reads_row.get("value", "true")).lower() in ("true", "1", "yes") if reads_row else True) else "false",
            "allow_writes": "true" if (str(writes_row.get("value", "true")).lower() in ("true", "1", "yes") if writes_row else True) else "false",
            "allow_proxy": "true" if (str(proxy_row.get("value", "true")).lower() in ("true", "1", "yes") if proxy_row else True) else "false",
        }
    else:
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                import json as _jc
                co = _jc.loads(co)
            except Exception:
                co = {}
        et_id = inst.get("engine_type_id")
        from db.adapters.configs import get_engine_config as _gec2
        from lib.lib_system_engine import load_env_config as _lec
        port_row = _gec2(_CONFIG["db_path"], et_id, "mcp_port") or {}
        api_host_row = _gec2(_CONFIG["db_path"], et_id, "mcp_api_host") or {}
        python_interp_row = _gec2(_CONFIG["db_path"], et_id, "mcp_python_interpreter") or {}
        reads_row = _gec2(_CONFIG["db_path"], et_id, "mcp_allow_reads") or {}
        writes_row = _gec2(_CONFIG["db_path"], et_id, "mcp_allow_writes") or {}
        proxy_row = _gec2(_CONFIG["db_path"], et_id, "mcp_allow_proxy") or {}
        detach_row = _gec2(_CONFIG["db_path"], et_id, "mcp_detach") or {}
        # MCP listen host and autostart: config_override > env file
        try:
            env_cfg = _lec(os.getcwd())
            mcp_host_val = co.get("mcp_host") or env_cfg["QUICKROBOT_MCP_HOST"]
            mcp_autostart_val = str(env_cfg.get("QUICKROBOT_MCP_AUTOSTART", "true")).lower() in ("true", "1")
        except FileNotFoundError:
            mcp_host_val = co.get("mcp_host") or _CONFIG["host"]
            mcp_autostart_val = True
        # DB override: if user explicitly set mcp_autostart via API, use DB value
        autostart_row = _gec2(_CONFIG["db_path"], et_id, "mcp_autostart") or {}
        if autostart_row:
            mcp_autostart_val = str(autostart_row.get("value", "false")).lower() in ("true", "1")
        result = {
            "mcp_port": str(co.get("mcp_port") or inst.get("port_assigned") or port_row.get("value", "")),
            "mcp_host": mcp_host_val,
            "mcp_api_base": f"http://{_CONFIG['host']}:{_CONFIG['api_port']}/api/v1",
            "mcp_python_interpreter": co.get("mcp_python_interpreter") or python_interp_row.get("value", ""),
            "mcp_autostart": str(mcp_autostart_val),
            "mcp_detach": str(detach_row.get("value", "false")),
            "allow_reads": "true" if (str(reads_row.get("value", "true")).lower() in ("true", "1", "yes") if reads_row else True) else "false",
            "allow_writes": "true" if (str(writes_row.get("value", "true")).lower() in ("true", "1", "yes") if writes_row else True) else "false",
            "allow_proxy": "true" if (str(proxy_row.get("value", "true")).lower() in ("true", "1", "yes") if proxy_row else True) else "false",
        }
    return success_single(result)


def api_mcp_update_settings():
    """Update MCP server settings (port, flags)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    # Separate engine_configs (global) from instance config_override
    # Accept both prefixed (mcp_allow_reads) and non-prefixed (allow_reads) keys for backwards compat
    engine_cfg_keys = ("mcp_port", "mcp_api_host", "mcp_python_interpreter", "mcp_autostart", "mcp_detach", "mcp_allow_reads", "mcp_allow_writes", "mcp_allow_proxy")
    key_prefix_map = {"allow_reads": "mcp_allow_reads", "allow_writes": "mcp_allow_writes", "allow_proxy": "mcp_allow_proxy"}
    config = {}
    for k, v in body.items():
        if k in engine_cfg_keys:
            config[k] = v
        elif k in key_prefix_map and key_prefix_map[k] in engine_cfg_keys:
            config[key_prefix_map[k]] = v  # normalize: allow_reads → mcp_allow_reads
    et_id = inst.get("engine_type_id")
    try:
        from db.adapters.configs import update_engine_config as _uec
        for k, v in config.items():
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, str):
                v = v.strip()
            else:
                v = str(v)
            _uec(_CONFIG["db_path"], et_id, k, v)
    except Exception as exc:
        print(f"[qr] WARNING: failed to update MCP engine config: {exc}")

    # For system-managed instances, skip per-instance overrides — all settings are global via engine_configs
    result = {}
    if not inst.get("system_managed"):
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                co = json.loads(co)
            except Exception:
                co = {}
        for k, v in config.items():
            co[k] = v
        result = engine.set_config(inst["id"], co, _CONFIG["db_path"])

    # Restart MCP process to pick up new config (flags, interpreter, port, etc.)
    try:
        from db.adapters.instances import log_action as _log_act
        from quickrobot.routes_instances import _restart_system_managed
        _restart_system_managed(inst["id"], QR_ENGINE_MCP_NAME, _log_act)
    except Exception as exc:
        print(f"[qr] WARNING: failed to restart MCP after settings update: {exc}")

    return success_single(result)


def api_mcp_update_setting(key):
    """Get or update a single MCP server setting by key."""
    from db.adapters.configs import get_engine_config as _gec, update_engine_config as _uec

    editable_keys = ("mcp_port", "mcp_api_host", "mcp_python_interpreter",
                     "mcp_autostart", "mcp_detach", "mcp_allow_reads",
                     "mcp_allow_writes", "mcp_allow_proxy")
    key_map = {"allow_reads": "mcp_allow_reads", "allow_writes": "mcp_allow_writes",
               "allow_proxy": "mcp_allow_proxy"}
    actual_key = key_map.get(key, key)
    if actual_key not in editable_keys:
        return error_response("INVALID_KEY", f"Editable keys: {', '.join(editable_keys)}")

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    et_id = inst.get("engine_type_id")

    if request.method == "GET":
        row = _gec(_CONFIG["db_path"], et_id, actual_key) or {}
        value = row.get("value", "")
        return success_single({"engine_type": QR_ENGINE_MCP_NAME, "key": actual_key, "value": value})

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid json"))

    value = body.get("value")
    if value is None:
        return error_response("VALIDATION_ERROR", '"value" field required')

    try:
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, str):
            value = value.strip()
        _uec(_CONFIG["db_path"], et_id, actual_key, str(value))
    except Exception as exc:
        return error_response("WRITE_ERROR", str(exc))

    # Restart MCP to pick up the new setting (flags, interpreter, port, etc.)
    try:
        from db.adapters.instances import log_action as _log_act
        from quickrobot.routes_instances import _restart_system_managed
        _restart_system_managed(inst["id"], QR_ENGINE_MCP_NAME, _log_act)
    except Exception as exc:
        print(f"[qr] WARNING: failed to restart MCP after setting update ({actual_key}): {exc}")

    return success_single({"engine_type": QR_ENGINE_MCP_NAME, "key": actual_key, "value": str(value)})


def api_mcp_start():
    """Start the MCP SSE server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    result = engine.execute(inst["id"], "start", _CONFIG["db_path"])
    return success_single(result)


def api_mcp_stop():
    """Stop the MCP SSE server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    result = engine.execute(inst["id"], "stop", _CONFIG["db_path"])
    return success_single(result)


def api_mcp_restart():
    """Restart the MCP SSE server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)

    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    result = engine.execute(inst["id"], "restart", _CONFIG["db_path"])
    return success_single(result)


def api_mcp_status():
    """Check MCP server status."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    status = engine.get_status(inst["id"], _CONFIG["db_path"])
    return success_single(status)


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
    from quickrobot.lib_instances import override_system_instance_states as _osis
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
        "mode": _CONFIG["pb_mode"],
        "bind_host": _CONFIG["host"],
        "bind_port": _CONFIG["api_port"],
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


def api_health_check():
    """Bulk health check endpoint for nodes and instances.

    POST /api/v1/health/check with JSON body specifying scope:
        {"scope": "all"}            — refresh nodes + all running instances
        {"scope": "nodes"}          — re-discover all active nodes
        {"scope": "instances"}      — query-status on all running instances
        {"scope": "node:<id>"}      — refresh single node only
        {"scope": "instance:<id>"}  — query-status on single instance

    Returns aggregated results with per-item status.

    Args:
        scope: String controlling which entities to check (see above).

    Returns:
        JSON with keys: action (str), summary (dict), results (dict).
    """
    from db.adapters.nodes import get_node, list_nodes, update_node
    from db.adapters.instances import get_instance, list_instances
    from lib.lib_ansible_runner import validate_node as _validate_node
    from engine import get_engine

    try:
        body = request.get_json(silent=True) or {}
        scope = body.get("scope", "all")
    except Exception:
        return error_response("VALIDATION_ERROR", "Invalid JSON body")

    results = {"nodes": {}, "instances": {}}
    summary = {"nodes_checked": 0, "nodes_ok": 0, "nodes_fail": 0,
                "instances_checked": 0, "instances_ok": 0, "instances_fail": 0}

    # ---- NODE CHECKS ----
    if scope in ("all", "nodes"):
        all_nodes = list_nodes(_CONFIG["db_path"])
        for node in all_nodes:
            nid = node["id"]
            node_key = f"node_{nid}"
            # Skip localhost (id=1) — not SSH-accessible, skip nodes already unknown
            if nid == 1 or node.get("status") != "active":
                continue
            try:
                result = _validate_node(_CONFIG["db_path"], nid)
                connected = result.get("connected", False)
                error_msg = result.get("error")
                caps = result.get("capabilities", {})
                devices_raw = result.get("available_devices", [])

                # Parse available_devices: Jinja2 YAML renders lists as Python str repr
                if isinstance(devices_raw, str):
                    import ast as _ast
                    try:
                        devices = _ast.literal_eval(devices_raw.strip())
                        if not isinstance(devices, list):
                            devices = [devices_raw]
                    except (ValueError, SyntaxError):
                        devices = [devices_raw]
                else:
                    devices = devices_raw

                status = "active" if connected else "unknown"
                status_reason = "" if connected else (error_msg or "")

                update_node(_CONFIG["db_path"], nid, status=status,
                            status_reason=status_reason,
                            capabilities=json.dumps(caps),
                            available_devices=json.dumps(devices))

                summary["nodes_checked"] += 1
                if connected:
                    summary["nodes_ok"] += 1
                else:
                    summary["nodes_fail"] += 1
                results["nodes"][node_key] = {
                    "connected": connected,
                    "status": status,
                    "capabilities": caps,
                    "error": error_msg,
                }
            except Exception as exc:
                summary["nodes_checked"] += 1
                summary["nodes_fail"] += 1
                results["nodes"][node_key] = {
                    "connected": False, "status": "unknown",
                    "error": str(exc),
                }

    # ---- INSTANCE CHECKS ----
    if scope in ("instances", "all"):
        running_insts = list_instances(_CONFIG["db_path"], state="running")
    elif scope.startswith("instance:"):
        try:
            inst_id = int(scope.split(":", 1)[1])
        except (ValueError, IndexError):
            return error_response("VALIDATION_ERROR",
                                "Invalid scope format. Use 'node:<id>' or 'instance:<id>'")
        inst = get_instance(_CONFIG["db_path"], inst_id)
        running_insts = [inst] if inst else []
    else:
        running_insts = []

    for inst in (running_insts or []):
        iid = inst["id"]
        inst_key = f"instance_{iid}"
        try:
            engine_type = inst.get("engine_type_name", "")
            engine = get_engine(engine_type)
            if engine is None:
                alt = engine_type.replace("-", "_")
                engine = get_engine(alt)
            if engine is None:
                alt = engine_type.replace("_", "-")
                engine = get_engine(alt)

            if engine:
                result = engine.query_status(iid, _CONFIG["db_path"])
            else:
                result = {"alive": False, "latency_ms": None,
                            "error": f"Engine '{engine_type}' not loaded"}

            summary["instances_checked"] += 1
            new_state = None
            if result.get("alive"):
                summary["instances_ok"] += 1
                # Update last_state_change timestamp for healthy instances
                from db.adapters.instances import update_instance as _ui
                try:
                    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    _ui(_CONFIG["db_path"], iid,
                        last_state_change=ts)
                except Exception:
                    pass
                # Recover error/build_error instances when health check confirms alive
                from db.adapters.instances import get_instance as _gi
                cur = _gi(_CONFIG["db_path"], iid)
                if cur:
                    cur_state = cur.get("state", "")
                    if cur_state in ("error", "build_error") and not result.get("model_loading"):
                        try:
                            from db.adapters.instances import transition_state as _ts
                            _ts(_CONFIG["db_path"], iid, "running")
                            new_state = "running"
                        except Exception:
                            pass
            else:
                summary["instances_fail"] += 1

            resp_entry = {
                "name": inst.get("name", ""),
                "engine": engine_type,
                "alive": result.get("alive"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error"),
            }
            if new_state:
                resp_entry["new_state"] = new_state
            results["instances"][inst_key] = resp_entry
        except Exception as exc:
            summary["instances_checked"] += 1
            summary["instances_fail"] += 1
            results["instances"][inst_key] = {
                "name": inst.get("name", ""),
                "engine": inst.get("engine_type_name", ""),
                "alive": False, "latency_ms": None,
                "error": str(exc),
            }

    return success_single({
        "action": "health_check",
        "scope": scope,
        "summary": summary,
        "results": results,
    })


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_args():
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="quickrobot LAN Controller API Server")
    parser.add_argument("--port", type=int, default=None,
                        help="HTTP port to listen on (reads from .quickrobot.env if not set)")
    parser.add_argument("--host", default=None,
                        help="Host to bind to (reads from .quickrobot.env if not set)")
    parser.add_argument("--db-path", default="./data/quickrobot.db",
                        help="Path to SQLite database (default: ./data/quickrobot.db)")
    parser.add_argument("--migrations-dir", default="./migrations",
                        help="Path to migrations directory")
    parser.add_argument("--replace", action="store_true", default=False,
                        help="Kill existing instance and restart")
    parser.add_argument("--mode", choices=["dev", "prod", "dev-update"],
                        default="prod", help="Operational mode: prod (default, manual-import + strict integrity), "
                        "prod (manual-import + strict integrity), dev-update (auto-import + sync checksums)")
    parser.add_argument("--init", action="store_true", default=False,
                        help="Backup existing DB (rename with timestamp) and create fresh DB from scratch")
    parser.add_argument("--webui-detach", action="store_true", default=False,
                        help="Run WebUI in detached process group (survives API death; default: attached, dies with API)")
    parser.add_argument("--no-webui", action="store_true", default=False,
                        help="Do not auto-start WebUI subprocess on API boot (overrides webui_autostart)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Playbook Registry API (Phase 3)
# ---------------------------------------------------------------------------

def api_list_playbooks():
    """List all registered playbooks with optional filtering.

    Query params:
        file_type: filter by "core" or "custom"
        search: text filter matching playbook_id or file_path (case-insensitive)

    Returns list of playbook records sorted by type then path.
    """
    from db.adapters.playbooks import list_playbooks as _list_pb

    file_type = request.args.get("file_type")
    search = request.args.get("search", "")

    items = _list_pb(_CONFIG["db_path"],
                         file_type=file_type if file_type else None)

    # Apply text search filter (client-side to avoid adapter changes)
    if search:
        s = search.lower()
        items = [item for item in items
                 if s in str(item.get("playbook_id", "")).lower()
                 or s in str(item.get("file_path", "")).lower()]

    from lib.lib_utils import relative_age
    for item in items:
        if "age_created" not in item and item.get("created_at"):
            item["age_created"] = relative_age(item["created_at"])
        # Alias usage_counter_since_update as usage_count for API consumers
        if "usage_count" not in item and "usage_counter_since_update" in item:
            item["usage_count"] = item["usage_counter_since_update"]

    return success_list(items)


def api_register_playbook():
    """Register a new custom playbook in the DB registry.

    Request body:
        file_path (str, required): relative path from project root
            e.g., "playbooks/my_custom.yml"
        checksum (str, required): SHA256 hex digest of the file content
        tags (str, optional): comma-separated tags
            e.g., "deploy,custom,myengine"
        version (str, optional): version string (default "1")

    Returns registered playbook record.
    """
    from db.adapters.playbooks import register_playbook as _reg_pb, get_playbook_by_path as _get_pb

    data = request.get_json(silent=True) or {}

    file_path = data.get("file_path")
    checksum = data.get("checksum")

    if not file_path or not checksum:
        return error_response("MISSING_FIELD",
                                "Both 'file_path' and 'checksum' are required")

    # Verify the file exists on disk (file_path is relative to project root)
    full_path = os.path.join(_project_root, file_path)
    if not os.path.isfile(full_path):
        return error_response("FILE_NOT_FOUND",
                                f"Playbook file not found: {full_path}")

    # Recompute checksum to validate
    import hashlib
    with open(full_path, "rb") as f:
        actual_checksum = hashlib.sha256(f.read()).hexdigest()

    if actual_checksum != checksum:
        return error_response("CHECKSUM_MISMATCH",
                                f"Checksum mismatch: expected {checksum}, got {actual_checksum}")

    file_type = data.get("file_type", "custom")
    tags = data.get("tags", "")
    version = data.get("version", "1")

    # Register the playbook
    new_id = _reg_pb(_CONFIG["db_path"], file_path, checksum, file_type=file_type, tags=tags)
    record = _get_pb(_CONFIG["db_path"], file_path)
    if new_id is not None:
        print(f"[qr] Registered new playbook: {file_path} (id={new_id}, type={file_type})")

    return success_single({
        "action": "registered",
        "playbook": record
    })


def api_update_playbook(playbook_id):
    """Update a registered playbook's metadata.

    Request body (all fields optional):
        version (str): new version string
        tags (str): new comma-separated tags
        checksum (str): new SHA256 checksum
        file_type (str): change from "core" to "custom" or vice versa

    Returns updated playbook record.
    """
    from db.adapters.playbooks import get_playbook_by_path as _get_pb

    with db_pool(_CONFIG["db_path"]) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE id = ?", (playbook_id,)
        ).fetchone()
        if row is None:
            return error_response("NOT_FOUND", f"Playbook ID {playbook_id} not found")

        data = request.get_json(silent=True) or {}

        update_fields = []
        params = []
        for field in ("version", "tags", "checksum", "file_type"):
            if field in data:
                update_fields.append(f"{field} = ?")
                params.append(data[field])

        if not update_fields:
            return error_response("NO_CHANGES", "No valid fields to update")

        update_fields.append("updated_at = datetime('now')")
        params.append(playbook_id)

        conn.execute(
            f"UPDATE playbook_registry SET {', '.join(update_fields)} WHERE id = ?",
            tuple(params),
        )

    updated_record = _get_pb(_CONFIG["db_path"], row["file_path"])
    return success_single({"action": "updated", "playbook": updated_record})


def api_delete_playbook(playbook_id):
    """Remove a playbook from the registry (does not delete file on disk).

    Returns confirmation with removed playbook info.
    """
    from db.adapters.playbooks import get_playbook_by_path as _get_pb

    with db_pool(_CONFIG["db_path"]) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE id = ?", (playbook_id,)
        ).fetchone()
        if row is None:
            return error_response("NOT_FOUND", f"Playbook ID {playbook_id} not found")

        file_path = row["file_path"]

        conn.execute(
            "DELETE FROM playbook_registry WHERE id = ?", (playbook_id,)
        )

    return success_single({
        "action": "removed",
        "playbook": {"id": playbook_id, "file_path": file_path}
    })


def api_reset_playbook_counters():
    """Reset usage and error counters for all (or a specific) playbook.

    Request body (optional):
        playbook_id: int — reset only this playbook; omit to reset all

    Returns count of playbooks whose counters were reset.
    """
    from db.adapters.playbooks import reset_counters as _reset_cb

    data = request.get_json(silent=True) or {}
    pb_id = data.get("playbook_id")

    count = _reset_cb(_CONFIG["db_path"], playbook_id=pb_id)
    return success_single({"action": "counters_reset", "reset_count": count})


def api_rescan_playbooks():
    """Re-scan the playbooks directory and register/update any changes.

    Compares current file checksums against DB records. Adds new files,
    updates changed checksums, and optionally removes deleted files.

    Query params:
        remove_deleted (bool, default false): also remove entries for files no longer on disk

    Returns count of registered, updated, and removed playbooks.
    """
    from db.adapters.playbooks import (
        register_playbook as _reg_pb,
        list_playbooks as _list_pb,
    )
    import hashlib

    # Read playbook_root_dir from env file (migrated from engine_configs)
    qr_env = _CONFIG.get("qr_env_config", {})
    pb_root_dir = qr_env.get("QUICKROBOT_API_PLAYBOOKDIR") or "playbooks/"
    root_dir = os.path.join(_project_root, pb_root_dir.lstrip("/"))
    registered = 0
    updated = 0
    removed = 0

    # Collect all current files on disk
    current_files = {}
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fname in filenames:
            if not (fname.endswith(".yml") or fname.endswith(".yaml")):
                continue
            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, _project_root)
            with open(full_path, "rb") as f:
                checksum = hashlib.sha256(f.read()).hexdigest()
            current_files[rel_path] = {
                "checksum": checksum,
                "full_path": full_path,
            }

    # Compare against DB records
    all_records = _list_pb(_CONFIG["db_path"], file_type="core")
    db_paths = {r["file_path"]: r for r in all_records}

    for fpath, info in current_files.items():
        if fpath not in db_paths:
            # New file — register it
            _reg_pb(_CONFIG["db_path"], fpath, info["checksum"], file_type="core", tags="")
            registered += 1
            print(f"[qr] Rescan: registered new playbook: {fpath}")
        elif db_paths[fpath]["checksum_sha256"] != info["checksum"]:
            # Changed file — update checksum
            with db_pool(_CONFIG["db_path"]) as c:
                c.execute(
                    "UPDATE playbook_registry SET checksum_sha256 = ?, updated_at = datetime('now') WHERE file_path = ?",
                    (info["checksum"], fpath),
                )
            updated += 1

    # Handle deletions
    remove_deleted = request.args.get("remove_deleted", "false").lower() == "true"
    if remove_deleted:
        for db_path_key, db_info in db_paths.items():
            if db_path_key not in current_files:
                with db_pool(_CONFIG["db_path"]) as c:
                    c.execute(
                        "DELETE FROM playbook_registry WHERE file_path = ?", (db_path_key,)
                    )
                removed += 1

    return success_single({
         "action": "rescan",
         "registered": registered,
         "updated": updated,
         "removed": removed,
         "total_registered": len(current_files) + (len(all_records) - registered - removed)
     })


def api_playbook_content(playbook_id):
    """Return the raw YAML content of a playbook for browser display.

    Used by WebUI /webui/playbooks/<id> route to show formatted playbook content.
    Also returns checksum and metadata for integrity verification in the UI.

    Args:
        playbook_id: Integer primary key from playbook_registry.

    Returns:
        JSON with playbook_id, file_path, checksum_sha256, playbook_name,
        and content (raw YAML string).
    """
    from db.sqlite import pool

    with pool(_CONFIG["db_path"]) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE id = ?",
            (playbook_id,),
        ).fetchone()

    if row is None:
        return error_response("RESOURCE_NOT_FOUND", f"Playbook ID {playbook_id} not found in registry")

    pb = {k: row[k] for k in row.keys()}
    file_path = pb.get("file_path", "")
    full_path = os.path.join(_project_root, file_path) if not os.path.isabs(file_path) else file_path

    if not os.path.exists(full_path):
        return error_response("FILE_MISSING", f"Playbook file not found on disk: {full_path}")

    try:
        with open(full_path, "r") as f:
            content = f.read()
    except Exception as exc:
        return error_response("READ_ERROR", f"Could not read playbook file: {exc}")

    return success_single({
        "playbook_id": pb.get("id"),
        "file_path": file_path,
        "playbook_name": os.path.basename(file_path),
        "checksum_sha256": pb.get("checksum_sha256", ""),
        "content": content,
    })


def _kill_subprocesses_on_exit():
    """Kill WebUI (inst 2) and MCP (inst 3) subprocesses when API exits.
    Ensures they don't survive API death when detach=false."""
    import sys as _sys
    import signal as _sig
    try:
        db_path = _CONFIG.get("db_path")
        if not db_path:
            return
        from db.sqlite import pool as _pool
        conn = _pool(db_path)
        for inst_id in (2, 3):
            row = conn.execute(
                "SELECT pid_last_known, state FROM instances WHERE id=?", (inst_id,)
            ).fetchone()
            if row and row[0]:
                pid = row[0]
                try:
                    os.kill(pid, _sig.SIGTERM)
                    import time as _t; _t.sleep(0.3)
                    try:
                        os.kill(pid, 0)
                        os.kill(pid, _sig.SIGKILL)
                    except OSError:
                        pass
                    # Update DB state to stopped
                    conn.execute(
                        "UPDATE instances SET state='stopped', pid_last_known=NULL WHERE id=?",
                        (inst_id,)
                    )
                except OSError:
                    pass  # already dead
        conn.commit()
        conn.close()
    except Exception:
        pass

def _handle_signal(signum, frame):
    """Signal handler: kill subprocesses before exit."""
    # Use process group kill — all our subprocesses share the API's process group
    import signal as _sig
    import time as _time
    try:
        import psutil as _ps
        api_proc = _ps.Process(os.getpid())
        for child in api_proc.children(recursive=True):
            try:
                child.terminate()
            except Exception:
                pass
        _time.sleep(0.5)
        for child in api_proc.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
    except ImportError:
        # Fallback: use os.killpg to kill entire process group
        try:
            import time as _time
            os.killpg(os.getpgrp(), _sig.SIGTERM)
            _time.sleep(0.5)
            os.killpg(os.getpgrp(), _sig.SIGKILL)
        except Exception:
            pass
    os._exit(0)


if __name__ == "__main__":
    # Register signal handlers for clean shutdown
    import signal as _signal_mod
    _signal_mod.signal(_signal_mod.SIGTERM, _handle_signal)
    _signal_mod.signal(_signal_mod.SIGINT, _handle_signal)
    """Entry point — delegate full startup to lib.lib_startup_pipeline.run_startup(),
    then launch the Flask app with resolved host/port from _CONFIG."""
    from lib.lib_startup_pipeline import run_startup
    run_startup()
    # Print startup banner using resolved config values
    mode_label = _CONFIG.get("pb_mode", "prod")
    print(f"[qr] quickrobot API server starting on {_CONFIG['host']}:{_CONFIG['api_port']}")
    print(f"[qr] version={VERSION} mode={mode_label}")
    try:
        app.run(host=_CONFIG["host"], port=_CONFIG["api_port"], debug=False)
    except OSError as exc:
        if "Address already in use" in str(exc):
            print(f"FATAL: Port {_CONFIG['api_port']} is already in use. Another instance is running on this host. Exiting.", file=sys.stderr)
        else:
            print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
