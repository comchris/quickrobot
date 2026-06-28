#!/usr/bin/env python3
# Copyright 2026 comchris quickrobot .de project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""quickrobot — Scheduler Engine (RUNNER-1).

Background subprocess that polls for queued jobs, executes stages via
PlaybookRunner, and updates instance state on completion.

Usage:
    python3 engine/quickrobot_scheduler/__main__.py [--db PATH] [--interval SECS]

This engine is registered as engine_type_id=23 (quickrobot-scheduler),
system-managed instance id=4, PID tracked in instances.pid_last_known.
"""

import argparse
import logging
import os
import signal
import sys
import threading as _threading
import time as _time_module

# ── SSOT timeouts for stale task detection ─────────────────────────
from lib.qr_engine_ids import (
    QR_TIMEOUT_COMPILE,   # 1800s = 30 min for cmake build
    QR_TIMEOUT_SOURCE,    # 600s = 10 min for git clone
    QR_TIMEOUT_DEFAULT,   # 300s = 5 min default for other stages
)

# Grace period: extra seconds beyond the stage timeout before declaring
# a legitimately-running task as stale. This prevents resetting compiles
# that are still progressing normally (e.g., large models taking longer).
_STALE_GRACE_PERIOD = 300  # 5 minutes extra

# Global grace period after task phase1 completes: skip ALL stale checks
# for this long. Covers the gap between DB status='running' and actual
# ansible subprocess spawning on the remote host (~2-5s typical, up to 10s).
_TASK_START_GRACE = 15  # seconds

# Helper: parse ISO-8601 UTC datetime string to epoch seconds.
# SQLite stores dates in UTC, but Python's naive .timestamp() assumes local TZ.
# This function always treats the input as UTC to avoid timezone drift.
def _parse_utc_epoch(ts_str):
    """Parse 'YYYY-MM-DDTHH:MM:SS' (UTC) to epoch seconds."""
    import datetime as _dt3
    st = _dt3.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
    return _dt3.datetime(
        st.year, st.month, st.day, st.hour, st.minute, st.second,
        tzinfo=_dt3.timezone.utc
    ).timestamp()

# Ensure project root is on sys.path so sibling packages (db/, lib/) resolve.
# This is required when running as `python3 -m engine.quickrobot_scheduler`
# because Python only adds the package directory (engine/) to sys.path.
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [qr-scheduler] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("quickrobot.scheduler")


class QueuedJobRunner:
    """Background scheduler loop.

    Three-phase execution:
      1. Due scheduled jobs → requeue when next_run_at <= now
      2. Recurring job reschedule → set next_run_at for completed recurring jobs
      3. Execute queued tasks → pick next task, run playbook, update results
    """

    def __init__(self, db_path, poll_interval=5, max_retries=3, playbook_dir="playbooks/"):
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.playbook_dir = playbook_dir.rstrip("/") + "/"
        self.running = False
        self._runner = None  # Lazy-init to avoid circular imports
        self._bg_threads: list[_threading.Thread] = []  # Track background exec threads
        self._bg_lock = _threading.Lock()  # Guards _bg_threads list mutation

    @property
    def runner(self):
        """Lazy import of PlaybookRunner to avoid module load issues."""
        if self._runner is None:
            from lib.lib_runner import PlaybookRunner
            self._runner = PlaybookRunner(self.db_path, self.playbook_dir)
        return self._runner

    def run(self):
        """Main loop — runs until process receives SIGTERM/SIGINT."""
        self.running = True
        logger.info("[qr-scheduler] Starting (db=%s, interval=%ds, retries=%d)", self.db_path, self.poll_interval, self.max_retries)

        # Detect and reset stale 'running' tasks left by a crashed scheduler.
        # This prevents zombie tasks from blocking future task selection.
        stale = self._detect_stale_tasks()
        if stale > 0:
            logger.info("[qr-scheduler] Recovered %d stale task(s)", stale)

        # Setup signal handlers
        def _shutdown(signum, frame):
            logger.info("[qr-scheduler] Received signal %d, shutting down", signum)
            self.running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        while self.running:
            try:
                self._process_cycle()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.error("[qr-scheduler] Loop error: %s", exc, exc_info=True)
            _time_module.sleep(self.poll_interval)

        logger.info("[qr-scheduler] Stopped")

    def _process_cycle(self):
        """Single processing cycle: schedule → execute."""
        self._due_scheduled_jobs()
        self._reschedule_recurring_jobs()
        # Detect stale tasks before picking new ones (catches mid-session crashes)
        self._detect_stale_tasks()
        task = self.runner.get_next_queued_task()
        if task:
            self._execute_task(task)

    def _due_scheduled_jobs(self):
        """Move scheduled jobs to queued when next_run_at <= now."""
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status='scheduled' AND next_run_at <= strftime('%Y-%m-%dT%H:%M:%S','now') AND disabled=0"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET status='queued', updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                    (row["id"],),
                )
            if rows:
                logger.info("[qr-scheduler] Requeued %d scheduled job(s)", len(rows))

    def _reschedule_recurring_jobs(self):
        """Set next_run_at for completed recurring jobs."""
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, recurrence_interval FROM jobs WHERE status='completed' AND recurrence_interval > 0"
            ).fetchall()
            for row in rows:
                conn.execute(
                    f"UPDATE jobs SET status='scheduled', next_run_at=datetime('now', '+{row['recurrence_interval']} seconds'), updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id={row['id']}",
                )

    def _detect_stale_tasks(self):
        """Detect and reset stale 'running' tasks from a crashed scheduler.

        A task is considered stale (zombie) if:
        A) status='running' AND started_at IS NULL — scheduler crashed after
           marking running but before starting ansible (immediate crash).
        B) status='running' AND started_at IS NOT NULL AND
           (now - started_at) > stage_timeout — task has been running longer
           than the expected stage timeout, so it's stuck.

        Reset stale tasks to 'queued' so the new scheduler can pick them up.

        Also resets jobs that only have one running task with all others
        already completed/cancelled (the job is effectively stalled).

        Returns:
            int — number of tasks reset.
        """
        from db.sqlite import pool

        # Stage timeout lookup: stage_name → timeout in seconds.
        # Timeout = max expected duration for that stage type.
        _STAGE_TIMEOUTS = {
            "compile": QR_TIMEOUT_COMPILE,  # 30 min cmake build
            "source": QR_TIMEOUT_SOURCE,     # 10 min git clone/pull
        }
        # Default timeout for all other stages (preflight, deps, config_svc,
        # config_env, start, stop, health_probe, etc.)
        DEFAULT_TIMEOUT = QR_TIMEOUT_DEFAULT  # 5 min

        with pool(self.db_path) as conn:
            # Find all currently 'running' tasks
            running_tasks = conn.execute("""
                SELECT t.id, t.stage, t.started_at, t.instance_id, t.job_id
                FROM tasks t
                WHERE t.status = 'running'
            """).fetchall()

        if not running_tasks:
            return 0

        reset_count = 0
        now_epoch = _time_module.time()

        # Lookup instance → hostname map for ansible process check (Case C)
        host_map = {}
        with pool(self.db_path) as conn:
            for r in conn.execute("""
                SELECT t.id, i.node_hostname FROM tasks t
                JOIN instances i ON t.instance_id = i.id
                WHERE t.status = 'running'
            """).fetchall():
                host_map[r["id"]] = r["node_hostname"]

        # Get list of ansible-playbook processes and their --limit values
        import subprocess as _subprocess
        try:
            ans_res = _subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5
            )
            active_ansible_hosts = set()
            for line in ans_res.stdout.splitlines():
                if "ansible-playbook" not in line:
                    continue
                if "--limit" in line:
                    limit_val = line.split("--limit")[-1].strip().split()[0]
                    active_ansible_hosts.add(limit_val)
        except Exception:
            active_ansible_hosts = set()

        for rt in running_tasks:
            task_id = rt["id"]
            stage = rt["stage"]
            started_at = rt["started_at"]
            instance_id = rt["instance_id"]
            job_id = rt["job_id"]

            # Global grace period: skip ALL stale checks for tasks started
            # within the last 15s. This covers the gap between execute_task
            # phase1 (marks task 'running' in DB) and the ansible subprocess
            # actually spawning on the remote host. Without this, every new
            # task gets reset to 'queued' before it can even begin running.
            if started_at is not None:
                try:
                    st_epoch = _parse_utc_epoch(started_at)
                    if (now_epoch - st_epoch) < _TASK_START_GRACE:
                        continue  # Skip this task — still in startup window
                except (ValueError, TypeError):
                    pass

            reason = None
            if started_at is None:
                # Case A: immediate crash — no started_at means the scheduler
                # died after setting status='running' but before starting
                # ansible. This has been stuck since the task was created.
                reason = "started_at=NULL (crashed before ansible start)"
            else:
                # Case C: check if an actual ansible process exists for this
                # host. If no ansible subprocess is running on the target host,
                # the task is effectively dead even if within timeout. This
                # catches crashes between marking 'running' and starting ansible.
                # (Global grace period at loop-top already skips tasks < 15s old.)
                hostname = host_map.get(task_id)
                if hostname and hostname not in active_ansible_hosts:
                    reason = f"no ansible process for host={hostname} (crashed after DB mark)"

                if not reason:
                    # Case B: check duration against stage timeout
                    try:
                        st_epoch = _parse_utc_epoch(started_at)
                        duration = now_epoch - st_epoch

                        timeout = _STAGE_TIMEOUTS.get(stage, DEFAULT_TIMEOUT) + _STALE_GRACE_PERIOD
                        if duration > timeout:
                            reason = f"duration={int(duration)}s > timeout={timeout}s"
                    except (ValueError, TypeError):
                        reason = "unparseable started_at"

            if reason:
                with pool(self.db_path) as conn:
                    conn.execute(
                        "UPDATE tasks SET status='queued', started_at=NULL, "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                        (task_id,),
                    )
                    conn.commit()
                reset_count += 1
                logger.warning(
                    "[qr-scheduler] STALE: Reset task %d (inst=%d, stage=%s) to queued: %s",
                    task_id, instance_id, stage, reason,
                )

        # Global job timeout: jobs older than their configured timeout get expired.
        # Catches zombies where finalize failed and the job stayed in 'running' state.
        # Also catches deeply queued jobs that the scheduler missed.
        with pool(self.db_path) as conn:
            expired_jobs = conn.execute("""
                SELECT j.id, j.instance_id, j.created_at, j.status, j.timeout_seconds
                FROM jobs j
                WHERE j.status IN ('running', 'queued')
                  AND (j.finished_at IS NULL OR j.finished_at = '')
                  AND j.timeout_seconds > 0
            """).fetchall()

        for ej in expired_jobs:
            job_id = ej["id"]
            timeout = ej["timeout_seconds"] or 7200  # Default 2h fallback
            try:
                created_epoch = _parse_utc_epoch(ej["created_at"])
                age = now_epoch - created_epoch
                if age > timeout:
                    reason = f"job_age={int(age)}s > timeout={timeout}s"
                    with pool(self.db_path) as conn:
                        # Reset any running tasks back to queued for retry
                        conn.execute(
                            "UPDATE tasks SET status='queued', started_at=NULL, "
                            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                            "WHERE job_id=? AND status='running'", (job_id,),
                        )
                        # Mark job as expired
                        conn.execute(
                            "UPDATE jobs SET status='error', "
                            "error_message='Job expired: age exceeded timeout', "
                            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                            (job_id,),
                        )
                    reset_count += 1
                    logger.warning(
                        "[qr-scheduler] EXPIRED: Job %d (inst=%d, type=%s) expired: %s",
                        job_id, ej["instance_id"], ej.get("job_type", "unknown"), reason,
                    )
            except (ValueError, TypeError):
                pass  # Malformed created_at — skip

        # Also check for jobs that have one 'running' task but all other
        # tasks are already completed/cancelled — the job is stalled.
        # This can happen when a crash leaves the last running task stuck.
        with pool(self.db_path) as conn:
            stalled_jobs = conn.execute("""
                SELECT j.id, j.instance_id
                FROM jobs j
                WHERE j.status IN ('running', 'queued')
                  AND (
                      SELECT COUNT(*) FROM tasks t
                      WHERE t.job_id = j.id AND t.status = 'running'
                  ) = 1
                  AND (
                      SELECT COUNT(*) FROM tasks t
                      WHERE t.job_id = j.id
                        AND t.status IN ('queued', 'completed')
                  ) = 0
            """).fetchall()

        for sj in stalled_jobs:
            job_id = sj["id"]
            # Check if the single running task is stale
            logger.debug("checking job %d with pool(db_path=%s)", job_id, self.db_path)
            try:
                with pool(self.db_path) as sconn:
                    single_running = sconn.execute("""
                        SELECT id, started_at, instance_id, stage
                        FROM tasks WHERE job_id=? AND status='running'
                        LIMIT 1
                    """, (job_id,)).fetchone()
                    logger.debug("job %d single_running=%s", job_id, single_running)
            except Exception as e:
                logger.debug("job %s: %s: %s", job_id, type(e).__name__, e)
                raise

            if single_running and single_running["started_at"] is None:
                # The running task in a stalled job is stale — reset it
                with pool(self.db_path) as conn:
                    conn.execute(
                        "UPDATE tasks SET status='queued', started_at=NULL, "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                        (single_running["id"],),
                    )
                    # Also reset job status if it was 'running'
                    conn.execute(
                        "UPDATE jobs SET status='queued', updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                        (job_id,),
                    )
                    conn.commit()
                reset_count += 1
                logger.warning(
                    "[qr-scheduler] STALL: Reset job %d (inst=%d, task %d/%s) to queued — "
                    "stale running task in stalled job",
                    job_id, sj["instance_id"], single_running["id"],
                    single_running["stage"],
                )

        return reset_count

    def _execute_task(self, task):
        """Execute a task asynchronously via daemon thread (Option C).

        Phase 1 (synchronous, <10ms): calls execute_task_phase1() which updates
        DB to 'running', computes vars, locks node. Returns immediately.

        Phase 2 (background daemon thread): runs ansible playbook + finalization.
        The main scheduler loop continues immediately after phase 1, picking up
        the next queued task on a different node.

        This enables parallel execution: while node-A compiles for 25 minutes,
        the main loop picks up tasks on node-B, node-C, etc. each poll cycle.

        Args:
            task: Task dict from get_next_queued_task().
        """
        import threading as _threading
        import time as _time

        # Phase 1: DB setup (instant — marks task 'running', updates instance state)
        try:
            setup = self.runner.execute_task_phase1(task["id"])
            if not setup["ok"]:
                logger.warning(
                    "[qr-scheduler] Phase1 failed for task %d: %s",
                    task["id"], setup.get("error", "unknown"),
                )
                return
        except FileNotFoundError as exc:
            logger.error(
                "[qr-scheduler] Task %d (%s) FAILED — playbook missing on instance %d: %s",
                task["id"], task["stage"], task["instance_id"], exc,
            )
            return
        except Exception as exc:
            logger.error(
                "[qr-scheduler] Task %d (%s) FAILED — %s on instance %d: %s",
                task["id"], task["stage"], type(exc).__name__, task["instance_id"], exc,
            )
            return

        # Phase 2: Run ansible + finalization in background daemon thread
        def _bg_task_worker(task_id, stage, inst_id, job_id, node_hostname):
            """Background worker: run ansible playbook + update DB results."""
            import time as _time

            try:
                result = self.runner._run_task_playbook(
                    task_id, job_id, inst_id,
                    setup["playbook_path"], stage, node_hostname,
                    setup["extra_vars"], setup["instance"], setup["task"],
                )
                if result["success"]:
                    logger.info(
                        "[qr-scheduler] Task %d (%s) completed in %dms on instance %d",
                        task_id, stage, result["duration_ms"], inst_id,
                    )
                    # Check if job is complete (no more queued tasks)
                    from db.sqlite import pool
                    with pool(self.db_path) as conn:
                        pending = conn.execute(
                            "SELECT count(*) FROM tasks WHERE job_id=? AND status IN ('queued','running')",
                            (job_id,),
                        ).fetchone()[0]
                        if pending == 0:
                            logger.info("[qr-scheduler] All tasks done for job %d — finalizing", job_id)
                            finalized = False
                            for _retry in range(self.max_retries):
                                try:
                                    self.runner.complete_job(job_id, conn=conn)
                                    conn.commit()
                                    finalized = True
                                    break
                                except Exception as _exc:
                                    if "database is locked" in str(_exc):
                                        logger.warning("[qr-scheduler] DB locked during finalization, retrying... (%d/%d)", _retry + 1, self.max_retries)
                                        _time_module.sleep(2)
                                    else:
                                        logger.error("[qr-scheduler] Finalization error for job %d: %s", job_id, _exc)
                                        raise
                            if not finalized:
                                logger.error("[qr-scheduler] Finalization failed for job %d after retries", job_id)
                else:
                    logger.warning(
                        "[qr-scheduler] Task %d (%s) failed on instance %d: %s (%dms)",
                        task_id, stage, inst_id,
                        result.get("error", "unknown"), result["duration_ms"],
                    )
            except Exception as exc:
                logger.error("[qr-scheduler] Background worker for task %d failed: %s", task_id, exc)

        t = _threading.Thread(
            target=_bg_task_worker,
            args=(task["id"], task["stage"], task["instance_id"],
                  task["job_id"], setup["node_hostname"]),
            daemon=True,
            name=f"task-{task['id']}-{task['stage']}",
        )
        t.start()

        # Track thread for cleanup on shutdown
        with self._bg_lock:
            self._bg_threads.append(t)


def _load_scheduler_config(db_path):
    """Read scheduler config from engine_configs table.

    Returns dict with keys: poll_interval_sec (int), log_level (str),
    max_retries (int). Falls back to CLI defaults if no DB config found.
    """
    config = {"poll_interval_sec": 5, "log_level": "info", "max_retries": 3}
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT key, value FROM engine_configs WHERE engine_type_id=(SELECT id FROM engine_types WHERE name='quickrobot-scheduler')"
        ).fetchall()
        for row in rows:
            key, value = row[0], row[1]
            if key == "scheduler_poll_interval_sec":
                try: config["poll_interval_sec"] = int(value)
                except (ValueError, TypeError): pass
            elif key == "scheduler_log_level":
                config["log_level"] = str(value)
            elif key == "scheduler_max_retries":
                try: config["max_retries"] = max(1, int(value))
                except (ValueError, TypeError): pass
        conn.close()
    except Exception:
        pass  # Non-critical — use CLI defaults
    return config

def main():
    """Entry point for scheduler subprocess."""
    # Root guard — refuse to run as root (non-interactive background worker)
    if os.getuid() == 0:
        print("this robot won't run as root", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Quickrobot Staged Playbook Scheduler")
    parser.add_argument("--db", default="data/quickrobot.db", help="Path to SQLite database")
    parser.add_argument("--interval", type=int, default=5, help="Poll interval in seconds")
    parser.add_argument("--playbook-dir", default="playbooks/", help="Playbook directory")
    args = parser.parse_args()

    # Resolve db path relative to project root
    if not os.path.isabs(args.db):
        args.db = os.path.join(os.getcwd(), args.db)

    # Validate required API connection config — no silent fallbacks
    api_host = os.getenv("QUICKROBOT_API_HOST")
    api_port_raw = os.getenv("QUICKROBOT_API_PORT")
    if not api_host:
        print("[qr-scheduler] FATAL: QUICKROBOT_API_HOST not set. Define it in .quickrobot.env or pass --api-host.", file=sys.stderr)
        sys.exit(1)
    if not api_port_raw:
        print(f"[qr-scheduler] FATAL: QUICKROBOT_API_PORT not set (host={api_host}). Define it in .quickrobot.env or pass --api-port.", file=sys.stderr)
        sys.exit(1)
    try:
        api_port = int(api_port_raw)
    except ValueError:
        print(f"[qr-scheduler] FATAL: QUICKROBOT_API_PORT value '{api_port_raw}' is not a valid integer.", file=sys.stderr)
        sys.exit(1)

    # Load config from engine_configs table (respects WebUI/DB overrides)
    db_config = _load_scheduler_config(args.db)
    poll_interval = db_config["poll_interval_sec"]
    log_level = db_config["log_level"]
    max_retries = db_config["max_retries"]

    # Apply log level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(numeric_level)
    for handler in logging.getLogger().handlers:
        handler.setLevel(numeric_level)
    logger.setLevel(numeric_level)

    # Log rotation (vC): truncate oversized log files on startup
    from lib.lib_system_engine import get_engine_log_path as _eng_log, rotate_log_if_needed as _rot
    _rot(_eng_log("scheduler"), "scheduler")

    # Structured startup log — single line with all config info minus tokens
    logger.info("STARTUP: pid=%d db=%s api=%s:%d interval=%ds log_level=%s",
                os.getpid(), args.db, api_host, api_port, poll_interval, log_level)

    runner = QueuedJobRunner(
        db_path=args.db,
        poll_interval=poll_interval,
        max_retries=max_retries,
        playbook_dir=args.playbook_dir,
    )

    # Enforce minimum poll interval of 1 second
    MIN_POLL_INTERVAL = int(os.environ.get("QUICKROBOT_SCHEDULER_MIN_INTERVAL", "1"))
    if runner.poll_interval < MIN_POLL_INTERVAL:
        logger.warning("[qr-scheduler] poll_interval=%ds < minimum %ds, clamping", runner.poll_interval, MIN_POLL_INTERVAL)
        runner.poll_interval = MIN_POLL_INTERVAL

    # === Start periodic health check thread ===
    import threading as _threading

    def _health_loop():
        """Periodic health check for scheduler subprocess."""
        import time as _time
        import requests as _requests
        from lib.lib_system_engine import api_health_check_loop as _hloop
        _hloop(api_host=api_host, api_port=api_port, max_retries=3, retry_delay=5, check_interval=10)
    
    _health_thread = _threading.Thread(target=_health_loop, daemon=True, name="scheduler-health-check")
    _health_thread.start()
    logger.info("[qr-scheduler] Health check thread started (interval=60s)")

    runner.run()


if __name__ == "__main__":
    main()
