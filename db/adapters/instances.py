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

"""quickrobot — Instance CRUD + state transitions + config merge.

Functions: create_instance, get_instance, list_instances, update_instance,
           transition_state, delete_instance, merge_configs, assign_port,
           log_action, get_instance_logs, cleanup_old_logs.
All functions accept db_path as first positional argument.
"""

import json
from datetime import datetime as _dt, timezone as _tz

from lib.qr_engine_ids import get_id_by_name, QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME, QR_ENGINE_PORT_DEFAULTS
from lib.lib_config_merge import _parse_config_override as _pcov


class InstanceError(Exception):
    """Raised on instance-specific errors."""


# Valid state transitions: {source_state: [allowed_target_states]}
# This is the base machine — engines extend it via super().get_state_machine()
VALID_TRANSITIONS = {
    "unconfigured": ["configuring", "stopped", "deployed", "stopping"],
   "configuring": ["deploying", "build_error", "unconfigured", "stopping"],
    "deploying": ["deployed", "build_error", "error", "unconfigured", "stopping"],
    "build_error": ["configuring", "error", "unconfigured", "starting", "running", "updating", "stopping"],
    "deployed": ["starting", "running", "stopped", "error", "unconfigured", "updating", "build_error", "compiling", "stopping"],
    "starting": ["running", "error", "timeout", "stopping"],
    "running": ["stopping", "error", "test_mode", "updating", "compiling"],
    "stopping": ["stopped", "running", "starting", "deployed", "configuring", "error", "timeout"],
    "stopped": ["starting", "running", "configuring", "stopping", "error", "test_mode", "unconfigured", "compiling", "updating"],
    "error": ["unconfigured", "configuring", "deploying", "starting", "stopping", "updating", "build_error", "compiling", "running"],
    "timeout": ["error", "stopping"],
    "test_mode": ["running", "stopped", "error", "stopping"],
    "updating": ["running", "deployed", "build_error", "error", "timeout", "unconfigured", "stopping"],
    "compiling": ["deployed", "error", "timeout", "stopping"],
}


def get_engine_state_transitions(engine_type_name):
    """Get the merged state machine for a specific engine type.

    Merges base VALID_TRANSITIONS with engine-specific overrides from the
    engine's get_state_machine() method. Engine subclasses that call
    super().get_state_machine() and extend the result will be handled correctly.

    Args:
        engine_type_name: Engine type name string (e.g., 'llama_server', 'rpc').

    Returns:
        Dict mapping source_state -> [allowed_target_states].
        Falls back to base VALID_TRANSITIONS if engine not found.
    """
    try:
        from engine import get_engine as _get_engine
        eng_inst = _get_engine(engine_type_name)
        if eng_inst and hasattr(eng_inst, "get_state_machine"):
            return eng_inst.get_state_machine()
    except Exception:
        pass
    # Fallback to base
    return dict(VALID_TRANSITIONS)


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# RPC binding resolution helpers
# ---------------------------------------------------------------------------


