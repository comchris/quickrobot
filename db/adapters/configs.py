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

"""quickrobot — Config adapters (engine_configs, config_global, node_configs).

Functions: set_engine_config, get_engine_config, delete_engine_config,
           get_all_engine_configs, set_global_config, get_global_config,
           get_all_global_config, set_node_config, get_node_config,
           delete_node_config.
All functions accept db_path as first positional argument.
"""

import json


class ConfigError(Exception):
    """Raised on config-specific errors."""


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Engine type configs (engine_configs table)
# ---------------------------------------------------------------------------

def set_engine_config(db_path, engine_type_id, key, value, description="", default_value=None):
    """Set or update a config key for an engine type.

    Uses INSERT OR IGNORE for default initialisation (no overwrite).
    External callers that need upsert semantics should use the low-level
    INSERT OR REPLACE directly or delete first.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Foreign key to engine_types table.
        key: Config key name (unique per engine_type_id).
        value: Config value string.
        description: Optional human-readable description.
        default_value: Optional original default for drift detection.

    Returns:
        True if set successfully.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO engine_configs
               (engine_type_id, key, value, description, default_value)
               VALUES (?, ?, ?, ?, ?)""",
            (engine_type_id, key, value, description, default_value),
        )
    return True


def update_engine_config(db_path, engine_type_id, key, value, description=""):
    """Set or update a config key for an engine type.

    Uses INSERT OR REPLACE for upsert semantics — works for both initial
    defaults and subsequent updates (unlike set_engine_config which uses
    INSERT OR IGNORE and silently skips existing rows).

    Description preservation: if description is empty string, the existing
    DB description is read and preserved (prevents "save all" from wiping
    descriptions on other keys in the batch).

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Foreign key to engine_types table.
        key: Config key name (unique per engine_type_id).
        value: Config value string.
        description: Optional human-readable description (empty = preserve existing).

    Returns:
        True if set successfully.
    """
    from db.sqlite import pool
    # Preserve existing description when caller passes empty string
    if not description:
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT description FROM engine_configs WHERE engine_type_id=? AND key=?",
                (engine_type_id, key),
            ).fetchone()
            if row and row["description"]:
                description = row["description"]
    with pool(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO engine_configs
               (engine_type_id, key, value, description)
               VALUES (?, ?, ?, ?)""",
            (engine_type_id, key, value, description),
        )
    return True


def get_engine_config(db_path, engine_type_id, key=None):
    """Get config for an engine type.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Foreign key to engine_types table.
        key: Optional specific key name. If None, returns all keys.

    Returns:
        If key provided: dict with {key, value, description}, or None.
        If key is None: dict mapping key -> {value, description}.
    """
    from db.sqlite import pool
    if key:
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM engine_configs WHERE engine_type_id = ? AND key = ?",
                (engine_type_id, key),
            ).fetchone()
            return _row_to_dict(row)
    else:
        with pool(db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM engine_configs WHERE engine_type_id = ?",
                (engine_type_id,),
            )
            result = {}
            for row in cursor.fetchall():
                d = _row_to_dict(row)
                result[d["key"]] = {"value": d["value"], "description": d["description"]}
            return result


def delete_engine_config(db_path, engine_type_id, key):
    """Remove a config key for an engine type.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Foreign key to engine_types table.
        key: Config key name to remove.

    Returns:
        True if deleted, False if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM engine_configs WHERE engine_type_id = ? AND key = ?",
            (engine_type_id, key),
        )
        return cursor.rowcount > 0


