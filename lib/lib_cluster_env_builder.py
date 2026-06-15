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

"""Cluster env builder — Python-generated ENV/CLI for llama-server + RPC deployments.

Replaces Jinja2 computation in Ansible playbooks with a single Python function
that produces the complete deployment config: merged env dict, CLI args string,
tensor_split value, and resolved RPC bindings.

Functions:
    build_llama_server_env(db_path, instance_id) — complete llama-server deployment config
    build_rpc_server_env(db_path, instance_id) — complete RPC server deployment config
    get_cluster_summary(db_path, llama_id) — cluster info for Herd page UI
"""

import json
import logging

from lib.qr_engine_ids import QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC


DEFAULT_SPLIT_MODE = "layer"
DEFAULT_SERVER_SPLIT_VALUE = 100
DEFAULT_RPC_BIND_HOST = "0.0.0.0"


class ClusterEnvError(Exception):
    """Raised on cluster env builder errors."""


def _parse_bind_ids(raw_value):
    """Parse rpc_bind_ids from DB value (JSON string or list) to a Python list.

    Args:
        raw_value: Raw value from instances.rpc_bind_ids column.

    Returns:
        list of integer RPC instance IDs, or empty list on error.
    """
    if not raw_value:
        return []
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            logging.warning("Failed to parse rpc_bind_ids: %s", raw_value)
            return []
    if isinstance(raw_value, list):
        return [int(x) for x in raw_value if x]
    return []


def _compute_tensor_split(server_split_raw, rpc_splits):
    """Compute gpu_slot flag and tensor_split string from split value + RPC bindings.

    Args:
        server_split_raw: Raw value from instances.split column (int/None/str/"none")
        rpc_splits: List of string split values from bound RPC instances

    Returns:
        tuple: (gpu_slot: bool, tensor_split_str: str)
            - gpu_slot: True when split is explicitly set (0 counts as set, NULL/empty="none" does not)
            - tensor_split_str: Comma-separated split values for LLAMA_ARG_TENSOR_SPLIT
              split=0 → "0,RPC1,RPC2,..."  (server contributes 0% — included in list)
              split=NULL/"none" → "RPC1,RPC2,..." (server excluded entirely from tensor_split)
    """
    # Normalize input: distinguish between explicit 0 and null/none
    is_explicit_zero = False
    val = None
    if server_split_raw in (None, "", "none"):
        val = None  # null/empty → server excluded from tensor_split
    else:
        try:
            val = int(server_split_raw)
            if val == 0:
                is_explicit_zero = True  # explicit 0 → included as "0" in tensor_split
        except (ValueError, TypeError):
            val = None

    gpu_slot = val is not None  # True for any explicit value including 0
    server_split = str(val) if gpu_slot else None  # None when split is null/none
    dev_count = len(rpc_splits) + (1 if gpu_slot else 0)
    split_values = ([server_split] + rpc_splits) if gpu_slot else rpc_splits
    tensor_split_str = ",".join(filter(None, split_values)) if dev_count > 0 else (server_split or "0")
    return gpu_slot, tensor_split_str