def _resolve_rpc_bindings(db_path, bind_ids):
    """Resolve RPC instance IDs to full instance metadata.

    Args:
        db_path: Path to the SQLite database.
        bind_ids: List of RPC instance IDs (from rpc_bind_ids column).

    Returns:
        list of dicts with id, name, node_hostname, port_assigned, split for each RPC.
    """
    if not bind_ids:
        return []
    # Handle both raw JSON string and already-parsed list
    if isinstance(bind_ids, str):
        try:
            bind_ids = json.loads(bind_ids)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(bind_ids, list):
        return []
    from db.sqlite import pool
    try:
        rpc_list = []
        for rid in bind_ids:
            with pool(db_path) as conn:
                row = conn.execute(
                     "SELECT id, name, node_id, port_assigned, split, experts, draft "
                     "FROM instances WHERE id = ?", (rid,)
                 ).fetchone()
                if row:
                    rpc_dict = _row_to_dict(row)
                    # Also get node hostname
                    node_row = conn.execute(
                        "SELECT hostname FROM nodes WHERE id = ?", (row["node_id"],)
                    ).fetchone()
                    if node_row:
                        rpc_dict["node_hostname"] = node_row["hostname"]
                    else:
                        rpc_dict["node_hostname"] = ""
                    rpc_list.append(rpc_dict)
        return rpc_list
    except Exception:
        return []


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def create_instance(db_path, name, engine_type_id, node_id, preset_id=None,
                    config_override=None, system_managed=0, start_on_boot=None,
                    start_after_deploy=0, gpu_device=None):
    """Create a new instance entry.

    Args:
        db_path: Path to the SQLite database.
        name: Human-readable instance name (unique with node_id).
        engine_type_id: Foreign key to engine_types table.
        node_id: Foreign key to nodes table.
        preset_id: Optional foreign key to engine_presets table.
        config_override: dict of per-instance parameter overrides.
        system_managed: Flag for system-managed instances (default 0).
        start_on_boot: Enable systemd unit on boot (default True).
        start_after_deploy: Start service immediately after deploy (default 0/False).
        gpu_device: GPU device string (e.g., "Vulkan0", "CPU").

    Returns:
        dict with the new instance's data.

    Raises:
        InstanceError: If creation fails or constraints violated.
    """
    from db.sqlite import pool
    try:
        # Only serialize if not already a string (prevents double-encoding)
        co_raw = config_override or {}
        co_json = co_raw if isinstance(co_raw, str) else json.dumps(co_raw)
        # Normalize start_on_boot: accept bool/int/string → store as "true"/"false"
        if isinstance(start_on_boot, bool):
            start_on_boot_val = "true" if start_on_boot else "false"
        elif isinstance(start_on_boot, str):
            start_on_boot_val = "true" if start_on_boot.lower() in ("true", "1", "yes") else "false"
        else:
            start_on_boot_val = 1 if start_on_boot else 0
        start_after_deploy_val = 1 if start_after_deploy else 0
        with pool(db_path) as conn:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            # Assign explicit ID for non-system instances: ensure ID >= 100
            if system_managed == 0:
                max_id_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM instances").fetchone()
                max_existing = max_id_row[0] if max_id_row else 0
                inst_id = max(max_existing, 99) + 1
                cursor = conn.execute(
                    """INSERT INTO instances
                       (id, name, engine_type_id, node_id, preset_id, config_override,
                        system_managed, start_on_boot, start_after_deploy, gpu_device,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (inst_id, name, engine_type_id, node_id, preset_id, co_json,
                     system_managed, start_on_boot_val, start_after_deploy_val, gpu_device,
                     now),
                )
            else:
                cursor = conn.execute(
                    """INSERT INTO instances
                       (name, engine_type_id, node_id, preset_id, config_override,
                        system_managed, start_on_boot, start_after_deploy, gpu_device,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, engine_type_id, node_id, preset_id, co_json,
                     system_managed, start_on_boot_val, start_after_deploy_val, gpu_device,
                     now),
                )
                inst_id = cursor.lastrowid
            row = conn.execute(
                "SELECT * FROM instances WHERE id = ?", (inst_id,)
            ).fetchone()
            result = _row_to_dict(row)
            result["config_override"] = _pcov(result.get("config_override") or "{}")
            return result
    except Exception as exc:
        raise InstanceError(f"Failed to create instance '{name}': {exc}") from exc


