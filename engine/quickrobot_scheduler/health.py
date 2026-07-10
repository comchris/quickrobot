#!/usr/bin/env python3
"""Scheduler health check cycle.

For each eligible instance:
  1. Check if instance already has a running task → skip if yes
  2. Check interval gate using last_state_change → skip if within window
  3. Queue health check (remote) or process check (local)
  4. Update last_state_change immediately

Config is read from engine_configs table once per cycle, cached in-memory.
"""

import logging
import time as _time_module
import datetime as _dt_module

logger = logging.getLogger("quickrobot.scheduler")

# Local engine types — monitored via process tree, not ansible playbooks
from lib.qr_engine_ids import QR_ENGINE_SUBPROCESS, QR_ENGINE_SCHEDULER

_LOCAL_ENGINE_TYPES = frozenset([QR_ENGINE_SUBPROCESS])

# Default intervals (seconds) used when config is unavailable
_DEFAULT_INTERVALS = {
    "running": 60,
    "stopped": 600,
    "error": 20,
    "_default": 120,
}


def _load_health_intervals(db_path):
    """Load health check intervals from engine_configs.

    Returns dict with keys: running, stopped, error, _default.
    Falls back to defaults for missing keys. Clamped to 10-3600 range.

    Args:
        db_path: Path to SQLite database.

    Returns:
        dict — interval configuration keyed by state group.
    """
    from db.adapters.configs import get_engine_config as _gec

    key_map = {
        "check_interval_running_sec": "running",
        "check_interval_stopped_sec": "stopped",
        "check_interval_error_sec": "error",
        "check_interval_default_sec": "_default",
    }

    config = {}
    for config_key, state_key in key_map.items():
        row = _gec(db_path, QR_ENGINE_SCHEDULER, config_key)
        raw = row["value"] if row else "" if row else ""

        try:
            v = int(raw)
        except (ValueError, TypeError):
            config[state_key] = _DEFAULT_INTERVALS.get(state_key, 60)
            continue

        # Clamp to valid range
        clamped = max(10, min(3600, v)) if v > 0 else _DEFAULT_INTERVALS.get(state_key, 60)
        config[state_key] = clamped

    # Ensure all keys present (merge with defaults for missing)
    for k, v in _DEFAULT_INTERVALS.items():
        if k not in config:
            config[k] = v

    return config


def _parse_timestamp(ts_str):
    """Parse ISO-8601 UTC timestamp string to datetime.

    Args:
        ts_str: Timestamp string (e.g., '2026-07-09T12:00:00Z') or None.

    Returns:
        datetime or None on parse failure.
    """
    if not ts_str:
        return None
    try:
        cleaned = ts_str.rstrip("Z") if isinstance(ts_str, str) and ts_str.endswith("Z") else ts_str
        return _dt_module.datetime.strptime(cleaned, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt_module.timezone.utc
        )
    except (ValueError, TypeError):
        return None


def run_health_cycle(db_path, runner, hc_logging=True):
    """Execute one cycle of health checks across all eligible instances.

    Args:
        db_path: Path to SQLite database.
        runner: PlaybookRunner instance for creating health check jobs.
        hc_logging: Whether to log debug-level health check messages.

    Returns:
        dict with keys: queued (int), skipped_no_running_task (int),
                        skipped_interval_window (int), skipped_disabled (int),
                        errors (int).
    """
    result = {
        "queued": 0,
        "skipped_no_running_task": 0,
        "skipped_interval_window": 0,
        "skipped_disabled": 0,
        "errors": 0,
    }

    # Load interval config once per cycle
    intervals = _load_health_intervals(db_path)
    state_to_interval = {
        "running": intervals.get("running", 60),
        "starting": intervals.get("running", 60),
        "deploying": intervals.get("running", 60),
        "configuring": intervals.get("running", 60),
        "updating": intervals.get("running", 60),
        "compiling": intervals.get("running", 60),
        "loading": intervals.get("running", 60),
        "stopped": intervals.get("stopped", 600),
        "error": intervals.get("error", 20),
        "build_error": intervals.get("error", 20),
        "timeout": intervals.get("error", 20),
    }
    default_interval = intervals.get("_default", 120)

    # Fetch eligible instances: not system-managed, health check enabled, on active node
    from db.sqlite import pool
    try:
        with pool(db_path) as conn:


            rows = conn.execute("""
                SELECT i.id, i.name, i.state, i.engine_type_id, i.node_id,
                       i.pid_last_known, i.last_state_change
                  FROM instances i
                 WHERE i.system_managed = 0
                   AND i.health_check_enabled = 1
                   AND i.id IN (
                       SELECT i2.id FROM instances i2
                       JOIN nodes n ON i2.node_id = n.id
                       WHERE n.is_active = 1
                         AND i2.state IN ('running','starting','deploying','configuring',
                                          'updating','compiling','loading','stopped',
                                          'error','build_error','timeout')
                   )
            """).fetchall()

    except Exception as exc:
        logger.error("[qr-scheduler] Health cycle: failed to query instances: %s", exc, exc_info=True)
        return result

    # Process each eligible instance
    for row in rows:
        instance_id = row["id"]
        state = row["state"]
        engine_type_id = row["engine_type_id"]
        node_id = row["node_id"]
        is_local = (node_id == 1) or (engine_type_id in _LOCAL_ENGINE_TYPES)

        # Check if instance already has a running task — skip if yes
        try:
            with pool(db_path) as conn2:
                has_running = conn2.execute(
                    "SELECT 1 FROM log_entries WHERE instance_id=? AND status='running' LIMIT 1",
                    (instance_id,),
                ).fetchone() is not None
        except Exception as exc:
            logger.warning("[qr-scheduler] Health cycle: failed to check running tasks for inst=%d: %s", instance_id, exc)
            result["errors"] += 1
            continue

        if has_running:
            result["skipped_no_running_task"] += 1
            continue

        # Check interval gate
        interval = state_to_interval.get(state, default_interval)
        if interval <= 0:
            result["skipped_disabled"] += 1
            continue

        lc_dt = _parse_timestamp(row["last_state_change"])
        if lc_dt is not None:
            now_utc = _dt_module.datetime.now(_dt_module.timezone.utc)
            elapsed = (now_utc - lc_dt).total_seconds()
            if elapsed < interval:
                result["skipped_interval_window"] += 1
                continue

        # Queue health check or do local process check
        if is_local:
            _do_local_health_check(db_path, instance_id, row)
        else:
            success = _queue_health_check(db_path, runner, instance_id)
            if success:
                result["queued"] += 1
                if hc_logging:
                    logger.debug(
                        "[qr-scheduler] Health check queued for instance %d (%s, state=%s)",
                        instance_id, row["name"], state,
                    )

    return result


