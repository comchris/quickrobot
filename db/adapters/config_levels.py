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

"""Quickrobot — config_levels table CRUD adapter (CONFIG-1 Phase 2).

Provides read/write operations for per-instance, per-layer configuration
stored in the config_levels table. Each layer (L1-L7) can be managed
independently via the API.
"""

import json


class ConfigLevelError(Exception):
    """Raised on config_levels operation errors."""


def get_config_level(db_path, instance_id, level):
    """Get a single config level for an instance.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.
        level: Layer level (1-7).

    Returns:
        ConfigLevel dict or None if not found.
        Dict has keys: id, instance_id, level, source, env_vars, cli_opts,
                       model_params, metadata, created_at, updated_at
        env_vars/cli_opts/model_params/metadata are parsed from JSON strings.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM config_levels WHERE instance_id = ? AND level = ?",
            (instance_id, level),
        ).fetchone()
        if row is None:
            return None
        return _parse_config_level(row)


def get_all_config_levels(db_path, instance_id):
    """Get all config levels for an instance.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.

    Returns:
        List of ConfigLevel dicts, ordered by level ascending.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM config_levels WHERE instance_id = ? ORDER BY level ASC",
            (instance_id,),
        ).fetchall()
        return [_parse_config_level(row) for row in rows]


def set_config_level(db_path, instance_id, level, source,
                     env_vars=None, cli_opts=None, model_params=None):
    """Set (upsert) a config level for an instance.

    Uses INSERT OR REPLACE for atomic upsert semantics. The updated_at
    timestamp is automatically set by SQLite.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.
        level: Layer level (1-7).
        source: Source identifier string.
        env_vars: Optional dict of environment variables.
        cli_opts: Optional list of CLI arguments.
        model_params: Optional dict of model parameters.

    Returns:
        True on success.

    Raises:
        ConfigLevelError: If instance_id or level is invalid.
    """
    if level < 1 or level > 7:
        raise ConfigLevelError(f"Invalid level {level}: must be 1-7")

    env_json = json.dumps(env_vars) if env_vars else '{}'
    cli_json = json.dumps(cli_opts) if cli_opts else '[]'
    model_json = json.dumps(model_params) if model_params else '{}'

    from db.sqlite import pool

    with pool(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO config_levels
               (instance_id, level, source, env_vars, cli_opts, model_params)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (instance_id, level, source, env_json, cli_json, model_json),
        )
    return True


def update_config_level(db_path, instance_id, level, **kwargs):
    """Update specific fields of a config level.

    Only the provided keyword arguments are updated; others remain unchanged.
    Supports: env_vars, cli_opts, model_params, metadata, source.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.
        level: Layer level (1-7).
        **kwargs: Fields to update.

    Returns:
        True on success, False if the level doesn't exist.

    Raises:
        ConfigLevelError: If level is invalid.
    """
    if level < 1 or level > 7:
        raise ConfigLevelError(f"Invalid level {level}: must be 1-7")

    updates = []
    params = []

    for key, value in kwargs.items():
        if key in ("env_vars", "cli_opts", "model_params", "metadata"):
            json_value = json.dumps(value) if value is not None else None
            updates.append(f"{key} = ?")
            params.append(json_value)
        elif key == "source":
            updates.append("source = ?")
            params.append(value)
        elif key == "updated_at":
            updates.append("updated_at = ?")
            params.append(value)

    if not updates:
        return False

    params.extend([instance_id, level])

    from db.sqlite import pool

    with pool(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE config_levels SET {', '.join(updates)} WHERE instance_id = ? AND level = ?",
            params,
        )
        return cursor.rowcount > 0


def delete_config_level(db_path, instance_id, level):
    """Delete a specific config level for an instance.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.
        level: Layer level (1-7) to delete.

    Returns:
        True if a row was deleted, False if not found.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM config_levels WHERE instance_id = ? AND level = ?",
            (instance_id, level),
        )
        return cursor.rowcount > 0


def clear_instance_levels(db_path, instance_id):
    """Clear all config levels for an instance.

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.

    Returns:
        Number of rows deleted.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM config_levels WHERE instance_id = ?",
            (instance_id,),
        )
        return cursor.rowcount


def merge_from_api(db_path, instance_id, level, patch):
    """Merge a patch dict into an existing config level.

    For PUT /instances/<id>/config-levels/<level>: merges the provided
    fields into the existing layer (preserving values not in the patch).

    Args:
        db_path: Path to SQLite database.
        instance_id: Instance primary key.
        level: Layer level (1-7).
        patch: Dict of fields to merge (env_vars, cli_opts, model_params).

    Returns:
        True on success.
    """
    from db.sqlite import pool

    existing = get_config_level(db_path, instance_id, level)
    if existing is None:
        # If the level doesn't exist yet, do a full set
        return set_config_level(
            db_path, instance_id, level,
            source=patch.get("source", "api_patch"),
            env_vars=patch.get("env_vars"),
            cli_opts=patch.get("cli_opts"),
            model_params=patch.get("model_params"),
        )

    # Merge: existing values + patch overrides
    merged_env = {**existing["env_vars"], **(patch.get("env_vars") or {})}
    merged_cli = list(existing["cli_opts"])
    for item in (patch.get("cli_opts") or []):
        if item not in merged_cli:
            merged_cli.append(item)
    merged_model = {**existing["model_params"], **(patch.get("model_params") or {})}

    return set_config_level(
        db_path, instance_id, level,
        source=patch.get("source", existing["source"]),
        env_vars=merged_env,
        cli_opts=merged_cli,
        model_params=merged_model,
    )


def _parse_config_level(row):
    """Parse a DB row into a ConfigLevel dict with JSON fields decoded.

    Args:
        row: SQLite Row object from config_levels table.

    Returns:
        Dict with parsed JSON fields (env_vars, cli_opts, model_params, metadata).
    """
    d = dict(row)
    for key in ("env_vars", "cli_opts", "model_params", "metadata"):
        raw = d.get(key) or '{}'
        try:
            parsed = json.loads(raw)
            d[key] = parsed if isinstance(parsed, (dict, list)) else {}
        except (json.JSONDecodeError, TypeError):
            d[key] = {} if key in ("env_vars", "model_params", "metadata") else []
    return d
