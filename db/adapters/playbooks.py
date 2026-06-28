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

"""quickrobot — Playbook registry adapter.

Tracks all playbooks in the database for version tracking, error counting,
and centralized resolution. Replaces hardcoded playbook paths throughout
the codebase.

Functions: register_playbook, get_playbook_by_path,
           register_known_playbooks, register_all_core_playbooks,
           resolve_playbook_by_tags, resolve_playbook_by_id,
           backfill_playbook_ids, increment_usage_counter,
           increment_error_counter, list_playbooks,
           verify_playbook_integrity.
"""

import hashlib
import os
import re
import sys

# Regex to match # @version: N comment at top of playbook YAML files.
_PLAYBOOK_VERSION_RE = re.compile(r"^\s*#\s*@version\s*:\s*(\d+)\s*$")


def _parse_playbook_version(playbook_path):
    """Extract version number from a YAML header comment.

    Reads the first 20 lines of the playbook file looking for:
        # @version: <number>

    Args:
        playbook_path: Full path to the YAML playbook file.

    Returns:
        String version (e.g., "1"), or None if not found.
    """
    try:
        with open(playbook_path, "r") as f:
            for _i in range(20):
                line = f.readline()
                m = _PLAYBOOK_VERSION_RE.match(line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def _parse_playbook_header(filepath):
    """Extract metadata directives from playbook YAML header.

    Reads the first 20 lines of a YAML file looking for comment-based
    metadata directives: @playbook_id, @version, @timeout, @name.

    Args:
        filepath: Absolute path to the YAML playbook file.

    Returns:
        dict with keys: playbook_id (str), version (str), timeout (int|None),
            name (str). Missing directives default to empty string or None.
    """
    result = {
        "playbook_id": "",
        "version": "",
        "timeout": None,
        "name": "",
    }
    try:
        with open(filepath, "r") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                line = line.strip()
                if line.startswith("# @playbook_id:"):
                    result["playbook_id"] = line.split(":", 1)[1].strip()
                elif line.startswith("# @version:"):
                    result["version"] = line.split(":", 1)[1].strip()
                elif line.startswith("# @timeout:"):
                    val = line.split(":", 1)[1].strip()
                    try:
                        result["timeout"] = int(val)
                    except ValueError:
                        result["timeout"] = None
                elif line.startswith("# @name:"):
                    result["name"] = line.split(":", 1)[1].strip()
    except Exception:
        pass  # Can't read — return defaults
    return result


def _parse_tags(rel_path):
    """Derive tags from a playbook's relative path.

    Template files (.j2) get tag 'template'. Subdirectory names are used
    as tags for files in subdirectories (e.g., 'core', 'node', 'common').
    Files in the playbook root get no tags (empty string).

    Args:
        rel_path: Relative path from playbook root (e.g., "core/preflight.yml").

    Returns:
        str — comma-separated tags, or empty string.
    """
    if "/" not in rel_path:
        return ""
    subdir = os.path.basename(os.path.dirname(rel_path))
    # Template files use 'template' tag, not the subdirectory name
    if subdir == "templates" or rel_path.endswith(".j2"):
        return "template"
    return subdir


def _compute_file_checksum(filepath):
    """Compute SHA256 checksum of a file's content.

    Args:
        filepath: Absolute or relative path to the file.

    Returns:
        str — hex digest of SHA256 hash.
    """
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def register_playbook(db_path, file_path, checksum, file_type="core",
                       tags="", playbook_id=None, file_size=None):
    """Register or update a playbook in the registry.

    Core playbooks: INSERT OR REPLACE on playbook_id (stable ID).
    Updates metadata (checksum, version) when playbook changes.
    Custom playbooks: INSERT OR REPLACE on file_path for user flexibility.

    Args:
        db_path: Path to the SQLite database.
        file_path: Relative path from project root (e.g., "playbooks/deploy.yml").
        checksum: SHA256 hex digest of the playbook file content.
        file_type: "core" for built-in playbooks, "custom" for user-added.
        tags: Comma-separated tag string for lookups.
        playbook_id: Stable identifier (e.g., "DEPLOY_LLAMA_SERVER_V1").
        file_size: File size in bytes (optional, from manifest).

    Returns:
        int — the row id (new or existing).
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        # Check if this file_path already exists in DB
        existing = conn.execute(
            "SELECT id FROM playbook_registry WHERE file_path = ?",
            (file_path,),
        ).fetchone()

        if file_type == "core" and existing:
            # UPDATE existing row — avoids FK constraint failures from DELETE
            cols = ["checksum_sha256", "file_type", "tags"]
            vals = [checksum, file_type, tags]
            params = list(vals)
            if playbook_id:
                cols.append("playbook_id")
                params.append(playbook_id)
            cols.append("updated_at")
            params.append("now")
            if file_size is not None:
                cols.append("file_size")
                params.append(file_size)
            sql = f"UPDATE playbook_registry SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?"
            params.append(existing["id"])
            conn.execute(sql, params)
        elif file_type == "core":
            conn.execute(
                """INSERT INTO playbook_registry
                   (file_path, checksum_sha256, file_type, tags, playbook_id, updated_at, file_size)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                (file_path, checksum, file_type, tags, playbook_id or "", file_size),
            )
        else:
            # Guard: custom playbooks cannot overwrite core entries
            core_existing = conn.execute(
                "SELECT id FROM playbook_registry WHERE file_path = ? AND file_type = 'core'",
                (file_path,),
            ).fetchone()
            if core_existing:
                raise ValueError(f"Core playbook already registered: {file_path} (id={core_existing[0]})")

            conn.execute(
                """INSERT INTO playbook_registry
                   (file_path, checksum_sha256, file_type, tags, playbook_id, updated_at, file_size)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                (file_path, checksum, file_type, tags, playbook_id or "", file_size),
            )

    # Return the id — look up by file_path (works for both core and custom)
    row = get_playbook_by_path(db_path, file_path)
    return row["id"] if row else None


def get_playbook_by_path(db_path, file_path):
    """Look up a registered playbook by its file path.

    Args:
        db_path: Path to the SQLite database.
        file_path: Exact file_path stored in the registry.

    Returns:
        dict with keys: id, file_path, version, checksum_sha256,
            file_type, tags, created_at, updated_at,
            usage_counter_since_update, error_counter_since_update.
        None if not found.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}


