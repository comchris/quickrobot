from flask import request, jsonify
import logging as _logging

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_ENGINE_LLAMA_RPC_NAME, QR_SSH_PORT_DEFAULT
from qr_api.lib_instances import _execute_playbook, _resolve_engine_playbook_id
from lib.lib_constants import DEFAULT_ANSIBLE_USER
from lib.lib_qr_actions import log_qr_action

logger = _logging.getLogger(__name__)


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

    # Guard: no duplicate name or hostname allowed (NO FALLBACK — reject hard)
    from db.adapters.nodes import check_duplicate_name_or_hostname as _cdn
    conflicts = _cdn(_CONFIG["db_path"], name, hostname)
    if conflicts:
        details = []
        for c in conflicts:
            existing = c.get('existing_name', '?')
            details.append(f"{c['column']}='{c['value']}' (used by node '{existing}', id={c['existing_node_id']})")
        return error_response("DUPLICATE_NAME_OR_HOSTNAME",
                              f"Node with this name or hostname already exists: {', '.join(details)}")

    model_base_path = body.get("model_base_path") or _CONFIG.get("qr_env_config", {}).get("QUICKROBOT_API_MODEL_BASE_PATH")
    try:
        node = add_node(_CONFIG["db_path"], name=name, hostname=hostname,
                        transport=body.get("transport", "ansible"),
                        ansible_user=(body.get("ansible_user")
                          or _CONFIG.get("qr_env_config", {}).get("QUICKROBOT_API_ANSIBLE_SSHUSER")
                          or DEFAULT_ANSIBLE_USER),
                        ssh_port=body.get("ssh_port", body.get("ansible_port", QR_SSH_PORT_DEFAULT)),
                        ansible_key_path=(body.get("ansible_key_path")
                           or (_CONFIG.get("qr_env_config", {}).get("QUICKROBOT_API_ANSIBLE_SSHKEY") or None)),
                        model_base_path=model_base_path,
                        ipv4_address=body.get("ipv4_address"),
                        ipv6_address=body.get("ipv6_address"))
    except Exception as exc:
        log_qr_action(_CONFIG["db_path"], "node_create_failed", actor="api",
                            details={"name": name, "hostname": hostname, "reason": str(exc)})
        return error_response("VALIDATION_ERROR", str(exc))

    from lib.lib_ansible_runner import validate_node as _vn
    # Create tracked task for lifecycle management (status updates)
    from lib.lib_qr_actions import log_qr_task as _lqt, update_qr_task as _uqt
    task_id = _lqt(_CONFIG["db_path"], "node_create", node["id"], actor="api",
                   extra_details={"name": name, "hostname": hostname})
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
        # Mark task as failed before returning error
        try:
            from lib.lib_qr_actions import update_qr_task as _uqt
            _uqt(_CONFIG["db_path"], task_id, 'failed', duration_ms=0)
        except Exception as _e:
            logger.debug("task update failed (node_create): %s", _e)
        # Include structured diagnostic for programmatic clients
        diag = disc_result.get("diagnostic")
        if diag:
            diag["hostname"] = hostname
            detail = {"diagnostic": diag}
        else:
            detail = None
        return error_response("NODE_UNREACHABLE",
            f"Node '{name}' ({hostname}) — {disc_result.get('error', 'validation failed')}",
            detail=detail)

    # Extract stale QR service info from validate_node output
    sqrs = disc_result.get("stale_qr_services", {})
    qr_units = sqrs.get("qr_units", {})
    stale = {
        "service_files": list(qr_units.keys()) if qr_units else [],
        "env_files": [],
        "total": len(qr_units) if qr_units else 0,
        "has_stale": len(qr_units) > 0 if qr_units else False,
    }

    # Mark task as completed
    try:
        from lib.lib_qr_actions import update_qr_task as _uqt
        _uqt(_CONFIG["db_path"], task_id, 'completed', duration_ms=0)
    except Exception as _e:
        logger.debug("task update failed (node_create completion): %s", _e)

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

    # System localhost node is never removable (auto-provisioned instances depend on it)
    if node_id == 1:
        return error_response("SYSTEM_NODE", "Cannot delete system localhost node (ID 1)")

    hostname = (node.get("ansible_inventory_host") or
               node.get("hostname"))
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
            engine_type_name = inst.get("engine_type_name", QR_ENGINE_LLAMA_RPC_NAME)
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
                    check_r = _execute_playbook("check_undeploy", resolver_type="playbook_id",
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
                except Exception as _e:
                    logger.debug("undeploy check failed for inst %d: %s", inst_id, _e)
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

    # Guard: no duplicate name or hostname when updating (NO FALLBACK — reject hard)
    new_name = body.get("name", existing.get("name"))
    new_hostname = body.get("hostname", existing.get("hostname"))
    from db.adapters.nodes import check_duplicate_name_or_hostname as _cdn
    conflicts = _cdn(_CONFIG["db_path"], new_name, new_hostname, exclude_node_id=node_id)
    if conflicts:
        details = []
        for c in conflicts:
            existing_name = c.get('existing_name', '?')
            details.append(f"{c['column']}='{c['value']}' (used by node '{existing_name}', id={c['existing_node_id']})")
        return error_response("DUPLICATE_NAME_OR_HOSTNAME",
                              f"Another node already uses this name or hostname: {', '.join(details)}")

    try:
        node = _un(_CONFIG["db_path"], node_id, **body)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(node)


