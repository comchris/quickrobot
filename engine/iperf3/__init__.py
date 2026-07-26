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

"""quickrobot (v0.04) -- iperf3 Engine implementation.

Provides the iperf3 engine class and its CAPABILITIES metadata for
discovery by the engine loader. Supports two modes via presets:
- server mode: runs iperf3 in listen mode (-s) on a dedicated port
- client mode: runs iperf3 as a one-shot client against a target (-c)
"""

import logging

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, QR_ENGINE_PORT_DEFAULTS, QR_ENGINE_IPERF3_NAME
from engine.base import BaseEngine

logger = logging.getLogger(__name__)
from lib.lib_constants import DEFAULT_ANSIBLE_USER


CAPABILITIES = {
    "name": "iperf3",
    "display_name": "Iperf 3",
    "supports_models": False,
    "supports_presets": True,
    "max_instances": 99,
    "base_port": QR_ENGINE_PORT_DEFAULTS.get("iperf3", 9900),
    "sub_pages": [
        {"path": "/engines/iperf3/config", "label": "Config", "order": 1},
        {"path": "/engines/iperf3/presets", "label": "Presets", "order": 2},
    ],
    "supported_jobs": ["deploy", "restart", "undeploy"],
    "undeploy_chain": [
        {"stage": "stop", "playbook": "service_stop"},
        {"stage": "undeploy", "playbook": "undeploy_iperf3"},
        {"stage": "verify", "playbook": "check_undeploy"},
    ],
}


