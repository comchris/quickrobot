"""Node route handlers for quickrobot.

Split into feature-group submodules (see routes_nodes/ directory).
Re-exports preserved for backwards compatibility.
"""

from .node_lifecycle import (
    api_list_nodes, api_create_node, api_delete_node,
    api_get_node, api_update_node,
)
from .node_status import (
    api_node_status, api_set_node_host_status,
    api_node_shutdown, api_node_reboot,
)
from .node_config import (
    api_node_configs, api_set_node_config, api_delete_node_config,
)

__all__ = [
    'api_list_nodes', 'api_create_node', 'api_delete_node', 'api_get_node', 'api_update_node',
    'api_node_status', 'api_set_node_host_status',
    'api_node_shutdown', 'api_node_reboot',
    'api_node_configs', 'api_set_node_config', 'api_delete_node_config',
]

# Additional functions from original routes_nodes.py — imported from their correct submodules
from .system_mgmt import api_health_check
from .node_apt import (
    api_node_apt, api_node_ping,
)
