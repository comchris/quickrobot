# Copyright 2026 comchris quickrobot .de project

"""quickrobot — Script API routes (SCRIPT-1).

Endpoints for creating and managing dynamic script jobs:
    POST /api/v1/scripts          — Create and queue a script
    GET  /api/v1/scripts          — List scripts
    GET  /api/v1/scripts/<id>     — Get script detail with steps
    POST /api/v1/scripts/<id>/run — Trigger execution
    DELETE /api/v1/scripts/<id>   — Cancel script
"""

import json
import logging

from flask import (
    Blueprint, jsonify, request,
)
from qr_api import _CONFIG

logger = logging.getLogger(__name__)
bp = Blueprint("scripts", __name__, url_prefix="/api/v1/scripts")


@bp.route("", methods=["GET"])
def list_scripts():
    """List all scripts with optional filters."""
    from db.sqlite import pool

    status = request.args.get("status")
    instance_id = request.args.get("instance_id", type=int)

    with pool(_CONFIG["db_path"]) as conn:
        query = "SELECT * FROM scripts WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if instance_id:
            query += """ AND id IN (
                SELECT s.id FROM scripts s
                JOIN jobs j ON s.parent_job_id = j.id
                WHERE j.instance_id = ?
            )"""
            params.append(instance_id)
        query += " ORDER BY created_at DESC LIMIT 50"

        rows = conn.execute(query, params).fetchall()
        items = [dict(r) for r in rows]

    return jsonify({"status": "ok", "total": len(items), "items": items})


@bp.route("/<int:script_id>", methods=["GET"])
def get_script(script_id):
    """Get script detail including all steps."""
    from db.sqlite import pool

    with pool(_CONFIG["db_path"]) as conn:
        script = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
        if not script:
            return jsonify({"status": "error", "code": "NOT_FOUND"}), 404

        steps = conn.execute(
            "SELECT * FROM script_steps WHERE script_id=? ORDER BY step_index",
            (script_id,),
        ).fetchall()

        data = dict(script)
        data["steps"] = [dict(s) for s in steps]
        return jsonify({"status": "ok", "data": data})


@bp.route("/<int:script_id>", methods=["DELETE"])
def delete_script(script_id):
    """Cancel a script (sets status=cancelled)."""
    from db.sqlite import pool

    with pool(_CONFIG["db_path"]) as conn:
        conn.execute(
            "UPDATE scripts SET status='cancelled', finished_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=? AND status IN ('queued', 'running')",
            (script_id,),
        )

        if conn.rowcount == 0:
            return jsonify({"status": "error", "code": "NOT_FOUND"}), 404

        conn.execute(
            "UPDATE script_steps SET status='cancelled' WHERE script_id=? AND status IN ('queued', 'running')",
            (script_id,),
        )
        conn.commit()

    return jsonify({"status": "ok", "data": {"id": script_id, "status": "cancelled"}})


@bp.route("/<int:script_id>/run", methods=["POST"])
def run_script(script_id):
    """Trigger execution of a queued script."""
    from db.sqlite import pool

    with pool(_CONFIG["db_path"]) as conn:
        script = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
        if not script:
            return jsonify({"status": "error", "code": "NOT_FOUND"}), 404

        if script["status"] != "queued":
            return jsonify({
                "status": "error",
                "code": "SCRIPT_NOT_QUEUED",
                "message": f"Script is {script['status']}, not queued"
            })

        conn.execute(
            "UPDATE scripts SET status='running', started_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?", (script_id,)
        )
        conn.commit()

    # Spawn runner in background thread
    from threading import Thread
    from lib.lib_script_runner import ScriptRunner

    def _run():
        sr = ScriptRunner(_CONFIG["db_path"])
        sr.execute_script(script_id)

    Thread(target=_run, daemon=True).start()

    return jsonify({
        "status": "ok",
        "code": "SCRIPT_RUNNING",
        "data": {"id": script_id, "status": "running"}
    })


@bp.route("", methods=["POST"])
def create_script():
    """Create a new script and queue it for execution.

    Request body:
        {
            "name": "Optional name",
            "actor": "api" or "agent",  // optional, default "api"
            "steps": [
                {
                    "type": "create_instance|deploy_instance|benchmark_run",
                    "params": {...},
                    "depends_on": [0],         // step indices this depends on
                    "if_condition": "step[0].success",
                    "label": "Optional label"
                }
            ]
        }

    Returns:
        Script record with steps, status=queued.
    """
    from db.sqlite import pool

    data = request.get_json()
    if not data or "steps" not in data:
        return jsonify({"status": "error", "code": "BAD_REQUEST", "message": "Missing 'steps' array"}), 400

    steps = data.get("steps", [])
    actor = data.get("actor", "api")
    name = data.get("name", "")

    if not isinstance(steps, list):
        return jsonify({"status": "error", "code": "BAD_REQUEST", "message": "'steps' must be an array"}), 400

    with pool(_CONFIG["db_path"]) as conn:
        # Create the script record
        cursor = conn.execute(
            """INSERT INTO scripts (actor, name, total_steps)
               VALUES (?, ?, ?)""",
            (actor, name, len(steps)),
        )
        script_id = cursor.lastrowid

        # Create each step
        for idx, step in enumerate(steps):
            params_json = json.dumps(step.get("params", {})) if isinstance(step.get("params"), dict) else step.get("params", "{}")
            depends_on_json = json.dumps(step.get("depends_on", []))
            if_condition = step.get("if_condition", "")

            conn.execute(
                """INSERT INTO script_steps
                   (script_id, step_index, type, params, depends_on, if_condition)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (script_id, idx, step["type"], params_json, depends_on_json, if_condition),
            )

        conn.commit()

    return jsonify({
        "status": "ok",
        "code": "SCRIPT_QUEUED",
        "data": {"id": script_id, "status": "queued", "steps_count": len(steps)}
    })
