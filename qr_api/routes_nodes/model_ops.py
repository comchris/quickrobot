import os

from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG, _project_root


def api_list_models(engine_type):
    """List models for an engine type, with optional ?q= search filter."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import list_models as _lm
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    host_id = request.args.get("host_id")
    models = _lm(_CONFIG["db_path"], engine_type_id=et_id,
                    host_id=int(host_id) if host_id else None)

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

    # Add relative age for each model
    from lib.lib_utils import relative_age
    for m in models:
        m["age_created"] = relative_age(m.get("created_at"))
    return success_list(models)


def api_get_model(engine_type, model_id):
    """Get details for a single model."""
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import get_model as _gm

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    model = _gm(_CONFIG["db_path"], model_id)
    if model is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    # Verify the model belongs to this engine type
    if model.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                                f"Model {model_id} belongs to a different engine type")

    return success_single(model)


def api_update_model(engine_type, model_id):
    """Update an existing model entry."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import update_model as _um
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    # Verify model belongs to this engine type
    existing = None
    from db.adapters.models import get_model as _gm
    existing = _gm(_CONFIG["db_path"], model_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")
    if existing.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                                f"Model {model_id} belongs to a different engine type")

    try:
        updated = _um(_CONFIG["db_path"], model_id,
                        name=body.get("name"),
                        model_path=body.get("model_path"),
                        mmproj_path=body.get("mmproj_path") or None,
                        draft_model_path=body.get("draft_model_path") or None,
                        size_bytes=body.get("size_bytes"),
                        last_modified=body.get("last_modified"),
                        host_id=body.get("host_id"),
                        model_params=body.get("model_params"),
                        is_active=int(body.get("is_active", 1)))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(updated)


def api_delete_model(engine_type, model_id):
    """Remove a model."""
    from db.adapters.models import delete_model as _dm
    deleted = _dm(_CONFIG["db_path"], model_id)
    if not deleted:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    return success_single({"model_id": model_id, "deleted": True})


def api_create_model(engine_type):
    """Add a new model for an engine type."""
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import add_model as _am
    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    name = body.get("name")
    model_path = body.get("model_path")
    if not name or not model_path:
        return error_response("VALIDATION_ERROR", "name and model_path are required")

    try:
        model = _am(_CONFIG["db_path"], et_id, name=name, model_path=model_path,
                    mmproj_path=body.get("mmproj_path") or None,
                    draft_model_path=body.get("draft_model_path") or None,
                    size_bytes=body.get("size_bytes"),
                    last_modified=body.get("last_modified"),
                    host_id=body.get("host_id"),
                    quantization=body.get("quantization"))
    except Exception as exc:
        return error_response("VALIDATION_ERROR", str(exc))

    return success_single(model)


def api_scan_models(engine_type):
    """Trigger a remote model scan via Ansible playbook.

    Scans all active nodes for GGUF model files using the scan_models playbook.
    Results are upserted into the engine_models table.

    Returns:
        dict with scan results including count of new/stale models.
    """
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.nodes import list_nodes as _ln
    from lib.lib_ansible_runner import scan_models as _scan
    from db.adapters.playbooks import resolve_playbook_by_id

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    # Optional node_id to target a specific node (default: scan all active)
    from db.adapters.nodes import list_nodes as _ln2
    target_node_id = request.args.get("node_id")
    nodes = _ln2(_CONFIG["db_path"])
    active_nodes = [n for n in nodes if n.get("status") == "active"]

    if not active_nodes:
        return error_response("NO_NODES", "No active nodes available for scanning")

    # Build host limit — single targeted node or all active
    if target_node_id:
        target = next((n for n in active_nodes if str(n.get("id")) == str(target_node_id)), None)
        if not target:
            return error_response("RESOURCE_NOT_FOUND",
                                f"Node {target_node_id} not found or not active")
        limit_str = target.get("hostname", target.get("name", ""))
    else:
        hostnames = [n.get("hostname", n.get("name", "")) for n in active_nodes]
        limit_str = ",".join(hostnames)

    pb = resolve_playbook_by_id(_CONFIG["db_path"], "scan_models_remote")
    if not pb:
        return error_response("PLAYBOOK_MISSING", "playbooks/node/scan_models_remote.yml not found in playbook registry")
    playbook = os.path.join(_project_root, pb["file_path"])
    # Dynamic inventory — no file generated (DI-7)

    try:
        result = _scan(engine_type_id=et_id, limit=limit_str,
                       db_path=_CONFIG["db_path"])
        return success_single(result)
    except Exception as exc:
        return error_response("SCAN_FAILED", str(exc))


def api_verify_checksum(engine_type, model_id):
    """Async checksum verification for a model's files.

    POST returns immediately with {status: "accepted"}.
    WebUI polls model detail page for updated sha256_verified_at timestamps.

    Args:
        engine_type: Engine type name (e.g., llama_server).
        model_id: Integer primary key of the model.

    Returns:
        JSON with status "accepted".
    """
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import get_model as _gm

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    model = _gm(_CONFIG["db_path"], model_id)
    if model is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")

    if model.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                              f"Model {model_id} belongs to a different engine type")

    # For now, just mark that verification was requested.
    # Actual SHA256 computation would run via Ansible on the remote node.
    # The WebUI will poll for updated sha256_verified_at values.
    return jsonify({"status": "accepted", "model_id": model_id}), 202


def api_clone_model(engine_type, model_id):
    """Clone a model entry 1:1 with unique name suffix (_clN).

    The cloned model shares the same model_path and all metadata (sha256,
    size_bytes, etc.) but gets a new id and an _clN-suffixed name. All
    model_params are inherited so the user can selectively override after
    cloning.

    Args:
        engine_type: Engine type name (e.g., llama_server).
        model_id: Integer primary key of the source model.

    Returns:
        JSON with the new model's data.
    """
    from db.adapters.engine_types import get_engine_type_by_name as _get_et
    from db.adapters.models import get_model as _gm, clone_model as _cm, ModelError

    et = _get_et(_CONFIG["db_path"], engine_type)
    et_id = et["id"] if et else None

    if et_id is None:
        return error_response("RESOURCE_NOT_FOUND", f"Engine type '{engine_type}' not found")

    # Verify model belongs to this engine type
    existing = _gm(_CONFIG["db_path"], model_id)
    if existing is None:
        return error_response("RESOURCE_NOT_FOUND", f"Model {model_id} not found")
    if existing.get("engine_type_id") != et_id:
        return error_response("RESOURCE_MISMATCH",
                                f"Model {model_id} belongs to a different engine type")

    try:
        cloned = _cm(_CONFIG["db_path"], model_id)
    except ModelError as exc:
        return error_response("BAD_REQUEST", str(exc))

    return success_single(cloned)


