"""Node route handlers for quickrobot.

Split into feature-group submodules (see routes_nodes/ directory).
Re-exports preserved for backwards compatibility.
"""

from .routes_nodes.node_lifecycle import api_list_nodes, api_create_node, api_delete_node, api_get_node, api_update_node
from .routes_nodes.node_status import api_node_status, api_set_node_host_status, api_node_shutdown, api_node_reboot, api_node_ping, api_node_discover
from .routes_nodes.node_config import api_node_configs, api_set_node_config, api_delete_node_config
from .routes_nodes.engine_mgmt import api_list_engines, api_get_engine_config, api_set_engine_config, api_delete_engine_config, api_batch_set_engine_config, _let_config, api_api_server_update_setting
from .routes_nodes.preset_mgmt import api_list_presets, api_create_preset, api_get_preset, api_update_preset, api_preset_restart_all, api_delete_preset, api_clone_preset, api_remove_empty_presets, api_remove_empty_presets_confirm
from .routes_nodes.model_mgmt import api_list_all_models, api_get_model_global, api_update_model_global, api_create_model_global, api_clear_all_models, api_model_active, api_scan_models_agnostic, api_checksum_diff, api_remove_missing_models, api_remove_missing_models_confirm
from .routes_nodes.model_ops import api_list_models, api_get_model, api_update_model, api_delete_model, api_create_model, api_clone_model, api_scan_models, api_verify_checksum
from .routes_nodes.misc_nodes import api_instance_rebuild, api_orphans, api_force_delete_instance, api_ansible_actions, api_qr_actions, api_clear_old_ansible_actions, api_clear_old_qr_actions, api_home

__all__ = [
    'api_list_nodes', 'api_create_node', 'api_delete_node', 'api_get_node', 'api_update_node',
    'api_node_status', 'api_set_node_host_status',
    'api_node_shutdown', 'api_node_reboot', 'api_node_ping', 'api_node_discover',
    'api_node_configs', 'api_set_node_config', 'api_delete_node_config',
    'api_list_engines', 'api_get_engine_config', 'api_set_engine_config',
    'api_delete_engine_config', 'api_batch_set_engine_config', 'api_api_server_update_setting',
    'api_list_presets', 'api_create_preset', 'api_get_preset', 'api_update_preset',
    'api_preset_restart_all', 'api_delete_preset', 'api_clone_preset',
    'api_remove_empty_presets', 'api_remove_empty_presets_confirm',
    'api_list_all_models', 'api_get_model_global', 'api_update_model_global',
    'api_create_model_global', 'api_clear_all_models', 'api_model_active',
    'api_scan_models_agnostic', 'api_checksum_diff',
    'api_remove_missing_models', 'api_remove_missing_models_confirm',
    'api_list_models', 'api_get_model', 'api_update_model', 'api_delete_model',
    'api_create_model', 'api_clone_model', 'api_scan_models', 'api_verify_checksum',
    'api_instance_rebuild', 'api_orphans', 'api_force_delete_instance',
    'api_ansible_actions', 'api_qr_actions', 'api_clear_old_ansible_actions',
    'api_clear_old_qr_actions', 'api_home',
]
