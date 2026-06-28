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

"""quickrobot — Staged Playbook Runner (RUNNER-1).

Orchestrates engine lifecycles as chains of focused, independently
retryable playbooks. Each engine type declares its stage sequence;
the runner creates jobs + tasks, executes them sequentially, and
updates instance state on completion.

Architecture: CQRS-lite
  - Command side (this module): creates jobs, runs playbooks, writes results
  - Query side (API routes): reads jobs/tasks status, <10ms per call

Key classes:
  PlaybookRunner — Main orchestrator; create_job(), run_stage(), complete_job()
  StageRegistry  — Maps engine_type_name → ordered list of stages
"""

import json
import logging
import os
import time

# ── SSOT imports — engine IDs, names, stage constants ────────────────
from lib.qr_engine_ids import (
    QR_ENGINE_API_NAME,
    QR_ENGINE_LLAMA_SERVER_NAME,
    QR_ENGINE_IPERF3_NAME,
    QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_UNIVERSAL_NAME,
    QR_ENGINE_PORT_DEFAULTS,
    STAGE_STATE_MAP,
    SKIPABLE_STAGES,
    JOB_FINAL_STATES,
    QR_JOB_DEPLOY,
    QR_JOB_REBUILD,
    QR_JOB_RECONFIGURE,
    QR_JOB_DEPLOY_FAST,
    QR_JOB_UNDEPLOY,
    QR_JOB_BIND,
    QR_JOB_UNBIND,
    QR_JOB_START,
    QR_JOB_RESTART,
    QR_JOB_STOP,
    QR_JOB_REBOOT,
    QR_JOB_APT_UPDATE,
    QR_JOB_APT_UPGRADE,
    QR_JOB_APT_ALL,
    QR_STAGE_PREFLIGHT,
    QR_STAGE_DEPS,
    QR_STAGE_SOURCE,
    QR_STAGE_COMPILE,
    QR_STAGE_CONFIG_SVC,
    QR_STAGE_CONFIG_ENV,
    QR_STAGE_START,
    QR_STAGE_STOP,
    QR_STAGE_HEALTH_PROBE,
    QR_TIMEOUT_COMPILE,
    QR_TIMEOUT_SOURCE,
    QR_TIMEOUT_DEFAULT,
    _QR_UNDEPLOY_CHAINS,
)

logger = logging.getLogger(__name__)


class PlaybookIntegrityError(Exception):
    """Playbook checksum/size/header mismatch — non-recoverable.

    Raised in non-dev mode by _verify_playbook_integrity(). In dev mode,
    mismatches are reported via print() and execution continues.
    """
    pass


# ── Default stage sequences per engine type ──────────────────────────

DEFAULT_STAGE_CHAINS = {
        QR_ENGINE_LLAMA_SERVER_NAME: [
            {"stage": "preflight",   "playbook": "preflight_check"},
            {"stage": "deps",        "playbook": "install_deps"},
            {"stage": "source",      "playbook": "source_llama"},
            {"stage": "compile",     "playbook": "build_compile_llama"},
            {"stage": "config_svc",  "playbook": "deploy_config_service"},
            {"stage": "config_env",  "playbook": "deploy_config_env"},
            {"stage": "start",       "playbook": "service_start"},
        ],
       QR_ENGINE_LLAMA_RPC_NAME: [
            {"stage": "preflight",   "playbook": "preflight_check"},
            {"stage": "deps",        "playbook": "install_deps"},
            {"stage": "source",      "playbook": "source_llama"},
            {"stage": "compile",     "playbook": "build_compile_llama"},
            {"stage": "config_svc",  "playbook": "deploy_config_service"},
            {"stage": "config_env",  "playbook": "deploy_config_env"},
            {"stage": "start",       "playbook": "service_start"},
        ],
        QR_ENGINE_IPERF3_NAME: [
            {"stage": "preflight",   "playbook": "preflight_check"},
            {"stage": "deps",        "playbook": "install_deps"},
            {"stage": "config_svc",  "playbook": "deploy_config_service"},
            {"stage": "config_env",  "playbook": "deploy_config_env"},
            {"stage": "start",       "playbook": "service_start"},
        ],
        QR_ENGINE_UNIVERSAL_NAME: [
            {"stage": "preflight",   "playbook": "preflight_check"},
            {"stage": "deps",        "playbook": "install_deps"},
            {"stage": "config_svc",  "playbook": "deploy_config_service"},
            {"stage": "config_env",  "playbook": "deploy_config_env"},
            {"stage": "start",       "playbook": "service_start"},
      ],
        # Fast deploy — config_svc + config_env + start only (no source/compile)
        # Used for new instances when skip_build=True: still deploys service files,
        # assumes binary already exists or will be provided separately.
        "deploy_fast": {
            QR_ENGINE_LLAMA_SERVER_NAME: [
                {"stage": "config_svc",  "playbook": "deploy_config_service"},
                {"stage": "config_env",  "playbook": "deploy_config_env"},
                {"stage": "start",       "playbook": "service_start"},
            ],
            QR_ENGINE_LLAMA_RPC_NAME: [
                {"stage": "config_svc",  "playbook": "deploy_config_service"},
                {"stage": "config_env",  "playbook": "deploy_config_env"},
                {"stage": "start",       "playbook": "service_start"},
            ],
            QR_ENGINE_IPERF3_NAME: [
                {"stage": "config_svc",  "playbook": "deploy_config_service"},
                {"stage": "config_env",  "playbook": "deploy_config_env"},
                {"stage": "start",       "playbook": "service_start"},
            ],
            QR_ENGINE_UNIVERSAL_NAME: [
                {"stage": "config_svc",  "playbook": "deploy_config_service"},
                {"stage": "config_env",  "playbook": "deploy_config_env"},
                {"stage": "start",       "playbook": "service_start"},
            ],
        },
}



