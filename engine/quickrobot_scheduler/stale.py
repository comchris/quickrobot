#!/usr/bin/env python3
"""Scheduler stale detection — orphaned jobs and stuck transitions.

Three-phase detection runs once at startup and periodically:
  Phase 1: Orphaned parent jobs (no children, >30s old) → mark error
  Phase 1b: Stuck running health_check jobs only (parent+children both running >60s)
            Deploy/rebuild jobs rely on Phase 2's 2h timeout — compiles can take 30min
  Phase 1c: Queued health_check parents >120s old (DB lock contention accumulation)
  Phase 2: Transitioning instances stuck for >2h → mark error

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
    QR_TIMEOUT_HEALTH_CHECK,    # 60s (used as stale threshold for health checks)
    QR_TIMEOUT_JOB,             # 7200s = 2h (stuck transition timeout)
)

logger = logging.getLogger("quickrobot.scheduler")


# Stale detection thresholds (use SSOT constants where available)
_ORPHAN_AGE_SEC = 30           # Orphaned parent jobs (>30s, no children)
_STUCK_TRANSITION_SEC = QR_TIMEOUT_JOB  # 2 hours

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

    # Phase 2: Stuck transitioning instances
    reset_count += _find_stuck_transitions(db_path)

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
    up to 30min (QR_TIMEOUT_COMPILE). They rely on Phase 2's 2h timeout instead.

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
            except Exception:
                pass

    return reset_count
