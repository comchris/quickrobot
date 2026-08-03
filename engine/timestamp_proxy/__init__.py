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

"""Quickrobot — Timestamp proxy engine.

Transparent chat proxy between clients and llama.cpp servers.
Injects timestamps into user messages, captures response timing.
Follows subprocess engine lifecycle (no build states).
"""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)
from lib.qr_engine_ids import (
    QR_ENGINE_TIMESTAMP_PROXY,
    QR_ENGINE_TIMESTAMP_PROXY_NAME,
    QR_JOB_RESTART,
    QR_JOB_START,
    QR_JOB_STOP,
    QR_JOB_UNDEPLOY,
    QR_STATE_COMPILING,
    QR_STATE_UPDATING,
    QR_STATE_BUILD_ERROR,
    QR_STATE_RUNNING,
    QR_STATE_CONFIGURING,
    QR_STAGE_STOP,
    QR_STAGE_UNDEPLOY,
    QR_STAGE_VERIFY,
)
from engine.base import BaseEngine


CAPABILITIES = {
    "name": QR_ENGINE_TIMESTAMP_PROXY_NAME,
    "display_name": "timestamp proxy",
    "category": "infra",
    "engine_type_id": QR_ENGINE_TIMESTAMP_PROXY,
    "supports_streaming": True,
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 99,
    "supported_jobs": [QR_JOB_START, QR_JOB_STOP, QR_JOB_RESTART, QR_JOB_UNDEPLOY],
    "env_builder": "build_timestamp_proxy_env",
    "undeploy_chain": [
        {"stage": QR_STAGE_STOP, "playbook": "service_stop"},
        {"stage": QR_STAGE_UNDEPLOY, "playbook": "undeploy_timestamp_proxy"},
        {"stage": QR_STAGE_VERIFY, "playbook": "check_undeploy"},
    ],
}