def register_known_playbooks(db_path, root_dir=None):
    """Register core playbooks by parsing their @playbook_id headers.

    Walks the playbook directory, reads the # @playbook_id: ID header from
    each file, and registers it as "core" if the header is present. Files
    without a valid @playbook_id header are skipped (require manual import).

    Unlike register_all_core_playbooks, this does NOT auto-register files
    without headers as "custom". Unknown playbooks require manual API import.

    Args:
        db_path: Path to the SQLite database.
        root_dir: Base directory containing playbooks (absolute path).

    Returns:
        int — number of playbooks registered.
    """
    from lib.lib_constants import UE_PLAYBOOK_ROOT_DIR

    if root_dir is None:
        _module_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(os.path.dirname(_module_dir))
        root_dir = os.path.normpath(
            os.path.join(_project_root, UE_PLAYBOOK_ROOT_DIR)
        )

    registered = 0
    for dirpath, _dirnames, filenames in os.walk(root_dir):
      for fname in filenames:
            if not (fname.endswith(".yml") or fname.endswith(".yaml") or fname.endswith(".j2")):
                continue
            if "_backup_" in fname:
                continue

            full_path = os.path.join(dirpath, fname)
            rel_from_project = os.path.relpath(full_path,
                                               os.path.dirname(root_dir))

            # Parse header to get the authoritative @playbook_id from the file
            header = _parse_playbook_header(full_path)
            pb_id = header.get("playbook_id", "") or ""

            # Only register files that declare a playbook_id in their header
            if not pb_id:
                continue

            tags = _parse_tags(rel_from_project)
            checksum = _compute_file_checksum(full_path)
            file_size = os.path.getsize(full_path)

            new_id = register_playbook(
                db_path, rel_from_project, checksum,
                file_type="core", tags=tags, playbook_id=pb_id,
                file_size=file_size,
            )
            if new_id is not None:
                registered += 1

    return registered


