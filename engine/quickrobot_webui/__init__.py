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

"""Quickrobot v2 -- Quickrobot WebUI engine.

Controls the web UI frontend server process lifecycle via direct
subprocess management (PID-in-DB tracking, no tmux).
"""

import os
import sys
import subprocess
import time

from lib.qr_engine_ids import QR_FORBIDDEN_HOSTS

from engine.base import BaseEngine


CAPABILITIES = {
    "name": "quickrobot-webui",
    "display_name": "Quickrobot WebUI",
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 1,
    "sub_pages": [
        {"path": "/engines/quickrobot-webui/settings", "label": "Settings", "order": 1},
        {"path": "/engines/quickrobot-webui/status", "label": "Status", "order": 2},
    ],
}


class QrWebuiEngine(BaseEngine):
    """Manages the web UI frontend server process via PID-in-DB tracking (no tmux)."""

    STATE_MACHINE_NAME = "quickrobot-webui"

    @classmethod
    def get_state_machine(cls):
        """State machine for quickrobot-webui (PID-based, no playbook states).

        Allows unconfigured → deployed → starting → running for system-managed instances
        that start via _start_system_engine() after provisioning.
        """
        sm = super().get_state_machine()
        if "configuring" not in sm["deployed"]:
            sm["deployed"].append("configuring")
        if "configuring" not in sm["running"]:
            sm["running"].append("configuring")
        # unconfigured → deployed is already in base; skip duplicate
        return sm

    def __init__(self, config=None):
        if os.getuid() == 0:
            print("this robot won't run as root")
            sys.exit(1)
        self.config = config or {}
        self._name = CAPABILITIES["name"]

    def get_status(self, instance_id, db_path=None):
        """Check if the web UI server process is running via PID-in-DB.

        Returns canonical shape: {engine, instance_id, service_state, error}
        plus WebUI-specific fields (port, host).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with canonical status shape + WebUI fields.
        """
        from engine.base import build_canonical_status as _bcs
        from db.adapters.instances import get_instance
        inst = get_instance(db_path, instance_id)
        port = inst.get("port_assigned") if inst else None
        pid = inst.get("pid_last_known") if inst else None

        running = False
        if pid:
            try:
                import psutil
                proc = psutil.Process(pid)
                if proc.status() != "zombie":
                    running = True
            except (Exception,):
                pass

        return _bcs(self._name, instance_id,
                    service_state="running" if running else "stopped",
                    error=None,
                    running=running, pid=pid if running else None,
                    web_ui_port=port,
                    web_ui_host=inst.get("config_override", {}).get("web_ui_host", "127.0.0.1") if inst else "127.0.0.1")

    def query_status(self, instance_id, db_path=None):
        """Remote health check for the web server via HTTP.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with alive/latency/error details from HTTP check.
        """
        from db.adapters.instances import get_instance as _gi
        import urllib.request as _ur
        try:
            inst = _gi(db_path, instance_id)
            if not inst:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance {instance_id} not found"}
            port = inst.get("port_assigned")
            if not port:
                return {"alive": False, "latency_ms": None,
                        "error": "No port assigned"}
            host = inst.get("config_override", {}).get("web_ui_host")
            # Try 127.0.0.1 first (local process), then config host
            hosts_to_try = ["127.0.0.1"]
            if host and host not in ("127.0.0.1",) + QR_FORBIDDEN_HOSTS:
                hosts_to_try.append(host)
            elif host in QR_FORBIDDEN_HOSTS:
                # Wildcard bind — try to find local LAN IP via socket
                import socket as _sock
                try:
                    s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    hosts_to_try.append(s.getsockname()[0])
                    s.close()
                except Exception:
                    pass
            import time as _time
            last_err = None
            for h in hosts_to_try:
                try:
                    url = f"http://{h}:{port}/"
                    start = _time.time()
                    resp = _ur.urlopen(url, timeout=3)
                    latency = (_time.time() - start) * 1000
                    return {"alive": True, "latency_ms": round(latency, 2)}
                except Exception as exc:
                    last_err = str(exc)
            return {"alive": False, "latency_ms": None,
                    "error": f"tried {hosts_to_try}: {last_err}"}
        except Exception as exc:
            return {"alive": False, "latency_ms": None,
                    "error": str(exc)}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Set web server config values.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters (web_ui_host, web_ui_port).
            db_path: Optional database path.

        Returns:
            dict with updated config stored in instance config_override.
        """
        from db.adapters.instances import update_instance
        if not config_dict:
            return {"engine": "quickrobot-webui", "config": {}}

        try:
            inst = update_instance(db_path, instance_id, config_override=config_dict)
            return {"engine": "quickrobot-webui", "config": config_dict, "applied": True}
        except Exception as exc:
            return {"engine": "quickrobot-webui", "error": str(exc)}

    def get_config(self, instance_id, db_path=None):
        """Read current web server config from instance.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with current configuration values.
        """
        from db.adapters.instances import get_instance
        inst = get_instance(db_path, instance_id)
        if inst and isinstance(inst.get("config_override"), dict):
            return inst["config_override"]
        return {}

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Start/stop/restart the web UI server process.

        Delegates to lib_system_engine for subprocess management.
        Handles DB state transitions and logging here.

        Args:
            instance_id: Integer primary key of the instance.
            command: Action string (start, stop, restart).
            db_path: Optional database path.
            **kwargs: Additional parameters.

        Returns:
            dict with action result and port info.
        """
        from db.adapters.instances import get_instance, update_instance, transition_state
        from lib.lib_system_engine import load_env_config, _build_command, _get_pid_status, _log_lifecycle

        inst = get_instance(db_path, instance_id)
        if not inst:
            return {"error": "instance not found", "action": command}

        # Load env config (single source of truth for system engines)
        try:
            env_config = load_env_config(os.getcwd())
        except FileNotFoundError as exc:
            return {"error": str(exc), "action": command}

        # Resolve API host/port from env file (single source of truth for system engines)
        api_host = env_config["QUICKROBOT_API_HOST"]
        raw_port = env_config.get("QUICKROBOT_API_PORT")
        if not raw_port:
            raise KeyError("QUICKROBOT_API_PORT not in .quickrobot.env")
        api_port = int(raw_port)

        if command == "start":
            old_pid = inst.get("pid_last_known")
            if old_pid and _get_pid_status(old_pid):
                # Check if process is orphaned (parent API died)
                import psutil
                try:
                    proc = psutil.Process(old_pid)
                    ppid = proc.ppid()
                    if ppid == 1 or not _get_pid_status(ppid):
                        # Orphaned — kill and restart
                        print(f"[qr] webui: orphaned process (pid={old_pid}), restarting")
                        proc.terminate()
                        time.sleep(1)
                        update_instance(db_path, instance_id, pid_last_known=None)
                    else:
                        try:
                            transition_state(db_path, instance_id, "deployed")
                        except Exception:
                             pass
                        webui_port = inst.get("port_assigned") or env_config["QUICKROBOT_WEBUI_PORT"]
                        return {"action": "start", "port": int(webui_port), "pid": old_pid,
                                "status": "existing_process_alive"}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    update_instance(db_path, instance_id, pid_last_known=None)

            # Build command and start subprocess
            try:
                cmd = _build_command("webui", env_config, api_host, api_port)
            except Exception as exc:
                _log_lifecycle("webui", "start", {"error": str(exc)})
                return {"error": f"Failed to build command: {exc}", "action": command}

            try:
                # Consolidated env whitelist builder (Phase 1)
                from lib.lib_system_engine import build_subprocess_env
                env = build_subprocess_env(
                    engine_name="webui",
                    env_config=env_config,
                    api_host=api_host,
                    api_port=api_port,
                )
                # Env whitelist verification
                _test_key = "QUICKROBOT_TEST_VAR"
                if _test_key in env_config and _test_key not in env:
                    print(f"[qr] ENV WHITELIST OK: {_test_key} excluded from child env")
                os_env_count = len(os.environ)
                child_env_count = len(env)
                if child_env_count < os_env_count:
                    print(f"[qr] ENV: subprocess env reduced {os_env_count} → {child_env_count} keys (whitelist)")
                log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs", "webui.log")
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, "a") as logf:
                    proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, env=env, cwd=os.getcwd())
            except OSError as exc:
                _log_lifecycle("webui", "start", {"error": str(exc)})
                return {"error": f"Failed to start webui: {exc}", "action": command}

            # Brief wait to detect immediate crashes
            import time as _time; _time.sleep(1)
            retcode = proc.poll()
            if retcode is not None:
                stdout, stderr = proc.communicate()
                _out = (stdout or b"").decode("utf-8", errors="replace").strip()[:500]
                _err = (stderr or b"").decode("utf-8", errors="replace").strip()[:500]
                err_msg = _err if _err else _out
                _log_lifecycle("webui", "start", {"crashed": True, "returncode": retcode, "error": err_msg})
                return {"error": f"WebUI crashed immediately (rc={retcode}): {err_msg}", "action": command}

            new_pid = proc.pid
            update_instance(db_path, instance_id, pid_last_known=new_pid)
            try:
                # Transition through proper state chain: unconfigured→deployed→starting→running
                cur = get_instance(db_path, instance_id)
                if cur and cur.get("state") == "unconfigured":
                    transition_state(db_path, instance_id, "deployed")
                # deployed→running is invalid (missing 'starting'); go through starting first
                transition_state(db_path, instance_id, "starting")
                transition_state(db_path, instance_id, "running")
            except Exception:
                pass
            _log_lifecycle("webui", "start", {"pid": new_pid, "api_host": api_host, "api_port": api_port})
            webui_port = inst.get("port_assigned") or env_config["QUICKROBOT_WEBUI_PORT"]
            return {"action": "start", "port": int(webui_port), "pid": new_pid, "status": "started"}

        elif command == "stop":
            pid = inst.get("pid_last_known")
            if pid and _get_pid_status(pid):
                try:
                    import psutil as _psutil
                    _psutil.Process(pid).terminate()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            update_instance(db_path, instance_id, pid_last_known=None)
            _log_lifecycle("webui", "stop", {"pid": pid})
            return {"action": "stop", "pid": pid}

        elif command == "restart":
            # Full restart cycle: stop → dead-check → start
            # Transition to stopping for visible state change in UI
            try:
                transition_state(db_path, instance_id, "stopping")
            except Exception:
                pass

            old_pid = inst.get("pid_last_known")
            if old_pid and _get_pid_status(old_pid):
                try:
                    import psutil as _psutil
                    _psutil.Process(old_pid).terminate()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass

            # Dead-check loop
            timeout = int(env_config.get("QUICKROBOT_SERVER_SPAWN_TIMEOUT", 5))
            deadline = __import__("time").time() + timeout
            dead_verified = False
            while __import__("time").time() < deadline:
                if not _get_pid_status(old_pid):
                    dead_verified = True
                    break
                __import__("time").sleep(0.5)

            if not dead_verified:
                print(f"[qr] webui restart: old PID {old_pid} didn't exit within {timeout}s, force killing")
                try:
                    import psutil as _psutil
                    if _get_pid_status(old_pid):
                        _psutil.Process(old_pid).kill()
                        __import__("time").sleep(1)
                except Exception:
                    pass

            # Start new process
            return self.execute(instance_id, "start", db_path)

        raise ValueError(f"Unknown action: {command}")

    def list_resources(self, instance_id, db_path=None):
        """No models or presets for the web server.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            Empty dicts for models and presets.
        """
        return {"models": [], "presets": []}

    def get_presets(self, engine_type_id, db_path=None):
        """No presets for web server.

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
        """Forward a request -- returns web server status.

        Args:
            instance_id: Integer primary key of the instance.
            method: Request method name string.
            params: Optional dict of parameters.
            db_path: Optional database path.

        Returns:
            dict with web server status data.
        """
        return self.get_status(instance_id, db_path)


def shutil_which(cmd):
    """Find executable in PATH (lightweight shutil.which replacement).

    Args:
        cmd: Command name to find.

    Returns:
        Full path to the command, or None if not found.
    """
    import glob as _glob
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for p in paths:
        candidate = os.path.join(p, cmd)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        # Also check with common extensions on Windows
        for ext in [".exe", ".bat", ".cmd"]:
            candidate_ext = candidate + ext
            if os.path.isfile(candidate_ext):
                return candidate_ext
    return None
