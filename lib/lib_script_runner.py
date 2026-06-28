# Copyright 2026 comchris quickrobot .de project

"""quickrobot — Dynamic Script Runner (SCRIPT-1).

Executes multi-step scripts as a parent job with child tasks.
Each step is defined as JSON in the script definition; the runner
resolves parameters, executes actions, and reports results.

Usage:
    POST /api/v1/scripts
    {
        "name": "Deploy three llama-servers",
        "steps": [
            {"type": "create_instance", "params": {...}},
            {"type": "deploy_instance", "instance_ids": ["latest"], "wait_for_state": "running"}
        ]
    }
"""

import json
import logging
import time
from datetime import datetime as _dt

logger = logging.getLogger(__name__)


class ScriptRunner:
    """Execute multi-step scripts with dependency resolution.

    Args:
        db_path: Path to the SQLite database.
    """

    def __init__(self, db_path):
        self.db_path = db_path

    def execute_script(self, script_id):
        """Execute all steps of a script in order, respecting dependencies.

        Args:
            script_id: Primary key of the script record.

        Returns:
            dict with keys: success (bool), completed_steps (int), failed_steps (int).
        """
        from db.sqlite import pool
        from lib.lib_runner import PlaybookRunner

        runner = PlaybookRunner(self.db_path)

        with pool(self.db_path) as conn:
            # Get script record
            script = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
            if not script:
                return {"success": False, "error": f"Script {script_id} not found"}

            # Get all steps
            steps = conn.execute(
                "SELECT * FROM script_steps WHERE script_id=? ORDER BY step_index",
                (script_id,),
            ).fetchall()

            # Update script to running
            conn.execute(
                "UPDATE scripts SET status='running', started_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?", (script_id,)
            )
            conn.commit()

        completed = 0
        failed = 0
        skipped = 0
        success = True

        # Track results for variable resolution: step_index -> result
        step_results = {}

        for step in steps:
            step_idx = step["step_index"]

            # Check dependencies
            if not self._check_dependencies(step, step_results):
                skipped += 1
                step_results[step_idx] = {"status": "skipped", "reason": "dependency not met"}
                continue

            # Execute this step
            result = self._execute_step(script_id, step, step_results)
            step_results[step_idx] = result

            if result["status"] == "completed":
                completed += 1
            elif result["status"] == "failed":
                failed += 1
                success = False
                # Failed steps don't block independent steps, but dependent ones will skip
            else:
                # skipped or other
                skipped += 1

        # Update script final status
        with pool(self.db_path) as conn:
            if success and completed == len(steps):
                new_status = "completed"
            elif failed > 0:
                new_status = "failed"
            else:
                new_status = "completed" if skipped == len(steps) else "failed"

            conn.execute(
                "UPDATE scripts SET status=?, completed_steps=?, failed_steps=?, skipped_steps=?, "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%S','now'), updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                (new_status, completed, failed, skipped, script_id),
            )

            # Update parent job status if linked
            if script["parent_job_id"]:
                conn.execute(
                    "UPDATE jobs SET status=?, finished_at=strftime('%Y-%m-%dT%H:%M:%S','now') "
                    "WHERE id=?", (new_status, script["parent_job_id"]),
                )

            conn.commit()

        return {"success": success, "completed_steps": completed, "failed_steps": failed, "skipped_steps": skipped}

    def _check_dependencies(self, step, step_results):
        """Check if all dependencies are met for a step.

        Args:
            step: Step dict from DB.
            step_results: Dict mapping step_index -> result.

        Returns:
            True if all dependencies satisfied, False otherwise.
        """
        depends_on = json.loads(step.get("depends_on", "[]"))
        for dep_idx in depends_on:
            dep_result = step_results.get(dep_idx)
            if dep_result and dep_result.get("status") != "completed":
                return False

        # Check if_condition if present
        if_condition = step.get("if_condition", "")
        if if_condition:
            return self._eval_condition(if_condition, step_results)

        return True

    def _eval_condition(self, condition, step_results):
        """Evaluate an if_condition expression.

        Args:
            condition: String like "step[0].success" or "step[2].status == 'completed'".
            step_results: Dict of step results.

        Returns:
            Boolean result.
        """
        # Simple pattern: step[N].field
        import re
        m = re.match(r"step\[(\d+)\]\.(\w+)", condition)
        if m:
            idx = int(m.group(1))
            field = m.group(2)
            result = step_results.get(idx, {})
            val = result.get(field, False)
            return bool(val)

        # Fallback: always evaluate to True
        return True

    def _execute_step(self, script_id, step, step_results):
        """Execute a single script step.

        Args:
            script_id: Script parent ID.
            step: Step dict from DB.
            step_results: Dict of previous step results.

        Returns:
            Result dict with status, data, and error.
        """
        from db.sqlite import pool
        from lib.lib_runner import PlaybookRunner

        runner = PlaybookRunner(self.db_path)
        step_type = step.get("type", "unknown")
        params = json.loads(step.get("params", "{}")) if isinstance(step.get("params"), str) else step.get("params", {})

        # Resolve variable references in params (e.g., "latest" -> latest instance ID)
        params = self._resolve_params(params, step_results)

        result = {"status": "completed", "data": {}, "error": None}

        try:
            if step_type == "create_instance":
                result = self._step_create_instance(params)
            elif step_type == "deploy_instance":
                result = self._step_deploy_instance(params, runner)
            elif step_type == "benchmark_run":
                result = self._step_benchmark_run(params)
            else:
                result["status"] = "completed"
                result["data"] = {"message": f"Step type '{step_type}' placeholder"}

        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

        # Update step record
        with pool(self.db_path) as conn:
            conn.execute(
                "UPDATE script_steps SET status=?, result=?, finished_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id=?",
                (result["status"], json.dumps(result), step["id"]),
            )

        return result

    def _resolve_params(self, params, step_results):
        """Resolve variable references in step parameters.

        Supported patterns:
            "latest" -> most recent instance ID for that type
            step[0].result.id -> ID from previous step result

        Args:
            params: Parameter dict (may contain string references).
            step_results: Previous step results.

        Returns:
            Resolved parameter dict.
        """
        if not isinstance(params, dict):
            return params

        resolved = {}
        for k, v in params.items():
            if isinstance(v, str):
                # Check for variable references
                import re
                m = re.match(r"step\[(\d+)\]\.(.+)", v)
                if m:
                    idx = int(m.group(1))
                    field_path = m.group(2)
                    step_result = step_results.get(idx, {})
                    # Navigate nested field (simple dot path)
                    val = step_result
                    for part in field_path.split("."):
                        if isinstance(val, dict):
                            val = val.get(part)
                        else:
                            val = None
                            break
                    resolved[k] = val
                elif v == "latest":
                    # Resolve to most recently created instance
                    from db.sqlite import pool
                    with pool(self.db_path) as conn:
                        row = conn.execute(
                            "SELECT id FROM instances ORDER BY created_at DESC LIMIT 1"
                        ).fetchone()
                        resolved[k] = row["id"] if row else None
                else:
                    resolved[k] = v
            elif isinstance(v, list):
                resolved[k] = [self._resolve_params({"_": i}, step_results).get("_", i) for i in v]
            else:
                resolved[k] = v

        return resolved

    def _step_create_instance(self, params):
        """Create a new instance. Placeholder — actual impl in routes_instances.py."""
        logger.info("[qr-script] create_instance params=%s", params)
        return {"status": "completed", "data": {"message": "instance created (placeholder)"}, "error": None}

    def _step_deploy_instance(self, params, runner):
        """Deploy one or more instances. Placeholder."""
        logger.info("[qr-script] deploy_instance params=%s", params)
        return {"status": "completed", "data": {"message": "deploy queued (placeholder)"}, "error": None}

    def _step_benchmark_run(self, params):
        """Run benchmark on instance. Placeholder."""
        logger.info("[qr-script] benchmark_run params=%s", params)
        return {"status": "completed", "data": {"message": "benchmark run (placeholder)"}, "error": None}
