#!/usr/bin/env python3
"""Scheduler stale detection — orphaned jobs and stuck transitions.

Six-phase detection runs once at startup and periodically:
  Phase 1: Orphaned parent jobs (no children, >30s old) → mark error
  Phase 1b: Stuck running health_check jobs only (parent+children both running >60s)
             Deploy/rebuild jobs handled by Phase 1d instead
  Phase 1c: Queued health_check parents >120s old (DB lock contention accumulation)
  Phase 1d: Stuck deploy/rebuild compile jobs (ansible subprocess died during compile)
  Phase 1e: Generic stuck running jobs (start, reconfigure, etc.) with timeout >60s
             Fills gap left by Phase 1b's health_check-only filter
  Phase 2: Transitioning instances stuck for >2h → mark error
  Phase 3: Parent job stale (all children done but parent still running, >QR_TIMEOUT_DEFAULT)

Design notes:
- Explicit connection scope per operation — no nested with pool() blocks
- No silent except Exception: pass in critical paths
- Simple queries, no per-task ps aux grep (fragile, unnecessary)
"""

import datetime as _dt_module
import logging
import time as _time_module

from lib.qr_engine_ids import (
    QR_JOB_HEALTH_CHECK,        # health_check job type constant
    QR_TIMEOUT_DEFAULT,         # 300s = 5min default timeout
    QR_TIMEOUT_HEALTH_CHECK,    # 60s (used as stale threshold for health checks)
    QR_TIMEOUT_JOB,             # 7200s = 2h (stuck transition timeout)
    QR_TIMEOUT_START,           # 60s (stale threshold for start/restart/simple jobs)
)

logger = logging.getLogger("quickrobot.scheduler")


# Stale detection thresholds (use SSOT constants where available)
_ORPHAN_AGE_SEC = 30           # Orphaned parent jobs (>30s, no children)
_STUCK_TRANSITION_SEC = QR_TIMEOUT_JOB  # 2 hours
_PARENT_STALE_SEC = QR_TIMEOUT_DEFAULT  # 5min — all children done but parent still running

# Transitioning states that indicate a potentially stuck deployment
_TRANSITIONING_STATES = (
    "configuring",
    "deploying",
    "compiling",
    "updating",
)


def _process_is_alive(pid):
    """Check if a process with the given PID is currently running.

    Uses /proc filesystem on Linux for fast, zero-overhead check.

    Args:
        pid: Process ID to check.

    Returns:
        True if process exists and is accessible, False otherwise.
    """
    try:
        import os
        return os.path.exists(f"/proc/{pid}") and os.access(f"/proc/{pid}", os.R_OK)
    except (OSError, ValueError):
        return False


def detect_stale_tasks(db_path):
    """Detect and reset stale tasks and parent jobs.

    Returns:
        int — total number of entries reset.
    """
    reset_count = 0

    # Phase 1: Orphaned parent jobs (no children, running/received)
    reset_count += _find_orphaned_jobs(db_path)

    # Phase 1b: Stuck running jobs (parent+children both in 'running' too long)
    reset_count += _find_stuck_running_jobs(db_path)

    # Phase 1c: Stale queued health check parents (accumulated due to DB lock contention)
    reset_count += _find_stale_queued_health_checks(db_path)

    # Phase 1d: Stuck deploy/rebuild compile jobs (ansible subprocess died during compile)
    reset_count += _find_stuck_compile_jobs(db_path)

    # Phase 1e: Generic stuck running jobs (start, reconfigure, etc.) not covered by 1b/1d
    reset_count += _find_stuck_running_generic_jobs(db_path)

    # Phase 2: Stuck transitioning instances
    reset_count += _find_stuck_transitions(db_path)

    # Phase 3: Parent job stale — all children done but parent still running
    reset_count += _find_parent_jobs_stale(db_path)

    return reset_count


