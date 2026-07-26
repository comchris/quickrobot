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

# Valid split mode values — single source for all split-mode references in this module
QR_SPLIT_MODES = ("layer", "row", "tensor")

# Config merge layer priority (highest to lowest): used in source annotation logic
_LAYERS_PRIORITY = ("instance_override", "cluster_binding", "model", "preset", "engine_default", "metadata")


def api_set_config_level(inst_id, level):
    """Set (upsert) a config level for an instance.

    PUT /api/v1/instances/<id>/config-levels/<level>
    Body: {source, env_vars, cli_opts, model_params}

    Args:
        inst_id: Instance primary key.
        level: Layer level (1-7).

    Returns:
        JSON with updated layer details.
    """
    try:
        from db.adapters.config_levels import set_config_level as _set
        from db.adapters.instances import get_instance as _gi
        from qr_api.lib_responses import success_single, error_response as _err

        inst = _gi(_CONFIG["db_path"], inst_id)
        if not inst:
            return _err("INSTANCE_NOT_FOUND", f"Instance {inst_id} not found")

        level_int = int(level)
        if level_int < 1 or level_int > 7:
            return _err("VALIDATION_ERROR", f"Invalid level {level}: must be 1-7")

        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return _err("VALIDATION_ERROR", "Request body must be a JSON object")

        source = data.get("source", "api_patch")
        env_vars = data.get("env_vars")
        cli_opts = data.get("cli_opts")
        model_params = data.get("model_params")

        _set(_CONFIG["db_path"], inst_id, level_int, source,
             env_vars=env_vars, cli_opts=cli_opts, model_params=model_params)

        return success_single({
            "instance_id": inst_id,
            "level": level_int,
            "source": source,
            "env_vars": env_vars or {},
            "cli_opts": cli_opts or [],
            "model_params": model_params or {},
        })

    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_delete_config_level(inst_id, level):
    """Delete a config level for an instance.

    DELETE /api/v1/instances/<id>/config-levels/<level>

    Args:
        inst_id: Instance primary key.
        level: Layer level (1-7) to delete.

    Returns:
        JSON confirming deletion.
    """
    try:
        from db.adapters.config_levels import delete_config_level as _del
        from db.adapters.instances import get_instance as _gi

        inst = _gi(_CONFIG["db_path"], inst_id)
        if not inst:
            return error_response("INSTANCE_NOT_FOUND", f"Instance {inst_id} not found")

        level_int = int(level)
        if level_int < 1 or level_int > 7:
            return error_response("VALIDATION_ERROR", f"Invalid level {level}: must be 1-7")

        deleted = _del(_CONFIG["db_path"], inst_id, level_int)
        if not deleted:
            return error_response("NOT_FOUND", f"Config level {level_int} not found for instance {inst_id}")

        return success_single({"instance_id": inst_id, "level": level_int, "deleted": True})

    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_get_merged_config(inst_id):
    """Return the full merged configuration with layer annotations.

    Uses build_config_layers (CONFIG-1 Phase 2) to return both the
    merged result and a detailed per-layer breakdown, plus L7 cluster
    bindings for llama-server instances (--rpc, -dev, tensor_split, expert_flags).

    GET /api/v1/instances/<id>/config-levels/merged

    Args:
        inst_id: Instance primary key.

    Returns:
        JSON with:
            merged: {env, cli_opts, model, restart_policy, start_on_boot}
            layers: {engine_default, model_definition, preset_template, instance_override}
                    each with level, source, env_vars, cli_opts, model_params, metadata
            cluster_bindings: {tensor_split_str, split_mode, rpc_bindings, expert_flags,
                              gpu_override, bind_count, draft_devices} (llama-server only)
    """
    try:
        from db.adapters.instances import get_instance as _gi
        from lib.lib_config_merge import build_config_layers as _build_layers

        inst = _gi(_CONFIG["db_path"], inst_id)
        if not inst:
            return error_response("INSTANCE_NOT_FOUND", f"Instance {inst_id} not found")

        merged, layers = _build_layers(_CONFIG["db_path"], inst_id)

        # Serialize ConfigLevel objects to dicts
        serialized_layers = {}
        for name, cl in layers.items():
            serialized_layers[name] = {
                "level": cl.level,
                "source": cl.source,
                "env_vars": dict(cl.env_vars),
                "cli_opts": list(cl.cli_opts),
                "model_params": dict(cl.model_params),
                "metadata": dict(cl.metadata),
            }

        # Build source_annotations: map each key to its contributing layer.
        # Use last-wins (overwrite) so instance_override takes priority over preset_template.
        source_annotations = {}
        for layer_name, layer_data in serialized_layers.items():
            for key in layer_data.get("env_vars", {}):
                source_annotations[key] = layer_name
            for key in layer_data.get("model_params", {}):
                source_annotations[key] = layer_name

        # Add L7 cluster bindings for llama-server instances
        cluster_bindings = {}
        if inst.get("engine_type_id") in (QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC):
            try:
                from lib.lib_cluster_env_builder import build_llama_server_env as _build_env
                cluster_result = _build_env(_CONFIG["db_path"], inst_id)
                cluster_bindings = {
                    "tensor_split_str": cluster_result.get("tensor_split_str"),
                    "split_mode": cluster_result.get("split_mode"),
                    "rpc_bindings": cluster_result.get("rpc_bindings"),
                    "expert_flags": cluster_result.get("expert_flags"),
                    "gpu_override": cluster_result.get("gpu_override"),
                    "bind_count": cluster_result.get("bind_count"),
                    "cli_args": cluster_result.get("cli_args"),
                }
            except Exception as _e:
                logger.debug("api_get_merged_config inst=%d: cluster env builder fallback failed: %s", inst_id, _e)

        return jsonify({
            "status": "ok",
            "data": {
                "instance_id": inst_id,
                "merged": merged,
                "layers": serialized_layers,
                "source_annotations": source_annotations,
                "cluster_bindings": cluster_bindings,
            },
        }), 200

    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_cycle_split_mode(inst_id):
    """Cycle split_mode — EP-CONSOLIDATE P3+P4 (thin wrapper)."""
    from db.adapters.instances import get_instance as _gi
    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")
    if inst.get("engine_type_name") != QR_ENGINE_LLAMA_SERVER_NAME:
        return error_response("INVALID_ENGINE", "split-mode cycle only works for llama_server instances")
    current = inst.get("split_mode") or "layer"
    idx = QR_SPLIT_MODES.index(current) if current in QR_SPLIT_MODES else 0
    new_mode = QR_SPLIT_MODES[(idx + 1) % len(QR_SPLIT_MODES)]
    result = api_set_instance_config(inst_id)
    # Return the new mode for legacy clients expecting a response body
    return success_single({"instance_id": inst_id, "split_mode": new_mode})


