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

"""Quickrobot API — Engine Binary template endpoints (BINARY-DL).

CRUD operations for engine_binaries table. Binary templates are versioned
downloads that presets can reference to deploy pre-built binaries instead of
building from source.

Endpoints:
    GET  /api/v1/engine_binaries          — list all (with filters)
    GET  /api/v1/engine_binaries/<int>     — get single binary
    POST /api/v1/engine_binaries          — create new binary template
    PUT  /api/v1/engine_binaries/<int>     — update existing binary
    DELETE /api/v1/engine_binaries/<int>   — delete (hard or soft)
    GET  /api/v1/engine_binaries/by_engine/<engine_type_name> — list active for engine
"""

import logging

from flask import request, jsonify

logger = logging.getLogger(__name__)


def _get_db_path():
    """Get database path from config."""
    from qr_api import _CONFIG
    return _CONFIG["db_path"]


def success_single(data):
    """Return a successful single-resource response."""
    from qr_api.lib_responses import success_single as _ss
    return _ss(data)


def success_list(items):
    """Return a successful list response."""
    from qr_api.lib_responses import success_list as _sl
    return _sl(items)


def error_response(code, message):
    """Return an error response."""
    from qr_api.lib_responses import error_response as _err
    return _err(code, message)


# ---------------------------------------------------------------------------
# List Endpoints
# ---------------------------------------------------------------------------


def api_list_binaries():
    """List all binary templates with optional filtering.

    Query params:
        engine_type_id: filter by engine type ID (integer)
        is_active: filter by active status (0 or 1)

    Returns list of binary template records sorted by engine, version, platform.
    """
    from db.sqlite import pool

    engine_type_id = request.args.get("engine_type_id")
    is_active = request.args.get("is_active")

    query = """
        SELECT eb.*, et.display_name as engine_display_name
        FROM engine_binaries eb
        JOIN engine_types et ON eb.engine_type_id = et.id
        WHERE 1=1
    """
    params = []

    if engine_type_id is not None:
        try:
            engine_type_id = int(engine_type_id)
            query += " AND eb.engine_type_id = ?"
            params.append(engine_type_id)
        except (ValueError, TypeError):
            pass

    if is_active is not None:
        try:
            is_active_val = int(is_active)
            query += " AND eb.is_active = ?"
            params.append(is_active_val)
        except (ValueError, TypeError):
            pass

    query += " ORDER BY eb.engine_type_id, eb.version, eb.platform"

    with pool(_get_db_path()) as conn:
        rows = conn.execute(query, params).fetchall()

    items = []
    for row in rows:
        r = dict(row)
        # Truncate SHA256 for display (first 16 chars)
        sha = r.get("sha256", "")
        if sha and len(sha) > 16:
            r["sha256_short"] = sha[:16] + "..."
        else:
            r["sha256_short"] = sha
        items.append(r)

    return success_list(items)


def api_list_binaries_by_engine(engine_type_name):
    """List active binary templates for a specific engine.

    Convenience endpoint for the preset editor's AJAX template dropdown.

    Args:
        engine_type_name (path param): Engine name (e.g., "llama_server")

    Returns list of active binary templates for the specified engine.
    """
    from db.sqlite import pool

    query = """
        SELECT eb.*, et.display_name as engine_display_name
        FROM engine_binaries eb
        JOIN engine_types et ON eb.engine_type_id = et.id
        WHERE eb.engine_type_id = (SELECT id FROM engine_types WHERE name = ?)
          AND eb.is_active = 1
        ORDER BY eb.version DESC, eb.platform
    """

    with pool(_get_db_path()) as conn:
        rows = conn.execute(query, (engine_type_name,)).fetchall()

    items = []
    for row in rows:
        r = dict(row)
        sha = r.get("sha256", "")
        if sha and len(sha) > 16:
            r["sha256_short"] = sha[:16] + "..."
        else:
            r["sha256_short"] = sha
        items.append(r)

    return success_list(items)


# ---------------------------------------------------------------------------
# Single Resource Endpoints
# ---------------------------------------------------------------------------


