from flask import request, jsonify
import json
import logging as _logging
import os

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG, _project_root
from lib.lib_constants import DEFAULT_ANSIBLE_USER
from lib.qr_engine_ids import QR_SSH_PORT_DEFAULT
from qr_api.lib_instances import _check_node_active, _execute_playbook
from db.sqlite import pool as db_pool

logger = _logging.getLogger(__name__)


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
    """Graceful node shutdown via Ansible (async fire-and-forget)."""

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = nd["hostname"]

    # _execute_playbook handles all logging (starting/success/error) — single logging point
    r = _execute_playbook("shutdown_node", resolver_type="playbook_id",
                         limit=hostname, node_id=node_id,
                         extra_vars={"inventory_host": hostname},
                         action_type="shutdown_node")
    if r["error"]:
        return error_response("DEPLOYMENT_FAILED", r["error"])
    return success_single({"action": "shutdown", "node_id": node_id,
                            "result": r.get("result")})


def api_node_reboot(node_id):
    """Reboot node via RUNNER-1 async staged chain.

    Uses async/poll:0 in playbook — returns within ~1-10s instead of blocking
    for the full reboot window (up to 300s). Job tracking via jobs+tasks tables.
    """
    from lib.lib_ansible_runner import log_ansible_action as _la
    from lib.lib_runner import PlaybookRunner

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = nd["hostname"]
    user = nd.get("ansible_user") or DEFAULT_ANSIBLE_USER
    port = nd.get("ssh_port", nd.get("ansible_port", QR_SSH_PORT_DEFAULT))

    # Use RUNNER-1 for job/task tracking with async playbook
    try:
        runner = PlaybookRunner(_CONFIG["db_path"])
        stages = runner._get_stage_chain(None, "reboot", None)
        if not stages:
            raise ValueError("No stages found for reboot job type")

        with db_pool(_CONFIG["db_path"]) as conn:
            # Create parent job (node_id stored in engine_type_name column)
            cursor = conn.execute(
                """INSERT INTO log_entries
                   (instance_id, job_type, engine_type_name, status, actor, created_at)
                   VALUES (?, ?, ?, 'queued', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                (None, "reboot", f"node_{node_id}", "api"),
            )
            job_id = cursor.lastrowid

            # Resolve playbook to get registry ID for task record
            from db.adapters.playbooks import resolve_playbook_by_id as _rpbi
            pb_record = _rpbi(_CONFIG["db_path"], "reboot_node")
            pb_registry_id = pb_record.get("id") if pb_record else None

            # Create task for the single reboot stage
            conn.execute(
                """INSERT INTO log_entries
                   (parent_id, task_stage, playbook_registry_id, status, created_at)
                   VALUES (?, 'reboot', ?, 'queued', strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                (job_id, pb_registry_id),
            )
            conn.commit()

        # Execute the async playbook — ansible returns immediately (poll:0)

        playbook_path = None
        if pb_record and pb_record.get("file_path"):
            playbook_path = os.path.join(
                _project_root, pb_record["file_path"]
                if not pb_record["file_path"].startswith("/") else pb_record["file_path"]
            )
        if not playbook_path:
            playbook_path = os.path.join(_project_root, "playbooks", "reboot_node.yml")

        # Run playbook — get raw ansible result with hosts_matched
        from lib.lib_ansible_runner import run_playbook as _rp, log_ansible_action as _la
        raw_result = _rp(playbook_path, limit=hostname, extra_vars={
            "inventory_host": hostname, "node_user": user, "ansible_port": port
        }, timeout=600)

        # Check: did ansible reach the target AND did any task fail?
        from lib.lib_ansible_runner import _detect_hosts_match as _dhm
        hosts_reached = _dhm(raw_result) if isinstance(raw_result, dict) else True
        task_failed = raw_result.get("failed", False) if isinstance(raw_result, dict) else False

        if not hosts_reached:
            with db_pool(_CONFIG["db_path"]) as conn2:
                conn2.execute("UPDATE log_entries SET status='failed' WHERE id=?", (job_id,))
                conn2.commit()
            return error_response("DEPLOYMENT_FAILED", "No hosts matched the target")

        # Persist ansible output to DB for audit trail
        _la(_CONFIG["db_path"], "reboot_node", node_id, None, playbook_path,
            {"inventory_host": hostname, "node_user": user}, raw_result, parent_id=job_id)

        # Mark task completed (or failed if ansible reported failures)
        final_status = "failed" if task_failed else "completed"
        with db_pool(_CONFIG["db_path"]) as conn2:
            conn2.execute("UPDATE log_entries SET status=?, finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE parent_id=? AND task_stage='reboot'", (final_status, job_id))
            # Only mark parent completed if task succeeded
            if not task_failed:
                conn2.execute("UPDATE log_entries SET status='completed', finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (job_id,))
            else:
                conn2.execute("UPDATE log_entries SET status='failed' WHERE id=?", (job_id,))
            conn2.commit()

        if task_failed:
            # For async reboot the shutdown task may report changed=True but ansible
            # returns non-zero because it can't confirm completion — this is expected.
            return success_single({"action": "reboot", "node_id": node_id,
                                   "job_id": job_id,
                                   "message": "Reboot initiated (async) — ansible returned non-zero (expected for fire-and-forget)"})

        return success_single({"action": "reboot", "node_id": node_id,
                               "job_id": job_id,
                               "message": "Reboot initiated (async)"})
    except Exception as exc:
        return error_response("DEPLOYMENT_FAILED", f"Reboot failed: {exc}")


def api_node_ping(node_id):
    """One-shot ping reachability check for a node (used by WebUI ping dots)."""
    import subprocess as _subp

    from db.adapters.nodes import get_node as _gn, update_ping_state as _ups

    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    hostname = node.get("ansible_inventory_host") or node.get("hostname")
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
    except Exception as _e:
        logger.debug("ping failed for %s: %s", hostname, _e)
        ps = "offline"

    # Update DB
    try:
        _ups(_CONFIG["db_path"], node_id, ps)
    except Exception as _e:
        logger.debug("ping state DB update failed (node_id=%d): %s", node_id, _e)

    return success_single({"ping_state": ps})


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
                warnings.append("ggml-rpc-server missing")

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