def _find_orphaned_jobs(db_path):
    """Find parent jobs with no child tasks that have been running too long.

    A parent job is considered orphaned when it has status='running' or
    'received', no child task rows exist, and it has been sitting for
    more than 30 seconds. This typically happens when the scheduler
    crashed after creating a job header but before persisting child tasks.

    Returns:
        int — number of orphaned jobs marked as error.
    """
    reset_count = 0
    now_epoch = _time_module.time()

    # Query: parent jobs with status=running/received, no children
    from db.sqlite import pool
    try:
        with pool(db_path) as conn:


            rows = conn.execute("""
                SELECT le.id AS job_id,
                       le.instance_id,
                       le.job_type,
                       le.created_at
                  FROM log_entries le
                 WHERE le.parent_id IS NULL
                   AND le.status IN ('running', 'received')
                   AND le.finished_at IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM log_entries le2
                       WHERE le2.parent_id = le.id
                   )
            """).fetchall()

            for row in rows:
                job_id = row["job_id"]
                instance_id = row["instance_id"]
                job_type = row["job_type"] or "unknown"
                created_at = row["created_at"]

                # Parse created_at and check age
                try:
                    ts_clean = created_at.rstrip("Z") if isinstance(created_at, str) and created_at.endswith("Z") else created_at
                    dt = _dt_module.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=_dt_module.timezone.utc
                    )
                    age = now_epoch - dt.timestamp()
                except (ValueError, TypeError):
                    logger.warning(
                        "[qr-scheduler] STALE: Job %d has unparseable created_at '%s' — marking error",
                        job_id, created_at,
                    )
                    age = 0  # Force mark as error

                if age > _ORPHAN_AGE_SEC:
                    conn.execute(
                        "UPDATE log_entries SET status='error', "
                        "error_message='Orphaned parent row: no child tasks' WHERE id=?",
                        (job_id,),
                    )
                    reset_count += 1
                    logger.warning(
                        "[qr-scheduler] STALE-ORPHAN: Job %d (inst=%d, type=%s) orphaned (%.0fs old) → error",
                        job_id, instance_id, str(job_type or ""), age,
                    )

    except Exception as exc:
        logger.error("[qr-scheduler] Stale orphan detection failed: %s", exc, exc_info=True)

    return reset_count


def _find_stuck_running_jobs(db_path):
    """Find parent health_check jobs where both the parent AND child tasks are stuck in 'running'.

    Health checks should complete in ~5-10s via SSH/systemctl probe. When the scheduler
    crashes or hits DB lock contention during finalization, both parent and children
    remain in 'running' status. This phase detects that pattern with a 60s threshold
    and marks them as error.

    Deploy/rebuild jobs are NOT handled here — their compile stages legitimately take
    up to 60min (QR_TIMEOUT_COMPILE). They rely on Phase 2's 2h timeout instead.

    Returns:
        int — number of stuck running health_check jobs marked as error.
    """
    reset_count = 0
    now_epoch = _time_module.time()

    # Query: parent health_check jobs in 'running' that have at least one child also in 'running'
    from db.sqlite import pool
    try:
        with pool(db_path) as conn:


            rows = conn.execute("""
                SELECT le.id AS job_id,
                       le.instance_id,
                       le.job_type,
                       le.created_at
                  FROM log_entries le
                 WHERE le.parent_id IS NULL
                   AND le.status = 'running'
                   AND le.finished_at IS NULL
                   AND le.job_type = ?
                   AND EXISTS (
                       SELECT 1 FROM log_entries le2
                       WHERE le2.parent_id = le.id
                         AND le2.status = 'running'
                   )
            """, (QR_JOB_HEALTH_CHECK,)).fetchall()

            for row in rows:
                job_id = row["job_id"]
                instance_id = row["instance_id"]
                job_type = row["job_type"] or "unknown"
                created_at = row["created_at"]

                # Parse created_at and check age
                try:
                    ts_clean = created_at.rstrip("Z") if isinstance(created_at, str) and created_at.endswith("Z") else created_at
                    dt = _dt_module.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=_dt_module.timezone.utc
                    )
                    age = now_epoch - dt.timestamp()
                except (ValueError, TypeError):
                    logger.warning(
                        "[qr-scheduler] STALE: Job %d has unparseable created_at '%s' — marking error",
                        job_id, created_at,
                    )
                    age = 0  # Force mark as error

                if age > QR_TIMEOUT_HEALTH_CHECK:
                    conn.execute(
                        "UPDATE log_entries SET status='error', "
                        "error_message='Stuck running health_check (parent+children) — stale detection' "
                        "WHERE parent_id=?", (job_id,),
                    )
                    conn.execute(
                        "UPDATE log_entries SET status='error', "
                        "error_message='Stuck running health_check (parent+children) — stale detection' "
                        "WHERE id=? AND parent_id IS NULL", (job_id,),
                    )
                    reset_count += 1
                    logger.warning(
                        "[qr-scheduler] STALE-RUNNING: Job %d (inst=%d, type=%s) stuck running (%.0fs) — marking error",
                        job_id, instance_id, str(job_type or ""), age,
                    )

    except Exception as exc:
        logger.error("[qr-scheduler] Stuck running detection failed: %s", exc, exc_info=True)

    return reset_count


