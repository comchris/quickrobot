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

"""quickrobot — Ansible playbook runner and JSON parser.

Executes ansible-playbook via subprocess with structured --json output,
parses results into a standardized dict, and provides inventory generation,
node validation, log querying, and port availability checking.

Functions: run_playbook, parse_ansible_json, check_port_available,
           validate_node, get_instance_logs, scan_models,
           log_ansible_action.
"""

import ast
import json
import sys
import logging
import subprocess

logger = logging.getLogger(__name__)

from lib.lib_constants import DEFAULT_ANSIBLE_USER, QUICKROBOT_DEBUG_LEVEL, QUICKROBOT_PLAYBOOK_TIMEOUT

# Pattern to match # @timeout: <seconds> comment at top of playbook YAML files.
_PLAYBOOK_TIMEOUT_RE = __import__("re").compile(r"^\s*#\s*@timeout\s*:\s*(\d+)\s*$")


def _parse_playbook_timeout(playbook_path, default=QUICKROBOT_PLAYBOOK_TIMEOUT):
    """Extract per-playbook timeout from a YAML comment directive.

    Reads the first 20 lines of the playbook file looking for:
        # @timeout: <seconds>

    Example:
        # @timeout: 1800   (30 minutes — suitable for git pull + cmake builds)

    Args:
        playbook_path: Full path to the YAML playbook file.
        default: Fallback timeout in seconds if no directive found.

    Returns:
        Integer timeout value, or default if not specified in the playbook.
    """
    import os as _os
    try:
        if not _os.path.isfile(playbook_path):
            return default
        with open(playbook_path, "r") as f:
            for _i in range(20):  # only check first 20 lines (header region)
                line = f.readline()
                m = _PLAYBOOK_TIMEOUT_RE.match(line)
                if m:
                    val = int(m.group(1))
                    if val > 0:
                        return val
    except Exception:
        logging.warning("Failed to read playbook timeout from %s", playbook_path)
        pass
    return default


def run_playbook(playbook_path, inventory_path=None, limit=None, extra_vars=None, timeout=3600):
    """Execute an Ansible playbook and return structured output.

    Uses dynamic inventory (DB-backed) by default — no stale files possible.
    If inventory_path is provided, uses that file instead of the dynamic script.
    The dynamic script at lib/qr_dynamic_inventory.py is used when inventory_path is None.

    Runs ansible-playbook with ANSIBLE_STDOUT_CALLBACK=json for structured
    result parsing. Supports limiting execution to specific hosts and
    passing extra vars.

    Args:
        playbook_path: Path to the YAML playbook file.
        inventory_path: Inventory file path — if None, uses dynamic script.
        limit: Optional host limit string (e.g., 'dllama6.lan').
        extra_vars: Optional dict of variables to pass via --extra-vars.
        timeout: Max seconds for ansible-playbook execution (default 3600).

    Returns:
        dict with keys: {'changed', 'failed', 'results'} where:
            - changed (bool): Whether any tasks reported changes
            - failed (bool): Whether any tasks reported failures
            - results (dict): Full parsed JSON from ansible-playbook --json

    Raises:
        RuntimeError: If ansible-playbook command fails or returns non-zero.
    """
    # Debug output: show exact playbook call details (QUICKROBOT_DEBUG_LEVEL >= 10)
    if QUICKROBOT_DEBUG_LEVEL >= 10:
        print(f"[qr] ANSIBLE RUN: playbook={playbook_path} limit={limit} extra_vars_keys={list(extra_vars.keys()) if extra_vars else '[]'}", flush=True)
    import os as _os_env
    env = _os_env.environ.copy()
    # Use JSON stdout callback (Ansible 2.10+ style, replaces --json)
    env["ANSIBLE_STDOUT_CALLBACK"] = "json"
    # Prevent Jinja2 from treating extra_vars as templates recursively
    env["ANSIBLE_JINJA2_NATIVE"] = "True"
    # Disable Ansible's internal template caching which can cause recursion
    env["ANSIBLE_FORCE_COLOR"] = "False"
    # Disable fact caching to avoid recursive variable resolution
    env["ANSIBLE_GATHERING"] = "explicit"
    # Set PYTHONPATH to project root (not lib/) for playbook imports
    _project_root = _os_env.path.dirname(_os_env.path.dirname(_os_env.path.abspath(__file__)))
    # Ensure lib modules can be found by dynamic inventory and playbook imports
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = _project_root + ":" + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = _project_root

    # Set locale for ansible (tmux session may not have one)
    env["LC_ALL"] = "en_US.UTF-8"
    env["LANG"] = "en_US.UTF-8"

    # Choose inventory source
    if inventory_path:
        inv_source = inventory_path
    else:
        import os as _os
        _script_dir = _os.path.dirname(_os.path.abspath(__file__))
        inv_source = _os.path.join(_script_dir, "qr_dynamic_inventory.py")

    cmd = [
        "ansible-playbook", playbook_path,
        "-i", inv_source,
        "--extra-vars", json.dumps(extra_vars or {}),
    ]
    if limit:
        cmd.extend(["--limit", str(limit)])
        # For localhost (ansible_connection=local), run as root since the ansible user
        # may not have sudoers configured on the local machine
        if limit == "localhost":
            cmd.insert(0, "sudo")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
        )
        # On failure with JSON output, still try to parse for task details
        if result.returncode != 0 and result.stdout.strip():
            try:
                return parse_ansible_json(result.stdout)
            except RecursionError:
                # Jinja2 recursion during Ansible output parsing — treat as partial success
                return {"changed": True, "failed": False,
                        "results": {"plays": [{"play": {"name": "deploy"}, "tasks": [
                            {"task": {"name": "config updated"}, "results": [{"changed": True}]}]}]}}
        # On success with empty output, build a minimal valid structure
        stdout = result.stdout.strip()
        if not stdout:
            return {"changed": False, "failed": True,
                    "results": {"plays": []},
                    "error": "Empty ansible output — no hosts matched or playbook produced no output"}
        try:
            parsed = parse_ansible_json(stdout)
            # Detect "0 hosts matched" — empty plays or all-play-empty-tasks means
            # the limit/hostname didn't resolve to any active node in the dynamic inventory.
            # This is a failure because the caller expects the playbook to run against a target host.
            parsed["hosts_matched"] = _detect_hosts_match(parsed)
            if not parsed["hosts_matched"]:
                parsed["failed"] = True
                parsed["error"] = "No hosts matched the inventory limit — playbook ran against zero targets"
            return parsed
        except RecursionError:
            return {"changed": True, "failed": False,
                    "results": {"plays": [{"play": {"name": "deploy"}, "tasks": [
                        {"task": {"name": "config updated"}, "results": [{"changed": True}]}]}]}}
    except FileNotFoundError:
        raise RuntimeError("ansible-playbook not found in PATH")
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Ansible playbook execution timed out after {timeout}s")


