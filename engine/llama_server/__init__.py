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

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_PORT_DEFAULTS, QR_ENGINE_LLAMA_SERVER_NAME
from lib.lib_constants import DEFAULT_ANSIBLE_USER


CAPABILITIES = {
    "name": "llama_server",
    "display_name": "llama.cpp server",
    "supports_models": True,
    "supports_presets": True,
    "max_instances": 99,
    "base_port": QR_ENGINE_PORT_DEFAULTS.get("llama_server", 8080),
    "sub_pages": [
        {"path": "/engines/llama_server/config", "label": "Config", "order": 1},
        {"path": "/engines/llama_server/presets", "label": "Presets", "order": 2},
        {"path": "/engines/llama_server/models", "label": "Models", "order": 3},
    ],
    # --- engine config defaults (SSOT — replaces seed file INSERTs) ---
    "config_defaults": {
        "LLAMA_ARG_FIT": ("off", "Fit mode off for production (on=enable fit)"),
        "LLAMA_ARG_LOG_LEVEL": ("off", "Log verbosity: off (production), info (standard), debug (verbose stderr output via --verbose)"),
        "LLAMA_ARG_HOST": ("0.0.0.0", "Host to bind llama-server (0.0.0.0=all interfaces)"),
        "LLAMA_ARG_MMAP": ("false", "Memory map models"),
        "LLAMA_ARG_MODELS_DIR": ("", "Model directory for router mode — preset 1 (no model ID) uses this"),
        "LLAMA_ARG_PORT": ("8080", "Default port for llama-server instances"),
        "LLAMA_ARG_SEED": ("1337", "Random seed for sampling (LLAMA_ARG_SEED)"),
        "LLAMA_ARG_UI": ("true", "Enable llama.cpp built-in Web UI (valid: on/enabled/true/1 or off/disabled/false/0)"),
        "LLAMA_ARG_UI_MCP_PROXY": ("true", "Enable MCP CORS proxy in Web UI (valid: on/enabled/true/1 or off/disabled/false/0)"),
        "LLAMA_API_KEY": ("", "API key for llama.cpp server authentication (env: LLAMA_API_KEY)"),
        "binary_path": ("/opt/quickrobot/llama.cpp/build/bin/llama-server", "Path to llama-server binary (shared per-node)"),
        "git_clone_url": ("https://github.com/ggml-org/llama.cpp.git", "Source git repository URL"),
        "model_root_path": ("/mnt/llama/gguf/models", "Root path for model scan (searches this directory for .gguf files)"),
        "node_build_dir": ("/opt/quickrobot/llama.cpp/build", "Shared cmake build dir (per-node)"),
        "node_build_install_depends": ("gcc libssl-dev cmake libvulkan-dev libvulkan1 glslc spirv-headers vulkan-tools libvulkan-dev libvulkan1 glslc spirv-headers", "Additional apt packages for Vulkan support"),
        "node_build_run_cmd": ("cmake --build build --config Release -j 2", "CMake build command"),
        "node_build_set_cmd": ("cmake -B build -DGGML_RPC=ON -DGGML_NATIVE=ON -DGGML_CPU=ON -DLLAMA_OPENSSL=ON -DGGML_AVX2=ON -DGGML_VULKAN=ON", "CMake configure command"),
        "node_git_pull_cmd": ("git pull origin master", "Git pull command for source update"),
        "node_src_dir": ("/opt/quickrobot/llama.cpp", "Shared llama.cpp source dir (per-node)"),
        "restart_policy": ("no", "Systemd restart policy"),
        "skip_build": ("true", "Skip cmake build (use when binary already exists or to pin a version)"),
        "start_on_boot": ("false", "Enable systemd unit on boot (true/false)"),
    },
    "supported_jobs": ["deploy", "restart", "undeploy", "rebuild", "reconfigure"],
    # env_builder: function name in lib.lib_cluster_env_builder that produces
    # merged_env + cli_args for config/start stages. Only llama_server and
    # llama_rpc have these; other engines (iperf3, universal, subprocess) use
    # their own extra_vars paths and skip the config merge chain.
    "env_builder": "build_llama_server_env",
    "undeploy_chain": [
        {"stage": "stop", "playbook": "service_stop"},
        {"stage": "undeploy", "playbook": "undeploy_llama_server"},
        {"stage": "verify", "playbook": "check_undeploy"},
    ],
}


