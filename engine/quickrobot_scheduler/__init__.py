# Copyright 2026 comchris quickrobot .de project
# Quickrobot Scheduler Engine — background job/task executor (RUNNER-1).

"""Scheduler engine class for lifecycle management.

The scheduler runs as a subprocess of the API server, polling for queued
jobs and executing staged playbooks via PlaybookRunner.
"""

from engine.base import BaseEngine
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST

CAPABILITIES = {
    "name": "quickrobot-scheduler",
    "display_name": "Scheduler",
    "category": "system",
    "description": "Staged playbook job scheduler — polls for queued tasks and executes stages",
}


class SchedulerEngine(BaseEngine):
    """Engine wrapper for the quickrobot-scheduler subprocess.

    Delegates start/stop/restart to lib_system_engine functions.
    The scheduler does not bind a port; it runs in the background.
    """

    STATE_MACHINE_NAME = "scheduler"

    def __init__(self):
        self.name = CAPABILITIES["name"]
        self.capabilities = CAPABILITIES

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Return scheduler subprocess status (STATUS-1 format).

        Args:
            db_path: Database path.
            instance_id: Instance primary key.

        Returns:
            Status dict in canonical STATUS-1 format.
        """
        eng = cls()
        return eng.get_status(instance_id, db_path)

    # ---- Abstract methods from BaseEngine (minimal implementations) ----

    def get_status(self, instance_id, db_path=None):
        """Return scheduler subprocess status in canonical STATUS-1 format.

        Args:
            instance_id: Integer DB primary key.
            db_path: Database path.

        Returns:
            Dict with id, state, engine_type_name, engine_data,
            actions list, and warnings (canonical STATUS-1 shape).
        """
        from db.adapters.instances import get_instance as _gi

        inst = _gi(db_path, instance_id) if db_path else None
        pid = inst.get("pid_last_known") if inst else None

        running = False
        uptime_seconds = 0
        rss_bytes = 0
        if pid:
            try:
                import psutil as _ps
                proc = _ps.Process(pid)
                if proc.status() != "zombie":
                    running = True
                    uptime_seconds = int(__import__("time").time() - proc.create_time())
                    rss_bytes = proc.memory_info().rss
            except Exception:
                pass

        return {
            "id": instance_id,
            "state": "running" if running else (inst.get("state") if inst else "stopped"),
            "engine_type_name": self.name,
            "engine_data": {
                "pid": pid if running else None,
                "uptime_seconds": uptime_seconds,
                "rss_bytes": rss_bytes,
            },
            "actions": [{"name": "restart", "label": "Restart"}],
            "warnings": [],
            "_meta": {"valid_next_states": ["stopping", "starting"], "is_transitioning": False},
        }

    def query_status(self, instance_id, db_path=None):
        """Alias for get_status."""
        return self.get_status(instance_id, db_path)

    def set_config(self, instance_id, config_dict, db_path=None):
        """No-op — scheduler config is in engine_configs table."""
        return {"status": "ok", "action": "set_config"}

    def get_config(self, instance_id, db_path=None):
        """Return scheduler config from engine_configs table."""
        return {}

    def list_resources(self, instance_id, db_path=None):
        """Return empty list — scheduler has no external resources."""
        return []

    def get_presets(self, engine_type_id, db_path=None):
        """Return empty list — scheduler has no presets."""
        return []

    def set_active_preset(self, instance_id, preset_id, db_path=None):
        """No-op — scheduler has no presets."""
        return {"status": "ok"}

    def forward_request(self, instance_id, method, params=None, db_path=None):
        """No-op — scheduler doesn't proxy HTTP requests."""
        return {"error": "Scheduler does not forward HTTP requests"}

    # ---- Lifecycle management ----

    def execute(self, instance_id, action, db_path=None, **kwargs):
        """Start/stop/restart the scheduler subprocess.

        Args:
            instance_id: Instance DB ID (4 for system scheduler).
            action: "start", "stop", or "restart".
            db_path: Database path.
            **kwargs: Additional parameters.

        Returns:
            dict with action result, pid, and status.
        """
        import time as _time
        from lib.lib_system_engine import (start_system_engine, stop_system_engine,
                                          load_env_config, _get_pid_status)
        from db.adapters.instances import get_instance, update_instance
        import os

        if db_path is None:
            db_path = os.path.join(os.getcwd(), "data", "quickrobot.db")

        inst = get_instance(db_path, instance_id)
        if not inst:
            return {"error": "instance not found", "action": action}

        # Load env config (single source of truth for system engines)
        try:
            env_config = load_env_config(os.getcwd())
        except FileNotFoundError as exc:
            return {"error": str(exc), "action": action}

        api_host = env_config.get("QUICKROBOT_API_HOST", QR_DEFAULT_LOCALHOST)
        raw_port = env_config.get("QUICKROBOT_API_PORT")
        if not raw_port:
            raise KeyError("QUICKROBOT_API_PORT not in .quickrobot.env")
        api_port = int(raw_port)

        if action == "start":
            # Check for existing live process via stored PID
            old_pid = inst.get("pid_last_known")
            if old_pid and _get_pid_status(old_pid):
                # REG-03-F1: Also scan by name — the old PID might be an orphan
                # (PPID=1) from a previous API that survived PDEATHSIG.
                try:
                    import psutil as _psutil
                    proc = _psutil.Process(old_pid)
                    ppid = proc.ppid()
                    if ppid == 1:
                        # Orphaned — kill and proceed to fresh start
                        print(f"[qr] scheduler: orphaned process (pid={old_pid}), killing and restarting")
                        try:
                            proc.kill()
                        except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                            pass
                    else:
                        return {"action": "start", "port": 0, "pid": old_pid,
                                "status": "existing_process_alive"}
                except Exception:
                    return {"action": "start", "port": 0, "pid": old_pid,
                            "status": "existing_process_alive"}

            result = start_system_engine(
                engine_name="scheduler",
                env_config=env_config,
                api_host=api_host,
                api_port=api_port,
            )
            return result

        elif action == "stop":
            pid = inst.get("pid_last_known")
            if pid and _get_pid_status(pid):
                try:
                    import psutil as _psutil
                    _psutil.Process(pid).terminate()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            update_instance(db_path, instance_id, pid_last_known=None)
            return {"action": "stop", "pid": pid}

        elif action == "restart":
            old_pid = inst.get("pid_last_known")

            # Clear PID from DB FIRST — prevents race condition where stale PID
            # is detected as "running" during the kill window
            try:
                update_instance(db_path, instance_id, pid_last_known=None)
            except Exception:
                pass

            # Step 1: Kill existing process (SIGKILL for immediate death)
            if old_pid and _get_pid_status(old_pid):
                try:
                    import psutil as _psutil
                    _psutil.Process(old_pid).kill()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass

            # Step 2-3: Wait for process to die (REG-03-F1: increased timeout + double-SIGKILL)
            timeout = int(env_config.get("QUICKROBOT_SERVER_SPAWN_TIMEOUT", 10))
            deadline = _time.time() + timeout
            dead_verified = False
            while _time.time() < deadline:
                if not _get_pid_status(old_pid):
                    dead_verified = True
                    break
                _time.sleep(0.5)

            if not dead_verified:
                # Double-SIGKILL: first attempt may have been missed (e.g., syscall block)
                print(f"[qr] scheduler restart: old PID {old_pid} didn't exit within {timeout}s, double-SIGKILL")
                try:
                    import psutil as _psutil
                    if _get_pid_status(old_pid):
                        _psutil.Process(old_pid).kill()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
                # Wait another 3s for the double-SIGKILL to take effect
                deadline2 = _time.time() + 3
                while _time.time() < deadline2:
                    if not _get_pid_status(old_pid):
                        dead_verified = True
                        break
                    _time.sleep(0.5)
                if not dead_verified:
                    print(f"[qr] scheduler restart: PID {old_pid} survived double-SIGKILL, proceeding anyway")

            # Step 4-5: Start new process
            result = start_system_engine(
                engine_name="scheduler",
                env_config=env_config,
                api_host=api_host,
                api_port=api_port,
            )
            if result.get("status") == "started":
                return {
                    "action": "restart",
                    "pid": result.get("pid"),
                    "port": 0,
                    "old_pid": old_pid,
                    "dead_verified": dead_verified,
                    "status": "restart_success",
                }
            return result

        return {"error": f"Unknown action: {action}", "action": action}