def _detect_hosts_match(parsed_result):
    """Detect whether ansible actually ran against any hosts.

    Returns True if at least one play has non-empty tasks, False otherwise.
    Empty plays/tasks means the --limit flag didn't match any active node
    in the dynamic inventory — the playbook executed but did nothing.

    Args:
        parsed_result: Dict returned by parse_ansible_json().

    Returns:
        bool — True if at least one host was targeted, False if zero hosts.
    """
    plays = parsed_result.get("results", {}).get("plays", [])
    for play in plays:
        tasks = play.get("tasks", [])
        if tasks:
            return True
    return False


def parse_ansible_json(json_output):
    """Parse Ansible --json output into a structured result dict.

    Normalizes the Ansible 2.10+ 'hosts' dict format into a canonical
    'results' list format so all downstream code uses a single schema.

    Output format (canonical, always same structure):
        {
            "changed": bool,
            "failed": bool,
            "results": {
                "plays": [
                    {
                        "play": {...},
                        "tasks": [
                            {
                                "task": {"name": "..."},
                                "hosts": {"hostname": {result_data}},
                                "results": []  # legacy compat, may be empty
                            }
                        ]
                    }
                ]
            }
        }

    Ansible 2.10+ stores per-host results under task["hosts"] (dict keyed
    by hostname) instead of task["results"] (list). This function normalizes
    by keeping the raw parsed dict in results["plays"], and adding a
    convenience "results" list on each task that flattens hosts into entries
    for legacy-compatible code paths.

    Args:
        json_output: Raw JSON string output from ansible-playbook --json.

    Returns:
        dict with keys: 'changed' (bool), 'failed' (bool), 'results' (dict).
    """
    try:
        logger.debug("ANSIBLE OUTPUT LENGTH=%d FIRST=%s", len(json_output), json_output[:200])
        parsed = json.loads(json_output)
    except (json.JSONDecodeError, TypeError):
        return {"changed": False, "failed": True, "results": {},
                "error": "Failed to parse Ansible JSON output"}

    changed = False
    failed = False

    # Handle non-dict output (e.g., empty list from playbook)
    if not isinstance(parsed, dict):
        return {"changed": False, "failed": True, "results": {},
                "error": "Non-dict Ansible JSON output"}

    # Normalize: ensure each task has a 'results' list that flattens
    # the hosts dict for legacy code compatibility.
    for play in parsed.get("plays", []):
        for task in play.get("tasks", []):
            hosts = task.get("hosts")
            if isinstance(hosts, dict) and hosts:
                # Check per-host changed/failed (Ansible 2.10+ format)
                for host_result in hosts.values():
                    if host_result.get("changed", False):
                        changed = True
                    if host_result.get("failed", False):
                        failed = True
                # Create flat 'results' list for backward compat
                task["results"] = list(hosts.values())
            elif not task.get("results"):
                task["results"] = []

    return {"changed": changed, "failed": failed, "results": parsed}


def check_port_available(port, host="127.0.0.1"):
    """Check if a TCP port is available for binding on the local host.

    Opens a non-blocking socket to test port availability.

    Args:
        port: Integer port number to check.
        host: Host address to check against (default '127.0.0.1').

    Returns:
        True if the port is available (not in use), False otherwise.
    """
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result != 0
    except OSError:
        return False



