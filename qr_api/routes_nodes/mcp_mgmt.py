"""MCP server management endpoints for quickrobot.

Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
import logging
import os
from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_MCP_NAME, get_system_instance_id
from db.sqlite import pool as db_pool
from lib.lib_constants import DEFAULT_ANSIBLE_USER
from engine import get_engine, get_engine_capabilities


def _fsi(db_path, engine_name):
    """Find system-managed instance by engine type name."""
    from db.adapters.instances import get_instance as _gi
    inst_id = get_system_instance_id(engine_name)
    if inst_id is None:
        return None
    return _gi(db_path, inst_id)

logger = logging.getLogger(__name__)

def api_mcp_settings():
    """Get MCP server settings (port, flags)."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    # For system-managed instances (engine_id < 100), all settings come from engine_configs only.
    if inst.get("system_managed"):
        et_id = inst.get("engine_type_id")
        from db.adapters.configs import get_engine_config as _gec
        from lib.lib_system_engine import load_env_config as _lec
        port_row = _gec(_CONFIG["db_path"], et_id, "mcp_port") or {}
        api_host_row = _gec(_CONFIG["db_path"], et_id, "mcp_api_host") or {}
        python_interp_row = _gec(_CONFIG["db_path"], et_id, "mcp_python_interpreter") or {}
        reads_row = _gec(_CONFIG["db_path"], et_id, "mcp_allow_reads") or {}
        writes_row = _gec(_CONFIG["db_path"], et_id, "mcp_allow_writes") or {}
        proxy_row = _gec(_CONFIG["db_path"], et_id, "mcp_allow_proxy") or {}
        detach_row = _gec(_CONFIG["db_path"], et_id, "mcp_detach") or {}
        # MCP listen host and autostart: read from .quickrobot.env (not API bind address)
        # DB override applies only if user explicitly set mcp_autostart via API
        try:
            env_cfg = _lec(os.getcwd())
            mcp_host_val = env_cfg["QUICKROBOT_MCP_HOST"]
            mcp_autostart_val = str(env_cfg.get("QUICKROBOT_MCP_AUTOSTART", "true")).lower() in ("true", "1")
            mcp_token_val = env_cfg.get("QUICKROBOT_API_KEY", "")
        except FileNotFoundError:
            mcp_host_val = _CONFIG["host"]
            mcp_autostart_val = True
            mcp_token_val = ""
        # DB override: if user explicitly set mcp_autostart via API, use DB value
        autostart_row = _gec(_CONFIG["db_path"], et_id, "mcp_autostart") or {}
        if autostart_row:
            mcp_autostart_val = str(autostart_row.get("value", "false")).lower() in ("true", "1")
        result = {
            "mcp_port": str(inst.get("port_assigned") or port_row.get("value", "")),
            "mcp_host": mcp_host_val,
            "mcp_api_base": f"http://{_CONFIG['host']}:{_CONFIG['api_port']}/api/v1",
            "mcp_python_interpreter": python_interp_row.get("value", "") if python_interp_row else "",
            "mcp_autostart": str(mcp_autostart_val),
            "mcp_detach": str(detach_row.get("value", "false")),
            "allow_reads": "true" if (str(reads_row.get("value", "true")).lower() in ("true", "1", "yes") if reads_row else True) else "false",
            "allow_writes": "true" if (str(writes_row.get("value", "true")).lower() in ("true", "1", "yes") if writes_row else True) else "false",
            "allow_proxy": "true" if (str(proxy_row.get("value", "true")).lower() in ("true", "1", "yes") if proxy_row else True) else "false",
            "mcp_token": mcp_token_val,
        }
    else:
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                import json as _jc
                co = _jc.loads(co)
            except Exception as _e:
                logger.debug("config_override JSON parse failed (mcp_settings): %s", _e)
                co = {}
        et_id = inst.get("engine_type_id")
        from db.adapters.configs import get_engine_config as _gec2
        from lib.lib_system_engine import load_env_config as _lec
        port_row = _gec2(_CONFIG["db_path"], et_id, "mcp_port") or {}
        api_host_row = _gec2(_CONFIG["db_path"], et_id, "mcp_api_host") or {}
        python_interp_row = _gec2(_CONFIG["db_path"], et_id, "mcp_python_interpreter") or {}
        reads_row = _gec2(_CONFIG["db_path"], et_id, "mcp_allow_reads") or {}
        writes_row = _gec2(_CONFIG["db_path"], et_id, "mcp_allow_writes") or {}
        proxy_row = _gec2(_CONFIG["db_path"], et_id, "mcp_allow_proxy") or {}
        detach_row = _gec2(_CONFIG["db_path"], et_id, "mcp_detach") or {}
        # MCP listen host and autostart: config_override > env file
        try:
            env_cfg = _lec(os.getcwd())
            mcp_host_val = co.get("mcp_host") or env_cfg["QUICKROBOT_MCP_HOST"]
            mcp_autostart_val = str(env_cfg.get("QUICKROBOT_MCP_AUTOSTART", "true")).lower() in ("true", "1")
            mcp_token_val = env_cfg.get("QUICKROBOT_API_KEY", "")
        except FileNotFoundError:
            mcp_host_val = co.get("mcp_host") or _CONFIG["host"]
            mcp_autostart_val = True
            mcp_token_val = ""
        # DB override: if user explicitly set mcp_autostart via API, use DB value
        autostart_row = _gec2(_CONFIG["db_path"], et_id, "mcp_autostart") or {}
        if autostart_row:
            mcp_autostart_val = str(autostart_row.get("value", "false")).lower() in ("true", "1")
        result = {
            "mcp_port": str(co.get("mcp_port") or inst.get("port_assigned") or port_row.get("value", "")),
            "mcp_host": mcp_host_val,
            "mcp_api_base": f"http://{_CONFIG['host']}:{_CONFIG['api_port']}/api/v1",
            "mcp_python_interpreter": co.get("mcp_python_interpreter") or python_interp_row.get("value", ""),
            "mcp_autostart": str(mcp_autostart_val),
            "mcp_detach": str(detach_row.get("value", "false")),
            "allow_reads": "true" if (str(reads_row.get("value", "true")).lower() in ("true", "1", "yes") if reads_row else True) else "false",
            "allow_writes": "true" if (str(writes_row.get("value", "true")).lower() in ("true", "1", "yes") if writes_row else True) else "false",
            "allow_proxy": "true" if (str(proxy_row.get("value", "true")).lower() in ("true", "1", "yes") if proxy_row else True) else "false",
            "mcp_token": mcp_token_val,
        }
    return success_single(result)


