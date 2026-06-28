"""Shared node business logic for quickrobot route handlers.

Contains: _get_node_build_state, _scan_orphaned_units.

Imported by routes_nodes.py and used by quickrobot.py root.
"""

import os
import re as _re
import subprocess as _sub

# Internal imports — avoid circular dependency with __init__.py
from db.adapters.nodes import list_nodes
from db.sqlite import pool


def _get_node_build_state(db_path, node_id):
    """Read the node_build_state from the nodes table.

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node.

    Returns:
        String state ('idle' or 'running'), defaults to 'idle'.
    """
    try:
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT node_build_state FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return row[0] if row and row[0] else "idle"
    except Exception:
        return "idle"


def _scan_orphaned_units(db_path):
    """Scan all active nodes for orphaned qr-*.service units.

    Cross-references remote systemd units against DB instance records.
    Returns list of {node_name, node_id, orphan_units: [{unit_key, uuid}]}.

    Uses Ansible ad-hoc via the dynamic inventory module.
    """
    # Build DB instance map: node_id -> set of valid unit keys
    with pool(db_path) as conn:
        node_instances = {}
        for row in conn.execute(
            "SELECT i.id, i.name, i.node_id, i.instance_uuid, e.name as engine_type_name "
            "FROM instances i JOIN engine_types e ON i.engine_type_id = e.id "
            "WHERE i.node_id IS NOT NULL AND i.node_id != 1"
        ):
            nid = row["node_id"]
            if nid not in node_instances:
                node_instances[nid] = []
            node_instances[nid].append({
                "id": row["id"],
                "name": row["name"],
                "unit_key": f"qr-{row['id']}-{row['engine_type_name']}",
                "uuid": row["instance_uuid"],
            })

    orphans = []
    all_nodes = list_nodes(db_path)

    # Get project root for inventory script path
    from qr_api.lib_instances import PROJECT_ROOT
    inv_script = os.path.join(PROJECT_ROOT, "lib", "qr_dynamic_inventory.py")

    for node in all_nodes:
        nid = node["id"]
        hostname = node.get("hostname", "")
        if not hostname or nid == 1 or node.get("status") != "active":
            continue

        # Get valid unit keys for this node from DB
        valid_keys = set()
        for inst in node_instances.get(nid, []):
            valid_keys.add(inst["unit_key"])

        # Run ansible ad-hoc to list qr-*.service files and parse UUIDs
        result = _sub.run(
            ["ansible", hostname, "-i", inv_script,
             "-m", "shell",
             "-a", "grep -h 'QR_UUID' /etc/systemd/system/qr-*.service 2>/dev/null || true"],
            capture_output=True, text=True, timeout=15,
        )

        if result.returncode != 0:
            continue

        # Parse remote units
        remote_units = {}
        for line in (result.stdout or "").strip().splitlines():
            if "QR_UUID=" not in line:
                continue
            # Format: /etc/systemd/system/qr-X-eng.service: QR_UUID=xxx
            parts = line.split(":", 1)
            file_path = parts[0].strip() if len(parts) > 0 else ""
            uuid_part = parts[1].strip() if len(parts) > 1 else ""
            uuid_val = uuid_part.replace("QR_UUID=", "").strip() if "QR_UUID=" in uuid_part else uuid_part.strip()

            # Extract unit key from path
            m = _re.search(r'qr-(\d+)-(\w+)\.service', file_path)
            if m:
                unit_key = f"qr-{m.group(1)}-{m.group(2)}"
                remote_units[unit_key] = {"uuid": uuid_val}

        # Find orphans: units on disk not in DB
        orphan_list = []
        for uk, info in remote_units.items():
            if uk not in valid_keys:
                orphan_list.append({
                    "unit_key": uk,
                    "uuid": info["uuid"],
                })

        if orphan_list:
            orphans.append({
                "node_name": hostname,
                "node_id": nid,
                "orphan_units": orphan_list,
            })

    return {"orphans": orphans, "total_orphans": sum(len(o["orphan_units"]) for o in orphans)}


def find_system_instance(db_path, engine_type_name):
    """Find the first system-managed instance matching an engine type name.

    Scans all instances in the database and returns the first record where
    engine_type_name matches and system_managed=1. Used by WebUI/MCP/API
    management endpoints to locate the singleton system-managed instance.

    Args:
        db_path: Path to the SQLite database.
        engine_type_name: Canonical engine type name (e.g., "quickrobot-webui").

    Returns:
        Dict with instance record, or None if not found.
    """
    from db.adapters.instances import list_instances as _li

    for i in _li(db_path):
        if i.get("engine_type_name") == engine_type_name and i.get("system_managed"):
            return i
    return None
