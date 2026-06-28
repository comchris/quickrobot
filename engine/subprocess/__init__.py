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

"""Quickrobot — Subprocess engine.

Manages arbitrary local processes via subprocess.Popen.
Configurable executable, working directory, and CLI args.

Use case: run any binary as a managed service (opencode, pi-agent, etc.)
"""

import os
import sys
import subprocess

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST
from engine.base import BaseEngine


CAPABILITIES = {
    "name": "subprocess",
    "display_name": "Subprocess",
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 99,
}


class QrSubprocessEngine(BaseEngine):
    """Manages arbitrary local processes via subprocess.Popen with PID tracking."""

    def __init__(self):
        self._name = "subprocess"

    @classmethod
    def get_state_machine(cls):
        """State machine for subprocess engine.

        Extends base machine but removes states not applicable to subprocess
        (compiling, updating, build_error) since subprocess has no cmake build.
        Adds config-from-running support for BC-1 style updates.
        """
        sm = super().get_state_machine()
        # Remove build-specific states not relevant to subprocess
        for key in list(sm.keys()):
            sm[key] = [s for s in sm[key] if s not in ("compiling", "updating", "build_error", "test_mode")]
        # Remove states that subprocess can't reach
        for key in ["unconfigured", "configuring", "deploying", "deployed", "starting", "running", "stopping", "stopped", "error"]:
            sm[key] = [s for s in sm[key] if s != "compiling" and s != "updating" and s != "build_error"]
        # Add configuring from running (BC-1 style config updates)
        sm["running"].append("configuring")
        return sm

    def get_status(self, instance_id, db_path=None):
        """Check if the subprocess is running via PID-in-DB.

        Returns canonical shape: {engine, instance_id, service_state, error}
        plus subprocess-specific fields (pid, port, executable).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with canonical status shape + subprocess fields.
        """
        from engine.base import build_canonical_status as _bcs
        from db.adapters.instances import get_instance as _gi
        inst = _gi(db_path, instance_id)
        if not inst:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": None, "error": "instance not found"}

        pid = inst.get("pid_last_known")
        # Prefer user-set port in config_override over auto-assigned
        co_data = inst.get("config_override") or {}
        port = co_data.get("port") or inst.get("port_assigned")
        executable = co_data.get("executable", "")
        host = co_data.get("host", QR_DEFAULT_LOCALHOST)
        db_state = inst.get("state")

        running = False
        if pid:
            try:
                import psutil as _psutil
                proc = _psutil.Process(pid)
                if proc.status() != "zombie":
                    running = True
            except Exception:
                pass

        return _bcs(self._name, instance_id,
                    service_state="running" if running else db_state or "stopped",
                    error=None,
                    running=running, pid=pid if running else None,
                    port=port, host=host, executable=executable)

    def query_status(self, instance_id, db_path=None):
        """Health check via HTTP probe to the configured port.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with alive/latency/error details from HTTP check.
        """
        from db.adapters.instances import get_instance as _gi
        import urllib.request as _ur
        import time as _time

        try:
            inst = _gi(db_path, instance_id)
            if not inst:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance {instance_id} not found"}

            # For subprocess, prefer config_override port (user-set) over auto-assigned
            co = inst.get("config_override") or {}
            if isinstance(co, str):
                try:
                    import json as _jc
                    co = _jc.loads(co)
                except Exception:
                    co = {}
            port = co.get("port") or inst.get("port_assigned")
            host = co.get("host", QR_DEFAULT_LOCALHOST) if co else QR_DEFAULT_LOCALHOST

            if not port:
                return {"alive": False, "latency_ms": None,
                        "error": "No port assigned"}

            url = f"http://{host}:{port}/"
            start = _time.time()
            try:
                resp = _ur.urlopen(url, timeout=3)
                latency = (_time.time() - start) * 1000
                return {"alive": True, "latency_ms": round(latency, 2)}
            except Exception as exc:
                # Non-2xx responses (401 auth, 404 not found, etc.) still mean server is alive
                import urllib.error as _ue
                if isinstance(exc, _ue.HTTPError) and 200 <= exc.code < 500:
                    latency = (_time.time() - start) * 1000
                    return {"alive": True, "latency_ms": round(latency, 2)}
                raise

        except Exception as exc:
            return {"alive": False, "latency_ms": None,
                    "error": str(exc)}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Set subprocess config values in config_override.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters (executable, working_dir, cli_args, host, port, env_vars).
            db_path: Optional database path.

        Returns:
            dict with updated config stored in instance config_override.
        """
        from db.adapters.instances import update_instance as _ui
        if not config_dict:
            return {"engine": "subprocess", "config": {}}
        try:
            inst = _ui(db_path, instance_id, config_override=config_dict)
            return {"engine": "subprocess", "config": config_dict, "applied": True}
        except Exception as exc:
            return {"engine": "subprocess", "error": str(exc)}

    def get_config(self, instance_id, db_path=None):
        """Read current subprocess config from instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with current configuration values.
        """
        from db.adapters.instances import get_instance as _gi
        inst = _gi(db_path, instance_id)
        if inst and isinstance(inst.get("config_override"), dict):
            return inst["config_override"]
        return {}

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Start/stop/restart the subprocess.

        Double-spawn protection: checks PID-in-DB before starting.
        Zombie process detection: clears stale PIDs.

        Args:
            instance_id: Integer primary key of the instance.
            command: Action string (start, stop, restart).
            db_path: Optional database path.

        Returns:
            dict with action result and port info.
        """
        from db.adapters.instances import get_instance as _gi, update_instance as _ui, transition_state as _ts
        inst = _gi(db_path, instance_id)
        if not inst:
            return {"error": "instance not found", "action": command}

        # Read config
        co = inst.get("config_override") or {}
        if isinstance(co, str):
            try:
                import json as _jc
                co = _jc.loads(co)
            except Exception:
                co = {}

        executable = co.get("executable", "")
        working_dir = co.get("working_dir", "")
        cli_args = co.get("cli_args", "") or ""
        env_vars = co.get("env_vars", {})
        host = co.get("host", QR_DEFAULT_LOCALHOST)
        # Prefer user-set port in config_override over auto-assigned
        port = co.get("port") or inst.get("port_assigned")

        if not executable:
            return {"error": "executable not set in config_override", "action": command}

        # Template variable substitution — replaces {IP}/{PORT}/{ID}/{NAME} placeholders in cli_args
        if command == "start":
            import re as _re
            inst_name = inst.get("name", "")
            if host:
                cli_args = cli_args.replace("{IP}", host)
            if port is not None:
                cli_args = cli_args.replace("{PORT}", str(port))
            if inst_name:
                cli_args = cli_args.replace("{NAME}", inst_name)
            cli_id = str(instance_id)
            cli_args = cli_args.replace("{ID}", cli_id)
            # Auto-inject --hostname if not present in cli_args
            if host and "--hostname" not in cli_args.lower():
                cli_args = f"{cli_args} --hostname {host}"

        if command == "start":
            # Double-spawn protection: check existing PID
            old_pid = inst.get("pid_last_known")
            if old_pid:
                try:
                    import psutil as _psutil
                    p = _psutil.Process(old_pid)
                    if p.status() != "zombie":
                        # Process already running — ensure correct state
                        current = inst.get("state", "")
                        if current == "running":
                            pass  # already running, no transition needed
                        elif current == "starting":
                            try:
                                _ts(db_path, instance_id, "running")
                            except Exception:
                                pass
                        else:
                            try:
                                _ts(db_path, instance_id, "starting")
                            except Exception:
                                pass
                            try:
                                _ts(db_path, instance_id, "running")
                            except Exception:
                                pass
                        return {"action": "start", "port": port,
                                "pid": old_pid, "status": "existing_process_alive"}
                    else:
                        # Zombie — clear stale PID and start fresh
                        _ui(db_path, instance_id, pid_last_known=None)
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    _ui(db_path, instance_id, pid_last_known=None)

            # Build command — use shlex for safe quoted-string parsing (handles {NAME} with spaces)
            import shlex as _shlex
            cmd_parts = [executable]
            if cli_args:
                cmd_parts.extend(_shlex.split(cli_args))

            # Set up environment — env_passthrough toggle (default True for backward compat)
            env_passthrough = co.get("env_passthrough", True)
            if env_passthrough:
                env = os.environ.copy()
            else:
                # Whitelist mode: consolidated builder (Phase 1)
                from lib.lib_system_engine import build_subprocess_env
                from qr_api import _CONFIG as _qr_config
                api_host = _qr_config.get("host", QR_DEFAULT_LOCALHOST) if isinstance(_qr_config, dict) else QR_DEFAULT_LOCALHOST
                api_port = _qr_config.get("api_port", 8039) if isinstance(_qr_config, dict) else 8039
                env = build_subprocess_env(
                    engine_name="subprocess",
                    env_config={},
                    api_host=api_host,
                    api_port=api_port,
                    instance_config=co,
                    is_system_managed=False,
                )

            # Start subprocess
            try:
                kwargs_start = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                    "env": env,
                }
                if working_dir and os.path.isdir(working_dir):
                    kwargs_start["cwd"] = working_dir
                proc = subprocess.Popen(cmd_parts, **kwargs_start)
            except OSError as exc:
                return {"error": f"Failed to start: {exc}", "action": command}

            new_pid = proc.pid
            _ui(db_path, instance_id, pid_last_known=new_pid)
            # Transition to starting first (valid from stopped/deployed/error)
            try:
                _ts(db_path, instance_id, "starting")
            except Exception:
                pass
            # Then to running (valid from starting); skip if already running
            try:
                _cur = get_instance(db_path, instance_id)
                if _cur and _cur.get("state") != "running":
                    _ts(db_path, instance_id, "running")
            except Exception:
                pass
            return {"action": "start", "port": port, "pid": new_pid, "status": "started"}

        elif command == "stop":
            pid = inst.get("pid_last_known")
            if pid:
                try:
                    import psutil as _psutil
                    _psutil.Process(pid).terminate()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            _ui(db_path, instance_id, pid_last_known=None)
            return {"action": "stop", "pid": pid}

        elif command == "reconfigure":
            # Update config_override (already in DB via api_update_instance).
            # Restart the process so new config takes effect.
            if inst.get("state") in ("running",):
                # Running instance: stop then start with updated config
                self.execute(instance_id, "stop", db_path)
                return self.execute(instance_id, "start", db_path)
            elif inst.get("state") in ("deployed", "stopped"):
                # Not running — config is already persisted. No restart needed.
                # User must explicitly start to pick up new config.
                return {"action": "reconfigure", "state": inst["state"],
                        "note": "Config updated; restart via Start when ready"}
            elif inst.get("state") == "error":
                # Recover from error by restarting with current config
                self.execute(instance_id, "stop", db_path)
                return self.execute(instance_id, "start", db_path)

        elif command == "restart":
            self.execute(instance_id, "stop", db_path)
            return self.execute(instance_id, "start", db_path)

        raise ValueError(f"Unknown action: {command}")

    def list_resources(self, instance_id, db_path=None):
        """No models or presets for subprocess engine."""
        return {"models": [], "presets": []}

    def get_presets(self, engine_type_id, db_path=None):
        """No presets for subprocess engine."""
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """No presets for subprocess engine."""
        pass

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for subprocess instances (STATUS-1).

        Returns a standardized dict with engine_data, available actions,
        warnings, and meta info for WebUI rendering.
        """
        from db.sqlite import pool

        with pool(db_path) as conn:
            inst = conn.execute(
                """SELECT i.id, i.name, i.state, i.port_assigned,
                          i.config_override,
                          e.name as engine_type_name,
                          n.hostname as node_hostname,
                          i.pid_last_known
                   FROM instances i
                   JOIN engine_types e ON i.engine_type_id = e.id
                   LEFT JOIN nodes n ON i.node_id = n.id
                   WHERE i.id = ?""",
                (instance_id,),
            ).fetchone()

        if not inst:
            return None

        # Extract executable from config_override JSON (stored as string)
        co_raw = inst["config_override"] or "{}"
        co_dict = {}
        try:
            import json as _json
            co_dict = _json.loads(co_raw) if isinstance(co_raw, str) else (co_raw if isinstance(co_raw, dict) else {})
        except Exception:
            pass
        executable = co_dict.get("executable", "")

        engine_data = {
            "port_assigned": inst["port_assigned"],
            "node_hostname": inst["node_hostname"],
            "pid": inst["pid_last_known"],
            "executable": executable or "-",
        }

        actions = cls._get_available_actions(inst["state"])
        warnings = []

        state_machine = cls.get_state_machine()
        valid_next = state_machine.get(inst["state"], [])

        return {
            "id": inst["id"],
            "state": inst["state"],
            "engine_type_name": inst["engine_type_name"],
            "engine_data": engine_data,
            "actions": actions,
            "warnings": warnings,
            "_meta": {
                "valid_next_states": valid_next,
                "is_transitioning": inst["state"] in ("configuring",),
            },
        }

    @classmethod
    def _get_available_actions(cls, state):
        """Map instance state to available actions.

        Subprocess engine does not support reconfigure — it has no config
        merge pipeline (no preset/env/template chain). Use restart instead.
        Delete is hidden in running state (instance must be stopped first).
        """
        action_map = {
            "unconfigured": [{"name": "deploy", "label": "Deploy"}, {"name": "delete", "label": "Delete"}],
            "configuring": [{"name": "restart", "label": "Restart"}],
            "deployed": [{"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}],
            "starting": [{"name": "stop", "label": "Stop"}],
            "running": [{"name": "stop", "label": "Stop"}, {"name": "restart", "label": "Restart"}],
            "stopping": [{"name": "start", "label": "Start"}],
            "stopped": [{"name": "start", "label": "Start"}, {"name": "delete", "label": "Delete"}],
            "error": [{"name": "start", "label": "Start"}, {"name": "restart", "label": "Restart"}, {"name": "deploy", "label": "Deploy"}, {"name": "rebuild", "label": "Rebuild"}, {"name": "stop", "label": "Stop"}],
            "build_error": [{"name": "deploy", "label": "Deploy"}, {"name": "start", "label": "Start"}, {"name": "stop", "label": "Stop"}],
            "timeout": [{"name": "deploy", "label": "Deploy"}],
        }
        return action_map.get(state, [])

    def forward_request(self, instance_id, method, params=None, db_path=None):
        """Forward a request — returns subprocess status."""
        return self.get_status(instance_id, db_path)