def api_mcp_update_settings():
    """Update MCP server settings (port, flags)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    # Separate engine_configs (global) from instance config_override
    # Accept both prefixed (mcp_allow_reads) and non-prefixed (allow_reads) keys for backwards compat
    engine_cfg_keys = ("mcp_port", "mcp_api_host", "mcp_python_interpreter", "mcp_autostart", "mcp_detach", "mcp_allow_reads", "mcp_allow_writes", "mcp_allow_proxy")
    key_prefix_map = {"allow_reads": "mcp_allow_reads", "allow_writes": "mcp_allow_writes", "allow_proxy": "mcp_allow_proxy"}
    config = {}
    for k, v in body.items():
        if k in engine_cfg_keys:
            config[k] = v
        elif k in key_prefix_map and key_prefix_map[k] in engine_cfg_keys:
            config[key_prefix_map[k]] = v  # normalize: allow_reads → mcp_allow_reads
    et_id = inst.get("engine_type_id")
    try:
        from db.adapters.configs import update_engine_config as _uec
        for k, v in config.items():
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, str):
                v = v.strip()
            else:
                v = str(v)
            _uec(_CONFIG["db_path"], et_id, k, v)
    except Exception as exc:
        logger.warning("[qr] WARNING: failed to update MCP engine config: %s", exc)

    # For system-managed instances, skip per-instance overrides — all settings are global via engine_configs
    result = {}
    if not inst.get("system_managed"):
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                co = json.loads(co)
            except Exception as _e:
                logger.debug("config_override JSON parse failed (mcp_config): %s", _e)
                co = {}
        for k, v in config.items():
            co[k] = v
        result = engine.set_config(inst["id"], co, _CONFIG["db_path"])

    # Restart MCP process to pick up new config (flags, interpreter, port, etc.)
    try:
        from db.adapters.instances import log_action as _log_act
        from qr_api.routes_instances import _restart_system_managed
        _restart_system_managed(inst["id"], QR_ENGINE_MCP_NAME, _log_act)
    except Exception as exc:
        logger.warning("[qr] WARNING: failed to restart MCP after settings update: %s", exc)

    return success_single(result)


def api_mcp_update_setting(key):
    """Get or update a single MCP server setting by key."""
    from db.adapters.configs import get_engine_config as _gec, update_engine_config as _uec

    editable_keys = ("mcp_port", "mcp_api_host", "mcp_python_interpreter",
                     "mcp_autostart", "mcp_detach", "mcp_allow_reads",
                     "mcp_allow_writes", "mcp_allow_proxy")
    key_map = {"allow_reads": "mcp_allow_reads", "allow_writes": "mcp_allow_writes",
               "allow_proxy": "mcp_allow_proxy"}
    actual_key = key_map.get(key, key)
    if actual_key not in editable_keys:
        return error_response("INVALID_KEY", f"Editable keys: {', '.join(editable_keys)}")

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    et_id = inst.get("engine_type_id")

    if request.method == "GET":
        row = _gec(_CONFIG["db_path"], et_id, actual_key) or {}
        value = row.get("value", "")
        return success_single({"engine_type": QR_ENGINE_MCP_NAME, "key": actual_key, "value": value})

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid json"))

    value = body.get("value")
    if value is None:
        return error_response("VALIDATION_ERROR", '"value" field required')

    try:
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, str):
            value = value.strip()
        _uec(_CONFIG["db_path"], et_id, actual_key, str(value))
    except Exception as exc:
        return error_response("WRITE_ERROR", str(exc))

    # Restart MCP to pick up the new setting (flags, interpreter, port, etc.)
    try:
        from db.adapters.instances import log_action as _log_act
        from qr_api.routes_instances import _restart_system_managed
        _restart_system_managed(inst["id"], QR_ENGINE_MCP_NAME, _log_act)
    except Exception as exc:
        logger.warning("[qr] WARNING: failed to restart MCP after setting update (%s): %s", actual_key, exc)

    return success_single({"engine_type": QR_ENGINE_MCP_NAME, "key": actual_key, "value": str(value)})


def api_mcp_start():
    """Start the MCP SSE server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    result = engine.execute(inst["id"], "start", _CONFIG["db_path"])
    return success_single(result)


def api_mcp_stop():
    """Stop the MCP SSE server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    result = engine.execute(inst["id"], "stop", _CONFIG["db_path"])
    return success_single(result)


def api_mcp_restart():
    """Restart the MCP SSE server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)

    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    result = engine.execute(inst["id"], "restart", _CONFIG["db_path"])
    return success_single(result)


def api_mcp_status():
    """Check MCP server status."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_MCP_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server instance not found")

    engine = get_engine(QR_ENGINE_MCP_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "MCP server engine not loaded")

    status = engine.get_status(inst["id"], _CONFIG["db_path"])
    return success_single(status)


def api_app_status():
    """Return app-level status + instance summary for WebUI consumption.

    Adds global_state (computed from active-host instances) and
    global_state_rgb (server-computed color string).
    """
    pass
