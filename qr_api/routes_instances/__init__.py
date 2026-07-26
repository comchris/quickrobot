"""Instance route handlers for quickrobot.

Split into feature-group submodules (see routes_instances/ directory).
Re-exports preserved for backwards compatibility.
"""

from .status_queries import (
    api_create_instance, api_list_instances, api_get_instance,
    api_update_instance, api_delete_instance,
)
from .deploy_lifecycle import (
    api_start_instance, api_stop_instance, api_restart_instance,
    api_deploy_instance, api_reconfigure_instance, api_undeploy_instance,
    api_execute_instance, api_run_client,
    api_update_log_level,
)
from .config_mgmt import (
    api_cycle_split_mode,
    api_set_draft, api_set_cli_flags, api_get_cli_flags,
    api_set_herd_config, api_get_gpu_override, api_set_gpu_override,
    api_patch_expert_split,
    api_get_instance_config, api_set_instance_config,
    api_deploy_preview, api_merged_config,
    api_get_config_levels, api_set_config_level, api_delete_config_level,
    api_get_merged_config,
)
from .rpccluster import (
    api_bind_rpc, api_unbind_rpc, api_list_rpc_bindings,
    api_cluster_bind, api_rpccluster_summary,
    api_rpccluster_bind, api_rpccluster_unbind,
)
from .jobs_tasks import (
    api_list_jobs, api_get_job, api_delete_job, api_delete_stale_jobs,
    api_list_tasks, api_get_task, api_cancel_task, api_delete_task,
)
from .instance_health import (
    api_instance_logs, api_instance_journal, api_instance_status,
    api_health_check_all, api_system_instance_status,
)
from .misc import api_model_load_sse
from .misc import (
    api_proxy_remote, api_restart_system_instance,
)

__all__ = [
    'api_create_instance', 'api_list_instances', 'api_get_instance',
    'api_update_instance', 'api_delete_instance',
    'api_start_instance', 'api_stop_instance', 'api_restart_instance',
    'api_deploy_instance', 'api_reconfigure_instance', 'api_undeploy_instance',
    'api_execute_instance', 'api_run_client',
    'api_update_log_level',
    'api_cycle_split_mode',
    'api_set_draft', 'api_set_cli_flags', 'api_get_cli_flags',
    'api_set_herd_config', 'api_get_gpu_override', 'api_set_gpu_override',
    'api_patch_expert_split',
    'api_get_instance_config', 'api_set_instance_config',
    'api_deploy_preview', 'api_merged_config',
    'api_get_config_levels', 'api_set_config_level', 'api_delete_config_level',
    'api_get_merged_config',
    'api_bind_rpc', 'api_unbind_rpc', 'api_list_rpc_bindings',
    'api_cluster_bind', 'api_rpccluster_summary',
    'api_rpccluster_bind', 'api_rpccluster_unbind',
    'api_list_jobs', 'api_get_job', 'api_delete_job', 'api_delete_stale_jobs',
    'api_list_tasks', 'api_get_task', 'api_cancel_task', 'api_delete_task',
    'api_model_load_sse',
    'api_instance_logs', 'api_instance_journal', 'api_instance_status',
    'api_health_check_all', 'api_system_instance_status',
    'api_proxy_remote', 'api_restart_system_instance',
]
