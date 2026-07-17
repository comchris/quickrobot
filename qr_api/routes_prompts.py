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

"""Quickrobot API — Prompt endpoints (file-based prompts).

CRUD operations for engine_prompts table + file I/O for prompt content.
Content stored in prompts/*.md files; DB stores registry + checksums only.
Mirrors playbook endpoint structure from routes_nodes.py.
"""

from flask import request


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


# ── Prompt CRUD Endpoints ────────────────────────────────────────────────

def api_list_engine_prompts():
    """List all registered prompts with optional filtering.

    Query params:
        file_type: filter by "core" or "custom"
        prompt_type: filter by "MCP", "Benchmark", or "Magi"
        search: text filter matching prompt_id, title, or description
        message_role: filter by MCP message role (systemprompt/usermessage/assistant/skill)

    Returns list of prompt records sorted by type then prompt_id.
    """
    from db.adapters.prompts import list_prompts as _list_prompts

    file_type = request.args.get("file_type")
    prompt_type = request.args.get("prompt_type")
    search = request.args.get("search", "")
    message_role = request.args.get("message_role")

    items = _list_prompts(
        _get_db_path(),
        file_type=file_type if file_type else None,
        prompt_type=prompt_type if prompt_type else None,
        search=search,
        message_role=message_role if message_role else None,
    )

    return success_list(items)


def api_get_engine_prompt(prompt_id):
    """Get a single prompt by its stable ID.

    Args:
        prompt_id (path param): Stable identifier (e.g., "designer-system-prompt")

    Returns:
        Single prompt record with metadata + file content read from disk.
    """
    from db.adapters.prompts import get_prompt_by_id as _get_prompt
    from db.adapters.prompts import _read_prompt_file
    import os

    row = _get_prompt(_get_db_path(), str(prompt_id))
    if row is None:
        return error_response("NOT_FOUND", f"Prompt '{prompt_id}' not found")

    # Read content from file on disk
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    content = _read_prompt_file(project_root, str(prompt_id))
    
    result = dict(row)
    result["content"] = content if content else ""

    return success_single(result)


def api_get_engine_prompt_by_db_id(db_id):
    """Get a single prompt by its DB row ID.

    Args:
        db_id (path param): Integer primary key in engine_prompts table

    Returns:
        Single prompt record with full content read from file.
    """
    from db.adapters.prompts import get_prompt_by_db_id as _get_prompt
    from db.adapters.prompts import _read_prompt_file
    import os

    row = _get_prompt(_get_db_path(), int(db_id))
    if row is None:
        return error_response("NOT_FOUND", f"Prompt ID {db_id} not found")

    # Read content from file on disk
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prompt_id = row.get("prompt_id")
    content = _read_prompt_file(project_root, prompt_id) if prompt_id else None
    
    result = dict(row)
    result["content"] = content if content else ""

    return success_single(result)


def api_create_engine_prompt():
    """Register a new custom prompt in the registry.

    Writes content to prompts/<prompt_id>.md file, computes checksum,
    and registers metadata in DB.

    Request body:
        prompt_id (str, required): Stable identifier, e.g. "my-debug-prompt"
        title (str, required): Display name
        description (str, required): One-line description
        content (str, required): The prompt content (max 100KB)
        file_type (str, optional): "custom" (default) or "core"
        tags (str, optional): Comma-separated tags (default "")
        message_role (str, optional): MCP message role, default "usermessage"
        prompt_type (str, optional): Prompt type, default "MCP"

    Returns registered prompt record.
    """
    from db.adapters.prompts import register_prompt as _reg_prompt
    from lib.qr_engine_ids import PROMPT_MAX_CONTENT_BYTES

    data = request.get_json(silent=True) or {}

    prompt_id = data.get("prompt_id")
    title = data.get("title", "")
    description = data.get("description", "")
    content = data.get("content", "")
    
    if not title:
        return error_response("MISSING_FIELD", "'title' is required")
    
    # Auto-generate prompt_id from title if not provided
    if not prompt_id:
        import re
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', title.lower().strip())
        slug = slug.strip('-')
        prompt_id = slug
    
    if not content:
        return error_response("MISSING_FIELD", "'content' is required")

    # Enforce max content size
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > PROMPT_MAX_CONTENT_BYTES:
        return error_response("TOO_LARGE", 
            f"Content size {content_bytes} bytes exceeds limit of {PROMPT_MAX_CONTENT_BYTES} bytes")

    file_type = data.get("file_type", "custom")
    prompt_type = data.get("prompt_type", "MCP")
    tags = data.get("tags", "")
    message_role = data.get("message_role", "usermessage")

    # Validate message_role
    if message_role not in ("systemprompt", "usermessage", "assistant", "skill"):
        return error_response("INVALID_VALUE", 
            f"message_role must be 'systemprompt', 'usermessage', 'assistant', or 'skill', got '{message_role}'")

    # Validate prompt_type
    if prompt_type not in ("MCP", "Benchmark", "Magi"):
        return error_response("INVALID_VALUE", 
            f"prompt_type must be 'MCP', 'Benchmark', or 'Magi', got '{prompt_type}'")

    record = _reg_prompt(_get_db_path(), prompt_id, title, description, content,
                         file_type=file_type, prompt_type=prompt_type, tags=tags, message_role=message_role)

    print(f"[qr] Registered new prompt: {prompt_id} (id={record['id'] if record else 'N/A'}, type={file_type})")

    return success_single({
        "action": "created",
        "prompt": record,
    })