def build_llama_server_env(db_path, instance_id):
    """Build complete environment dict + CLI args for a llama-server deployment.

    Delegates config merging to lib_config_merge.build_merged_config(), then adds
    cluster-specific fields: tensor_split, RPC bindings, -dev flag handling.

    Args:
        db_path: Path to SQLite database.
        instance_id: Integer primary key of the llama-server instance.

    Returns:
        dict with keys:
            env:              Complete merged env dict (ready for playbook)
            cli_args:         Pre-joined CLI string from merged config
            tensor_split_str: Computed LLAMA_ARG_TENSOR_SPLIT value
            split_mode:       Effective split mode ("layer", "row", or "tensor")
            rpc_bindings:     List of resolved RPC instance metadata dicts
            bind_count:       Number of RPC bindings
            build_command:    Build command string from engine_configs

    Raises:
        ClusterEnvError: If instance not found or critical data missing.
    """
    from db.sqlite import pool
    from lib.lib_config_merge import build_merged_config as _canonical_merge

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise ClusterEnvError(f"Instance {instance_id} not found")
        inst = dict(row)

        # Get engine type for build_command lookup
        et_row = conn.execute(
            "SELECT capabilities, name FROM engine_types WHERE id = ?",
            (inst["engine_type_id"],),
        ).fetchone()
        if not et_row:
            raise ClusterEnvError(f"Engine type for instance {instance_id} not found")

        # Read binary_path from engine_configs (used by deploy playbook)
        _bc_row = conn.execute(
            "SELECT value FROM engine_configs WHERE engine_type_id = ? AND key = 'binary_path'",
            (inst["engine_type_id"],),
        ).fetchone()
        binary_path = _bc_row["value"] if _bc_row else ""

    # Call canonical merge — handles all 6 layers + model path resolution
    merged = _canonical_merge(db_path, instance_id, node_id=inst.get("node_id"))
    split_mode = inst.get("split_mode") or DEFAULT_SPLIT_MODE
    if split_mode:
        merged["env"]["LLAMA_ARG_SPLIT_MODE"] = split_mode

    # Resolve RPC bindings and compute tensor_split
    bind_ids = _parse_bind_ids(inst.get("rpc_bind_ids"))
    rpc_bindings = []

    with pool(db_path) as conn:
        # Load preset gpu_device for -dev flag injection (needed regardless of bindings)
        preset_row = conn.execute(
            "SELECT gpu_device FROM engine_presets WHERE id = ?", (inst["preset_id"],)
        ).fetchone()
        preset_gpu_val = preset_row["gpu_device"] if preset_row and preset_row["gpu_device"] else None

        if bind_ids:
            for rid in bind_ids:
                try:
                    rpc_inst = conn.execute(
                        "SELECT id, name, node_id, port_assigned, split, experts, draft FROM instances WHERE id = ?",
                        (int(rid),),
                    ).fetchone()
                    if rpc_inst:
                        node_row = conn.execute(
                            "SELECT hostname FROM nodes WHERE id = ?", (rpc_inst["node_id"],)
                        ).fetchone()
                        hostname = node_row["hostname"] if rpc_inst else ""
                        split_val = int(rpc_inst["split"]) if rpc_inst["split"] is not None else 100
                        rpc_bindings.append({
                            "id": rpc_inst["id"],
                            "name": rpc_inst["name"],
                            "hostname": hostname,
                            "port_assigned": rpc_inst["port_assigned"] or 0,
                            "split": split_val,
                            "experts": int(rpc_inst["experts"]) if rpc_inst["experts"] else 0,
                            "draft": int(rpc_inst["draft"]) if rpc_inst["draft"] else 0,
                        })
                except (ValueError, TypeError):
                    logging.warning("Skipping invalid RPC ID in bindings for instance %s", inst["id"])
                    pass  # Skip invalid RPC IDs

   # Build CLI args: base --host/--port from merged env, then cluster-specific additions
    base_cli = []
    host = merged.get("env", {}).get("LLAMA_ARG_HOST")
    port = inst.get("port_assigned") or merged.get("env", {}).get("LLAMA_ARG_PORT")
    if host:
        _host_str = str(host)
        # Wrap IPv6 in brackets for llama.cpp --host flag (RFC 3986)
        if ":" in _host_str and "." not in _host_str:
            _host_str = f"[{_host_str}]"
        base_cli.extend(["--host", _host_str])
    if port:
        base_cli.extend(["--port", str(port)])

    # Build CLI args with cluster-specific -dev/--rpc handling
    preset_cli_opts = list(merged.get("cli_opts", []))
    base_dev = None
    # GPU Override: read from merged env (Layer 5: config_override), overrides preset gpu_device
    gpu_override = merged.get("env", {}).get("LLAMA_ARG_DEVICE") or None

    server_split_raw_val = inst.get("split")
    rpc_splits_list = [str(b["split"]) for b in rpc_bindings]
    gpu_slot, tensor_split_str = _compute_tensor_split(server_split_raw_val, rpc_splits_list)

    # Inject preset-level gpu_device as --device if present AND gpu_slot is true and no --device already in cli_parts
    if gpu_slot and preset_gpu_val and preset_gpu_val != "none" and "--device" not in preset_cli_opts:
        preset_cli_opts.insert(0, "--device")
        preset_cli_opts.insert(1, preset_gpu_val)

    # Build ordered CLI parts: base + preset_opts first
    cli_parts = base_cli + preset_cli_opts

    if rpc_bindings:
        # Extract base device from preset cli_opts (format: "-dev <value>")
        while "-dev" in preset_cli_opts:
            idx = preset_cli_opts.index("-dev")
            if idx + 1 < len(preset_cli_opts):
                base_dev = preset_cli_opts[idx + 1]
            # Remove the -dev pair (we rebuild it below)
            if idx + 1 < len(preset_cli_opts):
                preset_cli_opts.pop(idx + 1)
            preset_cli_opts.pop(idx)

        # Inject --rpc BEFORE -dev so llama.cpp can resolve RPC device names
        rpc_endpoints = [f"{b['hostname']}:{b['port_assigned']}" for b in rpc_bindings]
        cli_parts.extend(["--rpc", ",".join(rpc_endpoints)])

        # Build -dev flag: GPU override (if set) + base_dev + RPC refs
        dev_refs = []
        if gpu_override:
            dev_refs.append(gpu_override)
        elif base_dev is not None and base_dev != "none":
            dev_refs.append(base_dev)
        dev_refs += [f"RPC{n}" for n in range(len(rpc_bindings))]
        cli_parts.extend(["-dev", ",".join(dev_refs)])

        # Inject --device-draft RPC<N> at the END (after --rpc, -dev)
        for idx, b in enumerate(rpc_bindings):
            draft_val = int(b.get("draft") or 0) if isinstance(b.get("draft"), (int, float)) else 0
            if draft_val > 0:
                cli_parts.extend(["--device-draft", f"RPC{idx}"])

    else:
        # Standalone instance (no RPC bindings): build -dev from gpu_override or base_dev
        if gpu_override:
            cli_parts.extend(["-dev", gpu_override])
        elif base_dev is not None and base_dev != "none":
            cli_parts.extend(["-dev", base_dev])

    # Inject instance-level custom CLI flags from cli_flags column LAST
    # (so they appear after --rpc, -dev in the final ExecStart line)
    raw_flags = inst.get("cli_flags") or "[]"
    try:
        if isinstance(raw_flags, str):
            imported_flags = json.loads(raw_flags)
        else:
            imported_flags = raw_flags
        if isinstance(imported_flags, list):
            for flag in imported_flags:
                if isinstance(flag, str) and flag.strip():
                    cli_parts.append(flag.strip())
    except (json.JSONDecodeError, TypeError):
        logging.warning("Invalid cli_flags for instance %s: %s", inst["id"], raw_flags)
        pass  # Skip invalid cli_flags

    cli_args = " ".join(str(x) for x in cli_parts)

    # tensor_split_str already computed above (line 193) via _compute_tensor_split()

    # Add LLAMA_ARG_TENSOR_SPLIT to merged env (for playbook consumption)
    merged["env"]["LLAMA_ARG_TENSOR_SPLIT"] = tensor_split_str

    # Remove LLAMA_ARG_DEVICE from env — device is set via CLI -dev flag, not env var
    merged["env"].pop("LLAMA_ARG_DEVICE", None)

    # Inject binary_path into env for playbook ExecStart rendering
    if binary_path:
        merged["env"]["binary_path"] = binary_path

    return {
        "env": merged["env"],
        "cli_args": cli_args,
        "tensor_split_str": tensor_split_str,
        "split_mode": split_mode,
        "rpc_bindings": rpc_bindings,
        "bind_count": len(rpc_bindings),
        "build_command": merged["env"].get("node_build_set_cmd", ""),
        "gpu_override": gpu_override,
    }


