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

"""Shared action maps for engine instance states.

Extracts the ~45-line inline _get_available_actions() method from all engine
__init__.py files into a single lookup table. Each engine provides its name
(engines.LLAMA_SERVER_NAME, etc.) to select the appropriate action map.

Usage in engine class:
    @classmethod
    def _get_available_actions(cls, state):
        return get_action_map(cls._name).get(state, [])

Note: llama_server and llama_rpc share the same action map (both build-based).
iperf3 and subprocess have different maps (no rebuild, no compiling/updating).
"""

_A = {"name": lambda n: n, "label": lambda l: l}


def _a(name, label):
    """Create an action dict."""
    return {"name": name, "label": label}


# llama_server and llama_rpc share the same action map (both are build-based)
# Includes: deploy, rebuild, undeploy, delete, reconfig_restart
_QL = [
    _a("deploy", "Deploy"),
    _a("delete", "Delete"),
]

_QLS = [
    _a("stop", "Stop"),
]

_QLSR = [
    _a("reconfig_restart", "Reconfig/Restart"),
    _a("stop", "Stop"),
]

_QLSRDU = [
    _a("reconfig_restart", "Reconfig/Restart"),
    _a("start", "Start"),
    _a("stop", "Stop"),
    _a("rebuild", "Rebuild"),
    _a("deploy", "Deploy"),
    _a("undeploy", "Undeploy"),
]

_QLSRDU_del = _QLSRDU + [_a("delete", "Delete")]

_ACTION_MAPS = {
    # llama_server / llama_rpc — build-based engines
    "llama_server": {
        "unconfigured": _QL,
        "configuring": _QLS,
        "deployed": _QLSRDU_del,
        "starting": _QLS,
        "loading": _QLS,
        "running": _QLSR,
        "stopping": [_a("start", "Start")],
        "stopped": _QLSRDU,
        "error": _QLSRDU_del,
        "deploying": _QLS,
        "updating": [],
        "compiling": [],
        "build_error": [
            _a("deploy", "Deploy"),
            _a("start", "Start"),
            _a("undeploy", "Undeploy"),
            _a("delete", "Delete"),
        ],
        "timeout": [_a("deploy", "Deploy")],

    },
    # iperf3 — no rebuild, no undeploy
    "iperf3": {
        "unconfigured": [
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        "configuring": _QLS,
        "deployed": [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("rebuild", "Rebuild"),
            _a("delete", "Delete"),
        ],
        "starting": _QLS,
        "running": _QLSR,
        "stopping": [_a("start", "Start")],
        "stopped": [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("rebuild", "Rebuild"),
            _a("deploy", "Deploy"),
        ],
        "error": [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("rebuild", "Rebuild"),
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        "build_error": [
            _a("deploy", "Deploy"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("delete", "Delete"),
        ],
        "timeout": [_a("deploy", "Deploy")],
    },
    # subprocess — no rebuild, limited states
    "subprocess": {
        "unconfigured": [
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        "configuring": _QLS,
        "deployed": [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("delete", "Delete"),
        ],
        "starting": _QLS,
        "running": _QLSR,
        "stopping": [_a("start", "Start")],
        "stopped": [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("deploy", "Deploy"),
        ],
        "error": [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        "build_error": [
            _a("deploy", "Deploy"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("delete", "Delete"),
        ],
        "timeout": [_a("deploy", "Deploy")],
    },
    # timestamp_proxy — infra engine, start/stop/restart only (no build)
    "timestamp_proxy": {
        "unconfigured": [
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        "deploying": _QLS,
        "configuring": _QLS,
        "running": _QLSR,
        "stopping": [_a("start", "Start")],
        "stopped": [
            _a("start", "Start"),
            _a("restart", "Restart"),
            _a("delete", "Delete"),
        ],
        "error": [
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("restart", "Restart"),
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
    },
}


def get_action_map(engine_name):
    """Get the action map dict for an engine type.

    Args:
        engine_name: Engine name string (e.g., 'llama_server', 'llama_rpc').

    Returns:
        dict mapping state strings to list of action dicts.
    """
    if engine_name in ("llama_server", "llama_rpc"):
        return _ACTION_MAPS["llama_server"]
    if engine_name == "iperf3":
        return _ACTION_MAPS["iperf3"]
    if engine_name == "subprocess":
        return _ACTION_MAPS["subprocess"]
    if engine_name == "timestamp_proxy":
        return _ACTION_MAPS["timestamp_proxy"]
    # Fallback: empty map for unknown engines
    return {}


def get_available_actions(engine_name, state):
    """Get available actions for a specific engine and state.

    Convenience wrapper — preferred over direct dict lookup.

    Args:
        engine_name: Engine name string.
        state: Instance state string.

    Returns:
        list of action dicts (name + label).
    """
    return get_action_map(engine_name).get(state, [])