class LlamaServerEngine(BaseEngine):
    """Llama.cpp server engine for managing GPU inference instances.

    Instances communicate via llama.cpp HTTP API. Port range: 8080-8084
    (limited to 5 concurrent instances per node due to GPU memory).
    """

    STATE_EXTENSIONS = {
        "deployed": ["updating", "compiling", "stopping"],
        "running": ["updating", "compiling", "configuring"],
        "error": ["updating", "compiling"],
        "stopped": ["updating"],
        "updating": ["deployed", "build_error", "error", "timeout", "unconfigured", "running"],
        "compiling": ["deployed", "error", "timeout"],
        "build_error": ["updating", "running"],
        "starting": ["loading"],
        "loading": ["running", "error"],
        "deploying": ["running"],
        "configuring": ["running"],
    }

    def __init__(self):
        self._name = "llama_server"
        self._base_port = CAPABILITIES["base_port"]
        self._max_instances = CAPABILITIES["max_instances"]

    @classmethod
    def get_state_machine(cls):
        """State machine for llama_server engine.

        Extends base with build/update states (updating, compiling)
        and allows configuring from running (BC-1: config-only updates).
        """
        from lib.lib_engine_states import build_state_machine as _bsm
        return _bsm(cls.STATE_EXTENSIONS)

    def get_status(self, instance_id, db_path=None):
        """Get remote status of a llama.cpp server instance.

        Queries the systemd service state and process stats on the remote node.

        Returns:
            dict with keys: engine, instance_id, unit_name, service_state,
                main_pid, memory_mb, restart_count, error.
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "error": "db_path required for remote get_status"}

        try:
            from lib.lib_engine_status_query import query_systemd_status as _qs
            unit_builder = lambda row: f"qr-{instance_id}-{row['engine_type_name']}"
            return _qs(db_path, self._name, instance_id, unit_builder)
        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": "unknown", "error": str(exc),
                    "main_pid": None, "memory_mb": None,
                    "restart_count": 0, "service_substate": "error"}

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

    # set_config, get_config, execute inherited from BaseEngine (shared lib)

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

    # get_presets, set_active_preset inherited from BaseEngine (shared lib)

    # forward_request inherited from BaseEngine (shared lib)

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for llama_server instances (STATUS-1).

        Delegates to shared build_instance_status(), then adds llama_server
        specific data extras (running_job from log_entries) and warnings.
        """
        result = cls.build_instance_status(db_path, instance_id)
        if not result:
            return None

        # Engine-specific: running job status
        from db.sqlite import pool

        with pool(db_path) as conn:
            job = conn.execute(
                "SELECT status FROM log_entries WHERE instance_id=? AND status='running' AND parent_id IS NULL ORDER BY created_at DESC LIMIT 1",
                (instance_id,),
            ).fetchone()

        if job:
            result["engine_data"]["running_job"] = job["status"]

        # Engine-specific: node hostname warning
        if result["engine_data"].get("node_hostname"):
            result["warnings"].append(
                {"type": "info", "message": f"Running on {result['engine_data']['node_hostname']}"}
            )

        return result

    @classmethod
    def build_instance_status(cls, db_path, instance_id):
        """Shared STATUS-1 base response (lib/lib_engine_status.build_instance_status).

        llama_server-specific: merges remote systemd service health + running_job.
        """
        from lib.lib_engine_status import build_instance_status as _shared_build
        result = _shared_build(cls, db_path, instance_id)
        if not result:
            return None

        # Merge remote systemd service health into engine_data
        from lib.lib_engine_status_query import query_systemd_status as _qs
        svc = _qs(
            db_path, QR_ENGINE_LLAMA_SERVER_NAME, instance_id,
            unit_name_builder=lambda r: f"qr-{instance_id}-llama_server",
        )
        if svc and not svc.get("error"):
            result["engine_data"].update({
                "service_state": svc.get("service_state"),
                "main_pid": svc.get("main_pid"),
                "memory_mb": svc.get("memory_mb"),
                "restart_count": svc.get("restart_count"),
            })

        # Engine-specific: running job status
        from db.sqlite import pool
        with pool(db_path) as conn:
            job = conn.execute(
                "SELECT status FROM log_entries WHERE instance_id=? AND status='running' AND parent_id IS NULL ORDER BY created_at DESC LIMIT 1",
                (instance_id,),
            ).fetchone()
        if job:
            result["engine_data"]["running_job"] = job["status"]

        return result

    @classmethod
    def _get_available_actions(cls, state):
        """Map instance state to available actions (shared lib module)."""
        from lib.lib_engine_actions import get_action_map
        return get_action_map(CAPABILITIES["name"]).get(state, [])

    @classmethod
    def _get_warnings(cls, instance, service_info):
        """Generate warnings based on instance state and service info."""
        warnings = []
        if instance.get("node_hostname"):
            # Check if service is reported as unknown (possible stale state)
            if service_info and service_info.get("service_state") == "unknown":
                warnings.append({"type": "warning", "message": f"Service state unknown on {instance['node_hostname']}"})
        return warnings
