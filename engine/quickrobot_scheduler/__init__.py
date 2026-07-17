# quickrobot scheduler engine package
# Manages the background scheduler process via PID-in-DB tracking.

import logging
import os
import sys

logger = logging.getLogger(__name__)
from engine.base import BaseEngine
from lib.qr_engine_ids import QR_ENGINE_SCHEDULER_NAME


CAPABILITIES = {
    "name": QR_ENGINE_SCHEDULER_NAME,
    "display_name": "Quickrobot Scheduler",
    "supports_models": False,
    "supports_presets": False,
    "max_instances": 1,
}


class SchedulerEngine(BaseEngine):
    """Manages the scheduler background process via PID-in-DB tracking."""

    STATE_MACHINE_NAME = QR_ENGINE_SCHEDULER_NAME

    def __init__(self, config=None):
        self.config = config or {}
        self._name = CAPABILITIES["name"]

    # ── Abstract method stubs (minimal for system-managed engine) ──

    def get_status(self, instance_id, db_path=None):
        """Return scheduler process status via PID-in-DB."""
        from engine.base import build_canonical_status as _bcs
        from db.adapters.instances import get_instance
        inst = get_instance(db_path, instance_id)
        pid = inst.get("pid_last_known") if inst else None
        running = False
        if pid:
            try:
                os.kill(pid, 0)
                running = True
            except OSError:
                pass
        return _bcs(self._name, instance_id,
                    service_state="running" if running else "stopped",
                    error=None, running=running, pid=pid if running else None)

    def query_status(self, instance_id, db_path=None):
        """No HTTP endpoint — returns PID status."""
        return {"alive": self.get_status(instance_id, db_path).get("running", False)}

    def get_config(self, instance_id, db_path=None):
        """Return instance config_override."""
        from db.adapters.instances import get_instance
        inst = get_instance(db_path, instance_id)
        if inst and isinstance(inst.get("config_override"), dict):
            return inst["config_override"]
        return {}

    def set_config(self, instance_id, config_dict, db_path=None):
        """Update instance config_override."""
        from db.adapters.instances import update_instance
        if not config_dict:
            return {"engine": QR_ENGINE_SCHEDULER_NAME, "config": {}}
        try:
            update_instance(db_path, instance_id, config_override=config_dict)
            return {"engine": "quickrobot-scheduler", "config": config_dict, "applied": True}
        except Exception as exc:
            return {"engine": "quickrobot-scheduler", "error": str(exc)}

    def execute(self, instance_id, command, db_path=None, **kwargs):
        """Start/stop/restart the scheduler process via lib_system_engine."""
        from db.adapters.instances import get_instance, update_instance, transition_state
        from lib.lib_system_engine import (
            load_env_config, _build_command, _get_pid_status,
            _log_lifecycle, build_subprocess_env, _register_child,
        )

        inst = get_instance(db_path, instance_id)
        if not inst:
            return {"error": "instance not found", "action": command}

        try:
            env_config = load_env_config(os.getcwd())
        except FileNotFoundError as exc:
            return {"error": str(exc), "action": command}

        api_host = env_config["QUICKROBOT_API_HOST"]
        raw_port = env_config.get("QUICKROBOT_API_PORT")
        if not raw_port:
            raise KeyError("QUICKROBOT_API_PORT not in .quickrobot.env")
        api_port = int(raw_port)

        if command == "start":
            old_pid = inst.get("pid_last_known")
            if old_pid and _get_pid_status(old_pid):
                import psutil
                try:
                    proc = psutil.Process(old_pid)
                    ppid = proc.ppid()
                    if ppid == 1 or not _get_pid_status(ppid):
                        print(f"[qr] scheduler: orphaned process (pid={old_pid}), restarting")
                        proc.terminate()
                        import time; time.sleep(1)
                        update_instance(db_path, instance_id, pid_last_known=None)
                    else:
                        try:
                            transition_state(db_path, instance_id, "deployed")
                        except Exception as _e:
                            logger.debug("transition_state deployed for existing scheduler process failed: %s", _e)
                            pass
                        return {"action": "start", "pid": old_pid,
                                "status": "existing_process_alive"}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    update_instance(db_path, instance_id, pid_last_known=None)

            try:
                cmd = _build_command("scheduler", env_config, api_host, api_port)
            except Exception as exc:
                _log_lifecycle(QR_ENGINE_SCHEDULER_NAME, "start", {"error": str(exc)})
                return {"error": f"Failed to build command: {exc}", "action": command}

            try:
                from lib.lib_system_engine import build_subprocess_env, _register_child
                env = build_subprocess_env(
                    engine_name="scheduler",
                    env_config=env_config,
                    api_host=api_host,
                    api_port=api_port,
                )
                log_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs", "scheduler.log"
                )
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                proc = subprocess.Popen(cmd, stdout=open(log_path, "a"), stderr=subprocess.STDOUT,
                                        env=env, cwd=os.getcwd(), start_new_session=True)
                import ctypes as _ctypes
                _ctypes.CDLL("libc.so.6").prctl(1, 15)
            except OSError as exc:
                _log_lifecycle("scheduler", "start", {"error": str(exc)})
                return {"error": f"Failed to start scheduler: {exc}", "action": command}

            import time as _time; _time.sleep(1)
            retcode = proc.poll()
            if retcode is not None:
                stdout, stderr = proc.communicate()
                _err = (stderr or b"").decode("utf-8", errors="replace").strip()[:500]
                _log_lifecycle("scheduler", "start", {"crashed": True, "returncode": retcode, "error": _err})
                return {"error": f"Scheduler crashed immediately (rc={retcode}): {_err}",
                        "action": command}

            new_pid = proc.pid
            _register_child(new_pid)
            update_instance(db_path, instance_id, pid_last_known=new_pid)
            try:
                cur = get_instance(db_path, instance_id)
                if cur and cur.get("state") == "unconfigured":
                    transition_state(db_path, instance_id, "deployed")
                transition_state(db_path, instance_id, "starting")
                transition_state(db_path, instance_id, "running")
            except Exception as _e:
                logger.debug("state transition chain for scheduler start failed: %s", _e)
                pass
            _log_lifecycle("scheduler", "start", {"pid": new_pid, "api_host": api_host, "api_port": api_port})
            return {"action": "start", "pid": new_pid, "status": "started"}

        elif command == "stop":
            pid = inst.get("pid_last_known")
            if pid and _get_pid_status(pid):
                try:
                    import psutil as _psutil
                    _psutil.Process(pid).terminate()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            update_instance(db_path, instance_id, pid_last_known=None)
            _log_lifecycle("scheduler", "stop", {"pid": pid})
            return {"action": "stop", "pid": pid}

        elif command == "restart":
            try:
                transition_state(db_path, instance_id, "stopping")
            except Exception as _e:
                logger.debug("transition_state stopping for scheduler restart failed: %s", _e)
                pass
            old_pid = inst.get("pid_last_known")
            try:
                update_instance(db_path, instance_id, pid_last_known=None)
            except Exception as _e:
                logger.debug("update_instance pid_last_known=None for scheduler restart failed: %s", _e)
                pass
            if old_pid and _get_pid_status(old_pid):
                try:
                    import psutil as _psutil
                    _psutil.Process(old_pid).kill()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            import time as _time
            timeout = int(env_config.get("QUICKROBOT_SERVER_SPAWN_TIMEOUT", 5))
            deadline = _time.time() + timeout
            while _time.time() < deadline:
                if not _get_pid_status(old_pid):
                    break
                _time.sleep(0.5)
            result = self.execute(instance_id, "start", db_path)
            if old_pid and isinstance(result, dict):
                result["old_pid"] = old_pid
                result["pid_changed"] = result.get("pid") != old_pid
            return result

        raise ValueError(f"Unknown action: {command}")

    def list_resources(self, instance_id, db_path=None):
        """No models or presets for the scheduler."""
        return {"models": [], "presets": []}

    def get_presets(self, engine_type_id, db_path=None):
        """No presets for scheduler."""
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """No presets for scheduler."""
        pass

    def forward_request(self, instance_id, method, params=None, db_path=None):
        """Forward a request — returns scheduler status."""
        return self.get_status(instance_id, db_path)


import subprocess
