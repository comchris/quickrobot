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

"""Quickrobot — Shared get_status() helper for systemd-based engines.

Handles the common pattern: query DB → build unit_name → call _check_remote_service()
→ merge result with engine-specific fields. Engines that differ (subprocess) keep
their own implementation.

Usage:
    from lib.lib_engine_status_query import query_systemd_status as _qs

    def get_status(self, instance_id, db_path=None):
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "error": "db_path required for remote get_status"}
        try:
            return _qs(db_path, self._name, instance_id,
                       unit_name=f"qr-{instance_id}-llama_server",
                       extra_fields={"port_assigned": ...})
        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": "unknown", "error": str(exc),
                    "main_pid": None, "memory_mb": None,
                    "restart_count": 0}

Args:
    db_path: Path to SQLite database.
    engine_name: Engine type name string.
    instance_id: Integer primary key of the instance.
    unit_name_builder: Callable(db_row) -> str that builds the systemd unit name.
    extra_fields: Optional dict of additional fields to merge into result.
    node_user_default: Default SSH user if not in DB row (from lib_constants).
    localhost_default: Default localhost address (from qr_engine_ids).

Returns:
    dict with keys: engine, instance_id, unit_name, service_state, error,
        main_pid, memory_mb, restart_count, service_substate, plus extras.
"""


import logging
logger = logging.getLogger(__name__)


def query_systemd_status(db_path, engine_name, instance_id,
                         unit_name_builder, extra_fields=None,
                         node_user_default="mepaw", localhost_default="127.0.0.1"):
    """Query systemd-based status for a remote instance.

    Common DB query + service check pattern shared by llama_server,
    llama_rpc, and iperf3 engines. Each engine differs only in how
    unit_name is built and what extra fields are returned.

    Args:
        db_path: Path to SQLite database.
        engine_name: Engine type name string.
        instance_id: Integer primary key of the instance.
        unit_name_builder: Callable(row) -> str that builds the systemd unit name.
        extra_fields: Optional dict of additional fields to merge into result.
        node_user_default: Default SSH user if not in DB row.
        localhost_default: Default localhost address if node host missing.

    Returns:
        dict with canonical status shape + engine-specific fields.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            """SELECT i.port_assigned, i.state, i.name,
                       n.hostname as node_host, n.ansible_user as node_user,
                       e.name as engine_type_name
                FROM instances i
                LEFT JOIN nodes n ON i.node_id = n.id
                JOIN engine_types e ON i.engine_type_id = e.id
                WHERE i.id = ?""",
            (instance_id,),
        ).fetchone()

    if not row:
        return {"engine": engine_name, "instance_id": instance_id,
                "service_state": None, "error": f"Instance {instance_id} not found"}

    unit_name = unit_name_builder(row)
    node_host = (row["node_host"] or localhost_default)
    node_user = (row["node_user"] if row["node_user"] else None) or node_user_default
    engine_type = row["engine_type_name"]

    # Delegate remote check to shared lib_engine_health
    from lib.lib_engine_health import check_remote_service as _check
    result = _check(node_host, unit_name, node_user)

    # Build merged result
    merged = {
        "engine": engine_name,
        "instance_id": instance_id,
        "unit_name": unit_name,
        "node_host": node_host,
        "port_assigned": row["port_assigned"],
    }

    if extra_fields:
        merged.update(extra_fields)

    return merged | result
