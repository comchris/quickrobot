"""Playbook management endpoints for quickrobot.

Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
import logging
import os

from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG, _project_root
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST
from db.sqlite import pool as db_pool
from lib.lib_constants import DEFAULT_ANSIBLE_USER

logger = logging.getLogger(__name__)

def api_list_playbooks():
    """List all registered playbooks with optional filtering.

    Query params:
        file_type: filter by "core" or "custom"
        search: text filter matching playbook_id or file_path (case-insensitive)

    Returns list of playbook records sorted by type then path.
    """
    from db.adapters.playbooks import list_playbooks as _list_pb

    file_type = request.args.get("file_type")
    search = request.args.get("search", "")

    items = _list_pb(_CONFIG["db_path"],
                         file_type=file_type if file_type else None)

    # Apply text search filter (client-side to avoid adapter changes)
    if search:
        s = search.lower()
        items = [item for item in items
                 if s in str(item.get("playbook_id", "")).lower()
                 or s in str(item.get("file_path", "")).lower()]

    from lib.lib_utils import relative_age
    for item in items:
        if "age_created" not in item and item.get("created_at"):
            item["age_created"] = relative_age(item["created_at"])
        # Alias usage_counter_since_update as usage_count for API consumers
        if "usage_count" not in item and "usage_counter_since_update" in item:
            item["usage_count"] = item["usage_counter_since_update"]

    return success_list(items)


def api_register_playbook():
    """Register a new custom playbook in the DB registry.

    Security: file_type forced to 'custom', ID auto-generated >= 100.
    Core/system playbooks (ID < 100) cannot be overwritten via this endpoint.

    Request body:
        file_path (str, required): relative path from project root
            e.g., "playbooks/my_custom.yml"
        tags (str, optional): comma-separated tags (default "custom")
        version (str, optional): version string (default "1")

    Returns registered playbook record.
    """
    from db.adapters.playbooks import register_playbook as _reg_pb, get_playbook_by_path as _get_pb
    import hashlib

    data = request.get_json(silent=True) or {}

    file_path = data.get("file_path")
    if not file_path:
        return error_response("MISSING_FIELD", "'file_path' is required")

    # Verify the file exists on disk (file_path is relative to project root)
    full_path = os.path.join(_project_root, file_path)
    if not os.path.isfile(full_path):
        return error_response("FILE_NOT_FOUND",
                                f"Playbook file not found: {full_path}")

    # Auto-compute checksum from disk (caller cannot lie)
    with open(full_path, "rb") as f:
        actual_checksum = hashlib.sha256(f.read()).hexdigest()

    # Force custom type — core playbooks are managed by seed/init only
    file_type = "custom"
    tags = data.get("tags", "custom")
    version = data.get("version", "1")

    # Register the playbook (ID auto-generated >= 100 for new entries)
    try:
        new_id = _reg_pb(_CONFIG["db_path"], file_path, actual_checksum,
                         file_type=file_type, tags=tags)
    except ValueError as e:
        return error_response("CONFLICT", str(e))
    record = _get_pb(_CONFIG["db_path"], file_path)
    if new_id is not None:
        logger.info("[qr] Registered new playbook: %s (id=%d, type=%s)", file_path, new_id, file_type)

    return success_single({
        "action": "registered",
        "playbook": record
    })


def api_update_playbook(playbook_id):
    """Update a registered playbook's metadata.

    Security: only allows updates to custom playbooks (ID >= 100).
    Core/system playbooks (ID < 100) are locked — file_type forced to "custom",
    and checksum must be re-validated against disk.

    Request body (all fields optional):
        version (str): new version string
        tags (str): new comma-separated tags
        checksum (str, optional): if provided, re-validated against disk

    Returns updated playbook record.
    """
    from db.adapters.playbooks import get_playbook_by_path as _get_pb
    import hashlib

    with db_pool(_CONFIG["db_path"]) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE id = ?", (playbook_id,)
        ).fetchone()
        if row is None:
            return error_response("NOT_FOUND", f"Playbook ID {playbook_id} not found")

        # Core playbooks (ID < 100) are locked — no updates allowed
        if playbook_id < 100:
            return error_response("READ_ONLY",
                                  f"Core playbook (ID {playbook_id}) is read-only via API")

        data = request.get_json(silent=True) or {}

        update_fields = []
        params = []

        # Force file_type to "custom" — user cannot promote custom to core
        if "file_type" in data:
            data["file_type"] = "custom"

        for field in ("version", "tags", "checksum", "file_type"):
            if field in data:
                update_fields.append(f"{field} = ?")
                params.append(data[field])

        # Auto-revalidate checksum against disk if provided
        if "checksum" in data:
            full_path = os.path.join(_project_root, row["file_path"])
            if os.path.isfile(full_path):
                with open(full_path, "rb") as f:
                    actual = hashlib.sha256(f.read()).hexdigest()
                if actual != data["checksum"]:
                    return error_response("CHECKSUM_MISMATCH",
                                          f"Checksum mismatch on disk: expected={data['checksum'][:16]}... got={actual[:16]}...")

        if not update_fields:
            return error_response("NO_CHANGES", "No valid fields to update")

        update_fields.append("updated_at = datetime('now')")
        params.append(playbook_id)

        conn.execute(
            f"UPDATE playbook_registry SET {', '.join(update_fields)} WHERE id = ?",
            tuple(params),
        )

    updated_record = _get_pb(_CONFIG["db_path"], row["file_path"])
    return success_single({"action": "updated", "playbook": updated_record})


def api_delete_playbook(playbook_id):
    """Remove a playbook from the registry (does not delete file on disk).

    Returns confirmation with removed playbook info.
    """
    from db.adapters.playbooks import get_playbook_by_path as _get_pb

    with db_pool(_CONFIG["db_path"]) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE id = ?", (playbook_id,)
        ).fetchone()
        if row is None:
            return error_response("NOT_FOUND", f"Playbook ID {playbook_id} not found")

        file_path = row["file_path"]

        conn.execute(
            "DELETE FROM playbook_registry WHERE id = ?", (playbook_id,)
        )

    return success_single({
        "action": "removed",
        "playbook": {"id": playbook_id, "file_path": file_path}
    })


def api_reset_playbook_counters():
    """Reset usage and error counters for all (or a specific) playbook.

    Request body (optional):
        playbook_id: int — reset only this playbook; omit to reset all

    Returns count of playbooks whose counters were reset.
    """
    from db.adapters.playbooks import reset_counters as _reset_cb

    data = request.get_json(silent=True) or {}
    pb_id = data.get("playbook_id")

    count = _reset_cb(_CONFIG["db_path"], playbook_id=pb_id)
    return success_single({"action": "counters_reset", "reset_count": count})


def api_rescan_playbooks():
    """Rescan endpoint disabled. Use --mode dev-update to sync checksums/sizes,
    or POST /api/v1/playbooks to register individual custom playbooks."""
    return error_response("DISABLED",
                          "Playbook rescan via API is disabled. "
                          "Use 'python3 quickrobot.py --mode dev-update' for full sync, "
                          "or 'POST /api/v1/playbooks' to register a single custom playbook.")


def api_playbook_content(playbook_id):
    """Return the raw YAML content of a playbook for browser display.

    Used by the WebUI checksum verification to fetch playbook content
    and compute SHA256 hashes for integrity verification.

    Args:
        playbook_id: Integer primary key of the playbook.

    Returns:
        JSON with content, file_size, and actual_file_size from disk.
    """
    from db.sqlite import pool as _pool
    import os as _os

    record = None
    with _pool(_CONFIG["db_path"]) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE id = ?",
            (playbook_id,),
        ).fetchone()
        if row:
            record = {k: row[k] for k in row.keys()}

    if not record:
        return error_response("NOT_FOUND", f"Playbook ID {playbook_id} not found")

    file_path = record.get("file_path", "")
    full_path = _os.path.join(_project_root, file_path)
    
    if not _os.path.isfile(full_path):
        return error_response("FILE_NOT_FOUND", f"Playbook file not found: {full_path}")

    with open(full_path, 'r', encoding='utf-8') as f:
        content = f.read()

    actual_size = len(content.encode('utf-8'))

    return success_single({
        "content": content,
        "file_path": file_path,
        "playbook_id": record.get("playbook_id"),
        "version": record.get("version"),
        "file_type": record.get("file_type"),
        "usage_counter_since_update": record.get("usage_counter_since_update", 0),
        "error_counter_since_update": record.get("error_counter_since_update", 0),
        "created_at": record.get("created_at"),
        "file_size": record.get("file_size"),
        "actual_file_size": actual_size
    })


def api_validate_playbook(playbook_id):
    """Validate YAML syntax of a playbook file.

    Args:
        playbook_id: Integer primary key of the playbook.

    Returns:
        JSON with status and optional error message.
    """
    from db.sqlite import pool as _pool
    import os as _os
    import yaml as _yaml

    record = None
    with _pool(_CONFIG["db_path"]) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE id = ?",
            (playbook_id,),
        ).fetchone()
        if row:
            record = {k: row[k] for k in row.keys()}

    if not record:
        return error_response("NOT_FOUND", f"Playbook ID {playbook_id} not found")

    file_path = record.get("file_path", "")
    full_path = _os.path.join(_project_root, file_path)

    if not _os.path.isfile(full_path):
        return error_response("FILE_NOT_FOUND", f"Playbook file not found: {full_path}")

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            _yaml.safe_load(f)
        return success_single({"valid": True})
    except _yaml.YAMLError as e:
        return success_single({"valid": False, "error": str(e)[:200]})