def api_set_draft(inst_id):
    """Set draft value — EP-CONSOLIDATE P3+P4 (thin wrapper)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))
    draft_val = body.get("draft")
    try:
        result = api_set_instance_config(inst_id)
        return success_single({"instance_id": inst_id, "draft": draft_val})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update draft: {exc}")


def api_set_cli_flags(inst_id):
    """Set CLI flags — EP-CONSOLIDATE P3+P4 (thin wrapper)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))
    flags = body.get("flags")
    if not isinstance(flags, list):
        return error_response("VALIDATION_ERROR", "flags must be a JSON array")
    for f in flags:
        if not isinstance(f, str) or not f.strip():
            return error_response("VALIDATION_ERROR", f"Each flag must be a non-empty string, got: {f!r}")
    try:
        result = api_set_instance_config(inst_id)
        return success_single({"instance_id": inst_id, "flags": flags})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update cli_flags: {exc}")


def api_get_cli_flags(inst_id):
    """Get CLI flags — EP-CONSOLIDATE P3+P4 (thin wrapper)."""
    result = api_get_instance_config(inst_id)
    try:
        data = json.loads(result.get_data(as_text=True)) if hasattr(result, 'get_data') else result
        return success_single({"instance_id": inst_id, "flags": data.get("data", {}).get("cli_flags", [])})
    except Exception as _e:
        logger.debug("api_get_cli_flags inst=%d: json parse fallback: %s", inst_id, _e)
        return result


