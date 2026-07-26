"""Job and task query endpoints for quickrobot.

Handles listing, getting, deleting, and cancelling jobs/tasks.
Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
from flask import request
from qr_api.lib_responses import success_single, success_list, error_response
from qr_api import _CONFIG
from lib.lib_runner import PlaybookRunner
from db.sqlite import pool as db_pool

# Stale job statuses — non-running states eligible for cleanup
STALE_JOB_STATUSES = ("completed", "failed", "error", "queued")


def api_list_jobs(inst_id=None):
    """List jobs with optional filters.

    GET /api/v1/jobs?status=running&engine_type=llama_server&node_id=5
    GET /api/v1/instances/<id>/jobs  (scoped alias — filters by instance_id)
    """
    # Accept both "status" (API standard) and "job_status" (WebUI form param)
    status = request.args.get("status") or request.args.get("job_status")
    engine_type = request.args.get("engine_type")
    node_id = request.args.get("node_id")

    runner = PlaybookRunner(_CONFIG["db_path"])
    jobs = runner.list_jobs(status=status, engine_type=engine_type, node_id=node_id)

    # Filter by instance_id if scoped route was used
    if inst_id is not None:
        jobs = [j for j in jobs if j.get("instance_id") == inst_id]

    return success_list(jobs)


def api_get_job(job_id):
    """Get job details with task IDs.

    GET /api/v1/jobs/<id>
    Returns: { "job": {...}, "tasks": [task_id_1, task_id_2, ...] }
    """
    runner = PlaybookRunner(_CONFIG["db_path"])
    data = runner.get_job_with_task_ids(job_id)
    if not data:
        return error_response("RESOURCE_NOT_FOUND", f"Job {job_id} not found")
    return success_single(data)


def api_delete_job(job_id):
    """Delete a single job and all its tasks + playbook runs.

    DELETE /api/v1/jobs/<job_id>

    Returns deleted count (jobs + tasks + playbook_runs).
    """
    with db_pool(_CONFIG["db_path"]) as conn:
        # Count tasks and playbook_runs for this job
        task_count = conn.execute(
            "SELECT COUNT(*) FROM log_entries WHERE parent_id=?", (job_id,),
        ).fetchone()[0]
        pr_count = conn.execute(
            "SELECT COUNT(*) FROM playbook_runs pr "
            "JOIN log_entries le ON le.id=pr.task_id WHERE le.parent_id=?",
            (job_id,),
        ).fetchone()[0]
        # Delete in order: playbook_runs -> tasks -> jobs
        conn.execute(
            "DELETE FROM playbook_runs WHERE task_id IN "
            "(SELECT id FROM log_entries WHERE parent_id=?)", (job_id,),
        )
        conn.execute("DELETE FROM log_entries WHERE parent_id=?", (job_id,))
        conn.execute("DELETE FROM log_entries WHERE id=? AND parent_id IS NULL", (job_id,))
        conn.commit()
    return {"status": "ok", "deleted_jobs": 1, "deleted_tasks": task_count,
            "deleted_playbook_runs": pr_count}


def api_delete_stale_jobs():
    """Delete stale jobs by status filter.

    POST /api/v1/jobs/cleanup?older_than_minutes=30&status=completed&job_type=deploy&instance_id=N

    - older_than_minutes: default 30, delete jobs older than this
    - status: optional, filter by job status (running, completed, failed, error, queued)
              If not provided, defaults to 'completed,failed,error,queued' (all non-running states)
    - job_type: optional, filter to specific type (deploy|reconfigure|rebuild)
    - instance_id: optional, filter to specific instance

    Returns deleted count.
    """
    inst_id = request.args.get("instance_id")
    job_type = request.args.get("job_type")
    older_min = int(request.args.get("older_than_minutes", "30"))
    status_filter = request.args.get("status")  # single status or empty for all non-running

    if status_filter:
        query = "SELECT id FROM log_entries WHERE parent_id IS NULL AND status=?"
        params = [status_filter]
    else:
        # Default: delete stale non-running jobs (completed/failed/error/queued)
        # Queued included so users can clean up stuck jobs with no active scheduler
        status_placeholders = ",".join("?" for _ in STALE_JOB_STATUSES)
        query = f"SELECT id FROM log_entries WHERE parent_id IS NULL " \
                f"AND status IN ({status_placeholders})"
        params = list(STALE_JOB_STATUSES)

    if inst_id is not None:
        query += " AND instance_id=?"
        params.append(int(inst_id))
    if job_type:
        query += " AND job_type=?"
        params.append(job_type)
    query += " AND replace(created_at,'T',' ') < datetime('now', ?)"
    params.append(f"-{older_min} minutes")

    with db_pool(_CONFIG["db_path"]) as conn:
        jobs = conn.execute(query, params).fetchall()
        deleted = 0
        for jid_row in jobs:
            jid = jid_row[0]
            conn.execute(
                "DELETE FROM log_entries WHERE parent_id=? AND parent_id IS NOT NULL", (jid,),
            )
            conn.execute("DELETE FROM log_entries WHERE parent_id=?", (jid,))
            conn.execute("DELETE FROM log_entries WHERE id=? AND parent_id IS NULL", (jid,))
            deleted += 1
        conn.commit()

    return {"status": "ok", "deleted_jobs": deleted}


def api_list_tasks():
    """List tasks with optional filters.

    GET /api/v1/tasks?status=running&job_id=5&instance_id=103
    """
    status = request.args.get("status")   # queued|running|completed|failed
    job_id = request.args.get("job_id")
    instance_id = request.args.get("instance_id")

    runner = PlaybookRunner(_CONFIG["db_path"])
    tasks = runner.list_tasks(status=status, job_id=job_id, instance_id=instance_id)
    return success_list(tasks)


def api_get_task(task_id):
    """Get full task detail including playbook output.

    GET /api/v1/tasks/<id>
    Returns: { "task": {...}, "playbook_output": {...} }
    """
    runner = PlaybookRunner(_CONFIG["db_path"])
    data = runner.get_task_detail(task_id)
    if not data:
        return error_response("RESOURCE_NOT_FOUND", f"Task {task_id} not found")
    return success_single(data)


def api_cancel_task(task_id):
    """Cancel a running or queued task.

    POST /api/v1/tasks/<id>/cancel
    Body: {} (no body required)

    Behavior:
    - Running tasks: reset to 'queued' so the scheduler can re-pick them.
      The ansible subprocess may still be running on the remote node;
      it will complete and then report its result on next scheduler cycle.
    - Queued tasks: remain 'queued' (no-op, just confirm).
    - Completed/failed tasks: return 409 CONFLICT.

    Returns: { "status": "ok", "data": { "task_id": N, "previous_status": "...", "message": "..." } }
    """
    with db_pool(_CONFIG["db_path"]) as conn:
        task = conn.execute(
            "SELECT id, status, parent_id AS job_id, instance_id "
            "FROM log_entries WHERE id=? AND parent_id IS NOT NULL", (task_id,),
        ).fetchone()

    if not task:
        return error_response("RESOURCE_NOT_FOUND", f"Task {task_id} not found")

    prev_status = task["status"]

    if prev_status == "completed":
        return error_response("CONFLICT",
            f"Task {task_id} already {prev_status} — use DELETE to remove")
    if prev_status == "failed":
        return error_response("CONFLICT",
            f"Task {task_id} already {prev_status} — use DELETE to remove")

    # Reset running/queued/stuck tasks back to queued for scheduler re-pickup
    with db_pool(_CONFIG["db_path"]) as conn:
        conn.execute(
            "UPDATE log_entries SET status='queued', started_at=NULL, finished_at=NULL, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (task_id,),
        )
        conn.commit()

    return success_single({
        "task_id": task_id,
        "previous_status": prev_status,
        "message": f"Task {task_id} reset to queued (was {prev_status})",
    })


def api_delete_task(task_id):
    """Delete a completed or failed task and its playbook run data.

    POST /api/v1/tasks/<id>/delete
    Body: {} (no body required)

    Behavior:
    - Deletes the task record and associated playbook_runs entries.
    - Only completed or failed tasks can be deleted.
    - Running/queued/stuck tasks must be cancelled first.

    Returns: { "status": "ok", "data": { "task_id": N, "deleted": true } }
    """
    with db_pool(_CONFIG["db_path"]) as conn:
        task = conn.execute(
            "SELECT id, status FROM log_entries WHERE id=? AND parent_id IS NOT NULL", (task_id,),
        ).fetchone()

    if not task:
        return error_response("RESOURCE_NOT_FOUND", f"Task {task_id} not found")

    if task["status"] not in ("completed", "failed"):
        return error_response("CONFLICT",
            f"Task {task_id} is '{task['status']}' — cancel first or wait for completion")

    with db_pool(_CONFIG["db_path"]) as conn:
        # Delete playbook_runs entries first (FK order)
        conn.execute("DELETE FROM playbook_runs WHERE task_id=?", (task_id,))
        # Delete the task
        conn.execute(
            "DELETE FROM log_entries WHERE id=? AND parent_id IS NOT NULL", (task_id,),
        )
        conn.commit()

    return success_single({
        "task_id": task_id,
        "deleted": True,
    })