def _find_stuck_running_generic_jobs(db_path):
    """Find parent jobs of non-health_check, non-deploy/rebuild types stuck in 'running'.

    Phase 1b covers health_check only. Phase 1d covers deploy/rebuild with compile stage.
    Other job types (start, reconfigure, rebuild, undeploy, bind, etc.) have their own
    dedicated phases but none catch cases where ALL stages are short-lived and the whole
    chain hangs.

    This phase catches jobs where:
      - job_type is NOT 'health_check', 'deploy', or 'rebuild'
      - parent AND all children are in 'running' status
      - parent age exceeds QR_TIMEOUT_START (60s) — short-lived jobs should complete fast

    Returns:
        int — number of generic stuck running jobs marked as error.
    """
    reset_count = 0
    now_epoch = _time_module.time()

    from db.sqlite import pool
    try:
        with pool(db_path) as conn:
            rows = conn.execute("""
                SELECT le.id AS job_id,
                       le.instance_id,
                       le.job_type,
                       le.created_at
                  FROM log_entries le
                 WHERE le.parent_id IS NULL
                   AND le.status = 'running'
                   AND le.finished_at IS NULL
                   AND le.job_type NOT IN ('health_check', 'deploy', 'rebuild')
                   AND EXISTS (
                       SELECT 1 FROM log_entries le2
                       WHERE le2.parent_id = le.id
                         AND le2.status = 'running'
                    )
            """).fetchall()

            for row in rows:
                job_id = row["job_id"]
                instance_id = row["instance_id"]
                job_type = row["job_type"] or "unknown"
                created_at = row["created_at"]

                try:
                    ts_clean = created_at.rstrip("Z") if isinstance(created_at, str) and created_at.endswith("Z") else created_at
                    dt = _dt_module.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=_dt_module.timezone.utc
                    )
                    age = now_epoch - dt.timestamp()
                except (ValueError, TypeError):
                    logger.warning(
                        "[qr-scheduler] STALE: Job %d has unparseable created_at '%s' — marking error",
                        job_id, created_at,
                    )
                    age = 0

                if age > QR_TIMEOUT_START:
                    conn.execute(
                        "UPDATE log_entries SET status='error', "
                        "error_message='Stuck running generic job (parent+children) — stale detection' "
                        "WHERE parent_id=?", (job_id,),
                    )
                    conn.execute(
                        "UPDATE log_entries SET status='error', "
                        "error_message='Stuck running generic job (parent+children) — stale detection' "
                        "WHERE id=? AND parent_id IS NULL", (job_id,),
                    )
                    # Reset instance state to allow re-operation
                    conn.execute(
                        "UPDATE instances SET state='stopped', "
                        "last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                        "WHERE id=?", (instance_id,),
                    )
                    reset_count += 1
                    logger.warning(
                        "[qr-scheduler] STALE-GENERIC: Job %d (inst=%d, type=%s) stuck running (%.0fs) — marking error",
                        job_id, instance_id, str(job_type or ""), age,
                    )

    except Exception as exc:
        logger.error("[qr-scheduler] Stuck generic running detection failed: %s", exc, exc_info=True)

    return reset_count


