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

"""quickrobot — Engine preset CRUD adapters.

Functions: add_preset, get_preset, list_presets, update_preset,
           delete_preset, search_presets.
All functions accept db_path as first positional argument.
"""
import sqlite3

import json


class PresetError(Exception):
    """Raised on preset-specific errors."""


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _normalize_config_template(raw):
    """Normalize config_template input to a JSON string for storage.

    Accepts either:
      - A dict (from WebUI form submit) — will be json.dumps'd
      - A string (already-JSON from API or previous read) — validated and returned as-is
      - None/missing — defaults to '{}'
      - Double-encoded string — unwrapped automatically

    Prevents double-encoding when config_template is passed as a string that's
    already JSON-encoded (e.g., when editing a preset that was previously loaded).
    """
    if raw is None:
        return "{}"
    if isinstance(raw, str):
        # Validate it's valid JSON; if not, treat as empty
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # Return the original string (already valid JSON dict)
                return raw
            elif isinstance(parsed, str):
                # Double-encoded: outer string wraps inner JSON — unwrap once
                try:
                    inner = json.loads(parsed)
                    if isinstance(inner, dict):
                        return parsed  # Return the inner JSON string
                    else:
                        return "{}"
                except (json.JSONDecodeError, ValueError):
                    return "{}"
            else:
                # Valid JSON but not a dict — normalize to {}
                return "{}"
        except (json.JSONDecodeError, ValueError):
            return "{}"
    # Dict or other type — serialize
    return json.dumps(raw or {})