def build_rpc_server_env(db_path, instance_id):
    """Build complete environment dict + CLI args for an RPC server deployment.

    Delegates config merging to lib_config_merge.build_merged_config(), then builds
    RPC-specific CLI args (-H {host} -p {port} -d {device}).

    Args:
        db_path: Path to SQLite database.
        instance_id: Integer primary key of the RPC instance.

    Returns:
        dict with keys:
            env:      Complete merged env dict (ready for playbook)
            cli_args: Pre-joined RPC CLI string
    """
    from db.sqlite import pool
    from lib.lib_config_merge import build_merged_config as _canonical_merge

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise ClusterEnvError(f"Instance {instance_id} not found")
        inst = dict(row)

    # Call canonical merge — handles all layers + model path resolution
    merged = _canonical_merge(db_path, instance_id, node_id=inst.get("node_id"))

    # Build CLI args: -H {host} -p {port} -d {device} + remaining preset cli_opts
    # Device priority: instance gpu_device → preset gpu_device (LLAMA_ARG_DEVICE) → cli_opts "-d"
    # No fallback to CPU — empty preset means auto-detect (binary decides device)
    device = None
    if merged.get("env", {}).get("LLAMA_ARG_DEVICE"):
        device = merged["env"]["LLAMA_ARG_DEVICE"]
    for i in range(len(merged.get("cli_opts", [])) - 1):
        if merged["cli_opts"][i] == "-d" and i + 1 < len(merged["cli_opts"]):
            device = merged["cli_opts"][i + 1]
    if inst.get("gpu_device"):
        device = inst["gpu_device"]

    host = merged.get("env", {}).get("LLAMA_ARG_HOST") or DEFAULT_RPC_BIND_HOST
    port = inst.get("port_assigned", 0) or 0
    # Wrap IPv6 in brackets for llama.cpp -H flag (RFC 3986)
    # Only applies to pure IPv6 (contains colons, no dots) — preserves IPv4 and hostnames as-is
    if host != "0.0.0.0":
        _host_check = str(host)
        if ":" in _host_check and "." not in _host_check:
            host = f"[{_host_check}]"

    # Build base cli_args from config, then append remaining preset cli_opts
    # Skip "-d {device}" pairs so we don't duplicate them
    extra_opts = []
    opts_list = merged.get("cli_opts", []) if isinstance(merged.get("cli_opts"), list) else []
    skip_next = False
    for i, opt in enumerate(opts_list):
        if skip_next:
            skip_next = False
            continue
        if opt == "-d" and i + 1 < len(opts_list):
            skip_next = True
            continue
        extra_opts.append(opt)
    dev_part = f" -d {device}" if device else ""
    cli_args = f"-H {host} -p {port}{dev_part}"
    if extra_opts:
        cli_args += " " + " ".join(str(x) for x in extra_opts)

    return {
        "env": merged["env"],
        "cli_args": cli_args,
    }