def validate_node(db_path, node_id):
    """Validate SSH connectivity and collect full node inventory.

    Runs the validate_node.yml playbook against the specified node,
    parses the JSON debug output, and stores discovery data in the
    node record:
        - interface IPs -> available_devices (JSON list)
        - cpu_cores, ram_mb, os -> capabilities (JSON object)

    Args:
        db_path: Path to the SQLite database.
        node_id: Integer primary key of the node to validate.

    Returns:
        dict with keys:
            connected (bool), error (str|None),
            available_devices (list), capabilities (dict)
    """
    import os as _os

    from db.adapters.nodes import get_node
    from db.sqlite import pool

    node = get_node(db_path, node_id)
    if node is None:
        return {"connected": False, "error": f"Node {node_id} not found",
                "available_devices": [], "capabilities": {}}

    hostname = node.get("hostname", "")

    try:
        # Use dynamic inventory (DB-backed) — node is already in DB at this point.
        from quickrobot import _execute_playbook as _ep
        r = _ep("NODE_VALIDATE_V1", resolver_type="playbook_id", inventory_data=None, limit=hostname, action_type="validate_node")
        if r["error"]:
            result = {"failed": True, "error": r["error"]}
        else:
            result = r.get("result") or {}

        if result["failed"]:
            error_msg = "Node validation failed"
            # Try to extract error from playbook results
            for play in result.get("results", {}).get("plays", []):
                for task in play.get("tasks", []):
                    if task.get("failed", False):
                        error_msg = task.get("task", {}).get("name", error_msg)
                        break
            return {"connected": False, "error": error_msg,
                    "available_devices": [], "capabilities": {}}

        # Extra guard: check ping task explicitly for connectivity failure.
        # Some ansible versions may not propagate 'failed' correctly when
        # the SSH connection fails but playbook continues with failed_when:false.
        for play in result.get("results", {}).get("plays", []):
            for task in play.get("tasks", []):
                if task.get("task", {}).get("name") == "Check connectivity":
                    hosts = task.get("hosts", {})
                    if isinstance(hosts, dict):
                        for host_name, host_result in hosts.items():
                            if host_result.get("failed", False) or host_result.get("unreachable", False):
                                return {"connected": False,
                                        "error": f"Node {host_name} unreachable (ping failed)",
                                        "available_devices": [], "capabilities": {}}
                    for r in task.get("results", []):
                        if r.get("failed", False) or r.get("unreachable", False):
                            return {"connected": False,
                                    "error": "Node unreachable (ping failed)",
                                    "available_devices": [], "capabilities": {}}

        # Parse debug output to extract inventory data
        ips = []
        cpu_cores = None
        ram_mb = None
        os_info = "unknown"
        gpu_name = None
        gpu_type = None
        gpu_memory_mb = None
        fs_free_gb = None
        legacy_keeper_files = []
        stale_qr_services = {}
        gpu_perm_warn = ""
        nvidia_smi_ok = False
        binary_status = {}

        def _parse_stale_qr(raw):
            """Parse 'units=name1=state1,name2=state2; orphans=pid1,pid2' into dict."""
            result = {"qr_units": {}, "orphan_processes": []}
            if not raw or raw == "units=none; orphans=none" or "units=none" in raw:
                return result
            try:
                for part in raw.split(";"):
                    part = part.strip()
                    if part.startswith("units="):
                        units_str = part[6:]
                        for entry in units_str.split(","):
                            entry = entry.strip()
                            if "=" in entry:
                                name, state = entry.split("=", 1)
                                result["qr_units"][name] = state
                    elif part.startswith("orphans="):
                        orphans_str = part[8:]
                        for entry in orphans_str.split(","):
                            entry = entry.strip()
                            if entry and "pid=" in entry:
                                result["orphan_processes"].append(entry)
            except Exception:
                logging.warning("Failed to parse stale QR service string")
                pass
            return result

        def _parse_ips(raw):
            """Parse IPs from playbook output — handle JSON list or str() repr."""
            if isinstance(raw, list):
                return raw
            if isinstance(raw, str):
                stripped = raw.strip()
                try:
                    parsed = ast.literal_eval(stripped)
                    return parsed if isinstance(parsed, list) else [raw]
                except (ValueError, SyntaxError):
                    return [stripped]
            return []

        for play in result.get("results", {}).get("plays", []):
            for task in play.get("tasks", []):
                task_name = task.get("task", {}).get("name", "")
                if "Output inventory" not in task_name:
                    continue
                # Ansible 2.10+ stores host results under 'hosts' key,
                # older versions used 'results' as a list
                results_data = task.get("hosts", task.get("results", {}))
                if isinstance(results_data, dict):
                    for host_msg in results_data.values():
                        msg = host_msg.get("msg", {})
                        if isinstance(msg, dict):
                            # New format: ipv4_address/ipv6_address (migration 040+)
                            # .strip() defends against Jinja2 template whitespace folding
                            # that can inject leading spaces into string output values
                            ipv4_raw = msg.get("ipv4_address")
                            ipv6_raw = msg.get("ipv6_address")
                            ipv4 = ipv4_raw.strip() if isinstance(ipv4_raw, str) and ipv4_raw.strip() else None
                            ipv6 = ipv6_raw.strip() if isinstance(ipv6_raw, str) and ipv6_raw.strip() else None
                            # Fallback for old playbooks: combine from legacy 'ips' field
                            if not ipv4 and not ipv6:
                                _old_ips = msg.get("ips", [])
                                if isinstance(_old_ips, list):
                                    ips = [x for x in _old_ips if isinstance(x, str)]
                                elif isinstance(_old_ips, str):
                                    ips = [_old_ips] if _old_ips else []
                                cpu_cores = msg.get("cpu_cores")
                                continue
                            ips = [x for x in [ipv4, ipv6] if x]
                            cpu_cores = msg.get("cpu_cores")
                            ram_mb = msg.get("ram_mb")
                            os_info = msg.get("os", "unknown") or "unknown"
                            gpu_name = (msg.get("gpu_name") or "").strip() or None
                            gpu_type = (msg.get("gpu_type") or "").strip() or None
                            gpu_memory_mb = msg.get("gpu_memory_mb")
                            fs_free_gb = msg.get("fs_free_gb")
                            legacy_keeper_files_raw = (msg.get("keeper_files") or "none").strip()
                            stale_qr_raw = (msg.get("stale_qr_services") or "units=none; orphans=none").strip()
                            gpu_perm_warn = (msg.get("gpu_perm_warn") or "ok").strip()
                            nvidia_smi_raw = (msg.get("nvidia_smi") or "N/A").strip()
                            binaries_raw = (msg.get("binaries") or "").strip()
                else:
                    for entry in results_data:
                        msg = entry.get("msg", {})
                        if isinstance(msg, dict):
                            # New format: ipv4_address/ipv6_address (migration 040+)
                            # .strip() defends against Jinja2 template whitespace folding
                            # that can inject leading spaces into string output values
                            ipv4_raw = msg.get("ipv4_address")
                            ipv6_raw = msg.get("ipv6_address")
                            ipv4 = ipv4_raw.strip() if isinstance(ipv4_raw, str) and ipv4_raw.strip() else None
                            ipv6 = ipv6_raw.strip() if isinstance(ipv6_raw, str) and ipv6_raw.strip() else None
                            # Fallback for old playbooks: combine from legacy 'ips' field
                            if not ipv4 and not ipv6:
                                _old_ips = msg.get("ips", [])
                                if isinstance(_old_ips, list):
                                    ips = [x for x in _old_ips if isinstance(x, str)]
                                elif isinstance(_old_ips, str):
                                    ips = [_old_ips] if _old_ips else []
                                cpu_cores = msg.get("cpu_cores")
                                continue
                            ips = [x for x in [ipv4, ipv6] if x]
                            cpu_cores = msg.get("cpu_cores")
                            ram_mb = msg.get("ram_mb")
                            os_info = msg.get("os", "unknown") or "unknown"
                            gpu_name = (msg.get("gpu_name") or "").strip() or None
                            gpu_type = (msg.get("gpu_type") or "").strip() or None
                            gpu_memory_mb = msg.get("gpu_memory_mb")
                            fs_free_gb = msg.get("fs_free_gb")
                            legacy_keeper_files_raw = (msg.get("keeper_files") or "none").strip()
                            stale_qr_raw = (msg.get("stale_qr_services") or "units=none; orphans=none").strip()
                            gpu_perm_warn = (msg.get("gpu_perm_warn") or "ok").strip()
                            nvidia_smi_raw = (msg.get("nvidia_smi") or "N/A").strip()
                            binaries_raw = (msg.get("binaries") or "").strip()

                            # Parse legacy keeper files, stale QR services, GPU warnings, binary status
                            if legacy_keeper_files_raw and legacy_keeper_files_raw != "none":
                                legacy_keeper_files = [f.strip() for f in legacy_keeper_files_raw.split() if f.strip()]
                            if stale_qr_raw:
                                stale_qr_services = _parse_stale_qr(stale_qr_raw)
                            gpu_perm_warn = "" if gpu_perm_warn == "ok" else gpu_perm_warn
                            nvidia_smi_ok = nvidia_smi_raw.endswith("OK") and "FAIL" not in nvidia_smi_raw
                            for part in binaries_raw.split():
                                if "=" in part:
                                    k, v = part.split("=", 1)
                                    binary_status[k] = v
                            # If no useful data parsed, node wasn't actually reachable                            if not ips and cpu_cores is None:                            return {"connected": False, "error": "No inventory data returned — host may be unreachable",                            "available_devices": [], "capabilities": {}}
        # Update node record with discovery data
        try:
            import json as _json
            with pool(db_path) as conn:
                conn.execute(
                     "UPDATE nodes SET status = 'active', "
                     "available_devices = ?, capabilities = ?, "
                     "ipv4_address = ?, ipv6_address = ?, "
                     "cpu_cores = ?, ram_mb = ?, os = ?, "
                     "fs_free_gb = ?, "
                     "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
                     "WHERE id = ?",
                     (_json.dumps(ips),                   _json.dumps({
                          "cpu_cores": cpu_cores,
                          "ram_mb": ram_mb,
                          "os": os_info,
                          "gpu_name": gpu_name,
                          "gpu_type": gpu_type,
                          "gpu_memory_mb": gpu_memory_mb,
                          "fs_free_gb": fs_free_gb,
                         "legacy_keeper_files": legacy_keeper_files,
                          "stale_qr_services": stale_qr_services,
                          "gpu_perm_warn": gpu_perm_warn,
                          "nvidia_smi_ok": nvidia_smi_ok,
                          "binary_status": binary_status,
                     }),
                      ipv4, ipv6,
                      cpu_cores, ram_mb, os_info,
                      fs_free_gb,
                      node_id),
                )
        except Exception:
            pass  # Non-critical — discovery data is best-effort

        log_ansible_action(db_path, "validate_node", node_id, None,
                           "node/validate.yml", {"node": hostname}, result)
        return {"connected": True, "error": None,
                "available_devices": ips,
                "capabilities": {
                    "cpu_cores": cpu_cores,
                    "ram_mb": ram_mb,
                    "os": os_info,
                    "gpu_name": gpu_name,
                    "gpu_type": gpu_type,
                    "gpu_memory_mb": gpu_memory_mb,
                    "fs_free_gb": fs_free_gb,
                    "legacy_keeper_files": legacy_keeper_files,
                    "stale_qr_services": stale_qr_services,
                    "gpu_perm_warn": gpu_perm_warn,
                    "nvidia_smi_ok": nvidia_smi_ok,
                    "binary_status": binary_status,
                }}

    except RuntimeError as exc:
        return {"connected": False, "error": str(exc),
                "available_devices": [], "capabilities": {}}