class Iperf3Engine(BaseEngine):
    """iperf3 engine for network benchmarking instances.

    Instances communicate via iperf3 protocol. Port range: 9900-9904
    (limited to 5 concurrent server listeners per node).
    """

    STATE_EXTENSIONS = {
        "running": ["configuring"],
    }

    def __init__(self):
        self._name = "iperf3"
        self._base_port = CAPABILITIES["base_port"]
        self._max_instances = CAPABILITIES["max_instances"]

    @classmethod
    def get_state_machine(cls):
        """State machine for iperf3 engine (no build states).

        Extends base with "configuring" from running (BC-1: config updates while running).
        No updating/compiling since iperf3 has no cmake build pipeline.
        """
        from lib.lib_engine_states import build_state_machine as _bsm
        return _bsm(cls.STATE_EXTENSIONS)

    def get_status(self, instance_id, db_path=None):
        """Get remote status of an iperf3 instance via systemctl.

        Returns canonical shape: engine, instance_id, unit_name, service_state, error.
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "error": "db_path required for remote get_status"}

        try:
            from lib.lib_engine_status_query import query_systemd_status as _qs
            unit_builder = lambda row: f"qr-{instance_id}-{row['engine_type_name']}"
            return _qs(db_path, self._name, instance_id, unit_builder)
        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "service_state": "unknown", "error": str(exc),
                    "main_pid": None, "memory_mb": None,
                    "restart_count": 0, "service_substate": "error"}

    def _check_remote_service(self, node_host, unit_name, node_user=None):
        """Check remote systemd service and process stats via ansible playbook.

        Delegates to shared lib_engine_health.check_remote_service().

        Args:
            node_host: Hostname or IP of the remote node.
            unit_name: Name of the systemd unit (e.g., 'qr-19-iperf3').
            node_user: SSH username for the remote node.

        Returns:
            dict with keys: service_state, service_substate, main_pid,
                memory_mb, restart_count, error.
        """
        from lib.lib_engine_health import check_remote_service as _check
        return _check(node_host, unit_name, node_user)

    def query_status(self, instance_id, db_path=None):
        """Remote health check via ansible playbook.

        Uses instance_health_check for unified interlock-aware status checks.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with keys: alive (bool), latency_ms (float|None), error (str|None).
        """
        if db_path is None:
            return {"alive": False, "latency_ms": None,
                    "error": "db_path required for remote query_status"}

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                row = conn.execute(
                    """SELECT i.port_assigned, i.state, n.hostname as node_host
                       FROM instances i
                       LEFT JOIN nodes n ON i.node_id = n.id
                       WHERE i.id = ?""",
                    (instance_id,),
                ).fetchone()

            if row is None:
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance {instance_id} not found"}

            node_host = row["node_host"] or QR_DEFAULT_LOCALHOST
            state = row["state"] or "unknown"

            if state not in ("running", "starting", "deployed", "stopped", "error",
                              "updating", "build_error", "configuring", "deploying",
                              "compiling"):
                return {"alive": False, "latency_ms": None,
                        "error": f"Instance not running (state={state})"}

            unit_name = f"qr-{instance_id}-iperf3"
            # Check systemctl is-active via ansible playbook
            from qr_api import _execute_playbook as _ep
            r = _ep("instance_health_check", resolver_type="playbook_id",
                    limit=node_host,
                    extra_vars={"inventory_host": node_host, "unit_name": unit_name},
                    action_type="health_check")

            if r.get("error"):
                return {"alive": False, "latency_ms": None,
                        "error": r["error"]}

            svc_result = r.get("result", {})
            service_state = "unknown"
            for play in svc_result.get("results", {}).get("plays", []):
                for task in play.get("tasks", []):
                    if "Output health check result" in task.get("task", {}).get("name", ""):
                        entry = task.get("results", [{}])[0]
                        msg = entry.get("msg", "{}")
                        try:
                            import json as _json
                            d = _json.loads(msg)
                            service_state = d.get("service_state", "unknown")
                        except Exception as _e:
                            logger.debug("iperf3 health check JSON parse failed: %s", _e)
                            pass

            active = (service_state == "active")
            return {
                "alive": active,
                "latency_ms": None,
                "error": None if active else f"Service inactive on {node_host}",
            }

        except Exception as exc:
            return {"alive": False, "latency_ms": None,
                    "error": str(exc)}

    # set_config, get_config, execute inherited from BaseEngine (shared lib)

    # forward_request inherited from BaseEngine (shared lib)

    @classmethod
    def get_instance_status(cls, db_path, instance_id):
        """Unified status endpoint for iperf3 instances (STATUS-1).

        Delegates to shared build_instance_status() with iperf3-specific extras.
        """
        result = cls.build_instance_status(db_path, instance_id)
        if not result:
            return None

        # Engine-specific: node hostname warning
        if result["engine_data"].get("node_hostname"):
            result["warnings"].append(
                {"type": "info", "message": f"Running on {result['engine_data']['node_hostname']}"}
            )

        return result

    @classmethod
    def build_instance_status(cls, db_path, instance_id):
        """Shared STATUS-1 base response (lib/lib_engine_status.build_instance_status).

        iperf3-specific: merges remote systemd service health into engine_data.
        """
        from lib.lib_engine_status import build_instance_status as _shared_build
        result = _shared_build(cls, db_path, instance_id)
        if not result:
            return None

        # Merge remote systemd service health into engine_data
        from lib.lib_engine_status_query import query_systemd_status as _qs
        svc = _qs(
            db_path, QR_ENGINE_IPERF3_NAME, instance_id,
            unit_name_builder=lambda r: f"qr-{instance_id}-iperf3",
        )
        if svc and not svc.get("error"):
            result["engine_data"].update({
                "service_state": svc.get("service_state"),
                "main_pid": svc.get("main_pid"),
                "memory_mb": svc.get("memory_mb"),
                "restart_count": svc.get("restart_count"),
            })

        return result

    @classmethod
    def _get_available_actions(cls, state):
        """Map instance state to available actions (shared lib module)."""
        from lib.lib_engine_actions import get_action_map
        return get_action_map(CAPABILITIES["name"]).get(state, [])

    def list_resources(self, instance_id, db_path=None):
        """List available iperf3 server instances as connectable targets.

        Queries the DB for running iperf3 instances on the same node
        that could serve as client connection targets.

        Args:
            instance_id: Integer primary key of the instance.
            db_path: Optional database path.

        Returns:
            dict with keys: engine, instance_id, targets (list of available
                server instances with host/port info).
        """
        if db_path is None:
            return {"engine": self._name, "instance_id": instance_id,
                    "targets": []}

        from db.sqlite import pool

        try:
            with pool(db_path) as conn:
                row = conn.execute(
                    "SELECT node_id FROM instances WHERE id = ?",
                    (instance_id,),
                ).fetchone()
                if not row:
                    return {"engine": self._name, "instance_id": instance_id,
                            "targets": []}

                node_id = row["node_id"]

                target_rows = conn.execute(
                    """SELECT i.id, i.name, i.port_assigned, n.hostname as node_host
                       FROM instances i
                       JOIN nodes n ON i.node_id = n.id
                       WHERE i.node_id = ?
                         AND i.engine_type_id IN (
                             SELECT id FROM engine_types WHERE name = 'iperf3'
                         )
                         AND i.state = 'running'
                         AND i.system_managed = 0
                       ORDER BY i.name""",
                    (node_id,),
                ).fetchall()

                targets = []
                for t in target_rows:
                    if t["id"] != instance_id:
                        targets.append({
                            "id": t["id"],
                            "name": t["name"],
                            "host": t["node_host"],
                            "port": t["port_assigned"],
                        })

                return {"engine": self._name, "instance_id": instance_id,
                        "targets": targets}

        except Exception as exc:
            return {"engine": self._name, "instance_id": instance_id,
                    "targets": [], "_error": str(exc)}
