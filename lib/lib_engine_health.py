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

"""Shared engine health check function for systemd service monitoring.

Extracted from engine __init__.py files to eliminate duplication across
llama_server, llama_rpc, and iperf3 engines.

All three engines use _check_remote_service() identically — they query
the instance_health_check playbook via ansible and parse the JSON output
containing systemd state, PID, memory usage, and restart count.
"""


def check_remote_service(node_host, unit_name, node_user=None):
    """Check remote systemd service and process stats via ansible playbook.

    Uses instance_health_check playbook for unified, interlock-aware health checks.

    Args:
        node_host: Hostname or IP of the remote node.
        unit_name: Name of the systemd unit (e.g., 'qr-19-rpc').
        node_user: SSH username for the remote node (defaults to DEFAULT_ANSIBLE_USER).

    Returns:
        dict with keys: service_state, service_substate, main_pid,
            memory_mb, restart_count, error.
    """
    import json as _json

    try:
        from qr_api import _execute_playbook as _ep
        r = _ep("instance_health_check", resolver_type="playbook_id",
                limit=node_host,
                extra_vars={"inventory_host": node_host, "unit_name": unit_name},
                action_type="health_check")

        if r.get("error"):
            return {
                "service_state": "unknown", "service_substate": "ansible_error",
                "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
                "error": r["error"],
            }

        # Parse JSON from playbook debug msg
        svc_result = r.get("result", {})
        json_str = ""
        for play in svc_result.get("results", {}).get("plays", []):
            for task in play.get("tasks", []):
                if "Output health check result" in task.get("task", {}).get("name", ""):
                    entry = task.get("results", [{}])[0]
                    json_str = entry.get("msg", "")

        if not json_str:
            return {
                "service_state": "unknown", "service_substate": "no_output",
                "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
                "error": "Playbook returned no output",
            }

        data = _json.loads(json_str)
        memory_kb = int(data.get("memory_kb", 0))
        main_pid = int(data["main_pid"]) if data.get("main_pid") and data["main_pid"] not in ("0",) else None

        error = None
        state = data.get("service_state", "unknown")
        if state == "unknown" and main_pid is None:
            error = f"Service {unit_name} not found on {node_host}"

        return {
            "service_state": state,
            "service_substate": data.get("sub_state", "unknown"),
            "main_pid": main_pid,
            "memory_mb": round(memory_kb / 1024, 2) if memory_kb else 0.0,
            "restart_count": int(data.get("restart_count", 0)),
            "error": error,
        }

    except _json.JSONDecodeError:
        return {
            "service_state": "unknown", "service_substate": "parse_error",
            "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
            "error": f"Failed to parse playbook output: {json_str!r}",
        }
    except Exception as exc:
        return {
            "service_state": "unknown", "service_substate": "error",
            "main_pid": None, "memory_mb": 0.0, "restart_count": 0,
            "error": str(exc),
        }
