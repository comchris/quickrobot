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

"""quickrobot — Node CRUD adapters.

Functions: add_node, get_node, list_nodes, update_node, delete_node,
           update_capabilities, update_status, discover_node.
All functions accept db_path as first positional argument.
"""

import json

from lib.lib_constants import DEFAULT_ANSIBLE_USER


class NodeError(Exception):
    """Raised on node-specific errors."""


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def add_node(db_path, name, hostname, transport="ansible", ansible_user=None,
             ansible_port=22, ansible_key_path=None, ansible_inventory_host=None,
             model_base_path=None):
    """Create a new node entry.

    Args:
        db_path: Path to the SQLite database.
        name: Unique host identifier (e.g., 'dllama6').
        hostname: DNS name or IP for connection.
        transport: 'ansible' or 'ssh' (default 'ansible').
        ansible_user: SSH/Ansible user (defaults to DEFAULT_ANSIBLE_USER).
        ansible_port: SSH port (default 22).
        ansible_key_path: Optional path to private key.
        ansible_inventory_host: Override for Ansible inventory hostname.
        model_base_path: Default model root path for this node.

    Returns:
        dict with the new node's data including assigned id.

    Raises:
        NodeError: If node already exists or insert fails.
    """
    from db.sqlite import pool
    effective_user = ansible_user or DEFAULT_ANSIBLE_USER
    try:
        with pool(db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO nodes
                   (name, hostname, transport, ansible_user, ansible_port,
                    ansible_key_path, ansible_inventory_host, model_base_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, hostname, transport, effective_user, ansible_port,
                 ansible_key_path, ansible_inventory_host, model_base_path),
            )
            node_id = cursor.lastrowid
            row = conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return _row_to_dict(row)
    except Exception as exc:
        raise NodeError(f"Failed to add node '{name}': {exc}") from exc


def get_node(db_path, node_id):
    """Fetch a single node by its id.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.

    Returns:
        dict with node data, or None if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_dict(row)


def list_nodes(db_path):
    """Return all nodes ordered by name.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        list of dicts, each representing a node.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM nodes ORDER BY name"
        )
        return [_row_to_dict(r) for r in cursor.fetchall()]


def update_node(db_path, node_id, **fields):
    """Update node fields by id.

    Valid field names match the nodes table columns.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.
        **fields: Key-value pairs to update.

    Returns:
        dict with updated node data, or None if not found.

    Raises:
        NodeError: If node not found or update fails.
    """
    from db.sqlite import pool
    allowed = {"name", "hostname", "transport", "ansible_user",
               "ansible_port", "ansible_key_path", "ansible_inventory_host",
               "status", "status_reason", "capabilities", "available_devices",
               "cpu_cores", "ram_mb", "os", "node_build_state",
               "gpu_name", "gpu_type", "gpu_memory_mb"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        raise NodeError("No valid fields to update")

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [node_id]

    with pool(db_path) as conn:
        conn.execute(f"UPDATE nodes SET {set_clause} WHERE id = ?", values)
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise NodeError(f"Node {node_id} not found")
        return _row_to_dict(row)


def delete_node(db_path, node_id, stop_running=False):
    """Delete a node by id with optional running instance handling.

    System-managed instances are de-associated (node_id set to NULL) before
    deletion so they survive the cascade. User instances are handled normally.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.
        stop_running: If True, mark running instances as 'stopping' before deletion.

    Returns:
        True if deleted, False if not found.

    Raises:
        NodeError: If non-system instances are attached and stop_running is False.
                   Error detail includes instance list when possible.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        # Separate system-managed from user instances
        all_rows_raw = conn.execute(
            "SELECT id, name, state, system_managed FROM instances WHERE node_id = ?", (node_id,)
        ).fetchall()

        if not all_rows_raw:
            # Clean up FK-referencing rows before delete (no ON DELETE CASCADE)
            conn.execute("DELETE FROM ansible_actions WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM engine_models WHERE host_id = ?", (node_id,))
            cursor = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            return cursor.rowcount > 0

        # Convert sqlite3.Row to dict for reliable access
        all_rows = [dict(r) for r in all_rows_raw]

        user_instances = [r for r in all_rows if not r.get("system_managed", 0)]
        system_instances = [r for r in all_rows if r.get("system_managed", 0)]

        # Block deletion if non-system instances are attached and stop_running is False
        if user_instances and not stop_running:
            raise NodeError(f"Node {node_id} has {len(all_rows)} attached instance(s): "
                            + ", ".join(f"{r['name']}({r['state']})" for r in all_rows))

        # Mark running USER instances as 'stopping' before cascade delete
        running_user = [r for r in user_instances
                        if r["state"] in ("running", "deploying", "configuring", "stopping")]
        if running_user:
            running_ids = [r["id"] for r in running_user]
            placeholders = ",".join("?" * len(running_ids))
            conn.execute(
                f"UPDATE instances SET state = 'stopping', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                f"WHERE node_id = ? AND id IN ({placeholders})",
                [node_id] + running_ids,
            )

        # De-associate system-managed instances to survive node deletion
        if system_instances:
            sys_ids = [r["id"] for r in system_instances]
            placeholders = ",".join("?" * len(sys_ids))
            conn.execute(
                f"UPDATE instances SET node_id = NULL WHERE id IN ({placeholders})",
                sys_ids,
            )

        # Clean up FK-referencing rows before delete (no ON DELETE CASCADE)
        conn.execute("DELETE FROM ansible_actions WHERE node_id = ?", (node_id,))
        conn.execute("DELETE FROM engine_models WHERE host_id = ?", (node_id,))

        cursor = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        return cursor.rowcount > 0


def update_capabilities(db_path, node_id, capabilities, devices):
    """Store discovered hardware info on a node.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.
        capabilities: dict of discovered capabilities (cpu_cores, ram_mb, os).
        devices: list of GPU/TPU devices (will be JSON-encoded).

    Returns:
        Updated node dict, or None if not found.
    """
    from db.sqlite import pool
    cap_json = json.dumps(capabilities) if isinstance(capabilities, dict) else str(capabilities)
    dev_json = json.dumps(devices) if isinstance(devices, list) else str(devices)

    # Extract individual values for separate columns
    cpu_cores = capabilities.get("cpu_cores") if isinstance(capabilities, dict) else None
    ram_mb = capabilities.get("ram_mb") if isinstance(capabilities, dict) else None
    os_val = capabilities.get("os", "unknown") if isinstance(capabilities, dict) else "unknown"

    with pool(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET capabilities = ?, available_devices = ?,"
            " cpu_cores = ?, ram_mb = ?, os = ? WHERE id = ?",
            (cap_json, dev_json, cpu_cores, ram_mb, os_val, node_id),
        )
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_dict(row)


def update_status(db_path, node_id, status, reason=""):
    """Set node status and optional human-readable reason.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.
        status: One of 'active', 'inactive', 'error', 'unknown'.
        reason: Optional detail string.

    Returns:
        Updated node dict, or None if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET status = ?, status_reason = ? WHERE id = ?",
            (status, reason, node_id),
        )
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_dict(row)


