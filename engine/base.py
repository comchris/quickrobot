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

"""Quickrobot — BaseEngine abstract class.

Defines the interface that all engine implementations must follow.
Each engine controls a specific type of service deployed on remote nodes.
"""

import abc


class BaseEngine(abc.ABC):
    """Abstract base class for all engine implementations.

    Each engine represents a deployable service type (e.g., RPC engine,
    llama.cpp server) that runs on remote nodes managed by quickrobot.
    """

    STATE_MACHINE_NAME = "base"

    @classmethod
    def get_state_machine(cls):
        """Return engine-specific valid state transitions dict.

        Returns:
            dict mapping current_state -> [allowed_next_states].
            Engines override this to define their own lifecycle.
            The base machine is used as fallback for unknown engines.
        """
        return {
            "unconfigured": ["configuring", "stopped", "deployed"],
            "configuring": ["deploying", "build_error", "unconfigured", "stopping"],
            "deploying": ["deployed", "build_error", "error", "unconfigured"],
            "build_error": ["configuring", "error", "unconfigured"],
            "deployed": ["starting", "running", "stopped", "error", "unconfigured"],
            "starting": ["running", "error", "timeout", "stopping", "build_error"],
            "running": ["stopping", "error", "test_mode"],
            "stopping": ["stopped", "running", "starting", "deployed", "configuring", "error", "timeout"],
            "stopped": ["starting", "running", "configuring", "stopping", "error", "test_mode", "unconfigured"],
            "error": ["unconfigured", "configuring", "deploying", "starting", "stopping", "updating", "build_error", "compiling", "running"],
            "timeout": ["error"],
            "test_mode": ["running", "stopped", "error"],
        }

    @abc.abstractmethod
    def get_status(self, instance_id, db_path=None):
        """Get the current status of a running engine instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with status information (port, pid, uptime, etc.).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def query_status(self, instance_id, db_path=None):
        """Remote health check against the deployed service.

        Makes a live network call to verify the remote service is responding.
        RPC Engine: HTTP GET http://<host>:<port>/status
        Llama Server: HTTP GET http://<host>:<port>/health
        Fallback: Ansible systemctl is-active check

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with keys: alive (bool), latency_ms (float|None), error (str|None).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def set_config(self, instance_id, config_dict, db_path=None):
        """Apply a configuration update to a running instance.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters to apply.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with updated configuration.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_config(self, instance_id, db_path=None):
        """Retrieve the current running configuration of an instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with the instance's current configuration.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Execute a command on a running engine instance.

        Args:
            instance_id: Integer primary key of the instance.
            command: Command string or dict of parameters to send.
            db_path: Optional database path (required for system-managed engines).
            **kwargs: Additional arguments (timeout, extra_vars, etc.).

        Returns:
            dict with execution result.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def list_resources(self, instance_id, db_path=None):
        """List available resources (models, presets) for an engine instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with resource listings (models, presets, etc.).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_presets(self, engine_type_id, db_path=None):
        """Get available presets for an engine type.

        Args:
            engine_type_id: Integer primary key of the engine type.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            list of preset dicts.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """Set or change the active preset for an instance.

        Args:
            instance_id: Integer primary key of the instance.
            preset_id: Integer primary key of the target preset.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with updated instance data and merged config.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def forward_request(self, instance_id, method, params=None, db_path=None):
        """Forward an RPC-style request to a running engine instance.

        Args:
            instance_id: Integer primary key of the instance.
            method: RPC method name string.
            params: Optional dict of parameters.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with the response from the remote engine.
        """
        raise NotImplementedError


# Global engine registry
ENGINES = []
_ENGINES_MAP = {}


def register_engine(name, capabilities):
    """Register an engine class with its capabilities.

    Args:
        name: Unique engine name (e.g., 'rpc').
        capabilities: dict of capability metadata.
    """
    _ENGINES_MAP[name] = capabilities


def get_registered_engines():
    """Return the list of registered engine names with capabilities.

    Returns:
        list of dicts with 'name' and 'capabilities' keys.
    """
    return [{"name": n, "capabilities": c} for n, c in _ENGINES_MAP.items()]


def build_canonical_status(engine_name, instance_id, service_state=None,
                            error=None, **extra):
    """Build a canonical get_status() result dict.

    Canonical shape:
        {engine, instance_id, service_state, error} + optional subsystem keys.

    All 8 engines MUST return at minimum the 4 required keys.
    Additional keys are engine-specific and preserved via extra kwargs.

    Args:
        engine_name: Engine type name (e.g., "llama_server", "llama_rpc").
        instance_id: Integer primary key of the instance.
        service_state: Service state string (running, stopped, error, etc.).
        error: Error message string or None.
        **extra: Additional engine-specific keys to merge into result.

    Returns:
        dict with canonical status shape.
    """
    return {"engine": engine_name, "instance_id": instance_id,
            "service_state": service_state, "error": error} | extra


def derive_service_state(pid, db_state=None):
    """Derive a canonical service_state from PID + DB state.

    Args:
        pid: Process ID (int) or None/0.
        db_state: Current DB instance state string or None.

    Returns:
        String: "running", "stopped", or the original db_state.
    """
    if pid and isinstance(pid, int) and pid > 0:
        try:
            import psutil as _psutil
            proc = _psutil.Process(pid)
            if proc.status() != "zombie":
                return "running"
        except Exception:
            pass
    if db_state and db_state not in ("unconfigured",):
        return db_state
    return "stopped"
