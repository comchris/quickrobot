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

"""Shared resource methods for engine implementations.

Extracts the identical `list_resources()`, `get_presets()`, and
`set_active_preset()` stubs from all engine __init__.py files into
a single module. Each engine provides its `_name` attribute.

subprocess and llama_rpc engines used these as empty-stubs;
llama_server and iperf3 had custom implementations that remain
per-engine (list_resources only).
"""


def list_resources(engine_name, instance_id, db_path=None):
    """List available resources for an engine instance.

    Default stub — all engines use this identical implementation unless
    they override it (llama_server and iperf3 have custom list_resources).

    Returns empty models/presets dicts since actual resources are
    managed via the model/preset API endpoints.

    Args:
        engine_name: Engine type name string.
        instance_id: Instance primary key.
        db_path: Optional database path.

    Returns:
        dict with engine, instance_id, models=[], presets=[].
    """
    return {"engine": engine_name, "instance_id": instance_id,
            "models": [], "presets": []}


def get_presets(engine_name, engine_type_id, db_path=None):
    """Get available presets for an engine type.

    Default stub — all engines use this identical implementation.
    Presets are loaded from the DB at runtime via API endpoints.

    Args:
        engine_name: Engine type name string.
        engine_type_id: Engine type primary key.
        db_path: Optional database path.

    Returns:
        Empty list (presets managed via API endpoints).
    """
    return []


def set_active_preset(engine_name, instance_id, preset_id, db_path=None):
    """Set the active preset for an instance.

    Default stub — returns placeholder success dict.
    Actual preset assignment is handled by the API layer.

    Args:
        engine_name: Engine type name string.
        instance_id: Instance primary key.
        preset_id: Target preset primary key.
        db_path: Optional database path.

    Returns:
        dict with engine, instance_id, preset_id, applied=True.
    """
    return {"engine": engine_name, "instance_id": instance_id,
            "preset_id": preset_id, "applied": True}
