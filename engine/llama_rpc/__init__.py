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

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_PORT_DEFAULTS, QR_ENGINE_LLAMA_RPC_NAME
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
    "undeploy_chain": [
        {"stage": "stop", "playbook": "service_stop"},
        {"stage": "undeploy", "playbook": "undeploy_rpc"},
        {"stage": "verify", "playbook": "check_undeploy"},
    ],
}


class RpcEngine(BaseEngine):
    """RPC engine that manages remote RPC service instances.

    Instances communicate via JSON-RPC over HTTP. Port range: 9000-9098.
    """

    STATE_EXTENSIONS = {
        "deployed": ["updating", "compiling", "stopping"],
        "running": ["updating", "compiling", "configuring"],
        "error": ["updating", "compiling"],
        "stopped": ["updating"],
        "updating": ["deployed", "build_error", "error", "timeout", "unconfigured", "running"],
        "compiling": ["deployed", "error", "timeout"],
        "build_error": ["updating", "running"],
        "deploying": ["running"],
        "configuring": ["running"],
    }

    def __init__(self):
        self._name = "llama_rpc"
        self._base_port = CAPABILITIES["base_port"]
        self._max_instances = CAPABILITIES["max_instances"]

    @classmethod
    def get_state_machine(cls):
        """State machine for rpc engine (same as llama_server — build-based)."""
        from lib.lib_engine_states import build_state_machine as _bsm
        return _bsm(cls.STATE_EXTENSIONS)

    def get_status(self, instance_id, db_path=None):
        """Get current status of an RPC engine instance.

        Returns canonical shape: {engine, instance_id, service_state, error}
        plus optional subsystem keys (unit_name, port_assigned, etc.).
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": None, "error": "db_path required for remote get_status"}

        try:
            from lib.lib_engine_status_query import query_systemd_status as _qs
            unit_builder = lambda row: f"qr-{instance_id}-{row['engine_type_name']}"
            return _qs(db_path, self._name, instance_id, unit_builder)
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

    # set_config, get_config inherited from BaseEngine (shared lib)

    def _check_remote_service(self, node_host, unit_name, node_user=None):
        """Check remote systemd service and process stats via ansible playbook.

        Delegates to shared lib_engine_health.check_remote_service().

        Args:
            node_host: Hostname or IP of the remote node.
            unit_name: Name of the systemd unit (e.g., 'qr-19-rpc').
            node_user: SSH username for the remote node.

        Returns:
            dict with keys: service_state, service_substate, main_pid,
                memory_mb, restart_count, error.
        """
        from lib.lib_engine_health import check_remote_service as _check
        return _check(node_host, unit_name, node_user)

    # execute, list_resources, get_presets, set_active_preset, forward_request inherited from BaseEngine (shared lib)

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for llama_rpc instances (STATUS-1).

        Delegates to shared build_instance_status() with rpc-specific extras.
        """
        result = cls.build_instance_status(db_path, instance_id)
        if not result:
            return None

        # Engine-specific: node hostname warning
        if result["engine_data"].get("node_hostname"):
            result["warnings"].append(
                {"type": "info", "message": f"Running on {result['engine_data']['node_hostname']}"}
            )

        return result

    @classmethod
    def build_instance_status(cls, db_path, instance_id):
        """Shared STATUS-1 base response (lib/lib_engine_status.build_instance_status).

        llama_rpc-specific: merges remote systemd service health into engine_data.
        """
        from lib.lib_engine_status import build_instance_status as _shared_build
        result = _shared_build(cls, db_path, instance_id)
        if not result:
            return None

        # Merge remote systemd service health into engine_data
        from lib.lib_engine_status_query import query_systemd_status as _qs
        svc = _qs(
            db_path, QR_ENGINE_LLAMA_RPC_NAME, instance_id,
            unit_name_builder=lambda r: f"qr-{instance_id}-llama_rpc",
        )
        if svc and not svc.get("error"):
            result["engine_data"].update({
                "service_state": svc.get("service_state"),
                "main_pid": svc.get("main_pid"),
                "memory_mb": svc.get("memory_mb"),
                "restart_count": svc.get("restart_count"),
            })

        return result

    @classmethod
    def _get_available_actions(cls, state):
        """Map instance state to available actions (shared lib module)."""
        from lib.lib_engine_actions import get_action_map
        return get_action_map(CAPABILITIES["name"]).get(state, [])
