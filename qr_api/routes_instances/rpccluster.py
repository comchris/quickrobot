import json

from flask import request, jsonify
import logging as _logging

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG

logger = _logging.getLogger(__name__)
from lib.qr_engine_ids import (
    QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC,
)
from qr_api.lib_instances import _check_node_active


def _parse_rpc_bind_ids(raw):
    """Parse rpc_bind_ids JSON string or list value, returning a list of IDs.

    Handles both serialized JSON strings and in-memory lists from DB adapters.
    Returns empty list on any parse error.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    # Already a list/dict-like — return shallow copy or empty
    return list(raw) if raw else []


def api_bind_rpc(inst_id):
    """Bind RPC instances to a llama-server instance.

    Pure DB update — no stop, no RUNNER-1 job. New bindings take effect on
    next explicit deploy/restart from the herd page buttons.

    Args:
        inst_id: Integer primary key of the llama-server instance.

    Returns:
        JSON with bound_rpc_ids and split_mode.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui, \
        transition_state as _ts, log_action as _log

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
        return error_response("INVALID_ENGINE", "bind-rpc only works for llama_server instances")

    # Guard: block operations on inactive hosts
    nd = _check_node_active(_CONFIG["db_path"], inst.get("node_id"))
    if isinstance(nd, tuple):
        return nd

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    # Accept both rpc_ids (array) and rpc_instance_id (single int) for flexibility.
    # MCP tools and WebUI may pass either form — coerce single int to array.
    rpc_ids = body.get("rpc_ids")
    if rpc_ids is None or (isinstance(rpc_ids, list) and len(rpc_ids) == 0):
        rid = body.get("rpc_instance_id")
        if rid is not None:
            rpc_ids = [int(rid)]
        else:
            return error_response("VALIDATION_ERROR", "Missing 'rpc_ids' (array) or 'rpc_instance_id' (int)")
    elif isinstance(rpc_ids, int):
        rpc_ids = [rpc_ids]

    split_mode = body.get("split_mode", inst.get("split_mode") or "layer")

    # Validate all RPC IDs exist and are rpc engine type
    for rid in rpc_ids:
        ri = _gi(_CONFIG["db_path"], int(rid))
        if ri is None:
            return error_response("RESOURCE_NOT_FOUND", f"RPC instance {rid} not found")
        if ri.get("engine_type_name") != QR_ENGINE_LLAMA_RPC_NAME:
            return error_response("INVALID_ENGINE", f"Instance {rid} is {ri.get('engine_type_name')}, not llama_rpc")

    # Pure DB update — bind/unbind are instant config changes.
    # New bindings take effect on next explicit deploy/restart.
    try:
        _ui(_CONFIG["db_path"], inst_id,
            rpc_bind_ids=json.dumps(rpc_ids),
            split_mode=split_mode)
    except Exception as exc:
        _log(_CONFIG["db_path"], inst_id, "config_change", "bind_rpc_db_failed",
             detail={"error": str(exc)})
        return error_response("INTERNAL_ERROR", f"Bind failed: {exc}")

    # Health check phase removed — bind/unbind are instant DB-only operations.
    # RPC states verified at deploy/restart time.

    return success_single({
        "action": "bind-rpc",
        "instance_id": inst_id,
        "bound_rpc_ids": rpc_ids,
        "split_mode": split_mode,
    })


