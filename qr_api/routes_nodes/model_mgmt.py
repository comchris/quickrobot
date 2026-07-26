from flask import request, jsonify
import logging as _logging

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_SERVER_NAME

logger = _logging.getLogger(__name__)


def api_list_all_models():
    """List ALL models across all engines, with optional ?q= search filter."""
    from db.adapters.engine_types import list_engine_types as _let
    from db.adapters.models import list_models as _lm

    models = _lm(_CONFIG["db_path"], engine_type_id=None)

    # Enrich each model with engine type name and preset usage count
    et_map = {}
    from db.adapters.engine_types import list_engine_types as _let
    for et in _let(_CONFIG["db_path"]):
        et_map[et["id"]] = et["name"]
    for m in models:
        m["engine_type_name"] = et_map.get(m.get("engine_type_id"), "unknown")

    # Count preset usage per model (model_id FK in engine_presets)
    from db.sqlite import pool
    with pool(_CONFIG["db_path"]) as conn:
        preset_counts = {}
        for row in conn.execute(
            "SELECT model_id, COUNT(*) as cnt FROM engine_presets WHERE model_id IS NOT NULL GROUP BY model_id"
        ).fetchall():
            preset_counts[row["model_id"]] = row["cnt"]
    for m in models:
        m["preset_count"] = preset_counts.get(m["id"], 0)

    # Filter by engine_type (optional — e.g. from preset list nav)
    et_name = request.args.get("engine", "").strip()
    if et_name:
        from db.adapters.engine_types import get_engine_type_by_name as _get_et
        et = _get_et(_CONFIG["db_path"], et_name)
        et_id = et["id"] if et else None
        if et_id:
            models = [m for m in models if m.get("engine_type_id") == et_id]

    # Count active/inactive totals BEFORE any filtering (for counter display)
    total_active = sum(1 for m in models if m.get("is_active", 1))
    total_inactive = sum(1 for m in models if not m.get("is_active", 1))

    # Filter by is_active (default: show active only)
    show_inactive = request.args.get("include_inactive", "false").lower() == "true"
    if not show_inactive:
        models = [m for m in models if m.get("is_active", 1)]

    # Apply ?q= search filter (name, model_path, quantization)
    q = request.args.get("q", "").strip()
    if q:
        ql = q.lower()
        models = [m for m in models if
                  ql in str(m.get("name") or "").lower() or
                  ql in str(m.get("model_path") or "").lower() or
                  ql in str(m.get("quantization") or "").lower()]

    # Apply ?draft_filter=N — show only the model referenced as a draft
    df = request.args.get("draft_filter", "").strip()
    if df:
        try:
            df_id = int(df)
            models = [m for m in models if m.get("id") == df_id]
        except ValueError:
            pass

    # Add relative age for each model
    from lib.lib_utils import relative_age
    for m in models:
        m["age_created"] = relative_age(m.get("created_at"))
    return success_list(models, meta={"active_count": total_active, "inactive_count": total_inactive})


def api_get_model_global(model_id):
    """Get details for a single model by ID (global, no engine type check)."""
    from db.adapters.models import get_model as _gm

    model = _gm(_CONFIG["db_path"], model_id)
    if model is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")
    # Enrich with engine type name
    from db.adapters.engine_types import get_engine_type as _get_et_id
    et = _get_et_id(_CONFIG["db_path"], model.get("engine_type_id"))
    if et:
        model["engine_type_name"] = et["name"]
    else:
        model["engine_type_name"] = "unknown"
    return success_single(model)


def api_update_model_global(model_id):
    """Update an existing model by ID (global, no engine type check)."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.models import get_model as _gm, update_model as _um

    existing = _gm(_CONFIG["db_path"], model_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    # Build allowed fields dynamically (same as per-engine handler)
    from db.adapters.models import ALLOWED_FIELDS
    update_fields = {}
    for key in ALLOWED_FIELDS:
        if key in body and body[key] is not None:
            update_fields[key] = body[key]
        elif key == "name" or key == "model_path":
            # Always require these
            pass

    try:
        _um(_CONFIG["db_path"], model_id, **update_fields)
    except Exception as exc:
        return error_response("UPDATE_ERROR", str(exc))

    updated = _gm(_CONFIG["db_path"], model_id)
    return success_single(updated)


def api_create_model_global():
    """Create a new global model entry."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.models import add_model as _am

    name = body.get("name")
    model_path = body.get("model_path")
    if not name or not model_path:
        return error_response("VALIDATION_ERROR", "name and model_path are required")

    engine_type_id = body.get("engine_type_id", QR_ENGINE_LLAMA_SERVER)

    try:
        model = _am(_CONFIG["db_path"], engine_type_id, name=name, model_path=model_path,
                    mmproj_path=body.get("mmproj_path") or None,
                    draft_model_path=body.get("draft_model_path") or None,
                    size_bytes=body.get("size_bytes"),
                    last_modified=body.get("last_modified"),
                    quantization=body.get("quantization"),
                    model_params=body.get("model_params"))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(model)