def update_ping_state(db_path, node_id, ping_state):
    """Update a node's operational ping state.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.
        ping_state: One of 'online', 'offline', 'disabled'.

    Returns:
        Updated node dict, or None if not found.
    """
    from db.sqlite import pool
    if ping_state not in ("online", "offline", "disabled"):
        raise NodeError(f"Invalid ping_state: {ping_state}")
    with pool(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET ping_state = ? WHERE id = ?",
            (ping_state, node_id),
        )
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_dict(row)


def toggle_host_active(db_path, node_id, is_active):
    """Toggle a node's admin active/inactive state.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.
        is_active: 1 = active (show in filters), 0 = inactive (hide from filters).

    Returns:
        Updated node dict, or None if not found.
    """
    from db.sqlite import pool
    if is_active not in (0, 1):
        raise NodeError(f"Invalid is_active: {is_active}")
    with pool(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET is_active = ? WHERE id = ?",
            (is_active, node_id),
        )
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_dict(row)


# Backward-compatible alias for ping thread
update_host_status = update_ping_state


def list_nodes_with_host_status(db_path):
    """Return all nodes with ping_state and is_active columns included.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        list of dicts, each representing a node with ping_state + is_active fields.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM nodes ORDER BY name"
        )
        return [_row_to_dict(r) for r in cursor.fetchall()]


def discover_node(db_path, node_id):
    """Mark a node for capability discovery via Ansible.

    This is a placeholder — actual playbook execution goes through
    the engine loader and lib_ansible_runner.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.

    Returns:
        Updated node dict with status set to 'unknown'.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET status = 'unknown', status_reason = '' WHERE id = ?",
            (node_id,),
        )
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_dict(row)


def update_local_host_inventory(db_path, node_id, inventory):
    """Store localhost hardware inventory into the node record.

    This is the counterpart to gather_local_inventory() — writes the
    collected data back to the nodes table for node_id=1 (localhost).

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key (should be 1 for localhost).
        inventory: dict from gather_local_inventory().

    Returns:
        Updated node dict, or None if not found.
    """
    from db.sqlite import pool

    cpu_cores = inventory.get("cpu_cores")
    ram_mb = inventory.get("ram_mb")
    os_val = inventory.get("os", "unknown")
    gpu_name = inventory.get("gpu_name")
    gpu_type = inventory.get("gpu_type")
    gpu_mem = inventory.get("gpu_memory_mb")
    fs_free = inventory.get("fs_free_gb")
    devices = inventory.get("available_devices", [])

    # Build capabilities JSON (matches validate.yml output format)
    capabilities = {
        "cpu_cores": cpu_cores,
        "ram_mb": ram_mb,
        "os": os_val,
        "gpu_name": gpu_name,
        "gpu_type": gpu_type,
        "gpu_memory_mb": gpu_mem,
        "fs_free_gb": fs_free,
    }
    cap_json = json.dumps(capabilities)
    dev_json = json.dumps(devices)

    with pool(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET capabilities = ?, available_devices = ?,"
            " cpu_cores = ?, ram_mb = ?, os = ?, fs_free_gb = ?,"
            " status = 'active', status_reason = '' WHERE id = ?",
            (cap_json, dev_json, cpu_cores, ram_mb, os_val, fs_free, node_id),
        )
        # Also update gpu_name, gpu_type, gpu_memory_mb via capabilities JSON
        # These are stored inside the capabilities blob (no separate columns)
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_dict(row)