def _do_local_health_check(db_path, instance_id, row):
    """Perform local health check via process tree inspection.

    Checks if the instance's PID is alive. If yes and instance was in error state,
    auto-recover to running. If dead and not already stopped/error, mark as error.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.
        row: Instance row dict from query (includes pid_last_known).
    """
    from engine.quickrobot_scheduler.stale import _process_is_alive
    from db.sqlite import pool

    pid = row["pid_last_known"] if row["pid_last_known"] else None
    if not pid:
        return

    try:
        if _process_is_alive(pid):
            # Process is alive — check if we should auto-recover error→running
            with pool(db_path) as conn:
                cur_state = conn.execute(
                    "SELECT state FROM instances WHERE id=?", (instance_id,)
                ).fetchone()
                if cur_state and cur_state["state"] in ("error", "build_error"):
                    # Auto-recover: update last_state_change and transition to running
                    conn.execute(
                        "UPDATE instances SET state='running', "
                        "last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (instance_id,),
                    )
                    logger.info(
                        "[qr-scheduler] Local health OK: instance %d (pid=%d) recovered to running",
                        instance_id, pid,
                    )
        else:
            # Process is dead — mark as error if not already stopped/error
            with pool(db_path) as conn:
                cur_state = conn.execute(
                    "SELECT state FROM instances WHERE id=?", (instance_id,)
                ).fetchone()
                if cur_state and cur_state["state"] not in ("stopped", "error"):
                    conn.execute(
                        "UPDATE instances SET state='error', "
                        "last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (instance_id,),
                    )
                    logger.warning(
                        "[qr-scheduler] Local health FAIL: instance %d pid=%d dead, marked error",
                        instance_id, pid,
                    )

    except Exception as exc:
        logger.warning("[qr-scheduler] Local health check for instance %d failed: %s", instance_id, exc)


def _queue_health_check(db_path, runner, instance_id):
    """Queue a health check task for a remote instance and update last_state_change.

    Before queuing, cleans up any stale queued parent jobs from previous cycles
    (caused by DB lock contention or scheduler restart). This prevents accumulation
    of thousands of orphaned 'queued' entries in log_entries.

    Calls PlaybookRunner.create_periodic_health_check() which creates a job header
    and task row in log_entries. On success, immediately updates the instance's
    last_state_change timestamp so the interval gate works correctly.

    Args:
        db_path: Path to SQLite database.
        runner: PlaybookRunner instance.
        instance_id: Instance primary key.

    Returns:
        True if health check was queued and last_state_change updated, False otherwise.
    """
    # Clean up stale queued parent jobs from previous cycles
    from db.sqlite import pool
    try:
        with pool(db_path) as _conn:
            _cursor = _conn.execute(
                "SELECT id FROM log_entries WHERE instance_id=? AND parent_id IS NULL "
                "AND job_type='health_check' AND status='queued'",
                (instance_id,),
            )
            stale_ids = [r[0] for r in _cursor.fetchall()]
            if stale_ids:
                placeholders = ",".join("?" * len(stale_ids))
                _conn.execute(
                    f"UPDATE log_entries SET status='cancelled', "
                    f"finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                    f"WHERE parent_id IN ({placeholders})",
                    stale_ids,
                )
    except Exception as exc:
        logger.debug(
            "[qr-scheduler] Stale cleanup skipped for inst=%d: %s", instance_id, exc,
        )

    result = runner.create_periodic_health_check(instance_id)
    if not result:
        logger.warning(
            "[qr-scheduler] Failed to queue health check for instance %d (create returned None)",
            instance_id,
        )
        return False

    # Update last_state_change immediately — this is the critical fix.
    # Previous code used batch-write after loop which silently failed for some instances.
    try:
        now_str = _dt_module.datetime.now(_dt_module.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with pool(db_path) as conn:
            conn.execute(
                "UPDATE instances SET last_state_change=? WHERE id=?",
                (now_str, instance_id),
            )
    except Exception as exc:
        logger.error(
            "[qr-scheduler] Failed to update last_state_change for instance %d: %s",
            instance_id, exc, exc_info=True,
        )
        return False

    return True