def api_get_binary(binary_id):
    """Get a single binary template by DB row ID.

    Args:
        binary_id (path param): Integer primary key

    Returns single binary template record.
    """
    from db.sqlite import pool

    with pool(_get_db_path()) as conn:
        row = conn.execute(
            "SELECT eb.*, et.display_name as engine_display_name "
            "FROM engine_binaries eb "
            "JOIN engine_types et ON eb.engine_type_id = et.id "
            "WHERE eb.id = ?",
            (binary_id,),
        ).fetchone()

    if row is None:
        return error_response("NOT_FOUND", f"Binary template ID {binary_id} not found")

    result = dict(row)
    sha = result.get("sha256", "")
    if sha and len(sha) > 16:
        result["sha256_short"] = sha[:16] + "..."
    else:
        result["sha256_short"] = sha

    return success_single(result)


# ---------------------------------------------------------------------------
# Create Endpoint
# ---------------------------------------------------------------------------


def api_create_binary():
    """Create a new binary template.

    Request body:
        engine_type_id (int, required): Engine type ID
        name (str, required): Display name
        version (str, required): Version tag (e.g., "b10142")
        platform (str, required): Platform identifier
        template_type (str, optional): "binary" or "docker" (default: "binary")
        binary_name (str, optional): Extracted binary filename
        download_url (str, required): Full download URL
        sha256 (str, optional): Expected SHA256 checksum
        file_size (int, optional): Expected file size in bytes
        extract_type (str, optional): "none", "tar.gz", "zip", "gz" (default: "none")
        target_path (str, optional): Install path template (default provided)
        is_active (int, optional): 0 or 1 (default: 1)

    Returns created binary template record.
    """
    from db.sqlite import pool

    data = request.get_json(silent=True) or {}

    # Required fields validation
    name = data.get("name")
    if not name:
        return error_response("MISSING_FIELD", "'name' is required")

    engine_type_id = data.get("engine_type_id")
    if engine_type_id is None:
        return error_response("MISSING_FIELD", "'engine_type_id' is required")

    version = data.get("version")
    if not version:
        return error_response("MISSING_FIELD", "'version' is required")

    platform = data.get("platform")
    if not platform:
        return error_response("MISSING_FIELD", "'platform' is required")

    download_url = data.get("download_url")
    if not download_url:
        return error_response("MISSING_FIELD", "'download_url' is required")

    # Validate optional fields
    template_type = data.get("template_type", "binary")
    if template_type not in ("binary", "docker"):
        return error_response("INVALID_VALUE", f"template_type must be 'binary' or 'docker', got '{template_type}'")

    extract_type = data.get("extract_type", "none")
    if extract_type not in ("none", "tar.gz", "zip", "gz"):
        return error_response("INVALID_VALUE", f"extract_type must be 'none', 'tar.gz', 'zip', or 'gz', got '{extract_type}'")

    is_active = data.get("is_active", 1)
    if is_active not in (0, 1):
        return error_response("INVALID_VALUE", f"is_active must be 0 or 1, got {is_active}")

    binary_name = data.get("binary_name")
    sha256 = data.get("sha256")
    file_size = data.get("file_size")
    target_path = data.get("target_path", "/opt/quickrobot/binary-templates/{engine_type}/{version}-{platform}/")
    docker_image_name = data.get("docker_image_name")
    docker_tag = data.get("docker_tag")

    with pool(_get_db_path()) as conn:
        cursor = conn.execute(
            """INSERT INTO engine_binaries
               (engine_type_id, name, version, platform, template_type, binary_name,
                download_url, sha256, file_size, extract_type, target_path,
                docker_image_name, docker_tag, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (engine_type_id, name, version, platform, template_type, binary_name,
             download_url, sha256, file_size, extract_type, target_path,
             docker_image_name, docker_tag, is_active),
        )
        conn.commit()
        new_id = cursor.lastrowid

        # Fetch the created row
        row = conn.execute(
            "SELECT eb.*, et.display_name as engine_display_name "
            "FROM engine_binaries eb "
            "JOIN engine_types et ON eb.engine_type_id = et.id "
            "WHERE eb.id = ?",
            (new_id,),
        ).fetchone()

    if row is None:
        return error_response("CREATE_FAILED", "Binary template created but could not retrieve")

    result = dict(row)
    sha = result.get("sha256", "")
    if sha and len(sha) > 16:
        result["sha256_short"] = sha[:16] + "..."
    else:
        result["sha256_short"] = sha

    resp = jsonify({"status": "ok", "data": result})
    resp.status_code = 201
    return resp


# ---------------------------------------------------------------------------
# Update Endpoint
# ---------------------------------------------------------------------------


def api_update_binary(binary_id):
    """Update an existing binary template.

    Request body (all fields optional, only provided fields updated):
        name, version, platform, template_type, binary_name, download_url,
        sha256, file_size, extract_type, target_path, docker_image_name,
        docker_tag, is_active

    Returns updated binary template record.
    """
    from db.sqlite import pool

    data = request.get_json(silent=True) or {}

    # Validate the binary exists first
    with pool(_get_db_path()) as conn:
        existing = conn.execute(
            "SELECT * FROM engine_binaries WHERE id = ?",
            (binary_id,),
        ).fetchone()

    if existing is None:
        return error_response("NOT_FOUND", f"Binary template ID {binary_id} not found")

    # Build update query dynamically
    update_fields = []
    params = []

    valid_fields = [
        "name", "version", "platform", "template_type", "binary_name",
        "download_url", "sha256", "file_size", "extract_type",
        "target_path", "docker_image_name", "docker_tag", "is_active",
    ]

    for field in valid_fields:
        if field in data:
            update_fields.append(f"{field} = ?")
            params.append(data[field])

    if not update_fields:
        # Return existing without modification
        row = conn.execute(
            "SELECT eb.*, et.display_name as engine_display_name "
            "FROM engine_binaries eb "
            "JOIN engine_types et ON eb.engine_type_id = et.id "
            "WHERE eb.id = ?",
            (binary_id,),
        ).fetchone()
        result = dict(row)
        return success_single(result)

    # Add updated_at timestamp
    update_fields.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S','now')")

    params.append(binary_id)

    with pool(_get_db_path()) as conn:
        conn.execute(
            f"UPDATE engine_binaries SET {', '.join(update_fields)} WHERE id = ?",
            params,
        )
        conn.commit()

        # Fetch updated row
        row = conn.execute(
            "SELECT eb.*, et.display_name as engine_display_name "
            "FROM engine_binaries eb "
            "JOIN engine_types et ON eb.engine_type_id = et.id "
            "WHERE eb.id = ?",
            (binary_id,),
        ).fetchone()

    if row is None:
        return error_response("NOT_FOUND", f"Binary template ID {binary_id} not found")

    result = dict(row)
    sha = result.get("sha256", "")
    if sha and len(sha) > 16:
        result["sha256_short"] = sha[:16] + "..."
    else:
        result["sha256_short"] = sha

    return success_single(result)


# ---------------------------------------------------------------------------
# Delete Endpoint
# ---------------------------------------------------------------------------


def api_delete_binary(binary_id):
    """Delete a binary template (hard delete).

    Args:
        binary_id (path param): Integer primary key

    Returns deleted binary template record for confirmation.
    """
    from db.sqlite import pool

    with pool(_get_db_path()) as conn:
        # Fetch before deleting for confirmation
        row = conn.execute(
            "SELECT eb.*, et.display_name as engine_display_name "
            "FROM engine_binaries eb "
            "JOIN engine_types et ON eb.engine_type_id = et.id "
            "WHERE eb.id = ?",
            (binary_id,),
        ).fetchone()

        if row is None:
            return error_response("NOT_FOUND", f"Binary template ID {binary_id} not found")

        conn.execute("DELETE FROM engine_binaries WHERE id = ?", (binary_id,))
        conn.commit()

    result = dict(row)
    sha = result.get("sha256", "")
    if sha and len(sha) > 16:
        result["sha256_short"] = sha[:16] + "..."
    else:
        result["sha256_short"] = sha

    return success_single({"action": "deleted", "data": result})
