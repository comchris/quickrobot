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

"""quickrobot — Engine type registry CRUD adapters.

Functions: add_engine_type, get_engine_type, get_engine_type_by_name,
           list_engine_types, update_engine_type, delete_engine_type.
All functions accept db_path as first positional argument.
"""

import json


class EngineTypeError(Exception):
    """Raised on engine type errors."""


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def add_engine_type(db_path, name, display_name, module_path,
                    version="1.0", enabled=1, capabilities=None, engine_id=None):
    """Register a new engine type in the registry.

    Args:
        db_path: Path to the SQLite database.
        name: Unique identifier (e.g., 'llama_server', 'rpc').
        display_name: Human-readable name.
        module_path: Python import path (e.g., 'engine.llama_server').
        version: Module version string (default '1.0').
        enabled: 1 or 0 (default 1).
        capabilities: dict of capability metadata (will be JSON-encoded).

    Returns:
        dict with the new engine type's data including assigned id.

    Raises:
        EngineTypeError: If registration fails.
    """
    from db.sqlite import pool
    try:
        cap_json = json.dumps(capabilities or {})
        with pool(db_path) as conn:
            # If explicit engine_id provided, check if it's available (no existing row with that id)
            if engine_id is not None:
                existing = conn.execute(
                    "SELECT id FROM engine_types WHERE name = ?", (name,)
                ).fetchone()
                if existing is None:
                    # Fresh insert with explicit id
                    cursor = conn.execute(
                        """INSERT INTO engine_types
                           (id, name, display_name, module_path, version, enabled, capabilities)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (engine_id, name, display_name, module_path, version, enabled, cap_json),
                    )
                    et_id = engine_id
                elif existing["id"] != engine_id:
                    # Sync drifted ID back to expected fixed id
                    conn.execute(
                        "UPDATE engine_types SET id = ? WHERE id = ?", (engine_id, existing["id"])
                    )
                    et_id = engine_id
                else:
                    et_id = existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO engine_types
                       (name, display_name, module_path, version, enabled, capabilities)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (name, display_name, module_path, version, enabled, cap_json),
                )
                et_id = cursor.lastrowid
            row = conn.execute(
                "SELECT * FROM engine_types WHERE id = ?", (et_id,)
            ).fetchone()
            return _row_to_dict(row)
    except Exception as exc:
        raise EngineTypeError(f"Failed to add engine type '{name}': {exc}") from exc


def get_engine_type(db_path, engine_type_id):
    """Fetch a single engine type by its id.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Integer primary key.

    Returns:
        dict with engine type data and capabilities decoded, or None.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_types WHERE id = ?", (engine_type_id,)
        ).fetchone()
        if row is None:
            return None
        result = _row_to_dict(row)
        result["capabilities"] = json.loads(result.get("capabilities") or "{}")
        return result


def get_engine_type_by_name(db_path, name):
    """Fetch a single engine type by its name string.

    Args:
        db_path: Path to the SQLite database.
        name: String name (e.g., 'rpc', 'llama_server').

    Returns:
        dict with engine type data and capabilities decoded, or None.
    """
    # Alias: "rpc" → "llama_rpc" (legacy name still used by WebUI pages)
    _NAME_ALIASES = {"rpc": "llama_rpc"}
    resolved_name = _NAME_ALIASES.get(name, name)

    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_types WHERE name = ?", (resolved_name,)
        ).fetchone()
        if row is None:
            return None
        result = _row_to_dict(row)
        result["capabilities"] = json.loads(result.get("capabilities") or "{}")
        return result


def list_engine_types(db_path, enabled_only=False):
    """Return all engine types (optionally filtered to enabled).

    Args:
        db_path: Path to the SQLite database.
        enabled_only: If True, only return enabled types (default False).

    Returns:
        list of dicts, each with capabilities decoded from JSON.
    """
    from db.sqlite import pool
    query = "SELECT * FROM engine_types"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY name"

    with pool(db_path) as conn:
        cursor = conn.execute(query)
        results = []
        for row in cursor.fetchall():
            d = _row_to_dict(row)
            d["capabilities"] = json.loads(d.get("capabilities") or "{}")
            results.append(d)
        return results


def update_engine_type(db_path, engine_type_id, **fields):
    """Update engine type metadata by id.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Integer primary key.
        **fields: Key-value pairs to update (name, display_name, module_path, etc.)

    Returns:
        Updated engine type dict, or None if not found.

    Raises:
        EngineTypeError: If engine type not found.
    """
    from db.sqlite import pool
    allowed = {"name", "display_name", "module_path", "version",
               "enabled", "capabilities"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        raise EngineTypeError("No valid fields to update")

    if "capabilities" in updates:
        updates["capabilities"] = json.dumps(updates["capabilities"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [engine_type_id]

    with pool(db_path) as conn:
        conn.execute(f"UPDATE engine_types SET {set_clause} WHERE id = ?", values)
        row = conn.execute(
            "SELECT * FROM engine_types WHERE id = ?", (engine_type_id,)
        ).fetchone()
        if row is None:
            raise EngineTypeError(f"Engine type {engine_type_id} not found")
        result = _row_to_dict(row)
        result["capabilities"] = json.loads(result.get("capabilities") or "{}")
        return result


def delete_engine_type(db_path, engine_type_id):
    """Delete an engine type by id.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Integer primary key.

    Returns:
        True if deleted, False if not found.

    Raises:
        EngineTypeError: If active instances reference this engine type.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM instances WHERE engine_type_id = ?",
            (engine_type_id,),
        ).fetchone()["cnt"]
        if count > 0:
            raise EngineTypeError(
                f"Engine type {engine_type_id} has {count} instance(s); delete them first"
            )
        cursor = conn.execute(
            "DELETE FROM engine_types WHERE id = ?", (engine_type_id,)
        )
        return cursor.rowcount > 0
