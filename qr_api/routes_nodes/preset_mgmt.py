from flask import request, jsonify
import logging as _logging

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG

logger = _logging.getLogger(__name__)


def api_list_presets(engine_type):
    """List presets for an engine type."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import list_presets as _lp
    from db.sqlite import pool as _pool
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    presets = _lp(_CONFIG["db_path"], engine_type_id=et_id)
    # Apply ?q= search filter (name, category, tags, model_name)
    q = request.args.get("q", "").strip()
    if q:
        presets = [p for p in presets if
                   q.lower() in p.get("name", "").lower() or
                   q.lower() in str(p.get("category") or "").lower() or
                   q.lower() in str(p.get("tags") or "").lower() or
                   q.lower() in str(p.get("model_name") or "").lower()]
    # Enrich with model_name, size, quantization, mmproj/draft from DB
    with _pool(_CONFIG["db_path"]) as conn:
        for p in presets:
            mid = p.get("model_id")
            if mid:
                mrow = conn.execute(
                    "SELECT name, quantization, draft_model_path, mmproj_path, size_bytes FROM engine_models WHERE id = ?", (mid,)
                ).fetchone()
                p["model_name"] = mrow["name"] if mrow else None
                p["quantization"] = mrow["quantization"] if mrow else None
                p["draft_model"] = "yes" if (mrow and mrow["draft_model_path"]) else "no"
                p["mmproj_model"] = "yes" if (mrow and mrow["mmproj_path"]) else "no"
                # Compute human-readable size for display
                sz = mrow["size_bytes"] if (mrow and mrow["size_bytes"]) else 0
                if sz and isinstance(sz, (int, float)) and sz > 0:
                    if sz >= 1073741824:
                        p["model_size_display"] = "{:.1f} GB".format(sz / 1073741824)
                    elif sz >= 1048576:
                        p["model_size_display"] = "{:.0f} MB".format(sz / 1048576)
                    else:
                        p["model_size_display"] = "{:.0f} KB".format(sz / 1024)
                    # Numeric sort key (bytes)
                    p["model_size_sort"] = str(sz)
                else:
                    p["model_size_display"] = "—"
                    p["model_size_sort"] = ""
            else:
                p["model_name"] = None
                p["quantization"] = None
                p["draft_model"] = "no"
                p["mmproj_model"] = "no"
                p["model_size_display"] = "—"
                p["model_size_sort"] = ""
            p["gpu_device"] = p.get("gpu_device")
    # Add relative age for each preset
    from lib.lib_utils import relative_age
    for p in presets:
        p["age_created"] = relative_age(p.get("created_at"))
    return success_list(presets)


def api_create_preset(engine_type):
    """Create a new preset for an engine type."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import add_preset as _ap
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    name = body.get("name")
    if not name:
        return error_response("VALIDATION_ERROR", "preset name is required")

    # Pre-check for duplicate name within engine_type_id
    from db.sqlite import pool as _pool
    with _pool(_CONFIG["db_path"]) as conn:
        existing = conn.execute(
            "SELECT id, name FROM engine_presets WHERE engine_type_id = ? AND name = ?",
            (et_id, name),
        ).fetchone()
    if existing is not None:
        return error_response("CONFLICT_ERROR",
                                f"Preset '{name}' already exists for this engine type (id={existing['id']})",
                                status_code=409)

    try:
        preset = _ap(_CONFIG["db_path"], et_id, name=name,
                        category=body.get("category", "default"),
                        config_template=body.get("config_template", {}),
                        model_path=body.get("model_path"),
                        tags=body.get("tags", []),
                        model_id=body.get("model_id"),
                        gpu_device=body.get("gpu_device"))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(preset)


