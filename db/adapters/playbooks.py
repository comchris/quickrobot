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
           register_all_core_playbooks, resolve_playbook_by_tags,
           resolve_playbook_by_id, backfill_playbook_ids,
           increment_usage_counter, increment_error_counter, list_playbooks,
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


# Stable, human-readable IDs for core playbooks.
# Format: <ACTION>_<ENGINE>_V1 — survives filename changes.
# Keys are basenames (no directory prefix) for lookup by backfill/register.
_CORE_PLAYBOOK_IDS = {
    "deploy_llama_server.yml": "DEPLOY_LLAMA_SERVER_V1",
    "deploy_rpc.yml": "DEPLOY_LLAMA_RPC_V1",
    "deploy_iperf3.yml": "DEPLOY_IPERF3_V1",
    "update_llama_server.yml": "UPDATE_LLAMA_SERVER_V1",
    "update_and_compile.yml": "UPDATE_AND_COMPILE_V1",
    "undeploy_llama_server.yml": "UNDEPLOY_LLAMA_SERVER_V1",
    "undeploy_rpc.yml": "UNDEPLOY_LLAMA_RPC_V1",
    "undeploy_iperf3.yml": "UNDEPLOY_IPERF3_V1",
    "check_undeploy.yml": "CHECK_UNDEPLOY_V1",
    "manage_instance.yml": "MANAGE_INSTANCE_V1",
    "clean_shared_build.yml": "CLEAN_SHARED_LLAMACPP_BUILD_V1",
    # Node subdirectory playbooks (basename key for lookup)
    "validate.yml": "NODE_VALIDATE_V1",
    "get_instance_logs.yml": "NODE_GET_INSTANCE_LOGS_V1",
    "discover.yml": "NODE_DISCOVER_V1",
    "scan_models.yml": "NODE_SCAN_MODELS_V1",
    # Top-level management playbooks
    "apt_update.yml": "APT_UPDATE_V1",
    "apt_upgrade.yml": "APT_UPGRADE_V1",
    "reboot_node.yml": "REBOOT_NODE_V1",
    "shutdown_node.yml": "SHUTDOWN_NODE_V1",
    # Configuration and shared playbooks
    "update_config.yml": "UPDATE_CONFIG_V1",
    "check_binary.yml": "COMMON_CHECK_LLAMA_BINARY_V1",
    "undeploy_base.yml": "COMMON_UNDEPLOY_V1",
}