def get_instance(db_path, instance_id):
    """Fetch a single instance by its id with joined metadata.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key.

    Returns:
        dict with instance data plus engine_type_name, node_name, preset_name.
        Returns None if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            """SELECT i.*,
                      et.name as engine_type_name,
                      et.display_name as engine_display_name,
                      n.name as node_display_name,
                      n.hostname as node_display_hostname,
                      ep.name as preset_name
               FROM instances i
               LEFT JOIN engine_types et ON i.engine_type_id = et.id
               LEFT JOIN nodes n ON i.node_id = n.id
               LEFT JOIN engine_presets ep ON i.preset_id = ep.id
               WHERE i.id = ?""",
            (instance_id,),
        ).fetchone()
        if row is None:
            return None
        result = _row_to_dict(row)
        # Fix SQLite Row factory duplicate column name shadowing (same issue as list_instances)
        if result.get("node_display_name"):
            result["node_name"] = result.pop("node_display_name")
        if result.get("node_display_hostname"):
            result["node_hostname"] = result.pop("node_display_hostname")
        # Parse config_override — handle both single and double-encoded JSON strings
        co_raw = result.get("config_override") or "{}"
        result["config_override"] = _pcov(co_raw)
        result["ansible_vars"] = json.loads(result.get("ansible_vars") or "{}")
        # Resolve RPC bindings for llama_server instances
        if result.get("engine_type_name") == QR_ENGINE_LLAMA_SERVER_NAME:
            result["rpc_instances"] = _resolve_rpc_bindings(db_path, result.get("rpc_bind_ids"))
        return result



def list_instances(db_path, engine_type_id=None, node_id=None, state=None, orphan=None):
    """List instances with optional filters.

    Args:
        db_path: Path to the SQLite database.
        engine_type_id: Filter by engine type.
        node_id: Filter by node.
        state: Filter by instance state.
        orphan: If True, only return instances whose node_id references a deleted/missing node.

    Returns:
        list of dicts representing instances.
    """
    from db.sqlite import pool
    query = """SELECT i.*, et.name as engine_type_name, et.display_name as engine_display_name,
               n.name as node_display_name, n.hostname as node_display_hostname,
               ep.name as preset_name
                FROM instances i
                LEFT JOIN engine_types et ON i.engine_type_id = et.id
                LEFT JOIN nodes n ON i.node_id = n.id
                LEFT JOIN engine_presets ep ON i.preset_id = ep.id"""
    params = []
    conditions = []

    if engine_type_id is not None:
        conditions.append("i.engine_type_id = ?")
        params.append(engine_type_id)
    if node_id is not None:
        conditions.append("i.node_id = ?")
        params.append(node_id)
    if state is not None:
        conditions.append("i.state = ?")
        params.append(state)
    if orphan is True:
        # Orphaned: instance has node_id but node record was deleted
        conditions.append("n.id IS NULL AND i.node_id IS NOT NULL")
        conditions.append("i.state = ?")
        params.append(state)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY i.name"

    with pool(db_path) as conn:
        cursor = conn.execute(query, params)
        results = []
        for row in cursor.fetchall():
            d = _row_to_dict(row)
            # SQLite Row factory returns first occurrence of duplicate column names.
            # i.node_name/i.node_hostname from instances table (NULL) shadows n.name/n.hostname aliases.
            # Rename the joined aliases back to expected field names.
            if d.get("node_display_name"):
                d["node_name"] = d.pop("node_display_name")
            if d.get("node_display_hostname"):
                d["node_hostname"] = d.pop("node_display_hostname")
            d["config_override"] = _pcov(d.get("config_override") or "{}")
            d["ansible_vars"] = json.loads(d.get("ansible_vars") or "{}")
            # Parse rpc_bind_ids for cluster binding (both list and get need this)
            try:
                raw = d.get("rpc_bind_ids")
                if isinstance(raw, str):
                    parsed = json.loads(raw) if raw.strip() else []
                    d["rpc_bind_ids"] = parsed if isinstance(parsed, list) else []
                elif isinstance(raw, list):
                    d["rpc_bind_ids"] = raw
                else:
                    d["rpc_bind_ids"] = []
            except (json.JSONDecodeError, TypeError):
                d["rpc_bind_ids"] = []
            # Add rpc_bind_count for llama_server instances
            if d.get("engine_type_name") == QR_ENGINE_LLAMA_SERVER_NAME:
                d["rpc_bind_count"] = len(d["rpc_bind_ids"]) if isinstance(d["rpc_bind_ids"], list) else 0
            else:
                d["rpc_bind_count"] = 0
            results.append(d)
        return results


def list_instances_by_preset(db_path, preset_id):
    """List instances that use a specific preset.

    Args:
        db_path: Path to the SQLite database.
        preset_id: Integer primary key of the preset.

    Returns:
        list of dicts with instance data plus engine_type_name, node_name.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            """SELECT i.*, et.name as engine_type_name, n.name as node_display_name
               FROM instances i
               LEFT JOIN engine_types et ON i.engine_type_id = et.id
               LEFT JOIN nodes n ON i.node_id = n.id
               WHERE i.preset_id = ?
               ORDER BY i.name""",
            (preset_id,),
        )
        results = []
        for row in cursor.fetchall():
            d = _row_to_dict(row)
            if d.get("node_display_name"):
                d["node_name"] = d.pop("node_display_name")
            d["config_override"] = _pcov(d.get("config_override") or "{}")
            d["ansible_vars"] = json.loads(d.get("ansible_vars") or "{}")
            # Parse rpc_bind_ids for cluster binding (same as list_instances)
            try:
                raw = d.get("rpc_bind_ids")
                if isinstance(raw, str):
                    parsed = json.loads(raw) if raw.strip() else []
                    d["rpc_bind_ids"] = parsed if isinstance(parsed, list) else []
                elif isinstance(raw, list):
                    d["rpc_bind_ids"] = raw
                else:
                    d["rpc_bind_ids"] = []
            except (json.JSONDecodeError, TypeError):
                d["rpc_bind_ids"] = []
            # Add rpc_bind_count for llama_server instances
            if d.get("engine_type_name") == QR_ENGINE_LLAMA_SERVER_NAME:
                d["rpc_bind_count"] = len(d["rpc_bind_ids"]) if isinstance(d["rpc_bind_ids"], list) else 0
            else:
                d["rpc_bind_count"] = 0
            results.append(d)
        return results


