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

"""quickrobot — Instance logs adapters.

Functions: log_instance_action, get_instance_logs_paginated,
           cleanup_old_instance_logs, get_action_history.
All functions accept db_path as first positional argument.
"""

import json


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def log_instance_action(db_path, instance_id, action, status, detail=None,
                        duration_ms=None):
    """Append a log entry to the instance_logs table.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Foreign key to instances table.
        action: One of the allowed action types (create, start, stop, etc.).
        status: 'received', 'processing', 'success', or 'failed'.
        detail: Optional dict of action details (will be JSON-encoded).
        duration_ms: Optional duration in milliseconds.

    Returns:
        int — the new log entry id.
    """
    from db.sqlite import pool
    detail_json = json.dumps(detail or {})
    with pool(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO instance_logs
               (instance_id, action, status, detail, duration_ms)
               VALUES (?, ?, ?, ?, ?)""",
            (instance_id, action, status, detail_json, duration_ms),
        )
        return cursor.lastrowid


def get_instance_logs_paginated(db_path, instance_id, limit=50, offset=0):
    """Get paginated action logs for an instance.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Foreign key to instances table.
        limit: Max rows to return (default 50).
        offset: Number of rows to skip (default 0).

    Returns:
        dict with {total, limit, offset, items}.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM instance_logs WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()["cnt"]

        cursor = conn.execute(
            """SELECT * FROM instance_logs
               WHERE instance_id = ?
               ORDER BY created_at DESC
               LIMIT ? OFFSET ?""",
            (instance_id, limit, offset),
        )
        items = []
        for row in cursor.fetchall():
            d = _row_to_dict(row)
            if d.get("detail"):
                try:
                    d["detail"] = json.loads(d["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass
            items.append(d)

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }


def cleanup_old_instance_logs(db_path, days=30):
    """Remove logs older than the specified number of days.

    Args:
        db_path: Path to the SQLite database.
        days: Keep logs from the last N days (default 30).

    Returns:
        int — number of rows deleted.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM instance_logs WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        return cursor.rowcount


def get_action_history(db_path, instance_id, action_type=None, limit=25):
    """Get filtered action history for an instance.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Foreign key to instances table.
        action_type: Optional filter by specific action type.
        limit: Max rows to return (default 25).

    Returns:
        list of dicts with log entries.
    """
    from db.sqlite import pool
    if action_type:
        with pool(db_path) as conn:
            cursor = conn.execute(
                """SELECT * FROM instance_logs
                   WHERE instance_id = ? AND action = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (instance_id, action_type, limit),
            )
    else:
        with pool(db_path) as conn:
            cursor = conn.execute(
                """SELECT * FROM instance_logs
                   WHERE instance_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (instance_id, limit),
            )

    results = []
    for row in cursor.fetchall():
        d = _row_to_dict(row)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(d)
    return results


def cleanup_null_log_entries(db_path):
    """Remove orphaned log entries with NULL FK references.

    After migration 010 changed FK constraints from ON DELETE CASCADE to
    ON DELETE SET NULL, deleted nodes/instances leave behind log rows
    with NULL node_id or instance_id. This function removes those
    orphaned entries on demand.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        dict with {instance_logs_deleted, ansible_actions_deleted}.
    """
    from db.sqlite import pool
    deleted = {"instance_logs_deleted": 0, "ansible_actions_deleted": 0}

    with pool(db_path) as conn:
        # instance_logs: rows where FK was SET NULL after instance deletion
        cursor = conn.execute(
            "DELETE FROM instance_logs WHERE instance_id IS NULL"
        )
        deleted["instance_logs_deleted"] = cursor.rowcount

        # ansible_actions: rows where node_id or instance_id is NULL
        # (parent node or instance was deleted, logs preserved with FK SET NULL)
        cursor = conn.execute(
            "DELETE FROM ansible_actions WHERE node_id IS NULL OR instance_id IS NULL"
        )
        deleted["ansible_actions_deleted"] = cursor.rowcount

    return deleted