def register_all_core_playbooks(db_path, root_dir=None):
    """Scan the playbooks directory and register core or custom playbooks.

    Walks the directory tree under root_dir, parses each file's @playbook_id
    header, and registers it as "core" (has header) or "custom" (no header).
    Unlike register_known_playbooks, this also picks up files without headers
    as "custom" playbooks.

    Args:
        db_path: Path to the SQLite database.
        root_dir: Base directory containing playbooks (absolute path).
            Defaults to "playbooks/" from lib_constants, resolved relative
            to the project root (one level up from this module's parent).

    Returns:
        int — number of new playbooks registered.
    """
    from lib.lib_constants import UE_PLAYBOOK_ROOT_DIR

    if root_dir is None:
        # Fallback: resolve relative to project root (grandparent of db/adapters/)
        _module_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(os.path.dirname(_module_dir))
        root_dir = os.path.normpath(
            os.path.join(_project_root, UE_PLAYBOOK_ROOT_DIR)
        )

    registered = 0
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fname in filenames:
            if not (fname.endswith(".yml") or fname.endswith(".yaml") or fname.endswith(".j2")):
                continue
            # Skip backup files — they have "_backup_" in the name.
            if "_backup_" in fname:
                continue

            full_path = os.path.join(dirpath, fname)
            rel_from_project = os.path.relpath(full_path,
                                               os.path.dirname(root_dir))

            # Parse header to get the authoritative @playbook_id from the file
            header = _parse_playbook_header(full_path)
            pb_id = header.get("playbook_id", "") or ""

            # .j2 files → template; files with @playbook_id → core; without → custom
            if fname.endswith(".j2"):
                file_type = "template"
            elif pb_id:
                file_type = "core"
            else:
                file_type = "custom"

            tags = _parse_tags(rel_from_project)
            checksum = _compute_file_checksum(full_path)
            file_size = os.path.getsize(full_path)

            new_id = register_playbook(
                db_path, rel_from_project, checksum,
                file_type=file_type, tags=tags, playbook_id=pb_id if pb_id else None,
                file_size=file_size,
            )
            if new_id is not None:
                registered += 1

    return registered


def resolve_playbook_by_id(db_path, playbook_id):
    """Resolve a playbook from the registry by its stable ID.

    Args:
        db_path: Path to the SQLite database.
        playbook_id: Stable identifier (e.g., "DEPLOY_LLAMA_SERVER_V1").

    Returns:
        dict with file_path and other registry fields, or None if not found.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_registry WHERE playbook_id = ?",
            (playbook_id,),
        ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}


def backfill_playbook_ids(db_path, project_root=None):
    """Backfill playbook_id for existing core playbook entries by reading headers.

    Queries the DB for core playbooks with NULL/empty playbook_id, reads the
    # @playbook_id: ID directive from each file on disk, and updates the DB
    row. Files that have been renamed/moved use the header as authoritative.

    Args:
        db_path: Path to the SQLite database.
        project_root: Project root directory (default: auto-resolved).

    Returns:
        int — number of rows updated.
    """
    from db.sqlite import pool

    if project_root is None:
        _module_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(_module_dir))

    updated = 0
    with pool(db_path) as conn:
        rows = conn.execute(
            "SELECT id, file_path FROM playbook_registry "
            "WHERE file_type = 'core' AND (playbook_id IS NULL OR playbook_id = '')"
        ).fetchall()

        for row_id, file_path in rows:
            full_path = os.path.join(project_root, file_path)
            if not os.path.exists(full_path):
                continue

            header = _parse_playbook_header(full_path)
            pb_id = header.get("playbook_id", "") or ""
            if pb_id:
                conn.execute(
                    "UPDATE playbook_registry SET playbook_id = ? WHERE id = ?",
                    (pb_id, row_id),
                )
                updated += 1

    return updated


def resolve_playbook_by_tags(db_path, *tags):
    """Resolve a playbook path from the registry by matching tags.

    Searches for a registered playbook whose tags field contains ALL
    provided tags. Returns the first match ordered by usage_counter
    (least used first, to distribute load across alternatives).

    Args:
        db_path: Path to the SQLite database.
        *tags: One or more tag strings to match against.

    Returns:
        dict with file_path and other registry fields.
        None if no matching playbook found.
    """
    from db.sqlite import pool

    if not tags:
        return None

    # Build WHERE clause: each tag must appear in the comma-separated tags field (ALL match)
    conditions = " AND ".join(
        f"(',' || tags || ',') LIKE ('%,{t},%')" for t in tags
    )
    query = f"SELECT * FROM playbook_registry WHERE {conditions} ORDER BY usage_counter_since_update ASC LIMIT 1"

    with pool(db_path) as conn:
        row = conn.execute(query).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}


def increment_usage_counter(db_path, playbook_id):
    """Increment the usage counter since last update for a registered playbook.

    Args:
        db_path: Path to the SQLite database.
        playbook_id: Stable playbook_id string (e.g. "preflight_check").

    Returns:
        True if updated successfully.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM playbook_registry WHERE playbook_id = ?",
            (playbook_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE playbook_registry SET usage_counter_since_update = usage_counter_since_update + 1 WHERE id = ?",
                (row[0],),
            )
    return True