def update_instance(db_path, instance_id, **fields):
    """Update instance fields by id.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key.
        **fields: Key-value pairs to update.

    Returns:
        Updated instance dict, or None if not found.

    Raises:
        InstanceError: If not found or no valid fields.
    """
    from db.sqlite import pool
    allowed = {"name", "preset_id", "config_override", "port_override",
               "transport", "ansible_playbook", "ansible_vars",
               "ansible_extra_args", "state", "system_managed", "rss_bytes",
               "port_assigned", "pid_last_known", "uptime_seconds",
               "start_on_boot", "restart_policy", "gpu_device", "instance_uuid", "last_state_change",
               "rpc_bind_ids", "split_mode", "tensor_split", "split",
                "experts", "draft", "cli_flags"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        raise InstanceError("No valid fields to update")

    if "config_override" in updates:
        # Only serialize if not already a string (prevents double-encoding)
        co = updates["config_override"]
        if isinstance(co, str):
            updates["config_override"] = co  # Already JSON-encoded from client
        else:
            updates["config_override"] = json.dumps(co)
    # Normalize start_on_boot: accept bool/int/string → store as "true"/"false"
    if "start_on_boot" in updates:
        sob = updates["start_on_boot"]
        if isinstance(sob, bool):
            updates["start_on_boot"] = "true" if sob else "false"
        elif isinstance(sob, str):
            updates["start_on_boot"] = "true" if sob.lower() in ("true", "1", "yes") else "false"
    if "ansible_vars" in updates:
        updates["ansible_vars"] = json.dumps(updates["ansible_vars"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [instance_id]

    with pool(db_path) as conn:
        conn.execute(
            f"UPDATE instances SET {set_clause}, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
            values,
        )
        row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        if row is None:
            raise InstanceError(f"Instance {instance_id} not found")
        result = _row_to_dict(row)
        result["config_override"] = _pcov(result.get("config_override") or "{}")
        result["ansible_vars"] = json.loads(result.get("ansible_vars") or "{}")
        return result


def check_system_managed(db_path, instance_id):
    """Check if an instance is system-managed (protected from normal delete).

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key.

    Returns:
        True if the instance has system_managed=1.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT system_managed FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        return row is not None and row["system_managed"] == 1


def delete_instance(db_path, instance_id):
    """Delete an instance by id with cascading cleanup.

    If the instance is running or stopping, stop it first.
    Also cleans up benchmark_results, instance_logs, and ansible_actions
    that reference this instance (mimics ON DELETE CASCADE).

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key.

    Returns:
        True if deleted, False if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            return False

        state = row["state"]
        if state in ("running", "stopping"):
            conn.execute(
                "UPDATE instances SET state = 'stopped', pid_last_known = NULL, "
                "uptime_seconds = 0, last_state_change = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id = ?", (instance_id,)
            )

        # Cascading delete of related records (mimics ON DELETE CASCADE)
        conn.execute("DELETE FROM benchmark_results WHERE instance_id = ?", (instance_id,))
        conn.execute("DELETE FROM instance_logs WHERE instance_id = ?", (instance_id,))
        conn.execute("DELETE FROM ansible_actions WHERE instance_id = ?", (instance_id,))

        conn.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
        return True


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def transition_state(db_path, instance_id, new_state):
    """Transition an instance to a new state with validation.

    Uses engine-specific merged state machines (via get_engine_state_transitions)
    when available. Falls back to base VALID_TRANSITIONS for unknown engines.
    Each engine's get_state_machine() should call super().get_state_machine()
    and extend the returned dict for proper merging.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key.
        new_state: Target state string.

    Returns:
        Updated instance dict, or None if not found.

    Raises:
        InstanceError: If transition is invalid (409 INVALID_STATE).
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            return None

        current_state = row["state"]
        engine_type_name = row["engine_type_name"] if "engine_type_name" in row.keys() else ""
        # If no engine_type_name, look it up via engine_type_id
        if not engine_type_name and "engine_type_id" in row.keys():
            try:
                et_row = conn.execute(
                    "SELECT name FROM engine_types WHERE id = ?", (row["engine_type_id"],)
                ).fetchone()
                if et_row:
                    engine_type_name = et_row["name"]
            except Exception:
                pass

        # Resolve per-engine state machine via merge utility
        engine_sm = get_engine_state_transitions(engine_type_name) if engine_type_name else None
        allowed_targets = (engine_sm or VALID_TRANSITIONS).get(current_state, [])

        if new_state not in allowed_targets:
            raise InstanceError(
                f"Invalid state transition from '{current_state}' to '{new_state}' "
                f"(allowed: {allowed_targets})"
            )

        ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE instances SET state = ?, last_state_change = ? WHERE id = ?",
            (new_state, ts, instance_id),
        )
        # Reset node_build_state when instance transitions to error
        if new_state == "error":
            try:
                node_id = row["node_id"] if "node_id" in row.keys() else None
                if node_id is not None:
                    conn.execute(
                        "UPDATE nodes SET node_build_state = 'idle', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (node_id,),
                    )
            except Exception:
                pass  # Non-critical — build state reset failure doesn't block transition
        # Read back the updated row on the same connection to avoid
        # stale reads from WAL mode (a second connection won't see the
        # uncommitted transaction yet).
        result = _row_to_dict(conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone())
        return result


# ---------------------------------------------------------------------------
# Config merge — delegates to lib_config_merge.build_merged_config()
# ---------------------------------------------------------------------------

def merge_configs(db_path, instance_id):
    """Perform the 6-layer config merge for an instance via canonical merge.

    Delegates to lib_config_merge.build_merged_config() which handles:
        Layer 1: Engine default configs (engine_configs table)
        Layer 2: Preset config template (engine_presets.config_template)
        Layer 3: Model definition params (engine_models.model_params)
        Layer 4: Cluster binding (llama_server only, optional)
        Layer 5: Instance override (instances.config_override — FINAL)
        Layer 6: Metadata injection (restart_policy, start_on_boot)

    Wrapper adds caller-specific metadata: default_timeout, rpc_bind_ids (llama_server).

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key of the instance.

    Returns:
        dict with keys "env", "cli_opts", "model", plus metadata keys
        "restart_policy", "start_on_boot", "default_timeout", "rpc_bind_ids".

    Raises:
        InstanceError: If instance not found.
    """
    from db.sqlite import pool
    from lib.lib_config_merge import build_merged_config as _canonical_merge

    with pool(db_path) as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if inst is None:
            raise InstanceError(f"Instance {instance_id} not found")
        inst = dict(inst)

        # Call canonical merge — handles all 6 layers + model path resolution
        merged = _canonical_merge(db_path, instance_id, node_id=inst.get("node_id"))

    # Extract default_timeout from override chain (engine → preset → instance)
    default_timeout = None
    with pool(db_path) as conn:
        # Layer 1: Engine configs
        for row in conn.execute(
            "SELECT value FROM engine_configs WHERE engine_type_id = ? AND key = 'default_timeout'",
            (inst["engine_type_id"],),
        ).fetchall():
            try:
                default_timeout = int(row[0])
            except (ValueError, TypeError):
                pass

        # Layer 2: Node configs (if node_configs table exists)
        has_node_configs = any(
            r[0] == "node_configs"
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
        if has_node_configs and inst.get("node_id"):
            for row in conn.execute(
                "SELECT value FROM node_configs WHERE node_id = ? AND engine_type_id = ? AND key = 'default_timeout'",
                (inst["node_id"], inst["engine_type_id"]),
            ).fetchall():
                try:
                    default_timeout = int(row[0])
                except (ValueError, TypeError):
                    pass

        # Layer 3: Preset default_timeout
        if inst.get("preset_id"):
            preset_row = conn.execute(
                "SELECT config_template FROM engine_presets WHERE id = ?",
                (inst["preset_id"],),
            ).fetchone()
            if preset_row and preset_row[0]:
                preset_raw = json.loads(preset_row[0])
                if isinstance(preset_raw, dict) and "default_timeout" in preset_raw:
                    try:
                        default_timeout = int(preset_raw["default_timeout"])
                    except (ValueError, TypeError):
                        pass

        # Layer 5: Instance override default_timeout
        if inst["config_override"]:
            override_raw = _pcov(inst["config_override"])
            if isinstance(override_raw, dict) and "default_timeout" in override_raw:
                try:
                    default_timeout = int(override_raw["default_timeout"])
                except (ValueError, TypeError):
                    pass

        # Apply fallback to engine_config if nothing set
        if default_timeout is None:
            for row in conn.execute(
                "SELECT value FROM engine_configs WHERE engine_type_id = ? AND key = 'default_timeout'",
                (inst["engine_type_id"],),
            ).fetchall():
                try:
                    default_timeout = int(row[0])
                except (ValueError, TypeError):
                    pass

        merged["default_timeout"] = default_timeout

        # Add cluster binding fields for llama_server instances
        engine_name = ""
        et_row = conn.execute(
            "SELECT name FROM engine_types WHERE id = ?", (inst["engine_type_id"],)
        ).fetchone()
        if et_row:
            engine_name = et_row["name"]

        if engine_name == QR_ENGINE_LLAMA_SERVER_NAME:
            sm_val = inst.get("split_mode")
            if sm_val:
                merged["env"]["LLAMA_ARG_SPLIT_MODE"] = sm_val
            rbi = inst.get("rpc_bind_ids")
            if rbi:
                try:
                    merged["rpc_bind_ids"] = json.loads(rbi) if isinstance(rbi, str) else rbi
                except (json.JSONDecodeError, TypeError):
                    merged["rpc_bind_ids"] = []
            else:
                merged["rpc_bind_ids"] = []

    return merged


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

def assign_port(db_path, node_id, engine_type_id=None, exclude_instance_id=None,
                port_override=None):
    """Find an available port for a new instance on the given node.

    If port_override is a non-zero integer, use that exact port (still
    check for conflicts and return 409 CONFLICT if occupied).
    If port_override is null or 0, auto-allocate starting from the
    engine's base_port + slot_index strategy.

    Args:
        db_path: Path to the SQLite database.
        node_id: Target node id.
        engine_type_id: Engine type to determine base_port from configs.
        exclude_instance_id: Instance id to exclude from conflict check.
        port_override: Optional explicit port (0 or None = auto-allocate).

    Returns:
        int — the assigned port number.

    Raises:
        InstanceError: If all ports exhausted or override port in use.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        # Get existing ports on this node
        query = "SELECT port_assigned FROM instances WHERE node_id = ? AND port_assigned IS NOT NULL"
        params = [node_id]
        if exclude_instance_id is not None:
            query += " AND id != ?"
            params.append(exclude_instance_id)

        cursor = conn.execute(query, params)
        occupied = {row["port_assigned"] for row in cursor.fetchall()}

        if port_override and port_override > 0:
            if port_override in occupied:
                raise InstanceError(
                    f"Port {port_override} already assigned on node {node_id}"
                )
            return port_override

        # Auto-allocate: get base port from engine_configs + SSOT lookup
        if engine_type_id is not None:
            # Look up engine name from ID for SSOT port lookup
            eng_row = conn.execute("SELECT name FROM engine_types WHERE id = ?", (engine_type_id,)).fetchone()
            engine_name = eng_row["name"] if eng_row else ""
            base_port = QR_ENGINE_PORT_DEFAULTS.get(engine_name, 8080) if engine_name else 8080
            bp_row = conn.execute(
                "SELECT value FROM engine_configs "
                "WHERE engine_type_id = ? AND key IN ('LLAMA_ARG_PORT', 'base_port')"
                " ORDER BY CASE key "
                "  WHEN 'LLAMA_ARG_PORT' THEN 1 WHEN 'base_port' THEN 2 END",
                (engine_type_id,),
            ).fetchone()
            if bp_row and bp_row[0]:
                try:
                    base_port = int(bp_row[0])
                except (ValueError, TypeError):
                    pass
        else:
            # Unknown engine type — default to llama_server base port as conservative fallback
            base_port = QR_ENGINE_PORT_DEFAULTS.get("llama_server", 8080)

        # Find first free port starting from base_port
        candidate = base_port
        attempts = 1000  # safety limit
        while candidate in occupied and attempts > 0:
            candidate += 1
            attempts -= 1

        if attempts <= 0:
            raise InstanceError(
                f"All ports exhausted starting from base {base_port} on node {node_id}"
            )

        return candidate


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_action(db_path, instance_id, action, status, detail=None, duration_ms=None):
    """Append a log entry to the instance_logs table.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Foreign key to instances table.
        action: One of the allowed action types.
        status: One of 'received', 'processing', 'success', 'failed'.
        detail: Optional dict of action details (will be JSON-encoded).
        duration_ms: Optional duration in milliseconds.

    Returns:
        The new log entry id.
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


def get_instance_logs(db_path, instance_id, limit=50, offset=0):
    """Get paginated action logs for an instance.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Foreign key to instances table.
        limit: Max rows to return (default 50).
        offset: Number of rows to skip (default 0).

    Returns:
        list of dicts with log entries.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            """SELECT * FROM instance_logs
               WHERE instance_id = ?
               ORDER BY created_at DESC
               LIMIT ? OFFSET ?""",
            (instance_id, limit, offset),
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


def cleanup_old_logs(db_path, days=30):
    """Remove logs older than the specified number of days.

    Args:
        db_path: Path to the SQLite database.
        days: Keep logs from the last N days (default 30).

    Returns:
        Number of rows deleted.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM instance_logs WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        return cursor.rowcount