def api_get_preset(engine_type, preset_id):
    """Get a single preset by id.

    Args:
        engine_type: Engine type name string.
        preset_id: Integer primary key of the preset.

    Returns:
        Single preset dict with affected_instances count.
    """
    from db.sqlite import pool as _pool
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import get_preset as _gp

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    preset = _gp(_CONFIG["db_path"], preset_id)
    if preset is None:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    # Enrich with model_name
    mid = preset.get("model_id")
    if mid:
        with _pool(_CONFIG["db_path"]) as conn:
            mrow = conn.execute(
                "SELECT name FROM engine_models WHERE id = ?", (mid,)
            ).fetchone()
            preset["model_name"] = mrow["name"] if mrow else None
    else:
        preset["model_name"] = None
    preset["gpu_device"] = preset.get("gpu_device")

    # Verify the preset belongs to this engine type
    if et_id is not None and preset.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                                f"Preset {preset_id} belongs to a different engine type")

    # Get instances using this preset
    affected = []
    if et_id is not None:
        from db.adapters.instances import list_instances_by_preset as _libp
        instances = _libp(_CONFIG["db_path"], preset_id)
        for inst in instances:
            affected.append({
                "id": inst["id"],
                "name": inst["name"],
                "node_name": inst.get("node_name", ""),
                "state": inst["state"],
            })

    return jsonify({"status": "ok", "data": preset,
                        "affected_instances": affected}), 200


def api_update_preset(engine_type, preset_id):
    """Update a preset with affected instances count."""
    from db.adapters.engine_types import list_engine_types as _let
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.presets import update_preset as _up
    try:
        preset = _up(_CONFIG["db_path"], preset_id, **body)
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    if preset is None:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    # Find engine type id for instance lookup
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    # Get instances using this preset
    affected = []
    if et_id is not None:
        from db.adapters.instances import list_instances_by_preset as _libp
        instances = _libp(_CONFIG["db_path"], preset_id)
        for inst in instances:
            affected.append({
                "id": inst["id"],
                "name": inst["name"],
                "node_name": inst.get("node_name", ""),
                "state": inst["state"],
            })

    return jsonify({"status": "ok", "data": preset,
                        "affected_instances": affected}), 200


def api_preset_restart_all(engine_type, preset_id):
    """Restart all instances using a specific preset.

    Updates each instance via the update endpoint which triggers deploy
    if the preset config has changed.

    Args:
        engine_type: Engine type name string.
        preset_id: Integer primary key of the preset.

    Returns:
        dict with restart results per instance.
    """
    from db.adapters.presets import get_preset as _gp
    from db.adapters.engine_types import list_engine_types as _let

    preset = _gp(_CONFIG["db_path"], preset_id)
    if preset is None:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    # Get all instances using this preset
    from db.adapters.instances import list_instances_by_preset as _libp
    instances = _libp(_CONFIG["db_path"], preset_id)

    results = []
    for inst in instances:
        iid = inst["id"]
        result = {"instance_id": iid, "name": inst["name"]}
        try:
            # Stop if running
            if inst["state"] == "running":
                from db.adapters.instances import transition_state as _ts
                try:
                    _ts(_CONFIG["db_path"], iid, "stopping")
                    _ts(_CONFIG["db_path"], iid, "stopped")
                except Exception as _e:
                    logger.debug("preset_restart stop failed for inst %d: %s", iid, _e)

            # Trigger deploy via the deploy_instance function (skip_build since preset restart is config-only)
            deploy_result = deploy_instance(_CONFIG["db_path"], iid, skip_build=True)
            result["deploy"] = deploy_result

            if deploy_result.get("success"):
                from db.adapters.instances import log_action as _log
                _log(_CONFIG["db_path"], iid, "preset_restart", "success")
        except Exception as exc:
            result["deploy"] = {"success": False, "message": str(exc)}
            try:
                from db.adapters.instances import log_action as _log
                _log(_CONFIG["db_path"], iid, "preset_restart", "failed",
                        detail={"error": str(exc)})
            except Exception as _e:
                logger.debug("preset_restart log_action failed for inst %d: %s", iid, _e)

        results.append(result)

    succeeded = sum(1 for r in results if r.get("deploy", {}).get("success"))
    return success_list(results, meta={"succeeded": succeeded}), 200


