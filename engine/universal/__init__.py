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

"""quickrobot (v0.04) -- Universal Engine implementation.

Provides the universal engine class and its CAPABILITIES metadata for
discovery by the engine loader. This is a generic engine that runs any
command or Ansible playbook on remote nodes, with per-instance lifecycle
configuration stored in instances.config_override.
"""

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST
from engine.base import BaseEngine
from lib.lib_constants import DEFAULT_ANSIBLE_USER


CAPABILITIES = {
    "name": "universal",
    "display_name": "Universal Engine",
    "supports_models": False,
    "supports_presets": True,
    "max_instances": 99,
    "base_port": 0,
    "instant_feedback": True,
    "sub_pages": [
        {"path": "/engines/universal/config", "label": "Config", "order": 1},
    ],
}


class UniversalEngine(BaseEngine):
    """Generic engine: runs any command/playbook per instance config_override.

    No binary defaults, no port allocation defaults. Each instance is fully
    self-described via its config_override JSON column containing:
      - playbook_dir, deploy_playbook, undeploy_playbook
      - start_command, stop_command, restart_command
      - binary_path, env_vars, cli_args
      - base_port (0 = no port allocation)
      - instant_feedback (bool), feedback_timeout (int)
    """

    def __init__(self):
        self._name = "universal"

    def get_status(self, instance_id, db_path=None):
        """Get remote status via systemctl (no HTTP health endpoint)."""
        if db_path is None:
            return {
                "engine": self._name,
                "instance_id": instance_id,
                "error": "db_path required for remote get_status",
            }

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                row = conn.execute(
                    """SELECT i.port_assigned, i.state, i.name,
                               n.hostname as node_host, n.ansible_user as node_user,
                               e.name as engine_type_name
                         FROM instances i
                         LEFT JOIN nodes n ON i.node_id = n.id
                         JOIN engine_types e ON i.engine_type_id = e.id
                         WHERE i.id = ?""",
                    (instance_id,),
                ).fetchone()

                if not row:
                    return {
                        "engine": self._name,
                        "instance_id": instance_id,
                        "error": f"Instance {instance_id} not found",
                    }

                unit_name = f"qr-{instance_id}-{row['engine_type_name']}"
                node_host = row["node_host"] or QR_DEFAULT_LOCALHOST
                node_user = (row["node_user"] if row["node_user"] else None) or DEFAULT_ANSIBLE_USER

                result = self._check_remote_service(node_host, unit_name, node_user)

                return {
                    "engine": self._name,
                    "instance_id": instance_id,
                    "unit_name": unit_name,
                    "node_host": node_host,
                    "port_assigned": row["port_assigned"],
                } | result

        except Exception as exc:
            return {
                "engine": self._name,
                "instance_id": instance_id,
                "error": str(exc),
                "main_pid": None,
                "memory_mb": None,
                "restart_count": 0,
                "service_state": "unknown",
                "service_substate": "error",
            }

    def _check_remote_service(self, node_host, unit_name, node_user=None):
        """Check remote systemd service and process stats via ansible playbook.

        Uses instance_health_check for unified, interlock-aware health checks.
        """
        import json as _json

        try:
            from qr_api import _execute_playbook as _ep
            r = _ep("instance_health_check", resolver_type="playbook_id",
                    limit=node_host,
                    extra_vars={"inventory_host": node_host, "unit_name": unit_name},
                    action_type="health_check")

            if r.get("error"):
                return {
                    "service_state": "unknown", "service_substate": "ansible_error",
                    "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
                    "error": r["error"],
                }

            # Parse JSON from playbook debug msg
            svc_result = r.get("result", {})
            json_str = ""
            for play in svc_result.get("results", {}).get("plays", []):
                for task in play.get("tasks", []):
                    if "Output health check result" in task.get("task", {}).get("name", ""):
                        entry = task.get("results", [{}])[0]
                        json_str = entry.get("msg", "")

            if not json_str:
                return {
                    "service_state": "unknown", "service_substate": "no_output",
                    "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
                    "error": "Playbook returned no output",
                }

            data = _json.loads(json_str)
            memory_kb = int(data.get("memory_kb", 0))
            main_pid = int(data["main_pid"]) if data.get("main_pid") and data["main_pid"] not in ("0",) else None

            error = None
            state = data.get("service_state", "unknown")
            if state == "unknown" and main_pid is None:
                error = f"Service {unit_name} not found on {node_host}"

            return {
                "service_state": state,
                "service_substate": data.get("sub_state", "unknown"),
                "main_pid": main_pid,
                "memory_mb": round(memory_kb / 1024, 2) if memory_kb else 0.0,
                "restart_count": int(data.get("restart_count", 0)),
                "error": error,
            }

        except _json.JSONDecodeError:
            return {
                "service_state": "unknown", "service_substate": "parse_error",
                "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
                "error": f"Failed to parse playbook output: {json_str!r}",
            }
        except Exception as exc:
            return {
                "service_state": "unknown", "service_substate": "error",
                "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
                "restart_count": 0,
                "error": str(exc),
            }

    def query_status(self, instance_id, db_path=None):
        """Remote health check via ansible playbook.

        Uses instance_health_check for unified interlock-aware status checks.
        """
        if db_path is None:
            return {"alive": False, "latency_ms": None,
                    "error": "db_path required for remote query_status"}

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                row = conn.execute(
                    """SELECT i.state, n.hostname as node_host, e.name as engine_type_name
                       FROM instances i
                       LEFT JOIN nodes n ON i.node_id = n.id
                       JOIN engine_types e ON i.engine_type_id = e.id
                       WHERE i.id = ?""",
                    (instance_id,),
                ).fetchone()

            if row is None:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance {instance_id} not found"}

            node_host = row["node_host"] or QR_DEFAULT_LOCALHOST
            state = row["state"] or "unknown"
            eng = row["engine_type_name"] or "universal"

            if state not in ("running", "starting", "deployed", "stopped", "error",
                              "updating", "build_error", "configuring", "deploying",
                              "compiling"):
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance not active (state={state})"}

            unit_name = f"qr-{instance_id}-{eng}"
            from qr_api import _execute_playbook as _ep
            r = _ep("instance_health_check", resolver_type="playbook_id",
                    limit=node_host,
                    extra_vars={"inventory_host": node_host, "unit_name": unit_name},
                    action_type="health_check")

            if r.get("error"):
                return {"alive": False, "latency_ms": None,
                        "error": r["error"]}

            svc_result = r.get("result", {})
            service_state = "unknown"
            for play in svc_result.get("results", {}).get("plays", []):
                for task in play.get("tasks", []):
                    if "Output health check result" in task.get("task", {}).get("name", ""):
                        entry = task.get("results", [{}])[0]
                        msg = entry.get("msg", "{}")
                        try:
                            import json as _json
                            d = _json.loads(msg)
                            service_state = d.get("service_state", "unknown")
                        except Exception:
                            pass

            active = (service_state == "active")
            return {
                "alive": active,
                "latency_ms": None,
                "error": None if active else f"Service inactive on {node_host}",
            }

        except Exception as exc:
            return {"alive": False, "latency_ms": None,
                    "error": str(exc)}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Apply configuration to a universal instance.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters.
            db_path: Optional database path.

        Returns:
            dict with the updated configuration.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "config": config_dict, "applied": True}

    def get_config(self, instance_id, db_path=None):
        """Get current running config for a universal instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with current configuration from config_override.
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id, "config": {}}

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                row = conn.execute(
                    "SELECT config_override FROM instances WHERE id = ?",
                    (instance_id,),
                ).fetchone()

                if not row:
                    return {"engine": self._name, "instance_id": instance_id, "config": {}}

                import json as _json
                co = row["config_override"]
                if isinstance(co, str):
                    try:
                        co = _json.loads(co)
                    except Exception:
                        co = {}
                return {"engine": self._name, "instance_id": instance_id, "config": co or {}}

        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "config": {}, "_error": str(exc)}

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Execute a command on a universal engine instance.

        Supports both sync (instant feedback) and async modes based on
        the instance's config_override.instant_feedback setting.

        Args:
            instance_id: Integer primary key of the instance.
            command: Command string to execute.
            db_path: Optional database path.
            **kwargs: node_id, config_override (from handler), timeout.

        Returns:
            dict with execution result (different for sync vs async).
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "error": "db_path required"}

        # Get config_override from kwargs (passed by handler) or query DB
        co = kwargs.get("config_override")
        node_id = kwargs.get("node_id")

        if co is None:
            try:
                import sqlite3 as _sqlite3
                conn = _sqlite3.connect(db_path)
                conn.row_factory = _sqlite3.Row
                row = conn.execute(
                    "SELECT config_override FROM instances WHERE id = ?",
                    (instance_id,),
                ).fetchone()
                conn.close()
                if row:
                    co = row["config_override"] if isinstance(row["config_override"], dict) else {}
            except Exception:
                co = {}

        if not isinstance(co, dict):
            co = {}

        instant_fb = bool(co.get("instant_feedback", False))
        fb_timeout = int(co.get("feedback_timeout", 30))

        if instant_fb:
            return self._execute_sync(instance_id, co, db_path, timeout=fb_timeout, node_id=node_id)
        else:
            return self._execute_async(instance_id, command, co, db_path)

    def _execute_sync(self, instance_id, config_override, db_path, timeout=30, node_id=None):
        """Synchronous execution with timeout polling."""
        from db.adapters.nodes import get_node as _gn
        from lib.lib_ansible_runner import run_playbook
        import os as _os
        import subprocess as _sub
        import time

        if node_id is None:
            # Fallback: query DB for node_id if not provided by handler
            try:
                import sqlite3 as _sqlite3
                conn = _sqlite3.connect(db_path)
                conn.row_factory = _sqlite3.Row
                row = conn.execute(
                    "SELECT node_id FROM instances WHERE id = ?", (instance_id,),
                ).fetchone()
                conn.close()
                if row and row["node_id"] is not None:
                    node_id = int(row["node_id"])
            except Exception:
                pass

            if node_id is None:
                return {
                    "engine": self._name,
                    "instance_id": instance_id,
                    "error": "No target node",
                    "success": False,
                }

        nd = _gn(db_path, node_id)
        hostname = (nd.get("ansible_inventory_host") or
                    nd.get("hostname") or
                    nd.get("name")) if nd else None
        if not hostname:
            return {"engine": self._name, "instance_id": instance_id,
                    "error": "No hostname for node", "success": False}

        # Build the command to run on remote
        cmd_parts = []
        binary = config_override.get("binary_path", "")
        env_vars = config_override.get("env_vars", {}) or {}
        cli_args = config_override.get("cli_args", []) or []

        if binary:
            cmd_parts.append(binary)
            cmd_parts.extend(str(a) for a in cli_args)
        else:
            # Use command from request body (passed via extra_vars)
            cmd_parts.append("echo 'No binary_path configured; use ansible ad-hoc'")

        # Set env vars if any
        env_str = ""
        if env_vars:
            env_parts = [f'{k}="{v}"' for k, v in env_vars.items()]
            env_str = "export " + "; ".join(env_parts) + " && "

        full_cmd = env_str + " ".join(cmd_parts)

        inv_script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                  "..", "..", "lib", "qr_dynamic_inventory.py")

        start_time = time.time()
        try:
            result = _sub.run(
                [
                    "ansible", hostname, "-i", inv_script,
                    "-m", "shell",
                    "-a", full_cmd,
                    "-b",
                ],
                capture_output=True, text=True, timeout=timeout + 10,
            )

            elapsed_ms = int((time.time() - start_time) * 1000)

            return {
                "engine": self._name,
                "instance_id": instance_id,
                "success": not result.returncode,
                "exit_code": result.returncode,
                "stdout": (result.stdout or "").strip(),
                "stderr": (result.stderr or "").strip(),
                "duration_ms": elapsed_ms,
                "mode": "sync",
            }

        except _sub.TimeoutExpired:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {
                "engine": self._name,
                "instance_id": instance_id,
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "duration_ms": elapsed_ms,
                "mode": "sync",
            }

    def _execute_async(self, instance_id, command, config_override, db_path):
        """Async fire-and-forget execution via manage_instance.yml."""
        from db.adapters.instances import log_action
        import os as _os

        # Import _execute_playbook from quickrobot at call time to avoid circular imports
        try:
            from qr_api import _execute_playbook as _ep, _CONFIG
        except Exception:
            # Fallback: direct run_playbook with dynamic inventory if quickrobot not available
            from lib.lib_ansible_runner import run_playbook

            extra_vars = {
                "inventory_host": config_override.get("inventory_host", ""),
                "instance_id": instance_id,
                "engine_type": "universal",
                "action": "execute",
            }

            try:
                result = run_playbook(
                    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                  "..", "playbooks", "manage_instance.yml"),
                    inventory_path=None,
                    extra_vars=extra_vars,
                )

                log_action(db_path, instance_id, "execute",
                           "success" if not result.get("failed") else "failed")

                return {
                    "engine": self._name,
                    "instance_id": instance_id,
                    "success": not result.get("failed", False),
                    "mode": "async",
                }

            except Exception as exc:
                log_action(db_path, instance_id, "execute", "failed",
                           detail={"error": str(exc)})
                return {"engine": self._name, "instance_id": instance_id,
                        "error": str(exc), "success": False}

        # _execute_playbook available — use it
        extra_vars = {
            "inventory_host": config_override.get("inventory_host", ""),
            "instance_id": instance_id,
            "engine_type": "universal",
            "action": "execute",
        }

        try:
            playbook_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                          "..", "playbooks", "manage_instance.yml")
            r = _ep(playbook_path, resolver_type="file_path",
                    inventory_data=None,
                    extra_vars=extra_vars,
                    action_type="execute_instance")

            log_action(db_path, instance_id, "execute",
                       "success" if not r.get("failed", False) else "failed")

            return {
                "engine": self._name,
                "instance_id": instance_id,
                "success": r.get("success", False),
                "mode": "async",
            }

        except Exception as exc:
            log_action(db_path, instance_id, "execute", "failed",
                       detail={"error": str(exc)})
            return {"engine": self._name, "instance_id": instance_id,
                    "error": str(exc), "success": False}

    def list_resources(self, instance_id, db_path=None):
        """List available resources for a universal instance."""
        return {"engine": self._name, "instance_id": instance_id,
                "resources": []}

    def get_presets(self, engine_type_id, db_path=None):
        """Get presets for the universal engine type."""
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """Set the active preset for a universal instance."""
        return {"engine": self._name, "instance_id": instance_id,
                "preset_id": preset_id, "applied": True}

    def forward_request(self, instance_id, method, params=None, db_path=None):
        """Forward a request to a universal engine instance."""
        return {"engine": self._name, "instance_id": instance_id,
                "method": method, "params": params or {}, "result": None}

    def deploy(self, instance_id, db_path=None, **kwargs):
        """Deploy a universal engine instance via ansible playbook.

        Creates git clone (optional), venv, pip install, systemd unit, env file,
        and starts the service.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Database path.
            **kwargs: config_override, node_id, extra_vars for playbook.

        Returns:
            dict with execution result (success/failed + mode).
        """
        co = kwargs.get("config_override") or {}
        node_id = kwargs.get("node_id")

        if not isinstance(co, dict):
            co = {}

        # Resolve deploy playbook via _resolve_engine_playbook_by_id (mimic base pattern)
        try:
            from qr_api import _CONFIG
            from engine.base import BaseEngine as _Base
            from db.adapters.playbooks import resolve_playbook_by_id
            import os as _os

            pb_path = resolve_playbook_by_id("DEPLOY_UNIVERSAL_V1")
            if pb_path is None:
                return {"success": False, "error": "DEPLOY_PLAYBOOK_NOT_FOUND", "engine": self._name}

            extra_vars = {
                "instance_id": instance_id,
                "instance_name": co.get("name", f"universal-{instance_id}"),
                "install_dir": co.get("install_dir") or _os.path.join("/opt/quickrobot", co.get("name", f"universal-{instance_id}")),
                "git_url": co.get("git_url", ""),
                "requirements_file": co.get("requirements_file", "requirements.txt"),
                "start_command": co.get("start_command", ""),
                "binary_path": co.get("binary_path") or _os.path.join(co.get("install_dir", "/opt/quickrobot/universal") + "/venv/bin/python"),
                "cli_args": co.get("cli_args") or [],
                "env_vars": co.get("env_vars") or {},
                "user": co.get("user") or DEFAULT_ANSIBLE_USER,
                "start_after_deploy": bool(co.get("start_after_deploy", False)),
                "restart_policy": co.get("restart_policy", "no"),
                "start_on_boot": bool(co.get("start_on_boot", False)),
            }

            if node_id:
                from db.adapters.nodes import get_node as _gn
                nd = _gn(db_path, node_id)
                if nd:
                    # Prefer DNS hostname for stable SSH connections (AGENTS.md §9)
                    extra_vars["host"] = (nd.get("hostname") or nd.get("ipv4_address") or QR_DEFAULT_LOCALHOST).strip()
                    extra_vars["port"] = kwargs.get("port_assigned", 0)

            # Use _execute_playbook from quickrobot
            try:
                from qr_api import _execute_playbook as _ep
                r = _ep(pb_path, resolver_type="file_path",
                        inventory_data=kwargs.get("inventory_data"),
                        extra_vars=extra_vars,
                        action_type="deploy_instance")

                return {
                    "success": not r.get("failed", False),
                    "mode": "ansible",
                    "engine": self._name,
                    "result": r,
                }
            except Exception as exc:
                return {"success": False, "error": str(exc), "engine": self._name}

        except Exception as exc:
            return {"success": False, "error": f"Deploy setup failed: {exc}", "engine": self._name}

    def undeploy(self, instance_id, db_path=None, **kwargs):
        """Undeploy a universal engine instance via ansible playbook.

        Stops service, removes systemd unit + env file, optional cleanup of source dir.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Database path.
            **kwargs: config_override, node_id.

        Returns:
            dict with execution result (success/failed + mode).
        """
        co = kwargs.get("config_override") or {}
        if not isinstance(co, dict):
            co = {}

        try:
            from db.adapters.playbooks import resolve_playbook_by_id
            import os as _os

            pb_path = resolve_playbook_by_id("UNDEPLOY_UNIVERSAL_V1")
            if pb_path is None:
                return {"success": False, "error": "UNDEPLOY_PLAYBOOK_NOT_FOUND", "engine": self._name}

            extra_vars = {
                "instance_id": instance_id,
                "instance_name": co.get("name", f"universal-{instance_id}"),
                "install_dir": co.get("install_dir") or _os.path.join("/opt/quickrobot", co.get("name", f"universal-{instance_id}")),
                "clean_source_dir": bool(co.get("clean_source_dir", False)),
                "clean_venv": bool(co.get("clean_venv", False)),
            }

            try:
                from qr_api import _execute_playbook as _ep
                r = _ep(pb_path, resolver_type="file_path",
                        inventory_data=kwargs.get("inventory_data"),
                        extra_vars=extra_vars,
                        action_type="undeploy_instance")

                return {
                    "success": not r.get("failed", False),
                    "mode": "ansible",
                    "engine": self._name,
                    "result": r,
                }
            except Exception as exc:
                return {"success": False, "error": str(exc), "engine": self._name}

        except Exception as exc:
            return {"success": False, "error": f"Undeploy setup failed: {exc}", "engine": self._name}

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for universal instances (STATUS-1)."""
        from db.sqlite import pool

        with pool(db_path) as conn:
            inst = conn.execute(
                """SELECT i.id, i.name, i.state, i.port_assigned,
                          i.node_id, i.config_override,
                          e.name as engine_type_name,
                          n.hostname as node_hostname
                   FROM instances i
                   JOIN engine_types e ON i.engine_type_id = e.id
                   LEFT JOIN nodes n ON i.node_id = n.id
                   WHERE i.id = ?""",
                (instance_id,),
            ).fetchone()

        if not inst:
            return None

        engine_data = {"port_assigned": inst["port_assigned"], "node_hostname": inst["node_hostname"]}

        # Include config_override highlights for universal
        co_raw = inst.get("config_override") or {}
        if isinstance(co_raw, str):
            try:
                import json as _jc
                co_raw = _jc.loads(co_raw)
            except Exception:
                co_raw = {}
        if isinstance(co_raw, dict) and co_raw:
            engine_data["deploy_playbook"] = co_raw.get("deploy_playbook", "N/A")
            engine_data["start_command"] = co_raw.get("start_command", "")[:100]

        actions = cls._get_available_actions(inst["state"])
        warnings = []
        if inst["node_hostname"] and inst["state"] in ("running", "deployed"):
            warnings.append({"type": "info", "message": f"Running on {inst['node_hostname']}"})

        state_machine = cls.get_state_machine()
        valid_next = state_machine.get(inst["state"], [])

        return {
            "id": inst["id"],
            "state": inst["state"],
            "engine_type_name": inst["engine_type_name"],
            "engine_data": engine_data,
            "actions": actions,
            "warnings": warnings,
            "_meta": {
                "valid_next_states": valid_next,
                "is_transitioning": inst["state"] in ("configuring",),
            },
        }

    @classmethod
    def _get_available_actions(cls, state):
        """Map instance state to available actions."""
        action_map = {
            "unconfigured": [{"name": "deploy", "label": "Deploy"}],
            "configuring": [{"name": "restart", "label": "Restart"}],
            "deployed": [{"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "reconfigure", "label": "Reconfigure"}],
            "starting": [{"name": "stop", "label": "Stop"}],
            "running": [{"name": "stop", "label": "Stop"}, {"name": "restart", "label": "Restart"}, {"name": "reconfigure", "label": "Reconfigure"}],
            "stopping": [{"name": "start", "label": "Start"}],
            "stopped": [{"name": "start", "label": "Start"}, {"name": "rebuild", "label": "Rebuild"}],
            "error": [{"name": "start", "label": "Start"}, {"name": "deploy", "label": "Deploy"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "stop", "label": "Stop"}],
            "build_error": [{"name": "deploy", "label": "Deploy"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}],
            "timeout": [{"name": "deploy", "label": "Deploy"}],
        }
        return action_map.get(state, [])