def add_preset(db_path, engine_type_id, name, category="default",
               config_template=None, model_path=None, tags=None, model_id=None,
               gpu_device=None):
    """Create a new preset for an engine type.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Foreign key to engine_types table.
        name: Preset name (unique within engine_type).
        category: Category label (default 'default').
        config_template: dict of preset parameters (will be JSON-encoded).
        model_path: Optional model file path reference (deprecated, kept for compat).
        tags: list of tag strings (will be JSON-encoded).
        model_id: FK to engine_models(id) — preset inherits model params.
        gpu_device: GPU device setting (none/Vulkan/CUDA/custom).

    Returns:
        dict with the new preset's data.

    Raises:
        PresetError: If creation fails.
    """
    from db.sqlite import pool
    try:
        tmpl_json = _normalize_config_template(config_template)
        tags_json = json.dumps(tags or [])
        with pool(db_path) as conn:
            table_info = conn.execute("PRAGMA table_info(engine_presets)").fetchall()
            col_names = [r[1] for r in table_info]

            if "model_id" in col_names and "gpu_device" in col_names:
                # Current schema (migration 029+): no model_path
                cursor = conn.execute(
                    """INSERT INTO engine_presets
                       (engine_type_id, name, category, config_template, tags, model_id, gpu_device)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (engine_type_id, name, category, tmpl_json, tags_json, model_id, gpu_device),
                )
            elif "model_id" in col_names:
                # Pre-migration 029: has model_id but not gpu_device yet
                cursor = conn.execute(
                    """INSERT INTO engine_presets
                       (engine_type_id, name, category, config_template, model_path, tags, model_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (engine_type_id, name, category, tmpl_json, model_path, tags_json, model_id),
                )
            else:
                # Earliest schema: no model_id or gpu_device
                cursor = conn.execute(
                    """INSERT INTO engine_presets
                       (engine_type_id, name, category, config_template, model_path, tags)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (engine_type_id, name, category, tmpl_json, model_path, tags_json),
                )
            preset_id = cursor.lastrowid
            row = conn.execute(
                "SELECT * FROM engine_presets WHERE id = ?", (preset_id,)
            ).fetchone()
            result = _row_to_dict(row)
            result["config_template"] = json.loads(result.get("config_template") or "{}")
            result["tags"] = json.loads(result.get("tags") or "[]")
            return result
    except Exception as exc:
        raise PresetError(f"Failed to add preset '{name}': {exc}") from exc


def get_preset(db_path, preset_id):
    """Fetch a single preset by its id.

    Args:
        db_path: Path to the SQLite database.
        preset_id: Integer primary key.

    Returns:
        dict with preset data (config_template and tags decoded), or None.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_presets WHERE id = ?", (preset_id,)
        ).fetchone()
        if row is None:
            return None
        result = _row_to_dict(row)
        result["config_template"] = json.loads(result.get("config_template") or "{}")
        result["tags"] = json.loads(result.get("tags") or "[]")
        return result


def list_presets(db_path, engine_type_id=None):
    """List presets, optionally filtered by engine_type_id.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Filter to presets for a specific engine type.

    Returns:
        list of dicts with config_template and tags decoded.
    """
    from db.sqlite import pool
    query = "SELECT * FROM engine_presets"
    params = []
    if engine_type_id is not None:
        query += " WHERE engine_type_id = ?"
        params.append(engine_type_id)
    query += " ORDER BY name"

    with pool(db_path) as conn:
        cursor = conn.execute(query, params)
        results = []
        for row in cursor.fetchall():
            d = _row_to_dict(row)
            ct = d.get("config_template") or "{}"
            try:
                d["config_template"] = json.loads(ct)
            except (json.JSONDecodeError, ValueError):
                # Resilient parse: try truncating at first error position
                try:
                    parsed = json.loads(ct[:ct.rfind('}')+1])
                    d["config_template"] = parsed
                except Exception:
                    d["config_template"] = {}
            d["tags"] = json.loads(d.get("tags") or "[]")
            results.append(d)
        return results


def update_preset(db_path, preset_id, **fields):
    """Update preset fields by id.

    Args:
        db_path: Path to the SQLite database.
        preset_id: Integer primary key.
        **fields: Key-value pairs to update (including model_id for migration 026+).

    Returns:
        Updated preset dict, or None if not found.

    Raises:
        PresetError: If not found or no valid fields.
    """
    from db.sqlite import pool
    allowed = {"name", "category", "config_template", "model_path", "tags", "model_id", "gpu_device"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        raise PresetError("No valid fields to update")

    if "config_template" in updates:
        updates["config_template"] = _normalize_config_template(updates["config_template"])
    if "tags" in updates:
        updates["tags"] = json.dumps(updates["tags"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [preset_id]

    with pool(db_path) as conn:
        conn.execute(f"UPDATE engine_presets SET {set_clause} WHERE id = ?", values)
        row = conn.execute(
            "SELECT * FROM engine_presets WHERE id = ?", (preset_id,)
        ).fetchone()
        if row is None:
            raise PresetError(f"Preset {preset_id} not found")
        result = _row_to_dict(row)
        result["config_template"] = json.loads(result.get("config_template") or "{}")
        result["tags"] = json.loads(result.get("tags") or "[]")
        return result


def delete_preset(db_path, preset_id):
    """Delete a preset by id.

    Args:
        db_path: Path to the SQLite database.
        preset_id: Integer primary key.

    Returns:
        True if deleted, False if not found.
    Raises:
        sqlite3.IntegrityError: If instances reference this preset.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        # Check for FK references before deleting
        refs = conn.execute(
            "SELECT id, name FROM instances WHERE preset_id = ?", (preset_id,)
        ).fetchall()
        if refs:
            names = ", ".join(f'"{r[1]}"' for r in refs)
            raise sqlite3.IntegrityError(
                f"Preset {preset_id} is used by instance(s): {names}"
            )
        cursor = conn.execute(
            "DELETE FROM engine_presets WHERE id = ?", (preset_id,)
        )
        return cursor.rowcount > 0


def clone_preset(db_path, preset_id, engine_type_id):
    """Clone a preset 1:1, generating a unique name with _clN suffix.

    Args:
        db_path: Path to the SQLite database.
        preset_id: Integer primary key of source preset.
        engine_type_id: Engine type ID (must match source).

    Returns:
        dict with new preset data.

    Raises:
        PresetError: If source not found, engine mismatch, or clone fails.
    """
    from db.sqlite import pool
    import time as _time

    src = get_preset(db_path, preset_id)
    if src is None:
        raise PresetError(f"Preset {preset_id} not found")
    if src.get("engine_type_id") != engine_type_id:
        raise PresetError(
            f"Preset belongs to engine_type_id={src.get('engine_type_id')}, "
            f"requested {engine_type_id}"
        )

    # Collect all existing names for this engine_type to find unique suffix
    with pool(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM engine_presets WHERE engine_type_id = ?",
            (engine_type_id,),
        ).fetchall()
        existing_names = {r[0] for r in rows}

    base_name = src["name"]
    cloned_name = None
    for n in range(1, 100):
        candidate = f"{base_name}_cl{n}"
        if candidate not in existing_names:
            cloned_name = candidate
            break
    if cloned_name is None:
        # Fallback: timestamp suffix
        cloned_name = f"{base_name}_cl{_time.time_ns()}"

    return add_preset(
        db_path=db_path,
        engine_type_id=engine_type_id,
        name=cloned_name,
        category=src.get("category", "default"),
        config_template=src.get("config_template"),
        model_path=src.get("model_path"),
        tags=src.get("tags"),
        model_id=src.get("model_id"),
        gpu_device=src.get("gpu_device"),
    )


def search_presets(db_path, engine_type_id, tags=None, category=None):
    """Search presets by tags and/or category.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Filter to presets for this engine type.
        tags: list of tag strings to match (any match = included).
        category: Exact category filter.

    Returns:
        list of dicts matching the search criteria.
    """
    from db.sqlite import pool
    query = "SELECT * FROM engine_presets WHERE engine_type_id = ?"
    params = [engine_type_id]

    conditions = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if tags and len(tags) > 0:
        tag_patterns = [f"%{t}%" for t in tags]
        tag_clause = " OR ".join(["tags LIKE ?"] * len(tag_patterns))
        conditions.append(f"({tag_clause})")
        params.extend(tag_patterns)

    if conditions:
        query += " AND " + " AND ".join(conditions)
    query += " ORDER BY name"

    with pool(db_path) as conn:
        cursor = conn.execute(query, params)
        results = []
        for row in cursor.fetchall():
            d = _row_to_dict(row)
            d["config_template"] = json.loads(d.get("config_template") or "{}")
            d["tags"] = json.loads(d.get("tags") or "[]")
            results.append(d)
        return results
