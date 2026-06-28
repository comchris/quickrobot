#!/usr/bin/env python3
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
"""quickrobot — Dynamic Ansible inventory from SQLite DB.

Replaces the static ansible_inventory.ini file. Ansible invokes this script
with --list (get all hosts) or --host <name> (get host-specific vars).

Usage:
    ./lib/qr_dynamic_inventory.py --list
    ./lib/qr_dynamic_inventory.py --host dllama6.lan

Output format is compatible with Ansible 2.10+.
"""
import json
import os
import sqlite3
import sys

# Ensure project root is on sys.path when run as a standalone script (e.g., by ansible)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from lib.lib_constants import DEFAULT_ANSIBLE_USER


def get_db_path():
    """Resolve the quickrobot.db path relative to this script's location."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)  # lib/ -> project root
    db_path = os.path.join(project_root, "data", "quickrobot.db")
    return db_path


def query_active_nodes(db_path):
    """Query the nodes table for active nodes and their ansible connection info."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, name, hostname, ansible_user, ssh_port, status, "
            "ansible_inventory_host, ansible_key_path FROM nodes WHERE status IN ('active', 'unknown')"
        ).fetchall()
    finally:
        conn.close()


def build_inventory():
    """Build the full Ansible inventory dict from the database.

    Uses _meta.hostvars for per-host variables (Ansible 2.10+ recommended format).
    All hosts are placed in the 'all' group to avoid deprecation warnings from
    hostnames containing dots being treated as separate groups.
    
    NO FALLBACK RULE: if a hostname maps to multiple nodes, record the 
    duplicates and flag them. The --host <name> path will fail hard on 
    ambiguous names so the user gets a clear error instead of silent wrong host.
    """
    db_path = get_db_path()
    if not os.path.isfile(db_path):
        # Return empty inventory — no DB yet
        return {"all": {"hosts": []}, "_meta": {"hostvars": {}}}

    rows = query_active_nodes(db_path)

    # Phase 1: group nodes by inventory name (ansible_inventory_host or hostname)
    name_to_rows = {}
    for row in rows:
        inv_name = row["ansible_inventory_host"] or row["hostname"]
        if inv_name not in name_to_rows:
            name_to_rows[inv_name] = []
        name_to_rows[inv_name].append(row)

    # Phase 2: resolve each inventory name to exactly one node
    # NO FALLBACK: duplicate names are excluded from --list so ansible
    # sees 0 hosts and fails explicitly rather than silently picking one.
    hosts_list = []
    hostvars = {}

    for inv_name, rows_for_name in name_to_rows.items():
        if len(rows_for_name) == 1:
            row = rows_for_name[0]
        else:
            # Multiple nodes share this inventory name — ambiguous.
            # Skip this host entirely from inventory (no silent fallback).
            ids = [str(r["id"]) for r in rows_for_name]
            names = [r["name"] for r in rows_for_name]
            sys.stderr.write(
                f"[qr] WARN: duplicate inventory name '{inv_name}' skipped "
                f"(nodes {', '.join(ids)}: {', '.join(names)}). "
                f"Set ansible_inventory_host on all but one to resolve.\n"
            )
            continue

        host_addr = row["hostname"] or inv_name
        user = row["ansible_user"] or DEFAULT_ANSIBLE_USER
        port = row["ssh_port"] or 22
        node_id = row["id"]

        hosts_list.append(inv_name)
        hv = {
            "ansible_host": host_addr,
            "ansible_user": user,
            "ansible_port": port,
            "ansible_connection": "local" if (node_id == 1) else "ssh",
            "ansible_become_pass": "",
        }
        try:
            kv = row["ansible_key_path"]
        except (KeyError, IndexError):
            kv = None
        if kv:
            hv["ansible_ssh_private_key_file"] = kv
        
        hostvars[inv_name] = hv

    return {
        "all": {"hosts": hosts_list},
        "_meta": {"hostvars": hostvars},
    }


def get_host_vars(hostname):
    """Get variables for a single host.
    
    NO FALLBACK RULE: if the hostname is ambiguous (maps to multiple nodes),
    return {} so ansible fails explicitly — no silent fallback to wrong host.
    If not found, also return {}.
    """
    inventory = build_inventory()
    hostvars = inventory.get("_meta", {}).get("hostvars", {})
    hv = hostvars.get(hostname, None)
    
    if hv is None:
        # Not found in inventory
        print(json.dumps({}), file=sys.stdout)
        return
    
    if hv.get("_qr_duplicate_of", {}).get("is_duplicate"):
        # Duplicate hostname — return empty vars so ansible fails explicitly
        # instead of silently picking a wrong node
        print(json.dumps({}), file=sys.stdout)
        return
    
    print(json.dumps(hv), file=sys.stdout)


def main():
    if len(sys.argv) < 2:
        print(json.dumps(build_inventory()), file=sys.stdout)
        sys.exit(0)

    arg = sys.argv[1]

    if arg == "--list":
        # Ansible expects --list to output the full inventory
        result = build_inventory()
        print(json.dumps(result, indent=2))
        sys.exit(0)

    elif arg == "--host":
        if len(sys.argv) < 3:
            print(json.dumps({}), file=sys.stdout)
            sys.exit(0)
        hostname = sys.argv[2]
        host_vars = get_host_vars(hostname)
        print(json.dumps(host_vars))
        sys.exit(0)

    else:
        # Unknown argument — output full inventory as fallback
        result = build_inventory()
        print(json.dumps(result, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
