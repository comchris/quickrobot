#!/usr/bin/env python3
"""Scheduler task runner — wraps PlaybookRunner for async task execution.

This module provides two public methods:
  - get_next_queued_task() — find the next task to execute
  - execute_task() — run a task (phase 1 DB setup + phase 2 background ansible)

All heavy lifting is delegated to lib.lib_runner.PlaybookRunner.
"""

import logging
import threading as _threading
import time as _time_module

logger = logging.getLogger("quickrobot.scheduler")


class SchedulerRunner:
    """Wrapper around PlaybookRunner for scheduler use.

    Handles background task execution with thread tracking,
    phase 1+phase 2 split, and job finalization logic.
    """

    def __init__(self, db_path, playbook_dir="playbooks/"):
        """Initialize the scheduler runner.

        Args:
            db_path: Path to SQLite database.
            playbook_dir: Path to playbook directory.
        """
        self.db_path = db_path
        self.playbook_dir = playbook_dir.rstrip("/") + "/"
        self._runner = None  # Lazy-init PlaybookRunner
        self._bg_threads = []  # Track background execution threads
        self._bg_lock = _threading.Lock()  # Guards _bg_threads list mutation
        self.max_retries = 3  # Overridden from config at startup (~4s)

    @property
    def runner(self):
        """Lazy-init PlaybookRunner to avoid circular imports."""
        if self._runner is None:
            from lib.lib_runner import PlaybookRunner
            self._runner = PlaybookRunner(self.db_path, self.playbook_dir)
        return self._runner

    def get_next_queued_task(self):
        """Get the next queued task across all jobs.

        Delegates to PlaybookRunner.get_next_queued_task() which handles:
        - Per-node lock (only one task per node at a time)
        - Atomic claim via BEGIN IMMEDIATE
        - Node_id ASC + created_at ASC ordering

        Returns:
            dict with task info, or None if no tasks are queued.
        """
        return self.runner.get_next_queued_task()

    def execute_task(self, task):
        """Execute a task: phase 1 (DB setup) + phase 2 (background ansible).

        Phase 1 runs synchronously (<10ms): marks task 'running', validates
        playbook integrity, computes extra vars.

        Phase 2 runs in a daemon thread: executes ansible playbook + finalizes job.
        The main scheduler loop continues immediately after phase 1 returns.

        Args:
            task: Task dict from get_next_queued_task().
        """
        # Phase 1: DB setup (synchronous, <10ms)
        try:
            setup = self.runner.execute_task_phase1(task["id"])
            if not setup["ok"]:
                logger.warning(
                    "[qr-scheduler] PHASE1 failed for task %d: %s",
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
                "[qr-scheduler] Task %d (%s) PHASE1 error on instance %d: %s",
                task["id"], task["stage"], task["instance_id"], exc, exc_info=True,
            )
            return

        # Phase 2: Background daemon thread for ansible + finalization
        t = _threading.Thread(
            target=self._bg_task_worker,
            args=(task, setup),
            daemon=True,
            name=f"task-{task['id']}-{task['stage']}",
        )
        t.start()

        with self._bg_lock:
            self._bg_threads.append(t)

    def _bg_task_worker(self, task, setup):
        """Background worker: run ansible playbook + update DB results.

        Args:
            task: Task dict from get_next_queued_task().
            setup: Phase 1 setup result dict from execute_task_phase1().
        """
        try:
            result = self.runner._run_task_playbook(
                task["id"],
                setup.get("job_id", task.get("job_id")),
                task["instance_id"],
                setup["playbook_path"],
                task["stage"],
                setup["node_hostname"],
                setup["extra_vars"],
                setup["instance"],
                task,
            )

            if result["success"]:
                logger.info(
                    "[qr-scheduler] Task %d (%s) completed in %dms on instance %d",
                    task["id"], task["stage"], result["duration_ms"], task["instance_id"],
                )

                # Check if job is complete (no more queued tasks)
                from db.sqlite import pool
                with pool(self.db_path) as conn:


                    pending = conn.execute(
                        "SELECT count(*) FROM log_entries WHERE parent_id=? AND status IN ('queued','running')",
                        (task["job_id"],),
                    ).fetchone()[0]

                    if pending == 0:
                        logger.info("[qr-scheduler] All tasks done for job %d — finalizing", task["job_id"])
                        finalized = False
                        for retry in range(self.max_retries):
                            try:
                                self.runner.complete_job(task["job_id"], conn=conn)
                                conn.commit()
                                finalized = True
                                break
                            except Exception as exc:
                                if "database is locked" in str(exc):
                                    logger.warning(
                                        "[qr-scheduler] DB locked during finalization, retrying... (%d/%d)",
                                        retry + 1, self.max_retries,
                                    )
                                    _time_module.sleep(2)
                                else:
                                    logger.error("[qr-scheduler] Finalization error for job %d: %s", task["job_id"], exc)
                                    break

                        if not finalized:
                            logger.error("[qr-scheduler] Finalization failed for job %d after retries", task["job_id"])

                conn.close()
            else:
                logger.warning(
                    "[qr-scheduler] Task %d (%s) FAILED on instance %d: %s (%dms)",
                    task["id"], task["stage"], task["instance_id"],
                    result.get("error", "unknown"), result["duration_ms"],
                )

                # Finalize parent job so Phase 3 stale detection can resolve it.
                # _run_task_playbook already set task status='failed' in DB;
                # we need to finalize the parent (mark error or requeue if retries remain).
                from db.sqlite import pool
                with pool(self.db_path) as conn:
                    try:
                        self.runner.complete_job(task["job_id"], conn=conn)
                        conn.commit()
                    except Exception as exc:
                        logger.error(
                            "[qr-scheduler] Failed to finalize job %d after task %d error: %s",
                            task["job_id"], task["id"], exc,
                        )

        except Exception as exc:
            logger.error(
                "[qr-scheduler] Background worker for task %d failed: %s",
                task["id"], exc, exc_info=True,
            )

    def create_periodic_health_check(self, instance_id):
        """Create a one-shot health check task for periodic scheduler runs.

        Delegates to PlaybookRunner.create_periodic_health_check() which creates
        a job header and task row in log_entries.

        Args:
            instance_id: Integer primary key of the instance.

        Returns:
            Dict with job info, or None if the instance doesn't exist.
        """
        return self.runner.create_periodic_health_check(instance_id)