# Mapping from core playbook basename to DB tags.
# Used during initial registration only.
_CORE_PLAYBOOK_TAGS = {
    "deploy_llama_server.yml": "deploy,llama_server",
    "deploy_rpc.yml": "deploy,rpc",
    "deploy_iperf3.yml": "deploy,iperf3",
    "update_llama_server.yml": "update,llama_server",
    "update_and_compile.yml": "update,compile,llama_server,rpc",
    "undeploy_llama_server.yml": "undeploy,llama_server",
    "undeploy_rpc.yml": "undeploy,rpc",
    "undeploy_iperf3.yml": "undeploy,iperf3",
    "check_undeploy.yml": "check_undeploy",
    "manage_instance.yml": "manage",
    "clean_shared_build.yml": "cleanup,build",
    # Node subdirectory playbooks
    "node/validate.yml": "validate,node",
    "node/get_instance_logs.yml": "logs,node",
    "node/discover.yml": "discover,node",
    "node/scan_models.yml": "scan_models,node",
    # Top-level management playbooks
    "apt_update.yml": "apt_update,node",
    "apt_upgrade.yml": "apt_upgrade,node",
    "reboot_node.yml": "reboot,node",
    "shutdown_node.yml": "shutdown,node",
    # Shared subdirectory playbooks
    "check_binary.yml": "common,binary_check",
    "undeploy_base.yml": "common,undeploy",
}


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
        if file_type == "core":
            if playbook_id:
                conn.execute(
                    """INSERT OR REPLACE INTO playbook_registry
                       (file_path, checksum_sha256, file_type, tags, playbook_id, file_size)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (file_path, checksum, file_type, tags, playbook_id, file_size),
                )
            else:
                conn.execute(
                    """INSERT OR REPLACE INTO playbook_registry
                       (file_path, checksum_sha256, file_type, tags, file_size)
                       VALUES (?, ?, ?, ?, ?)""",
                    (file_path, checksum, file_type, tags, file_size),
                )
        else:
            if playbook_id is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO playbook_registry
                       (file_path, checksum_sha256, file_type, tags,
                        playbook_id, updated_at, file_size)
                       VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                    (file_path, checksum, file_type, tags, playbook_id, file_size),
                )
            else:
                conn.execute(
                    """INSERT OR REPLACE INTO playbook_registry
                       (file_path, checksum_sha256, file_type, tags, updated_at, file_size)
                       VALUES (?, ?, ?, ?, datetime('now'), ?)""",
                    (file_path, checksum, file_type, tags, file_size),
                )

    # Return the id — look up by playbook_id (core) or file_path (custom)
    if playbook_id:
        row = resolve_playbook_by_id(db_path, playbook_id)
    else:
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
    """Register only known (mapped) core playbooks from the _CORE_PLAYBOOK_IDS map.

    Unlike register_all_core_playbooks, this does NOT auto-register unknown
    .yml files as "custom". Only playbooks with a stable ID in the known map
    are registered on startup. Unknown playbooks require manual API import.

    Header integrity check: before registration, verifies that the file's
    @playbook_id and @version directives match the expected values. Playbooks
    with mismatched headers are skipped (INSERT OR REPLACE still used for
    those that pass checks).

    Args:
        db_path: Path to the SQLite database.
        root_dir: Base directory containing playbooks (absolute path).

    Returns:
        int — number of playbooks registered.
    """
    from lib.lib_constants import UE_PLAYBOOK_ROOT_DIR

    if root_dir is None:
        _module_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(_module_dir)
        root_dir = os.path.normpath(
            os.path.join(_project_root, UE_PLAYBOOK_ROOT_DIR)
        )

    registered = 0
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fname in filenames:
            if not (fname.endswith(".yml") or fname.endswith(".yaml")):
                continue
            if "_backup_" in fname:
                continue

            full_path = os.path.join(dirpath, fname)
            rel_from_project = os.path.relpath(full_path,
                                               os.path.dirname(root_dir))

            tags = _CORE_PLAYBOOK_TAGS.get(fname, "")
            if "/" in rel_from_project:
                subdir = os.path.basename(os.path.dirname(rel_from_project))
                if not tags and subdir:
                    tags = subdir

            # Parse header BEFORE computing checksum (header may be implanted)
            actual_header = _parse_playbook_header(full_path)

            # Only register known playbooks — skip unknowns entirely
            full_basename = os.path.basename(rel_from_project)
            pb_id = _CORE_PLAYBOOK_IDS.get(full_basename)
            if not pb_id:
                continue  # Skip unknown playbooks in dev mode

            # Header integrity check
            if actual_header["playbook_id"] != pb_id:
                print(
                    f"[qr] SKIP {rel_from_project}: "
                    f"header playbook_id={actual_header['playbook_id']} "
                    f"!= expected {pb_id}"
                )
                continue

            checksum = _compute_file_checksum(full_path)
            file_type = "core"
            new_id = register_playbook(
                db_path, rel_from_project, checksum,
                file_type=file_type, tags=tags, playbook_id=pb_id,
            )
            if new_id is not None:
                registered += 1

    return registered


def register_all_core_playbooks(db_path, root_dir=None):
    """Scan the playbooks directory and register any missing core playbooks.

    Walks the directory tree under root_dir, computes checksums for all
    .yml/.yaml files, and inserts them into playbook_registry if not
    already present (INSERT OR IGNORE). Registers unknowns as "custom" type.

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
        # Resolve relative to project root (parent of db/ dir)
        _module_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(_module_dir)
        root_dir = os.path.normpath(
            os.path.join(_project_root, UE_PLAYBOOK_ROOT_DIR)
        )

    registered = 0
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fname in filenames:
            if not (fname.endswith(".yml") or fname.endswith(".yaml")):
                continue
            # Skip backup files — they have "_backup_" in the name and are
            # named with correct extension (<name>.yml_backup_TIMESTAMP),
            # but old-style backups (<name>_backup_TIMESTAMP.yml) would match.
            if "_backup_" in fname:
                continue

            full_path = os.path.join(dirpath, fname)
            # Compute relative path from project root
            rel_from_project = os.path.relpath(full_path,
                                               os.path.dirname(root_dir))

            tags = _CORE_PLAYBOOK_TAGS.get(fname, "")
            # If the file is in a subdirectory and has no pre-assigned tags,
            # use the immediate subdirectory name as the tag.
            if "/" in rel_from_project:
                subdir = os.path.basename(os.path.dirname(rel_from_project))
                if not tags and subdir:
                    tags = subdir

            checksum = _compute_file_checksum(full_path)
            # Derive playbook_id from the filename mapping (full path -> basename)
            full_basename = os.path.basename(rel_from_project)
            pb_id = _CORE_PLAYBOOK_IDS.get(full_basename, "")

            # Known playbooks get "core" type; unknowns get "custom"
            file_type = "core" if pb_id else "custom"

            new_id = register_playbook(
                db_path, rel_from_project, checksum,
                file_type=file_type, tags=tags, playbook_id=pb_id if pb_id else None,
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


def backfill_playbook_ids(db_path):
    """Backfill playbook_id for existing core playbook entries.

    Walks the _CORE_PLAYBOOK_IDS map, matches by file_path basename,
    and updates rows with empty or NULL playbook_id.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        int — number of rows updated.
    """
    from db.sqlite import pool

    updated = 0
    with pool(db_path) as conn:
        # Find core playbooks with empty/NULL playbook_id
        rows = conn.execute(
            "SELECT id, file_path FROM playbook_registry "
            "WHERE file_type = 'core' AND (playbook_id IS NULL OR playbook_id = '')"
        ).fetchall()

        for row_id, file_path in rows:
            basename = os.path.basename(file_path)
            pb_id = _CORE_PLAYBOOK_IDS.get(basename)
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
        playbook_id: ID of the playbook in the registry.

    Returns:
        True if updated successfully.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        conn.execute(
            "UPDATE playbook_registry SET usage_counter_since_update = usage_counter_since_update + 1 WHERE id = ?",
            (playbook_id,),
        )
    return True


def increment_error_counter(db_path, playbook_id):
    """Increment the error counter since last update for a registered playbook.

    Args:
        db_path: Path to the SQLite database.
        playbook_id: ID of the playbook in the registry.

    Returns:
        True if updated successfully.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        conn.execute(
            "UPDATE playbook_registry SET error_counter_since_update = error_counter_since_update + 1 WHERE id = ?",
            (playbook_id,),
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
            conn.execute(
                "UPDATE playbook_registry SET usage_counter_since_update = 0, "
                "error_counter_since_update = 0 WHERE id = ?",
                (playbook_id,),
            )
            rows = 1
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
    mismatch_details = []  # (file_path, expected_hash, actual_hash) for dev-update

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
                if actual_hash != expected_hash:
                    hash_mismatches.append(file_path)
                    mismatch_details.append((file_path, expected_hash, actual_hash))
                    mismatches.append(file_path)
                    if mode == "dev-update":
                        conn.execute(
                            "UPDATE playbook_registry SET checksum_sha256 = ?, updated_at = datetime('now') WHERE id = ?",
                            (actual_hash, row_id),
                        )
                        updated += 1
                    elif mode == "dev":
                        print(f"[qr] DEV: hash changed: {file_path} ({expected_hash[:8]} -> {actual_hash[:8]})")

                # Update file_size in dev-update mode if it changed
                actual_size = os.path.getsize(full_path)
                if expected_size is not None and actual_size != expected_size:
                    size_mismatches.append(file_path)
                    if mode == "dev-update":
                        conn.execute(
                            "UPDATE playbook_registry SET file_size = ?, updated_at = datetime('now') WHERE id = ?",
                            (actual_size, row_id),
                        )
                        size_updated += 1
                        updated += 1
                    elif mode == "prod":
                        print(f"[qr] CRITICAL: size mismatch: {file_path}")
                        print(f"  expected: {expected_size} bytes")
                        print(f"  actual:   {actual_size} bytes")
                    else:
                        print(f"[qr] DEV: size changed: {file_path} ({expected_size} -> {actual_size})")

            if updated:
                conn.commit()
                print(f"[qr] Updated {updated} playbook hash(es) in DB")
            if size_updated:
                conn.commit()
                print(f"[qr] Updated {size_updated} playbook file_size(s) in DB")
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

        # dev-update exit-on-update: print detailed mismatch list then quit
        if mode == "dev-update" and mismatch_details and exit_on_update:
            print("")
            print("=" * 60)
            print("PLAYBOOK CHECKSUMS UPDATED")
            print("=" * 60)
            for fp, old_h, new_h in mismatch_details:
                print(f"  {fp}")
                print(f"    DB had: {old_h}")
                print(f"    Disk:   {new_h}")
            print("-" * 60)
            print("CHECKSUMS IN DATABASE HAVE BEEN ALTERED!")
            print("=" * 60)
            sys.exit(0)

        return {"mismatches": mismatches, "new_files": new_files}

    except SystemExit:
        raise
    except Exception as exc:
        print(f"[qr] WARNING: integrity check failed: {exc}")
        return {"mismatches": [], "new_files": []}
