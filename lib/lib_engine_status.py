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

"""Shared STATUS-1 response builder for engine instances.

Extracts the ~65-line near-identical get_instance_status() method from all
engine __init__.py files into a single shared helper. Each engine overrides
build_instance_status() to inject engine-specific data extras and warnings.

Usage:
    # In engine class (e.g. LlamaServerEngine):
    @classmethod
    def build_instance_status(cls, db_path, instance_id):
        result = super().build_instance_status(db_path, instance_id)
        if not result:
            return None
        # Engine-specific extras
        with pool(db_path) as conn:
            job = ...  # running_job query
        result["engine_data"]["running_job"] = job["status"] if job else None
        # Engine-specific warnings
        if result["engine_data"]["node_hostname"]:
            result["warnings"].append(...)
        return result

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        return cls.build_instance_status(db_path, instance_id)

See also: engine/llama_server/__init__.py (527 lines), engine/llama_rpc/__init__.py
(443 lines), engine/iperf3/__init__.py (428 lines), engine/subprocess/__init__.py
(484 lines) — all now delegate to this module.
"""


def build_instance_status(engine_cls, db_path, instance_id):
    """Build STATUS-1 response dict for an engine instance.

    Shared base implementation used by all engine types. Queries the database
    for instance metadata, builds a standardized response with engine_data,
    available actions, warnings, and meta info.

    Each engine class overrides build_instance_status() to add engine-specific
    data extras (e.g., running_job for llama_server, pid_last_known for
    subprocess) and engine-specific warning rules.

    Args:
        engine_cls: Engine class (must have _get_available_actions classmethod).
        db_path: Path to the SQLite database.
        instance_id: Integer primary key of the instance.

    Returns:
        dict with keys: id, state, engine_type_name, engine_data, actions,
            warnings, _meta. Returns None if instance not found.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        inst = conn.execute(
            """SELECT i.id, i.name, i.state, i.port_assigned,
                      i.node_id,
                      e.name as engine_type_name,
                      n.hostname as node_hostname
               FROM instances i
               JOIN engine_types e ON i.engine_type_id = e.id
               LEFT JOIN nodes n ON i.node_id = n.id
               WHERE i.id = ?""",
            (instance_id,),
        ).fetchone()

    if not inst:
        return None

    engine_data = {
        "port_assigned": inst["port_assigned"],
        "node_hostname": inst["node_hostname"],
    }

    # Build available actions from engine-specific state machine
    actions = engine_cls._get_available_actions(inst["state"])

    # Base warnings — subclasses may append more
    warnings = []

    state_machine = engine_cls.get_state_machine()
    valid_next = state_machine.get(inst["state"], [])

    # Default transitioning states — subclasses override via _TRANSITIONING_STATES
    transitioning_states = getattr(engine_cls, "_TRANSITIONING_STATES", (
        "configuring", "deploying", "updating", "compiling", "starting", "stopping"
    ))
    is_transitioning = inst["state"] in transitioning_states

    return {
        "id": inst["id"],
        "state": inst["state"],
        "engine_type_name": inst["engine_type_name"],
        "engine_data": engine_data,
        "actions": actions,
        "warnings": warnings,
        "_meta": {
            "valid_next_states": valid_next,
            "is_transitioning": is_transitioning,
        },
    }


class EngineStatusHelper:
    """Mixin providing shared STATUS-1 response building.

    Usage in engine classes:
        class LlamaServerEngine(BaseEngine, EngineStatusHelper):
            _TRANSITIONING_STATES = ("configuring", "deploying", "updating",
                                     "compiling", "starting", "stopping")

            @classmethod
            def get_instance_status(cls, db_path, instance_id):
                result = cls.build_instance_status(db_path, instance_id)
                if not result:
                    return None
                # Engine-specific extras (running_job, model info, etc.)
                ...
                return result
    """

    @classmethod
    def build_instance_status(cls, db_path, instance_id):
        """Build base STATUS-1 response — delegate to module-level function."""
        return build_instance_status(cls, db_path, instance_id)