def get_all_engine_configs(db_path):
    """Return all engine configs grouped by engine_type_id.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        dict mapping engine_type_id -> {key -> value}.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM engine_configs ORDER BY engine_type_id, key"
        )
        result = {}
        for row in cursor.fetchall():
            et_id = row["engine_type_id"]
            if et_id not in result:
                result[et_id] = {}
            result[et_id][row["key"]] = row["value"]
        return result


# ---------------------------------------------------------------------------
# Global config (config_global table)
# ---------------------------------------------------------------------------

def set_global_config(db_path, key, value, description=""):
    """Set or update a global config key.

    Args:
        db_path: Path to the SQLite database.
        key: Config key name (primary key).
        value: Config value string.
        description: Optional human-readable description.

    Returns:
        True if set successfully.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO config_global (key, value, description)
               VALUES (?, ?, ?)""",
            (key, value, description),
        )
    return True


def get_global_config(db_path, key=None):
    """Get global config values.

    Args:
        db_path: Path to the SQLite database.
        key: Optional specific key name. If None, returns all keys.

    Returns:
        If key provided: dict with {key, value, description}, or None.
        If key is None: dict mapping key -> {value, description}.
    """
    from db.sqlite import pool
    if key:
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM config_global WHERE key = ?", (key,)
            ).fetchone()
            return _row_to_dict(row)
    else:
        with pool(db_path) as conn:
            cursor = conn.execute("SELECT * FROM config_global")
            result = {}
            for row in cursor.fetchall():
                d = _row_to_dict(row)
                result[d["key"]] = {"value": d["value"], "description": d["description"]}
            return result


def get_all_global_config(db_path):
    """Return all global config as a flat key->value dict.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        dict mapping key -> value string.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute("SELECT key, value FROM config_global")
        return {row["key"]: row["value"] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Node configs (node_configs table)
# ---------------------------------------------------------------------------

def set_node_config(db_path, node_id, engine_type_id, key, value):
    """Set a per-node config value for an engine type.

    Args:
        db_path: Path to the SQLite database.
        node_id: Foreign key to nodes table.
        engine_type_id: Foreign key to engine_types table.
        key: Config key name (unique within node+engine_type).
        value: Config value string.

    Returns:
        True if set successfully.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO node_configs
               (node_id, engine_type_id, key, value)
               VALUES (?, ?, ?, ?)""",
            (node_id, engine_type_id, key, value),
        )
    return True


def get_node_config(db_path, node_id, engine_type_id, key=None):
    """Get config values for a node+engine type pair.

    Args:
        db_path: Path to the SQLite database.
        node_id: Foreign key to nodes table.
        engine_type_id: Foreign key to engine_types table.
        key: Optional specific key name. If None, returns all keys.

    Returns:
        If key provided: dict with {key, value}, or None.
        If key is None: dict mapping key -> value.
    """
    from db.sqlite import pool
    if key:
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM node_configs WHERE node_id = ? AND engine_type_id = ? AND key = ?",
                (node_id, engine_type_id, key),
            ).fetchone()
            return _row_to_dict(row)
    else:
        with pool(db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM node_configs WHERE node_id = ? AND engine_type_id = ?",
                (node_id, engine_type_id),
            )
            result = {}
            for row in cursor.fetchall():
                result[row["key"]] = row["value"]
            return result


def delete_node_config(db_path, node_id, engine_type_id, key):
    """Remove a node config key.

    Args:
        db_path: Path to the SQLite database.
        node_id: Foreign key to nodes table.
        engine_type_id: Foreign key to engine_types table.
        key: Config key name to remove.

    Returns:
        True if deleted, False if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM node_configs WHERE node_id = ? AND engine_type_id = ? AND key = ?",
            (node_id, engine_type_id, key),
        )
        return cursor.rowcount > 0


def get_polling_intervals(db_path, engine_type_id, is_local=True):
    """Get polling interval for an engine type from DB config with code defaults as fallback.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Foreign key to engine_types table.
        is_local: True for localhost (node_id==1), False for remote nodes.

    Returns:
        int — polling interval in seconds.
    """
    from lib.lib_constants import POLLING_INTERVAL_LOCAL_SEC, POLLING_INTERVAL_REMOTE_SEC
    from db.sqlite import pool

    key = "polling_interval_local_sec" if is_local else "polling_interval_remote_sec"

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM engine_configs WHERE engine_type_id = ? AND key = ?",
            (engine_type_id, key),
        ).fetchone()

    if row and isinstance(row["value"], str) and row["value"].isdigit():
        return int(row["value"])

    # Fallback to code defaults
    return POLLING_INTERVAL_LOCAL_SEC if is_local else POLLING_INTERVAL_REMOTE_SEC


