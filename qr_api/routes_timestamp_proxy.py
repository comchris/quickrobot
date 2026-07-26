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

"""Timestamp proxy engine routes.

Handles settings and config management for timestamp_proxy instances.
"""

import logging as _logging
logger = _logging.getLogger(__name__)

from flask import request, jsonify
from qr_api.lib_responses import success_single, success_list, error_response, require_json
from lib.qr_engine_ids import QR_ENGINE_TIMESTAMP_PROXY_NAME, QR_ENGINE_TIMESTAMP_PROXY


def api_timestamp_proxy_settings():
    """GET /api/v1/engine/timestamp_proxy/settings — List timestamp proxy engine settings."""
    from db.adapters.configs import list_engine_configs as _lec

    configs = _lec(_CONFIG["db_path"], QR_ENGINE_TIMESTAMP_PROXY)
    result = []
    for cfg in configs:
        entry = {
            "key": cfg.get("key", ""),
            "value": cfg.get("value", ""),
            "description": cfg.get("description", ""),
        }
        result.append(entry)

    return success_list(result)


def api_timestamp_proxy_update_setting():
    """PUT /api/v1/engine/timestamp_proxy/settings/<key> — Update a single setting."""
    from db.adapters.configs import set_engine_config as _sec, \
        delete_engine_config as _dec

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "Invalid JSON"))

    key = request.view_args.get("key")
    if not key:
        return error_response("VALIDATION_ERROR", "Missing 'key' parameter")

    value = body.get("value")
    try:
        _sec(_CONFIG["db_path"], QR_ENGINE_TIMESTAMP_PROXY, key, str(value),
             f"Updated via API by {request.remote_addr}")
        return success_single({"key": key, "value": str(value), "updated": True})
    except Exception as exc:
        logger.debug("timestamp_proxy setting update failed for %s: %s", key, exc)
        return error_response("VALIDATION_ERROR", str(exc))


def api_timestamp_proxy_instance_config():
    """GET/PUT /api/v1/instances/timestamp-proxy/config — Read/update timestamp proxy instance config.

    Handles config_override for timestamp_proxy instances (backend_host,
    backend_port, inject options, timestamp format).
    """
    from db.adapters.instances import get_instance as _gi, \
        update_instance as _ui

    if request.method == "GET":
        inst_id = request.view_args.get("inst_id")
        if not inst_id:
            return error_response("VALIDATION_ERROR", "Missing instance ID")

        inst = _gi(_CONFIG["db_path"], int(inst_id))
        if not inst:
            return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

        if not (inst.get("engine_type_name") or "").lower().endswith("timestamp_proxy"):
            return error_response("VALIDATION_ERROR", "Engine is not timestamp_proxy")

        co = inst.get("config_override") or {}
        if isinstance(co, str):
            try:
                import json as _j
                co = _j.loads(co)
            except Exception as _e:
                logger.debug("json parse error on config_override: %s", _e)
                co = {}

        return success_single({
            "instance_id": inst["id"],
            "name": inst.get("name", ""),
            "config_override": co,
        })

    elif request.method == "PUT":
        inst_id = request.view_args.get("inst_id")
        if not inst_id:
            return error_response("VALIDATION_ERROR", "Missing instance ID")

        body, is_err = require_json()
        if is_err:
            return error_response("VALIDATION_ERROR", body.get("_error", "Invalid JSON"))

        inst = _gi(_CONFIG["db_path"], int(inst_id))
        if not inst:
            return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

        if not (inst.get("engine_type_name") or "").lower().endswith("timestamp_proxy"):
            return error_response("VALIDATION_ERROR", "Engine is not timestamp_proxy")

        # Merge into existing config_override
        co = inst.get("config_override") or {}
        if isinstance(co, str):
            try:
                import json as _j
                co = _j.loads(co)
            except Exception as _e:
                logger.debug("json parse error on config_override: %s", _e)
                co = {}

        co.update(body)

        try:
            _ui(_CONFIG["db_path"], int(inst_id), config_override=co)
            return success_single({
                "instance_id": inst["id"],
                "config_override": co,
                "updated": True,
            })
        except Exception as exc:
            logger.debug("timestamp_proxy config update failed for %s: %s", inst_id, exc)
            return error_response("VALIDATION_ERROR", str(exc))


def api_timestamp_proxy_validate_config():
    """POST /api/v1/engine/timestamp_proxy/validate — Validate config_override values."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "Invalid JSON"))

    errors = []
    warnings = []

    # backend_host validation
    backend_host = body.get("backend_host")
    if not backend_host or not isinstance(backend_host, str) or not backend_host.strip():
        errors.append("backend_host is required and must be a non-empty string")

    # backend_port validation
    backend_port = body.get("backend_port")
    if backend_port is not None:
        try:
            port_val = int(backend_port)
            if port_val < 1 or port_val > 65535:
                errors.append(f"backend_port must be 1-65535, got {port_val}")
        except (ValueError, TypeError):
            errors.append(f"backend_port must be a valid integer, got '{backend_port}'")

    # inject_user_timestamp validation
    inject_ts = body.get("inject_user_timestamp")
    if inject_ts is not None and not isinstance(inject_ts, bool):
        errors.append("inject_user_timestamp must be true or false")

    # inject_response_time validation
    inject_rt = body.get("inject_response_time")
    if inject_rt is not None and not isinstance(inject_rt, bool):
        errors.append("inject_response_time must be true or false")

    # timestamp_position validation
    ts_pos = body.get("timestamp_position", "front")
    valid_positions = ("front", "back", "both")
    if ts_pos not in valid_positions:
        errors.append(f"timestamp_position must be one of: {', '.join(valid_positions)}")

    # timestamp_format validation (if provided)
    ts_fmt = body.get("timestamp_format")
    if ts_fmt and isinstance(ts_fmt, str):
        try:
            from datetime import datetime
            datetime.now().strftime(ts_fmt)
        except Exception as exc:
            errors.append(f"Invalid strftime format: {exc}")

    # Warning: backend_port conflict with assigned instance port
    assigned_port = body.get("assigned_port")
    if backend_port and assigned_port and int(backend_port) == int(assigned_port):
        warnings.append("backend_port matches the assigned proxy port — this may cause conflicts")

    return success_single({
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    })


# Export for qr_api/__init__.py route registration
__all__ = [
    "api_timestamp_proxy_settings",
    "api_timestamp_proxy_update_setting",
    "api_timestamp_proxy_instance_config",
    "api_timestamp_proxy_validate_config",
]
