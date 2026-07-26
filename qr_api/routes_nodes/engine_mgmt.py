from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_ENGINE_API_NAME
from engine import get_engine_capabilities, ENGINES


def api_list_engines():
    """List all engine types: DB entries + in-memory discovered engines not in DB."""
    from db.adapters.engine_types import list_engine_types as _let, get_engine_type as _get_et
    from db.adapters.instances import list_instances as _li
    engine_types = _let(_CONFIG["db_path"], enabled_only=False)

    result = []
    db_names = {et["name"] for et in engine_types}

    # Add DB engine types
    for et in engine_types:
        instances = _li(_CONFIG["db_path"], engine_type_id=et["id"])
        count = len(instances)
        loaded_cap = get_engine_capabilities(et["name"])
        if loaded_cap:
            et["capabilities"].update(loaded_cap)
        et["instance_count"] = count
        result.append(et)

    # Add in-memory engines not yet registered in DB (normalize names for comparison)
    for eng_name, cls, cap in ENGINES:
        cap_name = cap.get("name") if isinstance(cap, dict) else None
        db_name = eng_name.replace("_", "-")
        if db_name not in db_names and eng_name not in db_names and (cap_name is None or cap_name not in db_names):
            cap_dict = cap if isinstance(cap, dict) else {}
            result.append({
                "name": db_name if db_name != eng_name else eng_name,
                "display_name": cap_dict.get("display_name", eng_name.replace("_", " ").title()),
                "capabilities": cap_dict,
                "instance_count": 0,
                "enabled": 1,
            })

    return success_list(result)


def api_get_engine_config(engine_type):
    """List all config keys for an engine type."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.configs import get_engine_config as _gec
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    configs = _gec(_CONFIG["db_path"], et_id)
    return success_single(configs or {})


def api_set_engine_config(engine_type, key):
    """Set/update an engine config key."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.configs import update_engine_config as _sec
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    value = body.get("value", "")
    description = body.get("description", "")
    _sec(_CONFIG["db_path"], et_id, key, value, description)
    return success_single({"engine_type": engine_type, "key": key, "value": value})


def api_delete_engine_config(engine_type, key):
    """Remove an engine config key."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.configs import delete_engine_config as _dec
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    deleted = _dec(_CONFIG["db_path"], et_id, key)
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Config {key} not found")

    return success_single({"engine_type": engine_type, "key": key, "deleted": True})


def api_batch_set_engine_config(engine_type):
    """Set multiple config keys at once (batch update).

    Accepts a JSON body with {configs: {key1: value1, key2: value2}} and
    persists all values in a single request. Used by the WebUI batch save.

    Args:
        engine_type: Engine type name string.

    Returns:
        dict with saved_keys count and engine_type.
    """
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    configs = body.get("configs", {})
    if not isinstance(configs, dict):
        return error_response("VALIDATION_ERROR", "configs must be a dict")

    from db.adapters.configs import update_engine_config as _sec

    saved = 0
    for key, value in configs.items():
        _sec(_CONFIG["db_path"], et_id, key, str(value), "")
        saved += 1

    return success_single({"saved_keys": saved, "engine_type": engine_type})


def _let_config():
    """Cached engine type list for batch config lookups."""
    from db.adapters.engine_types import list_engine_types as _let
    return _let(_CONFIG["db_path"])


def api_api_server_update_setting(key):
    """Get or update a single quickrobot-api config key."""
    from db.adapters.configs import get_engine_config as _gec, update_engine_config as _uec

    editable_keys = ("db_path", "ping_interval",
                       "polling_interval_local_sec", "polling_interval_remote_sec",
                       "refresh_interval_default_sec")
    # env-sourced keys (read-only, set from .quickrobot.env):
    #   api_host, api_port, ansible_user, ansible_key_path,
    #   playbook_root_dir, ping_command
    if key not in editable_keys:
        return error_response("INVALID_KEY", f"Editable keys: {', '.join(editable_keys)}")

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], QR_ENGINE_API_NAME)
    et_id = et["id"] if et else None
    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", "Engine type quickrobot-api not found")

    if request.method == "GET":
        row = _gec(_CONFIG["db_path"], et_id, key) or {}
        value = row.get("value", "")
        return success_single({"engine_type": QR_ENGINE_API_NAME, "key": key, "value": value})

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid json"))

    value = body.get("value")
    if value is None:
        return error_response("VALIDATION_ERROR", '"value" field required')

    try:
        _uec(_CONFIG["db_path"], et_id, key, str(value))
    except Exception as exc:
        return error_response("WRITE_ERROR", str(exc))

    return success_single({"engine_type": QR_ENGINE_API_NAME, "key": key, "value": str(value)})


