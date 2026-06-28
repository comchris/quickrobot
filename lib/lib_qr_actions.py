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

"""Quickrobot Actions Log — records framework-level operations.

Unlike ansible_actions (which logs playbook executions), qr_actions
logs decisions made by the quickrobot API itself: node CRUD, instance
state transitions, auto-registration events, etc.

Functions:
    log_qr_action(db_path, action_type, node_id, instance_id, actor, details)
"""

import json


def log_qr_action(db_path, action_type, node_id=None, instance_id=None,
                      actor="api", details=None):
    """Log a quickrobot framework action to the database.

    Args:
        db_path: Path to the SQLite database.
        action_type: String identifying the action (e.g. 'node_delete',
                     'instance_create', 'system_managed_provision').
        node_id: Optional FK to nodes table.
        instance_id: Optional FK to instances table.
        actor: Who triggered this action — 'api', 'auto_register', or 'system'.
        details: Dict of context data (will be JSON-encoded).

    Returns:
        True if logged successfully, False otherwise.
    """
    from db.sqlite import pool

    from lib.lib_time import utcnow_str
    details_json = json.dumps(details or {})
    created_at = utcnow_str()
    try:
        with pool(db_path) as conn:
            conn.execute(
                """INSERT INTO qr_actions
                   (action_type, node_id, instance_id, actor, details, created_at, override)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                 (action_type, node_id, instance_id, actor, details_json, created_at),
            )
        return True
    except Exception:
        return False


def log_qr_override(db_path, action_type, node_id=None, instance_id=None,
                        actor="api", details=None):
    """Log a quickrobot action marked as an OVERRIDE requiring user verification.

    The override flag (INTEGER=1) signals to agents that this action bypassed
    normal guards and requires explicit user confirmation. A WARNING prefix is
    added to the details JSON for visual prominence in logs.

    Args:
        db_path: Path to the SQLite database.
        action_type: String identifying the action (e.g. 'node_delete_override').
        node_id: Optional FK to nodes table.
        instance_id: Optional FK to instances table.
        actor: Who triggered this action — 'api', 'auto_register', or 'system'.
        details: Dict of context data (WARNING prefix added automatically).

    Returns:
        True if logged successfully, False otherwise.
    """
    from db.sqlite import pool
    from lib.lib_time import utcnow_str

    details = details or {}
    details["__warning__"] = "OVERRIDE: This action bypassed normal guards. Verify with user before proceeding."
    details_json = json.dumps(details)
    created_at = utcnow_str()
    try:
        with pool(db_path) as conn:
            conn.execute(
                """INSERT INTO qr_actions
                   (action_type, node_id, instance_id, actor, details, created_at, override)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                 (action_type, node_id, instance_id, actor, details_json, created_at),
            )
        return True
    except Exception:
        return False


def log_qr_task(db_path, action_type, node_id=None, instance_id=None,
                playbook_registry_id=None, actor="api", extra_details=None):
    """Log a framework task in 'running' state for real-time visibility.

    Creates a qr_actions record with status='running' and started_at.
    Callers should use update_qr_task() later to mark it completed/failed.

    Args:
        db_path: Path to the SQLite database.
        action_type: String identifying the task (e.g., 'deploy_instance').
        node_id: Optional FK to nodes table.
        instance_id: Optional FK to instances table.
        playbook_registry_id: Optional FK to playbook_registry (for audit trail).
        extra_details: Optional dict of context data (will be JSON-encoded).

    Returns:
        int — the new qr_actions record id, or 0 on failure.
    """
    from db.sqlite import pool
    from datetime import datetime, timezone

    details = extra_details or {}
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with pool(db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO qr_actions
                   (action_type, node_id, instance_id, actor, details,
                    created_at, started_at, status, override)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', 0)""",
                (action_type, node_id, instance_id, actor,
                 json.dumps(details), now, now),
            )
            task_id = cursor.lastrowid
            if playbook_registry_id is not None:
                conn.execute(
                    "UPDATE qr_actions SET playbook_registry_id = ? WHERE id = ?",
                    (playbook_registry_id, task_id),
                )
            conn.commit()
        return task_id
    except Exception:
        return 0


def update_qr_task(db_path, task_id, status, duration_ms=0, finished_at=None):
    """Update a running task's status and timing.

    Args:
        db_path: Path to the SQLite database.
        task_id: The qr_actions record id (from log_qr_task).
        status: New status — 'completed', 'failed', 'timeout', or 'stuck'.
        duration_ms: Task duration in milliseconds.
        finished_at: ISO timestamp for finish time (defaults to now).

    Returns:
        True if updated successfully, False otherwise.
    """
    from db.sqlite import pool
    from datetime import datetime, timezone

    if finished_at is None:
        finished_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with pool(db_path) as conn:
            conn.execute(
                """UPDATE qr_actions
                   SET status = ?, finished_at = ?, duration_ms = ?
                   WHERE id = ?""",
                (status, finished_at, duration_ms, task_id),
            )
            conn.commit()
        return True
    except Exception:
        return False