def api_unbind_rpc(inst_id, rpc_id):
    """Remove a single RPC binding from a llama-server instance.

    Pure DB update — no stop, no RUNNER-1 job. New bindings take effect on
    next explicit deploy/restart from the herd page buttons.

    Args:
        inst_id: Integer primary key of the llama-server instance.
        rpc_id: Integer primary key of the RPC instance to unbind.

    Returns:
        JSON with remaining_rpc_ids.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
        return error_response("INVALID_ENGINE", "unbind-rpc only works for llama_server instances")

    # Guard: block operations on inactive hosts
    nd = _check_node_active(_CONFIG["db_path"], inst.get("node_id"))
    if isinstance(nd, tuple):
        return nd

    current_ids = _parse_rpc_bind_ids(inst.get("rpc_bind_ids"))
    if rpc_id not in current_ids:
        return error_response("NOT_FOUND", f"RPC instance {rpc_id} not in bindings")
    remaining = [x for x in current_ids if x != rpc_id]

    # Pure DB update — bind/unbind are instant config changes.
    # New bindings take effect on next explicit deploy/restart.
    try:
        _ui(_CONFIG["db_path"], inst_id, rpc_bind_ids=json.dumps(remaining))
        return success_single({
            "action": "unbind-rpc",
            "instance_id": inst_id,
            "remaining_rpc_ids": remaining,
        })
    except Exception as exc:
        _log(_CONFIG["db_path"], inst_id, "config_change", "unbind_rpc_db_failed",
             detail={"error": str(exc)})
        return error_response("INTERNAL_ERROR", f"Unbind failed: {exc}")


def api_list_rpc_bindings():
    """List all RPC instances bound to a specific llama-server.

    Query param: llama_id — the llama-server instance ID.
    Returns: list of RPC instance metadata (id, name, hostname, port, split).
    """
    llama_id = request.args.get("llama_id")
    if not llama_id:
        return error_response("VALIDATION_ERROR", "llama_id query param required")

    from db.adapters.instances import get_instance as _gi, list_instances as _list_all
    from db.sqlite import pool

    # Get the llama-server instance
    llama_inst = _gi(_CONFIG["db_path"], int(llama_id))
    if not llama_inst:
        return error_response("RESOURCE_NOT_FOUND", f"Llama-server {llama_id} not found")

    try:
        bind_ids = _parse_rpc_bind_ids(llama_inst.get("rpc_bind_ids"))
    except Exception as _e:
        logger.debug("api_list_rpc_bindings inst=%d: parse failed: %s", llama_id, _e)
        bind_ids = []

    bindings = []
    for rid in bind_ids:
        ri = _gi(_CONFIG["db_path"], int(rid))
        if ri:
            bindings.append({
                "id": ri["id"],
                "name": ri["name"],
                "node_hostname": ri.get("node_hostname") or "",
                "port_assigned": ri.get("port_assigned"),
                "split": ri.get("split") or 0,
                "state": ri.get("state"),
            })

    return success_single({"llama_id": llama_id, "bindings": bindings})


def api_cluster_bind(inst_id):
    """Bind an RPC instance to a llama-server (or unbind).

    Sets rpc_bind_ids to [llama_id] for the target llama-server.
    This is a 1:1 convenience endpoint — the underlying DB supports N:1.

    Args:
        inst_id: Integer primary key of the RPC instance.

    Returns:
        JSON with bind result.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if inst is None:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_RPC_NAME:
        return error_response("INVALID_ENGINE", "cluster-bind only works for llama_rpc instances")

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    llama_id = body.get("llama_id")  # null or 0 = unbind
    ls = None  # Will be set below
    is_bind = llama_id is not None  # True = bind, False = unbind
    if is_bind:
        try:
            llama_id = int(llama_id)
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "llama_id must be integer or null")
        # Validate the llama-server exists and is llama_server type
        ls = _gi(_CONFIG["db_path"], llama_id)
        if not ls or ls.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
            return error_response("RESOURCE_NOT_FOUND", f"Llama-server {llama_id} not found")
    else:
        # Unbind: find which llama-server this RPC is bound to
        from db.adapters.instances import list_instances as _list_all
        all_ls = _list_all(_CONFIG["db_path"], engine_type_id=QR_ENGINE_LLAMA_SERVER)
        for lsi in all_ls:
            try:
                ids = _parse_rpc_bind_ids(lsi.get("rpc_bind_ids"))
                if inst_id in ids:
                    ls = lsi
                    llama_id = lsi["id"]
                    break
            except Exception as _e:
                logger.debug("api_cluster_bind search unbind: parse failed: %s", _e)
                pass

    try:
        if is_bind and ls:
            # Bind: append RPC instance ID to target's list
            current_ids = _parse_rpc_bind_ids(ls.get("rpc_bind_ids"))
            if inst_id not in current_ids:
                current_ids.append(inst_id)
            _ui(_CONFIG["db_path"], llama_id, rpc_bind_ids=json.dumps(current_ids))
        elif not is_bind and ls:
            # Unbind: remove RPC instance ID from target's list
            current_ids = _parse_rpc_bind_ids(ls.get("rpc_bind_ids"))
            current_ids = [x for x in current_ids if x != inst_id]
            if current_ids:
                _ui(_CONFIG["db_path"], ls["id"], rpc_bind_ids=json.dumps(current_ids))

        return success_single({"rpc_id": inst_id, "llama_id": llama_id, "bound": is_bind and bool(llama_id)})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Bind failed: {exc}")