def _find_stuck_compile_jobs(db_path):
    """Detect deploy/rebuild jobs where the compile stage has been running too long.

    Compile stages can take up to 60min (QR_TIMEOUT_COMPILE). When the ansible
    subprocess dies or gets orphaned during compilation, the task stays in 'running'
    status and the parent job never finalizes. Phase 2's stuck-transition detection
    only works while the instance is in a transitioning state — once the instance
    state changes (e.g., to 'stopped'), Phase 2 can no longer find it.

    This phase catches deploy/rebuild jobs with running compile tasks older than
    QR_TIMEOUT_COMPILE (3600s = 60min), marking both the parent job and child
    tasks as 'error'.

    Returns:
        int — number of stuck compile jobs marked as error.
    """
    from lib.qr_engine_ids import QR_TIMEOUT_COMPILE
    reset_count = 0
    now_epoch = _time_module.time()

    from db.sqlite import pool
    try:
        with pool(db_path) as conn:
            rows = conn.execute("""
                SELECT le.id AS job_id,
                       le.instance_id,
                       le.job_type,
                       le.created_at
                  FROM log_entries le
                 WHERE le.parent_id IS NULL
                   AND le.status = 'running'
                   AND le.finished_at IS NULL
                   AND le.job_type IN ('deploy', 'rebuild')
                   AND EXISTS (
                       SELECT 1 FROM log_entries le2
                       WHERE le2.parent_id = le.id
                         AND le2.status IN ('queued', 'running')
                         AND le2.task_stage = 'compile'
                   )
            """).fetchall()

            for row in rows:
                job_id = row["job_id"]
                instance_id = row["instance_id"]
                job_type = row["job_type"] or "unknown"
                created_at = row["created_at"]

                try:
                    ts_clean = created_at.rstrip("Z") if isinstance(created_at, str) and created_at.endswith("Z") else created_at
                    dt = _dt_module.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=_dt_module.timezone.utc
                    )
                    age = now_epoch - dt.timestamp()
                except (ValueError, TypeError):
                    logger.warning(
                        "[qr-scheduler] STALE: Job %d has unparseable created_at '%s' — marking error",
                        job_id, created_at,
                    )
                    age = 0

                if age > QR_TIMEOUT_COMPILE:
                    # Mark all child tasks as error
                    conn.execute(
                        "UPDATE log_entries SET status='error', "
                        "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                        "error_message='Stuck compile stage (parent+compile task) — stale detection' "
                        "WHERE parent_id=? AND status IN ('queued','running')", (job_id,),
                    )
                    # Mark parent job as error
                    conn.execute(
                        "UPDATE log_entries SET status='error', "
                        "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                        "error_message='Stuck compile stage (parent+compile task) — stale detection' "
                        "WHERE id=? AND parent_id IS NULL", (job_id,),
                    )
                    # Reset instance state to allow restart
                    conn.execute(
                        "UPDATE instances SET state='stopped', "
                        "last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                        "WHERE id=?", (instance_id,),
                    )
                    reset_count += 1
                    logger.warning(
                        "[qr-scheduler] STALE-COMPILE: Job %d (inst=%d, type=%s) stuck compile (%.0fs) — marking error",
                        job_id, instance_id, str(job_type or ""), age,
                    )

    except Exception as exc:
        logger.error("[qr-scheduler] Stuck compile detection failed: %s", exc, exc_info=True)

    return reset_count


def _find_stale_queued_health_checks(db_path):
    """Cancel old queued health check parent jobs that never got picked up.

    When `get_next_queued_task()` fails with "database is locked", child tasks
    stay in 'queued' status and the scheduler can't claim them. New health
    cycles keep creating fresh parents on top, causing accumulation.

    This phase cancels queued parent jobs older than a threshold (120s) so the
    scheduler can pick up fresh ones on the next cycle.

    Returns:
        int — number of stale queued parents cancelled.
    """
    reset_count = 0
    now_epoch = _time_module.time()
    _STALE_QUEUED_SEC = 120  # Cancel queued parents older than 2 minutes

    from db.sqlite import pool
    try:
        with pool(db_path) as conn:


            rows = conn.execute("""
                SELECT id AS job_id, instance_id, created_at
                  FROM log_entries
                 WHERE parent_id IS NULL
                   AND job_type = 'health_check'
                   AND status = 'queued'
                   AND finished_at IS NULL
            """).fetchall()

            for row in rows:
                job_id = row["job_id"]
                instance_id = row["instance_id"]
                created_at = row["created_at"]

                try:
                    ts_clean = created_at.rstrip("Z") if isinstance(created_at, str) and created_at.endswith("Z") else created_at
                    dt = _dt_module.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=_dt_module.timezone.utc
                    )
                    age = now_epoch - dt.timestamp()
                except (ValueError, TypeError):
                    age = 0

                if age > _STALE_QUEUED_SEC:
                    # Cancel child tasks first
                    conn.execute(
                        "UPDATE log_entries SET status='cancelled', "
                        "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                        "WHERE parent_id=?", (job_id,),
                    )
                    # Cancel parent job
                    conn.execute(
                        "UPDATE log_entries SET status='cancelled', "
                        "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                        "WHERE id=? AND parent_id IS NULL", (job_id,),
                    )
                    reset_count += 1
                    logger.debug(
                        "[qr-scheduler] STALE-QUEUED: Job %d (inst=%d) cancelled (%.0fs old)",
                        job_id, instance_id, age,
                    )

    except Exception as exc:
        logger.error("[qr-scheduler] Stale queued detection failed: %s", exc, exc_info=True)

    return reset_count