def increment_error_counter(db_path, playbook_id):
    """Increment the error counter since last update for a registered playbook.

    Args:
        db_path: Path to the SQLite database.
        playbook_id: Stable playbook_id string (e.g. "preflight_check").

    Returns:
        True if updated successfully.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM playbook_registry WHERE playbook_id = ?",
            (playbook_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE playbook_registry SET error_counter_since_update = error_counter_since_update + 1 WHERE id = ?",
                (row[0],),
            )
    return True


def reset_counters(db_path, playbook_id=None):
    """Reset usage and error counters to zero.

    Args:
        db_path: Path to the SQLite database.
        playbook_id: If None, resets all playbooks. Otherwise resets one.

    Returns:
        int — number of rows updated.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        if playbook_id is not None:
            row = conn.execute(
                "SELECT id FROM playbook_registry WHERE playbook_id = ?",
                (playbook_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE playbook_registry SET usage_counter_since_update = 0, "
                    "error_counter_since_update = 0 WHERE id = ?",
                    (row[0],),
                )
                rows = 1
            else:
                rows = 0
        else:
            conn.execute(
                "UPDATE playbook_registry SET usage_counter_since_update = 0, "
                "error_counter_since_update = 0"
            )
            rows = conn.total_changes

    return rows


def list_playbooks(db_path, file_type=None, tags=None):
    """List all registered playbooks with optional filtering.

    Args:
        db_path: Path to the SQLite database.
        file_type: Optional filter — "core" or "custom".
        tags: Optional comma-separated tag string for filtering.

    Returns:
        list of dicts — matching playbook records.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        query = "SELECT * FROM playbook_registry"
        params = []

        if file_type and tags:
            query += " WHERE file_type = ? AND ("
            params.append(file_type)
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            conditions = " OR ".join(
                f"(',' || tags || ',') LIKE ('%,{t},%')" for t in tag_list
            )
            query += f"{conditions})"
        elif file_type:
            query += " WHERE file_type = ?"
            params.append(file_type)
        elif tags:
            query += " WHERE "
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            conditions = " OR ".join(
                f"(',' || tags || ',') LIKE ('%,{t},%')" for t in tag_list
            )
            query += conditions

        query += " ORDER BY file_type, file_path"

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        return [{k: row[k] for k in row.keys()} for row in rows]


def verify_playbook_integrity(db_path, project_root, mode="prod", exit_on_update=False):
    """Verify playbook hashes on disk match DB records.

    Also reads the # @version: N comment from each playbook and updates
    the version column if it differs from the stored value (dev-update mode).

    Args:
        db_path: Path to the SQLite database.
        project_root: Root directory of the project (for resolving file paths).
        mode: "prod" = exit on mismatch, "dev" = alert only,
              "dev-update" = update DB with current file hashes + versions.
        exit_on_update: If True and in dev-update mode with mismatches,
                        print detail output then sys.exit(0).

    Returns:
        dict with results: {"mismatches": [...], "new_files": []}
    """
    from db.sqlite import pool

    mismatches = []
    hash_mismatches = []
    size_mismatches = []
    new_files = []
    updated = 0
    version_updated = 0
    size_updated = 0
    mismatch_details = []  # (file_path, expected_hash, actual_hash, expected_size, actual_size) for dev-update

    try:
        with pool(db_path) as conn:
            rows = conn.execute(
                "SELECT id, file_path, checksum_sha256, version, file_size FROM playbook_registry"
            ).fetchall()

            for row_id, file_path, expected_hash, db_version, expected_size in rows:
                full_path = os.path.join(project_root, file_path)
                if not os.path.exists(full_path):
                    if mode == "prod":
                        print(f"[qr] CRITICAL: playbook missing from disk: {file_path}")
                        mismatches.append(file_path)
                    else:
                        new_files.append(file_path)
                    continue

                # Read and update version from playbook header
                disk_version = _parse_playbook_version(full_path)
                if disk_version and db_version and str(db_version) != str(disk_version):
                    print(f"[qr] VERSION UPDATE: {file_path} DB={db_version} -> file={disk_version}")
                    conn.execute(
                        "UPDATE playbook_registry SET version = ?, updated_at = datetime('now') WHERE id = ?",
                        (disk_version, row_id),
                    )
                    version_updated += 1

                actual_hash = _compute_file_checksum(full_path)
                actual_size = os.path.getsize(full_path)
                hash_changed = (actual_hash != expected_hash)
                size_changed = (expected_size is not None and actual_size != expected_size)

                if hash_changed:
                    hash_mismatches.append(file_path)
                    mismatch_details.append((file_path, expected_hash, actual_hash, expected_size, actual_size))
                    mismatches.append(file_path)
                    if mode == "dev-update":
                        conn.execute(
                            "UPDATE playbook_registry SET checksum_sha256 = ?, updated_at = datetime('now') WHERE id = ?",
                            (actual_hash, row_id),
                        )
                        updated += 1
                    elif mode == "dev":
                        print(f"[qr] DEV: hash changed: {file_path} ({expected_hash[:8]} -> {actual_hash[:8]})")

                if size_changed:
                    size_mismatches.append(file_path)
                    if mode == "dev-update":
                        conn.execute(
                            "UPDATE playbook_registry SET file_size = ?, updated_at = datetime('now') WHERE id = ?",
                            (actual_size, row_id),
                        )
                        # Don't double-count: hash updates already counted above
                        if not hash_changed:
                            updated += 1
                    elif mode == "prod":
                        print(f"[qr] CRITICAL: size mismatch: {file_path}")
                        print(f"  expected: {expected_size} bytes")
                        print(f"  actual:   {actual_size} bytes")
                    else:
                        print(f"[qr] DEV: size changed: {file_path} ({expected_size} -> {actual_size})")

            if updated:
                conn.commit()
                print(f"[qr] Updated {updated} hash(es)/size(s) in DB")
            if version_updated:
                conn.commit()
                print(f"[qr] Updated {version_updated} playbook version(s) in DB")

        if mode == "prod" and mismatches:
            parts = []
            if hash_mismatches:
                parts.append(f"{len(hash_mismatches)} hash(es)")
            if size_mismatches:
                parts.append(f"{len(size_mismatches)} size(s)")
            detail = ", ".join(parts)
            print(f"[qr] FATAL: {detail} mismatch(es) in {len(mismatches)} playbook(s). Exiting.")
            raise SystemExit(1)

        # dev-update: after sync, switch pb_mode to "prod".
        # Plain dev-update: incremental sync, keep running in prod mode.
        if mode == "dev-update" and mismatch_details:
            from qr_api import _CONFIG as _qr_cfg
            print("")
            print("=" * 60)
            print("PLAYBOOK CHECKSUMS + SIZES UPDATED")
            print("=" * 60)
            for fp, old_h, new_h, old_s, new_s in mismatch_details:
                size_info = ""
                if old_s is not None and new_s is not None and old_s != new_s:
                    size_info = f" | size {old_s} -> {new_s}"
                print(f"  {fp}{size_info}")
                print(f"    DB had: {old_h}")
                print(f"    Disk:   {new_h}")
            print("-" * 60)
            print("CHECKSUMS IN DATABASE HAVE BEEN ALTERED!")
            print("=" * 60)
            _qr_cfg["pb_mode"] = "prod"
            # Always keep running after dev-update sync
            print("[qr] Switched to prod mode — server will continue running")

        return {"mismatches": mismatches, "new_files": new_files}

    except SystemExit:
        raise
    except Exception as exc:
        print(f"[qr] WARNING: integrity check failed: {exc}")
        return {"mismatches": [], "new_files": []}
