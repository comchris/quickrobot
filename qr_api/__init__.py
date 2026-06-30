"""
Route registration for quickrobot.

Imports all route handlers and registers them with the Flask app.
"""

import os
import sys
import json
import atexit
from flask import Flask, request, jsonify
from werkzeug.exceptions import NotFound, MethodNotAllowed

app = Flask(__name__)

import time as _time_mod
_START_TIME = _time_mod.time()

# Project root — PARENT of this package directory
# qr_api/__init__.py is at /path/to/quickrobot/qr_api/__init__.py
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Global config — mirrors the old quickrobot.py module-level _CONFIG
_CONFIG = {
    "db_path": os.path.join(_project_root, "data", "quickrobot.db"),
    "create_and_autodeploy": True,
    "clean_build_on_last_instance": False,
    "backup_dir": os.path.join(_project_root, "data", "_backups"),
    "max_backups": 5,
    "api_port": 8040,  # default; overridden by .quickrobot.env at startup
    "qr_env_config": {},
}

# Mode — single source of truth (set by phase0_mode_flags, default "prod")
# Always read from _CONFIG["pb_mode"] — never use a captured module-level copy.


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------

def _pid_file_path():
    """Get the path to the PID file."""
    db_dir = os.path.dirname(_CONFIG.get("db_path", "")) or "."
    return os.path.join(db_dir, "quickrobot.pid")


def _check_pid_file():
    """Check if a previous instance is running via PID file.

    Returns:
        tuple (pid, error_message). pid is None if no PID file or process dead.
    """
    pid_path = _pid_file_path()
    if not os.path.isfile(pid_path):
        return None, None
    try:
        with open(pid_path, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, IOError):
        return None, None
    try:
        import signal
        os.kill(pid, 0)
        port = _CONFIG["_last_port"]
        return pid, f"quickrobot already running on port {port} (PID {pid})."
    except OSError:
        return None, None


def _write_pid_file():
    """Write current process PID to the PID file."""
    pid_path = _pid_file_path()
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid_file():
    """Remove the PID file if it exists."""
    pid_path = _pid_file_path()
    try:
        os.remove(pid_path)
    except OSError:
        pass


def _kill_existing(port=None):
    """Kill the existing qr process identified by PID file.

    Sends SIGTERM first, waits up to 5 seconds, then SIGKILL if still alive.
    """
    pid_path = _pid_file_path()
    target_port = port if port is not None else _CONFIG["_last_port"]
    if not os.path.isfile(pid_path):
        return
    try:
        with open(pid_path, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, IOError):
        os.remove(pid_path)
        return

    print(f"Found existing process (PID {pid}) on port {target_port}")
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        import time
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.2)
            except OSError:
                print(f"Process {pid} terminated gracefully")
                break
        else:
            os.kill(pid, signal.SIGKILL)
            print(f"Process {pid} force-killed")
    except OSError:
        pass


# Re-export key functions from submodules for backward compat
from qr_api.lib_instances import _execute_playbook, deploy_instance

# ── Engine type constants — re-exported for backward compat ────────────
from lib.qr_engine_ids import (
    QR_ENGINE_API, QR_ENGINE_WEBUI, QR_ENGINE_MCP,
    QR_ENGINE_UNIVERSAL, QR_ENGINE_SUBPROCESS, QR_ENGINE_LLAMA_SERVER,
    QR_ENGINE_IPERF3, QR_ENGINE_LLAMA_RPC,
)

# System instance ID map
_SYSTEM_INSTANCE_ID_MAP = {
    "quickrobot-api": 1,
    "quickrobot-webui": 2,
    "quickrobot-mcp": 3,
    "quickrobot-scheduler": 4,
}


@app.errorhandler(404)
def _not_found(error):
    """Handle 404 errors."""
    return jsonify({"status": "error", "code": "RESOURCE_NOT_FOUND", "message": str(error)}), 404


@app.errorhandler(405)
def _method_not_allowed(error):
    """Handle 405 errors."""
    return jsonify({"status": "error", "code": "VALIDATION_ERROR", "message": str(error)}), 405