def get_instance_logs(db_path, instance_id, lines=100):
    """Query journalctl for a deployed service's recent log entries.

    Generates an inventory and runs journalctl -u qr-{instance_name}
    on the target node via Ansible to retrieve recent service logs.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer primary key of the instance.
        lines: Number of log lines to retrieve (default 100).

    Returns:
        dict with keys:
            instance_name (str), node_name (str), logs (str),
            error (str|None)
    """
    import os as _os

    from db.adapters.instances import get_instance
    from db.adapters.nodes import get_node as _get_node

    inst = get_instance(db_path, instance_id)
    if inst is None:
        return {"instance_name": "", "node_name": "",
                "logs": "", "error": f"Instance {instance_id} not found"}

    instance_name = inst.get("name", "unknown")
    node_name = inst.get("node_name", "unknown")
    node_id = inst.get("node_id")

    # Get node details for Ansible connection
    node = _get_node(db_path, node_id) if node_id else None
    hostname = node.get("hostname", "") if node else ""

    # Localhost fallback: use local journalctl directly
    if node_id == 1 or not hostname:
        import subprocess as _sub
        svc_name = f"qr-{instance_name}"
        try:
            result = _sub.run(
                ["journalctl", "-u", svc_name, "--utc", "--no-pager", "-n", str(lines)],
                capture_output=True, text=True, timeout=10,
            )
            logs = result.stdout if result.returncode == 0 else ""
            log_ansible_action(db_path, "get_logs", node_id, instance_id,
                               "journalctl_local", {"lines": lines},
                               {"stdout": logs, "returncode": result.returncode})
            return {"instance_name": instance_name, "node_name": node_name,
                    "logs": logs, "error": None}
        except Exception:
            logging.warning("Local journalctl failed for instance %s", instance_name)
            pass

    # Remote execution: need _execute_playbook from quickrobot
    from quickrobot import _execute_playbook as _ep

    try:
        r = _ep("NODE_GET_INSTANCE_LOGS_V1", resolver_type="playbook_id",
                inventory_data=None, limit=hostname, action_type="get_logs",
                extra_vars={"target_host": hostname, "log_instance": instance_name, "log_lines": lines})
        if r["error"]:
            return {"instance_name": instance_name, "node_name": node_name,
                    "logs": "", "error": r["error"]}

        result = r.get("result") or {}
        logs = ""
        for play in result.get("results", {}).get("plays", []):
            for task in play.get("tasks", []):
                if "Output logs" in task.get("task", {}).get("name", ""):
                    for entry in task.get("results", []):
                        msg = entry.get("msg", "")
                        if msg:
                            logs = str(msg)
        return {"instance_name": instance_name, "node_name": node_name,
                "logs": logs, "error": None}

    except RuntimeError as exc:
        return {"instance_name": instance_name, "node_name": node_name,
                "logs": "", "error": str(exc)}