class PlaybookRunner:
    """Staged playbook execution orchestrator.

    Creates jobs and tasks in the DB, executes playbooks via
    lib_ansible_runner, parses results, and updates instance state.

    Args:
        db_path: Path to the SQLite database.
        playbook_dir: Base directory for playbooks (default: "playbooks/").
    """

    def __init__(self, db_path, playbook_dir="playbooks/"):
        self.db_path = db_path
        self.playbook_dir = playbook_dir.rstrip("/") + "/"

    # ── Job & Task Creation ────────────────────────────────────────

    def create_deploy_job(self, instance_id, job_type="deploy", priority=5, actor="api"):
        """Create a deploy job for an instance; return (job, tasks) tuple.

        Args:
            instance_id: Instance to deploy.
            job_type: Type of operation (deploy, rebuild, etc.).
            priority: Scheduler priority (1=highest).
            actor: Who triggered this (api, agent, system).

        Returns:
            Tuple of (job_dict, list_of_task_dicts).
        """
        from db.sqlite import pool
        from db.adapters.instances import get_instance

        inst = get_instance(self.db_path, instance_id)
        if not inst:
            raise ValueError(f"Instance {instance_id} not found")

        engine_name = inst.get("engine_type_name", "")
        stages = self._get_stage_chain(engine_name, job_type, inst)

        with pool(self.db_path) as conn:
            # Create the parent job
            cursor = conn.execute(
                """INSERT INTO jobs
                   (instance_id, job_type, engine_type_name, priority, status, actor)
                   VALUES (?, ?, ?, ?, 'queued', ?)""",
                (instance_id, job_type, engine_name, priority, actor),
            )
            job_id = cursor.lastrowid

            # Create one task per stage
            tasks = []
            for s in stages:
                # service_start has no retry value — if systemd can't start the
                # service (wrong path, bad CLI args, missing env), retrying is
                # pointless and just wastes time / creates noise.
                _max_retries = 0 if s["playbook"] == "service_start" else None
                if _max_retries is not None:
                    cur = conn.execute(
                        """INSERT INTO tasks
                           (job_id, instance_id, stage, playbook, status, max_retries)
                           VALUES (?, ?, ?, ?, 'queued', ?)""",
                        (job_id, instance_id, s["stage"], s["playbook"], _max_retries),
                    )
                else:
                    cur = conn.execute(
                        """INSERT INTO tasks
                           (job_id, instance_id, stage, playbook, status)
                           VALUES (?, ?, ?, ?, 'queued')""",
                        (job_id, instance_id, s["stage"], s["playbook"]),
                    )
                task_id = cur.lastrowid
                tasks.append({
                    "id": task_id,
                    "job_id": job_id,
                    "instance_id": instance_id,
                    "stage": s["stage"],
                    "playbook": s["playbook"],
                    "status": "queued",
                })

            conn.execute(
                "UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id = ?", (job_id,)
            )
            conn.commit()

        return {"id": job_id, "job_type": job_type, "status": "queued", "stage_count": len(tasks)}, tasks

    def create_health_check_job(self, instance_id):
        """Create a recurring health check job."""
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            # Check for existing enabled health check
            existing = conn.execute(
                "SELECT id FROM jobs WHERE instance_id=? AND job_type='health_check' AND disabled=0",
                (instance_id,),
            ).fetchone()
            if existing:
                return None  # Already exists

            cursor = conn.execute(
                """INSERT INTO jobs
                   (instance_id, job_type, priority, status, recurrence_interval, next_run_at)
                   VALUES (?, 'health_check', 10, 'queued', 30, datetime('now'))""",
                (instance_id,),
            )
            job_id = cursor.lastrowid

            conn.execute(
                "INSERT INTO tasks (job_id, instance_id, stage, playbook, status) "
                "VALUES (?, ?, 'health_probe', 'playbooks/core/service_start.yml', 'queued')",
                (job_id, instance_id),
            )
            conn.commit()

            return {"id": job_id, "job_type": "health_check", "status": "queued"}

    def cancel_job(self, job_id):
        """Cancel all tasks in a job."""
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status='cancelled', updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE job_id=? AND status IN ('queued','running')", (job_id,)
            )
            conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id=?", (job_id,)
            )
            conn.commit()

    # ── Stage Execution ────────────────────────────────────────────

    def execute_task(self, task_id):
        """Execute a single task's playbook. Sets up → running → completed/failed.

        This is the full synchronous version used by chain(). It blocks until
        the ansible playbook completes (5-30 min for compiles).

        For async/scheduler use, call execute_task_phase1() then let a background
        thread run execute_task_phase2().

        Args:
            task_id: Primary key of the task to execute.

        Returns:
            dict with keys: success (bool), error (str|None), duration_ms (int).
        """
        # Phase 1: DB setup
        setup_result = self.execute_task_phase1(task_id)
        if not setup_result["ok"]:
            return {"success": False, "error": setup_result["error"], "duration_ms": 0}

        task = setup_result["task"]
        instance_id = task["instance_id"]
        playbook_path = setup_result["playbook_path"]
        stage = task["stage"]
        node_hostname = setup_result["node_hostname"]
        extra_vars = setup_result["extra_vars"]
        inst = setup_result["instance"]

        # Phase 2: Run ansible + finalization (blocking)
        # Integrity check is in _run_task_playbook — no duplicate here.
        result = self._run_task_playbook(task_id, task["job_id"], instance_id,
                                         playbook_path, stage, node_hostname,
                                         extra_vars, inst, task)
        return result

    def execute_task_phase1(self, task_id):
        """Phase 1: DB setup for task execution.

        Gets task, validates it, computes extra vars, updates status to 'running'.
        Returns immediately — does NOT run ansible. Used by async scheduler path.

        Args:
            task_id: Primary key of the task to execute.

        Returns:
            dict with keys:
                ok (bool): True if phase 1 succeeded
                task (dict|None): Task record
                instance (dict|None): Instance record
                playbook_path (str|None): Resolved playbook path
                node_hostname (str|None): Target hostname
                extra_vars (dict|None): Ansible extra vars
                error (str|None): Error message if ok=False
        """
        from db.sqlite import pool
        from lib.lib_ansible_runner import run_playbook, parse_ansible_json

        task = self._get_task(task_id)
        if not task or task["status"] != "queued":
            return {
                "ok": False,
                "error": f"Task {task_id} is {task['status'] if task else 'missing'}",
            }

        instance_id = task["instance_id"]
        playbook_path = self._resolve_playbook(task["playbook"])
        stage = task["stage"]

        # Integrity check: verify playbook checksum + size against DB
        # Lookup MUST succeed with valid data — a failed lookup is a hard failure, not a silent skip.
        playbook_ref = task.get("playbook", "")
        with pool(self.db_path) as conn:
            row = conn.execute(
                "SELECT checksum_sha256, file_size FROM playbook_registry WHERE playbook_id = ?",
                (playbook_ref,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT checksum_sha256, file_size FROM playbook_registry WHERE file_path = ?",
                    (playbook_ref,),
                ).fetchone()
        if not row or not row["checksum_sha256"] or not row["file_size"]:
            raise PlaybookIntegrityError(
                f"Playbook registry lookup failed for '{playbook_ref}': "
                f"{'row not found' if not row else 'missing checksum or file_size'}"
            )
        expected_hash = row["checksum_sha256"]
        expected_size = row["file_size"]
        self._verify_playbook_integrity(playbook_path, expected_hash, expected_size)

        # Gather instance info for extra vars
        inst = get_instance(self.db_path, instance_id)
        if not inst:
            return {"ok": False, "error": f"Instance {instance_id} not found"}

        node_hostname = inst.get("node_hostname", "")
        if not node_hostname:
            node_hostname = inst.get("ipv4_address", "") or ""

        # Compute merged env/cli_opts for config/start stages
        engine_type_name = inst.get("engine_type_name", "")
        merged_cli_opts = None
        merged_env = None
        if engine_type_name in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME):
            try:
                if engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME:
                    from lib.lib_cluster_env_builder import (
                        build_llama_server_env as _builder,
                    )
                    result = _builder(self.db_path, instance_id)
                    merged_cli_opts = result.get("cli_args")
                    merged_env = result.get("env")
                else:
                    from lib.lib_cluster_env_builder import (
                        build_rpc_server_env as _builder,
                    )
                    result = _builder(self.db_path, instance_id)
                    merged_cli_opts = result.get("cli_args")
                    merged_env = result.get("env")
            except Exception as exc:
                logger.warning("[qr-runner] Env builder failed for instance %d: %s", instance_id, exc)

        extra_vars = self._build_extra_vars(inst, stage, merged_cli_opts, merged_env, task)

        # Update task to running and job to running (first task)
        with pool(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status='running', started_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?", (task_id,)
            )
            conn.execute(
                "UPDATE jobs SET status='running', started_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                "WHERE id=? AND status='queued'", (task["job_id"],)
            )
            state = STAGE_STATE_MAP.get(stage, "configuring")
            conn.execute(
                "UPDATE instances SET state=?, last_state_change=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                (state, task["instance_id"]),
            )
            conn.commit()

        return {
            "ok": True,
            "task": task,
            "instance": inst,
            "playbook_path": playbook_path,
            "node_hostname": node_hostname,
            "extra_vars": extra_vars,
        }

    def _run_task_playbook(self, task_id, job_id, instance_id, playbook_path, stage,
                           node_hostname, extra_vars, inst, task):
        """Phase 2: Run ansible playbook and update DB with results.

        This is the blocking part — runs ansible (5-30 min for compiles).
        Should be called from a background thread in async mode.

        Args:
            task_id, job_id, instance_id, playbook_path, stage, node_hostname,
            extra_vars, inst, task: Pre-computed values from phase 1.

        Returns:
            dict with keys: success (bool), error (str|None), duration_ms (int).
        """
        from db.sqlite import pool
        from lib.lib_ansible_runner import run_playbook, parse_ansible_json

        # Integrity check: verify playbook checksum + size against DB.
        # Placed here (not in execute_task_phase1) so it runs for both scheduler async
        # and sync chain paths. A failed lookup is a hard failure — not a silent skip.
        playbook_ref = task.get("playbook", "")
        with pool(self.db_path) as conn:
            row = conn.execute(
                "SELECT checksum_sha256, file_size FROM playbook_registry WHERE playbook_id = ?",
                (playbook_ref,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT checksum_sha256, file_size FROM playbook_registry WHERE file_path = ?",
                    (playbook_ref,),
                ).fetchone()
        if not row or not row["checksum_sha256"] or not row["file_size"]:
            raise PlaybookIntegrityError(
                f"Playbook registry lookup failed for '{playbook_ref}': "
                f"{'row not found' if not row else 'missing checksum or file_size'}"
            )
        expected_hash = row["checksum_sha256"]
        expected_size = row["file_size"]
        self._verify_playbook_integrity(playbook_path, expected_hash, expected_size)

        start_time = time.time()
        success = False
        error_msg = None

        # Per-node build lock: only hold during compile stage (shared cmake build per node)
        from qr_api.lib_instances import get_node_build_lock
        build_lock = None
        if stage == QR_STAGE_COMPILE and inst.get("node_id"):
            build_lock = get_node_build_lock(inst["node_id"])

        try:
            if build_lock is not None:
                build_lock.acquire(timeout=300)
            result = run_playbook(
                playbook_path,
                limit=node_hostname,
                extra_vars=extra_vars,
                timeout=self._get_stage_timeout(stage, playbook_path),
            )

            if not result.get("failed", False):
                success = True
                logger.info("[qr-runner] Task %d (%s) completed on %s", task_id, stage, node_hostname)
            else:
                error_msg = self._extract_error(result)
                logger.warning("[qr-runner] Task %d (%s) failed on %s: %s", task_id, stage, node_hostname, error_msg)

        except TimeoutError as exc:
            error_msg = f"Stage {stage} timed out: {exc}"
            logger.error("[qr-runner] %s", error_msg)
        except RuntimeError as exc:
            error_msg = f"Stage {stage} error: {exc}"
            logger.error("[qr-runner] %s", error_msg)
        except Exception as exc:
            error_msg = f"Stage {stage} unexpected error: {exc}"
            logger.exception("[qr-runner] %s", error_msg)
        finally:
            if build_lock is not None and build_lock.locked():
                build_lock.release()

        # Save playbook output to playbook_runs table for audit/debugging
        try:
            output_json = json.dumps(result) if isinstance(result, dict) else str(result)
            with pool(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO playbook_runs (task_id, output) VALUES (?, ?)",
                    (task_id, output_json),
                )
                conn.commit()
        except Exception:
            pass

        # Update playbook_registry counters + log ansible_actions for runner chain
        try:
            from db.adapters.playbooks import (
                increment_usage_counter, increment_error_counter,
            )
            playbook_id = task.get("playbook")
            if playbook_id:
                if success:
                    increment_usage_counter(self.db_path, playbook_id)
                else:
                    increment_error_counter(self.db_path, playbook_id)
        except Exception:
            pass  # Non-critical — counters shouldn't break job lifecycle

        # Log to ansible_actions for WebUI Ansible Logs tab integration
        try:
            from lib.lib_ansible_runner import log_ansible_action as _log_aa
            _stage = task.get("stage", "")
            # Map stage names to ansible_actions.action_type (must match CHECK constraint).
            # Uses existing allowed types: validate_node, deploy_instance, undeploy_instance,
            # restart_instance, stop_instance, config_change, update_and_compile,
            # rpc_health_check, get_logs, apt_update, ansible_execute
            _action_map = {
                QR_STAGE_PREFLIGHT:  "validate_node",
                QR_STAGE_DEPS:       "apt_update",
                QR_STAGE_SOURCE:     "ansible_execute",
                QR_STAGE_COMPILE:    "update_and_compile",
                QR_STAGE_CONFIG_SVC: "config_change",
                QR_STAGE_CONFIG_ENV: "config_change",
                QR_STAGE_START:      "restart_instance",
                QR_STAGE_STOP:       "stop_instance",
                QR_STAGE_HEALTH_PROBE: "rpc_health_check",
            }
            _action_map["undeploy"] = "undeploy_instance"
            _action_map["verify"]   = "get_logs"
            action_type = _action_map.get(_stage)
            if not action_type:
                # Fallback for unknown stages — try common prefix mapping
                _fallback = {
                    "preflight": "validate_node", "deps": "apt_update",
                    "source": "ansible_execute", "compile": "update_and_compile",
                    "config_svc": "config_change", "config_env": "config_change",
                    "start": "restart_instance", "stop": "stop_instance",
                }
                action_type = _fallback.get(_stage, "ansible_execute")
            node_id = inst.get("node_id") if inst else None
            _log_aa(
                self.db_path, action_type, node_id, instance_id,
                playbook_id or "", extra_vars or {}, result,
            )
        except Exception as _log_exc:
            logger.warning("[qr-runner] ansible_actions logging failed for task %d (%s): %s",
                           task_id, _stage, _log_exc)

        duration_ms = int((time.time() - start_time) * 1000)
        new_status = "completed" if success else "failed"
        err_val = error_msg if not success and error_msg else None

        # Update task + job status
        with pool(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status=?, error_message=?, finished_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                (new_status, err_val, task_id),
            )

            if not success and error_msg:
                conn.execute(
                    "INSERT INTO qr_actions (action_type, instance_id, actor, details, status, created_at) "
                    "VALUES ('runner_task_failed', ?, 'scheduler', ?, 'failed', strftime('%Y-%m-%dT%H:%M:%S','now'))",
                    (instance_id, json.dumps({"task_id": task_id, "stage": stage, "error": error_msg})),
                )

            if success:
                self._advance_job_to_next(conn, task_id)
            else:
                conn.execute(
                    "UPDATE tasks SET retry_count=retry_count+1 WHERE id=?", (task_id,)
                )
                row = conn.execute("SELECT retry_count, max_retries FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row and row["retry_count"] < row["max_retries"]:
                    conn.execute(
                        "UPDATE tasks SET status='queued', started_at=NULL, finished_at=NULL, "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?", (task_id,)
                    )
                    logger.info("[qr-runner] Task %d (%s) requeued (retry %d/%d)", task_id, stage, row["retry_count"], row["max_retries"])
                else:
                    conn.execute(
                        "UPDATE jobs SET status='failed', error_message=?, finished_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                        (f"Task '{stage}' failed after max retries", job_id),
                    )
                    conn.execute(
                        "UPDATE tasks SET status='cancelled', updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                        "WHERE job_id=? AND status IN ('queued','running')", (job_id,)
                    )
                    conn.execute(
                        "UPDATE instances SET state='error', last_state_change=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                        (instance_id,),
                    )

            conn.commit()

        return {"success": success, "error": error_msg, "duration_ms": duration_ms}

    def complete_job(self, job_id, conn=None):
        """Finalize a completed job and update instance state.

        Args:
            job_id: Primary key of the completed job.
            conn: Optional existing DB connection to reuse. If provided,
                  the caller is responsible for committing/rolling back.
                  When None, opens its own connection (backward compat).
        """
        from db.sqlite import pool

        owns_conn = conn is None
        if owns_conn:
            with pool(self.db_path) as conn:
                self._finalize_job(conn, job_id)
        else:
            self._finalize_job(conn, job_id)

    def _finalize_job(self, conn, job_id):
        """Core job finalization logic — runs within an active connection.

        Args:
            conn: Active DB connection (caller-created or passed-through).
            job_id: Primary key of the completed job.
        """
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return
        instance_id = job["instance_id"]
        job_type = job["job_type"]

        # Capture pre-operation state (needed for bind/unbind which only update env)
        pre_state = conn.execute(
            "SELECT state FROM instances WHERE id=?", (instance_id,)
        ).fetchone()["state"]

        # Engine-aware final state: RPC has no SSE model-load endpoint,
        # so start/restart should go straight to "running" instead of "loading".
        instance_info = conn.execute(
            "SELECT i.engine_type_id, e.name FROM instances i "
            "JOIN engine_types e ON i.engine_type_id = e.id WHERE i.id=?", (instance_id,)
        ).fetchone()
        engine_type_name = instance_info[1] if instance_info else ""

        # Set instance state based on job type — SSOT lookup from JOB_FINAL_STATES
        # bind/unbind are not in the dict — they preserve the pre-operation state
        if job_type in (QR_JOB_BIND, QR_JOB_UNBIND):
            new_state = pre_state
        else:
            if job_type not in JOB_FINAL_STATES:
                print(f"[qr] DEBUG: Unknown job_type '{job_type}' for instance {instance_id} — expected one of {list(JOB_FINAL_STATES.keys())}")
                raise ValueError(f"Unknown job_type '{job_type}' not in JOB_FINAL_STATES")
            new_state = JOB_FINAL_STATES[job_type]
            # RPC: no /models/sse endpoint → no loading state needed
            if job_type in (QR_JOB_START, QR_JOB_RESTART) and engine_type_name == QR_ENGINE_LLAMA_RPC_NAME:
                new_state = "running"
            # llama_server start/restart: if instance was already running or loading,
            # keep it in its current state instead of forcing intermediate "loading".
            # This prevents instances from getting stuck in "loading" when no WebUI SSE
            # client is connected to monitor model load progress.
            elif engine_type_name == QR_ENGINE_LLAMA_SERVER_NAME and job_type in (QR_JOB_START, QR_JOB_RESTART):
                if pre_state in ("running", "starting", "deploying", "configuring"):
                    new_state = pre_state

        conn.execute(
            "UPDATE instances SET state=?, last_state_change=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
            (new_state, instance_id),
        )
        conn.execute(
            "UPDATE jobs SET status='completed', finished_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?", (job_id,)
        )

        # Handle recurring jobs
        if job["recurrence_interval"] and job["recurrence_interval"] > 0:
            conn.execute(
                "UPDATE jobs SET next_run_at=datetime('now', '+' || ? || ' seconds'), "
                "status='scheduled', updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                (str(job["recurrence_interval"]), job_id),
            )

        # Extract build_number from source stage output (deploy + rebuild)
        if job_type in (QR_JOB_DEPLOY, QR_JOB_REBUILD):
            try:
                source_run = conn.execute(
                    "SELECT output FROM playbook_runs pr JOIN tasks t ON t.id=pr.task_id "
                    "WHERE t.job_id=? AND t.stage='source' ORDER BY pr.id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if source_run and source_run["output"]:
                    import re as _re
                    bm = _re.search(r'commit=([a-f0-9]{7})', str(source_run["output"]))
                    if bm:
                        conn.execute(
                            "UPDATE instances SET build_number=? WHERE id=?",
                            (bm.group(1), instance_id),
                        )
            except Exception:
                pass  # Non-critical

    # ── Sync Chain (API Route Integration) ─────────────────────────

    def chain(self, instance_id, job_type="deploy", actor="api", skip_build=False, async_mode=False):
        """Execute full stage chain synchronously for API route.

        Creates a deploy job, executes all tasks sequentially, collects
        results and warnings, and returns a response matching the current
        api_deploy_instance() output format for WebUI compatibility.

        Args:
            instance_id: Instance primary key.
            job_type: Type of operation (deploy, rebuild).
            actor: Who triggered this (api, agent, system).
            skip_build: If True, skip source+compile stages (build lock not acquired).
            async_mode: If True, create job + tasks and return immediately without
                        executing tasks. Scheduler picks up tasks within next poll cycle.

        Returns:
            dict matching api_deploy_instance response shape:
                {"success": bool, "message": str, "job_id": int,
                 "task_ids": list[int], "warnings": list,
                 "uuid_mismatches": list, "duration_ms": int}
        """
        from db.adapters.instances import get_instance as _gi, check_system_managed as _csm
        from db.sqlite import pool

        start_time = time.time()
        result = {"success": True, "message": "", "job_id": None, "task_ids": [],
                  "warnings": [], "uuid_mismatches": None, "duration_ms": 0}

        # Validate instance
        inst = _gi(self.db_path, instance_id)
        if not inst:
            result["success"] = False
            result["message"] = f"Instance {instance_id} not found"
            return result

        engine_name = inst.get("engine_type_name", "")
        node_hostname = inst.get("node_hostname", "") or (
            inst.get("ipv4_address", "") or ""
        )
        # RPC binding warnings for llama_server (same as api_deploy_instance)
        rpc_warnings = []
        if engine_name == QR_ENGINE_LLAMA_SERVER_NAME and inst.get("rpc_bind_ids"):

            try:
                from lib.lib_cluster_env_builder import rpc_binding_warnings as _rbw
                rpc_warnings = _rbw(self.db_path, instance_id)
            except Exception:
                pass

        result["warnings"] = rpc_warnings

        # UUID preflight — run ad-hoc check + build uuid_map for preflight playbook
        self._current_uuid_map = {}  # Instance-level context for playbook extra_vars
        uuid_mismatches = None
        try:
            from qr_api.lib_instances import check_remote_uuids as _check_uuids
            uuid_check = _check_uuids(self.db_path, instance_id)
            if uuid_check.get("mismatches"):
                uuid_mismatches = uuid_check["mismatches"]
            # Build uuid_map for preflight.yml: {unit_key: expected_uuid}
            with pool(self.db_path) as conn:
                for row in conn.execute(
                    "SELECT i.id, i.instance_uuid, e.name as engine_type_name "
                    "FROM instances i JOIN engine_types e ON i.engine_type_id = e.id "
                    "WHERE i.node_id = ?", (inst.get("node_id"),),
                ):
                    unit_key = f"qr-{row['id']}-{row['engine_type_name']}"
                    self._current_uuid_map[unit_key] = row["instance_uuid"]
        except Exception:
            pass  # Non-critical — proceed regardless

        # RUNNER-EIO-1: Verify scheduler is alive before pipeline work.
        # A stale/dead scheduler can corrupt pipe/FD state → EIO on subprocess.run().
        try:
            from db.adapters.instances import get_instance as _sgi
            import psutil as _psutil
            sched_inst = _sgi(self.db_path, 4)  # scheduler instance ID is always 4
            sched_pid = sched_inst.get("pid_last_known") if sched_inst else None
            if sched_pid and not _psutil.pid_exists(sched_pid):
                # Scheduler PID stale — attempt auto-restart via lib_system_engine
                try:
                    from lib.lib_system_engine import start_system_engine
                    from qr_api import _CONFIG
                    env_cfg = {}  # Minimal env config for scheduler restart
                    start_system_engine("scheduler", env_cfg,
                                        _CONFIG.get("host", "127.0.0.1"),
                                        _CONFIG.get("api_port") or QR_ENGINE_PORT_DEFAULTS["quickrobot-api"])
                    print(f"[qr] Stale scheduler PID ({sched_pid}) detected, auto-restarted")
                except Exception as _re:
                    print(f"[qr] Warning: stale scheduler restart failed: {_re}")
        except ImportError:
            pass  # psutil not available — skip check
        except Exception:
            pass  # Non-critical — proceed regardless

        # Async mode: create job + tasks, return immediately. Scheduler picks up.
        if async_mode:
            job, tasks = self.create_deploy_job(instance_id, job_type, priority=5, actor=actor)
            result["job_id"] = job["id"]
            result["tasks_created"] = len(tasks)
            result["task_ids"] = [t["id"] for t in tasks]
            result["uuid_mismatches"] = uuid_mismatches
            result["message"] = f"Job {job['id']} queued (async)"
            result["duration_ms"] = int((time.time() - start_time) * 1000)
            return result

        try:
            # Create the deploy job + tasks
            job, tasks = self.create_deploy_job(instance_id, job_type, priority=5, actor=actor)

            result["job_id"] = job["id"]
            task_ids = []

            for task in tasks:
                task_ids.append(task["id"])
                try:
                    task_result = self.execute_task(task["id"])
                except FileNotFoundError as exc:
                    logger.error("[qr-runner] Task %d (%s) FAILED — playbook missing: %s",
                                 task["id"], task["stage"], exc)
                    task_result = {"success": False, "error": str(exc), "duration_ms": 0}
                except PlaybookIntegrityError as exc:
                    logger.error("[qr-runner] Task %d (%s) FAILED — playbook integrity: %s",
                                 task["id"], task["stage"], exc)
                    task_result = {"success": False, "error": str(exc), "duration_ms": 0}

                if not task_result["success"]:
                    result["success"] = False
                    result["message"] = f"Stage '{task['stage']}' failed: {task_result.get('error', '')}"
                    break

            # Apply JOB_FINAL_STATES override via _finalize_job
            # This ensures start/restart jobs transition to "loading", stop→"stopped", etc.
            if result["success"]:
                try:
                    self.complete_job(job["id"])
                except Exception as _fe:
                    logger.warning("[qr-runner] finalize_job failed after success: %s", _fe)
            # Build response
            duration_ms = int((time.time() - start_time) * 1000)
            result["task_ids"] = task_ids
            result["duration_ms"] = duration_ms
            result["uuid_mismatches"] = uuid_mismatches
            if result["success"]:
                result["message"] = f"Instance {instance_id} deployed via staged chain " \
                                    f"({len(task_ids)} stages in {duration_ms}ms)"
            else:
                if not result["message"]:
                    result["message"] = f"Deploy failed after {duration_ms}ms"

        finally:
            # Clean up uuid_map context to prevent leakage between chain calls
            if hasattr(self, "_current_uuid_map"):
                del self._current_uuid_map

        return result

    # ── Query Helpers ──────────────────────────────────────────────

    def get_instance_jobs(self, instance_id, status=None):
        """Get jobs for an instance, optionally filtered by status.

        Args:
            instance_id: Instance to query.
            status: Filter by status (queued, running, completed, etc.).

        Returns:
            List of job dicts.
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE instance_id=? AND status=? ORDER BY created_at DESC",
                    (instance_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE instance_id=? ORDER BY created_at DESC LIMIT 10",
                    (instance_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_job_tasks(self, job_id):
        """Get all tasks for a job.

        Args:
            job_id: Job to query.

        Returns:
            List of task dicts.
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE job_id=? ORDER BY stage", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_next_queued_task(self):
        """Get the next queued task across all jobs, by host + priority.

        Interlock is per-host (not global or per-instance): tasks on different
        hosts can run in parallel, but only one task per host at a time.
        This prevents concurrent compiles on shared-cmake hosts while allowing
        full parallelism across independent nodes.

        Sorting: by node_id ASC first (round-robin across hosts), then
        created_at ASC within each host. This interleaves tasks across hosts
        so each host gets a turn before the scheduler cycles back.

        Returns:
            Task dict or None if no tasks are queued.
        """
        from db.sqlite import pool

        # REG-03-F1 Part 2: Atomic task claim via BEGIN IMMEDIATE.
        # BEGIN IMMEDIATE acquires a reserved lock before any read, ensuring
        # only one scheduler can claim the task at a time. Without this, two
        # schedulers can SELECT the same 'queued' row before either UPDATEs it.
        with pool(self.db_path) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT t.id, t.job_id, t.instance_id, t.stage, t.playbook,
                              t.status, t.error_message, t.started_at, t.finished_at,
                              t.retry_count, t.max_retries, t.created_at, t.updated_at,
                              j.priority, i.node_id
                       FROM tasks t
                       JOIN jobs j ON t.job_id = j.id
                       JOIN instances i ON t.instance_id = i.id
                       WHERE t.status = 'queued'
                         AND NOT EXISTS (
                             SELECT 1 FROM tasks t2
                             JOIN jobs j2 ON t2.job_id = j2.id
                             JOIN instances i2 ON t2.instance_id = i2.id
                             WHERE t2.status = 'running'
                               AND i2.node_id = i.node_id
                         )
                       ORDER BY i.node_id ASC, t.created_at ASC
                       LIMIT 1"""
                ).fetchone()
                return dict(row) if row else None
            except Exception:
                conn.rollback()
                raise

    def list_jobs(self, status=None, engine_type=None, node_id=None):
        """List jobs with optional filters.

        Args:
            status: Filter by job status (queued, running, completed, failed, etc.).
            engine_type: Filter by engine type name (e.g., 'llama_server').
            node_id: Filter by node ID.

        Returns:
            List of job dicts with node_hostname included.
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            query = ("SELECT j.*, i.node_id, "
                     "n.name AS node_name, n.hostname AS node_hostname "
                     "FROM jobs j "
                     "LEFT JOIN instances i ON j.instance_id = i.id "
                     "LEFT JOIN nodes n ON i.node_id = n.id")
            conditions = []
            params = []

            if status:
                conditions.append("j.status = ?")
                params.append(status)
            if engine_type:
                conditions.append("j.engine_type_name = ?")
                params.append(engine_type)
            if node_id:
                conditions.append("i.node_id = ?")
                params.append(node_id)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY j.created_at DESC LIMIT 100"

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_job_with_task_ids(self, job_id):
        """Get job details with list of task IDs (not full task objects).

        Args:
            job_id: Job primary key.

        Returns:
            Dict with 'job' (job data) and 'tasks' (list of task IDs).
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return None
            tasks = conn.execute(
                "SELECT id FROM tasks WHERE job_id=? ORDER BY stage", (job_id,)
            ).fetchall()
            return {"job": dict(job), "tasks": [t["id"] for t in tasks]}

    def list_tasks(self, status=None, job_id=None, instance_id=None):
        """List tasks with optional filters.

        Args:
            status: Filter by task status (queued, running, completed, failed, etc.).
            job_id: Filter by parent job ID.
            instance_id: Filter by instance ID.

        Returns:
            List of task dicts.
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            query = "SELECT t.* FROM tasks t"
            conditions = []
            params = []

            if status:
                conditions.append("t.status = ?")
                params.append(status)
            if job_id:
                conditions.append("t.job_id = ?")
                params.append(job_id)
            if instance_id:
                conditions.append("t.instance_id = ?")
                params.append(instance_id)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY t.created_at DESC LIMIT 200"

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_task_detail(self, task_id):
        """Get full task detail including playbook_runs output.

        Args:
            task_id: Task primary key.

        Returns:
            Dict with 'task' (task data) and 'playbook_output' (raw ansible JSON if available).
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return None
            playbook_output = None
            row = conn.execute(
                "SELECT output FROM playbook_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            if row and row["output"]:
                import json
                try:
                    playbook_output = json.loads(row["output"])
                except (json.JSONDecodeError, TypeError):
                    playbook_output = {"raw": row["output"][:2000]}
            return {"task": dict(task), "playbook_output": playbook_output}

    # ── Internal Helpers ───────────────────────────────────────────

    def _get_stage_chain(self, engine_name, job_type, instance):
        """Get the stage chain for an engine type + job type.

        Args:
            engine_name: Engine type name (e.g., 'llama_server').
            job_type: Job type (deploy, rebuild, etc.).
            instance: Instance dict from DB.

        Returns:
            List of stage dicts: [{stage, playbook}, ...].

        Raises:
            ValueError: If engine_name has no registered stage chain and a full deploy is requested.
        """
        if engine_name not in DEFAULT_STAGE_CHAINS:
            if job_type == QR_JOB_DEPLOY:
                raise ValueError(
                    f"Engine '{engine_name}' has no registered stage chain. "
                    f"Known engines: {list(DEFAULT_STAGE_CHAINS.keys())}. "
                    f"Add an entry to DEFAULT_STAGE_CHAINS before deploying."
                )
            # For rebuild, reconfigure, undeploy — empty or minimal is acceptable
            if job_type == QR_JOB_REBUILD:
                return []
            if job_type == QR_JOB_RECONFIGURE:
                return []
            if job_type == QR_JOB_START:
                return [{"stage": "start", "playbook": "service_start"}]
            if job_type == QR_JOB_RESTART:
                return [{"stage": "stop", "playbook": "service_stop"},
                        {"stage": "start", "playbook": "service_start"}]
            if job_type == QR_JOB_STOP:
                return [{"stage": "stop", "playbook": "service_stop"}]
            # undeploy below handles its own chain

        if job_type == QR_JOB_REBUILD:
            # Pre-flight check + source pull + compile → config rewrite (no restart)
            return [
                s for s in DEFAULT_STAGE_CHAINS[engine_name]
                if s["stage"] != QR_STAGE_DEPS
            ]

        if job_type == QR_JOB_RECONFIGURE:
            # Config-only env update + service restart (no sudo, no service unit regen)
            # Replaces legacy api_reconfigure_instance() direct playbook call with RUNNER-1 chain.
            # Includes start stage so new env values are picked up by the running service.
            return [
                s for s in DEFAULT_STAGE_CHAINS[engine_name]
                if s["stage"] in (QR_STAGE_CONFIG_ENV, QR_STAGE_START)
            ]

        if job_type == QR_JOB_DEPLOY_FAST:
            # Fast deploy: config_svc + config_env + start (no source/compile)
            # Used for new instances with skip_build=True when binary exists.
            return DEFAULT_STAGE_CHAINS["deploy_fast"].get(engine_name, [])

        if job_type == QR_JOB_UNDEPLOY:
            # Engine-specific undeploy chain: stop → engine-undeploy → verify
            return _QR_UNDEPLOY_CHAINS.get(engine_name, [])

        if job_type == QR_JOB_BIND:
            # Bind RPC: rewrite env file with new RPC bindings (no systemd changes)
            return [s for s in DEFAULT_STAGE_CHAINS[engine_name] if s["stage"] == QR_STAGE_CONFIG_ENV]

        if job_type == QR_JOB_UNBIND:
            # Unbind RPC: rewrite env file to remove RPC bindings (no systemd changes)
            return [s for s in DEFAULT_STAGE_CHAINS[engine_name] if s["stage"] == QR_STAGE_CONFIG_ENV]

        if job_type == QR_JOB_START:
            # Simple start: only start the systemd unit, no build/config
            return [{"stage": "start", "playbook": "service_start"}]

        if job_type == QR_JOB_RESTART:
            # Restart: stop → start (no RPC health probes)
            return [{"stage": "stop", "playbook": "service_stop"},
                    {"stage": "start", "playbook": "service_start"}]

        if job_type == QR_JOB_STOP:
            # Stop: just stop the systemd service
            return [{"stage": "stop", "playbook": "service_stop"}]

        if job_type == QR_JOB_DEPLOY:
            # Full deploy — use registered chain
            return DEFAULT_STAGE_CHAINS[engine_name]

        if job_type == QR_JOB_REBOOT:
            # Async fire-and-forget reboot — returns immediately
            return [{"stage": "reboot", "playbook": "reboot_node"}]

        if job_type == QR_JOB_APT_UPDATE:
            # Node-level apt update
            return [{"stage": "apt_update", "playbook": "apt_update"}]

        if job_type == QR_JOB_APT_UPGRADE:
            # Node-level apt upgrade
            return [{"stage": "apt_upgrade", "playbook": "apt_upgrade"}]

        if job_type == QR_JOB_APT_ALL:
            # Combined: apt update then apt upgrade
            return [
                {"stage": "apt_update", "playbook": "apt_update"},
                {"stage": "apt_upgrade", "playbook": "apt_upgrade"},
            ]

        # Unknown job_type — fail explicitly instead of silent fallback
        raise ValueError(
            f"Unknown job_type '{job_type}' for engine '{engine_name}'. "
            f"Known types: deploy, rebuild, reconfigure, undeploy, bind, unbind, start, restart, stop, reboot"
        )

    def _resolve_playbook(self, playbook_rel):
        """Resolve a playbook reference to full path.

        Args:
            playbook_rel: Playbook ID (e.g., 'service_start') or file path
                         ('playbooks/core/preflight_check.yml').

        Returns:
            Full path string.

        Raises:
            FileNotFoundError: If resolved path does not exist on disk.
        """
        if playbook_rel.startswith("/"):
            if not os.path.exists(playbook_rel):
                logger.error("[qr-runner] Playbook file missing: %s", playbook_rel)
                raise FileNotFoundError(f"Playbook file not found: {playbook_rel}")
            return playbook_rel
        # Check registry first: if it's a playbook_id, resolve to file_path
        from db.adapters.playbooks import resolve_playbook_by_id as _gpbi
        pb_record = _gpbi(self.db_path, playbook_rel)
        if pb_record and pb_record.get("file_path"):
            resolved = self.playbook_dir + pb_record["file_path"].removeprefix("playbooks/")
        else:
            # Fallback: treat as file path relative to playbook_dir
            cleaned = playbook_rel.removeprefix("playbooks/")
            resolved = self.playbook_dir + cleaned
            logger.warning("[qr-runner] Playbook '%s' not in registry, resolved as raw path: %s",
                           playbook_rel, resolved)
        if not os.path.exists(resolved):
            logger.error("[qr-runner] Playbook file missing after resolution: %s (ref=%s)", resolved, playbook_rel)
            raise FileNotFoundError(f"Playbook file not found: {playbook_rel} -> {resolved}")
        return resolved

    def _verify_playbook_integrity(self, playbook_path, expected_hash, expected_size):
        """Verify playbook integrity before execution.

        Computes fresh checksum and size from disk, compares against DB values.
        Raises FileNotFoundError if playbook file missing from disk.
        Raises SystemExit(1) if mismatch in prod mode (warns only in dev).

        Args:
            playbook_path: Full path to the playbook file on disk.
            expected_hash: SHA256 from playbook_registry (DB).
            expected_size: file_size from playbook_registry (DB).

        Returns:
            str — "pass" if all checks OK, "mismatch" if any fail.
        """
        import hashlib
        from qr_api import _CONFIG as _qr_cfg
        from db.adapters.playbooks import _parse_playbook_header as _pph

        # Read pb_mode with layered fallback: env var (subprocess-safe) → module config → default
        pb_mode = os.environ.get("QUICKROBOT_PB_MODE") or _qr_cfg.get("pb_mode", "prod")

        if not os.path.exists(playbook_path):
            logger.error("[qr-runner] Playbook file missing: %s (expected by DB registry)", playbook_path)
            raise FileNotFoundError(f"Playbook file missing from disk: {playbook_path}")

        actual_hash = hashlib.sha256(open(playbook_path, "rb").read()).hexdigest()
        actual_size = os.path.getsize(playbook_path)

        # Check @playbook_id header in YAML
        header = _pph(playbook_path)
        header_pb_id = header.get("playbook_id", "")

        # Both hash and size must be present — empty values are a verification failure, not a pass.
        if not expected_hash or not expected_size:
            raise PlaybookIntegrityError(
                f"Missing integrity data for {os.path.basename(playbook_path)}: "
                f"hash={'null' if not expected_hash else 'set'}, size={'null' if not expected_size else expected_size}"
            )

        hash_ok = actual_hash == expected_hash
        size_ok = actual_size == expected_size
        id_ok = True  # Header ID check optional for staged chain playbooks

        issues = []
        if not hash_ok:
            issues.append(f"checksum ({expected_hash[:12]} -> {actual_hash[:12]})")
        if not size_ok:
            issues.append(f"size ({expected_size}B -> {actual_size}B)")
        if not id_ok:
            issues.append("playbook_id header mismatch")

        if not (hash_ok and size_ok):
            issue_str = "; ".join(issues)
            print(f"[qr] PLAYBOOK VERIFY FAIL: {playbook_path} — {issue_str}")
            if pb_mode != "dev":
                raise PlaybookIntegrityError(
                    f"Playbook integrity mismatch: {os.path.basename(playbook_path)} — {issue_str}"
                )
            return "mismatch"

        return "pass"

    def _build_extra_vars(self, instance, stage, merged_cli_opts=None, merged_env=None, task=None):
        """Build extra_vars dict for ansible-playbook execution.

        Args:
            instance: Instance dict from DB.
            stage: Current stage name.
            merged_cli_opts: Pre-merged CLI options list (from deploy_instance route).
            merged_env: Pre-merged env dict (from deploy_instance route).
            task: Optional task dict (for health_probe metadata like _rpc_instance_id).

        Returns:
            Dict of extra vars.
        """
        config_override = {}
        if instance.get("config_override"):
            try:
                config_override = json.loads(instance["config_override"])
            except (json.JSONDecodeError, TypeError):
                pass

        # For health_probe stages (RPC), inject vars from task metadata or DB lookup
        rpc_vars = {}
        if stage == "health_probe":
            try:
                # Prefer RPC ID from task metadata (_rpc_instance_id set by _get_stage_chain)
                rpc_id = None
                if task and task.get("_rpc_instance_id"):
                    rpc_id = int(task["_rpc_instance_id"])
                else:
                    # Fallback: parse from stage name or do DB lookup
                    rpc_id = int(stage.split("_")[-1]) if "_" in stage else None

                if rpc_id is not None:
                    from db.sqlite import pool as _pool
                    with _pool(self.db_path) as _conn:
                        row = _conn.execute(
                            "SELECT i.id, n.hostname FROM instances i "
                            "JOIN nodes n ON i.node_id=n.id WHERE i.id=?", (rpc_id,)
                        ).fetchone()
                        if row:
                            rpc_vars = {
                                "unit_name": f"qr-{row['id']}-llama_rpc",
                                "rpc_id": row["id"],
                                "inventory_host": row["hostname"],
                            }
            except (ValueError, Exception):
                pass  # Non-critical — playbook will use defaults

        # Look up engine_configs for global settings (binary_path, build dirs, git url)
        ec_rows = {}
        try:
            from db.sqlite import pool
            with pool(self.db_path) as conn:
                for row in conn.execute(
                    "SELECT key, value FROM engine_configs WHERE engine_type_id=?",
                    (instance.get("engine_type_id", 0),),
                ).fetchall():
                    ec_rows[row["key"]] = row["value"] or ""
        except Exception:
            pass

        # Resolve remote node user from node record (PRIO-1-A1)
        from lib.lib_constants import DEFAULT_ANSIBLE_USER
        remote_user = DEFAULT_ANSIBLE_USER
        try:
            with pool(self.db_path) as conn:
                row = conn.execute(
                    "SELECT ansible_user FROM nodes WHERE id=?",
                    (instance.get("node_id", 1),),
                ).fetchone()
                if row and row["ansible_user"]:
                    remote_user = row["ansible_user"]
        except Exception:
            pass

        extra = {
            # Host / identity — used by all playbooks
            "inventory_host": instance.get("node_hostname") or instance.get("ipv4_address", ""),
            "instance_id": instance["id"],
            "instance_name": instance.get("name", ""),
            "engine_type": instance.get("engine_type_name", ""),
            "instance_port": instance.get("port_assigned"),
            # UUID — used in service templates ({{ instance_uuid }})
            "instance_uuid": instance.get("instance_uuid", ""),
            # Service config
            "start_on_boot": instance.get("start_on_boot", "true") == "true",
            "restart_policy": config_override.get("restart_policy", "no"),
            "start_after_deploy": instance.get("start_after_deploy", 0) != 0,
            # Device / GPU
            "device": instance.get("gpu_device") or ec_rows.get("LLAMA_ARG_DEVICE", ""),
            # Remote node user — resolved from node record (PRIO-1-A1)
            "remote_node_user": remote_user,
            "user": remote_user,  # alias for universal engine compatibility
            "model_path": instance.get("model_path", ""),
            # Build source paths + cmake commands + git pull (engine_configs)
            "node_src_dir": ec_rows.get("node_src_dir", "/opt/quickrobot/llama.cpp"),
            "node_build_dir": ec_rows.get("node_build_dir", "/opt/quickrobot/llama.cpp/build"),
            "node_build_set_cmd": ec_rows.get("node_build_set_cmd"),
            "node_build_run_cmd": ec_rows.get("node_build_run_cmd"),
            "node_git_pull_cmd": ec_rows.get("node_git_pull_cmd", "git pull origin main"),
            "git_clone_url": ec_rows.get("git_clone_url", "https://github.com/ggml-org/llama.cpp.git"),
            # Binary path — used in service templates for ExecStart
            "binary_path": ec_rows.get("binary_path", ""),
            # Additional apt dependencies from engine_configs (install_deps stage)
            "node_build_install_depends": ec_rows.get("node_build_install_depends"),
        }

        # RPC health check stages override inventory_host, unit_name, rpc_id
        extra.update(rpc_vars)

        # Pass merged CLI opts for env file generation (CONFIG-1)
        if merged_cli_opts is not None:
            extra["merged_cli_opts"] = merged_cli_opts
        if merged_env is not None:
            extra["merged_env"] = merged_env

        # Include UUID map from chain() context for preflight playbook verification
        if hasattr(self, "_current_uuid_map") and self._current_uuid_map:
            extra["uuid_map"] = self._current_uuid_map

        return extra

    def _get_stage_timeout(self, stage, playbook_path=None):
        """Return timeout in seconds for a stage.

        Priority: playbook header # @timeout: > SSOT constant fallback.
        
        Args:
            stage: Stage name (e.g., 'compile', 'source', 'preflight').
            playbook_path: Optional full path to the playbook file.
                If provided, reads # @timeout: from header first.

        Returns:
            int: Timeout in seconds.
        """
        # Layer 1: Read from playbook header if path provided
        if playbook_path is not None:
            try:
                from db.adapters.playbooks import _parse_playbook_header as _pph
                header = _pph(playbook_path)
                pb_timeout = header.get("timeout")
                if pb_timeout and pb_timeout > 0:
                    return pb_timeout
            except Exception:
                pass  # Non-critical — fall through to SSOT defaults

        # Layer 2: SSOT constant fallback based on stage
        if stage == QR_STAGE_COMPILE:
            return QR_TIMEOUT_COMPILE    # 30 min for cmake build
        if stage == QR_STAGE_SOURCE:
            return QR_TIMEOUT_SOURCE     # 10 min for git clone
        return QR_TIMEOUT_DEFAULT       # 5 min default

    def _extract_error(self, result):
        """Extract error message from ansible result dict."""
        # Check for top-level msg (e.g., Ansible inventory warnings)
        top_msg = result.get("msg", "")
        if isinstance(top_msg, str) and top_msg.strip():
            return top_msg[:500]
        plays = result.get("results", {}).get("plays", [])
        for play in plays:
            # Check play-level msg (e.g., "No hosts matched" from Ansible)
            play_msg = play.get("msg", "")
            if isinstance(play_msg, str) and play_msg.strip():
                return play_msg[:500]
            for task in play.get("tasks", []):
                for entry in task.get("results", []):
                    msg = entry.get("msg", "")
                    if isinstance(msg, str) and msg.strip():
                        return msg[:500]
                    elif isinstance(msg, dict):
                        return json.dumps(msg)[:500]
        return result.get("error", "Playbook reported failure")

    def _advance_job_to_next(self, conn, completed_task_id):
        """Advance job to next queued task, or complete the job.

        Args:
            conn: DB connection (must be open).
            completed_task_id: ID of just-completed task.
        """
        # Get the completed task
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (completed_task_id,)).fetchone()
        if not task:
            return

        job_id = task["job_id"]
        current_stage = task["stage"]

        # Check if there are more queued tasks for this job
        # ORDER BY id ASC respects creation order (tasks inserted in chain sequence)
        next_task = conn.execute(
            "SELECT * FROM tasks WHERE job_id=? AND status='queued' "
            "ORDER BY id ASC LIMIT 1", (job_id,)
        ).fetchone()

        if next_task:
            # Transition instance state based on completed stage
            state = STAGE_STATE_MAP.get(current_stage, "configuring")
            conn.execute(
                "UPDATE instances SET state=?, last_state_change=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                (state, task["instance_id"]),
            )
        else:
            # No more tasks — job complete (reuse existing conn to avoid nested pool locks)
            self._finalize_job(conn, job_id)

    def _get_task(self, task_id):
        """Get a single task by ID.

        Args:
            task_id: Task primary key.

        Returns:
            Task dict or None.
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None


def get_instance(db_path, instance_id):
    """Get instance record — local helper to avoid circular imports."""
    from db.adapters.instances import get_instance
    return get_instance(db_path, instance_id)