def get_cluster_summary(db_path, llama_id):
    """Get full cluster summary for a llama-server instance.

    Used by GET /api/v1/rpccluster/summary and Herd page right panel.

    Args:
        db_path: Path to SQLite database.
        llama_id: Integer primary key of the llama-server instance.

    Returns:
        dict with:
            id, name, node_id, node_hostname, state
            split_mode, split (server value)
            rpc_bind_ids (raw), rpc_bindings (resolved list)
            tensor_split (computed string)
            preset_name (if any)
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT i.*, ep.name as preset_name, ep.config_template, "
            "n.name as node_display_name, n.hostname as node_display_hostname "
            "FROM instances i "
            "LEFT JOIN engine_presets ep ON i.preset_id = ep.id "
            "LEFT JOIN nodes n ON i.node_id = n.id "
            "WHERE i.id = ?", (llama_id,)
        ).fetchone()
        if row is None:
            raise ClusterEnvError(f"Llama-server {llama_id} not found")
        inst = dict(row)
        # instances table has node_name/node_hostname columns that shadow JOIN aliases.
        # Rename the joined values back to the expected field names.
        if inst.get("node_display_name"):
            inst["node_name"] = inst.pop("node_display_name")
        if inst.get("node_display_hostname"):
            inst["node_hostname"] = inst.pop("node_display_hostname")

        # Check preset for -dev value to determine device count for tensor_split
        base_dev = None
        preset_template = inst.get("config_template") or "{}"
        try:
            pt = json.loads(preset_template) if isinstance(preset_template, str) else {}
            if isinstance(pt, dict):
                cli_opts = pt.get("cli_opts", []) or []
                while "-dev" in cli_opts:
                    idx = cli_opts.index("-dev")
                    if idx + 1 < len(cli_opts):
                        base_dev = cli_opts[idx + 1]
                    if idx + 1 < len(cli_opts):
                        cli_opts.pop(idx + 1)
                    cli_opts.pop(idx)
        except (json.JSONDecodeError, TypeError):
            logging.warning("Failed to parse preset config_template for instance %s", inst["id"])
            pass

        # Resolve RPC bindings
        bind_ids = _parse_bind_ids(inst.get("rpc_bind_ids"))
        rpc_bindings = []
        rpc_splits = []

        for rid in bind_ids:
            try:
                rpc_inst = conn.execute(
                    "SELECT id, name, node_id, port_assigned, split, experts, draft, state FROM instances WHERE id = ?",
                    (int(rid),),
                ).fetchone()
                if rpc_inst:
                    node_row = conn.execute(
                        "SELECT hostname FROM nodes WHERE id = ?", (rpc_inst["node_id"],)
                    ).fetchone()
                    hostname = node_row["hostname"] if node_row else ""
                    split_val = int(rpc_inst["split"]) if rpc_inst["split"] is not None else 100
                    rpc_bindings.append({
                        "id": rpc_inst["id"],
                        "name": rpc_inst["name"],
                        "hostname": hostname,
                        "port_assigned": rpc_inst["port_assigned"] or 0,
                        "split": split_val,
                        "experts": int(rpc_inst["experts"]) if rpc_inst["experts"] else 0,
                        "draft": int(rpc_inst["draft"]) if rpc_inst["draft"] else 0,
                        "state": rpc_inst["state"],
                    })
                    rpc_splits.append(str(split_val))
            except (ValueError, TypeError):
                logging.warning("Skipping invalid RPC ID in cluster summary for instance %s", llama_id)
                pass

        # Compute tensor_split using extracted helper
        gpu_slot, tensor_split = _compute_tensor_split(inst.get("split"), rpc_splits)

        # Parse cli_flags from instance and resolve draft device flags
        raw_flags = inst.get("cli_flags") or "[]"
        try:
            instance_cli_flags = json.loads(raw_flags) if isinstance(raw_flags, str) else raw_flags
            if not isinstance(instance_cli_flags, list):
                instance_cli_flags = []
        except (json.JSONDecodeError, TypeError):
            logging.warning("Invalid cli_flags for cluster summary of instance %s", llama_id)
            instance_cli_flags = []
        # Check for GPU override from config_override (stored as LLAMA_ARG_DEVICE)
        gpu_override = None
        co_raw = inst.get("config_override") or "{}"
        try:
            co = json.loads(co_raw) if isinstance(co_raw, str) else co_raw
            if isinstance(co, dict):
                gpu_override = co.get("LLAMA_ARG_DEVICE") or None
        except (json.JSONDecodeError, TypeError):
            pass
        # Resolve draft devices: RPCs with draft > 0 get --device-draft RPC<N>
        draft_devices = []
        for idx, b in enumerate(rpc_bindings):
            d = b.get("draft", 0)
            if isinstance(d, int) and d > 0:
                draft_devices.append(f"RPC{idx}")

        return {
            "id": inst["id"],
            "name": inst["name"],
            "node_id": inst["node_id"],
            "node_hostname": inst.get("node_hostname") or "",
            "state": inst.get("state"),
           "split_mode": inst.get("split_mode") or DEFAULT_SPLIT_MODE,
            "split": inst.get("split"),
            "bind_count": len(bind_ids),
            "rpc_bind_ids": bind_ids,
               "rpc_bindings": rpc_bindings,
            "tensor_split": tensor_split,
             "preset_id": inst.get("preset_id"),
             "preset_name": inst.get("preset_name"),
            "cli_flags": instance_cli_flags,
            "draft_devices": draft_devices,
            "gpu_override": gpu_override,
        }


def rpc_binding_warnings(db_path, llama_instance_id):
    """Check RPC binding states for a llama-server instance.

    Returns a list of warning strings:
      - "RPC <name> not running (state=<s>)" for each non-running RPC
      - "RPC <name> bound to other server <other_name>" if another
        llama-server also has this RPC bound (detected at DB level)

    Args:
        db_path: Path to SQLite database.
        llama_instance_id: Integer primary key of the llama-server instance.

    Returns:
        list of warning strings (empty = all good).
    """
    from db.sqlite import pool as _pool
    with _pool(db_path) as conn:
        srv_row = conn.execute(
            "SELECT name, rpc_bind_ids FROM instances WHERE id = ?",
            (llama_instance_id,),
        ).fetchone()
        if srv_row is None:
            return []
        bind_ids = _parse_bind_ids(srv_row["rpc_bind_ids"])
        if not bind_ids:
            return []

        warnings = []
        # Build reverse map: for ALL llama-servers, collect bound RPC IDs
        # This detects multi-server-per-RPC bindings regardless of other server state
        all_servers = conn.execute(
            "SELECT id, name, rpc_bind_ids FROM instances WHERE engine_type_id IN (?, ?)",
            (QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC),
        ).fetchall()
        rpc_to_servers = {}  # rpc_id -> [server_name, ...]
        for srv in all_servers:
            srv_ids = _parse_bind_ids(srv["rpc_bind_ids"])
            for sid in srv_ids:
                rpc_to_servers.setdefault(int(sid), []).append(srv["name"])

        # Check each RPC's state + detect cross-server bindings
        for rid in bind_ids:
            rpc_row = conn.execute(
                "SELECT name, state FROM instances WHERE id = ?",
                (int(rid),),
            ).fetchone()
            if rpc_row is None:
                continue  # dangling reference — skip silently
            rpc_name = rpc_row["name"]
            if rpc_row["state"] != "running":
                warnings.append(
                    f"RPC {rpc_name} not running (state={rpc_row['state']})"
                )
            # Detect: RPC bound to another llama-server?
            servers = rpc_to_servers.get(int(rid), [])
            other_names = [s for s in servers if s != srv_row["name"]]
            if other_names:
                warnings.append(
                    f"RPC {rpc_name} bound to other server {other_names[0]}"
                )

    return warnings
