"""WebUI management endpoints for quickrobot.

Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
import logging
from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_WEBUI_NAME, get_system_instance_id
from db.sqlite import pool as db_pool
from lib.lib_constants import DEFAULT_ANSIBLE_USER
from engine import get_engine, get_engine_capabilities
from qr_api.routes_nodes.system_mgmt import _get_webui_settings_from_engine_config


def _fsi(db_path, engine_name):
    """Find system-managed instance by engine type name."""
    from db.adapters.instances import get_instance as _gi
    inst_id = get_system_instance_id(engine_name)
    if inst_id is None:
        return None
    return _gi(db_path, inst_id)

logger = logging.getLogger(__name__)

def api_web_server_settings():
    """Get current web server settings (port, host)."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    config = engine.get_config(inst["id"], _CONFIG["db_path"])
    # For system-managed instances (engine_id < 100), all settings come from engine_configs only.
    # Non-system instances may use per-instance config_override as an additional layer.
    is_system = inst.get("system_managed")
    if is_system:
        result = _get_webui_settings_from_engine_config(_CONFIG["db_path"], inst)
    else:
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                import json as _jc
                co = _jc.loads(co)
            except Exception as _e:
                logger.debug("config_override JSON parse failed (web_instance): %s", _e)
                co = {}
        from db.adapters.configs import get_engine_config as _gec_web, get_polling_intervals
        et_id = inst.get("engine_type_id")
        host_row = _gec_web(_CONFIG["db_path"], et_id, "web_ui_host") or {}
        port_row = _gec_web(_CONFIG["db_path"], et_id, "web_ui_port") or {}
        tz_row = _gec_web(_CONFIG["db_path"], et_id, "web_ui_timezone") or {}
        def_host = host_row.get("value", "") if host_row else ""
        def_port = int(port_row["value"]) if port_row and port_row.get("value") else 0
        def_tz = tz_row.get("value", DEFAULT_TIMEZONE) if tz_row else DEFAULT_TIMEZONE
        autostart_row = _gec_web(_CONFIG["db_path"], et_id, "webui_autostart") or {}
        def_autostart = autostart_row.get("value", "true") if autostart_row else "true"
        detach_row = _gec_web(_CONFIG["db_path"], et_id, "webui_detach") or {}
        def_detach = detach_row.get("value", "false") if detach_row else "false"
        result = {
            "web_ui_host": co.get("web_ui_host") or def_host,
            "web_ui_port": str(co.get("web_ui_port") or inst.get("port_assigned") or def_port),
            "web_ui_timezone": co.get("web_ui_timezone") or def_tz,
            "webui_autostart": def_autostart,
            "webui_detach": co.get("webui_detach") or def_detach,
        }
    # Add polling intervals from engine_configs (not per-instance)
    try:
        from db.sqlite import pool as _pool
        with _pool(_CONFIG["db_path"]) as conn:
            first_engine = conn.execute("SELECT id FROM engine_types WHERE name='quickrobot-api' LIMIT 1").fetchone()
            if first_engine:
                api_et_id = first_engine[0]
                local_poll = get_polling_intervals(_CONFIG["db_path"], api_et_id, is_local=True) or "10000"
                remote_poll = get_polling_intervals(_CONFIG["db_path"], api_et_id, is_local=False) or "600000"
                result["polling_interval_local_sec"] = local_poll
                result["polling_interval_remote_sec"] = remote_poll
    except Exception as _e:
        logger.debug("polling interval fetch failed for web_instance %d: %s", inst["id"], _e)
    return success_single(result)


def api_web_server_update_settings():
    """Update web server settings (port, host)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    # Separate engine_configs (global) from instance config_override (per-instance)
    engine_cfg_keys = ("web_ui_port", "web_ui_host", "web_ui_timezone", "webui_autostart", "webui_detach")
    config = {k: v for k, v in body.items() if k in engine_cfg_keys}
    # Save engine_configs first (global settings)
    et_id = inst.get("engine_type_id")
    try:
        from db.adapters.configs import update_engine_config as _uec
        for k, v in config.items():
            if k in ("webui_autostart", "webui_detach") and isinstance(v, bool):
                v = "true" if v else "false"
            elif k in ("webui_autostart", "webui_detach") and isinstance(v, str):
                v = "true" if v.lower() in ("true", "1", "yes") else "false"
            _uec(_CONFIG["db_path"], et_id, k, str(v))
    except Exception as exc:
        logger.warning("[qr] WARNING: failed to update engine config: %s", exc)
    # For system-managed instances (engine_id < 100), skip per-instance overrides
    # All settings are global via engine_configs — prevents divergence between
    # config_override and engine_configs that causes "settings revert" bugs.
    result = {}
    if not inst.get("system_managed"):
        co = inst.get("config_override", {}) or {}
        if isinstance(co, str):
            try:
                import json as _jc2
                co = _jc2.loads(co)
            except Exception as _e:
                logger.debug("config_override JSON parse failed (web_server_settings): %s", _e)
                co = {}
        for k, v in config.items():
            co[k] = v
        result = engine.set_config(inst["id"], co, _CONFIG["db_path"])
    return success_single(result)


def api_web_server_update_setting(key):
    """Get or update a single web server setting by key."""
    from db.adapters.configs import get_engine_config as _gec, update_engine_config as _uec

    editable_keys = ("web_ui_port", "web_ui_host", "web_ui_timezone",
                     "webui_autostart", "webui_detach")
    if key not in editable_keys:
        return error_response("INVALID_KEY", f"Editable keys: {', '.join(editable_keys)}")

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    et_id = inst.get("engine_type_id")

    if request.method == "GET":
        row = _gec(_CONFIG["db_path"], et_id, key) or {}
        value = row.get("value", "")
        return success_single({"engine_type": QR_ENGINE_WEBUI_NAME, "key": key, "value": value})

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
        _uec(_CONFIG["db_path"], et_id, key, str(value))
    except Exception as exc:
        return error_response("WRITE_ERROR", str(exc))

    return success_single({"engine_type": QR_ENGINE_WEBUI_NAME, "key": key, "value": str(value)})


def api_web_server_start():
    """Start the web UI server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    result = engine.execute(inst["id"], "start", _CONFIG["db_path"])
    return success_single(result)


def api_web_server_stop():
    """Stop the web UI server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    result = engine.execute(inst["id"], "stop", _CONFIG["db_path"])
    return success_single(result)


def api_web_server_restart():
    """Restart the web UI server."""
    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    result = engine.execute(inst["id"], "restart", _CONFIG["db_path"])
    return success_single(result)


def api_web_server_status():
    """Check WebUI server status (redirects to system engine health check)."""
    from engine import get_engine
    from lib.qr_engine_ids import QR_ENGINE_WEBUI_NAME

    inst = _fsi(_CONFIG["db_path"], QR_ENGINE_WEBUI_NAME)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server instance not found")

    engine = get_engine(QR_ENGINE_WEBUI_NAME)
    if engine is None:
        return error_response("RESOURCE_NOT_FOUND", "Web server engine not loaded")

    status = engine.get_status(inst["id"], _CONFIG["db_path"])
    return success_single(status)