def api_update_engine_prompt(prompt_id):
    """Update a prompt's content and metadata.

    If content changes: writes to file + recomputes checksum.
    If prompt_id changes: renames file + updates DB row.

    Args:
        prompt_id (path param): Stable identifier of the prompt to update.

    Request body (all fields optional):
        title (str): New display title
        description (str): New description  
        content (str): New content — writes to file, recalculates checksum
        prompt_id (str): New prompt_id — renames file + updates DB
        message_role (str): New MCP message role
        tags (str): New comma-separated tags

    Returns updated prompt record.
    """
    from db.adapters.prompts import update_prompt as _update_prompt, get_prompt_by_id as _get_prompt
    from lib.qr_engine_ids import PROMPT_MAX_CONTENT_BYTES

    data = request.get_json(silent=True) or {}

    # Check existence first
    existing = _get_prompt(_get_db_path(), str(prompt_id))
    if existing is None:
        return error_response("NOT_FOUND", f"Prompt '{prompt_id}' not found")

    content = data.get("content")
    if content is not None:
        # Enforce max content size
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > PROMPT_MAX_CONTENT_BYTES:
            return error_response("TOO_LARGE", 
                f"Content size {content_bytes} bytes exceeds limit of {PROMPT_MAX_CONTENT_BYTES} bytes")

    # Validate message_role if provided
    message_role = data.get("message_role")
    if message_role is not None and message_role not in ("systemprompt", "usermessage", "assistant", "skill"):
        return error_response("INVALID_VALUE", 
            f"message_role must be 'systemprompt', 'usermessage', 'assistant', or 'skill', got '{message_role}'")

    # If prompt_id changed, we need to use the new ID for the update
    new_prompt_id = data.get("prompt_id", prompt_id)
    
    updated = _update_prompt(
        _get_db_path(), str(prompt_id),
        title=data.get("title"),
        description=data.get("description"),
        content=content,
        message_role=message_role,
        tags=data.get("tags"),
    )

    print(f"[qr] Updated prompt: {prompt_id} (id={updated['id']})")

    return success_single({
        "action": "updated",
        "prompt": updated,
    })


def api_delete_engine_prompt(prompt_id):
    """Delete a prompt by its stable ID.

    Removes the file from disk and deletes the DB row.

    Args:
        prompt_id (path param): Stable identifier of the prompt to delete.

    Returns: success with action confirmation.
    """
    from db.adapters.prompts import delete_prompt as _delete_prompt

    deleted = _delete_prompt(_get_db_path(), str(prompt_id))
    if not deleted:
        return error_response("NOT_FOUND", f"Prompt '{prompt_id}' not found")

    print(f"[qr] Deleted prompt: {prompt_id}")

    return success_single({
        "action": "deleted",
        "prompt_id": prompt_id,
    })