def api_rpccluster_summary():
    """List all llama-server instances with resolved cluster info.

    Returns: list of llama-servers (excluding inactive-host instances) with rpc_bindings and computed tensor_split.

    Returns:
        JSON with status and data.llama_servers array.
    """
    from db.adapters.instances import list_instances as _list_all
    from lib.lib_cluster_env_builder import get_cluster_summary as _get_summary
    from db.sqlite import pool
    from db.adapters.nodes import get_node as _get_node

    try:
        all_ls = _list_all(_CONFIG["db_path"], engine_type_id=QR_ENGINE_LLAMA_SERVER)
        servers = []
        for lsi in all_ls:
            # Exclude instances on inactive hosts
            node_id = lsi.get("node_id")
            if node_id:
                nd = _get_node(_CONFIG["db_path"], node_id)
                if nd and not nd.get("is_active", 1):
                    continue
            try:
                summary = _get_summary(_CONFIG["db_path"], lsi["id"])
                servers.append(summary)
            except Exception as _e:
                logger.debug("api_rpccluster_summary: summary fetch failed for inst=%d: %s", lsi.get("id", "?"), _e)
        return success_single({"llama_servers": servers})
    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_rpccluster_bind(llama_id):
    """Bind RPC instance(s) to a llama-server (herd page enriched endpoint).

    Delegates to api_bind_rpc() for core logic, appends cluster summary
    for the herd page's enriched response format.

    Args:
        llama_id: Integer primary key of the llama-server instance.
        Body: {"rpc_ids": [132, 133]}

    Returns:
        JSON with engine-specific summary + bind result.
    """
    from lib.lib_cluster_env_builder import get_cluster_summary as _get_summary
    from db.adapters.instances import get_instance as _gi_bind

    # Delegate to canonical endpoint handler.
    # api_bind_rpc returns (Response, status_code) tuple — unwrap for inspection.
    result = api_bind_rpc(llama_id)
    if isinstance(result, tuple):
        resp, status_code = result[0], result[1]
    else:
        resp, status_code = result, 200

    # Check status using the Response's get_json() (safe since jsonify created it)
    data = resp.get_json(silent=True) or {}
    if data.get("status") != "ok":
        return result  # Pass through error response unchanged

    # Enrich with cluster summary for herd page (best-effort — don't fail the bind).
    # Use dynamic engine_type_name as response key instead of hardcoded "llama_server"
    try:
        inst = _gi_bind(_CONFIG["db_path"], llama_id)
        engine_key = inst.get("engine_type_name", "server") if inst else "server"
        summary = _get_summary(_CONFIG["db_path"], llama_id)
        data["data"][engine_key] = summary
        return jsonify(data), status_code
    except Exception as _e:
        logger.debug("api_rpccluster_bind: herd enrichment failed for llama_id=%d: %s", llama_id, _e)
    return result


def api_rpccluster_unbind(llama_id, rpc_id):
    """Unbind a single RPC from a llama-server (herd page enriched endpoint).

    Delegates to api_unbind_rpc() for core logic, appends cluster summary
    for the herd page's enriched response format.

    Args:
        llama_id: Integer primary key of the llama-server instance.
        rpc_id: Integer primary key of the RPC instance to unbind.

    Returns:
        JSON with engine-specific summary + unbind result.
    """
    from lib.lib_cluster_env_builder import get_cluster_summary as _get_summary
    from db.adapters.instances import get_instance as _gi_unbind

    # Delegate to canonical endpoint handler.
    # api_unbind_rpc returns (Response, status_code) tuple — unwrap for inspection.
    result = api_unbind_rpc(llama_id, rpc_id)
    if isinstance(result, tuple):
        resp, status_code = result[0], result[1]
    else:
        resp, status_code = result, 200

    # Check status using the Response's get_json() (safe since jsonify created it)
    data = resp.get_json(silent=True) or {}
    if data.get("status") != "ok":
        return result  # Pass through error response unchanged

    # Enrich with cluster summary for herd page (best-effort — don't fail the unbind).
    # Use dynamic engine_type_name as response key instead of hardcoded "llama_server"
    try:
        inst = _gi_unbind(_CONFIG["db_path"], llama_id)
        engine_key = inst.get("engine_type_name", "server") if inst else "server"
        summary = _get_summary(_CONFIG["db_path"], llama_id)
        data["data"][engine_key] = summary
        return jsonify(data), status_code
    except Exception as _e:
        logger.debug("api_rpccluster_unbind: herd enrichment failed for llama_id=%d: %s", llama_id, _e)
    return result