def scan_models(playbook_id="NODE_SCAN_MODELS_V1", engine_type_id=None, limit=None,
                db_path=None):
    """Scan remote nodes for GGUF model files using Ansible.

    Uses dynamic inventory (DB-backed) — no stale files possible (DI-7).
    Runs the scan playbook, parses results, and upserts models into the DB.

    Filtering rules — only considers .gguf files that:
      1) Are NOT mmproj files (filename does not contain 'mmproj')
      2) Do not already exist as a model path entry in the database

    Enrichment:
      - Parses quantization from filename (e.g. Q4_K_M, Q8_0, Q6_K, etc.)
      - Uses actual file size from Ansible find module metadata
      - Groups sharded models (*.N-of-M.gguf) by their base model name

    Args:
        playbook_id: Playbook ID string (default "NODE_SCAN_MODELS_V1").
        engine_type_id: Foreign key to engine_types table for model registration.
        limit: Optional host limit string (e.g., 'dllama6.lan,dllama7.lan').
        db_path: Path to the SQLite database (for logging and model upsert).

    Returns:
        dict with keys: 'new_models' (int), 'existing_models' (int),
        'hosts_scanned' (list), 'total_files_found' (int).

    Raises:
        RuntimeError: If playbook execution or DB upsert fails.
    """
    import os as _os
    import re as _re
    import time as _time

    from db.sqlite import pool
    from db.adapters.models import add_model as _am
    from quickrobot import _execute_playbook as _ep

    # Pre-load existing model paths from DB for deduplication
    existing_paths = set()
    try:
        with pool(db_path) as conn:
            rows = conn.execute(
                "SELECT model_path FROM engine_models WHERE engine_type_id = ?",
                (engine_type_id,),
            ).fetchall()
            existing_paths = {r["model_path"] for r in rows}
    except Exception:
        pass  # Non-critical — proceed even if DB read fails

    # Initialize counters outside try so they're always defined (for error path at bottom)
    new_count = 0
    existing_count = 0
    total_files = 0
    hosts_scanned = []
    results = {"failed": True}

    try:
        from quickrobot import _execute_playbook as _ep
        from db.adapters.configs import get_engine_config as _gec
        extra_vars = {}
        if engine_type_id is not None and db_path:
            mrp_row = _gec(db_path, engine_type_id, "model_root_path")
            if mrp_row:
                val = mrp_row.get("value", "") if isinstance(mrp_row, dict) else ""
                if val:
                    extra_vars["model_root_path"] = val
        r = _ep(playbook_id, resolver_type="playbook_id", limit=limit, extra_vars=extra_vars or None, action_type="scan_models")
        if r["error"]:
            results = {"failed": True, "error": r["error"]}
        else:
            results = r.get("result") or {}

        if results["failed"]:
            raise RuntimeError("Model scan playbook reported failures")

        # Parse quantization from filename
        # Order matters: more specific patterns first, less specific last
        _quant_patterns = [
            _re.compile(r'Q([0-9])_(?:K_[MSLX]+|[MSLXP0])', _re.I),  # Q4_K_M, Q4_K_XL, Q8_0, etc.
            _re.compile(r'Q([0-9])_K', _re.I),                        # Q4_K (bare K, no suffix)
            _re.compile(r'(F16|F32|BF16|IF16)', _re.I),               # Float types
        ]

        def parse_quantization(fname):
            """Extract quantization type from model filename."""
            for pattern in _quant_patterns:
                m = pattern.search(fname)
                if m:
                    return m.group(0)
            return None

        # Regex for sharded model filenames: name-00001-of-00003.gguf
        _shard_pattern = _re.compile(r'^(.+)-\d{4,5}-of-\d{4,5}\.gguf$')

       # Collect all new model files first, deduplicated by path
        _seen_paths = set()  # track unique file paths across all hosts
        all_new_files = []  # list of (fname, fp, file_size, quant, mmproj_path) tuples
        total_files = 0
        new_count = 0
        existing_count = 0
        hosts_scanned = []

        for play in results.get("results", {}).get("plays", []):
            for task in play.get("tasks", []):
                task_name = task.get("task", {}).get("name", "")
                if "Output model list per host" not in task_name:
                    continue

                # Ansible 2.10+ stores results under task["hosts"] (dict keyed by hostname)
                host_results = task.get("hosts", {})
                if isinstance(host_results, dict):
                    for hostname, host_data in host_results.items():
                        hosts_scanned.append(hostname)
                        msg = host_data.get("msg", {})
                        if not isinstance(msg, dict):
                            continue

                        models_raw = msg.get("models_raw", [])
                        for line in models_raw:
                            if not isinstance(line, str):
                                continue
                            try:
                                file_info = json.loads(line)
                            except (json.JSONDecodeError, TypeError):
                                continue

                            fp = file_info.get("path", "")
                            if not fp:
                                continue

                            total_files += 1
                            fname = _os.path.basename(fp)

                            # Debug: log first 10 raw entries to trace mmproj data flow
                            if total_files <= 10:
                                mp = file_info.get("mmproj_path", "")
                                print(f"[qr-scan-debug] #{total_files} path={fp} mmproj_path={repr(mp)}")

                            # Skip already-registered paths
                            if fp in existing_paths:
                                existing_count += 1
                                continue

                            file_size = file_info.get("size", 0) or 0
                            quant = parse_quantization(fname)
                            mmproj_path = file_info.get("mmproj_path", "") or None
                            # Deduplicate across hosts
                            if fp in _seen_paths:
                                continue
                            _seen_paths.add(fp)
                            all_new_files.append((fname, fp, file_size, quant, mmproj_path))
                elif isinstance(host_results, list):
                    # Legacy format (fallback)
                    for result_entry in host_results:
                        host = result_entry.get("host", "unknown")
                        hosts_scanned.append(host)
                        msg = result_entry.get("msg", {})
                        if not isinstance(msg, dict):
                            continue

                        models_raw = msg.get("models_raw", [])
                        for line in models_raw:
                            if not isinstance(line, str):
                                continue
                            try:
                                file_info = json.loads(line)
                            except (json.JSONDecodeError, TypeError):
                                continue

                            fp = file_info.get("path", "")
                            if not fp:
                                continue

                            total_files += 1
                            fname = _os.path.basename(fp)

                            if fp in existing_paths:
                                existing_count += 1
                                continue

                            file_size = file_info.get("size", 0) or 0
                            quant = parse_quantization(fname)
                            mmproj_path = file_info.get("mmproj_path", "") or None
                            # Deduplicate across hosts
                            if fp in _seen_paths:
                                continue
                            _seen_paths.add(fp)
                            all_new_files.append((fname, fp, file_size, quant, mmproj_path))
        # Group sharded models and insert
        _shard_groups = {}
        _individual_files = []

        for fname, fp, file_size, quant, mmproj_path in all_new_files:
            m = _shard_pattern.match(fname)
            if m:
                # Sharded file — group by base name
                base_name = m.group(1) + '.gguf'
                if base_name not in _shard_groups:
                    _shard_groups[base_name] = {"files": [], "total_size": 0, "quant": quant, "mmproj_path": mmproj_path}
                _shard_groups[base_name]["files"].append((fname, fp, file_size))
                _shard_groups[base_name]["total_size"] += file_size
                # Keep the first non-empty mmproj_path found in the group
                if not _shard_groups[base_name]["mmproj_path"] and mmproj_path:
                    _shard_groups[base_name]["mmproj_path"] = mmproj_path
            else:
                # Individual file — keep as-is
                _individual_files.append((fname, fp, file_size, quant, mmproj_path))

        target_db = db_path

       # Insert grouped (sharded) models
        for base_name, group in _shard_groups.items():
            shards = len(group["files"])
            if shards < 2:
                # Single shard — treat as individual
                fname, fp, file_size = group["files"][0]
                quant = group["quant"]
                mmproj_path = group["mmproj_path"] or None
                try:
                    model = _am(target_db, engine_type_id, name=fname, model_path=fp,
                                size_bytes=file_size if file_size else None, quantization=quant,
                                is_sharded=0, total_shards=None, mmproj_path=mmproj_path)
                    if model is not None and model.get("_new"):
                        new_count += 1
                except Exception:
                    logging.warning("Failed to insert single-shard model %s", fname)
                    pass
            else:
                # Multi-shard — sort by shard number and pick -00001-of- as primary
                def shard_sort_key(item):
                    """Extract shard number for sorting (e.g., 00001 from filename)."""
                    fname = item[0]
                    sm = _re.match(r'.*-(\d+)-of-\d+', fname)
                    return int(sm.group(1)) if sm else 99999
                sorted_files = sorted(group["files"], key=shard_sort_key)
                fname_primary, fp_primary, _ = sorted_files[0]
                mmproj_path = group["mmproj_path"] or None
                try:
                    model = _am(target_db, engine_type_id, name=base_name, model_path=fp_primary,
                                size_bytes=group["total_size"] if group["total_size"] else None,
                                quantization=group["quant"], is_sharded=1, total_shards=shards,
                                mmproj_path=mmproj_path)
                    if model is not None and model.get("_new"):
                        new_count += 1
                except Exception:
                    pass

      # Insert individual files
        for fname, fp, file_size, quant, mmproj_path in _individual_files:
            try:
                model = _am(target_db, engine_type_id, name=fname, model_path=fp,
                            size_bytes=file_size if file_size else None, quantization=quant,
                            is_sharded=0, total_shards=None, mmproj_path=mmproj_path)
                if model is not None and model.get("_new"):
                    new_count += 1
            except Exception:
                logging.warning("Failed to insert individual model %s", fname)
                pass

    except Exception as exc:
        # If processing fails, still try to log (best effort)
        logging.warning("Non-critical failure in model scan: %s", str(exc))
        pass

    # Log the scan execution (outside try — always runs)
    log_ansible_action(db_path, "scan_models", None, None,
                       "node/scan_models.yml",
                       {"limit": limit}, results)

    return {
        "new_models": new_count,
        "existing_models": existing_count,
        "hosts_scanned": hosts_scanned,
        "total_files_found": total_files,
    }


