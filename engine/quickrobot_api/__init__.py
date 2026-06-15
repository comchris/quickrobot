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

"""Quickrobot — Quickrobot API (system) self-monitoring engine.

Tracks PID, RSS memory, uptime, Python and Flask versions
for the local quickrobot API server process.
"""

import os
import sys
import time

from engine.base import BaseEngine


CAPABILITIES = {
    "name": "quickrobot-api",
    "display_name": "Quickrobot API",
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 1,
    "sub_pages": [
        {"path": "/engines/quickrobot-api/status", "label": "Runtime Status", "order": 1},
    ],
}


class QrApiEngine(BaseEngine):
    """Self-monitoring engine that reads its own process stats via psutil."""

    STATE_MACHINE_NAME = "quickrobot-api"

    @classmethod
    def get_state_machine(cls):
        """State machine for quickrobot-api (tmux-based, no playbook states)."""
        sm = super().get_state_machine()
        # Simpler: no build/update/compiling, but allow config from deployed
        sm["deployed"].append("configuring")
        sm["running"].append("configuring")
        return sm

    def __init__(self, config=None):
        if os.getuid() == 0:
            print("this robot won't run as root")
            sys.exit(1)
        self.config = config or {}
        self._start_time = time.time()
        self._name = CAPABILITIES["name"]

    def get_status(self, instance_id, db_path=None):
        """Return runtime info for the quickrobot API process.

        Returns canonical shape: {engine, instance_id, service_state, error}
        plus runtime info (PID, RSS, uptime, Python version).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path (required for system-managed engines).

        Returns:
            dict with canonical status shape + runtime fields.
        """
        import psutil
        pid = os.getpid()
        try:
            proc = psutil.Process(pid)
            mem_info = proc.memory_info()
            rss_bytes = mem_info.rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            rss_bytes = 0

        version_info = sys.version_info
        python_version = f"{version_info.major}.{version_info.minor}.{version_info.micro}"

        uptime_seconds = int(time.time() - self._start_time)

        result = {
            "pid": pid,
            "rss_bytes": rss_bytes,
            "uptime_seconds": uptime_seconds,
            "python_version": python_version,
            "running": True,
        }

        # Try to get Flask version if available
        try:
            import flask
            result["flask_version"] = flask.__version__
        except ImportError:
            pass

        # Add database file size if db_path is available
        if db_path is not None and os.path.exists(db_path):
            db_size_bytes = os.path.getsize(db_path)
            if db_size_bytes < 1024:
                result["db_size"] = f"{db_size_bytes} B"
            elif db_size_bytes < 1024 * 1024:
                result["db_size"] = f"{db_size_bytes / 1024:.1f} KB"
            else:
                result["db_size"] = f"{db_size_bytes / (1024 * 1024):.1f} MB"

        # Update rss_bytes in DB if db_path provided
        if db_path is not None and instance_id:
            from db.sqlite import pool
            try:
                with pool(db_path) as conn:
                    conn.execute(
                        "UPDATE instances SET rss_bytes = ? WHERE id = ?",
                        (rss_bytes, instance_id),
                    )
            except Exception:
                pass  # Don't let DB errors break status reporting

        return {"engine": self._name, "instance_id": instance_id,
                "service_state": "running", "error": None} | result

    def query_status(self, instance_id, db_path=None):
        """Health check for the quickrobot API (always alive locally).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with alive=True and local process status details.
        """
        import psutil
        pid = os.getpid()
        try:
            proc = psutil.Process(pid)
            return {"alive": True, "latency_ms": 0.0,
                    "pid": pid, "rss_bytes": proc.memory_info().rss}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {"alive": False, "latency_ms": None,
                    "error": f"Process {pid} not found"}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Quickrobot API config is read-only.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters (ignored).
            db_path: Optional database path.

        Returns:
            dict confirming read-only status.
        """
        return {"engine": "quickrobot-api", "read_only": True, "config": {}}

    def get_config(self, instance_id, db_path=None):
        """Return the current system config snapshot.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with system configuration values.
        """
        return {
            "pid": os.getpid(),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        }

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Execute an action on the quickrobot API service.

        Args:
            instance_id: Integer primary key of the instance.
            command: Action string (health_check, metrics).
            db_path: Optional database path.
            **kwargs: Additional parameters.

        Returns:
            dict with execution result or self-monitoring data.
        """
        if command == "health_check":
            return self.get_status(instance_id, db_path)
        elif command == "metrics":
            import psutil
            pid = os.getpid()
            proc = psutil.Process(pid)
            return {
                "cpu_percent": proc.cpu_percent(interval=0.1),
                "memory_rss": proc.memory_info().rss,
                "memory_vms": proc.memory_info().vms,
                "threads": proc.num_threads(),
                "open_files": len(proc.open_files()),
                "connections": len(proc.net_connections()),
            }
        raise ValueError(f"Unknown action: {command}")

    def list_resources(self, instance_id, db_path=None):
        """No models or presets for the keeper service itself.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            Empty dicts for models and presets.
        """
        return {"models": [], "presets": []}

    def get_presets(self, engine_type_id, db_path=None):
        """No presets for the keeper service.

        Args:
            engine_type_id: Integer primary key of the engine type.
            db_path: Optional database path.

        Returns:
            Empty list.
        """
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """No presets for system-managed engines.

        Args:
            instance_id: Integer primary key of the instance.
            preset_id: Integer primary key of the target preset.
            db_path: Optional database path.
        """
        pass

    def forward_request(self, instance_id, method, params=None, db_path=None):
        """Forward a request -- returns self-monitoring data.

        Args:
            instance_id: Integer primary key of the instance.
            method: Request method name string.
            params: Optional dict of parameters.
            db_path: Optional database path.

        Returns:
            dict with self-monitoring status data.
        """
        return self.get_status(instance_id, db_path)
