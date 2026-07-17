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

"""quickrobot — Prompt registry adapter (file-based).

Tracks prompts in the database for version tracking, error counting,
and centralized resolution. Content stored in files on disk.
Mirrors playbook_registry structure: file_path + checksum in DB, content on disk.

Functions: register_prompt, get_prompt_by_id, list_prompts,
           delete_prompt, update_prompt, reset_counters,
           bump_version, verify_integrity, verify_prompt_integrity,
           _read_prompt_file, _write_prompt_file, _compute_checksum_from_file,
           _parse_prompt_version.
"""

import hashlib
import logging
import os
import re
from pathlib import Path

from db.sqlite import pool

logger = logging.getLogger(__name__)

# Regex to extract version from HTML header comment: <!-- ... version: N ... -->
# Uses re.DOTALL so .* matches newlines (multi-line HTML comment blocks)
_PROMPT_VERSION_RE = re.compile(r"<!--.*?version\s*:\s*(\d+).*?-->", re.DOTALL)


def _compute_checksum_from_file(project_root, prompt_id):
    """Compute SHA256 checksum of a prompt file on disk.

    Args:
        project_root: Absolute path to project root directory.
        prompt_id: Stable identifier (filename without .md extension).

    Returns:
        str — hex digest of SHA256 hash.
    """
    filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_prompt_version(project_root, prompt_id):
    """Extract version number from HTML header comment in prompt file.

    Reads the first 20 lines and joins them to handle multi-line HTML comments:
        <!-- ... version: N ... -->

    Args:
        project_root: Absolute path to project root directory.
        prompt_id: Stable identifier (filename without .md extension).

    Returns:
        int — version number from header, or None if not found.
    """
    filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            # Read first 20 lines and join for multi-line comment matching
            header = "".join(f.readline() for _ in range(20))
        m = _PROMPT_VERSION_RE.search(header)
        if m:
            return int(m.group(1))
    except (OSError, ValueError):
        pass
    return None


def _read_prompt_file(project_root, prompt_id):
    """Read a prompt file from disk.

    Args:
        project_root: Absolute path to project root directory.
        prompt_id: Stable identifier (filename without .md extension).

    Returns:
        str — file content, or None if file not found.
    """
    filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
    if not os.path.isfile(filepath):
        return None
    return Path(filepath).read_text(encoding="utf-8")


def _write_prompt_file(project_root, prompt_id, content):
    """Write prompt content to disk.

    Args:
        project_root: Absolute path to project root directory.
        prompt_id: Stable identifier (filename without .md extension).
        content: Full file content (header + body).

    Returns:
        str — absolute path of written file.
    """
    filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    Path(filepath).write_text(content, encoding="utf-8")
    return filepath


def _resolve_project_root():
    """Resolve the project root directory.

    Walks up from this module's location to find the project root.

    Returns:
        str — absolute path to project root.
    """
    _module_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(_module_dir))