def api_set_herd_config(inst_id):
    """Set ENV overrides via config_override['env'] — direct write, no double-read."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))
    env_overrides = body.get("env", {})
    if not isinstance(env_overrides, dict):
        env_overrides = {}

    from db.adapters.instances import get_instance as _gi, update_instance as _ui
    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    co = _load_config_override(inst)
    # Merge env overrides into config_override["env"]
    if "env" not in co:
        co["env"] = {}
    co["env"].update(env_overrides)
    # Remove keys whose value is empty string (clear the override)
    co["env"] = {k: v for k, v in co["env"].items() if v != ""}

    try:
        _save_config_override(inst_id, co)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to save env overrides: {exc}")

    return success_single({"instance_id": inst_id, "env": env_overrides})


def api_get_gpu_override(inst_id):
    """Get GPU override — EP-CONSOLIDATE P3+P4 (thin wrapper)."""
    result = api_get_instance_config(inst_id)
    try:
        data = json.loads(result.get_data(as_text=True)) if hasattr(result, 'get_data') else result
        return success_single({"instance_id": inst_id, "gpu_override": data.get("data", {}).get("gpu_override", "")})
    except Exception as _e:
        logger.debug("api_get_gpu_override inst=%d: json parse fallback: %s", inst_id, _e)
        return result


def api_set_gpu_override(inst_id):
    """Set GPU override — EP-CONSOLIDATE P3+P4 (thin wrapper)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))
    gpu_override = body.get("gpu_override")
    if gpu_override is None:
        gpu_override = ""
    try:
        result = api_set_instance_config(inst_id)
        return success_single({"instance_id": inst_id, "gpu_override": gpu_override})
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to update gpu_override: {exc}")



