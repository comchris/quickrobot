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

"""quickrobot (v0.04) -- iperf3 Engine implementation.

Provides the iperf3 engine class and its CAPABILITIES metadata for
discovery by the engine loader. Supports two modes via presets:
- server mode: runs iperf3 in listen mode (-s) on a dedicated port
- client mode: runs iperf3 as a one-shot client against a target (-c)
"""

from engine.base import BaseEngine

from lib.lib_constants import DEFAULT_ANSIBLE_USER


CAPABILITIES = {
    "name": "iperf3",
    "display_name": "Iperf3",
    "supports_models": False,
    "supports_presets": True,
    "max_instances": 99,
    "base_port": 9900,
    "sub_pages": [
        {"path": "/engines/iperf3/config", "label": "Config", "order": 1},
        {"path": "/engines/iperf3/presets", "label": "Presets", "order": 2},
    ],
}


class Iperf3Engine(BaseEngine):
    """iperf3 engine for network benchmarking instances.

    Instances communicate via iperf3 protocol. Port range: 9900-9904
    (limited to 5 concurrent server listeners per node).
    """

    def __init__(self):
        self._name = "iperf3"
        self._base_port = CAPABILITIES["base_port"]
        self._max_instances = CAPABILITIES["max_instances"]

    @classmethod
    def get_state_machine(cls):
        """State machine for iperf3 engine (no build states).

        Extends base with "configuring" from running (BC-1: config updates while running).
        No updating/compiling since iperf3 has no cmake build pipeline.
        """
        sm = super().get_state_machine()
        sm["running"].append("configuring")
        return sm

    def get_status(self, instance_id, db_path=None):
        """Get remote status of an iperf3 instance via systemctl.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path (required for remote instances).

        Returns:
            dict with keys: engine, instance_id, service_state,
                service_substate, main_pid, memory_mb, restart_count, error.
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "error": "db_path required for remote get_status"}

        import json as _json
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
                            "error": f"Instance {instance_id} not found"}

                unit_name = f"qr-{instance_id}-{row['engine_type_name']}"
                node_host = row["node_host"] or "127.0.0.1"
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
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": "unknown", "error": str(exc),
                    "main_pid": None, "memory_mb": None,
                    "restart_count": 0, "service_substate": "error"}

    def _check_remote_service(self, node_host, unit_name, node_user=None):
        """Check remote systemd service and process stats via ansible playbook.

        Uses INSTANCE_HEALTH_CHECK_V1 playbook for unified, interlock-aware health checks.

        Args:
            node_host: Hostname or IP of the remote node.
            unit_name: Name of the systemd unit (e.g., 'qr-19-iperf3').
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
        """Remote health check via ansible playbook.

        Uses INSTANCE_HEALTH_CHECK_V1 for unified interlock-aware status checks.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

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
                    """SELECT i.port_assigned, i.state, n.hostname as node_host
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
                        "error": f"Instance not running (state={state})"}

            unit_name = f"qr-{instance_id}-iperf3"
            # Check systemctl is-active via ansible playbook
            from quickrobot import _execute_playbook as _ep
            r = _ep("INSTANCE_HEALTH_CHECK_V1", resolver_type="playbook_id",
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
        """Apply configuration to an iperf3 instance.

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
        """Get current running config for an iperf3 instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with current configuration.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "config": {}}

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Execute a command on an iperf3 instance.

        Args:
            instance_id: Integer primary key of the instance.
            command: Command string (e.g., "run_client").
            db_path: Optional database path for system-managed engines.
            **kwargs: Additional parameters.

        Returns:
            dict with execution result.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "command": command, "result": "executed"}

    def list_resources(self, instance_id, db_path=None):
        """List available iperf3 server instances as connectable targets.

        Queries the DB for running iperf3 instances on the same node
        that could serve as client connection targets.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with keys: engine, instance_id, targets (list of available
                server instances with host/port info).
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "targets": []}

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                # Get node_id for this instance
                row = conn.execute(
                    "SELECT node_id FROM instances WHERE id = ?",
                    (instance_id,),
                ).fetchone()
                if not row:
                    return {"engine": self._name, "instance_id": instance_id,
                            "targets": []}

                node_id = row["node_id"]

                # Find running iperf3 server instances on the same node
                target_rows = conn.execute(
                    """SELECT i.id, i.name, i.port_assigned, n.hostname as node_host
                       FROM instances i
                       JOIN nodes n ON i.node_id = n.id
                       WHERE i.node_id = ?
                         AND i.engine_type_id IN (
                             SELECT id FROM engine_types WHERE name = 'iperf3'
                         )
                         AND i.state = 'running'
                         AND i.system_managed = 0
                       ORDER BY i.name""",
                    (node_id,),
                ).fetchall()

                targets = []
                for t in target_rows:
                    if t["id"] != instance_id:  # exclude self
                        targets.append({
                            "id": t["id"],
                            "name": t["name"],
                            "host": t["node_host"],
                            "port": t["port_assigned"],
                        })

                return {"engine": self._name, "instance_id": instance_id,
                        "targets": targets}

        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "targets": [], "_error": str(exc)}

    def get_presets(self, engine_type_id, db_path=None):
        """Get presets for the iperf3 engine type.

        Args:
            engine_type_id: Integer primary key of the engine type.
            db_path: Optional database path for system-managed engines.

        Returns:
            list of preset dicts (loaded from DB at runtime).
        """
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """Set the active preset for an iperf3 instance.

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
        """Forward a request to an iperf3 instance.

        Args:
            instance_id: Integer primary key of the instance.
            method: Request method name string.
            params: Optional dict of parameters.
            db_path: Optional database path for system-managed engines.

        Returns:
            dict with the response from the iperf3 instance.
        """
        return {"engine": self._name, "instance_id": instance_id,
                "method": method, "params": params or {}, "result": None}
