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

"""Shared config methods for engine implementations.

Extracts the identical `set_config()` and `get_config()` stubs from
all engine __init__.py files into a single module. Each engine provides
its `_name` attribute; these methods return engine-scoped config results.

All engines (llama_server, llama_rpc, iperf3, subprocess) use these
identical stub implementations.
"""


def set_config(engine_name, instance_id, config_dict, db_path=None):
    """Apply configuration to an engine instance.

    All engines use this identical stub — actual persistence is handled
    by the API layer (config_override column update).

    Args:
        engine_name: Engine type name string.
        instance_id: Instance primary key.
        config_dict: Configuration parameters dict.
        db_path: Optional database path.

    Returns:
        dict with engine, instance_id, config, applied=True.
    """
    return {"engine": engine_name, "instance_id": instance_id,
            "config": config_dict, "applied": True}


def get_config(engine_name, instance_id, db_path=None):
    """Get current running configuration for an engine instance.

    All engines use this identical stub — returns empty dict since
    actual config is stored in the instance's config_override column
    and resolved at deploy time via the config merge chain.

    Args:
        engine_name: Engine type name string.
        instance_id: Instance primary key.
        db_path: Optional database path.

    Returns:
        dict with engine, instance_id, config={} (empty).
    """
    return {"engine": engine_name, "instance_id": instance_id,
            "config": {}}