def api_patch_expert_split(inst_id):
    """PATCH /instances/<id>/expert-split — dedicated expert split update endpoint.

    Atomic write to config_override.expert_split with validation and audit trail.
    Supports per-RPC mode overrides, extra OT flags, skip_n_first offset, etc.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    # Validate instance is a llama_server cluster node
    if inst.get("engine_type_id") != QR_ENGINE_LLAMA_SERVER:
        return error_response("CONFLICT_ERROR", "Expert split requires a llama_server instance")

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    co = _load_config_override(inst)

    # Ensure expert_split dict exists
    if "expert_split" not in co:
        co["expert_split"] = {}

    es = co["expert_split"]

    # Validate and merge experts value
    if "experts" in body:
        ev = body["experts"]
        try:
            ev = int(ev)
            if ev < 0 or ev > 1000:
                return error_response("VALIDATION_ERROR", "experts must be between 0 and 1000")
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "experts must be an integer 0-1000")
        es["experts"] = ev

    # Validate and merge _rpc_modes (per-RPC mode overrides)
    if "_rpc_modes" in body:
        rm = body["_rpc_modes"]
        if not isinstance(rm, dict):
            return error_response("VALIDATION_ERROR", "_rpc_modes must be a JSON object")
        valid_modes = ("a", "b", "c", "d", "s", "f")
        for rpc_id, mode_cfg in rm.items():
            if isinstance(mode_cfg, dict) and "mode" in mode_cfg:
                if mode_cfg["mode"] not in valid_modes:
                    return error_response("VALIDATION_ERROR",
                        f"Invalid mode '{mode_cfg['mode']}' for RPC {rpc_id}: must be one of {valid_modes}")
            elif isinstance(mode_cfg, str):
                if mode_cfg not in valid_modes:
                    return error_response("VALIDATION_ERROR",
                        f"Invalid mode '{mode_cfg}' for RPC {rpc_id}: must be one of {valid_modes}")
        es["_rpc_modes"] = rm

    # Validate and merge _rpc_experts (per-RPC expert counts)
    if "_rpc_experts" in body:
        re_data = body["_rpc_experts"]
        if not isinstance(re_data, dict):
            return error_response("VALIDATION_ERROR", "_rpc_experts must be a JSON object")
        for rpc_id, expert_val in re_data.items():
            try:
                expert_val = int(expert_val)
                if expert_val < 0 or expert_val > 1000:
                    return error_response("VALIDATION_ERROR", f"_rpc_experts[{rpc_id}] must be between 0 and 1000")
            except (ValueError, TypeError):
                return error_response("VALIDATION_ERROR", f"_rpc_experts[{rpc_id}] must be an integer 0-1000")
        es["_rpc_experts"] = re_data

    # Validate and merge skip_n_first offset
    if "skip_n_first" in body:
        snf = body["skip_n_first"]
        try:
            snf = int(snf)
            if snf < 0:
                return error_response("VALIDATION_ERROR", "skip_n_first must be >= 0")
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "skip_n_first must be an integer >= 0")
        es["skip_n_first"] = snf

    # Validate and merge extra_ot_flags
    if "extra_ot_flags" in body:
        eflags = body["extra_ot_flags"]
        if not isinstance(eflags, list):
            return error_response("VALIDATION_ERROR", "extra_ot_flags must be a JSON array")
        for flag in eflags:
            if not isinstance(flag, str) or not flag.strip():
                return error_response("VALIDATION_ERROR", "Each extra_ot_flag must be a non-empty string")
        es["extra_ot_flags"] = eflags

    # Save merged config_override (atomic write)
    try:
        _save_config_override(inst_id, co)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to save expert split config: {exc}")

    # Log audit trail
    logger.info("expert_split updated for instance %d: experts=%s modes=%s",
        inst_id, es.get("experts"), es.get("_rpc_modes"))

    # Return merged result
    return success_single({"instance_id": inst_id, "expert_split": es})


# ============================================================
# EP-CONSOLIDATE P3+P4: Unified Instance Config + Split Handlers
# ============================================================

def _load_config_override(inst):
    """Load and parse config_override from instance record.

    Handles both dict and JSON-string formats, normalises to dict.
    """
    co_raw = inst.get("config_override") or "{}"
    if isinstance(co_raw, str):
        try:
            return json.loads(co_raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return co_raw if isinstance(co_raw, dict) else {}


def _save_config_override(inst_id, co):
    """Save parsed config_override back to DB.

    Args:
        inst_id: Instance primary key.
        co: Dict of merged config_override values.
    """
    from db.adapters.instances import update_instance as _ui
    _ui(_CONFIG["db_path"], inst_id, config_override=json.dumps(co))


def api_get_instance_config(inst_id):
    """Unified GET /instances/<id>/config (EP-CONSOLIDATE P3+P4).

    Returns ALL config areas in a single response:
      { cli_flags, gpu_override, expert_split, split_mode, split_value, experts, draft }

    Args:
        inst_id: Instance primary key.

    Returns:
        JSON with all config areas merged from config_override and instance columns.
    """
    from db.adapters.instances import get_instance as _gi

    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    co = _load_config_override(inst)

    # CLI flags: from config_override or legacy column
    cli_flags = co.get("cli_flags", [])
    if not cli_flags:
        raw = inst.get("cli_flags") or "[]"
        try:
            cli_flags = json.loads(raw) if isinstance(raw, str) else []
        except (json.JSONDecodeError, TypeError):
            cli_flags = []
    if not isinstance(cli_flags, list):
        cli_flags = []

    # GPU override: qr_cluster_gpu_override from config_override
    gpu_override = co.get("qr_cluster_gpu_override") or ""

    # Expert split: from config_override with defaults
    expert_split = co.get("expert_split", {})
    if not isinstance(expert_split, dict):
        expert_split = {}
    expert_split.setdefault("template_prefix", "blk.")
    expert_split.setdefault("template_suffix", "ffn_(up|gate|down)_exps.*")
    expert_split.setdefault("skip_n_first", 0)

    # Split config from instance columns
    split_mode = inst.get("split_mode") or "layer"
    split_value = inst.get("split")
    experts = inst.get("experts")
    draft = inst.get("draft")

    # Target instance IDs (from column, not config_override)
    target_ids = inst.get("target_instance_ids") or []
    if isinstance(target_ids, str):
        try:
            target_ids = json.loads(target_ids)
        except (json.JSONDecodeError, TypeError):
            target_ids = []
    if not isinstance(target_ids, list):
        target_ids = []

    return success_single({
        "instance_id": inst_id,
        "cli_flags": cli_flags,
        "gpu_override": gpu_override,
        "expert_split": expert_split,
        "split_mode": split_mode,
        "split_value": split_value,
        "experts": experts,
        "draft": draft,
        "target_instance_ids": target_ids,
    })


def api_set_instance_config(inst_id):
    """Unified PUT /instances/<id>/config (EP-CONSOLIDATE P3+P4).

    Partial merge: any subset of fields can be sent in the body.
    Fields are merged into config_override and instance columns.

    Supported fields in request body:
      cli_flags, gpu_override, expert_split, split_mode, split_value, experts, draft

    Args:
        inst_id: Instance primary key.
        Body: Partial or full config dict.

    Returns:
        JSON with merged config values.
    """
    from db.adapters.instances import get_instance as _gi, update_instance as _ui

    inst = _gi(_CONFIG["db_path"], inst_id)
    if not inst:
        return error_response("RESOURCE_NOT_FOUND", f"Instance {inst_id} not found")

    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body.get("_error", "invalid body"))

    co = _load_config_override(inst)

    # CLI flags
    if "cli_flags" in body:
        flags = body["cli_flags"]
        if not isinstance(flags, list):
            return error_response("VALIDATION_ERROR", "cli_flags must be a JSON array")
        for f in flags:
            if not isinstance(f, str) or not f.strip():
                return error_response("VALIDATION_ERROR", f"Each flag must be a non-empty string, got: {f!r}")
        co["cli_flags"] = flags

    # ENV overrides (stored in config_override["env"])
    if "env" in body:
        env_overrides = body["env"]
        if not isinstance(env_overrides, dict):
            return error_response("VALIDATION_ERROR", "env must be a JSON object")
        if "env" not in co:
            co["env"] = {}
        co["env"].update(env_overrides)
        # Remove keys whose value is empty string (clear the override)
        co["env"] = {k: v for k, v in co["env"].items() if v != ""}

    # GPU override
    if "gpu_override" in body:
        gpu = body["gpu_override"]
        if gpu is None:
            gpu = ""
        if gpu == "" or gpu is None:
            co.pop("qr_cluster_gpu_override", None)
        else:
            co["qr_cluster_gpu_override"] = gpu

    # Expert split
    if "expert_split" in body:
        es = body["expert_split"]
        if not isinstance(es, dict):
            return error_response("VALIDATION_ERROR", "expert_split must be a JSON object")
        if "expert_split" not in co:
            co["expert_split"] = {}
        co["expert_split"].update(es)

    # Split mode (instance column only)
    if "split_mode" in body:
        mode = body["split_mode"]
        valid_modes = ("layer", "row", "tensor")
        if mode not in valid_modes:
            return error_response("VALIDATION_ERROR", f"split_mode must be one of {valid_modes}")
        _ui(_CONFIG["db_path"], inst_id, split_mode=mode)

    # Split value (instance column only)
    if "split_value" in body or "split" in body:
        sv = body.get("split_value", body.get("split"))
        if sv is not None:
            try:
                sv = int(sv)
                if sv < 0 or sv > 100:
                    return error_response("VALIDATION_ERROR", "split must be between 0 and 100")
            except (ValueError, TypeError):
                return error_response("VALIDATION_ERROR", "split must be an integer 0-100")
        _ui(_CONFIG["db_path"], inst_id, split=sv)

    # Experts (instance column only)
    if "experts" in body:
        ev = body["experts"]
        try:
            ev = int(ev)
            if ev < 0 or ev > 1000:
                return error_response("VALIDATION_ERROR", "experts must be between 0 and 1000")
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "experts must be an integer 0-1000")
        _ui(_CONFIG["db_path"], inst_id, experts=ev)

    # Draft (instance column only)
    if "draft" in body:
        dv = body["draft"]
        try:
            dv = int(dv)
            if dv < 0 or dv > 100:
                return error_response("VALIDATION_ERROR", "draft must be between 0 and 100")
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "draft must be an integer 0-100")
        _ui(_CONFIG["db_path"], inst_id, draft=dv)

    # Target instance IDs (instance column only — not config_override)
    if "target_instance_ids" in body:
        tids = body["target_instance_ids"]
        if tids is None:
            tids = []
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except (json.JSONDecodeError, TypeError):
                tids = []
        if not isinstance(tids, list):
            return error_response("VALIDATION_ERROR",
                "target_instance_ids must be a JSON array of instance IDs")
        for tid in tids:
            try:
                int(tid)
            except (ValueError, TypeError):
                return error_response("VALIDATION_ERROR",
                    f"Each target_instance_id must be an integer, got: {tid!r}")
        _ui(_CONFIG["db_path"], inst_id, target_instance_ids=json.dumps(tids))

    # Save merged config_override
    try:
        _save_config_override(inst_id, co)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", f"Failed to save config: {exc}")

    # Return merged result
    return api_get_instance_config(inst_id)


def api_deploy_preview(inst_id):
    """Return the computed deployment config (env + CLI args) for a llama-server instance.

    Uses build_llama_server_env() to compute the full merged environment and CLI string
    that would be used on deploy. Useful for previewing RPC bindings, tensor_split, etc.

    Args:
        inst_id: Integer primary key of the llama-server instance.

    Returns:
        JSON with env dict, cli_args, tensor_split, split_mode, rpc_bindings.
    """
    try:
        from lib.lib_cluster_env_builder import build_llama_server_env as _build_env
        result = _build_env(_CONFIG["db_path"], inst_id)
        # Add cli_flags and draft_devices from instance data
        # Read from config_override.cli_flags (unified herd state), fallback to legacy column
        from db.adapters.instances import get_instance as _gi
        inst = _gi(_CONFIG["db_path"], inst_id)
        parsed_flags = []
        if inst:
            co = inst.get("config_override") or {}
            if isinstance(co, dict):
                flags = co.get("cli_flags", [])
                if not flags:
                    # Fallback to legacy cli_flags column for backward compat
                    raw = inst.get("cli_flags") or "[]"
                    try:
                        parsed_flags = json.loads(raw) if isinstance(raw, str) else []
                    except (json.JSONDecodeError, TypeError):
                        parsed_flags = []
                elif isinstance(flags, list):
                    parsed_flags = flags
        # Compute draft devices from rpc_bindings
        draft_devices = []
        for idx, b in enumerate(result.get("rpc_bindings", [])):
            d = b.get("draft", 0)
            if isinstance(d, int) and d > 0:
                draft_devices.append(f"RPC{idx}")
        result["cli_flags"] = parsed_flags
        result["draft_devices"] = draft_devices
        # Wrap in success response format
        return success_single(result)
    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_merged_config(inst_id):
    """Return the complete merged configuration with source annotations for each key.

    Shows the 6-layer merge trace: engine defaults → preset → model → cluster binding →
    instance override → metadata. Each key is annotated with its source layer.

    Args:
        inst_id: Integer primary key of the instance.

    Returns:
        JSON with env/cli_opts/model sections, each key annotated with source_layer,
        plus layer_summary showing keys contributed per layer.
    """
    try:
        from db.adapters.instances import merge_configs as _merge
        result = _merge(_CONFIG["db_path"], inst_id)

        # Build annotated response format for WebUI
        env_annotated = {}
        model_annotated = {}
        cli_annotated = []

        # Layer source annotation from _layers metadata
        layers = result.get("_layers", {})

        # Annotate env keys
        for key, val in result.get("env", {}).items():
            if key.startswith("_"):
                continue  # Skip internal metadata keys
            # Find which layer contributed this key — prefer highest priority layer.
            # Layer priority: instance_override > cluster_binding > model > preset > engine_default > metadata
            source = "unknown"
            for priority_name in _LAYERS_PRIORITY:
                if priority_name in layers:
                    layer_data = layers[priority_name]
                    if isinstance(layer_data, dict) and key in layer_data.get("env_keys", []):
                        source = priority_name
                        break
            env_annotated[key] = {"value": val, "source_layer": source}

        # Annotate model keys
        for key, val in result.get("model", {}).items():
            # Prefer highest priority layer for model keys too
            source = "unknown"
            for priority_name in _LAYERS_PRIORITY:
                if priority_name in layers:
                    layer_data = layers[priority_name]
                    if isinstance(layer_data, dict) and key in layer_data.get("model_keys", []):
                        source = priority_name
                        break
            model_annotated[key] = {"value": val, "source_layer": source}

        # Annotate CLI opts (source from preset or instance_override cli_opts_count)
        cli_opts = result.get("cli_opts", [])
        for opt in cli_opts:
            cli_annotated.append({"value": opt, "source_layer": "preset"})

        # Build layer summary
        layer_summary_layers = []
        layer_name_map = {
            "engine_default": "Engine default configs",
            "preset": "Preset config template",
            "model": "Model definition",
            "cluster_binding": "Cluster/RPC binding",
            "instance_override": "Per-instance override",
            "metadata": "Metadata injection",
        }
        for layer_key, count_info in layers.items():
             if isinstance(count_info, dict):
                 env_keys = count_info.get("env_keys", [])
                 model_keys = count_info.get("model_keys", [])
                 total = len(env_keys) + len(model_keys) + int(count_info.get("cli_opts_count", 0))
             else:
                 total = 0
             layer_summary_layers.append({
                  "name": layer_name_map.get(layer_key, layer_key),
                  "keys_contribution": total if isinstance(total, (int, float)) else 0,
             })

        # Extract actual overrides from instance_override layer
        instance_ov = layers.get("instance_override", {})
        actual_overrides = {}
        if isinstance(instance_ov, dict):
            ov_env_keys = instance_ov.get("env_keys", [])
            ov_model_keys = instance_ov.get("model_keys", [])
            # env_annotated and model_annotated contain the actual values with source_layer
            for key in ov_env_keys:
                if key in env_annotated:
                    actual_overrides[key] = env_annotated[key]["value"]
            for key in ov_model_keys:
                if key in model_annotated:
                    actual_overrides[key] = model_annotated[key]["value"]

        return jsonify({
            "status": "ok",
            "data": {
                "env": env_annotated,
                "cli_opts": cli_annotated,
                "model": model_annotated,
                "actual_overrides": actual_overrides,
                "start_on_boot": result.get("start_on_boot"),
            },
            "layer_summary": {"layers": layer_summary_layers},
        }), 200

    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


# ---------------------------------------------------------------------------
# CONFIG-1 Phase 2: Config-levels endpoints
# ---------------------------------------------------------------------------


def api_get_config_levels(inst_id):
    """Return all config levels for an instance.

    GET /api/v1/instances/<id>/config-levels
    GET /api/v1/instances/<id>/config-levels?level=N (single level)

    Args:
        inst_id: Instance primary key.

    Returns:
        JSON with list of config layers (env, cli_opts, model, metadata per layer).
    """
    try:
        from db.adapters.config_levels import get_all_config_levels as _get_all
        from db.adapters.instances import get_instance as _gi

        inst = _gi(_CONFIG["db_path"], inst_id)
        if not inst:
            return error_response("INSTANCE_NOT_FOUND", f"Instance {inst_id} not found")

        import flask
        level_filter = flask.request.args.get("level")

        all_levels = _get_all(_CONFIG["db_path"], inst_id)
        if level_filter:
            try:
                level_filter = int(level_filter)
            except (ValueError, TypeError):
                return error_response("VALIDATION_ERROR", f"Invalid level parameter: {level_filter}")
            all_levels = [l for l in all_levels if l["level"] == level_filter]

        return jsonify({
            "status": "ok",
            "data": {"instance_id": inst_id, "levels": all_levels},
        }), 200

    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


