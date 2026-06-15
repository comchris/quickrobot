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

"""quickrobot — Engine model CRUD adapters.

Functions: add_model, update_model, get_model, list_models, update_model_discovered,
           delete_model, scan_host_for_models.
All functions accept db_path as first positional argument.
"""

import json

# Fields that can be updated via the model update endpoint
ALLOWED_FIELDS = (
    "name", "model_path", "mmproj_path", "draft_model_path",
    "size_bytes", "last_modified", "host_id", "quantization",
    "model_params", "is_active",
    "sha256_model", "sha256_mmproj", "sha256_draft",
    "sha256_verified_at_model", "sha256_verified_at_mmproj", "sha256_verified_at_draft",
)


class ModelError(Exception):
    """Raised on model-specific errors."""


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def add_model(db_path, engine_type_id, name, model_path, mmproj_path=None,
              draft_model_path=None, size_bytes=None, last_modified=None,
              host_id=None, quantization=None, is_sharded=0, total_shards=None,
              model_params=None, sha256_model=None, sha256_mmproj=None,
              sha256_draft=None):
    """Register a discovered model for an engine type.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Foreign key to engine_types table for model registration.
        name: Human-readable model name.
        model_path: Primary GGUF model file path (unique with engine_type_id).
        mmproj_path: Optional multimodal projector path (LLAMA_ARG_MMPROJ).
        draft_model_path: Optional draft model for speculative decoding (LLAMA_ARG_SPEC_DRAFT_MODEL).
        size_bytes: Optional file size in bytes.
        last_modified: Optional ISO timestamp of last modification.
        host_id: Optional foreign key to nodes table.
        quantization: Optional quantization type (e.g., Q4_K_M, Q8_0).
        is_sharded: 0=individual file, 1=part of multi-file model.
        total_shards: Number of shards (for grouped models) or None.
        model_params: JSON string of LLAMA_ARG_* params (migration 026+).
        sha256_model: Expected SHA256 of model file.
        sha256_mmproj: Expected SHA256 of mmproj file.
        sha256_draft: Expected SHA256 of draft model file.

    Returns:
        dict with the new model's data, or None if already exists.

    Raises:
        ModelError: If insert fails.
    """
    from db.sqlite import pool
    try:
        with pool(db_path) as conn:
            # Build column list dynamically — only include columns that exist
            cols = ["engine_type_id", "name", "model_path", "mmproj_path",
                    "draft_model_path", "size_bytes", "last_modified", "quantization"]
            placeholders = ["?"] * len(cols)
            values = [engine_type_id, name, model_path, mmproj_path, draft_model_path,
                      size_bytes, last_modified, quantization]

            # Check if sharded columns exist (migration 022+)
            table_info = conn.execute("PRAGMA table_info(engine_models)").fetchall()
            has_is_sharded = any(r[1] == "is_sharded" for r in table_info)
            has_total_shards = any(r[1] == "total_shards" for r in table_info)
            if has_is_sharded:
                cols.append("is_sharded")
                placeholders.append("?")
                values.append(is_sharded)
            if has_total_shards:
                cols.append("total_shards")
                placeholders.append("?")
                values.append(total_shards)

            # Check if host_id column exists (may not in older schemas)
            has_host_id = any(r[1] == "host_id" for r in table_info)
            if has_host_id:
                cols.append("host_id")
                placeholders.append("?")
                values.append(host_id)

            # Check if model_params + sha256 columns exist (migration 026+)
            has_model_params = any(r[1] == "model_params" for r in table_info)
            has_sha256_model = any(r[1] == "sha256_model" for r in table_info)
            if has_model_params and model_params:
                cols.append("model_params")
                placeholders.append("?")
                values.append(model_params)
            if has_sha256_model and sha256_model:
                cols.append("sha256_model")
                placeholders.append("?")
                values.append(sha256_model)
            if has_sha256_model and sha256_mmproj:
                cols.append("sha256_mmproj")
                placeholders.append("?")
                values.append(sha256_mmproj)
            if has_sha256_model and sha256_draft:
                cols.append("sha256_draft")
                placeholders.append("?")
                values.append(sha256_draft)

            cursor = conn.execute(
                f"""INSERT OR IGNORE INTO engine_models
                    ({', '.join(cols)})
                    VALUES ({', '.join(placeholders)})""",
                values,
            )
            if cursor.lastrowid:
                row = conn.execute(
                    "SELECT * FROM engine_models WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                result = _row_to_dict(row)
                result["_new"] = True
                return result
            # Already exists — find existing id
            existing = conn.execute(
                "SELECT * FROM engine_models WHERE engine_type_id = ? AND model_path = ?",
                (engine_type_id, model_path),
            ).fetchone()
            return _row_to_dict(existing)
    except Exception as exc:
        raise ModelError(f"Failed to add model '{name}': {exc}") from exc


def update_model(db_path, model_id, name=None, model_path=None,
                 mmproj_path=None, draft_model_path=None, size_bytes=None,
                 last_modified=None, host_id=None, quantization=None,
                 model_params=None, sha256_model=None, sha256_mmproj=None,
                 sha256_draft=None, sha256_verified_at_model=None,
                 sha256_verified_at_mmproj=None, sha256_verified_at_draft=None,
                 is_active=None):
    """Update an existing model entry.

    Args:
        db_path: Path to the SQLite database.
        model_id: Integer primary key of the model to update.
        name: New human-readable name (optional).
        model_path: New model file path (optional).
        mmproj_path: New multimodal projector path (optional).
        draft_model_path: New draft model path (optional).
        size_bytes: New file size in bytes (optional).
        last_modified: New ISO timestamp (optional).
        host_id: New node host ID (optional).
        quantization: New quantization type (optional, e.g., Q4_K_M).
        model_params: JSON string of LLAMA_ARG_* params (migration 026+).
        sha256_model: Expected SHA256 of model file.
        sha256_mmproj: Expected SHA256 of mmproj file.
        sha256_draft: Expected SHA256 of draft model file.
        sha256_verified_at_model: Last verification timestamp for model file.
        sha256_verified_at_mmproj: Last verification timestamp for mmproj file.
        sha256_verified_at_draft: Last verification timestamp for draft model file.

    Returns:
        Updated model dict, or None if not found.

    Raises:
        ModelError: If update fails.
    """
    from db.sqlite import pool
    try:
        with pool(db_path) as conn:
            # Check which new columns exist (migration 026+)
            table_info = conn.execute("PRAGMA table_info(engine_models)").fetchall()
            has_model_params = any(r[1] == "model_params" for r in table_info)
            has_sha256 = any(r[1] == "sha256_model" for r in table_info)

            fields = []
            values = []
            field_pairs = [
                ("name", name), ("model_path", model_path),
                ("mmproj_path", mmproj_path), ("draft_model_path", draft_model_path),
                ("size_bytes", size_bytes), ("last_modified", last_modified),
                ("host_id", host_id), ("quantization", quantization),
            ]
            if is_active is not None:
                field_pairs.append(("is_active", int(is_active)))
            if has_model_params:
                # Serialize dict to JSON string for TEXT column storage
                if isinstance(model_params, dict):
                    model_params = json.dumps(model_params)
                field_pairs.append(("model_params", model_params))
            if has_sha256:
                field_pairs.extend([
                    ("sha256_model", sha256_model),
                    ("sha256_mmproj", sha256_mmproj),
                    ("sha256_draft", sha256_draft),
                    ("sha256_verified_at_model", sha256_verified_at_model),
                    ("sha256_verified_at_mmproj", sha256_verified_at_mmproj),
                    ("sha256_verified_at_draft", sha256_verified_at_draft),
                ])
            for field, val in field_pairs:
                # Convert empty string to explicit NULL for clearable fields
                if field in ("mmproj_path", "draft_model_path", "quantization") and val == "":
                    fields.append(field)
                    values.append(None)  # Set NULL (clear the field)
                elif val is not None:
                    fields.append(field)
                    values.append(val)
            if not fields:
                # Nothing to update — return current state
                row = conn.execute(
                    "SELECT * FROM engine_models WHERE id = ?", (model_id,)
                ).fetchone()
                return _row_to_dict(row)
            set_clause = ", ".join(f"{f} = ?" for f in fields)
            values.append(model_id)
            conn.execute(
                f"UPDATE engine_models SET {set_clause} WHERE id = ?", values
            )
            row = conn.execute(
                "SELECT * FROM engine_models WHERE id = ?", (model_id,)
            ).fetchone()
            return _row_to_dict(row)
    except Exception as exc:
        raise ModelError(f"Failed to update model {model_id}: {exc}") from exc


def get_model(db_path, model_id):
    """Fetch a single model by its id.

    Args:
        db_path: Path to the SQLite database.
        model_id: Integer primary key.

    Returns:
        dict with model data, or None if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_models WHERE id = ?", (model_id,)
        ).fetchone()
        return _row_to_dict(row)


def list_models(db_path, engine_type_id=None, host_id=None):
    """List models with optional filters.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Filter by engine type.
        host_id: Filter by host node.

    Returns:
        list of dicts representing models.
    """
    from db.sqlite import pool
    query = "SELECT * FROM engine_models"
    params = []

    if engine_type_id is not None:
        query += " WHERE engine_type_id = ?"
        params.append(engine_type_id)
    if host_id is not None:
        query += " AND host_id = ?"
        params.append(host_id)
    query += " ORDER BY name"

    with pool(db_path) as conn:
        cursor = conn.execute(query, params)
        return [_row_to_dict(r) for r in cursor.fetchall()]


def update_model_discovered(db_path, model_id, discovered):
    """Mark a model as newly discovered or stale.

    Args:
        db_path: Path to the SQLite database.
        model_id: Integer primary key.
        discovered: 1 (newly discovered) or 0 (stale).

    Returns:
        Updated model dict, or None if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        conn.execute(
            "UPDATE engine_models SET discovered = ? WHERE id = ?",
            (discovered, model_id),
        )
        row = conn.execute(
            "SELECT * FROM engine_models WHERE id = ?", (model_id,)
        ).fetchone()
        return _row_to_dict(row)


def delete_model(db_path, model_id):
    """Remove a model entry by id.

    Args:
        db_path: Path to the SQLite database.
        model_id: Integer primary key.

    Returns:
        True if deleted, False if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM engine_models WHERE id = ?", (model_id,)
        )
        return cursor.rowcount > 0


def scan_host_for_models(db_path, host_id, engine_type_id):
    """Placeholder for host model scanning logic.

    In Phase 1 this is a no-op stub that returns the current list.
    Future phases will run Ansible discovery and populate results.

    Args:
        db_path: Path to the SQLite database.
        host_id: Integer primary key of the node to scan.
        engine_type_id: Filter models for this engine type.

    Returns:
        list of dicts for existing models on that host.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM engine_models WHERE host_id = ? AND engine_type_id = ?",
            (host_id, engine_type_id),
        )
        return [_row_to_dict(r) for r in cursor.fetchall()]
