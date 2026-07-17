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

import logging

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_PORT_DEFAULTS
from engine.base import BaseEngine

logger = logging.getLogger(__name__)


CAPABILITIES = {
    "name": "llama_rpc",
    "display_name": "llama.cpp RPC",
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 99,
    "base_port": QR_ENGINE_PORT_DEFAULTS.get("llama_rpc", 9000),
    "sub_pages": [
        {"path": "/engines/llama_rpc/config", "label": "Config", "order": 1},
    ],
    "config_defaults": {
        "LLAMA_ARG_HOST": ("0.0.0.0", "Host to bind RPC server (0.0.0.0=all interfaces)"),
        "base_port": ("50052", "Base port for RPC server instances (sequential allocation)"),
        "binary_path": ("/opt/quickrobot/llama.cpp/build/bin/ggml-rpc-server", "Path to rpc-server binary (shared per-node)"),
        "git_clone_url": ("https://github.com/ggml-org/llama.cpp.git", "Source git repository URL"),
        "node_build_dir": ("/opt/quickrobot/llama.cpp/build", "Shared cmake build dir (per-node)"),
        "node_build_install_depends": ("gcc libssl-dev cmake libvulkan-dev libvulkan1 glslc spirv-headers vulkan-tools libvulkan-dev libvulkan1 glslc spirv-headers", "Additional apt packages for Vulkan support"),
        "node_build_run_cmd": ("cmake --build build --config Release -j 2", "CMake build command"),
        "node_build_set_cmd": ("cmake -B build -DGGML_RPC=ON -DGGML_NATIVE=ON -DGGML_CPU=ON -DLLAMA_OPENSSL=ON -DGGML_AVX2=ON -DGGML_VULKAN=ON", "CMake configure command"),
        "node_git_pull_cmd": ("git pull origin master", "Git pull command for source update"),
        "node_src_dir": ("/opt/quickrobot/llama.cpp", "Shared llama.cpp source dir (per-node)"),
        "restart_policy": ("no", "Systemd restart policy"),
        "skip_build": ("false", "Skip cmake build (use when binary already exists or to pin a version)"),
        "start_on_boot": ("false", "Enable systemd unit on boot (true/false)"),
    },
    "supported_jobs": ["deploy", "restart", "undeploy", "rebuild"],
    # env_builder: function name in lib.lib_cluster_env_builder that produces
    # merged_env + cli_args for config/start stages. Only llama_server and
    # llama_rpc have these; other engines use their own extra_vars paths.
    "env_builder": "build_rpc_server_env",
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
        # Allow recovery from deploying/configuring to running when health check confirms alive
        sm["deploying"].append("running")
        sm["configuring"].append("running")
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
                node_host = row["node_host"] or QR_DEFAULT_LOCALHOST
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

            node_host = row["node_host"] or QR_DEFAULT_LOCALHOST
            state = row["state"] or "unknown"

            if state not in ("running", "starting", "deployed", "stopped", "error",
                              "updating", "build_error", "configuring", "deploying",
                              "compiling"):
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

        Uses instance_health_check for unified interlock-aware health checks.
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
            from qr_api import _execute_playbook as _ep
            r = _ep("instance_health_check", resolver_type="playbook_id",
                    limit=node_host,
                    extra_vars={"inventory_host": node_host, "unit_name": unit_name},
                    node_id=_row["node_id"], instance_id=_row["id"],
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
                            d = _json.loads(msg)
                            service_state = d.get("service_state", "unknown")
                        except Exception as _e:
                            logger.debug("rpc health check JSON parse failed: %s", _e)
                            pass

            active = (service_state == "active")
            return {
                "alive": active,
                "latency_ms": None,
                "error": None if active else f"systemctl reports: {service_state or 'unknown'}",
            }

        except Exception as _e:
            logger.debug("rpc systemd health check failed: %s", _e)
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

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for llama_rpc instances (STATUS-1)."""
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
            "unconfigured":   [{"name": "deploy", "label": "Deploy"}, {"name": "delete", "label": "Delete"}],
            "configuring":    [{"name": "stop", "label": "Stop"}],
            "deployed":       [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
            "starting":       [{"name": "stop", "label": "Stop"}],
            "loading":        [{"name": "stop", "label": "Stop"}],
            "running":        [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "stop", "label": "Stop"}],
            "stopping":       [{"name": "start", "label": "Start"}],
            "stopped":        [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}],
            "error":          [{"name": "reconfig_restart", "label": "Reconfig/Restart"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "deploy", "label": "Deploy"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
            "deploying":      [{"name": "stop", "label": "Stop"}],
            "updating":       [],
            "compiling":      [],
            "build_error":    [{"name": "deploy", "label": "Deploy"}, {"name": "start", "label": "Start"}, {"name": "undeploy", "label": "Undeploy"}, {"name": "delete", "label": "Delete"}],
            "timeout":        [{"name": "deploy", "label": "Deploy"}],
            "test_mode":      [{"name": "stop", "label": "Stop"}],
        }
        return action_map.get(state, [])
