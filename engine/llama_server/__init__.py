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

"""Quickrobot — LLAMA.cpp Server engine implementation.

Provides the llama_server engine class and its CAPABILITIES metadata for
discovery by the engine loader.
"""

from engine.base import BaseEngine

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_PORT_DEFAULTS
from lib.lib_constants import DEFAULT_ANSIBLE_USER


CAPABILITIES = {
    "name": "llama_server",
    "display_name": "LLAMA.cpp",
    "supports_models": True,
    "supports_presets": True,
    "max_instances": 99,
    "base_port": QR_ENGINE_PORT_DEFAULTS.get("llama_server", 8080),
    "sub_pages": [
        {"path": "/engines/llama_server/config", "label": "Config", "order": 1},
        {"path": "/engines/llama_server/presets", "label": "Presets", "order": 2},
        {"path": "/engines/llama_server/models", "label": "Models", "order": 3},
    ],
}


class LlamaServerEngine(BaseEngine):
    """Llama.cpp server engine for managing GPU inference instances.

    Instances communicate via llama.cpp HTTP API. Port range: 8080-8084
    (limited to 5 concurrent instances per node due to GPU memory).
    """

    def __init__(self):
        self._name = "llama_server"
        self._base_port = CAPABILITIES["base_port"]
        self._max_instances = CAPABILITIES["max_instances"]

    @classmethod
    def get_state_machine(cls):
        """State machine for llama_server engine.

        Extends base machine with build/update states (updating, compiling)
        and allows configuring from running (BC-1: config-only updates).
        """
        sm = super().get_state_machine()
        # Add build-related states
        sm["deployed"].extend(["updating", "compiling", "stopping"])
        sm["running"].extend(["updating", "compiling", "configuring"])
        sm["error"].extend(["updating", "compiling"])
        sm["stopped"].extend(["updating"])
        sm["updating"] = ["deployed", "build_error", "error", "timeout", "unconfigured", "running"]
        sm["compiling"] = ["deployed", "error", "timeout"]
        # Allow recovery from build_error to running when health check confirms alive
        sm["build_error"].extend(["updating", "running"])
        # Loading state: model load in progress after start/restart for llama_server.
        sm["starting"].append("loading")
        sm["loading"] = ["running", "error"]
        # Allow recovery from deploying/configuring to running when health check confirms alive
        sm["deploying"].append("running")
        sm["configuring"].append("running")
        return sm

    def get_status(self, instance_id, db_path=None):
        """Get remote status of a llama.cpp server instance.

        Queries the systemd service state and process stats on the remote node.

        Returns:
            dict with keys:
                engine (str): engine name
                instance_id (int): instance ID
                unit_name (str): systemd unit name
                service_state (str): active/inactive/activating/degrading
                service_substate (str): detailed substate
                main_pid (int|None): PID of the main process
                restart_count (int): number of times the service has restarted
                memory_mb (float|None): RSS memory in MB
                error (str|None): error message if query failed
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "error": "db_path required for remote get_status"}

        import json as _json
        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                # Get instance info
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
                    return {"engine": self._name, "instance_id": instance_id,
                            "error": f"Instance {instance_id} not found"}

                unit_name = f"qr-{instance_id}-{row['engine_type_name']}"
                node_host = row["node_host"] or QR_DEFAULT_LOCALHOST
                node_user = (row["node_user"] if row["node_user"] else None) or DEFAULT_ANSIBLE_USER
                engine_type = row["engine_type_name"]

                # Use ssh to check systemd service + process stats
                result = self._check_remote_service(node_host, unit_name, node_user)

                return {
                    "engine": self._name,
                    "instance_id": instance_id,
                    "unit_name": unit_name,
                    "node_host": node_host,
                    "port_assigned": row["port_assigned"],
                } | result

        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": "unknown", "error": str(exc),
                    "main_pid": None, "memory_mb": None,
                    "restart_count": 0, "service_substate": "error"}

    def _check_remote_service(self, node_host, unit_name, node_user=None):
        """Check remote systemd service and process stats via ansible playbook.

        Uses instance_health_check playbook for unified, interlock-aware health checks.

        Args:
            node_host: Hostname or IP of the remote node.
            unit_name: Name of the systemd unit (e.g., 'qr-19-rpc').
            node_user: SSH username for the remote node (defaults to DEFAULT_ANSIBLE_USER).

        Returns:
            dict with keys: service_state, service_substate, main_pid,
                memory_mb, restart_count, error.
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
                "error": str(exc),
            }

    def query_status(self, instance_id, db_path=None):
        """Remote health check for a llama.cpp server instance.

        Queries the /health endpoint on the target server.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with keys: alive (bool), latency_ms (float|None), error (str|None).
        """
        import urllib.request as _ur
        import time as _time

        if db_path is None:
            return {"alive": False, "latency_ms": None,
                    "error": "db_path required for remote query_status"}

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                row = conn.execute(
                    """SELECT i.port_assigned, i.state, n.hostname as node_host
                       FROM instances i
                       LEFT JOIN nodes n ON i.node_id = n.id
                       WHERE i.id = ?""",
                    (instance_id,),
                ).fetchone()

            if row is None:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance {instance_id} not found"}

            port = row["port_assigned"]
            node_host = row["node_host"] or QR_DEFAULT_LOCALHOST
            state = row["state"] or "unknown"

            if state not in ("running", "starting", "deployed", "stopped", "error",
                              "updating", "build_error", "configuring", "deploying",
                              "compiling", "loading") or not port:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance not active (state={state})"}

            url = f"http://{node_host}:{port}/health"
            start = _time.time()
            resp = _ur.urlopen(url, timeout=5)
            latency = (_time.time() - start) * 1000
            body = resp.read().decode("utf-8")
            import re as _re
            model_loading = bool(_re.search(
                r'model is loading|loading.*please wait', body, _re.IGNORECASE))
            result = {"alive": True, "latency_ms": round(latency, 2), "error": None}
            if model_loading:
                result["model_loading"] = True
            return result

        except Exception as exc:
            # HTTP check failed — fall back to systemd service state.
            # systemd check distinguishes "active" (server loading) from
            # "inactive/failed" (crashed) — replaces old grace-period approach.
            unit_name = f"qr-{instance_id}-llama_server.service"
            svc = self._check_remote_service(node_host, unit_name)
            if svc.get("service_state") == "active":
                return {"alive": True, "latency_ms": None, "error": None,
                        "note": "alive via systemd (HTTP not responding — model may be loading)"}
            elif svc.get("service_state") in ("inactive", "failed", "deactivating"):
                return {"alive": False, "latency_ms": None,
                        "error": f"systemd {svc['service_state']} (HTTP: {exc})"}
            elif svc.get("error"):
                return {"alive": False, "latency_ms": None,
                        "error": f"systemd check failed: {svc['error']} (HTTP: {exc})"}
            # HTTP failed and systemd check inconclusive — assume dead.
            # Grace period deprecated (2026-06-26): api_query_status() no longer applies timer.
            return {"alive": False, "latency_ms": None,
                    "error": str(exc)}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Apply configuration to a llama.cpp server instance.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters
                (gpu_layers, ctx_size, threads, model_path, etc.).
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with the updated configuration.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "config": config_dict, "applied": True}

    def get_config(self, instance_id, db_path=None):
        """Get current running config for a llama.cpp server instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with current configuration.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "config": {}}

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Execute a command on a llama.cpp server instance.

        Args:
            instance_id: Integer primary key of the instance.
            command: Command string or dict.
            db_path: Optional database path for system-managed engines.
            **kwargs: Additional parameters.

        Returns:
            dict with execution result.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "command": command, "result": "executed"}

    def list_resources(self, instance_id, db_path=None):
        """List available models and presets for the llama_server engine.

        Returns:
            dict with keys:
                engine (str): engine name
                instance_id (int): instance ID
                models (list[dict]): from engine_models table for this engine_type
                presets (list[dict]): from engine_presets table for this engine_type
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "models": [], "presets": []}

        import json as _json
        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                # Get engine_type_id for this instance
                row = conn.execute(
                    "SELECT engine_type_id FROM instances WHERE id = ?",
                    (instance_id,),
                ).fetchone()
                if not row:
                    return {"engine": self._name, "instance_id": instance_id,
                            "models": [], "presets": []}

                engine_type_id = row["engine_type_id"]

                # Models from engine_models table (shared across all engine types)
                model_rows = conn.execute(
                    "SELECT id, name, path, size_bytes, last_modified, host_id, discovered "
                    "FROM engine_models WHERE engine_type_id = ? ORDER BY name",
                    (engine_type_id,),
                ).fetchall()
                models = []
                for m in model_rows:
                    models.append({
                        "id": m["id"],
                        "name": m["name"],
                        "path": m["path"],
                        "size_bytes": m["size_bytes"] or 0,
                        "last_modified": m["last_modified"],
                        "host_id": m["host_id"],
                        "discovered": bool(m["discovered"]),
                    })

                # Presets from engine_presets table (engine-specific)
                preset_rows = conn.execute(
                    "SELECT id, name, category, config_template FROM "
                    "engine_presets WHERE engine_type_id = ? ORDER BY name",
                    (engine_type_id,),
                ).fetchall()
                presets = []
                for p in preset_rows:
                    try:
                        template = _json.loads(p["config_template"]) if p["config_template"] else {}
                    except (_json.JSONDecodeError, TypeError):
                        template = {}
                    presets.append({
                        "id": p["id"],
                        "name": p["name"],
                        "category": p["category"],
                        "config_template": template,
                    })

                return {"engine": self._name, "instance_id": instance_id,
                        "models": models, "presets": presets}

        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "models": [], "presets": [], "_error": str(exc)}

    def get_presets(self, engine_type_id, db_path=None):
        """Get presets for the llama_server engine type.

        Args:
            engine_type_id: Integer primary key of the engine type.
            db_path: Optional database path for system-managed engines.

        Returns:
            list of preset dicts (loaded from DB at runtime).
        """
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """Set the active preset for a llama.cpp server instance.

        Args:
            instance_id: Integer primary key of the instance.
            preset_id: Integer primary key of the target preset.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with updated preset assignment.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "preset_id": preset_id, "applied": True}

    def forward_request(self, instance_id, method, params=None, db_path=None):
        """Forward a request to a running llama.cpp server instance.

        Args:
            instance_id: Integer primary key of the instance.
            method: Request method name string.
            params: Optional dict of parameters.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with the response from the llama.cpp server.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "method": method, "params": params or {}, "result": None}

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for llama_server instances (STATUS-1).

        Returns a standardized dict with engine_data, available actions,
        warnings, and meta info for WebUI rendering.

        Args:
            db_path: Path to the SQLite database.
            instance_id: Integer primary key of the instance.

        Returns:
            dict with keys: id, state, engine_type_name, engine_data,
                actions, warnings, _meta.
        """
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

            # Get recent job status for running builds
            job = conn.execute(
                "SELECT status FROM jobs WHERE instance_id=? AND status='running' ORDER BY created_at DESC LIMIT 1",
                (instance_id,),
            ).fetchone()

        if not inst:
            return None

        engine_data = {
            "port_assigned": inst["port_assigned"],
            "node_hostname": inst["node_hostname"],
        }

        # Add model info if available

        # Note about running job (build in progress)
        if job:
            engine_data["running_job"] = job["status"]

        # Build available actions from state machine
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
                "is_transitioning": inst["state"] in ("configuring", "deploying", "updating", "compiling", "starting", "stopping"),
            },
        }

    @classmethod
    def _get_available_actions(cls, state):
        """Map instance state to available actions."""
        action_map = {
            "unconfigured": [{"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
            "deployed": [{"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "reconfigure", "label": "Reconfigure"}, {"name": "delete", "label": "Delete"}],
            "starting": [{"name": "stop", "label": "Stop"}],
            "loading": [{"name": "stop", "label": "Stop"}],
            "running": [{"name": "stop", "label": "Stop"}, {"name": "restart", "label": "Restart"}, {"name": "reconfigure", "label": "Reconfigure"}],
            "stopping": [{"name": "start", "label": "Start"}],
            "stopped": [{"name": "start", "label": "Start"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "reconfigure", "label": "Reconfigure"}, {"name": "deploy", "label": "Deploy"}, {"name": "delete", "label": "Delete"}],
            "error": [{"name": "start", "label": "Start"}, {"name": "deploy", "label": "Deploy"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "stop", "label": "Stop"}, {"name": "delete", "label": "Delete"}],
            "configuring": [{"name": "stop", "label": "Stop"}],
            "deploying": [{"name": "stop", "label": "Stop"}],
            "updating": [],
            "compiling": [],
            "build_error": [{"name": "deploy", "label": "Deploy"}, {"name": "start", "label": "Start"}, {"name": "delete", "label": "Delete"}],
            "timeout": [{"name": "deploy", "label": "Deploy"}],
            "test_mode": [{"name": "stop", "label": "Stop"}],
        }
        return action_map.get(state, [])

    @classmethod
    def _get_warnings(cls, instance, service_info):
        """Generate warnings based on instance state and service info."""
        warnings = []
        if instance.get("node_hostname"):
            # Check if service is reported as unknown (possible stale state)
            if service_info and service_info.get("service_state") == "unknown":
                warnings.append({"type": "warning", "message": f"Service state unknown on {instance['node_hostname']}"})
        return warnings