def _find_parent_jobs_stale(db_path):
    """Detect parent jobs where all child tasks have finished but the parent is still 'running'.

    When a child task hangs (ansible subprocess deadlocks or produces unparseable output),
    the task never transitions to completed/failed/error. The parent job also stays in
    'running' indefinitely because "_finalize_job()" only fires when a task completes.

    Phase 3 detects this by querying for parent jobs that:
    - Have status='running' and no finished_at
    - All child tasks have finished (status IN ('completed','failed','error','cancelled'))
      OR: at least one child completed but remaining children stuck in 'queued' (claim query
          blocked the next stage from being claimed)
    - Parent age exceeds QR_TIMEOUT_DEFAULT (5min)

    The parent is then marked as 'completed' (if all children completed), 'error'
    (if any child failed or children are stuck queued), with an appropriate error_message.
    Instance state is also reset to 'stopped' to allow re-operation.

    Returns:
        int — number of stale parent jobs corrected.
    """
    reset_count = 0
    now_epoch = _time_module.time()

    from db.sqlite import pool
    try:
        with pool(db_path) as conn:
            # Query parent jobs: status=running, no finished_at, all children done
            # OR: at least one child completed but remaining children stuck in queued
            # (catches stale detection gap when claim query blocks next stage)
            rows = conn.execute("""
                SELECT le.id AS job_id,
                       le.instance_id,
                       le.job_type,
                       le.created_at
                  FROM log_entries le
                 WHERE le.parent_id IS NULL
                   AND le.status = 'running'
                   AND le.finished_at IS NULL
                   AND (
                       NOT EXISTS (
                           SELECT 1 FROM log_entries le2
                           WHERE le2.parent_id = le.id
                             AND le2.status IN ('queued', 'running')
                       )
                       OR (
                           -- Stuck queued children: at least one completed child,
                           -- but remaining children stuck in 'queued' for too long.
                           -- This catches the gap where claim query blocks the next stage.
                           EXISTS (
                               SELECT 1 FROM log_entries le3
                               WHERE le3.parent_id = le.id
                                 AND le3.status IN ('completed', 'failed', 'error')
                           )
                           AND NOT EXISTS (
                               SELECT 1 FROM log_entries le4
                               WHERE le4.parent_id = le.id
                                 AND le4.status = 'running'
                           )
                       )
                   )
            """).fetchall()

            for row in rows:
                job_id = row["job_id"]
                instance_id = row["instance_id"]
                job_type = row["job_type"] or "unknown"
                created_at = row["created_at"]

                # Parse created_at and check age
                try:
                    ts_clean = created_at.rstrip("Z") if isinstance(created_at, str) and created_at.endswith("Z") else created_at
                    dt = _dt_module.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=_dt_module.timezone.utc
                    )
                    age = now_epoch - dt.timestamp()
                except (ValueError, TypeError):
                    logger.warning(
                        "[qr-scheduler] STALE: Job %d has unparseable created_at '%s' — marking error",
                        job_id, created_at,
                    )
                    age = 0  # Force mark as error

                if age > _PARENT_STALE_SEC:
                    # Determine final state based on child results
                    result = conn.execute("""
                        SELECT COUNT(*) AS total,
                               SUM(CASE WHEN status IN ('failed','error','cancelled') THEN 1 ELSE 0 END) AS failures,
                               SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued
                          FROM log_entries
                         WHERE parent_id=?
                    """, (job_id,)).fetchone()

                    total = result["total"]
                    failures = result["failures"]
                    stuck_queued = result["queued"]

                    # Determine status and message
                    if failures > 0:
                        final_status = "error"
                        error_msg = f"All children done but parent stale ({age:.0f}s): {failures}/{total} child(ren) failed"
                    elif stuck_queued > 0:
                        # Children are stuck in queued — claim query blocked next stage
                        final_status = "error"
                        error_msg = f"Children stuck queued ({stuck_queued}/{total}) but parent stale ({age:.0f}s): claim query may have blocked next stage"
                    else:
                        final_status = "completed"
                        error_msg = None

                    # Use parameterized query to avoid SQL injection from final_status
                    if error_msg:
                        conn.execute(
                            "UPDATE log_entries SET status=?, "
                            "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                            "error_message=? WHERE id=?",
                            (final_status, error_msg, job_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE log_entries SET status=?, "
                            "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                            "WHERE id=?",
                            (final_status, job_id),
                        )

                    # Reset instance state to stopped (allows re-operation)
                    conn.execute(
                        "UPDATE instances SET state='stopped', "
                        "last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                        "WHERE id=?", (instance_id,),
                    )

                    reset_count += 1
                    if stuck_queued > 0:
                        logger.warning(
                            "[qr-scheduler] STALE-PARENT-QUEUED: Job %d (inst=%d, type=%s) "
                            "%d/%d children stuck queued, parent stale (%.0fs) — marking error",
                            job_id, instance_id, str(job_type or ""), stuck_queued, total, age,
                        )
                    else:
                        logger.warning(
                            "[qr-scheduler] STALE-PARENT: Job %d (inst=%d, type=%s, children=%d/%d failed) "
                            "stale (%.0fs old) — marking %s",
                            job_id, instance_id, str(job_type or ""), failures, total, age, final_status,
                        )

    except Exception as exc:
        logger.error("[qr-scheduler] Parent job stale detection failed: %s", exc, exc_info=True)

    return reset_count


def _find_stuck_transitions(db_path):
    """Find instances stuck in transitional states for too long.

    When an instance remains in configuring/deploying/compiling/updating
    state with no completed child task for over 2 hours, the deployment
    is likely stuck. Mark it as error so the user knows to investigate.

    Returns:
        int — number of stuck instances marked as error.
    """
    reset_count = 0
    now_epoch = _time_module.time()

    # Build IN clause placeholders dynamically for safety
    state_list = list(_TRANSITIONING_STATES)
    placeholders = ",".join("?" * len(state_list))

    conn = None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Query: instances in transitioning states with no completed child task
        rows = conn.execute(f"""
            SELECT id AS instance_id,
                   state AS status,
                   created_at
              FROM instances
             WHERE state IN ({placeholders})
               AND NOT EXISTS (
                   SELECT 1 FROM log_entries le2
                   WHERE le2.instance_id = instances.id
                     AND le2.status = 'completed'
               )
        """, state_list).fetchall()

        for row in rows:
            instance_id = row["instance_id"]
            status = row["status"]
            created_at = row["created_at"]

            try:
                ts_clean = created_at.rstrip("Z") if isinstance(created_at, str) and created_at.endswith("Z") else created_at
                dt = _dt_module.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=_dt_module.timezone.utc
                )
                age = now_epoch - dt.timestamp()
            except (ValueError, TypeError):
                logger.warning(
                    "[qr-scheduler] STUCK: Instance %d has unparseable created_at '%s'",
                    instance_id, str(created_at or ""),
                )
                continue

            if age > _STUCK_TRANSITION_SEC:
                conn.execute(
                    "UPDATE instances SET state='error', "
                    "last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (instance_id,),
                )
                conn.commit()
                reset_count += 1
                logger.warning(
                    "[qr-scheduler] STUCK-TRANSITION: Instance %d in '%s' for %.0fs — marked error",
                    instance_id, status, age,
                )

    except Exception as exc:
        logger.error("[qr-scheduler] Stuck transition detection failed: %s", exc, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _e:
                logger.debug("stale task DB cleanup conn.close failed: %s", _e)
                pass

    return reset_count
