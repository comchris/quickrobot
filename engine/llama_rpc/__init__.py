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

"""Quickrobot — LLAMA.RPC Engine implementation.

Provides the LLAMA.RPC engine class and its CAPABILITIES metadata for
discovery by the engine loader.
"""

from engine.base import BaseEngine


CAPABILITIES = {
    "name": "llama_rpc",
    "display_name": "LLAMA.RPC Server",
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 99,
    "base_port": 9000,
    "sub_pages": [
        {"path": "/engines/llama_rpc/config", "label": "Config", "order": 1},
    ],
}


class RpcEngine(BaseEngine):
    """RPC engine that manages remote RPC service instances.

    Instances communicate via JSON-RPC over HTTP. Port range: 9000-9098.
    """

    def __init__(self):
        self._name = "llama_rpc"
        self._base_port = CAPABILITIES["base_port"]
        self._max_instances = CAPABILITIES["max_instances"]

    @classmethod
    def get_state_machine(cls):
        """State machine for rpc engine (same as llama_server — build-based)."""
        sm = super().get_state_machine()
        sm["deployed"].extend(["updating", "compiling", "stopping"])
        sm["running"].extend(["updating", "compiling", "configuring"])
        sm["error"].extend(["updating", "compiling"])
        sm["stopped"].extend(["updating"])
        sm["updating"] = ["deployed", "build_error", "error", "timeout", "unconfigured", "running"]
        sm["compiling"] = ["deployed", "error", "timeout"]
        # Allow recovery from build_error to running when health check confirms alive
        sm["build_error"].extend(["updating", "running"])
        return sm

    def get_status(self, instance_id, db_path=None):
        """Get current status of an RPC engine instance.

        Returns canonical shape: {engine, instance_id, service_state, error}
        plus optional subsystem keys (unit_name, port_assigned, etc.).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with canonical status shape.
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": None, "error": "db_path required for remote get_status"}

        import json as _json
        from engine.base import build_canonical_status as _bcs
        from lib.lib_constants import DEFAULT_ANSIBLE_USER
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
                    return {"engine": self._name, "instance_id": instance_id,
                            "service_state": None, "error": f"Instance {instance_id} not found"}

                unit_name = f"qr-{instance_id}-{row['engine_type_name']}"
                node_host = row["node_host"] or "127.0.0.1"
                node_user = (row["node_user"] if row["node_user"] else None) or DEFAULT_ANSIBLE_USER

                result = self._check_remote_service(node_host, unit_name, node_user)

                return _bcs(self._name, instance_id,
                            service_state=result.get("service_state"),
                            error=result.get("error") or None,
                            unit_name=unit_name, node_host=node_host,
                            port_assigned=row["port_assigned"]) | result

        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": "unknown", "error": str(exc),
                    "main_pid": None, "memory_mb": None,
                    "restart_count": 0}

    def query_status(self, instance_id, db_path=None):
        """Remote health check via Ansible systemctl is-active.

        Always uses systemd check — RPC ports are occupied by llama-server
        tensor_split bindings, so HTTP JSON-RPC is unreliable for all RPC
        instances (both standalone and cluster-bound).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path for remote instances.

        Returns:
            dict with keys: alive (bool), latency_ms (float|None), error (str|None).
        """
        if db_path is None:
            return {"alive": False, "latency_ms": None,
                    "error": "db_path required for remote query_status"}

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                row = conn.execute(
                    """SELECT i.state, n.hostname as node_host
                       FROM instances i
                       LEFT JOIN nodes n ON i.node_id = n.id
                       WHERE i.id = ?""",
                    (instance_id,),
                ).fetchone()

            if row is None:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance {instance_id} not found"}

            node_host = row["node_host"] or "127.0.0.1"
            state = row["state"] or "unknown"

            if state not in ("running", "starting", "deployed", "stopped", "error",
                              "updating", "build_error", "configuring", "deploying",
                              "compiling", "loading"):
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance not active (state={state})"}

            # Always use systemd check — RPC ports occupied by tensor_split
            result = self._check_rpc_systemd(node_host, instance_id, db_path)
            if result is not None:
                return result
            return {"alive": False, "latency_ms": None,
                    "error": "systemd check returned no result"}

        except Exception as exc:
            return {"alive": False, "latency_ms": None,
                    "error": str(exc)}

    def _check_rpc_systemd(self, node_host, instance_id, db_path):
        """Check RPC service status via centralized ansible playbook.

        Uses INSTANCE_HEALTH_CHECK_V1 for unified interlock-aware health checks.
        Primary health check — RPC ports are occupied by llama-server tensor_split,
        so HTTP JSON-RPC is unreliable.

        Args:
            node_host: Hostname of the remote node.
            instance_id: Integer primary key of the RPC instance.
            db_path: Path to the SQLite database.

        Returns:
            dict with alive/latency/error, or None if check fails.
        """
        import json as _json
        from db.sqlite import pool

        try:
            with pool(db_path) as _conn:
                _row = _conn.execute(
                    "SELECT id, node_id FROM instances WHERE id = ?",
                    (instance_id,),
                ).fetchone()
            if not _row:
                return None

            unit_name = f"qr-{_row['id']}-llama_rpc"
            from quickrobot import _execute_playbook as _ep
            r = _ep("INSTANCE_HEALTH_CHECK_V1", resolver_type="playbook_id",
                    limit=node_host,
                    extra_vars={"inventory_host": node_host, "unit_name": unit_name},
                    node_id=_row["node_id"], instance_id=_row["id"],
                    action_type="llama_rpc_health_check")

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
                            d = _json.loads(msg)
                            service_state = d.get("service_state", "unknown")
                        except Exception:
                            pass

            active = (service_state == "active")
            return {
                "alive": active,
                "latency_ms": None,
                "error": None if active else f"systemctl reports: {service_state or 'unknown'}",
            }

        except Exception:
            return {"alive": False, "latency_ms": None,
                    "error": "_check_rpc_systemd failed"}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Apply configuration to an RPC engine instance.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with the updated configuration.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "config": config_dict, "applied": True}

    def get_config(self, instance_id, db_path=None):
        """Get current running config for an RPC instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with current configuration.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "config": {}}

    def _check_remote_service(self, node_host, unit_name, node_user=None):
        """Check remote systemd service and process stats via ansible playbook.

        Uses INSTANCE_HEALTH_CHECK_V1 playbook for unified, interlock-aware health checks.

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
            from quickrobot import _execute_playbook as _ep
            r = _ep("INSTANCE_HEALTH_CHECK_V1", resolver_type="playbook_id",
                    limit=node_host,
                    extra_vars={"inventory_host": node_host, "unit_name": unit_name},
                    action_type="health_check")

            if r.get("error"):
                return {
                    "service_state": "unknown", "service_substate": "ansible_error",
                    "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
                    "error": r["error"],
                }

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

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Execute a command on the RPC engine.

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
        """List available resources for an RPC instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with models and presets listings.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "models": [], "presets": []}

    def get_presets(self, engine_type_id, db_path=None):
        """Get presets for the RPC engine type.

        Args:
            engine_type_id: Integer primary key of the engine type.
            db_path: Optional database path for system-managed engines.

        Returns:
            list of preset dicts (empty in Phase 1 -- loaded from DB at runtime).
        """
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """Set the active preset for an RPC instance.

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
        """Forward an RPC request to a running instance.

        Args:
            instance_id: Integer primary key of the instance.
            method: RPC method name string.
            params: Optional dict of parameters.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with the response from the remote engine.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "method": method, "params": params or {}, "result": None}