class TimestampProxyEngine(BaseEngine):
    """Transparent chat proxy with configurable timestamp injection.

    Intercepts /v1/chat/completions requests, injects timestamps into
    user message content, captures response timing, and returns modified
    responses to the client. Runs as a tmux-managed subprocess.
    """

    STATE_EXTENSIONS = {}
    STATE_REMOVALS = [QR_STATE_COMPILING, QR_STATE_UPDATING, QR_STATE_BUILD_ERROR]

    def __init__(self, db_path=None, instance_id=None):
        self.db_path = db_path
        self.instance_id = instance_id
        self._name = QR_ENGINE_TIMESTAMP_PROXY_NAME

    @classmethod
    def get_state_machine(cls):
        """State machine for timestamp proxy engine.

        Removes build states (compiling, updating, build_error) from all entries
        and adds configuring from running (config update pattern).
        """
        from lib.lib_engine_states import build_state_machine as _bsm
        return _bsm(
            removals=[QR_STATE_COMPILING, QR_STATE_UPDATING, QR_STATE_BUILD_ERROR],
            extensions={QR_STATE_RUNNING: [QR_STATE_CONFIGURING]},
        )

    def get_status(self, instance_id, db_path=None):
        """Check if the timestamp proxy is running via tmux PID-in-DB.

        Returns canonical shape: {engine, instance_id, service_state, error}
        plus proxy-specific fields (backend_host, backend_port).

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with canonical status shape + proxy fields.
        """
        from engine.base import build_canonical_status as _bcs
        from db.adapters.instances import get_instance as _gi
        inst = _gi(db_path, instance_id)
        if not inst:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": None, "error": "instance not found"}

        pid = inst.get("pid_last_known")
        co_data = inst.get("config_override") or {}
        if isinstance(co_data, str):
            try:
                import json as _jc
                co_data = _jc.loads(co_data)
            except Exception as _e:
                logger.debug("timestamp_proxy: failed to parse config_override for instance %d: %s", instance_id, _e)
                co_data = {}

        backend_host = co_data.get("backend_host", "")
        backend_port = co_data.get("backend_port", 0)
        db_state = inst.get("state")

        running = False
        if pid:
            try:
                import psutil as _psutil
                proc = _psutil.Process(pid)
                if proc.status() != "zombie":
                    running = True
            except Exception as _e:
                logger.debug("timestamp_proxy: process check failed for PID %d, instance %d: %s", pid, instance_id, _e)
                pass

        return _bcs(self._name, instance_id,
                    service_state="running" if running else db_state or "stopped",
                    error=None,
                    running=running, pid=pid if running else None,
                    backend_host=backend_host, backend_port=backend_port)

    def query_status(self, instance_id, db_path=None):
        """Health check via HTTP probe to the proxy's own port.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with alive/latency/error details.
        """
        from db.adapters.instances import get_instance as _gi
        import urllib.request as _ur
        import time as _time

        try:
            inst = _gi(db_path, instance_id)
            if not inst:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance {instance_id} not found"}

            co = inst.get("config_override") or {}
            if isinstance(co, str):
                try:
                    import json as _jc
                    co = _jc.loads(co)
                except Exception as _e:
                    logger.debug("timestamp_proxy: failed to parse config_override for health check, instance %d: %s", instance_id, _e)
                    co = {}

            port = inst.get("port_assigned")
            host = "127.0.0.1"

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
                import urllib.error as _ue
                if isinstance(exc, _ue.HTTPError) and 200 <= exc.code < 500:
                    latency = (_time.time() - start) * 1000
                    return {"alive": True, "latency_ms": round(latency, 2)}
                raise

        except Exception as exc:
            return {"alive": False, "latency_ms": None,
                    "error": str(exc)}

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Start/stop/restart the timestamp proxy subprocess.

        Double-spawn protection: checks PID-in-DB before starting.
        Uses tmux session for process isolation.

        Args:
            instance_id: Integer primary key of the instance.
            command: Action string (start, stop, restart).
            db_path: Optional database path.

        Returns:
            dict with action result.
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
            except Exception as _e:
                logger.debug("timestamp_proxy: failed to parse config_override for execute, instance %d: %s", instance_id, _e)
                co = {}

        port = inst.get("port_assigned")

        if command == QR_JOB_START:
            # Double-spawn protection: check existing PID
            old_pid = inst.get("pid_last_known")
            if old_pid:
                try:
                    import psutil as _psutil
                    p = _psutil.Process(old_pid)
                    if p.status() != "zombie":
                        current = inst.get("state", "")
                        if current == "running":
                            pass
                        elif current == "starting":
                            try:
                                _ts(db_path, instance_id, "running")
                            except Exception as _e:
                                logger.debug("timestamp_proxy: state transition to running failed for instance %d: %s", instance_id, _e)
                        else:
                            try:
                                _ts(db_path, instance_id, "starting")
                            except Exception as _e:
                                logger.debug("timestamp_proxy: state transition to starting failed for instance %d: %s", instance_id, _e)
                            try:
                                _ts(db_path, instance_id, "running")
                            except Exception as _e:
                                logger.debug("timestamp_proxy: state transition to running failed for instance %d: %s", instance_id, _e)
                        return {"action": "start", "port": port,
                                "pid": old_pid, "status": "existing_process_alive"}
                    else:
                        # Zombie — clear stale PID and start fresh
                        _ui(db_path, instance_id, pid_last_known=None)
                except Exception as _e:
                    logger.debug("timestamp_proxy: psutil check failed for old PID %d, instance %d: %s", old_pid, instance_id, _e)
                    _ui(db_path, instance_id, pid_last_known=None)

            # Build tmux session name from instance ID
            tmux_session = f"qr-ts-{instance_id}"
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cmd_parts = [
                "python3", "-u",
                os.path.join(project_root, "engine", "timestamp_proxy_server.py"),
                "--port", str(port),
                "--db", str(inst.get("node_id", 1)),
            ]

            # Start in tmux session
            try:
                proc = subprocess.Popen(
                    ["tmux", "new-session", "-d", "-s", tmux_session],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.wait()
            except Exception as exc:
                logger.debug("tmux session create failed for %s: %s", tmux_session, exc)

            # Run the proxy server in the tmux session
            try:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_session,
                     " ".join(cmd_parts), "C-m"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.debug("tmux send-keys failed for %s: %s", tmux_session, exc)

            new_pid = None
            try:
                import psutil as _psutil
                session_pids = [p.pid for p in _psutil.process_iter()
                                if "qr-ts-" + str(instance_id) in (p.name() or "")]
                if session_pids:
                    new_pid = session_pids[0]
            except Exception as _e:
                logger.debug("timestamp_proxy: failed to find proxy PID for instance %d: %s", instance_id, _e)

            _ui(db_path, instance_id, pid_last_known=new_pid)
            try:
                _ts(db_path, instance_id, "starting")
            except Exception as _e:
                logger.debug("timestamp_proxy: state transition to starting failed for instance %d: %s", instance_id, _e)
            try:
                cur = get_instance(db_path, instance_id)
                if cur and cur.get("state") != "running":
                    _ts(db_path, instance_id, "running")
            except Exception as _e:
                logger.debug("timestamp_proxy: state transition to running failed for instance %d: %s", instance_id, _e)
            return {"action": "start", "port": port, "pid": new_pid, "status": "started"}

        elif command == QR_JOB_STOP:
            pid = inst.get("pid_last_known")
            if pid:
                try:
                    import psutil as _psutil
                    _psutil.Process(pid).terminate()
                except Exception as _e:
                    logger.debug("timestamp_proxy: failed to terminate process %d for instance %d: %s", pid, instance_id, _e)
            _ui(db_path, instance_id, pid_last_known=None)
            return {"action": "stop", "pid": pid}

        elif command == QR_JOB_RESTART:
            self.execute(instance_id, QR_JOB_STOP, db_path)
            return self.execute(instance_id, QR_JOB_START, db_path)

        raise ValueError(f"Unknown action: {command}")

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for timestamp proxy instances (STATUS-1).

        Delegates to shared build_instance_status() with proxy-specific
        extras (backend_host, backend_port from config_override).
        """
        result = cls.build_instance_status(db_path, instance_id)
        if not result:
            return None

        from db.sqlite import pool

        with pool(db_path) as conn:
            co_row = conn.execute(
                "SELECT config_override FROM instances WHERE id = ?",
                (instance_id,),
            ).fetchone()

        if co_row:
            co_raw = co_row["config_override"] or "{}"
            co_dict = {}
            try:
                import json as _json
                co_dict = _json.loads(co_raw) if isinstance(co_raw, str) else (co_raw if isinstance(co_raw, dict) else {})
            except Exception as _e:
                logger.debug("timestamp_proxy: failed to parse config_override in get_instance_status for instance %d: %s", instance_id, _e)
                pass
            backend_host = co_dict.get("backend_host", "")
            backend_port = co_dict.get("backend_port", 0)
            result["engine_data"]["backend_host"] = backend_host or "-"
            result["engine_data"]["backend_port"] = backend_port or "-"

        return result

    @classmethod
    def build_instance_status(cls, db_path, instance_id):
        """Shared STATUS-1 base response."""
        from lib.lib_engine_status import build_instance_status as _shared_build
        return _shared_build(cls, db_path, instance_id)

    @classmethod
    def _get_available_actions(cls, state):
        """Map instance state to available actions (shared lib module)."""
        from lib.lib_engine_actions import get_action_map
        return get_action_map(CAPABILITIES["name"]).get(state, [])
