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

"""Shared command/execute methods for engine implementations.

Extracts the identical `execute()` and `forward_request()` stubs from
llama_server, llama_rpc, and iperf3 engine __init__.py files. These
return placeholder results — actual execution is handled by API routes.

subprocess engine overrides execute() with full PID/state management
logic, so it does not use this shared implementation.
"""


def execute(engine_name, instance_id, command, db_path=None, **kwargs):
    """Execute a command on an engine instance.

    Shared stub used by llama_server, llama_rpc, and iperf3.
    All engines use this identical placeholder — actual command
    execution is handled by the API layer via playbook runners.

    Args:
        engine_name: Engine type name string.
        instance_id: Instance primary key.
        command: Command string or dict.
        db_path: Optional database path.
        **kwargs: Additional parameters (ignored in stub).

    Returns:
        dict with engine, instance_id, command, result="executed".
    """
    return {"engine": engine_name, "instance_id": instance_id,
            "command": command, "result": "executed"}


def forward_request(engine_name, instance_id, method, params=None, db_path=None):
    """Forward an RPC-style request to an engine instance.

    Shared stub used by llama_server, llama_rpc, and iperf3.
    Returns a placeholder result — actual forwarding is done via
    the API proxy layer.

    Args:
        engine_name: Engine type name string.
        instance_id: Instance primary key.
        method: Request method name string.
        params: Optional dict of parameters.
        db_path: Optional database path.

    Returns:
        dict with engine, instance_id, method, params, result=None.
    """
    return {"engine": engine_name, "instance_id": instance_id,
            "method": method, "params": params or {}, "result": None}