# Instance route handlers
from .routes_instances import (
    api_bind_rpc,
    api_cluster_bind,
    api_create_instance,
    api_cycle_split_mode,
    api_delete_instance,
    api_set_split_mode,
    api_deploy_instance,
     api_reconfigure_instance,
    api_deploy_preview,
    api_execute_instance,
    api_get_cli_flags,
    api_get_expert_split_config,
    api_get_gpu_override,
    api_set_gpu_override,
    api_get_instance,
    api_get_instance_status,
    api_instance_health,
    api_instance_journal,
    api_instance_logs,
    api_instance_status,
    api_list_instances,
    api_list_rpc_bindings,
    api_merged_config,
    # CONFIG-1 Phase 2: config-levels endpoints
    api_get_config_levels,
    api_set_config_level,
    api_delete_config_level,
    api_get_merged_config,
    api_query_status,
    api_restart_instance,
    api_restart_system_instance,
    api_rpccluster_bind,
    api_rpccluster_summary,
    api_rpccluster_unbind,
     api_run_client,
    api_set_cli_flags,
    api_set_herd_config,
    api_set_expert_split_config,
    api_set_draft,
    api_set_experts,
    api_set_split,
    api_start_instance,
    api_stop_instance,
    api_system_instance_status,
    api_toggle_test_mode,
    api_unbind_rpc,
    api_proxy_remote,
    api_undeploy_instance,
    api_update_instance,
    api_update_log_level,
    # Job & task query endpoints
    api_list_jobs,
    api_get_job,
    api_delete_job,
    api_delete_stale_jobs,
    api_list_tasks,
    api_get_task,
    api_cancel_task,
    api_delete_task,
    api_model_load_sse,
)

# Node and misc route handlers
from .routes_nodes import (
    api_ansible_actions,
    api_api_server_update_setting,
    api_app_status,
    api_batch_set_engine_config,
    api_checksum_diff,
    api_cleanup_null_logs,
    api_clear_all_models,
    api_clear_old_ansible_actions,
    api_qr_actions,
    api_clear_old_qr_actions,
    api_clear_results,
    api_clone_preset,

    api_create_model,
    api_create_model_global,
    api_create_node,
    api_create_preset,
    api_create_prompt,
    api_delete_benchmark_run,
    api_delete_config,
    api_delete_engine_config,
    api_delete_model,
    api_delete_node,

    api_delete_node_config,
    api_delete_playbook,
    api_delete_preset,
    api_delete_prompt,
    api_force_delete_instance,
    api_get_config,
    api_get_engine_config,
    api_get_model,
    api_get_model_global,
    api_get_node,

    api_get_preset,
    api_get_progress,
    api_get_prompt,
    api_get_result_detail,
    api_get_webui_settings,
    api_health_check,
    api_home,
    api_instance_rebuild,
    api_list_all_models,
    api_list_engines,

    api_list_models,
    api_model_active,
    api_list_nodes,
    api_list_playbooks,
    api_list_presets,
    api_list_prompts,
    api_list_results,
    api_list_system_engines,
    api_mcp_restart,
    api_mcp_settings,

    api_mcp_start,
    api_mcp_status,
    api_mcp_stop,
    api_mcp_update_setting,
    api_mcp_update_settings,
    api_node_apt_update,
    api_node_apt_upgrade,
    api_node_apt_update_upgrade,
    api_node_configs,
    api_node_discover,
    api_discover_local,
    api_node_ping,

    api_node_reboot,
    api_reset_node_build_state,
    api_node_shutdown,
    api_node_status,
    api_orphans,
    api_playbook_content,
    api_preset_restart_all,
    api_quickrobot_api_metrics,
    api_quickrobot_api_status,
    api_register_playbook,

    api_register_system_engine,
    api_rescan_playbooks,
    api_reset_playbook_counters,
    api_scan_models,
    api_scan_models_agnostic,
    api_set_config,
    api_set_engine_config,
    api_set_node_config,
    api_set_node_host_status,
    api_set_webui_settings,

    api_start_benchmark,
    api_update_model,
    api_update_model_global,
    api_update_node,
    api_update_playbook,
    api_update_preset,
    api_update_prompt,
    api_verify_checksum,
    api_web_server_restart,
    api_web_server_settings,

    api_web_server_start,
    api_web_server_status,
    api_web_server_stop,
    api_web_server_update_setting,
    api_web_server_update_settings,
)

