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

"""Quickrobot MCP engine — FastMCP SSE server wrapping the quickrobot API.

Manages an MCP (Model Context Protocol) server process that exposes
quickrobot operations as MCP tools via SSE transport.

The mcp package is an optional dependency — this module handles ImportError
gracefully if the MCP server is not installed.
"""

import os
import sys
import subprocess
import json
import time

from lib.qr_engine_ids import QR_FORBIDDEN_HOSTS

from engine.base import BaseEngine


# Resolve project root relative to this module's location
_project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

CAPABILITIES = {
    "name": "quickrobot-mcp",
    "display_name": "Quickrobot MCP Server",
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 1,
    "sub_pages": [
        {"path": "/engines/quickrobot-mcp/settings", "label": "Settings", "order": 1},
        {"path": "/engines/quickrobot-mcp/status", "label": "Status", "order": 2},
    ],
}


class QrMcpEngine(BaseEngine):
    """Manages the MCP SSE server process via PID-in-DB tracking."""

    STATE_MACHINE_NAME = "quickrobot-mcp"

    @classmethod
    def get_state_machine(cls):
        """State machine for MCP engine.

        Extends base machine with config-from-deployed and config-from-running
        (BC-1 style config updates). Allows unconfigured → deployed → starting → running
        for system-managed instances that start via _start_system_engine().
        No build/compiling states.
        """
        sm = super().get_state_machine()
        if "configuring" not in sm["deployed"]:
            sm["deployed"].append("configuring")
        if "configuring" not in sm["running"]:
            sm["running"].append("configuring")
        # unconfigured → deployed is already in base; skip duplicate
        return sm

    def __init__(self, config=None):
        self.config = config or {}
        self._process = None
        self._mcp_available = False
        self._mcp_python = None
        self._name = CAPABILITIES["name"]
        self._check_mcp_available()

    def _resolve_python_interpreter(self, db_path, et_id):
        """Resolve the Python interpreter path for MCP server subprocess.

        Priority: config_override/engine_config mcp_python_interpreter > pipx auto-detect > system python.
        Returns None if mcp_python_interpreter is set but file doesn't exist.
        """
        # 1) Check explicit config value first
        from db.adapters.configs import get_engine_config as _gec
        config_val = ""
        try:
            if et_id:
                row = _gec(db_path, et_id, "mcp_python_interpreter")
                if row and row.get("value"):
                    config_val = str(row["value"]).strip()
        except Exception:
            pass
        if config_val:
            if os.path.isfile(config_val) and os.access(config_val, os.X_OK):
                return config_val
            # Config value specified but path doesn't exist — warn but don't fail init
            import logging
            logging.warning(f"MCP: configured python interpreter not found: {config_val}")
            return None

        # 2) Auto-detect pipx venv
        return self._find_pipx_mcp_python()

    def _find_pipx_mcp_python(self):
        """Locate the pipx venv Python for the mcp package."""
        import shutil as _shutil
        pipx_venv = os.path.expanduser("~/.local/share/pipx/venvs/mcp")
        python_path = os.path.join(pipx_venv, "bin", "python")
        if os.path.isfile(python_path) and os.access(python_path, os.X_OK):
            return python_path
        # Fallback: search for any python with mcp installed nearby
        for candidate in ["/usr/local/bin/pipx", "/usr/bin/pipx"]:
            try:
                result = _shutil.which("pipx")
                if result:
                    import subprocess as _subp
                    out = _subp.check_output([result, "list"], stderr=_subp.DEVNULL).decode()
                    if "mcp" in out.lower():
                        # Try common pipx paths
                        for p in [os.path.expanduser("~/.local/share/pipx/venvs/mcp/bin/python"),
                                  os.path.expanduser("~/.local/pipx/venvs/mcp/bin/python")]:
                            if os.path.isfile(p):
                                return p
            except Exception:
                pass
        return None

    def _check_mcp_available(self):
        """Verify mcp package is available.

        Uses resolved interpreter path (from config or auto-detect), then falls back to system python.
        """
        self._mcp_python = self._resolve_python_interpreter(None, None)
        if self._mcp_python:
            try:
                import subprocess as _subp
                result = _subp.run([self._mcp_python, "-c", "import mcp.server.fastmcp"],
                                   capture_output=True, timeout=5)
                if result.returncode == 0:
                    self._mcp_available = True
                    return
            except Exception:
                pass
        # Fallback: check system Python
        try:
            import mcp.server.fastmcp  # noqa: F401
            self._mcp_available = True
            self._mcp_python = sys.executable
        except ImportError:
            self._mcp_available = False

    def get_status(self, instance_id, db_path=None):
        """Check if the MCP server process is running via PID-in-DB.

        Returns canonical shape: {engine, instance_id, service_state, error}
        plus MCP-specific fields (flags, interpreter, etc.).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with canonical status shape + MCP fields.
        """
        from engine.base import build_canonical_status as _bcs
        from db.adapters.instances import get_instance
        inst = get_instance(db_path, instance_id)
        port = inst.get("port_assigned") if inst else None
        pid = inst.get("pid_last_known") if inst else None

        running = False
        rss_bytes = 0
        uptime_seconds = 0
        if pid:
            try:
                import psutil
                proc = psutil.Process(pid)
                if proc.status() != "zombie":
                    running = True
                    pid = pid
                    rss_bytes = proc.memory_info().rss
                    uptime_seconds = int(__import__("time").time() - proc.create_time())
            except Exception:
                pass

        # Read flags: config_override takes priority, then engine_configs, then defaults
        allow_reads = False
        allow_writes = False
        allow_proxy = False
        if inst:
            try:
                from db.adapters.configs import get_engine_config as _gec
                et_id = inst.get("engine_type_id")
                co_flags = inst.get("config_override", {}) or {}
                if isinstance(co_flags, str):
                    try:
                        co_flags = json.loads(co_flags)
                    except Exception:
                        co_flags = {}

                def _flag(key, default_val=False):
                    if key in co_flags:
                        return str(co_flags[key]).lower() in ("true", "1", "yes")
                    if et_id:
                        row = _gec(db_path, et_id, key) or {}
                        val = row.get("value", "")
                        if val:
                            return str(val).lower() in ("true", "1", "yes")
                    return default_val

                allow_reads = _flag("mcp_allow_reads")
                allow_writes = _flag("mcp_allow_writes")
                allow_proxy = _flag("mcp_allow_proxy")
            except Exception:
                pass

        return _bcs(self._name, instance_id,
                    service_state="running" if running else "stopped",
                    error=None,
                    running=running, pid=pid if running else None,
                    mcp_port=port,
                    allow_reads=allow_reads, allow_writes=allow_writes, allow_proxy=allow_proxy,
                    rss_bytes=rss_bytes, uptime_seconds=uptime_seconds,
                    interpreter_path=self._mcp_python,
                    mcp_available=self._mcp_available)

    def query_status(self, instance_id, db_path=None):
        """Health check via PID verification + optional HTTP probe.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with alive/latency/error details.
        """
        from db.adapters.instances import get_instance as _gi
        try:
            inst = _gi(db_path, instance_id)
            if not inst:
                return {"alive": False, "latency_ms": None, "error": f"Instance {instance_id} not found"}
            pid = inst.get("pid_last_known")
            port = inst.get("port_assigned")
            host = inst.get("config_override", {}).get("mcp_host") or os.environ.get("QUICKROBOT_MCP_HOST") or _CONFIG.get("host")

            if pid:
                try:
                    import psutil as _psutil
                    proc = _psutil.Process(pid)
                    if proc.status() != "zombie":
                        # PID alive — do a quick HTTP probe to the root path (not /sse which is SSE stream)
                        latency = None
                        if port:
                            import urllib.request as _ur
                            import time as _time
                            try:
                                url = f"http://{host}:{port}/"
                                start = _time.time()
                                resp = _ur.urlopen(url, timeout=2)
                                latency = round((_time.time() - start) * 1000, 2)
                            except Exception:
                                pass  # root may not serve HTML — PID check is authoritative
                        return {"alive": True, "latency_ms": latency}
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass

            return {"alive": False, "latency_ms": None, "error": f"PID {pid} not found or exited"}
        except Exception as exc:
            return {"alive": False, "latency_ms": None, "error": str(exc)}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Set MCP server config values.

        Args:
            instance_id: Integer primary key of the instance.
            config_dict: dict of configuration parameters (mcp_port, mcp_allow_reads, etc).
            db_path: Optional database path.

        Returns:
            dict with updated config stored in instance config_override.
        """
        from db.adapters.instances import update_instance
        if not config_dict:
            return {"engine": "quickrobot-mcp", "config": {}}
        try:
            inst = update_instance(db_path, instance_id, config_override=config_dict)
            return {"engine": "quickrobot-mcp", "config": config_dict, "applied": True}
        except Exception as exc:
            return {"engine": "quickrobot-mcp", "error": str(exc)}

    def get_config(self, instance_id, db_path=None):
        """Read current MCP server config from instance.

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
        """Start/stop/restart the MCP SSE server process.

        Delegates to lib_system_engine for subprocess management.
        Handles DB state transitions and MCP-specific flag resolution here.

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

        et_id = inst.get("engine_type_id")

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

        # Resolve MCP listen host/port from env file
        mcp_listen_host = env_config["QUICKROBOT_MCP_HOST"]
        if mcp_listen_host in QR_FORBIDDEN_HOSTS:
            print(f"[qr] FATAL: MCP bind host is '{mcp_listen_host}' — {QR_FORBIDDEN_HOSTS}")
            sys.exit(1)
        mcp_port_raw = env_config.get("QUICKROBOT_MCP_PORT")
        if not mcp_port_raw:
            raise KeyError("QUICKROBOT_MCP_PORT not in .quickrobot.env")
        mcp_listen_port = int(mcp_port_raw)

        # MCP flags: read from engine_configs (runtime API override takes priority over env file)
        try:
            from db.adapters.configs import get_engine_config as _gec
            def _flag_val(key, fallback_default="false"):
                row = _gec(db_path, et_id, key) if et_id else None
                if row and row.get("value"):
                    return str(row["value"]).lower()
                return env_config.get(f"QUICKROBOT_MCP_{key.upper()}", fallback_default)

            reads_val = _flag_val("mcp_allow_reads", "false")
            writes_val = _flag_val("mcp_allow_writes", "false")
            proxy_val = _flag_val("mcp_allow_proxy", "false")
        except Exception:
            reads_val = env_config.get("QUICKROBOT_MCP_READ", "false")
            writes_val = env_config.get("QUICKROBOT_MCP_WRITE", "false")
            proxy_val = env_config.get("QUICKROBOT_MCP_FULLPROXY", "false")

        if command == "start":
            # Check for existing live process via stored PID
            old_pid = inst.get("pid_last_known")
            if old_pid and _get_pid_status(old_pid):
                # Check if process is orphaned (parent API died)
                import psutil
                try:
                    proc = psutil.Process(old_pid)
                    ppid = proc.ppid()
                    if ppid == 1 or not _get_pid_status(ppid):
                        # Orphaned — kill and restart
                        print(f"[qr] mcp: orphaned process (pid={old_pid}), restarting")
                        proc.terminate()
                        time.sleep(1)
                        update_instance(db_path, instance_id, pid_last_known=None)
                    else:
                        try:
                            transition_state(db_path, instance_id, "running")
                        except Exception:
                            pass
                        return {"action": "start", "port": mcp_listen_port, "pid": old_pid, "status": "existing_process_alive"}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    update_instance(db_path, instance_id, pid_last_known=None)

            # Check MCP package availability
            if not self._mcp_available:
                return {"error": "mcp package not installed. Run: pipx install mcp or pip install mcp",
                        "action": command}

            # Resolve interpreter (reads current config value — stays in module)
            python_exe = self._resolve_python_interpreter(db_path, et_id) or self._mcp_python or sys.executable

            # Build CLI flags for MCP server
            extra_flags = []
            if reads_val == "true":
                extra_flags.append("--read")
            if writes_val == "true":
                extra_flags.append("--write")
            if proxy_val == "true":
                extra_flags.append("--proxy")

            # WRITE and PROXY imply READ per design
            reads_effective = "true" if (reads_val == "true" or writes_val == "true" or proxy_val == "true") else "false"
            write_implied_read = "true" if ((writes_val == "true" or proxy_val == "true") and reads_val != "true") else "false"
            if write_implied_read == "true":
                print(f"[qr] MCP started with WRITE flag — READ is implied")

            try:
                cmd = _build_command("mcp", env_config, api_host, api_port, extra_flags)
            except Exception as exc:
                _log_lifecycle("mcp", "start", {"error": str(exc)})
                return {"error": f"Failed to build command: {exc}", "action": command}

            try:
                # Consolidated env whitelist builder (Phase 1)
                from lib.lib_system_engine import build_subprocess_env
                env = build_subprocess_env(
                    engine_name="mcp",
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

                # Build command: replace sys.executable with resolved interpreter
                cmd = _build_command("mcp", env_config, api_host, api_port, extra_flags)
                cmd[0] = python_exe  # Use pipx/python interpreter instead of sys.executable
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=os.getcwd())
            except OSError as exc:
                _log_lifecycle("mcp", "start", {"error": str(exc)})
                return {"error": f"Failed to start MCP server: {exc}", "action": command}

            # Brief wait to detect immediate crashes
            import time as _time; _time.sleep(1)
            retcode = proc.poll()
            if retcode is not None:
                stdout, stderr = proc.communicate()
                _out = (stdout or b"").decode("utf-8", errors="replace").strip()[:500]
                _err = (stderr or b"").decode("utf-8", errors="replace").strip()[:500]
                err_msg = _err if _err else _out
                _log_lifecycle("mcp", "start", {"crashed": True, "returncode": retcode, "error": err_msg})
                return {"error": f"MCP crashed immediately (rc={retcode}): {err_msg}", "action": command}

            new_pid = proc.pid
            update_instance(db_path, instance_id, pid_last_known=new_pid)
            try:
                # Transition through proper state chain: unconfigured→deployed→starting→running
                cur = get_instance(db_path, instance_id)
                if cur and cur.get("state") == "unconfigured":
                    transition_state(db_path, instance_id, "deployed")
                transition_state(db_path, instance_id, "starting")
                transition_state(db_path, instance_id, "running")
            except Exception:
                pass
            _log_lifecycle("mcp", "start", {"pid": new_pid, "interpreter": python_exe, "flags": f"r={reads_val} w={writes_val} p={proxy_val}", "effective_flags": f"r={reads_effective} w={writes_val} p={proxy_val}", "write_implied_read": write_implied_read, "api_host": api_host, "api_port": api_port, "mcp_host": mcp_listen_host, "mcp_port": mcp_listen_port})
            return {"action": "start", "port": mcp_listen_port, "pid": new_pid, "status": "started"}

        elif command == "stop":
            pid = inst.get("pid_last_known")
            if pid and _get_pid_status(pid):
                try:
                    import psutil as _psutil
                    _psutil.Process(pid).terminate()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            update_instance(db_path, instance_id, pid_last_known=None)
            try:
                transition_state(db_path, instance_id, "stopped")
            except Exception:
                pass
            _log_lifecycle("mcp", "stop", {"pid": pid})
            return {"action": "stop", "pid": pid}

        elif command == "restart":
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
                print(f"[qr] mcp restart: old PID {old_pid} didn't exit within {timeout}s, force killing")
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

    def _get_flag(self, db_path, et_id, key, default):
        """Read a boolean flag from engine_configs."""
        try:
            if not et_id:
                return default
            row = _gec(db_path, et_id, key)
            if not row or not row.get("value"):
                return default
            val = str(row["value"]).lower()
            return val in ("true", "1", "yes")
        except Exception:
            return default

    def list_resources(self, instance_id, db_path=None):
        """No models or presets for the MCP server."""
        return {"models": [], "presets": []}

    def get_presets(self, engine_type_id, db_path=None):
        """No presets for MCP server."""
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """No presets for system-managed engines."""
        pass

    def forward_request(self, instance_id, method, params=None, db_path=None):
        """Forward a request -- returns MCP server status."""
        return self.get_status(instance_id, db_path)