def api_clear_all_models():
    """Delete all models from the database.

    POST body: {"engine_type": "llama_server"} (required) or omit for all engines.

    Args:
        engine_type: Engine type to clear. If omitted, clears all models across all engines.

    Returns:
        dict with count of deleted models.
    """
    from db.sqlite import pool as _pool
    from db.adapters.engine_types import list_engine_types as _let

    data = request.get_json() or {}
    engine_type = data.get("engine_type")  # optional; None clears all engines

    try:
        with _pool(_CONFIG["db_path"]) as conn:
            if engine_type:
                from db.adapters.engine_types import get_engine_type_by_name as _get_et
                et = _get_et(_CONFIG["db_path"], engine_type)
                et_id = et["id"] if et else None
                if et_id:
                    count = conn.execute("DELETE FROM engine_models WHERE engine_type_id = ?", (et_id,)).rowcount
                else:
                    count = 0
            else:
                count = conn.execute("DELETE FROM engine_models").rowcount
        return success_single({"deleted_count": count})
    except Exception as exc:
        return error_response("INTERNAL_ERROR", str(exc))


def api_model_active(model_id):
    """Toggle model active/inactive state."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])
    val = body.get("is_active")
    if val is None:
        return error_response("VALIDATION_ERROR", "Request must include is_active (0 or 1)")
    if val not in (0, 1):
        return error_response("VALIDATION_ERROR", "is_active must be 0 or 1")
    from db.adapters.models import update_model as _um
    model = _um(_CONFIG["db_path"], model_id, is_active=val)
    if not model:
        return error_response("NOT_FOUND", f"Model {model_id} not found")
    return success_single({"is_active": model["is_active"]})


def api_scan_models_agnostic():
    """Engine-agnostic GGUF model discovery on a specific node.

    Required params:
        ?node=<node_id> -- node to scan (required, no default "scan all")
        ?compute_checksums=0|1 -- include SHA256 computation (default 0)

    Returns:
        JSON with new/existing/mismatch counts and discovered model details.
    """
    from db.adapters.engine_types import list_engine_types as _let
    from db.adapters.nodes import list_nodes as _ln
    from lib.lib_ansible_runner import scan_models as _scan

    # Required: node parameter
    target_node_id = request.args.get("node")
    if not target_node_id:
        return error_response("VALIDATION_ERROR", "?node=<id> is required")

    nodes = _ln(_CONFIG["db_path"])
    active_nodes = [n for n in nodes if n.get("status") == "active"]

    target = next((n for n in active_nodes if str(n.get("id")) == str(target_node_id)), None)
    if not target:
        return error_response("RESOURCE_NOT_FOUND",
                              f"Node {target_node_id} not found or not active")

    # compute_checksums default to 0 (fast scan)
    compute_checksums = request.args.get("compute_checksums", "0") == "1"

    # Use the llama_server engine_type_id for scanning
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    et = _get_et(_CONFIG["db_path"], QR_ENGINE_LLAMA_SERVER_NAME)
    et_id = et["id"] if et else None
    if not et_id:
        return error_response("RESOURCE_NOT_FOUND", "llama_server engine type not found")

    limit_str = target.get("hostname", target.get("name", ""))

    try:
        result = _scan(engine_type_id=et_id, limit=limit_str,
                       db_path=_CONFIG["db_path"])
        # Add compute_checksums flag for future use
        result["compute_checksums"] = compute_checksums
        return success_single(result)
    except Exception as exc:
        return error_response("SCAN_FAILED", str(exc))


def api_checksum_diff(engine_type):
    """List models where computed hash != expected hash or not yet verified.

    Args:
        engine_type: Engine type name.

    Returns:
        List of models with hash mismatch or missing verification.
    """
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import list_models as _lm

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    models = _lm(_CONFIG["db_path"], engine_type_id=et_id)
    diff_models = [m for m in models
                   if not m.get("sha256_model") or not m.get("sha256_verified_at_model")]

    return success_list(diff_models)


# ---------------------------------------------------------------------------
# Remove missing models endpoint (model_mgmt)
# ---------------------------------------------------------------------------

def api_remove_missing_models_confirm():
    """Confirm deletion of specified models.

    POST /api/v1/models/remove-missing/confirm
    Body: {"model_ids": [101, 102, ...]}

    Returns:
        dict with deleted_count.
    """
    from db.sqlite import pool as _pool
    from db.adapters.models import delete_model as _dm

    body = request.get_json(silent=True) or {}
    model_ids = body.get("model_ids", [])

    if not model_ids:
        return error_response("VALIDATION_ERROR", "No model IDs provided")

    deleted_count = 0
    with _pool(_CONFIG["db_path"]) as conn:
        for mid in model_ids:
            try:
                rc = conn.execute("DELETE FROM engine_models WHERE id = ?", (mid,)).rowcount
                if rc > 0:
                    deleted_count += 1
            except Exception as e:
                logger.debug("Delete failed for model %d: %s", mid, e)

    return success_single({"deleted_count": deleted_count})


def api_remove_missing_models():
    """Remove models whose files no longer exist on their host node.

    Accepts a JSON body with list of model IDs to check:
        POST /api/v1/models/remove-missing
        Body: {"model_ids": [101, 102, ...]}

    If no body provided, scans ALL models against their host nodes.

    Returns:
        dict with status and missing_model_ids list.
    """
    from db.sqlite import pool as _pool
    from db.adapters.nodes import get_node as _get_node
    import subprocess as _sub
    import json as _json

    body = request.get_json(silent=True) or {}
    model_ids = body.get("model_ids")

    with _pool(_CONFIG["db_path"]) as conn:
        if model_ids and len(model_ids) > 0:
            ids_str = ",".join(str(i) for i in model_ids)
            rows = conn.execute(
                f"SELECT id, model_path, host_id FROM engine_models WHERE id IN ({ids_str})"
            ).fetchall()
        else:
            # Scan ALL models — check existence on their host node
            rows = conn.execute(
                "SELECT id, model_path, host_id FROM engine_models WHERE model_path IS NOT NULL AND host_id IS NOT NULL"
            ).fetchall()

    if not rows:
        return success_single({"missing_model_ids": [], "missing_count": 0})

    # Resolve host_id → hostname for SSH check
    with _pool(_CONFIG["db_path"]) as conn:
        node_map = {}
        for nid in {r["host_id"] for r in rows}:
            node = _get_node(_CONFIG["db_path"], nid)
            if node and node.get("hostname"):
                node_map[nid] = node["hostname"]

    # Check file existence via SSH
    ssh_opts = (
        "-o StrictHostKeyChecking=accept-new "
        "-o ConnectTimeout=5 "
        "-o BatchMode=yes"
    )
    ansible_user = _CONFIG.get("ansible_user") or ""
    ansible_key = _CONFIG.get("ansible_key_path") or ""

    missing_ids = []
    for row in rows:
        hid = row["host_id"]
        hostname = node_map.get(hid)
        if not hostname:
            continue  # Can't check — skip
        path = row["model_path"] or ""
        cmd = f"ssh {ssh_opts}"
        if ansible_key:
            cmd += f" -i {ansible_key}"
        if ansible_user:
            cmd += f" -l {ansible_user}"
        cmd += f" {hostname} \"test -f '{path}' && echo EXISTS || echo MISSING\""
        try:
            result = _sub.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            output = result.stdout.strip()
            if output != "EXISTS":
                missing_ids.append(row["id"])
        except Exception as e:
            logger.debug("SSH check failed for model %d on %s: %s", row["id"], hostname, e)
            # If we can't connect, treat as missing (file might be gone)
            missing_ids.append(row["id"])

    return success_single({
        "missing_model_ids": missing_ids,
        "missing_count": len(missing_ids),
    })


# ---------------------------------------------------------------------------
# Global config endpoints
# ---------------------------------------------------------------------------

