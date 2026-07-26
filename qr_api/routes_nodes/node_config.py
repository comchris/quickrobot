from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG


def api_node_configs(node_id):
    """List all node config values (for all engine types)."""
    from db.adapters.nodes import get_node as _gn
    from db.adapters.engine_types import list_engine_types
    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    engine_types = list_engine_types(_CONFIG["db_path"], enabled_only=True)
    result = {}
    from db.adapters.configs import get_node_config as _gnc
    for et in engine_types:
        configs = _gnc(_CONFIG["db_path"], node_id, et["id"])
        if configs:
            result[et["name"]] = configs

    return success_single({"node_id": node_id, "configs": result})


def api_set_node_config(node_id, key):
    """Set/update a per-node config value for any engine type."""
    from db.adapters.nodes import get_node as _gn
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    engine_type_name = request.args.get("engine_type")
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type_name)
    et_id = et["id"] if et else None
    if et_id is None and engine_type_name:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type_name}' not found")

    value = body.get("value", "")
    from db.adapters.configs import set_node_config
    set_node_config(_CONFIG["db_path"], node_id, et_id, key, value)

    return success_single({"node_id": node_id, "key": key, "value": value})


def api_delete_node_config(node_id, key):
    """Remove a per-node config value."""
    from db.adapters.nodes import get_node as _gn
    node = _gn(_CONFIG["db_path"], node_id)
    if node is None:
        return error_response("RESOURCE_NOT_FOUND", f"Node {node_id} not found")

    engine_type_name = request.args.get("engine_type")
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type_name)
    et_id = et["id"] if et else None

    from db.adapters.configs import delete_node_config
    deleted = delete_node_config(_CONFIG["db_path"], node_id, et_id, key)
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Config {key} not found for node {node_id}")

    return success_single({"node_id": node_id, "key": key, "deleted": True})


# ---------------------------------------------------------------------------
# Engine type management endpoints
# ---------------------------------------------------------------------------

