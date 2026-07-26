"""Node APT operation endpoints for quickrobot.

Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
import logging
import os
import threading
from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG, _project_root
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST
from qr_api.lib_instances import _check_node_active, _execute_playbook
from db.sqlite import pool as db_pool
from lib.lib_constants import DEFAULT_ANSIBLE_USER

logger = logging.getLogger(__name__)

def api_node_apt(node_id):
    """Unified APT operation endpoint (EP-CONSOLIDATE P1).

    Dispatches to apt_update, apt_upgrade, or apt_update_upgrade based on action.
    Reads 'action' from POST body: "update", "upgrade", or "all".

    All actions run asynchronously — returns job_id immediately. Background
    threads execute playbooks; clients poll log_entries for progress.

    Args:
        node_id: Node primary key.
        action (from request body): "update", "upgrade", or "all" (update+upgrade combined).

    Returns:
        JSON with action, node_id, job_id.
    """
    action = request.json.get("action", "update") if request.is_json else "update"
    from lib.lib_runner import PlaybookRunner as _Runner

    # Check node is active (admin toggle)
    nd = _check_node_active(_CONFIG["db_path"], node_id)
    if isinstance(nd, tuple):
        return nd

    hostname = nd.get("hostname") or nd.get("name")

    def _finalize_job(job_id, job_type, success=True, error=None):
        """Update log_entries to completed/failed state."""
        try:
            now = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            with db_pool(_CONFIG["db_path"]) as conn:
                if success:
                    conn.execute("""UPDATE log_entries SET status='completed',
                                    finished_at=? WHERE parent_id=?""", (now, job_id))
                    conn.execute("""UPDATE log_entries SET status='completed',
                                    finished_at=? WHERE id=?""", (now, job_id))
                else:
                    conn.execute("UPDATE log_entries SET status='failed' WHERE id=?", (job_id,))
                    conn.execute("UPDATE log_entries SET status='failed', error_message=? WHERE parent_id=?", (error, job_id))
                conn.commit()
        except Exception as exc:
            logger.error("_finalize_job failed for job %d: %s", job_id, exc)

    def _run_stage(job_id, stage):
        """Background worker: execute one playbook stage."""
        from db.adapters.playbooks import resolve_playbook_by_id as _rpbi
        try:
            pb_id = stage["playbook"]
            pb_record = _rpbi(_CONFIG["db_path"], pb_id)
            playbook_path = None
            if pb_record and pb_record.get("file_path"):
                playbook_path = os.path.join(
                    _project_root, pb_record["file_path"]
                    if not pb_record["file_path"].startswith("/") else pb_record["file_path"]
                )
            if not playbook_path:
                playbook_path = os.path.join(_project_root, "playbooks", f"{pb_id}.yml")

            r = _execute_playbook(playbook_path, resolver_type="file_path",
                                 limit=hostname, node_id=node_id,
                                 extra_vars={"inventory_host": hostname},
                                 action_type=f"apt_{stage['stage']}")
            if r.get("error"):
                _finalize_job(job_id, f"apt_{stage['stage']}", success=False, error=r["error"])
            else:
                _finalize_job(job_id, f"apt_{stage['stage']}", success=True)
        except Exception as exc:
            logger.error("_run_stage failed for %s: %s", stage["stage"], exc)
            _finalize_job(job_id, f"apt_{stage.get('stage', '?')}", success=False, error=str(exc))

    def _run_single(job_id, action_type, playbook_name):
        """Background worker: execute one playbook."""
        try:
            r = _execute_playbook(playbook_name, resolver_type="playbook_id",
                                 limit=hostname, node_id=node_id,
                                 extra_vars={"inventory_host": hostname},
                                 action_type=action_type)
            if r.get("error"):
                _finalize_job(job_id, action_type, success=False, error=r["error"])
            else:
                _finalize_job(job_id, action_type, success=True)
        except Exception as exc:
            logger.error("_run_single failed: %s", exc)
            _finalize_job(job_id, action_type, success=False, error=str(exc))

    if action == "all":
        # Combined update + upgrade via RUNNER-1 staged chain (async)
        runner = _Runner(_CONFIG["db_path"])
        stages = runner._get_stage_chain(None, "apt_update_upgrade", None)
        if not stages:
            return error_response("PLAYBOOK_NOT_FOUND", "No stages found for apt_update_upgrade job type")

        with db_pool(_CONFIG["db_path"]) as conn:
            cursor = conn.execute(
                """INSERT INTO log_entries
                   (instance_id, job_type, engine_type_name, node_id, status, actor, created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                (None, "apt_update_upgrade", f"node_{node_id}", node_id, "api"),
            )
            job_id = cursor.lastrowid

            for stage in stages:
                conn.execute(
                    """INSERT INTO log_entries
                       (parent_id, task_stage, playbook_registry_id, status, created_at)
                       VALUES (?, ?, NULL, 'queued', strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                    (job_id, stage["stage"]),
                )
            conn.commit()

        threads = []
        for stage in stages:
            t = threading.Thread(target=_run_stage, args=(job_id, stage), daemon=True)
            t.start()
            threads.append(t)

        return success_single({"action": "apt_update_upgrade", "node_id": node_id,
                               "job_id": job_id})

    else:
        # Single operation: update or upgrade (async)
        job_type = "apt_update" if action == "update" else "apt_upgrade"
        playbook_name = "apt_update" if action == "update" else "apt_upgrade"

        with db_pool(_CONFIG["db_path"]) as conn:
            cursor = conn.execute(
                """INSERT INTO log_entries
                   (instance_id, job_type, engine_type_name, node_id, status, actor, created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                (None, job_type, f"node_{node_id}", node_id, "api"),
            )
            job_id = cursor.lastrowid

            conn.execute(
                """INSERT INTO log_entries
                   (parent_id, task_stage, playbook_registry_id, status, created_at)
                   VALUES (?, ?, NULL, 'queued', strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                (job_id, f"apt_{action}"),
            )
            conn.commit()

        t = threading.Thread(target=_run_single, args=(job_id, f"apt_{action}", playbook_name), daemon=True)
        t.start()

        return success_single({"action": f"apt_{action}", "node_id": node_id,
                               "job_id": job_id})


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

