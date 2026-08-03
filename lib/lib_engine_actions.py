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

from lib.qr_engine_ids import (
    QR_ENGINE_IPERF3_NAME,
    QR_ENGINE_LLAMA_RPC_NAME,
    QR_ENGINE_LLAMA_SERVER_NAME,
    QR_ENGINE_SUBPROCESS_NAME,
    QR_ENGINE_TIMESTAMP_PROXY_NAME,
    QR_STATE_BUILD_ERROR,
    QR_STATE_COMPILING,
    QR_STATE_CONFIGURING,
    QR_STATE_DEPLOYED,
    QR_STATE_DEPLOYING,
    QR_STATE_ERROR,
    QR_STATE_LOADING,
    QR_STATE_RUNNING,
    QR_STATE_STARTING,
    QR_STATE_STOPPED,
    QR_STATE_STOPPING,
    QR_STATE_UNCONFIGURED,
    QR_STATE_UPDATING,
)

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
    QR_ENGINE_LLAMA_SERVER_NAME: {
        QR_STATE_UNCONFIGURED: _QL,
        QR_STATE_CONFIGURING: _QLS,
        QR_STATE_DEPLOYED: _QLSRDU_del,
        QR_STATE_STARTING: _QLS,
        QR_STATE_LOADING: _QLS,
        QR_STATE_RUNNING: _QLSR,
        QR_STATE_STOPPING: [_a("start", "Start")],
        QR_STATE_STOPPED: _QLSRDU,
        QR_STATE_ERROR: _QLSRDU_del,
        QR_STATE_DEPLOYING: _QLS,
        QR_STATE_UPDATING: [],
        QR_STATE_COMPILING: [],
        QR_STATE_BUILD_ERROR: [
            _a("deploy", "Deploy"),
            _a("start", "Start"),
            _a("undeploy", "Undeploy"),
            _a("delete", "Delete"),
        ],
        "timeout": [_a("deploy", "Deploy")],

    },
    # iperf3 — no rebuild, no undeploy
    QR_ENGINE_IPERF3_NAME: {
        QR_STATE_UNCONFIGURED: [
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        QR_STATE_CONFIGURING: _QLS,
        QR_STATE_DEPLOYED: [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("rebuild", "Rebuild"),
            _a("delete", "Delete"),
        ],
        QR_STATE_STARTING: _QLS,
        QR_STATE_RUNNING: _QLSR,
        QR_STATE_STOPPING: [_a("start", "Start")],
        QR_STATE_STOPPED: [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("rebuild", "Rebuild"),
            _a("deploy", "Deploy"),
        ],
        QR_STATE_ERROR: [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("rebuild", "Rebuild"),
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        QR_STATE_BUILD_ERROR: [
            _a("deploy", "Deploy"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("delete", "Delete"),
        ],
        "timeout": [_a("deploy", "Deploy")],
    },
    # subprocess — no rebuild, limited states
    QR_ENGINE_SUBPROCESS_NAME: {
        QR_STATE_UNCONFIGURED: [
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        QR_STATE_CONFIGURING: _QLS,
        QR_STATE_DEPLOYED: [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("delete", "Delete"),
        ],
        QR_STATE_STARTING: _QLS,
        QR_STATE_RUNNING: _QLSR,
        QR_STATE_STOPPING: [_a("start", "Start")],
        QR_STATE_STOPPED: [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("deploy", "Deploy"),
        ],
        QR_STATE_ERROR: [
            _a("reconfig_restart", "Reconfig/Restart"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        QR_STATE_BUILD_ERROR: [
            _a("deploy", "Deploy"),
            _a("start", "Start"),
            _a("stop", "Stop"),
            _a("delete", "Delete"),
        ],
        "timeout": [_a("deploy", "Deploy")],
    },
    # timestamp_proxy — infra engine, start/stop/restart only (no build)
    QR_ENGINE_TIMESTAMP_PROXY_NAME: {
        QR_STATE_UNCONFIGURED: [
            _a("deploy", "Deploy"),
            _a("delete", "Delete"),
        ],
        QR_STATE_DEPLOYING: _QLS,
        QR_STATE_CONFIGURING: _QLS,
        QR_STATE_RUNNING: _QLSR,
        QR_STATE_STOPPING: [_a("start", "Start")],
        QR_STATE_STOPPED: [
            _a("start", "Start"),
            _a("restart", "Restart"),
            _a("delete", "Delete"),
        ],
        QR_STATE_ERROR: [
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
    if engine_name in (QR_ENGINE_LLAMA_SERVER_NAME, QR_ENGINE_LLAMA_RPC_NAME):
        return _ACTION_MAPS[QR_ENGINE_LLAMA_SERVER_NAME]
    if engine_name == QR_ENGINE_IPERF3_NAME:
        return _ACTION_MAPS[QR_ENGINE_IPERF3_NAME]
    if engine_name == QR_ENGINE_SUBPROCESS_NAME:
        return _ACTION_MAPS[QR_ENGINE_SUBPROCESS_NAME]
    if engine_name == QR_ENGINE_TIMESTAMP_PROXY_NAME:
        return _ACTION_MAPS[QR_ENGINE_TIMESTAMP_PROXY_NAME]
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