def api_engine_prompt_content(prompt_id):
    """Return prompt content read from disk file with checksum and size.

    Returns DB-registered checksum/size (for verification) alongside
    disk-computed values. Mirrors playbook_content endpoint pattern:
    db_checksum, db_file_size, actual_file_size, content.

    Args:
        prompt_id (path param): Stable identifier or DB row ID of the prompt.

    Returns:
        {content, checksum_sha256, file_size, actual_file_size} — 
        checksum_sha256 and file_size from DB registry;
        actual_file_size computed from disk.
    """
    from db.adapters.prompts import get_prompt_by_db_id as _get_prompt
    from db.adapters.prompts import _read_prompt_file
    import os

    try:
        db_id = int(prompt_id)
    except (ValueError, TypeError):
        db_id = None

    row = _get_prompt(_get_db_path(), db_id) if db_id else None
    
    # Try looking up by stable ID if not found by db_id
    if row is None:
        from db.adapters.prompts import get_prompt_by_id
        row = get_prompt_by_id(_get_db_path(), str(prompt_id))

    if row is None:
        return error_response("NOT_FOUND", f"Prompt {prompt_id} not found")

    prompt_id = row.get("prompt_id")
    
    # Read content from file on disk
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    content = _read_prompt_file(project_root, prompt_id)
    
    if content is None:
        return error_response("NOT_FOUND", f"File not found for prompt '{prompt_id}'")

    # Get actual disk file size
    filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
    actual_file_size = os.path.getsize(filepath) if os.path.isfile(filepath) else 0

    return success_single({
        "id": row["id"],
        "prompt_id": prompt_id,
        "content": content,
        "checksum_sha256": row.get("checksum_sha256", ""),
        "file_size": row.get("file_size"),
        "actual_file_size": actual_file_size,
    })


def api_prompt_rescan(prompt_id):
    """Re-scan a single prompt: compare disk checksum/size to DB and optionally fix.

    Query params:
        fix: "true" to update DB with disk values (default: "false" — report only)

    Returns:
        {action, prompt_id, checksum_updated, size_updated, version, mismatches}
        
    In prod mode: always reports mismatch at warning level.
    In dev mode: auto-fixes on mismatch.
    """
    from db.adapters.prompts import verify_prompt_integrity as _verify_pi

    data = request.get_json(silent=True) or {}
    fix = str(data.get("fix", request.args.get("fix", "false"))).lower() == "true"
    
    # In prod, report-only is default; fix requires explicit ?fix=true
    result = _verify_pi(_get_db_path(), action="fix" if fix else "report", mode="prod")
    
    mismatch_count = len(result.get("mismatches", []))
    
    return success_single({
        "action": "fixed" if (fix and mismatch_count) else "scanned",
        "prompt_id": prompt_id,
        "mismatch_count": mismatch_count,
        "checksum_updated": result.get("updated", 0) > 0,
        "version_bumped": result.get("version_bumped", 0),
        "mismatches": result.get("mismatches", []),
    })


def api_rescan_engine_prompts():
    """Batch-scan all registered prompts and optionally fix checksums/sizes/versions.

    Query params:
        fix: "true" to update DB (default: "false" in prod, auto in dev mode)
        file_type: filter by "core" or "custom"
        prompt_type: filter by "MCP", "Benchmark", or "Magi"
        discover: "true" to also scan for new .md files not in DB (dev-import behavior)

    Returns:
        {total_scanned, updated, version_bumped, mismatches, newly_discovered}
        
    In prod mode: ?fix=true required to modify DB.
    In dev mode: auto-fixes all mismatches.
    """
    from db.adapters.prompts import list_prompts as _list_prompts, verify_prompt_integrity as _verify_pi

    fix = str(request.args.get("fix", "false")).lower() == "true"
    file_type = request.args.get("file_type")
    prompt_type = request.args.get("prompt_type")
    discover = str(request.args.get("discover", "false")).lower() == "true"
    
    # Build filtered list for counting
    prompts = _list_prompts(_get_db_path(), file_type=file_type, prompt_type=prompt_type)
    
    # Determine mode: if discover=true, use dev-import behavior (auto-register new files)
    mode = "dev-import" if discover else "prod"
    action = "fix" if fix else "report"
    
    result = _verify_pi(_get_db_path(), action=action, mode=mode)
    
    return success_single({
        "total_scanned": len(prompts),
        "updated": result.get("updated", 0),
        "version_bumped": result.get("version_bumped", 0),
        "mismatch_count": len(result.get("mismatches", [])),
        "mismatches": result.get("mismatches", []),
    })


def api_reset_engine_prompt_counters(prompt_id=None):
    """Reset usage and error counters for one or all prompts.

    Query param or path param:
        prompt_id: Stable identifier (optional — if omitted, resets all)

    Returns count of reset prompts.
    """
    from db.adapters.prompts import reset_counters as _reset

    # Accept prompt_id from query params if not in path
    pid = request.args.get("prompt_id") or prompt_id
    
    if pid:
        count = _reset(_get_db_path(), str(pid))
    else:
        count = _reset(_get_db_path())

    return success_single({
        "action": "counters_reset",
        "reset_count": count,
    })