def api_delete_preset(engine_type, preset_id):
    """Delete a preset."""
    from db.adapters.presets import delete_preset as _dp
    try:
        deleted = _dp(_CONFIG["db_path"], preset_id)
    except Exception as exc:
        return error_response("BAD_REQUEST", str(exc))
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Preset {preset_id} not found")

    return success_single({"preset_id": preset_id, "deleted": True})


def api_remove_empty_presets(engine_type):
    """Find presets with no model assigned (model_id IS NULL) and id > 100.

    POST /api/v1/engine/<type>/presets/remove-empty
    Body: {"preset_ids": [NNN, ...]} — optional filter; if empty, scans all.

    Returns:
        dict with preset list to be deleted, count, and skipped (in-use) count.
    """
    from db.sqlite import pool as _pool

    body = request.get_json(silent=True) or {}
    preset_ids = body.get("preset_ids", [])

    with _pool(_CONFIG["db_path"]) as conn:
        if preset_ids and len(preset_ids) > 0:
            ids_str = ",".join(str(i) for i in preset_ids)
            rows = conn.execute(
                f"SELECT id, name FROM engine_presets WHERE id IN ({ids_str})"
            ).fetchall()
        else:
            # Find all presets with no model (model_id IS NULL) or orphaned model reference (model deleted)
            # id > 100 excludes the seed Router Mode preset (id=100) which is intentionally model-less
            rows = conn.execute(
                "SELECT id, name FROM engine_presets WHERE (model_id IS NULL OR model_id NOT IN (SELECT id FROM engine_models)) AND id > 100 ORDER BY id"
            ).fetchall()

    if not rows:
        return success_single({
            "presets": [],
            "count": 0,
            "skipped_count": 0,
        })

    # Check which are in use by instances (skip those)
    in_use_ids = set()
    with _pool(_CONFIG["db_path"]) as conn:
        for row in rows:
            ref = conn.execute(
                "SELECT id FROM instances WHERE preset_id = ?", (row["id"],)
            ).fetchone()
            if ref:
                in_use_ids.add(row["id"])

    skip_count = len(in_use_ids.intersection({r["id"] for r in rows}))
    presets = [{"id": r["id"], "name": r["name"]} for r in rows]

    return success_single({
        "presets": presets,
        "count": len(presets),
        "skipped_count": skip_count,
    })


def api_remove_empty_presets_confirm(engine_type):
    """Confirm deletion of specified empty presets.

    POST /api/v1/engine/<type>/presets/remove-empty/confirm
    Body: {"preset_ids": [NNN, ...]}

    Returns:
        dict with deleted_count and skipped_count.
    """
    from db.sqlite import pool as _pool

    body = request.get_json(silent=True) or {}
    preset_ids = body.get("preset_ids", [])

    if not preset_ids:
        return error_response("VALIDATION_ERROR", "No preset IDs provided")

    deleted_count = 0
    skipped_count = 0
    with _pool(_CONFIG["db_path"]) as conn:
        for pid in preset_ids:
            # Check FK constraint — skip if used by any instance
            ref = conn.execute(
                "SELECT id FROM instances WHERE preset_id = ?", (pid,)
            ).fetchone()
            if ref:
                skipped_count += 1
                continue
            try:
                rc = conn.execute("DELETE FROM engine_presets WHERE id = ?", (pid,)).rowcount
                if rc > 0:
                    deleted_count += 1
                else:
                    skipped_count += 1
            except Exception as e:
                logger.debug("Delete failed for preset %d: %s", pid, e)
                skipped_count += 1

    return success_single({
        "deleted_count": deleted_count,
        "skipped_count": skipped_count,
    })


def api_clone_preset(engine_type, preset_id):
    """Clone a preset 1:1 with unique name suffix."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.presets import PresetError, clone_preset as _cp

    et = _get_et(_CONFIG["db_path"], engine_type)
    if not et:
        return error_response("ENGINE_NOT_FOUND", f"Engine '{engine_type}' not found")
    et_id = et["id"]

    try:
        new_preset = _cp(_CONFIG["db_path"], preset_id, et_id)
    except PresetError as exc:
        return error_response("BAD_REQUEST", str(exc))

    return success_single({"id": new_preset["id"], "name": new_preset["name"]})


