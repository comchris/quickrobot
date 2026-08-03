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
    QR_ENGINE_TIMESTAMP_PROXY_NAME,
    QR_ENGINE_UNIVERSAL_NAME,
    QR_ENGINE_SUBPROCESS_NAME,
    QR_ENGINE_PORT_DEFAULTS,
    STAGE_STATE_MAP,
    SKIPABLE_STAGES,
    JOB_FINAL_STATES,
    QR_JOB_DEPLOY,
    QR_JOB_REBUILD,
    QR_JOB_RECONFIGURE,
    QR_JOB_DEPLOY_FAST,
    QR_JOB_DEPLOY_BINARY,
    QR_JOB_UNDEPLOY,
    QR_JOB_BIND,
    QR_JOB_UNBIND,
    QR_JOB_START,
    QR_JOB_RESTART,
    QR_JOB_STOP,
    QR_JOB_HEALTH_CHECK,
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
    QR_STAGE_HEALTH_CHECK,
    QR_STAGE_UNDEPLOY,
    QR_STAGE_VERIFY,
    QR_STAGE_BINARY_DOWNLOAD,
    QR_STAGE_REBOOT,
    QR_STAGE_APT_UPDATE,
    QR_STAGE_APT_UPGRADE,
    QR_STATE_ERROR,
    QR_STATE_BUILD_ERROR,
    QR_STATE_DEPLOYING,
    QR_STATE_CONFIGURING,
    QR_STATE_COMPILING,
    QR_STATE_UPDATING,
    QR_STATE_LOADING,
    QR_STATE_STOPPED,
    QR_STATE_RUNNING,
    QR_STATE_STARTING,
    QR_TIMEOUT_COMPILE,
    QR_TIMEOUT_SOURCE,
    QR_TIMEOUT_DEFAULT,
    QR_NODE_SRC_DIR,
    QR_NODE_BUILD_DIR,
    QR_BINARY_TEMPLATE_ROOT,
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
            {"stage": QR_STAGE_PREFLIGHT,   "playbook": "preflight_check"},
            {"stage": QR_STAGE_DEPS,        "playbook": "install_deps"},
            {"stage": QR_STAGE_SOURCE,      "playbook": "source_llama"},
            {"stage": QR_STAGE_COMPILE,     "playbook": "build_compile_llama"},
            {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
            {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
            {"stage": QR_STAGE_START,       "playbook": "service_start"},
        ],
       QR_ENGINE_LLAMA_RPC_NAME: [
            {"stage": QR_STAGE_PREFLIGHT,   "playbook": "preflight_check"},
            {"stage": QR_STAGE_DEPS,        "playbook": "install_deps"},
            {"stage": QR_STAGE_SOURCE,      "playbook": "source_llama"},
            {"stage": QR_STAGE_COMPILE,     "playbook": "build_compile_llama"},
            {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
            {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
            {"stage": QR_STAGE_START,       "playbook": "service_start"},
        ],
        QR_ENGINE_IPERF3_NAME: [
            {"stage": QR_STAGE_PREFLIGHT,   "playbook": "preflight_check"},
            {"stage": QR_STAGE_DEPS,        "playbook": "install_deps"},
            {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
            {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
            {"stage": QR_STAGE_START,       "playbook": "service_start"},
        ],
        QR_ENGINE_UNIVERSAL_NAME: [
            {"stage": QR_STAGE_PREFLIGHT,   "playbook": "preflight_check"},
            {"stage": QR_STAGE_DEPS,        "playbook": "install_deps"},
            {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
            {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
            {"stage": QR_STAGE_START,       "playbook": "service_start"},
      ],
        QR_ENGINE_TIMESTAMP_PROXY_NAME: [
            {"stage": QR_STAGE_PREFLIGHT,   "playbook": "preflight_check"},
            {"stage": "deploy",              "playbook": "deploy_timestamp_proxy"},
            {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
            {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
            {"stage": QR_STAGE_START,       "playbook": "service_start"},
      ],
        # Binary download — preflight → binary_download → config_svc → config_env → start
        # Replaces source + compile stages with a binary download stage.
        # Used when preset references an engine_binary template.
        QR_JOB_DEPLOY_BINARY: {
            QR_ENGINE_LLAMA_SERVER_NAME: [
                {"stage": QR_STAGE_PREFLIGHT,       "playbook": "preflight_check"},
                {"stage": QR_STAGE_BINARY_DOWNLOAD, "playbook": "deploy_binary_llama"},
                {"stage": QR_STAGE_CONFIG_SVC,      "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,      "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,           "playbook": "service_start"},
            ],
            QR_ENGINE_LLAMA_RPC_NAME: [
                {"stage": QR_STAGE_PREFLIGHT,       "playbook": "preflight_check"},
                {"stage": QR_STAGE_BINARY_DOWNLOAD, "playbook": "deploy_binary_llama"},
                {"stage": QR_STAGE_CONFIG_SVC,      "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,      "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,           "playbook": "service_start"},
            ],
            QR_ENGINE_SUBPROCESS_NAME: [
                {"stage": QR_STAGE_PREFLIGHT,       "playbook": "preflight_check"},
                {"stage": QR_STAGE_BINARY_DOWNLOAD, "playbook": "deploy_binary_subprocess"},
                {"stage": QR_STAGE_CONFIG_SVC,      "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,      "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,           "playbook": "service_start"},
            ],
            QR_ENGINE_UNIVERSAL_NAME: [
                {"stage": QR_STAGE_PREFLIGHT,       "playbook": "preflight_check"},
                {"stage": QR_STAGE_BINARY_DOWNLOAD, "playbook": "deploy_binary_universal"},
                {"stage": QR_STAGE_CONFIG_SVC,      "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,      "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,           "playbook": "service_start"},
            ],
        },
        # Fast deploy — config_svc + config_env + start only (no source/compile)
        # Used for new instances when skip_build=True: still deploys service files,
        # assumes binary already exists or will be provided separately.
        QR_JOB_DEPLOY_FAST: {
            QR_ENGINE_LLAMA_SERVER_NAME: [
                {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,       "playbook": "service_start"},
            ],
            QR_ENGINE_LLAMA_RPC_NAME: [
                {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,       "playbook": "service_start"},
            ],
            QR_ENGINE_IPERF3_NAME: [
                {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,       "playbook": "service_start"},
            ],
            QR_ENGINE_UNIVERSAL_NAME: [
                {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,       "playbook": "service_start"},
            ],
            QR_ENGINE_TIMESTAMP_PROXY_NAME: [
                {"stage": QR_STAGE_CONFIG_SVC,  "playbook": "deploy_config_service"},
                {"stage": QR_STAGE_CONFIG_ENV,  "playbook": "deploy_config_env"},
                {"stage": QR_STAGE_START,       "playbook": "service_start"},
            ],
        },
        # Health check — runs instance_health_check playbook once, returns status
        QR_JOB_HEALTH_CHECK: [
            {"stage": QR_STAGE_HEALTH_CHECK,  "playbook": "instance_health_check"},
        ],
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

    def create_deploy_job(self, instance_id, job_type="deploy", priority=5, actor="api", binary_template_id=None):
        """Create a deploy job for an instance; return (job, tasks) tuple.

        Args:
            instance_id: Instance to deploy.
            job_type: Type of operation (deploy, rebuild, etc.).
            priority: Scheduler priority (1=highest).
            actor: Who triggered this (api, agent, system).
            binary_template_id: Optional explicit binary template ID — overrides
                                preset's binary_id for chain resolution and extra_vars.

        Returns:
            Tuple of (job_dict, list_of_task_dicts).
        """
        from db.sqlite import pool
        from db.adapters.instances import get_instance

        inst = get_instance(self.db_path, instance_id)
        if not inst:
            raise ValueError(f"Instance {instance_id} not found")

        engine_name = inst.get("engine_type_name", "")
        stages = self._get_stage_chain(engine_name, job_type, inst, binary_template_id=binary_template_id)

        # Compute merged env/cli_opts for config/start stages — needed for extra_vars
        # during task creation (pre-computed in details_json).
        merged_cli_opts = None
        merged_env = None
        if engine_name:
            try:
                from engine import get_engine_capabilities
                cap = get_engine_capabilities(engine_name)
                builder_name = cap.get("env_builder") if cap else None
                if builder_name:
                    from lib.lib_cluster_env_builder import (
                        build_llama_server_env,
                        build_rpc_server_env,
                        build_timestamp_proxy_env,
                    )
                    _BUILDERS = {
                        "build_llama_server_env": build_llama_server_env,
                        "build_rpc_server_env": build_rpc_server_env,
                        "build_timestamp_proxy_env": build_timestamp_proxy_env,
                    }
                    builder = _BUILDERS.get(builder_name)
                    if builder:
                        result = builder(self.db_path, instance_id)
                        merged_cli_opts = result.get("cli_args")
                        merged_env = result.get("env")
            except Exception as _e:
                logger.debug("create_chain_tasks: env builder failed for %s: %s", engine_name, _e)

        with pool(self.db_path) as conn:
            # Create the parent job-header row (parent_id=NULL)
            cursor = conn.execute(
                """INSERT INTO log_entries
                   (parent_id, job_type, engine_type_name, instance_id, node_id, status, actor,
                    created_at, task_stage, stage_playbook, retry_count, max_retries, details_json)
                   VALUES (NULL, ?, ?, ?, ?, 'queued', ?,
                           strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                           NULL, NULL, 0, 1, ?)""",
                 (job_type, engine_name, instance_id, inst.get("node_id"), actor, json.dumps({"job_type": job_type, "binary_template_id": binary_template_id})),
            )
            job_id = cursor.lastrowid

            # Create task sub-rows (parent_id=job_id)
            tasks = []
            for s in stages:
                # service_start has no retry value
                _max_retries = 0 if s["playbook"] == "service_start" else 1
                # Look up playbook_registry_id and version for logging
                try:
                    registry_entry = conn.execute(
                        "SELECT id, version FROM playbook_registry WHERE playbook_id=?",
                        (s["playbook"],),
                    ).fetchone()
                    pb_reg_id = registry_entry[0] if registry_entry else None
                    pb_ver = registry_entry[1] if registry_entry else None
                except Exception as _e:
                    logger.debug("playbook_registry lookup failed for %s: %s", s["playbook"], _e)
                    pb_reg_id = None
                    pb_ver = None
                # Pre-compute SSOT extra_vars at creation time — avoids re-fetching
                # instance + engine_configs during task execution (Issue 1B).
                try:
                    computed_vars = self._build_extra_vars(inst, s["stage"], merged_cli_opts, merged_env, binary_template_id=binary_template_id)
                    details_payload = {"playbook": s["playbook"], "extra_vars": computed_vars}
                except Exception as _e:
                    logger.debug("extra_vars build failed for stage %s: %s", s["stage"], _e)
                    details_payload = {"playbook": s["playbook"]}
                cur = conn.execute(
                    """INSERT INTO log_entries
                       (parent_id, job_type, engine_type_name, instance_id, node_id, status, actor,
                        error_message, created_at, started_at, finished_at, duration_ms, task_stage, stage_playbook,
                        retry_count, max_retries, details_json, results_json, playbook_registry_id,
                        playbook_version)
                       VALUES (?, ?, ?, ?, ?, 'queued', ?, NULL,
                               strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                               NULL, NULL, 0, ?, ?,
                               ?, 1, ?, '', ?, ?)""",
                    (job_id, job_type, engine_name, instance_id, inst.get("node_id"), actor,
                     s["stage"], s["playbook"], _max_retries, json.dumps(details_payload), pb_reg_id, pb_ver),
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

            conn.commit()

        return {"id": job_id, "job_type": job_type, "status": "queued", "stage_count": len(tasks)}, tasks

    def create_health_check_job(self, instance_id):
        """Create a recurring health check job."""
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            # Check for existing enabled health check
            existing = conn.execute(
                f"SELECT id FROM log_entries WHERE instance_id=? AND job_type='{QR_JOB_HEALTH_CHECK}' AND parent_id IS NULL",
                (instance_id,),
            ).fetchone()
            if existing:
                return None  # Already exists

            cursor = conn.execute(
                f"""INSERT INTO log_entries
                   (parent_id, job_type, engine_type_name, instance_id, status, actor,
                    created_at, started_at, finished_at, task_stage, stage_playbook,
                    retry_count, max_retries, details_json)
                   VALUES (NULL, '{QR_JOB_HEALTH_CHECK}', NULL, ?, 'queued', 'system',
                           strftime('%Y-%m-%dT%H:%M:%SZ','now'), NULL, NULL, NULL, NULL,
                           0, 1, '{{}}')""",
                (instance_id,),
            )
            job_id = cursor.lastrowid

            # Look up playbook_registry for health_probe task
            try:
                hpb = conn.execute(
                    "SELECT id, version FROM playbook_registry WHERE playbook_id='service_start'",
                ).fetchone()
                hpb_reg_id = hpb[0] if hpb else None
                hpb_ver = hpb[1] if hpb else None
            except Exception as _e:
                logger.debug("playbook_registry lookup for health_probe failed: %s", _e)
                hpb_reg_id = None
                hpb_ver = None
            conn.execute(
                f"""INSERT INTO log_entries
                   (parent_id, job_type, engine_type_name, instance_id, status, actor,
                    created_at, started_at, finished_at, task_stage, stage_playbook,
                    retry_count, max_retries, playbook_registry_id, playbook_version, details_json)
                   VALUES (?, '{QR_JOB_HEALTH_CHECK}', NULL, ?, 'queued', 'system',
                           strftime('%Y-%m-%dT%H:%M:%SZ','now'), NULL, NULL, '{QR_STAGE_HEALTH_CHECK}',
                           'playbooks/core/service_start.yml', 0, 1, ?, ?, '{{}}')""",
                (job_id, instance_id, hpb_reg_id, hpb_ver),
            )
            conn.commit()

            return {"id": job_id, "job_type": QR_JOB_HEALTH_CHECK, "status": "queued"}

    def create_periodic_health_check(self, instance_id):
        """Create a one-shot health check task for periodic scheduler runs.

        Unlike create_health_check_job() (which is recurring with existence check),
        this creates a fresh job+task pair each time for the scheduler's periodic loop.
        Uses the instance_health_check playbook via the standard health_check chain.

        Args:
            instance_id: Integer primary key of the instance.

        Returns:
            Dict with job info, or None if the instance doesn't exist.
        """
        from db.sqlite import pool

        # Verify instance exists — join engine_types for engine_type_name
        with pool(self.db_path) as conn:
            inst = conn.execute(
                """SELECT i.id, et.name as engine_type_name
                   FROM instances i
                   LEFT JOIN engine_types et ON i.engine_type_id = et.id
                   WHERE i.id = ?""",
                (instance_id,),
            ).fetchone()
        if not inst:
            return None

        with pool(self.db_path) as conn:
            # Create job header row
            cursor = conn.execute(
                f"""INSERT INTO log_entries
                   (parent_id, job_type, engine_type_name, instance_id, status, actor,
                    created_at, started_at, finished_at, task_stage, stage_playbook,
                    retry_count, max_retries, details_json)
                   VALUES (NULL, '{QR_JOB_HEALTH_CHECK}', ?, ?, 'queued', 'system',
                           strftime('%Y-%m-%dT%H:%M:%SZ','now'), NULL, NULL, NULL, NULL,
                           0, 1, '{{}}')""",
                (inst["engine_type_name"], instance_id),
            )
            job_id = cursor.lastrowid

            # Create task row using the health_check chain playbook
            conn.execute(
                f"""INSERT INTO log_entries
                   (parent_id, job_type, engine_type_name, instance_id, status, actor,
                    created_at, started_at, finished_at, task_stage, stage_playbook,
                    retry_count, max_retries, details_json)
                   VALUES (?, '{QR_JOB_HEALTH_CHECK}', ?, ?, 'queued', 'system',
                           strftime('%Y-%m-%dT%H:%M:%SZ','now'), NULL, NULL, '{QR_STAGE_HEALTH_CHECK}',
                           'instance_health_check', 0, 1, '{{}}')""",
                (job_id, inst["engine_type_name"], instance_id),
            )
            conn.commit()

        return {"id": job_id, "job_type": QR_JOB_HEALTH_CHECK, "status": "queued"}

    def cancel_job(self, job_id):
        """Cancel all tasks in a job."""
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            # Cancel child task rows
            conn.execute(
                "UPDATE log_entries SET status='cancelled', finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE parent_id=? AND status IN ('queued','running')", (job_id,)
            )
            # Cancel parent job-header row
            conn.execute(
                "UPDATE log_entries SET status='cancelled', finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id=? AND parent_id IS NULL", (job_id,)
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

        # Load binary_template_id from parent job's details_json for binary chain
        _binary_template_id = None
        try:
            with pool(self.db_path) as conn:
                job_row = conn.execute(
                    "SELECT details_json FROM log_entries WHERE id=? AND parent_id IS NULL",
                    (task["job_id"],),
                ).fetchone()
                if job_row and job_row["details_json"]:
                    import json as _json
                    _details = _json.loads(job_row["details_json"])
                    _binary_template_id = _details.get("binary_template_id")
        except Exception:
            pass  # Non-critical — chain will default to git_build

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
        logger.info(
            "[qr-runner] PHASE1: instance_id=%d node_hostname='%s' ipv4_addr='%s' engine=%s",
            instance_id, node_hostname, inst.get("ipv4_address", ""),
            inst.get("engine_type_name", ""),
        )

        if not node_hostname:
            node_hostname = inst.get("ipv4_address", "") or ""

        # Compute merged env/cli_opts for config/start stages.
        # Uses engine CAPABILITIES["env_builder"] — if the key is missing, the
        # engine does not participate in the config merge chain (iperf3,
        # universal use their own extra_vars paths). For engines without an
        # env_builder (e.g., subprocess), fall back to preset cli_opts/env.
        engine_type_name = inst.get("engine_type_name", "")
        merged_cli_opts = None
        merged_env = None
        if engine_type_name:
            try:
                from engine import get_engine_capabilities
                cap = get_engine_capabilities(engine_type_name)
                builder_name = cap.get("env_builder") if cap else None
                if builder_name:
                    from lib.lib_cluster_env_builder import (
                        build_llama_server_env,
                        build_rpc_server_env,
                        build_timestamp_proxy_env,
                    )
                    _BUILDERS = {
                        "build_llama_server_env": build_llama_server_env,
                        "build_rpc_server_env": build_rpc_server_env,
                        "build_timestamp_proxy_env": build_timestamp_proxy_env,
                    }
                    builder = _BUILDERS.get(builder_name)
                    if builder:
                        result = builder(self.db_path, instance_id)
                        merged_cli_opts = result.get("cli_args")
                        merged_env = result.get("env")
                else:
                    # No env_builder — fall back to preset config_template
                    # This handles subprocess, iperf3, universal engine types.
                    try:
                        from db.sqlite import pool as _pool
                        with _pool(self.db_path) as _conn:
                            preset_row = _conn.execute(
                                "SELECT config_template FROM engine_presets WHERE id=(SELECT preset_id FROM instances WHERE id=?)",
                                (instance_id,),
                            ).fetchone()
                            if preset_row:
                                ct = json.loads(preset_row["config_template"] or "{}")
                                merged_env = ct.get("env")
                                _cli = ct.get("cli_opts")
                                if isinstance(_cli, list):
                                    # Join list into space-separated string for ansible
                                    merged_cli_opts = " ".join(str(x) for x in _cli)
                                logger.debug("[qr-runner] Preset fallback: merged_cli_opts=%s merged_env=%s", merged_cli_opts, bool(merged_env))
                            else:
                                logger.warning("[qr-runner] Preset fallback: no config_template for preset_id of instance %d", instance_id)
                    except Exception as _e2:
                        logger.warning("[qr-runner] Preset fallback failed: %s", _e2)
            except Exception as exc:
                logger.warning(
                    "[qr-runner] Env builder failed for instance %d (%s): %s",
                    instance_id, engine_type_name, exc,
                )

        print(f"[DEBUG-PT1] execute_task_phase1: stage={stage} binary_template_id={_binary_template_id}", file=__import__('sys').stderr)
        extra_vars = self._build_extra_vars(inst, stage, merged_cli_opts, merged_env, task, binary_template_id=_binary_template_id)

        # Update task sub-row to running and parent job-header to running
        with pool(self.db_path) as conn:
            conn.execute(
                "UPDATE log_entries SET status='running', started_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id=? AND parent_id IS NOT NULL", (task_id,)
            )
            conn.execute(
                "UPDATE log_entries SET status='running', started_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id=? AND parent_id IS NULL", (task["job_id"],)
            )
            # Health check is read-only — do NOT change instance state in Phase 1.
            # _finalize_job() handles all state transitions based on actual service findings.
            if stage != QR_STAGE_HEALTH_CHECK:
                state = STAGE_STATE_MAP.get(stage, "configuring")
                conn.execute(
                    "UPDATE instances SET state=?, last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
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

        result = None
        try:
            if build_lock is not None:
                build_lock.acquire(timeout=QR_TIMEOUT_DEFAULT)
            logger.info(
                "[qr-runner] PLAYBOOK RUN: playbook=%s limit=%s node_id=%s inventory_host=%s",
                playbook_path, node_hostname,
                extra_vars.get("node_id", "N/A"),
                extra_vars.get("inventory_host", "N/A"),
            )
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
                # Stop stage is idempotent: already-stopped or no hosts matched = success
                if stage == QR_STAGE_STOP and error_msg:
                    low = error_msg.lower()
                    if "no hosts matched" in low or "empty ansible" in low or "already stopped" in low:
                        success = True
                        error_msg = None
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
        except Exception as _e:
            logger.debug("playbook_runs save failed for task %d: %s", task_id, _e)
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
        except Exception as _e:
            logger.debug("playbook counter update failed for %s: %s", playbook_id, _e)
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
            _action_map[QR_STAGE_UNDEPLOY] = "undeploy_instance"
            _action_map[QR_STAGE_VERIFY]   = "get_logs"
            action_type = _action_map.get(_stage)
            if not action_type:
                # Fallback for unknown stages — try common prefix mapping
                _fallback = {
                    QR_STAGE_PREFLIGHT: "validate_node", QR_STAGE_DEPS: "apt_update",
                    QR_STAGE_SOURCE: "ansible_execute", QR_STAGE_COMPILE: "update_and_compile",
                    QR_STAGE_CONFIG_SVC: "config_change", QR_STAGE_CONFIG_ENV: "config_change",
                    QR_STAGE_START: "restart_instance", QR_STAGE_STOP: "stop_instance",
                }
                action_type = _fallback.get(_stage, "ansible_execute")
            node_id = inst.get("node_id") if inst else None
            _log_aa(
                self.db_path, action_type, node_id, instance_id,
                playbook_id or "", extra_vars or {}, result,
                parent_id=job_id, task_id=task_id,
            )
        except Exception as _log_exc:
            logger.warning("[qr-runner] ansible_actions logging failed for task %d (%s): %s",
                           task_id, _stage, _log_exc)

        duration_ms = int((time.time() - start_time) * 1000)
        new_status = "completed" if success else "failed"
        err_val = error_msg if not success and error_msg else None

        # Update task sub-row + job-header status
        with pool(self.db_path) as conn:
            result_json = json.dumps(result) if isinstance(result, dict) and result else None
            conn.execute(
                "UPDATE log_entries SET status=?, error_message=?, finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                "results_json=? WHERE id=? AND parent_id IS NOT NULL",
                (new_status, err_val, result_json, task_id),
            )

            if not success and error_msg:
                conn.execute(
                    """INSERT INTO log_entries
                       (parent_id, job_type, engine_type_name, instance_id, status, actor,
                        details_json, created_at)
                       VALUES (NULL, 'runner_task_failed', NULL, ?, 'failed', 'scheduler',
                               ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                    (instance_id, json.dumps({"task_id": task_id, "stage": stage, "error": error_msg})),
                )

            if success:
                self._advance_job_to_next(conn, task_id)
            else:
                # Retry logic — update log_entries row
                conn.execute(
                    "UPDATE log_entries SET retry_count=retry_count+1 WHERE id=? AND parent_id IS NOT NULL", (task_id,)
                )
                row = conn.execute(
                    "SELECT retry_count, max_retries FROM log_entries WHERE id=? AND parent_id IS NOT NULL", (task_id,)
                ).fetchone()
                if row and row["retry_count"] < row["max_retries"]:
                    conn.execute(
                        "UPDATE log_entries SET status='queued', started_at=NULL, finished_at=NULL "
                        "WHERE id=?", (task_id,)
                    )
                    logger.info("[qr-runner] Task %d (%s) requeued (retry %d/%d)", task_id, stage, row["retry_count"], row["max_retries"])
                else:
                    conn.execute(
                        "UPDATE log_entries SET status='failed', error_message=?, finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                        "WHERE id=? AND parent_id IS NULL",
                        (f"Task '{stage}' failed after max retries", job_id),
                    )
                    conn.execute(
                        "UPDATE log_entries SET status='cancelled', finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                        "WHERE parent_id=? AND status IN ('queued','running')", (job_id,)
                    )
                    conn.execute(
                        "UPDATE instances SET state='error', last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
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
            job_id: Primary key of the completed job (parent row in log_entries).
        """
        # Get job header row from log_entries (parent_id IS NULL)
        job = conn.execute(
            "SELECT id, instance_id, job_type, engine_type_name FROM log_entries WHERE id=? AND parent_id IS NULL", (job_id,)
        ).fetchone()
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
        ERROR_STATES = frozenset([QR_STATE_ERROR, QR_STATE_BUILD_ERROR, "timeout"])

        if job_type == QR_JOB_HEALTH_CHECK:
            # Health check = discrepancy detector.
            # Compare remote service state vs DB instance.state.
            # Only update if there's an unexpected mismatch.
            # Skip if instance is in a transition state (deploy/rebuild chain active).
            
            _TRANSITIONING = frozenset([QR_STATE_DEPLOYING, QR_STATE_CONFIGURING, QR_STATE_COMPILING, QR_STATE_UPDATING, QR_STATE_LOADING])

            task_run = conn.execute(
                "SELECT results_json FROM log_entries "
                "WHERE parent_id=? AND status='completed' LIMIT 1",
                (job_id,),
            ).fetchone()

            # Read the instance state BEFORE this health check ran.
            # Phase 1 no longer changes instance state for health checks,
            # so this is the pre-check DB state.
            pre_state = conn.execute(
                "SELECT state FROM instances WHERE id=?", (instance_id,)
            ).fetchone()["state"]

            # If in a transition state, skip — let the chain handle state
            if pre_state in _TRANSITIONING:
                logger.debug(
                    "[qr-runner] HC skipped state update for inst=%d: instance in '%s' "
                    "(deploy/rebuild chain active)",
                    instance_id, pre_state,
                )
            elif not task_run or not task_run["results_json"]:
                # Parse failure → error, unless instance intentionally stopped
                logger.warning(
                    "[qr-runner] HC parse failure for inst=%d — marking error", instance_id,
                )
                # Guard: preserve stopped state instead of overwriting with error
                if pre_state == QR_STATE_STOPPED:
                    new_state = pre_state
                else:
                    new_state = "error"
            else:
                try:
                    import json as _json
                    parsed = _json.loads(task_run["results_json"])

                    # Extract service_state from ansible playbook debug output
                    service_state = None
                    plays = parsed.get("results", {}).get("plays", [])
                    if plays:
                        for play in plays:
                            tasks = play.get("tasks", [])
                            for task in tasks:
                                host_results = task.get("hosts", {})
                                for host, result_data in host_results.items():
                                    debug_msg = result_data.get("msg", "")
                                    if debug_msg and debug_msg.startswith("{"):
                                        try:
                                            svc_data = _json.loads(debug_msg)
                                            service_state = svc_data.get("service_state")
                                        except ValueError:
                                            pass
                                if service_state:
                                    break
                            if service_state:
                                break

                    if service_state is None:
                        # Parse failure — preserve current state if intentionally stopped
                        if pre_state == QR_STATE_STOPPED:
                            new_state = pre_state
                        else:
                            new_state = "error"
                    elif service_state in ("active", "activating"):
                        new_state = "running"
                        # Guard: don't correct stopped→running if a stop job is active
                        # (would collide with the user-initiated stop chain)
                        if pre_state == QR_STATE_STOPPED:
                            has_stop_job = conn.execute(
                                "SELECT 1 FROM log_entries WHERE instance_id=? "
                                "AND parent_id IS NULL AND job_type IN ('stop', 'undeploy') "
                                "AND status IN ('running', 'queued', 'received') LIMIT 1",
                                (instance_id,),
                            ).fetchone()
                            if has_stop_job:
                                new_state = pre_state  # preserve stopped

                    elif service_state in ("inactive", "failed", "deactivating"):
                        # Guard: user intentionally stopped the instance — preserve stopped
                        # Don't overwrite intentional stop with error from health check
                        if pre_state == QR_STATE_STOPPED:
                            new_state = pre_state  # preserve stopped
                        else:
                            new_state = "error"
                    else:
                        # Unknown state (e.g., "unknown") — preserve current
                        new_state = pre_state

                    # Only write if the detected state differs from DB state
                    if new_state != pre_state:
                        conn.execute(
                            "UPDATE instances SET state=?, "
                            "last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                            (new_state, instance_id),
                        )
                        logger.info(
                            "[qr-runner] HC corrected inst=%d: '%s' → '%s' "
                            "(service_state=%s)",
                            instance_id, pre_state, new_state, service_state,
                        )
                    else:
                        logger.debug(
                            "[qr-runner] HC stable for inst=%d: '%s' (service_state=%s) — no change",
                            instance_id, pre_state, service_state,
                        )

                except Exception as exc:
                    logger.warning(
                        "[qr-runner] HC parse error for inst=%d: %s", instance_id, exc,
                    )
        elif job_type in (QR_JOB_BIND, QR_JOB_UNBIND):
            new_state = pre_state
        else:
            if job_type not in JOB_FINAL_STATES:
                logger.debug("[qr] DEBUG: Unknown job_type '%s' for instance %d — expected one of %s", job_type, instance_id, list(JOB_FINAL_STATES.keys()))
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
                if pre_state in (QR_STATE_RUNNING, QR_STATE_STARTING, QR_STATE_DEPLOYING, QR_STATE_CONFIGURING):
                    new_state = pre_state

        # HS-1: Preserve error state when deploy/rebuild tasks actually failed.
        # Check for both 'failed' (from _run_task_playbook) and 'error' (from
        # _detect_stale_tasks stale/expired detection). If any child task is
        # in a terminal failure state, do NOT overwrite with JOB_FINAL_STATES.
        # This ensures expired jobs correctly keep the instance in 'error'
        # instead of masking failures as 'running'.
        if job_type in (QR_JOB_DEPLOY, QR_JOB_REBUILD):
            failed_children = conn.execute(
                "SELECT COUNT(*) FROM log_entries WHERE parent_id=? AND status IN ('failed','error')",
                (job_id,),
            ).fetchone()[0]
            if failed_children > 0:
                new_state = "error"

        # Update state + last_state_change. Health_check handled above
        # (conditional update only on state transition). All other job types
        # always write their final state here.
        if job_type != QR_JOB_HEALTH_CHECK:
            conn.execute(
                "UPDATE instances SET state=?, last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (new_state, instance_id),
            )
        conn.execute(
            "UPDATE log_entries SET status='completed', finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE id=? AND parent_id IS NULL", (job_id,)
        )

        # HC-LOG-CLEANUP: When health_check_logging=false, remove successful health_check
        # log entries from DB to prevent table bloat. Includes parent + all children
        # (health_check task, ansible_execute sub-tasks, playbook_runs).
        if job_type == QR_JOB_HEALTH_CHECK:
            try:
                hc_row = conn.execute(
                    "SELECT value FROM engine_configs WHERE engine_type_id=4 AND key='health_check_logging'",
                ).fetchone()
                hc_off = hc_row and str(hc_row["value"]).lower() not in ("false", "0", "no")
                if not hc_off:
                    child_count = conn.execute(
                        "SELECT COUNT(*) FROM log_entries WHERE parent_id=?", (job_id,),
                    ).fetchone()[0]
                    # Delete playbook_runs for these tasks
                    conn.execute(
                        "DELETE FROM playbook_runs WHERE task_id IN (SELECT id FROM log_entries WHERE parent_id=?)",
                        (job_id,),
                    )
                    # Delete all child entries AND the parent job row
                    conn.execute("DELETE FROM log_entries WHERE parent_id=?", (job_id,))
                    conn.execute("DELETE FROM log_entries WHERE id=? AND parent_id IS NULL", (job_id,))
                    logger.debug(
                        "[qr-runner] HC-LOG-CLEANUP: removed job %d + %d child(ren) from log_entries",
                        job_id, child_count,
                    )
            except Exception as exc:
                logger.warning("[qr-runner] HC log cleanup failed for job %d: %s", job_id, exc)

        # Extract build_number from source stage output (deploy + rebuild)
        if job_type in (QR_JOB_DEPLOY, QR_JOB_REBUILD):
            try:
                source_run = conn.execute(
                    "SELECT results_json FROM log_entries WHERE parent_id=? AND task_stage='source' ORDER BY id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if source_run and source_run["results_json"]:
                    import re as _re
                    bm = _re.search(r'commit=([a-zA-Z0-9][a-zA-Z0-9._-]*)', str(source_run["results_json"]))
                    if bm:
                        conn.execute(
                            "UPDATE instances SET build_number=? WHERE id=?",
                            (bm.group(1), instance_id),
                        )
            except Exception as _e:
                logger.debug("build_number update failed for instance %d: %s", instance_id, _e)
                pass  # Non-critical

        # Extract build_number from binary template version (deploy_binary)
        if job_type == QR_JOB_DEPLOY_BINARY:
            try:
                binary_run = conn.execute(
                    "SELECT results_json FROM log_entries WHERE parent_id=? AND task_stage='binary_download' ORDER BY id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if binary_run and binary_run["results_json"]:
                    _bv = json.loads(binary_run["results_json"])
                    _extra = _bv.get("extra_vars", {})
                    _ver = _extra.get("binary_version") or _extra.get("version", "")
                    if _ver:
                        conn.execute(
                            "UPDATE instances SET build_number=? WHERE id=?",
                            (_ver, instance_id),
                        )
            except Exception as _e:
                logger.debug("build_number update failed for instance %d (binary): %s", instance_id, _e)
                pass  # Non-critical

    # ── Sync Chain (API Route Integration) ─────────────────────────

    def chain(self, instance_id, job_type="deploy", actor="api", skip_build=False, async_mode=False, binary_template_id=None):
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
            binary_template_id: Optional explicit binary template ID — overrides
                                preset's binary_id for chain resolution and extra_vars.

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
            except Exception as _e:
                logger.debug("rpc_binding_warnings failed for instance %d: %s", instance_id, _e)
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
        except Exception as _e:
            logger.debug("uuid_preflight failed for instance %d: %s", instance_id, _e)
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
                    logger.info("[qr] Stale scheduler PID (%d) detected, auto-restarted", sched_pid)
                except Exception as _re:
                    logger.warning("[qr] Stale scheduler restart failed: %s", _re)
        except ImportError:
            pass  # psutil not available — skip check
        except Exception as _e:
            logger.debug("scheduler_alive_check failed: %s", _e)
            pass  # Non-critical — proceed regardless

        # Async mode: create job + tasks, update instance state, return immediately.
        if async_mode:
            job, tasks = self.create_deploy_job(instance_id, job_type, priority=5, actor=actor, binary_template_id=binary_template_id)
            # Update instance state immediately — visible in WebUI before scheduler picks up task.
            # The first task's stage determines the initial display state (deploying/configuring/etc.)
            from db.sqlite import pool
            with pool(self.db_path) as conn:
                first_task = conn.execute(
                    "SELECT task_stage FROM log_entries WHERE parent_id=? ORDER BY id ASC LIMIT 1",
                    (job["id"],),
                ).fetchone()
                initial_state = STAGE_STATE_MAP.get(first_task["task_stage"]) if first_task else None
                if initial_state:
                    conn.execute(
                        "UPDATE instances SET state=?, last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (initial_state, instance_id),
                    )
                    conn.commit()
            result["job_id"] = job["id"]
            result["tasks_created"] = len(tasks)
            result["task_ids"] = [t["id"] for t in tasks]
            result["uuid_mismatches"] = uuid_mismatches
            result["message"] = f"Job {job['id']} queued (async)"
            result["duration_ms"] = int((time.time() - start_time) * 1000)
            return result

        try:
            # Create the deploy job + tasks
            job, tasks = self.create_deploy_job(instance_id, job_type, priority=5, actor=actor, binary_template_id=binary_template_id)

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
                    "SELECT * FROM log_entries WHERE instance_id=? AND parent_id IS NULL AND status=? ORDER BY created_at DESC",
                    (instance_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM log_entries WHERE instance_id=? AND parent_id IS NULL ORDER BY created_at DESC LIMIT 10",
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
                "SELECT *, parent_id AS job_id FROM log_entries WHERE parent_id=? ORDER BY task_stage", (job_id,)
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
                    """SELECT le.id, j.id AS job_id, le.instance_id, le.task_stage AS stage,
                              le.stage_playbook AS playbook,
                              le.status, le.error_message, le.started_at, le.finished_at,
                              le.retry_count, le.max_retries, le.created_at,
                              0 AS priority, i.node_id
                        FROM log_entries le
                        JOIN log_entries j ON le.parent_id = j.id
                        JOIN instances i ON le.instance_id = i.id
                        WHERE le.status = 'queued'
                          AND NOT EXISTS (
                              SELECT 1 FROM log_entries le2
                              JOIN log_entries j2 ON le2.parent_id = j2.id
                              WHERE le2.parent_id = j.id
                                AND le2.status = 'running'
                          )
                        ORDER BY i.node_id ASC, le.created_at ASC
                        LIMIT 1"""
                ).fetchone()
                return dict(row) if row else None
            except Exception as _e:
                logger.debug("playbook_runs insert failed, rolling back: %s", _e)
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
                      "FROM log_entries j "
                      "LEFT JOIN instances i ON j.instance_id = i.id "
                      "LEFT JOIN nodes n ON i.node_id = n.id "
                      "WHERE j.parent_id IS NULL")
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
                query += " AND " + " AND ".join(conditions)
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
            job = conn.execute("SELECT * FROM log_entries WHERE id=? AND parent_id IS NULL", (job_id,)).fetchone()
            if not job:
                return None
            tasks = conn.execute(
                "SELECT id, parent_id AS job_id FROM log_entries WHERE parent_id=? ORDER BY task_stage", (job_id,)
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
            query = "SELECT *, parent_id AS job_id FROM log_entries le WHERE parent_id IS NOT NULL"
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
                query += " AND " + " AND ".join(conditions)
            query += " ORDER BY le.created_at DESC LIMIT 200"

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
            task = conn.execute("SELECT *, parent_id AS job_id FROM log_entries WHERE id=? AND parent_id IS NOT NULL", (task_id,)).fetchone()
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

    def _get_stage_chain(self, engine_name, job_type, instance, binary_template_id=None):
        """Get the stage chain for an engine type + job type.

        Args:
            engine_name: Engine type name (e.g., 'llama_server').
            job_type: Job type (deploy, rebuild, etc.).
            instance: Instance dict from DB (may be None for node-level operations).

        Returns:
            List of stage dicts: [{stage, playbook}, ...].

        Raises:
            ValueError: If engine_name has no registered stage chain and a full deploy is requested.
        """
        if engine_name not in DEFAULT_STAGE_CHAINS:
            # Check persisted binary_template_id in config_override (re-deploy detection)
            _co = (instance.get("config_override") or {}) if instance else {}
            if isinstance(_co, str):
                try:
                    _co = json.loads(_co)
                except (json.JSONDecodeError, TypeError):
                    _co = {}
            _persisted_btid = _co.get("binary_template_id")
            if _persisted_btid is not None and engine_name in DEFAULT_STAGE_CHAINS.get("deploy_binary", {}):
                _bt_chain = DEFAULT_STAGE_CHAINS[QR_JOB_DEPLOY_BINARY][engine_name]
                logger.debug(
                    "[qr] Re-deploy detected for inst=%s via config_override.binary_template_id=%s, using deploy_binary chain",
                    instance.get("id", "??"), _persisted_btid,
                )
                if job_type == QR_JOB_REBUILD:
                    return [s for s in _bt_chain if s["stage"] != QR_STAGE_DEPS]
                return _bt_chain
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
                return [{"stage": QR_STAGE_START, "playbook": "service_start"}]
            if job_type == QR_JOB_RESTART:
                return [{"stage": QR_STAGE_STOP, "playbook": "service_stop"},
                        {"stage": QR_STAGE_START, "playbook": "service_start"}]
            if job_type == QR_JOB_STOP:
                return [{"stage": QR_STAGE_STOP, "playbook": "service_stop"}]
            # undeploy below handles its own chain

        if job_type == QR_JOB_REBUILD:
            # Check persisted binary_template_id in config_override (binary-deploy detection).
            # This ensures rebuild uses the same chain as the original deploy.
            _co = (instance.get("config_override") or {}) if instance else {}
            if isinstance(_co, str):
                try:
                    _co = json.loads(_co)
                except Exception:
                    _co = {}
            _persisted_btid = _co.get("binary_template_id")
            if _persisted_btid is not None and engine_name in DEFAULT_STAGE_CHAINS.get(
                QR_JOB_DEPLOY_BINARY, {}
            ):
                # Binary-deployed instance: rebuild via binary chain (skip DEPS only).
                return [s for s in DEFAULT_STAGE_CHAINS[QR_JOB_DEPLOY_BINARY][engine_name]
                        if s["stage"] != QR_STAGE_DEPS]
            # Git-build instance: standard rebuild chain (compile + config).
            return [
                s for s in DEFAULT_STAGE_CHAINS[engine_name]
                if s["stage"] not in (QR_STAGE_DEPS, QR_STAGE_SOURCE)
            ]

        if job_type == QR_JOB_RECONFIGURE:
            # Config-only update: service unit regen (for start_on_boot/restart_policy) +
            # env file update + service restart. Replaces legacy api_reconfigure_instance().
            # config_svc stage handles systemd enable/disable for start_on_boot changes.
            # config_env stage handles env file for all env vars including restart_policy.
            return [
                s for s in DEFAULT_STAGE_CHAINS[engine_name]
                if s["stage"] in (QR_STAGE_CONFIG_SVC, QR_STAGE_CONFIG_ENV, QR_STAGE_START)
            ]

        if job_type == QR_JOB_DEPLOY_FAST:
            # Fast deploy: config_svc + config_env + start (no source/compile)
            # Used for new instances with skip_build=True when binary exists.
            return DEFAULT_STAGE_CHAINS[QR_JOB_DEPLOY_FAST].get(engine_name, [])

        if job_type == QR_JOB_DEPLOY_BINARY:
            # Binary download: preflight → binary_download → config_svc → config_env → start
            # Replaces source + compile stages; uses deploy_binary_llama playbook.
            return DEFAULT_STAGE_CHAINS[QR_JOB_DEPLOY_BINARY].get(engine_name, [])

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
            return [{"stage": QR_STAGE_START, "playbook": "service_start"}]

        if job_type == QR_JOB_RESTART:
            # Restart: stop → start (no RPC health probes)
            return [{"stage": QR_STAGE_STOP, "playbook": "service_stop"},
                    {"stage": QR_STAGE_START, "playbook": "service_start"}]

        if job_type == QR_JOB_STOP:
            # Stop: just stop the systemd service
            return [{"stage": QR_STAGE_STOP, "playbook": "service_stop"}]

        if job_type == QR_JOB_DEPLOY:
            # Full deploy — use registered chain
            return DEFAULT_STAGE_CHAINS[engine_name]

        if job_type == QR_JOB_REBOOT:
            # Async fire-and-forget reboot — returns immediately
            return [{"stage": QR_STAGE_REBOOT, "playbook": "reboot_node"}]

        if job_type == QR_JOB_APT_UPDATE:
            # Node-level apt update
            return [{"stage": QR_STAGE_APT_UPDATE, "playbook": "apt_update"}]

        if job_type == QR_JOB_APT_UPGRADE:
            # Node-level apt upgrade
            return [{"stage": QR_STAGE_APT_UPGRADE, "playbook": "apt_upgrade"}]

        if job_type == QR_JOB_APT_ALL:
            # Combined: apt update then apt upgrade
            return [
                {"stage": QR_STAGE_APT_UPDATE, "playbook": "apt_update"},
                {"stage": QR_STAGE_APT_UPGRADE, "playbook": "apt_upgrade"},
            ]

        # Unknown job_type — fail explicitly instead of silent fallback
        raise ValueError(
            f"Unknown job_type '{job_type}' for engine '{engine_name}'. "
            f"Known types: deploy, rebuild, reconfigure, undeploy, bind, unbind, start, restart, stop, reboot"
        )

    def _get_instance_binary_ref(self, instance_id, binary_template_id=None):
        """Look up binary reference from explicit template ID or preset config_template.

        If binary_template_id is provided, bypasses preset lookup entirely
        (orthogonal preset/template design — see §3.2A of binary-download-template.md).
        Otherwise falls back to preset's config_template.binary_id.

        Args:
            instance_id: Integer primary key of the instance.
            binary_template_id: Optional explicit template ID — overrides preset lookup.

        Returns:
            Dict with binary template data (from engine_binaries table), or None.
        """
        from db.sqlite import pool

        # Fast path: explicit template ID bypasses preset entirely
        if binary_template_id is not None:
            with pool(self.db_path) as conn:
                binary = conn.execute(
                    "SELECT * FROM engine_binaries WHERE id=? AND is_active=1",
                    (binary_template_id,),
                ).fetchone()
            return dict(binary) if binary else None

        # Fallback: look up preset, read binary_id from config_template
        with pool(self.db_path) as conn:
            inst = conn.execute(
                "SELECT i.preset_id, p.config_template FROM instances i "
                "JOIN engine_presets p ON i.preset_id = p.id WHERE i.id=?",
                (instance_id,),
            ).fetchone()

        if not inst or not inst["config_template"]:
            return None

        try:
            config = json.loads(inst["config_template"])
        except (json.JSONDecodeError, TypeError):
            return None

        binary_id = config.get("binary_id")
        if not binary_id:
            return None

        with pool(self.db_path) as conn:
            binary = conn.execute(
                "SELECT * FROM engine_binaries WHERE id=? AND is_active=1",
                (binary_id,),
            ).fetchone()

        return dict(binary) if binary else None

    def _get_binary_chain_for_engine(self, engine_name):
        """Return the binary download stage chain for an engine type.

        Args:
            engine_name: Engine type name (e.g., 'llama_server').

        Returns:
            List of stage dicts, or empty list if engine not supported.
        """
        return DEFAULT_STAGE_CHAINS.get("deploy_binary", {}).get(engine_name, [])

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
            logger.warning("PLAYBOOK VERIFY FAIL: %s — %s", playbook_path, issue_str)
            if pb_mode != "dev":
                raise PlaybookIntegrityError(
                    f"Playbook integrity mismatch: {os.path.basename(playbook_path)} — {issue_str}"
                )
            return "mismatch"

        return "pass"

    def _build_extra_vars(self, instance, stage, merged_cli_opts=None, merged_env=None, task=None, binary_template_id=None):
        """Build extra_vars dict for ansible-playbook execution.

        Args:
            instance: Instance dict from DB.
            stage: Current stage name.
            merged_cli_opts: Pre-merged CLI options list (from deploy_instance route).
            merged_env: Pre-merged env dict (from deploy_instance route).
            task: Optional task dict (for health_probe metadata like _rpc_instance_id).
            binary_template_id: Optional explicit template ID — overrides preset lookup.

        Returns:
            Dict of extra vars.
        """
        # Use pre-computed extra_vars from task details_json when available
        # (stored at job creation time to avoid re-fetching instance + engine_configs).
        if task and task.get("details_json"):
            try:
                payload = json.loads(task["details_json"])
                precomputed = payload.get("extra_vars")
                if precomputed and stage == QR_STAGE_STOP:
                    # Stop stage only needs base params — no merge chain needed.
                    return precomputed
            except (json.JSONDecodeError, TypeError):
                pass  # Fall through to normal build

        config_override = {}
        if instance.get("config_override"):
            try:
                config_override = json.loads(instance["config_override"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Binary template: resolve binary reference once for all stages.
        # When binary_template_id is provided (explicit or from preset config_template),
        # compute the resolved binary_path so that config_svc/config_env stages use it
        # in ExecStart instead of falling back to git-build path.
        binary_ref = None
        if binary_template_id is not None:
            binary_ref = self._get_instance_binary_ref(instance.get("id"), binary_template_id=binary_template_id)
        elif not binary_template_id and instance.get("preset_id"):
            # Fallback: check preset config_template.binary_id (backward compat)
            _btid_fallback = self._get_instance_binary_ref(instance.get("id"), binary_template_id=None)
            if _btid_fallback:
                binary_ref = _btid_fallback

        # Binary download stage vars — only injected during binary_download stage
        binary_vars = {}
        if stage == QR_STAGE_BINARY_DOWNLOAD and binary_ref:
            binary_vars = {
                "binary_download_url": binary_ref.get("download_url", ""),
                "binary_checksum": binary_ref.get("sha256"),
                "binary_file_size": binary_ref.get("file_size"),
                "binary_extract_type": binary_ref.get("extract_type", "none"),
                "binary_target_path": binary_ref.get("target_path", f"{QR_BINARY_TEMPLATE_ROOT}{{engine_type}}/{{version}}-{{platform}}/"),
                "binary_version": binary_ref.get("version", ""),
                "binary_platform": binary_ref.get("platform", ""),
                "binary_engine_type": instance.get("engine_type_name", ""),
                "binary_binary_name": binary_ref.get("binary_name", ""),
                "binary_template_type": binary_ref.get("template_type", "binary"),
            }

        # Subprocess/universal: inject template metadata from engine_binaries or config_override
        # metadata is a JSON TEXT column storing engine-specific fields (cli_opts, service_name, etc.)
        _engine_name = instance.get("engine_type_name", "")
        _subprocess_vars = {}
        if _engine_name in (QR_ENGINE_SUBPROCESS_NAME, QR_ENGINE_UNIVERSAL_NAME):
            # Priority 1: fresh template data from binary_ref (new deploy)
            if binary_ref and binary_ref.get("metadata"):
                try:
                    _tmpl_meta = json.loads(binary_ref["metadata"])
                except (json.JSONDecodeError, TypeError):
                    _tmpl_meta = {}
            else:
                # Priority 2: persisted metadata from config_override (re-deploy)
                co_meta = config_override.get("metadata") or config_override.get("_binary_template_metadata")
                try:
                    _tmpl_meta = json.loads(co_meta) if isinstance(co_meta, str) else (co_meta or {})
                except (json.JSONDecodeError, TypeError):
                    _tmpl_meta = {}

            # Flatten metadata fields into extra_vars for playbook consumption
            if isinstance(_tmpl_meta, dict):
                if _tmpl_meta.get("cli_opts"):
                    _subprocess_vars["merged_cli_opts"] = _tmpl_meta["cli_opts"]
                if _tmpl_meta.get("env_vars"):
                    _subprocess_vars["template_env_vars"] = _tmpl_meta.get("env_vars") or "{}"
                if _tmpl_meta.get("service_name"):
                    _subprocess_vars["template_service_name"] = _tmpl_meta["service_name"]
                if _tmpl_meta.get("working_dir"):
                    _subprocess_vars["template_working_dir"] = _tmpl_meta["working_dir"]
                if _tmpl_meta.get("start_command"):
                    _subprocess_vars["template_start_command"] = _tmpl_meta["start_command"]
                if _tmpl_meta.get("env_passthrough"):
                    _subprocess_vars["template_env_passthrough"] = _tmpl_meta.get("env_passthrough") or "[]"

        # For health_probe stages (RPC), inject vars from task metadata or DB lookup
        rpc_vars = {}
        if stage == QR_STAGE_HEALTH_PROBE:
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
        except Exception as _e:
            logger.debug("engine_configs lookup failed for instance %d: %s", instance.get("id"), _e)
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
        except Exception as _e:
            logger.debug("ansible_user lookup failed for node %d: %s", instance.get("node_id"), _e)
            pass

        # Service config — read from config_override (top-level or nested env) first, fall back to DB column
        sob_raw = instance.get("start_on_boot", "true")
        if isinstance(sob_raw, bool):
            sob_bool = sob_raw
        elif isinstance(sob_raw, str):
            sob_bool = sob_raw.lower() in ("true", "1", "yes")
        else:
            sob_bool = bool(sob_raw)
        # Override with config_override value if present (top-level or nested env)
        co_sob = config_override.get("start_on_boot") or (config_override.get("env", {}) or {}).get("start_on_boot")
        if co_sob is not None:
            if isinstance(co_sob, bool):
                sob_bool = co_sob
            elif isinstance(co_sob, str):
                sob_bool = co_sob.lower() in ("true", "1", "yes")
            else:
                 sob_bool = bool(co_sob)

        # DEBUG: log config_override values used

        # Binary path override: when a binary template is used, set binary_path
        # to the extracted binary location. This overrides the engine_configs default
        # so systemd ExecStart points to the downloaded binary, not git-build path.
        _binary_path = (merged_env or {}).get("binary_path") if merged_env else None
        if not _binary_path:
            _binary_path = ec_rows.get("binary_path", "")
        if binary_ref:
            # Construct the full path: target_path + binary_name
            # e.g., /opt/quickrobot/binary-templates/llama_server/b10146-ubuntu-x64-cpu/llama-server
            _tp = binary_ref.get("target_path", f"{QR_BINARY_TEMPLATE_ROOT}{{engine_type}}/{{version}}-{{platform}}/")
            _tp = _tp.replace("{engine_type}", instance.get("engine_type_name", "")) \
                     .replace("{version}", binary_ref.get("version", "")) \
                     .replace("{platform}", binary_ref.get("platform", ""))
            _binary_path = _tp + binary_ref.get("binary_name", "llama-server")

        extra = {
            # Host / identity — used by all playbooks
            "inventory_host": instance.get("node_hostname") or instance.get("ipv4_address", ""),
            "node_id": instance.get("node_id", 1),
            "instance_id": instance["id"],
            "instance_name": instance.get("name", ""),
            "engine_type": instance.get("engine_type_name", ""),
            "instance_port": instance.get("port_assigned"),
            # UUID — used in service templates ({{ instance_uuid }})
            "instance_uuid": instance.get("instance_uuid", ""),
            # Service config
            "start_on_boot": sob_bool,
            "restart_policy": config_override.get("restart_policy") or (config_override.get("env", {}) or {}).get("restart_policy") or "no",
            "start_after_deploy": instance.get("start_after_deploy", 0) != 0,
            # Device / GPU — merged_env includes Layer 5 (per-instance override)
            "device": instance.get("gpu_device") or (merged_env or {}).get("qr_cluster_gpu_override", "") or ec_rows.get("qr_cluster_gpu_override", ""),
            # Remote node user — resolved from node record (PRIO-1-A1)
            "remote_node_user": remote_user,
            "user": remote_user,       # alias for universal engine compatibility
            "ansible_user": remote_user, # alias for preflight/validate playbooks
            "model_path": instance.get("model_path", ""),
            # Build source paths + cmake commands + git pull
            # Use merged_env (Layer 1-5 merge including per-instance overrides) as primary source,
            # fall back to ec_rows (engine_configs defaults) for keys not overridden at Layer 5.
             "node_src_dir": (merged_env or {}).get("node_src_dir") or ec_rows.get("node_src_dir", QR_NODE_SRC_DIR),
             "node_build_dir": (merged_env or {}).get("node_build_dir") or ec_rows.get("node_build_dir", QR_NODE_BUILD_DIR),
            "node_build_set_cmd": (merged_env or {}).get("node_build_set_cmd") or ec_rows.get("node_build_set_cmd"),
            "node_build_run_cmd": (merged_env or {}).get("node_build_run_cmd") or ec_rows.get("node_build_run_cmd"),
            "node_git_pull_cmd": (merged_env or {}).get("node_git_pull_cmd") or ec_rows.get("node_git_pull_cmd", "git pull origin main"),
            "git_clone_url": (merged_env or {}).get("git_clone_url") or ec_rows.get("git_clone_url", "https://github.com/ggml-org/llama.cpp.git"),
            # Binary path — used in service templates for ExecStart
            "binary_path": _binary_path,
            # Additional apt dependencies from engine_configs (install_deps stage)
            "node_build_install_depends": (merged_env or {}).get("node_build_install_depends") or ec_rows.get("node_build_install_depends"),
        }

        # For non-cluster-engine types (iperf3, universal, subprocess) where
        # merged_env is None: fall back to config_override for known build keys.
        # This mirrors the _BUILD_KEYS routing fix in lib_cluster_env_builder.py
        # so flat overrides work uniformly across all engine types.
        _CFG_OV_BUILD_KEYS = {
            "binary_path", "git_clone_url", "model_root_path", "node_build_dir",
            "node_build_install_depends", "node_build_run_cmd", "node_build_set_cmd",
            "node_git_pull_cmd", "node_src_dir", "restart_policy", "skip_build",
            "start_on_boot", "base_port",
        }
        for _k in _CFG_OV_BUILD_KEYS:
            if _k not in extra and _k in config_override:
                extra[_k] = config_override[_k]

        # RPC health check stages override inventory_host, unit_name, rpc_id
        extra.update(rpc_vars)

        # Binary download stage overrides: inject template vars from preset binary_ref
        if binary_vars:
            extra.update(binary_vars)

        # Subprocess/universal template metadata overrides
        if _subprocess_vars:
            extra.update(_subprocess_vars)

        # Health check stage requires unit_name for systemctl probe
        if stage == QR_STAGE_HEALTH_CHECK:
            _eng_name = instance.get("engine_type_name", "instance")
            extra["unit_name"] = f"qr-{instance['id']}-{_eng_name}"

        # Pass merged CLI opts for env file generation (CONFIG-1)
        if merged_cli_opts is not None:
            extra["merged_cli_opts"] = merged_cli_opts
        # For subprocess/universal/iperf3 engines without presets, merged_env stays None.
        # Always ensure merged_env is set (empty dict if no preset merge) so Jinja2 templates
        # {% for k, v in merged_env.items() %} don't fail with 'undefined' error.
        if merged_env is None:
            extra["merged_env"] = {}
        else:
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
            except Exception as _e:
                logger.debug("playbook header timeout parse failed: %s", _e)

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
            completed_task_id: ID of just-completed task (sub-row in log_entries).
        """
        # Get the completed task sub-row
        task = conn.execute(
            "SELECT id, parent_id, task_stage AS stage, instance_id FROM log_entries WHERE id=? AND parent_id IS NOT NULL", (completed_task_id,)
        ).fetchone()
        if not task:
            return

        job_id = task["parent_id"]  # parent_id on sub-row = parent job-header ID
        current_stage = task["stage"]

        # Check if there are more queued tasks for this job
        # ORDER BY id ASC respects creation order (tasks inserted in chain sequence)
        next_task = conn.execute(
            "SELECT * FROM log_entries WHERE parent_id=? AND status='queued' "
            "ORDER BY id ASC LIMIT 1", (job_id,)
        ).fetchone()

        if next_task:
            # Transition instance state based on completed stage
            state = STAGE_STATE_MAP.get(current_stage, "configuring")
            conn.execute(
                "UPDATE instances SET state=?, last_state_change=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (state, task["instance_id"]),
            )
        else:
            # No more tasks — job complete (reuse existing conn to avoid nested pool locks)
            self._finalize_job(conn, job_id)

    def _get_task(self, task_id):
        """Get a single task sub-row by ID.

        Args:
            task_id: Task sub-row primary key (parent_id IS NOT NULL).

        Returns:
            Task dict or None.
        """
        from db.sqlite import pool

        with pool(self.db_path) as conn:
            row = conn.execute(
                "SELECT *, parent_id AS job_id FROM log_entries WHERE id=? AND parent_id IS NOT NULL", (task_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            # Alias log_entries columns → legacy task column names for compatibility
            if "task_stage" in d and "stage" not in d:
                d["stage"] = d["task_stage"]
            if "stage_playbook" in d and "playbook" not in d:
                d["playbook"] = d["stage_playbook"]
            return d


def get_instance(db_path, instance_id):
    """Get instance record — local helper to avoid circular imports."""
    from db.adapters.instances import get_instance
    return get_instance(db_path, instance_id)