# ── SCRIPT-1: Dynamic job scripting blueprint ──────────────────────────
from . import routes_scripts
app.register_blueprint(routes_scripts.bp)

def register_routes(app):
    """Register all route handlers with the Flask app.

    All 130 routes from quickrobot.py are registered here using
    app.add_url_rule().
    """

    app.add_url_rule("/api/v1/", "api_home", api_home, methods=["GET"])
    app.add_url_rule("/api/v1/ansible_actions", "api_ansible_actions", api_ansible_actions, methods=["GET"])
    app.add_url_rule("/api/v1/ansible_actions/clear-old", "api_clear_old_ansible_actions", api_clear_old_ansible_actions, methods=["POST"])
    app.add_url_rule("/api/v1/qr_actions", "api_qr_actions", api_qr_actions, methods=["GET"])
    app.add_url_rule("/api/v1/qr_actions/clear-old", "api_clear_old_qr_actions", api_clear_old_qr_actions, methods=["POST"])
    app.add_url_rule("/api/v1/app/status", "api_app_status", api_app_status, methods=["GET"])
    app.add_url_rule("/api/v1/benchmarks/prompts", "api_create_prompt", api_create_prompt, methods=["POST"])
    app.add_url_rule("/api/v1/benchmarks/prompts", "api_list_prompts", api_list_prompts, methods=["GET"])
    app.add_url_rule("/api/v1/benchmarks/prompts/<int:prompt_id>", "api_delete_prompt", api_delete_prompt, methods=["DELETE"])
    app.add_url_rule("/api/v1/benchmarks/prompts/<int:prompt_id>", "api_get_prompt", api_get_prompt, methods=["GET"])
    app.add_url_rule("/api/v1/benchmarks/prompts/<int:prompt_id>", "api_update_prompt", api_update_prompt, methods=["PUT"])
    app.add_url_rule("/api/v1/benchmarks/results", "api_clear_results", api_clear_results, methods=["DELETE"])
    app.add_url_rule("/api/v1/benchmarks/results", "api_list_results", api_list_results, methods=["GET"])
    app.add_url_rule("/api/v1/benchmarks/results/<run_id>", "api_delete_benchmark_run", api_delete_benchmark_run, methods=["DELETE"])
    app.add_url_rule("/api/v1/benchmarks/results/<run_id>", "api_get_result_detail", api_get_result_detail, methods=["GET"])
    app.add_url_rule("/api/v1/benchmarks/results/<run_id>/progress", "api_get_progress", api_get_progress, methods=["GET"])
    app.add_url_rule("/api/v1/benchmarks/run", "api_start_benchmark", api_start_benchmark, methods=["POST"])
    app.add_url_rule("/api/v1/config", "api_get_config", api_get_config, methods=["GET"])
    app.add_url_rule("/api/v1/config/<key>", "api_delete_config", api_delete_config, methods=["DELETE"])
    app.add_url_rule("/api/v1/config/<key>", "api_set_config", api_set_config, methods=["PUT"])
    app.add_url_rule("/api/v1/engine/<engine_type>/config", "api_get_engine_config", api_get_engine_config, methods=["GET"])
    app.add_url_rule("/api/v1/engine/<engine_type>/config/<key>", "api_delete_engine_config", api_delete_engine_config, methods=["DELETE"])
    app.add_url_rule("/api/v1/engine/<engine_type>/config/<key>", "api_set_engine_config", api_set_engine_config, methods=["PUT"])
    app.add_url_rule("/api/v1/engine/<engine_type>/config/batch", "api_batch_set_engine_config", api_batch_set_engine_config, methods=["POST"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models", "api_create_model", api_create_model, methods=["POST"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models", "api_list_models", api_list_models, methods=["GET"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models/<int:model_id>", "api_delete_model", api_delete_model, methods=["DELETE"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models/<int:model_id>", "api_get_model", api_get_model, methods=["GET"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models/<int:model_id>", "api_update_model", api_update_model, methods=["PUT"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models/<int:model_id>/verify-checksum", "api_verify_checksum", api_verify_checksum, methods=["POST"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models/checksum-diff", "api_checksum_diff", api_checksum_diff, methods=["GET"])
    app.add_url_rule("/api/v1/engine/<engine_type>/models/scan", "api_scan_models", api_scan_models, methods=["POST"])
    app.add_url_rule("/api/v1/engine/<engine_type>/presets", "api_create_preset", api_create_preset, methods=["POST"])
    app.add_url_rule("/api/v1/engine/<engine_type>/presets", "api_list_presets", api_list_presets, methods=["GET"])
    app.add_url_rule("/api/v1/engine/<engine_type>/presets/<int:preset_id>", "api_delete_preset", api_delete_preset, methods=["DELETE"])
    app.add_url_rule("/api/v1/engine/<engine_type>/presets/<int:preset_id>", "api_get_preset", api_get_preset, methods=["GET"])
    app.add_url_rule("/api/v1/engine/<engine_type>/presets/<int:preset_id>", "api_update_preset", api_update_preset, methods=["PUT"])
    app.add_url_rule("/api/v1/engine/<engine_type>/presets/<int:preset_id>/clone", "api_clone_preset", api_clone_preset, methods=["POST"])
    app.add_url_rule("/api/v1/engine/<engine_type>/presets/<int:preset_id>/restart_all", "api_preset_restart_all", api_preset_restart_all, methods=["POST"])
    app.add_url_rule("/api/v1/engine/quickrobot-api/config/<key>", "api_api_server_update_setting", api_api_server_update_setting, methods=["GET", "PUT"])
    app.add_url_rule("/api/v1/engines", "api_list_engines", api_list_engines, methods=["GET"])
    app.add_url_rule("/api/v1/engines/quickrobot-api/metrics", "api_quickrobot_api_metrics", api_quickrobot_api_metrics, methods=["GET"])
    app.add_url_rule("/api/v1/engines/quickrobot-api/status", "api_quickrobot_api_status", api_quickrobot_api_status, methods=["GET"])
    app.add_url_rule("/api/v1/engines/quickrobot-mcp/restart", "api_mcp_restart", api_mcp_restart, methods=["POST"])
    app.add_url_rule("/api/v1/engines/quickrobot-mcp/settings", "api_mcp_settings", api_mcp_settings, methods=["GET"])
    app.add_url_rule("/api/v1/engines/quickrobot-mcp/settings", "api_mcp_update_settings", api_mcp_update_settings, methods=["PUT"])
    app.add_url_rule("/api/v1/engines/quickrobot-mcp/settings/<key>", "api_mcp_update_setting", api_mcp_update_setting, methods=["GET", "PUT"])
    app.add_url_rule("/api/v1/engines/quickrobot-mcp/start", "api_mcp_start", api_mcp_start, methods=["POST"])
    app.add_url_rule("/api/v1/engines/quickrobot-mcp/status", "api_mcp_status", api_mcp_status, methods=["GET"])
    app.add_url_rule("/api/v1/engines/quickrobot-mcp/stop", "api_mcp_stop", api_mcp_stop, methods=["POST"])
    app.add_url_rule("/api/v1/engines/quickrobot-webui/restart", "api_web_server_restart", api_web_server_restart, methods=["POST"])
    app.add_url_rule("/api/v1/engines/quickrobot-webui/settings", "api_web_server_settings", api_web_server_settings, methods=["GET"])
    app.add_url_rule("/api/v1/engines/quickrobot-webui/settings", "api_web_server_update_settings", api_web_server_update_settings, methods=["PUT"])
    app.add_url_rule("/api/v1/engines/quickrobot-webui/settings/<key>", "api_web_server_update_setting", api_web_server_update_setting, methods=["GET", "PUT"])
    app.add_url_rule("/api/v1/engines/quickrobot-webui/start", "api_web_server_start", api_web_server_start, methods=["POST"])
    app.add_url_rule("/api/v1/engines/quickrobot-webui/status", "api_web_server_status", api_web_server_status, methods=["GET"])
    app.add_url_rule("/api/v1/engines/quickrobot-webui/stop", "api_web_server_stop", api_web_server_stop, methods=["POST"])
    app.add_url_rule("/api/v1/health/check", "api_health_check", api_health_check, methods=["POST"])
    app.add_url_rule("/api/v1/instances", "api_create_instance", api_create_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances", "api_list_instances", api_list_instances, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>", "api_delete_instance", api_delete_instance, methods=["DELETE"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>", "api_get_instance", api_get_instance, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/status", "api_get_instance_status", api_get_instance_status, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>", "api_update_instance", api_update_instance, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/bind-rpc", "api_bind_rpc", api_bind_rpc, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/bind-rpc/<int:rpc_id>", "api_unbind_rpc", api_unbind_rpc, methods=["DELETE"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/cli-flags", "api_get_cli_flags", api_get_cli_flags, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/cli-flags", "api_set_cli_flags", api_set_cli_flags, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/herd-config", "api_set_herd_config", api_set_herd_config, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/gpu-override", "api_get_gpu_override", api_get_gpu_override, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/gpu-override", "api_set_gpu_override", api_set_gpu_override, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/expert-split-config", "api_get_expert_split_config", api_get_expert_split_config, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/expert-split-config", "api_set_expert_split_config", api_set_expert_split_config, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/cluster-bind", "api_cluster_bind", api_cluster_bind, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/deploy", "api_deploy_instance", api_deploy_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/redeploy", "api_deploy_instance", api_deploy_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/reconfigure", "api_reconfigure_instance", api_reconfigure_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/deploy-preview", "api_deploy_preview", api_deploy_preview, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/draft", "api_set_draft", api_set_draft, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/execute", "api_execute_instance", api_execute_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/experts", "api_set_experts", api_set_experts, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/force-delete", "api_force_delete_instance", api_force_delete_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/health", "api_instance_health", api_instance_health, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/journal", "api_instance_journal", api_instance_journal, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/log-level", "api_update_log_level", api_update_log_level, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/logs", "api_instance_logs", api_instance_logs, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/merged-config", "api_merged_config", api_merged_config, methods=["GET"])
    # CONFIG-1 Phase 2: config-levels endpoints
    app.add_url_rule("/api/v1/instances/<int:inst_id>/config-levels", "api_get_config_levels", api_get_config_levels, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/config-levels/<int:level>", "api_set_config_level", api_set_config_level, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/config-levels/<int:level>", "api_delete_config_level", api_delete_config_level, methods=["DELETE"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/config-levels/merged", "api_get_merged_config", api_get_merged_config, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/query-status", "api_query_status", api_query_status, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/restart", "api_restart_instance", api_restart_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/restart_system", "api_restart_system_instance", api_restart_system_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/run_client", "api_run_client", api_run_client, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/split", "api_set_split", api_set_split, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/split-mode", "api_cycle_split_mode", api_cycle_split_mode, methods=["PATCH"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/split-mode", "api_set_split_mode", api_set_split_mode, methods=["PUT"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/start", "api_start_instance", api_start_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/status", "api_instance_status", api_instance_status, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/stop", "api_stop_instance", api_stop_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/system-status", "api_system_instance_status", api_system_instance_status, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/test_mode", "api_toggle_test_mode", api_toggle_test_mode, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/undeploy", "api_undeploy_instance", api_undeploy_instance, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/rebuild", "api_instance_rebuild", api_instance_rebuild, methods=["POST"])
    # Job & Task query endpoints
    app.add_url_rule("/api/v1/jobs", "api_list_jobs", api_list_jobs, methods=["GET"])
    app.add_url_rule("/api/v1/jobs/<int:job_id>", "api_get_job", api_get_job, methods=["GET"])
    app.add_url_rule("/api/v1/jobs/<int:job_id>", "api_delete_job", api_delete_job, methods=["DELETE"])
    app.add_url_rule("/api/v1/jobs/cleanup", "api_delete_stale_jobs", api_delete_stale_jobs, methods=["POST"])
    app.add_url_rule("/api/v1/tasks", "api_list_tasks", api_list_tasks, methods=["GET"])
    app.add_url_rule("/api/v1/tasks/<int:task_id>", "api_get_task", api_get_task, methods=["GET"])
    app.add_url_rule("/api/v1/tasks/<int:task_id>/cancel", "api_cancel_task", api_cancel_task, methods=["POST"])
    app.add_url_rule("/api/v1/tasks/<int:task_id>/delete", "api_delete_task", api_delete_task, methods=["POST"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/models-sse", "api_model_load_sse", api_model_load_sse, methods=["GET"])
    app.add_url_rule("/api/v1/instances/<int:inst_id>/jobs", "api_instance_jobs", api_list_jobs, methods=["GET"])
    app.add_url_rule("/api/v1/logs/cleanup-null", "api_cleanup_null_logs", api_cleanup_null_logs, methods=["POST"])
    app.add_url_rule("/api/v1/models", "api_create_model_global", api_create_model_global, methods=["POST"])
    app.add_url_rule("/api/v1/models", "api_list_all_models", api_list_all_models, methods=["GET"])
    app.add_url_rule("/api/v1/models/<int:model_id>", "api_get_model_global", api_get_model_global, methods=["GET"])
    app.add_url_rule("/api/v1/models/<int:model_id>", "api_update_model_global", api_update_model_global, methods=["PUT"])
    app.add_url_rule("/api/v1/models/clear-all", "api_clear_all_models", api_clear_all_models, methods=["POST"])
    app.add_url_rule("/api/v1/models/<int:model_id>/active", "api_model_active", api_model_active, methods=["PUT"])
    app.add_url_rule("/api/v1/models/scan", "api_scan_models_agnostic", api_scan_models_agnostic, methods=["POST"])
    app.add_url_rule("/api/v1/nodes", "api_create_node", api_create_node, methods=["POST"])
    app.add_url_rule("/api/v1/nodes", "api_list_nodes", api_list_nodes, methods=["GET"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>", "api_delete_node", api_delete_node, methods=["DELETE"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>", "api_get_node", api_get_node, methods=["GET"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>", "api_update_node", api_update_node, methods=["PUT"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/apt-update", "api_node_apt_update", api_node_apt_update, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/apt-upgrade", "api_node_apt_upgrade", api_node_apt_upgrade, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/apt-update-upgrade", "api_node_apt_update_upgrade", api_node_apt_update_upgrade, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/configs", "api_node_configs", api_node_configs, methods=["GET"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/configs/<key>", "api_delete_node_config", api_delete_node_config, methods=["DELETE"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/configs/<key>", "api_set_node_config", api_set_node_config, methods=["PUT"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/discover", "api_node_discover", api_node_discover, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/1/discover-local", "api_discover_local", api_discover_local, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/host-status", "api_set_node_host_status", api_set_node_host_status, methods=["PUT"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/ping", "api_node_ping", api_node_ping, methods=["GET"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/reboot", "api_node_reboot", api_node_reboot, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/reset-build-state", "api_reset_node_build_state", api_reset_node_build_state, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/shutdown", "api_node_shutdown", api_node_shutdown, methods=["POST"])
    app.add_url_rule("/api/v1/nodes/<int:node_id>/status", "api_node_status", api_node_status, methods=["GET"])
    app.add_url_rule("/api/v1/orphans", "api_orphans", api_orphans, methods=["GET"])
    app.add_url_rule("/api/v1/playbooks", "api_list_playbooks", api_list_playbooks, methods=["GET"])
    app.add_url_rule("/api/v1/playbooks", "api_register_playbook", api_register_playbook, methods=["POST"])
    app.add_url_rule("/api/v1/playbooks/<int:playbook_id>", "api_delete_playbook", api_delete_playbook, methods=["DELETE"])
    app.add_url_rule("/api/v1/playbooks/<int:playbook_id>", "api_update_playbook", api_update_playbook, methods=["PUT"])
    app.add_url_rule("/api/v1/playbooks/<int:playbook_id>/content", "api_playbook_content", api_playbook_content, methods=["GET"])
    app.add_url_rule("/api/v1/playbooks/rescan", "api_rescan_playbooks", api_rescan_playbooks, methods=["POST"])
    app.add_url_rule("/api/v1/playbooks/reset-counters", "api_reset_playbook_counters", api_reset_playbook_counters, methods=["POST"])
    app.add_url_rule("/api/v1/proxy/<path:subpath>", "api_proxy_remote", api_proxy_remote, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    app.add_url_rule("/api/v1/rpc-bindings", "api_list_rpc_bindings", api_list_rpc_bindings, methods=["GET"])
    app.add_url_rule("/api/v1/rpccluster/llama/<int:llama_id>/bind-rpc", "api_rpccluster_bind", api_rpccluster_bind, methods=["PUT"])
    app.add_url_rule("/api/v1/rpccluster/llama/<int:llama_id>/bind-rpc/<int:rpc_id>", "api_rpccluster_unbind", api_rpccluster_unbind, methods=["DELETE"])
    app.add_url_rule("/api/v1/rpccluster/summary", "api_rpccluster_summary", api_rpccluster_summary, methods=["GET"])
    app.add_url_rule("/api/v1/system-engines", "api_list_system_engines", api_list_system_engines, methods=["GET"])
    app.add_url_rule("/api/v1/system-engines", "api_register_system_engine", api_register_system_engine, methods=["POST"])
    app.add_url_rule("/api/v1/webui/settings", "api_get_webui_settings", api_get_webui_settings, methods=["GET"])
    app.add_url_rule("/api/v1/webui/settings", "api_set_webui_settings", api_set_webui_settings, methods=["POST"])


# ---------------------------------------------------------------------------
# Utility functions exported for lib/lib_startup_pipeline.py
# ---------------------------------------------------------------------------

def _let_config():
    """Cached engine type list for batch config lookups."""
    from db.adapters.engine_types import list_engine_types as _let
    return _let(_CONFIG["db_path"])


def _seed_presets(conn):
     """Insert default presets per engine type. Uses INSERT OR IGNORE."""
     # llama_server (id=21) — base preset with no model (fast deploy test)
     presets_llama = [
         (21, "1-NoModel-ROUER-Mode", "only for fast deploy test", json.dumps({"env":{"LLAMA_ARG_BATCH":"2048","LLAMA_ARG_CACHE_RAM":"2048","LLAMA_ARG_CTX_SIZE":"32768","LLAMA_ARG_FLASH_ATTN":"1","LLAMA_ARG_N_GPU_LAYERS":"100","LLAMA_ARG_N_PARALLEL":"1","LLAMA_ARG_UBATCH":"512"},"cli_opts":[]})),
     ]
     # rpc (id=22) — CLI args (-d device, -t threads) come from preset cli_opts via merge chain
     presets_rpc = [
         (22, "RPC-Default", "system", json.dumps({"env":{},"cli_opts":[]})),
         (22, "RPC-CPU-Default", "default", json.dumps({"env":{},"cli_opts":["-d","CPU"]})),
         (22, "RPC-Vulkan0-Default", "default", json.dumps({"env":{},"cli_opts":["-d","Vulkan0"]})),
         (22, "RPC-CUDA0-Default", "default", json.dumps({"env":{},"cli_opts":["-d","CUDA0"]})),
     ]
     # iperf3 (id=31) — from quickrobot v2 engine_type_id=7
     presets_iperf3 = [
         (31, "iperf3-Server", "server", json.dumps({"env":{"binary_path":"/usr/bin/iperf3"},"cli_opts":["-s","-B","0.0.0.0","-p","5201"],"model":{}})),
         (31, "iperf3-Client", "client", json.dumps({"env":{"binary_path":"/usr/bin/iperf3","target_host":"","target_port":"5201"},"cli_opts":["-c","{{ target_host }}","-p","{{ target_port }}","-t","30"],"model":{}})),
     ]
     # Insert presets — gpu_device column exists after migration 029
     for p in presets_llama + presets_rpc + presets_iperf3:
         conn.execute(
             "INSERT OR IGNORE INTO engine_presets (engine_type_id,name,category,config_template,gpu_device) VALUES (?,?,?,?,?)",
             (p[0], p[1], p[2], p[3], None),
         )
