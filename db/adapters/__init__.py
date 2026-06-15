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

"""quickrobot — Database adapters package.

Each module provides CRUD + domain-specific functions for a single table.
All public functions return plain dicts or lists of dicts (never sqlite3.Row).
"""

from db.adapters.nodes import (
    add_node, get_node, list_nodes, update_node, delete_node,
    update_capabilities, update_status, discover_node,
)
from db.adapters.engine_types import (
    add_engine_type, get_engine_type, list_engine_types,
    update_engine_type, delete_engine_type,
)
from db.adapters.presets import (
    add_preset, get_preset, list_presets, update_preset,
    delete_preset, search_presets,
)
from db.adapters.models import (
    add_model, get_model, list_models, update_model_discovered,
    delete_model, scan_host_for_models,
)
from db.adapters.configs import (
    set_engine_config, get_engine_config, delete_engine_config,
    get_all_engine_configs, set_global_config, get_global_config,
    get_all_global_config, set_node_config, get_node_config,
    delete_node_config,
)
from db.adapters.instances import (
    create_instance, get_instance, list_instances, update_instance,
    transition_state, delete_instance, check_system_managed, merge_configs,
    assign_port, log_action, get_instance_logs, cleanup_old_logs,
)
from db.adapters.logs import (
    log_instance_action, get_instance_logs_paginated,
    cleanup_old_instance_logs, get_action_history,
)
from db.adapters.playbooks import (
    register_playbook, get_playbook_by_path,
    register_all_core_playbooks, resolve_playbook_by_tags,
    increment_usage_counter, increment_error_counter, list_playbooks,
)

__all__ = [
    # nodes
    "add_node", "get_node", "list_nodes", "update_node", "delete_node",
    "update_capabilities", "update_status", "discover_node",
    # engine_types
    "add_engine_type", "get_engine_type", "list_engine_types",
    "update_engine_type", "delete_engine_type",
    # presets
    "add_preset", "get_preset", "list_presets", "update_preset",
    "delete_preset", "search_presets",
    # models
    "add_model", "get_model", "list_models", "update_model_discovered",
    "delete_model", "scan_host_for_models",
    # configs
    "set_engine_config", "get_engine_config", "delete_engine_config",
    "get_all_engine_configs", "set_global_config", "get_global_config",
    "get_all_global_config", "set_node_config", "get_node_config",
    "delete_node_config",
    # instances
    "create_instance", "get_instance", "list_instances", "update_instance",
    "transition_state", "delete_instance", "check_system_managed", "merge_configs",
    "assign_port", "log_action", "get_instance_logs", "cleanup_old_logs",
    # logs
    "log_instance_action", "get_instance_logs_paginated",
    "cleanup_old_instance_logs", "get_action_history",
    # playbooks
    "register_playbook", "get_playbook_by_path",
    "register_all_core_playbooks", "resolve_playbook_by_tags",
    "increment_usage_counter", "increment_error_counter", "list_playbooks",
]