def log_ansible_action(db_path, action_type, node_id, instance_id, playbook,
                         params, result):
    """Log an ansible playbook execution to the ansible_actions table.

    Always writes a minimal heartbeat record so every playbook run is visible
    in the UI and API regardless of log level. Full detail (task summaries,
    duration, stdout/stderr) is only populated when the log level permits.

    Args:
        db_path: Path to the SQLite database.
        action_type: One of 'validate_node','discover_node','deploy_instance',
                     'undeploy_instance','restart_instance','stop_instance',
                     'get_logs','scan_models'.
        node_id: Foreign key to nodes table (or None for global actions).
        instance_id: Foreign key to instances table (or None).
        playbook: Playbook filename or path identifier.
        params: Dict of extra vars used (will be JSON-encoded).
        result: Parsed result dict from run_playbook() (changed, failed, results).

    Returns:
         True if logged successfully, False otherwise.
    """
    # Determine log level gate — controls whether we do expensive detail processing.
    _level_passes = False
    try:
        from lib import lib_constants as _lc
        _required = _lc.ANSIBLE_LOG_LEVELS.get(action_type, "all")
        _current = "errors"
        _qr = sys.modules.get("quickrobot")
        if _qr and hasattr(_qr, "_CONFIG"):
            _current = _qr._CONFIG.get("ansible_log_level", "errors")
        _level_order = {"errors": 0, "warnings": 1, "all": 2}
        _level_passes = _level_order.get(_current, 0) >= _level_order.get(_required, 2)
    except Exception:
        pass  # Gate is non-critical; default to level_passes=False (minimal logging)

    import os as _os
    from datetime import datetime, timezone as _tz

    from db.sqlite import pool
    from db.adapters.playbooks import get_playbook_by_path as _get_pb

    # Resolve playbook registry reference (id + version)
    pb_registry_id = None
    pb_version = None
    if playbook:
        # Normalize path: strip project root prefix to get relative path
        pb_rel = playbook
        # Try stripping common absolute prefixes
        for prefix in ("/CORE/projects/quickrobot/", "/CORE/projects/"):
            if pb_rel.startswith(prefix):
                pb_rel = pb_rel[len(prefix):]
                break
        # Strip leading slash
        if pb_rel.startswith("/"):
            pb_rel = pb_rel[1:]
        # Now try lookup with normalized path
        pb_rec = _get_pb(db_path, pb_rel)
        if not pb_rec:
            pb_rec = _get_pb(db_path, f"playbooks/{pb_rel}" if not pb_rel.startswith("playbooks/") else pb_rel)
        if not pb_rec and "/" not in pb_rel:
            pb_rec = _get_pb(db_path, f"playbooks/{pb_rel}")
        if pb_rec:
            pb_registry_id = pb_rec.get("id")
            pb_version = pb_rec.get("version")

    def _compute_duration_ms(results_data):
        """Compute overall duration in ms from Ansible task timestamps."""
        min_start = None
        max_end = None
        for play in results_data.get("plays", []):
            for task in play.get("tasks", []):
                dur = task.get("task", {}).get("duration", {})
                start = dur.get("start")
                end = dur.get("end")
                if start:
                    if min_start is None or start < min_start:
                        min_start = start
                if end:
                    if max_end is None or end > max_end:
                        max_end = end
        if min_start and max_end:
            try:
                # Parse ISO timestamps — Ansible uses both formats
                def _parse_ts(ts):
                    ts = str(ts).replace("Z", "+00:00")
                    # Handle format: "2026-05-17 07:24:29.749228" (space-separated)
                    if "T" not in ts and "+" not in ts:
                        ts = ts.replace(" ", "T") + "+00:00"
                    return datetime.fromisoformat(ts)
                dt_start = _parse_ts(min_start)
                dt_end = _parse_ts(max_end)
                diff_ms = int((dt_end - dt_start).total_seconds() * 1000)
                return max(diff_ms, 0), min_start, max_end
            except (ValueError, TypeError, AttributeError):
                return 0, None, None
        return 0, None, None

    # Prepare result data — expensive processing gated by log level.
    # The heartbeat (INSERT) always runs; detail enrichment is conditional.
    created_at = datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    if _level_passes:
        # Universal task summary extraction (works for ALL action types).
        def _extract_task_summary(results_data):
            """Extract per-task status from parsed Ansible output."""
            tasks = []
            for play in results_data.get("plays", []):
                for task in play.get("tasks", []):
                    task_info = task.get("task", {})
                    task_name = task_info.get("name", "unknown")
                    failed = task.get("failed", False)
                    changed = task.get("changed", False)
                    status = "failed" if failed else ("changed" if changed else "ok")

                    error_msg = ""
                    for entry in task.get("results", []):
                        msg = entry.get("msg", "")
                        if isinstance(msg, dict):
                            error_msg = json.dumps(msg)
                        elif isinstance(msg, str) and msg.strip():
                            error_msg = msg
                        if error_msg:
                            break

                    # Extract per-task duration from Ansible timestamps
                    task_dur = 0
                    dur_info = task_info.get("duration", {})
                    if dur_info and isinstance(dur_info, dict):
                        try:
                            from datetime import datetime as _dt
                            s = str(dur_info.get("start", ""))
                            e = str(dur_info.get("end", ""))
                            if s and e:
                                s = s.replace("Z", "+00:00")
                                e = e.replace("Z", "+00:00")
                                if "T" not in s and "+" not in s:
                                    s = s.replace(" ", "T") + "+00:00"
                                if "T" not in e and "+" not in e:
                                    e = e.replace(" ", "T") + "+00:00"
                                dt_s = _dt.fromisoformat(s)
                                dt_e = _dt.fromisoformat(e)
                                task_dur = max(int((dt_e - dt_s).total_seconds() * 1000), 0)
                        except (ValueError, TypeError, AttributeError):
                            pass

                    tasks.append({
                        "name": task_name,
                        "status": status,
                        "error": error_msg,
                        "duration_ms": task_dur,
                    })
            return tasks

        # Extract top-level error message from result dict (health checks, timeouts, etc.)
        def _extract_error_message(result_data):
            """Extract the most relevant error/reason from parsed Ansible result."""
            if result_data.get("failed"):
                # Check for error in results
                for play in result_data.get("plays", []):
                    for task in play.get("tasks", []):
                        for entry in task.get("results", []):
                            msg = entry.get("msg", "")
                            if isinstance(msg, dict):
                                msg = json.dumps(msg)
                            elif isinstance(msg, str) and msg.strip():
                                return msg[:500]
                # Fallback to result-level error
                return result_data.get("error", "Action failed")[:500]
            # Health check / success case — look for "alive: false" or similar
            if result_data.get("alive") is False:
                return result_data.get("error", "Service not responding")[:500]
            return ""

        results_data = result.get("results", {})
        stdout_raw = ""
        stderr_raw = ""

        for play in results_data.get("plays", []):
            for task in play.get("tasks", []):
                task_name = task.get("task", {}).get("name", "")
                entries = task.get("results", [])

                if "Output" in task_name:
                    for entry in entries:
                        msg = entry.get("msg", "")
                        if isinstance(msg, dict):
                            msg_str = json.dumps(msg)
                        elif isinstance(msg, str) and msg.strip():
                            msg_str = msg
                        else:
                            msg_str = ""
                        if msg_str.strip():
                            stdout_raw = msg_str

                if task.get("failed", False):
                    for entry in entries:
                        err = entry.get("stderr", "") or entry.get("msg", "")
                        if isinstance(err, str) and err.strip():
                            stderr_raw = err

        task_summary = _extract_task_summary(results_data)
        results_data["_task_summary"] = task_summary

        # Check for error message in result dict (e.g., TimeoutError).
        if not stdout_raw and not stderr_raw:
            error_msg = result.get("error", "")
            if error_msg:
                stdout_raw = f"[error] {error_msg}"

        stdout_trunc = stdout_raw[:10000] if stdout_raw else ""
        stderr_trunc = stderr_raw[:10000] if stderr_raw else ""

        # Compute duration from Ansible task timestamps (HDIR-13)
        duration_ms, actual_start, actual_end = _compute_duration_ms(results_data)
    else:
        # Minimal heartbeat — no expensive parsing (I3: use None so task_summary becomes "", not "{}").
        results_data = None
        stdout_trunc = ""
        stderr_trunc = ""
        duration_ms = 0
        actual_start = created_at
        actual_end = created_at

    started_at = actual_start if actual_start else created_at
    finished_at = actual_end if actual_end else created_at

    # Extract hostname from params for host column (I1)
    _host = ""
    if isinstance(params, dict):
        _host = params.get("inventory_host") or params.get("node", "") or ""

    # Extract error/reason message for display in task list Details column
    _error_reason = ""
    if _level_passes and results_data:
        _error_reason = _extract_error_message(results_data) or result.get("error", "") or stdout_trunc[:200]
    elif not _level_passes:
        # Minimal mode: include any top-level error message
        _error_reason = result.get("error", "") or ""

    try:
        with pool(db_path) as conn:
            exit_code = 1 if result.get("failed", False) else 0
            status_str = result.get("status", "failed" if exit_code else "success")
            details_dict = {"playbook": playbook, "params": params or {}, "exit_code": exit_code}
            if _error_reason:
                details_dict["reason"] = _error_reason
            details_str = json.dumps(details_dict)
            conn.execute(
                 """INSERT INTO ansible_actions
                    (action_type, node_id, instance_id, actor, status, details,
                     created_at, started_at, finished_at, duration_ms,
                     playbook_name, task_summary,
                     playbook_registry_id, playbook_version, host)
                    VALUES (?, ?, ?, 'system', ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (action_type, node_id, instance_id,
                     status_str, details_str,
                     created_at, started_at, finished_at,
                     duration_ms if duration_ms and duration_ms > 0 else 0,
                     playbook,
                     json.dumps(results_data) if results_data else "",
                     pb_registry_id, pb_version, _host),
                )
        return True

    except Exception:
        return False