def register_prompt(db_path, prompt_id, title, description, content, 
                    file_type="custom", tags="", message_role="usermessage", prompt_type="MCP"):
    """Register or update a prompt.

    Writes content to disk (prompts/<prompt_id>.md), computes checksum,
    and registers metadata in DB.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Stable identifier (e.g., "designer-system-prompt").
        title: Display name for the prompt.
        description: One-line description.
        content: The prompt content (raw text).
        file_type: "core" for system prompts, "custom" for user-created.
        tags: Comma-separated tags for filtering.
        message_role: MCP message role — systemprompt, usermessage, assistant, or skill.
        prompt_type: Prompt type — MCP, Benchmark, or Magi.

    Returns:
        dict with updated prompt record, or None if not found.
    """
    project_root = _resolve_project_root()
    
    # Write content to file
    filepath = _write_prompt_file(project_root, prompt_id, content)
    
    # Compute checksum and size from written file
    checksum = _compute_checksum_from_file(project_root, prompt_id)
    file_size = os.path.getsize(filepath)
    file_path = f"prompts/{prompt_id}.md"

    with pool(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM engine_prompts WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchone()

        if existing:
            # Update existing row
            cols = ["title", "description", "file_path", "message_role",
                    "checksum_sha256", "file_size", "tags", "updated_at"]
            vals = [title, description, file_path, message_role, checksum, file_size, tags, "now"]
            params = list(vals)
            if file_type:
                cols.append("file_type")
                params.append(file_type)
            if prompt_type:
                cols.append("prompt_type")
                params.append(prompt_type)
            sql = f"UPDATE engine_prompts SET {', '.join(c + ' = ?' for c in cols)} WHERE prompt_id = ?"
            params.append(prompt_id)
            conn.execute(sql, params)
        else:
            # Insert new row
            conn.execute(
                """INSERT INTO engine_prompts
                   (prompt_id, title, description, file_path, message_role, 
                    checksum_sha256, file_size, file_type, prompt_type, tags, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                (prompt_id, title, description, file_path, message_role,
                 checksum, file_size, file_type, prompt_type, tags),
            )

    return get_prompt_by_id(db_path, prompt_id)


def get_prompt_by_id(db_path, prompt_id):
    """Look up a registered prompt by its stable ID.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Stable identifier stored in the registry.

    Returns:
        dict with keys: id, prompt_id, title, description, file_path, 
            message_role, checksum_sha256, file_size, version, tags,
            arguments, usage_counter_since_update, error_counter_since_update,
            created_at, updated_at.
        None if not found.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_prompts WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}


def get_prompt_by_db_id(db_path, db_id):
    """Look up a prompt by its internal DB row ID.

    Args:
        db_path: Path to the SQLite database.
        db_id: Integer primary key in engine_prompts table.

    Returns:
        dict with prompt fields, or None if not found.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_prompts WHERE id = ?",
            (db_id,),
        ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}


def list_prompts(db_path, file_type=None, search="", message_role=None, prompt_type=None):
    """List all registered prompts with optional filtering.

    Args:
        db_path: Path to the SQLite database.
        file_type: Optional filter — "core" or "custom".
        search: Text filter matching prompt_id, title, or description.
        message_role: Optional filter by MCP message role (systemprompt, usermessage, assistant, skill).
        prompt_type: Optional filter by prompt type (MCP, Benchmark, Magi).

    Returns:
        list of dicts — matching prompt records.
    """
    from db.sqlite import pool
    from lib.lib_utils import relative_age

    with pool(db_path) as conn:
        query = "SELECT * FROM engine_prompts"
        params = []
        
        conditions = []
        if file_type:
            conditions.append("file_type = ?")
            params.append(file_type)
        if search:
            conditions.append(
                "(prompt_id LIKE ? OR title LIKE ? OR description LIKE ?)"
            )
            s = f"%{search}%"
            params.extend([s, s, s])
        if message_role:
            conditions.append("message_role = ?")
            params.append(message_role)
        if prompt_type:
            conditions.append("prompt_type = ?")
            params.append(prompt_type)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY prompt_type, file_type, prompt_id"
        
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        
        items = []
        for row in rows:
            item = {k: row[k] for k in row.keys()}
            # Add derived fields
            if "age_created" not in item and item.get("created_at"):
                item["age_created"] = relative_age(item["created_at"])
            items.append(item)
        
        return items


def get_mcp_prompts(db_path):
    """Get all prompts with TYPE='MCP' for dynamic MCP registration.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        list of dicts — prompts with prompt_type='MCP', sorted by prompt_id.
    """
    return list_prompts(db_path, prompt_type="MCP")


def get_skill_entries(db_path):
    """Get skill entries for MCP resource registration (SKILLS-MIGRATION).

    Returns skill files (SKILL.md, SKILL_MCP.md) from the engine_prompts table.
    These are served as MCP resources, not prompts.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        list of dicts — skill entries sorted by prompt_id.
    """
    try:
        with pool(db_path) as conn:
            rows = conn.execute(
                "SELECT id, prompt_id, title, description, file_path, "
                "message_role, file_type, prompt_type, file_size, checksum_sha256, "
                "version, tags, arguments, usage_counter_since_update, "
                "error_counter_since_update, created_at, updated_at "
                "FROM engine_prompts "
                "WHERE prompt_id IN ('skill', 'skill_mcp') "
                "ORDER BY prompt_id"
            ).fetchall()
            # Convert Row objects to dicts for JSON serialization compatibility
            return [dict(row) for row in rows]
    except Exception as e:
        logger.warning("[qr-prompts] Failed to get skill entries: %s", e)
        return []


def update_prompt(db_path, prompt_id, title=None, description=None, 
                  content=None, message_role=None, tags=None):
    """Update a prompt's fields.

    Only updates provided (non-None) fields. If content changes, writes new file
    and recalculates checksum. If prompt_id changes, renames the file.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Stable identifier of the prompt to update.
        title: New display title (optional).
        description: New description (optional).
        content: New content (optional) — writes to file + recalculates checksum if provided.
        message_role: New message role (optional).
        tags: New comma-separated tags (optional).

    Returns:
        dict with updated prompt record, or None if not found.
    """
    project_root = _resolve_project_root()
    
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_prompts WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchone()
        
        if row is None:
            return None

        updates = {}
        old_prompt_id = prompt_id
        
        if title is not None:
            updates["title"] = title
        if description is not None:
            updates["description"] = description
        if content is not None:
            # Write to file (rename if prompt_id changed)
            filepath = _write_prompt_file(project_root, prompt_id, content)
            updates["file_path"] = f"prompts/{prompt_id}.md"
            updates["checksum_sha256"] = _compute_checksum_from_file(project_root, prompt_id)
            updates["file_size"] = os.path.getsize(filepath)
            
            # Sync version from file header if present (file is source of truth)
            disk_ver = _parse_prompt_version(project_root, prompt_id)
            if disk_ver:
                db_ver = row.get("version") or 0
                if disk_ver != db_ver:
                    updates["version"] = disk_ver

            # If prompt_id changed, rename the file
            if old_prompt_id != prompt_id:
                old_filepath = os.path.join(project_root, "prompts", f"{old_prompt_id}.md")
                new_filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
                try:
                    os.rename(old_filepath, new_filepath)
                except OSError:
                    pass  # File may not exist or already renamed
        if message_role is not None:
            updates["message_role"] = message_role
        if tags is not None:
            updates["tags"] = tags
        
        # Always update timestamp
        updates["updated_at"] = "now"
        
        if not updates:
            return {k: row[k] for k in row.keys()}

        set_clause = ", ".join(f"{col} = ?" for col in updates)
        params = list(updates.values()) + [prompt_id]
        
        conn.execute(
            f"UPDATE engine_prompts SET {set_clause}, updated_at = datetime('now') WHERE prompt_id = ?",
            params,
        )
    
    return get_prompt_by_id(db_path, prompt_id)


def delete_prompt(db_path, prompt_id):
    """Delete a prompt by its stable ID or DB row ID.

    Accepts either a string prompt_id (e.g. 'designer-system-prompt')
    or an integer DB row ID. Deletes the file from disk and removes
    the DB row.

    Also deletes all associated prompt_versions audit records.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Stable identifier string or integer DB row ID.

    Returns:
        True if deleted, False if not found.
    """
    project_root = _resolve_project_root()
    
    with pool(db_path) as conn:
        # Try integer DB row ID first
        try:
            db_id = int(prompt_id)
            existing = conn.execute(
                "SELECT prompt_id FROM engine_prompts WHERE id = ?",
                (db_id,),
            ).fetchone()
            if not existing:
                return False
            prompt_id = existing["prompt_id"]  # Use the stable ID for cascade deletes
        except (ValueError, TypeError):
            # Not an integer — treat as stable prompt_id string
            existing = conn.execute(
                "SELECT prompt_id FROM engine_prompts WHERE prompt_id = ?",
                (str(prompt_id),),
            ).fetchone()
            if not existing:
                return False

        # Delete the file from disk
        filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
        try:
            os.remove(filepath)
        except OSError:
            pass  # File may not exist

        # Delete audit records first
        conn.execute(
            "DELETE FROM prompt_versions WHERE prompt_id = ?",
            (prompt_id,),
        )
        
        # Delete the prompt
        conn.execute(
            "DELETE FROM engine_prompts WHERE prompt_id = ?",
            (prompt_id,),
        )
    
    return True


def reset_counters(db_path, prompt_id=None):
    """Reset usage and error counters to zero.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: If provided, resets one prompt. Otherwise resets all.

    Returns:
        int — number of rows updated.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        if prompt_id is not None:
            existing = conn.execute(
                "SELECT id FROM engine_prompts WHERE prompt_id = ?",
                (prompt_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE engine_prompts SET usage_counter_since_update = 0, "
                    "error_counter_since_update = 0 WHERE prompt_id = ?",
                    (prompt_id,),
                )
                return 1
            return 0
        else:
            conn.execute(
                "UPDATE engine_prompts SET usage_counter_since_update = 0, "
                "error_counter_since_update = 0"
            )
            return conn.total_changes


def bump_version(db_path, prompt_id):
    """Increment prompt version and write audit record.

    Reads the current file content for the audit snapshot.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Stable identifier of the prompt.

    Returns:
        int — new version number, or None if prompt not found.
    """
    project_root = _resolve_project_root()
    
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT version FROM engine_prompts WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchone()
        
        if not row:
            return None
        
        new_version = row["version"] + 1
        
        # Read file content for audit snapshot
        content = _read_prompt_file(project_root, prompt_id)
        if content is None:
            content = ""
        
        # Update version in prompts table
        conn.execute(
            "UPDATE engine_prompts SET version = ?, updated_at = datetime('now') WHERE prompt_id = ?",
            (new_version, prompt_id),
        )
        
        # Write audit record
        snapshot_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO prompt_versions (prompt_id, version, content_snapshot_sha256) VALUES (?, ?, ?)",
            (prompt_id, new_version, snapshot_hash),
        )
    
    return new_version


def verify_integrity(db_path):
    """Backwards-compatible wrapper — calls verify_prompt_integrity(action="report").

    Returns dict with 'mismatches' list of (prompt_id, expected, actual) tuples.
    """
    result = verify_prompt_integrity(db_path, action="report", mode="prod")
    return {"mismatches": [
        (r["prompt_id"], r.get("expected", ""), r.get("actual", ""))
        for r in result.get("mismatches", [])
    ]}


def verify_prompt_integrity(db_path, action="report", mode="prod"):
    """Verify prompt checksums and sizes match their files on disk.

    Also reads the <!-- version: N --> header from each file and updates
    the version column if it differs from the stored value (dev-update/dev-import mode).

    In dev-import mode: also discovers new .md files in prompts/ that are not
    yet registered in DB and registers them automatically.

    Args:
        db_path: Path to the SQLite database.
        action: "report" = detect mismatches, log, return (do NOT update DB).
                "fix" = update checksums/sizes/versions for all mismatches.
        mode: "prod" = report only, "dev" = warn only,
              "dev-import" = register new files + sync all.

    Returns:
        dict with keys: mismatches (list of {prompt_id, expected, actual,
        expected_size, actual_size}), updated (count), version_bumped (count).
    """
    project_root = _resolve_project_root()
    mismatches = []
    updated = 0
    version_bumped = 0

    with pool(db_path) as conn:
        rows = conn.execute(
            "SELECT id, prompt_id, checksum_sha256, file_size, version FROM engine_prompts"
        ).fetchall()

        for row in rows:
            row_id = row["id"]
            prompt_id = row["prompt_id"]
            expected_hash = row["checksum_sha256"] or ""
            expected_size = row["file_size"] if "file_size" in row else None
            db_version = row["version"] if "version" in row else 0

            # Compute actual values from disk
            try:
                actual_hash = _compute_checksum_from_file(project_root, prompt_id)
                filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
                actual_size = os.path.getsize(filepath) if os.path.isfile(filepath) else 0
            except OSError as e:
                logger.warning("[qr] Prompt file read error: %s (%s)", e, prompt_id)
                mismatches.append({
                    "prompt_id": prompt_id,
                    "expected": expected_hash[:16],
                    "actual": "read_error",
                    "expected_size": expected_size,
                    "actual_size": 0,
                })
                continue

            hash_changed = (actual_hash != expected_hash)
            size_changed = (expected_size is not None and actual_size != expected_size)

            if hash_changed or size_changed:
                mismatches.append({
                    "prompt_id": prompt_id,
                    "expected": expected_hash[:16],
                    "actual": actual_hash[:16],
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                })

                # Log the mismatch
                if hash_changed and size_changed:
                    logger.warning(
                        "[qr] Prompt integrity mismatch: %s hash=%s..%s->%s..%s size=%s->%s",
                        prompt_id, expected_hash[:8], expected_hash[-8:],
                        actual_hash[:8], actual_hash[-8:],
                        expected_size, actual_size,
                    )
                elif hash_changed:
                    logger.warning(
                        "[qr] Prompt checksum changed: %s (%s..%s -> %s..%s)",
                        prompt_id, expected_hash[:8], expected_hash[-8:],
                        actual_hash[:8], actual_hash[-8:],
                    )
                else:
                    logger.warning(
                        "[qr] Prompt size changed: %s %s -> %s",
                        prompt_id, expected_size, actual_size,
                    )

                if action == "fix":
                    # Update checksum and size in DB
                    conn.execute(
                        "UPDATE engine_prompts SET checksum_sha256 = ?, file_size = ?, updated_at = datetime('now') WHERE id = ?",
                        (actual_hash, actual_size, row_id),
                    )
                    updated += 1

            # Version sync from file header
            disk_version = _parse_prompt_version(project_root, prompt_id)
            if disk_version and db_version and str(disk_version) != str(db_version):
                if disk_version > db_version:
                    logger.debug(
                        "[qr] Prompt version update: %s DB=%s -> file=%s",
                        prompt_id, db_version, disk_version,
                    )
                    conn.execute(
                        "UPDATE engine_prompts SET version = ?, updated_at = datetime('now') WHERE id = ?",
                        (disk_version, row_id),
                    )
                    version_bumped += 1

        if updated or version_bumped:
            conn.commit()

    # dev-import: discover new .md files not yet in DB
    if mode == "dev-import":
        try:
            import_dir = os.path.join(project_root, "prompts")
            if os.path.isdir(import_dir):
                for fname in os.listdir(import_dir):
                    if not fname.endswith(".md"):
                        continue
                    if "_backup_" in fname:
                        continue
                    pid = fname[:-3]  # strip .md
                    has = conn.execute(
                        "SELECT 1 FROM engine_prompts WHERE prompt_id = ?",
                        (pid,),
                    ).fetchone()
                    if not has:
                        try:
                            fpath = os.path.join(import_dir, fname)
                            csum = _compute_checksum_from_file(project_root, pid)
                            fsize = os.path.getsize(fpath)
                            ver = _parse_prompt_version(project_root, pid) or 1
                            conn.execute(
                                "INSERT INTO engine_prompts "
                                "(prompt_id, title, description, file_path, message_role, "
                                "checksum_sha256, file_size, version, file_type, prompt_type, "
                                "tags, arguments, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                                (pid, pid, "Auto-discovered from prompts/ directory",
                                 f"prompts/{fname}", "systemprompt", csum, fsize, ver,
                                 "custom", "MCP", "", "[]"),
                            )
                            updated += 1
                            logger.info("[qr] Registered new prompt from disk: %s (v%d)", pid, ver)
                        except Exception as exc:
                            logger.debug("prompt_discovery failed for %s: %s", pid, exc)
                conn.commit()
        except OSError as exc:
            logger.debug("prompt directory scan failed: %s", exc)

    return {"mismatches": mismatches, "updated": updated, "version_bumped": version_bumped}
