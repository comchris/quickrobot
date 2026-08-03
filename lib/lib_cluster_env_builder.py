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

Uses LayeredMergeChain (CONFIG-1 Phase 2) internally. Cluster bindings are
appended as layer 7 (highest precedence), overriding any previous layer values.

Functions:
    build_llama_server_env(db_path, instance_id) — complete llama-server deployment config
    build_rpc_server_env(db_path, instance_id) — complete RPC server deployment config
    get_cluster_summary(db_path, llama_id) — cluster info for Herd page UI
"""

import json
import logging

from lib.lib_config_merge import _clean_model_val
from lib.lib_config_level import (
    ConfigLevel,
    LayeredMergeChain,
    _deep_merge_dicts,
)
from lib.qr_engine_ids import QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC, QR_DEFAULT_LOCALHOST, QR_ENGINE_LLAMA_SERVER_NAME, QR_STATE_RUNNING


DEFAULT_SPLIT_MODE = "layer"
DEFAULT_SERVER_SPLIT_VALUE = 100
DEFAULT_RPC_BIND_HOST = QR_DEFAULT_LOCALHOST


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


def _compute_tensor_split(server_split_raw, rpc_splits, has_explicit_device=False):
    """Compute gpu_slot flag and tensor_split string from split value + RPC bindings.

    Args:
        server_split_raw: Raw value from instances.split column (int/None/str/"none")
        rpc_splits: List of string split values from bound RPC instances
        has_explicit_device: True if server preset has explicit -dev flag (Vulkan/CUDA).
                             When False and exactly 1 RPC, that RPC replaces the implicit default.

    Returns:
        tuple: (gpu_slot: bool, tensor_split_str: str)
            - gpu_slot: True when split is explicitly set (0 counts as set, NULL/empty="none" does not)
            - tensor_split_str: Comma-separated split values for LLAMA_ARG_TENSOR_SPLIT
              split=0 → "0,RPC1,RPC2,..."  (server contributes 0% — included in list)
              split=NULL/"none" → "RPC1,RPC2,..." (server excluded entirely from tensor_split)
              no -dev + 1 RPC → "100" (RPC replaces implicit default, single entry)
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
    
    # Fix: when server has no explicit -dev flag and exactly 1 RPC, the RPC replaces
    # the implicit default — single tensor_split entry, not "split,RPC"
    if not has_explicit_device and len(rpc_splits) == 1:
        tensor_split_str = rpc_splits[0]
    else:
        split_values = ([server_split] + rpc_splits) if gpu_slot else rpc_splits
        tensor_split_str = ",".join(filter(None, split_values)) if dev_count > 0 else (server_split or "0")
    
    return gpu_slot, tensor_split_str


def _generate_expert_split_flags(inst, rpc_bindings, expert_config):
    """Generate -ot CLI flags from per-RPC expert-split configuration.

    For each RPC with experts > 0, generates -ot flags based on mode:
      A (stride)   — 1 flag per RPC, interleaved expert indices
      B (block)    — 1 flag per RPC, contiguous index blocks
      C (load-dist) — 1 flag per RPC, greedy distance-maximization
      D (decouple) — 1 flag per RPC, expert block reversed (separate from attention)
      S (staggered) — 3 flags per RPC (up/gate/down), staggered indices
      F (freeform) — uses stored index_pattern as literal -ot string

    Mode D (decouple): like Block mode but shifts expert indices to the end of the pool.
      RPC0 gets experts [total-N .. total-1], RPC1 gets [total-2N .. total-N-1], etc.
      This separates attention layers from expert layers across different RPC nodes,
      so each node handles a distinct range of both — no overlap between attention
      and expert compute on the same RPC.

    CRITICAL: The "RPC0" in the -ot flag is a FIXED LITERAL, not a positional index.
    llama.cpp's -ot syntax uses RPC0 as the protocol role name for all entries.
    The actual target differentiation is done solely by hostname:port inside [].
    Do NOT use RPC1, RPC2, etc. — every entry must be RPC0[hostname:port].

    extra_ot_flags from expert_config are appended after mode-generated flags,
    enabling manual -ot entries for attention layers or arbitrary device targeting.

    Args:
        inst: Instance dict from DB.
        rpc_bindings: List of resolved RPC binding dicts (with id, hostname, port_assigned).
        expert_config: Expert-split config dict from config_override.

    Returns:
        list of '-ot "..."' flag strings, or empty list if no RPCs have experts > 0.
    """
    if not rpc_bindings:
        return []

    prefix = expert_config.get("template_prefix", "blk.")
    suffix = expert_config.get("template_suffix", "ffn_(up|gate|down)_exps.*")
    skip_n_first = int(expert_config.get("skip_n_first", 0) or 0)
    # Modes stored under _rpc_modes sub-key (WebUI format): {"_rpc_modes": {"109": {"mode":"b"}, ...}}
    # Fall back to top-level keys for backward compat (legacy format).
    _rpc_modes = expert_config.get("_rpc_modes", {})
    if not _rpc_modes:
        # Legacy: modes stored directly as top-level keys in expert_split
        _rpc_modes = {k: v for k, v in expert_config.items()
                      if k not in ("template_prefix", "template_suffix") and isinstance(v, dict)}

    flags = []
    total_experts = sum(int(b.get("experts") or 0) for b in rpc_bindings if int(b.get("experts") or 0) > 0)
    # Count RPCs with experts for use by Mode S (round-robin sub-layer distribution).
    # Cache here to avoid recomputing inside the per-RPC loop below.
    count_with_experts = len([r for r in rpc_bindings if int(r.get("experts") or 0) > 0])

    # Pre-compute expert-split indices for all RPCs (needed for modes that require global pool)
    _expert_allocation = {}  # rpc_id -> list of indices
    _use_greedy = False  # whether greedy allocation was needed
    if count_with_experts > 0 and total_experts > 0:
        # Build list of RPCs with experts, sorted by count descending (slowest first = best spacing)
        rpcs_with_experts = [(i, b) for i, b in enumerate(rpc_bindings) if int(b.get("experts") or 0) > 0]
        rpcs_by_load = sorted(rpcs_with_experts, key=lambda x: -int(x[1].get("experts") or 0))

        # Greedy distance-maximization allocation — respects per-RPC quotas exactly
        allocated = {i: [] for i, _ in rpcs_with_experts}
        quotas = {i: int(b.get("experts") or 0) for i, b in rpcs_with_experts}

        # Pre-assign one index per RPC via round-robin to ensure clean stride start positions
        for rr_idx in range(count_with_experts):
            rpc = rpcs_by_load[rr_idx][0]
            allocated[rpc].append(rr_idx)
            quotas[rpc] -= 1

        for exp_idx in range(count_with_experts, total_experts):
            # Find best RPC for this index: one with remaining quota
            candidates = [r for r, q in quotas.items() if q > 0]
            if not candidates:
                break
            # For each candidate, compute max distance to its already-assigned indices
            best_rpc = None
            best_dist = -1
            for r in candidates:
                 dist = min(abs(exp_idx - a) for a in allocated[r])
                 if best_rpc is None or dist > best_dist or (dist == best_dist and quotas.get(r, 0) < quotas.get(best_rpc, 0)):
                    best_rpc = r
                    best_dist = dist
            allocated[best_rpc].append(exp_idx)
            quotas[best_rpc] -= 1

       # Build lookup by rpc_id
        for i, _ in rpcs_with_experts:
            _expert_allocation[str(rpc_bindings[i]["id"])] = allocated[i]

    # block_offset: running offset for contiguous expert allocation (both Mode A stride and Mode B block).
    block_offset = 0

    for idx, b in enumerate(rpc_bindings):
        experts_val = int(b.get("experts") or 0)
        if experts_val <= 0:
            continue

        rpc_id = str(b["id"])
        rpc_mode = _rpc_modes.get(rpc_id, {}).get("mode", "a")

        if rpc_mode == "c":
            # Mode C (load-distribution): greedy distance-maximization allocation.
            # Already pre-computed above — look up the pre-assigned indices.
            indices = _expert_allocation.get(rpc_id, [])
        elif rpc_mode == "f":
            # Mode F (freeform): use stored index_pattern as the full -ot pattern string.
            # The index_pattern is taken as-is (e.g. "blk.(0|2|4).ffn_up_exps.*")
            # and inserted directly into the -ot flag without wrapping or transformation.
            pattern = _rpc_modes.get(rpc_id, {}).get("index_pattern", "")
            if not pattern:
                # No stored pattern — fall back to using prefix.suffix with range
                indices = list(range(experts_val))
        elif rpc_mode == "b":
            # Block mode: consecutive indices starting after previous RPCs' blocks.
            indices = list(range(block_offset, block_offset + experts_val))
            block_offset += experts_val
        elif rpc_mode == "d":
            # Mode D (decouple): expert block reversed — earlier RPCs get later experts.
            # This separates attention layers from expert layers across different RPC nodes.
            start_idx = max(0, total_experts - block_offset - experts_val)
            indices = list(range(start_idx, start_idx + experts_val))
            block_offset += experts_val
        elif rpc_mode == "a":
            # Mode A (stride): greedy allocation respecting per-RPC quotas.
            # Uses pre-computed allocation which distributes indices across
            # the expert pool with distance-maximization for clean stride.
            indices = _expert_allocation.get(rpc_id, [])
        elif rpc_mode == "s":
            # Mode S (staggered): quota-aware round-robin FFN sub-layer distribution.
            # Each RPC generates 3 -ot flags, one per FFN sub-layer (up/gate/down).
            # Pool size = experts_val * count_with_experts (per-RPC: N*RPCs).
            # Stride-3 rotation ensures each sub-layer gets N indices from this pool.

            sub_layer_types = ["up", "gate", "down"]
            pool_size = experts_val * count_with_experts

            for sl_idx, sub_type in enumerate(sub_layer_types):
                # Determine which residue class this RPC handles for this sub-layer.
                target_mod = (sl_idx + idx) % count_with_experts if count_with_experts > 0 else 0

                # Generate indices from this RPC's expanded pool with stride rotation.
                indices = [i for i in range(pool_size)
                           if i % count_with_experts == target_mod] if count_with_experts > 0 and pool_size > 0 else []

                if skip_n_first > 0:
                    indices = [x + skip_n_first for x in indices]

                # Sort indices
                indices = sorted(indices)
                if not indices:
                    continue

                pattern_str = "|".join(str(x) for x in indices)
                rpc_host = b.get("hostname", "")
                rpc_port = b.get("port_assigned", "")
                flag = f'-ot "{prefix}({pattern_str}).ffn_{sub_type}_exps.*=RPC0[{rpc_host}:{rpc_port}]"'
                flags.append(flag)
            continue  # Mode S handles its own flag generation above

        # Apply skip_n_first offset to indices (not applied to Mode F freeform patterns)
        if skip_n_first > 0 and rpc_mode != "f":
            indices = [x + skip_n_first for x in indices]

        # Generate the -ot flag
        rpc_host = b.get("hostname", "")
        rpc_port = b.get("port_assigned", "")

        if rpc_mode == "f" and pattern:
            # Mode F: use the freeform pattern string directly
            flag = f'-ot "{pattern}=RPC0[{rpc_host}:{rpc_port}]"'
        else:
            # All other modes: construct from prefix, indices, suffix
            # RPC0 is a fixed literal in llama.cpp -ot syntax (not RPC{idx})
            pattern_str = "|".join(str(x) for x in indices)
            flag = f'-ot "{prefix}({pattern_str}).{suffix}=RPC0[{rpc_host}:{rpc_port}]"'
        flags.append(flag)

    # Append extra_ot_flags if present (from config_override.expert_split.extra_ot_flags)
    if expert_config.get("extra_ot_flags"):
        for ef in expert_config["extra_ot_flags"]:
            if isinstance(ef, str) and ef.strip():
                flags.append(ef.strip())

    return flags


def build_llama_server_env(db_path, instance_id):
    """Build complete environment dict + CLI args for a llama-server deployment.

    Uses LayeredMergeChain (CONFIG-1 Phase 2) to build the config:
      L1-L5: Standard merge chain (engine → model → preset → override)
      L7: Cluster bindings (tensor_split, -dev, --rpc) — highest precedence

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
            gpu_override:     Effective GPU override value

    Raises:
        ClusterEnvError: If instance not found or critical data missing.
    """
    from db.sqlite import pool
    from lib.lib_config_merge import _parse_config_override, _wrap_flat_as_model, _clean_model_dict

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise ClusterEnvError(f"Instance {instance_id} not found")
        inst = dict(row)

        engine_type_id = inst["engine_type_id"]
        preset_id = inst.get("preset_id")

        et_row = conn.execute(
            "SELECT capabilities, name FROM engine_types WHERE id = ?",
            (engine_type_id,),
        ).fetchone()
        if not et_row:
            raise ClusterEnvError(f"Engine type for instance {instance_id} not found")

        cap_data = {}
        if et_row and et_row["capabilities"]:
            try:
                cap_data = json.loads(et_row["capabilities"])
            except (json.JSONDecodeError, TypeError):
                pass

        supports_models = bool(cap_data.get("supports_models", True))

        # Read binary_path from engine_configs (used by deploy playbook)
        _bc_row = conn.execute(
            "SELECT value FROM engine_configs WHERE engine_type_id = ? AND key = 'binary_path'",
            (engine_type_id,),
        ).fetchone()
        binary_path = _bc_row["value"] if _bc_row else ""

    # BINARY-DL: Resolve binary path from preset's binary_id template
    if preset_id is not None:
        from db.sqlite import pool as _pool2
        with _pool2(db_path) as _conn2:
            preset_row = _conn2.execute(
                "SELECT config_template FROM engine_presets WHERE id = ?",
                (preset_id,),
            ).fetchone()
            if preset_row and preset_row["config_template"]:
                try:
                    ct = json.loads(preset_row["config_template"])
                    binary_id = ct.get("binary_id")
                    if binary_id:
                        bin_row = _conn2.execute(
                            "SELECT * FROM engine_binaries WHERE id=? AND is_active=1",
                            (binary_id,),
                        ).fetchone()
                        if bin_row and bin_row["template_type"] == "binary":
                            b = dict(bin_row)
                            engine_name = et_row["name"] if et_row else ""
                            resolved_target = b["target_path"].format(
                                engine_type=engine_name,
                                version=b["version"],
                                platform=b["platform"]
                            )
                            binary_path = f"{resolved_target}{b['binary_name']}"
                except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                    pass  # Keep default binary_path

    # ===== Build LayeredMergeChain L1-L5 =====
    chain = LayeredMergeChain()

    with pool(db_path) as conn:
        # L1: Engine default configs
        ec_rows = conn.execute(
            "SELECT key, value FROM engine_configs WHERE engine_type_id = ?",
            (engine_type_id,),
        ).fetchall()
        layer1_env = {r[0]: r[1] for r in ec_rows}
        if layer1_env:
            chain.append(ConfigLevel(1, "engine_configs", env_vars=layer1_env))

        # L2+L3: Model + Preset
        if preset_id is not None:
            preset_row = conn.execute(
                "SELECT config_template, gpu_device, model_id FROM engine_presets WHERE id = ?",
                (preset_id,),
            ).fetchone()

            preset_gpu = preset_row["gpu_device"] if preset_row and preset_row["gpu_device"] else None

            # --- L2: Model params as base defaults ---
            model_env = {}
            model_model = {}
            if preset_row and preset_row["model_id"]:
                model_row = conn.execute(
                    "SELECT model_params, model_path, mmproj_path, draft_model_path "
                    "FROM engine_models WHERE id = ?",
                    (preset_row["model_id"],),
                ).fetchone()
                if model_row and model_row[0]:
                    mp = json.loads(model_row[0])
                    if isinstance(mp, dict):
                        wrapped = _wrap_flat_as_model(mp)
                        for k, v in wrapped.items():
                            if k.startswith("LLAMA_ARG_"):
                                model_env[k] = v
                            else:
                                model_model[k] = v

                    # Resolve model file paths
                    draft_val = _clean_model_val(model_row[3])
                    if draft_val and isinstance(draft_val, str) and draft_val.startswith('..ID..'):
                        parts = draft_val.split('..')
                        if len(parts) >= 3:
                            try:
                                ref_id = int(parts[2])
                                ref_row = conn.execute(
                                    "SELECT model_path FROM engine_models WHERE id = ?",
                                    (ref_id,),
                                ).fetchone()
                                if ref_row and ref_row["model_path"]:
                                    draft_val = ref_row["model_path"]
                            except (ValueError, IndexError):
                                pass

                    path_keys = {
                        1: ("LLAMA_ARG_MODEL", "model"),
                        2: ("LLAMA_ARG_MMPROJ", "mmproj_path"),
                        3: ("LLAMA_ARG_SPEC_DRAFT_MODEL", "draft_model_path"),
                    }
                    for idx, (env_key, model_key) in path_keys.items():
                        val = draft_val if idx == 3 else _clean_model_val(model_row[idx])
                        if val:
                            model_env[env_key] = val
                            model_model[model_key] = val

            if model_env or model_model:
                chain.append(ConfigLevel(2, "model_definition", env_vars=model_env, model_params=model_model))

            # --- L3: Preset template ---
            if preset_row and preset_row[0]:
                preset_raw = json.loads(preset_row[0])
                is_structured = any(k in preset_raw for k in ("env", "cli_opts", "model"))

                if is_structured:
                    preset_env = preset_raw.get("env") or {}
                    preset_cli = list(preset_raw.get("cli_opts") or [])
                    preset_model = preset_raw.get("model") or {}
                else:
                    if supports_models:
                        preset_model = _wrap_flat_as_model(preset_raw)
                        preset_env = {}
                        preset_cli = []
                    else:
                        preset_env = preset_raw or {}
                        preset_model = {}
                        preset_cli = []

                cleaned = _clean_model_dict(preset_model)
                preset_metadata = {}
                if preset_gpu:
                    preset_env["qr_cluster_gpu_override"] = preset_gpu
                    preset_metadata["gpu_device"] = preset_gpu

                chain.append(ConfigLevel(3, "preset_template",
                                         env_vars=preset_env, cli_opts=preset_cli,
                                         model_params=cleaned, metadata=preset_metadata))

        # L5: Instance override
        if inst.get("config_override"):
            override_raw = _parse_config_override(inst["config_override"])
            has_structured = isinstance(override_raw, dict) and any(
                k in override_raw for k in ("env", "cli_opts", "model")
            )
            has_flat = isinstance(override_raw, dict) and len(override_raw) > 0

            if has_structured:
                ov_env = dict(override_raw.get("env") or {})
                ov_cli = list(override_raw.get("cli_opts") or [])
                ov_model = override_raw.get("model") or {}
                # GPU override aliases at top-level of config_override — merge into env
                for alias in ("gpu_override", "device", "qr_cluster_gpu_override"):
                    if alias in override_raw and override_raw[alias] is not None:
                        ov_env["qr_cluster_gpu_override"] = override_raw[alias]
            elif supports_models and has_flat:
                # Build/engine config keys go into env — these are used directly by playbooks,
                # not as model inference parameters. Without this, per-instance overrides for
                # build keys (e.g., git_clone_url, node_build_run_cmd) would land in model
                # section and fail to override the engine_default value from Layer 1.
                _BUILD_KEYS = {
                    "binary_path", "git_clone_url", "model_root_path", "node_build_dir",
                    "node_build_install_depends", "node_build_run_cmd", "node_build_set_cmd",
                    "node_git_pull_cmd", "node_src_dir", "restart_policy", "skip_build",
                    "start_on_boot", "base_port",
                }
                # NOTE: qr_cluster_gpu_override goes into env — it's a GPU device name, not a file path.
                # If it lands in model, _resolve_model_paths() post-cleanup will strip it out
                # because "none" doesn't end with .gguf/.bin/.model.
                ov_env = {}
                ov_model = {}
                for k, v in override_raw.items():
                    # Normalize gpu_override/device aliases to canonical name for env
                    if k in ("gpu_override", "device"):
                        k = "qr_cluster_gpu_override"
                    if k.startswith("LLAMA_ARG_") or k == "qr_cluster_gpu_override" or k in _BUILD_KEYS:
                        ov_env[k] = v
                    else:
                        wrapped = _wrap_flat_as_model({k: v})
                        ov_model.update(wrapped)
                ov_cli = []
            else:
                ov_env = override_raw if isinstance(override_raw, dict) else {}
                ov_model = {}
                ov_cli = []

            cleaned_ov = _clean_model_dict(ov_model)
            chain.append(ConfigLevel(5, "instance_override",
                                     env_vars=ov_env, cli_opts=ov_cli, model_params=cleaned_ov))

    # ===== L7: Cluster bindings (highest precedence) =====
    split_mode = inst.get("split_mode") or DEFAULT_SPLIT_MODE

    bind_ids = _parse_bind_ids(inst.get("rpc_bind_ids"))
    rpc_bindings = []

    with pool(db_path) as conn:
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
                    pass

    # Compute cluster-specific CLI args and env modifications
    base_cli = []
    host = None
    port = inst.get("port_assigned")

    # Read host from merged env (L1-L5 already merged)
    chain_result, _ = chain.get_merged()
    host = chain_result.get("env", {}).get("LLAMA_ARG_HOST")

    if host:
        _host_str = str(host)
        if ":" in _host_str and "." not in _host_str:
            _host_str = f"[{_host_str}]"
        base_cli.extend(["--host", _host_str])
    if port:
        base_cli.extend(["--port", str(port)])

    # GPU Override: read from env (qr_cluster_gpu_override now lands in env, not model,
    # since it's a GPU device name not a file path — avoids _resolve_model_paths() mangling)
    # get_merged() returns {"env": ..., "cli_opts": ..., "model": ...}
    gpu_override = chain_result.get("env", {}).get("qr_cluster_gpu_override") or None
    preset_cli_opts = list(chain_result.get("cli_opts", []))

    # Extract base device from preset cli_opts (format: "-dev <value>")
    base_dev = None
    while "-dev" in preset_cli_opts:
        idx = preset_cli_opts.index("-dev")
        if idx + 1 < len(preset_cli_opts):
            base_dev = preset_cli_opts[idx + 1]
        if idx + 1 < len(preset_cli_opts):
            preset_cli_opts.pop(idx + 1)
        preset_cli_opts.pop(idx)

    # Determine if server has explicit -dev to avoid false tensor_split expansion
    has_explicit_device = (gpu_override is not None) or (base_dev is not None and base_dev != "none")
    
    gpu_slot, tensor_split_str = _compute_tensor_split(
        inst.get("split"), [str(b["split"]) for b in rpc_bindings], has_explicit_device)

    # Build -dev flag: GPU override > base_dev > RPC refs
    # NOTE: gpu_override can be "none" — this is a VALID option meaning "no GPU devices"
    # (explicitly tell llama-server to use CPU only, distinct from omitting -dev entirely)
    dev_refs = []
    if gpu_override:
        dev_refs.append(gpu_override)
    elif base_dev is not None and base_dev != "none":
        dev_refs.append(base_dev)
    dev_refs += [f"RPC{n}" for n in range(len(rpc_bindings))]

    # Build --rpc endpoints
    rpc_endpoints = [f"{b['hostname']}:{b['port_assigned']}" for b in rpc_bindings]

    # Build draft device flags — consolidated single --device-draft RPC0,RPC1,...
    draft_indices = [f"RPC{idx}" for idx, b in enumerate(rpc_bindings) if int(b.get("draft") or 0) > 0]
    draft_devices_cli = []
    if draft_indices:
        draft_devices_cli.extend(["--device-draft", ",".join(draft_indices)])

   # Expert split flags: generated from per-RPC expert-split config
    raw_co = inst.get("config_override") or "{}"
    expert_config = {}
    try:
        co_data = json.loads(raw_co) if isinstance(raw_co, str) else {}
        if isinstance(co_data, dict):
            expert_config = co_data.get("expert_split", {})
    except (json.JSONDecodeError, TypeError):
        pass

    expert_flags = _generate_expert_split_flags(inst, rpc_bindings, expert_config)

    # Build final CLI args: base + preset_opts + cluster additions
    cli_parts = base_cli + preset_cli_opts
    if rpc_bindings:
        cli_parts.extend(["--rpc", ",".join(rpc_endpoints)])
        cli_parts.extend(["-dev", ",".join(dev_refs)])
        cli_parts.extend(draft_devices_cli)
        # Insert expert flags after draft, before custom CLI flags
        for ef in expert_flags:
            cli_parts.append(ef)
    else:
        # Standalone: just -dev from gpu_override or base_dev
        if gpu_override:
            cli_parts.extend(["-dev", gpu_override])
        elif base_dev is not None and base_dev != "none":
            cli_parts.extend(["-dev", base_dev])

    # Log level: append --verbose flag when LLAMA_ARG_LOG_LEVEL is info or debug
    log_level = chain_result.get("env", {}).get("LLAMA_ARG_LOG_LEVEL", "off")
    if log_level in ("info", "debug"):
        cli_parts.append("--verbose")

    # Instance-level custom CLI flags from config_override (unified herd state), fallback to column
    co_raw = inst.get("config_override") or "{}"
    imported_flags = []
    try:
        co_data = json.loads(co_raw) if isinstance(co_raw, str) else {}
        if isinstance(co_data, dict):
            imported_flags = co_data.get("cli_flags", [])
    except (json.JSONDecodeError, TypeError):
        pass
    if not imported_flags:
        # Fallback to legacy cli_flags column
        raw_flags = inst.get("cli_flags") or "[]"
        try:
            imported_flags = json.loads(raw_flags) if isinstance(raw_flags, str) else []
            if not isinstance(imported_flags, list):
                imported_flags = []
        except (json.JSONDecodeError, TypeError):
            imported_flags = []

    for flag in imported_flags:
        if isinstance(flag, str) and flag.strip():
            cli_parts.append(flag.strip())

    # HERD-CLI-DUP: Deduplicate CLI flags by flag name, keeping LAST occurrence.
    # Also strips --host/--port for llama_server (handled by LLAMA_ARG_HOST/PORT env vars).
    # NOTE: instances table has engine_type_id column but NOT engine_type_name —
    # use the ID directly to avoid comparing against a missing DB field.
    _strip_flags = set()
    if inst.get("engine_type_id") == QR_ENGINE_LLAMA_SERVER:
        _strip_flags.add("--host")
        _strip_flags.add("--port")

    # Parse cli_parts into (flag_key, flag_value_or_None) pairs, then dedup by key.
    # Iterates backwards to keep the last occurrence of each flag while preserving order.
    _pairs = []  # [(key, value_or_None), ...]
    i = 0
    while i < len(cli_parts):
        part = cli_parts[i]
        if isinstance(part, str) and part.startswith("--"):
            # Long flag: check if next element is its value (doesn't start with -)
            nxt = cli_parts[i + 1] if i + 1 < len(cli_parts) else None
            if isinstance(nxt, str) and not nxt.startswith("-"):
                _pairs.append((part, nxt))
                i += 2
            elif " " in part:
                # Combined format flag: "--flag value" → split into (key, value).
                # Handles mixed-format presets where some cli_opts are combined
                # (single array element) while others are separate (two elements).
                idx = part.index(" ")
                _pairs.append((part[:idx], part[idx+1:]))
                i += 1
            else:
                _pairs.append((part, None))
                i += 1
        elif isinstance(part, str) and part.startswith("-") and len(part) == 2:
            # Short flag like "-v" — single element, value goes in separate slot
            _pairs.append((part, None))
            i += 1
        else:
            # Free-form value (e.g., positional args) — keep as-is
            _pairs.append((str(part), None))
            i += 1

    # Deduplicate: iterate backwards, keep only first seen key (last in original order).
    # Also removes flags listed in _strip_flags (--host/--port for llama_server).
    _seen = set()
    _deduped = []
    for key, val in reversed(_pairs):
        if key not in _seen:
            if _strip_flags and key in _strip_flags:
                continue  # skip this flag entirely
            _seen.add(key)
            _deduped.append((key, val))
    _deduped.reverse()

    # Flatten back to list
    cli_parts_final = []
    for key, val in _deduped:
        cli_parts_final.append(key)
        if val is not None:
            cli_parts_final.append(val)

    cli_args = " ".join(str(x) for x in cli_parts_final)

    # Build L7 cluster bindings ConfigLevel
    # env: tensor_split + split_mode + binary_path; remove qr_cluster_gpu_override
    cluster_env = {
        "LLAMA_ARG_TENSOR_SPLIT": tensor_split_str,
        "binary_path": binary_path,
    }
    if split_mode:
        cluster_env["LLAMA_ARG_SPLIT_MODE"] = split_mode
    # Remove qr_cluster_gpu_override — device is set via CLI -dev flag, not env var
    # (The merge will remove it since we add it with None sentinel)

    # Build L7 CLI opts from cluster additions
    cluster_cli = [f"--rpc {','.join(rpc_endpoints)}"] if rpc_bindings else []
    if gpu_override:
        cluster_cli.extend(["-dev", gpu_override])
    elif base_dev is not None and base_dev != "none":
        cluster_cli.extend(["-dev", base_dev])
    cluster_cli.extend(draft_devices_cli)

    # Remove qr_cluster_gpu_override from the L7 env layer (null sentinel)
    cluster_env["qr_cluster_gpu_override"] = ""  # Empty string removes the key

    chain.append(ConfigLevel(7, "cluster_bindings",
                             env_vars=cluster_env, cli_opts=cluster_cli))

    # Get final merged result
    final_merged, _ = chain.get_merged()

    # Override LLAMA_ARG_PORT from instance port_assigned so the env file
    # reflects the actual allocated port instead of the engine_configs default.
    if port:
        final_merged["env"]["LLAMA_ARG_PORT"] = str(port)

    # Inject per-instance auth token into merged env.
    # NULL/empty auth_token → skip LLAMA_API_KEY entirely (no auth on server).
    _token = inst.get("auth_token")
    if _token:
        final_merged["env"]["LLAMA_API_KEY"] = _token

    return {
        "env": final_merged["env"],
        "cli_args": cli_args,
        "tensor_split_str": tensor_split_str,
        "split_mode": split_mode,
        "rpc_bindings": rpc_bindings,
        "bind_count": len(rpc_bindings),
        "build_command": final_merged.get("env", {}).get("node_build_set_cmd", ""),
        "gpu_override": gpu_override,
        "expert_flags": expert_flags,
    }


def build_rpc_server_env(db_path, instance_id):
    """Build complete environment dict + CLI args for an RPC server deployment.

    Uses LayeredMergeChain (CONFIG-1 Phase 2) to build the config:
      L1-L5: Standard merge chain
      L7: RPC bindings (-H host -p port -d device) — highest precedence

    Args:
        db_path: Path to SQLite database.
        instance_id: Integer primary key of the RPC instance.

    Returns:
        dict with keys:
            env:      Complete merged env dict (ready for playbook)
            cli_args: Pre-joined RPC CLI string
    """
    from db.sqlite import pool
    from lib.lib_config_merge import _parse_config_override, _wrap_flat_as_model, _clean_model_dict

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise ClusterEnvError(f"Instance {instance_id} not found")
        inst = dict(row)

    # ===== Build LayeredMergeChain L1-L5 =====
    chain = LayeredMergeChain()

    with pool(db_path) as conn:
        engine_type_id = inst["engine_type_id"]
        preset_id = inst.get("preset_id")

        ec_rows = conn.execute(
            "SELECT key, value FROM engine_configs WHERE engine_type_id = ?",
            (engine_type_id,),
        ).fetchall()
        layer1_env = {r[0]: r[1] for r in ec_rows}
        if layer1_env:
            chain.append(ConfigLevel(1, "engine_configs", env_vars=layer1_env))

        if preset_id is not None:
            preset_row = conn.execute(
                "SELECT config_template, gpu_device, model_id FROM engine_presets WHERE id = ?",
                (preset_id,),
            ).fetchone()

            preset_gpu = preset_row["gpu_device"] if preset_row and preset_row["gpu_device"] else None

            # BINARY-DL: Resolve binary path from preset's binary_id template
            # Override layer1_env binary_path so it flows through chain.merge
            if preset_row and preset_row["config_template"]:
                try:
                    ct = json.loads(preset_row["config_template"])
                    b_id = ct.get("binary_id")
                    if b_id:
                        bin_row = conn.execute(
                            "SELECT * FROM engine_binaries WHERE id=? AND is_active=1",
                            (b_id,),
                        ).fetchone()
                        if bin_row and bin_row["template_type"] == "binary":
                            b = dict(bin_row)
                            resolved_target = b["target_path"].format(
                                engine_type="llama_rpc",
                                version=b["version"],
                                platform=b["platform"]
                            )
                            layer1_env["binary_path"] = f"{resolved_target}{b['binary_name']}"
                except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                    pass

            # L2: Model params
            model_env = {}
            model_model = {}
            if preset_row and preset_row["model_id"]:
                model_row = conn.execute(
                    "SELECT model_params, model_path, mmproj_path, draft_model_path "
                    "FROM engine_models WHERE id = ?",
                    (preset_row["model_id"],),
                ).fetchone()
                if model_row and model_row[0]:
                    mp = json.loads(model_row[0])
                    if isinstance(mp, dict):
                        wrapped = _wrap_flat_as_model(mp)
                        for k, v in wrapped.items():
                            if k.startswith("LLAMA_ARG_"):
                                model_env[k] = v
                            else:
                                model_model[k] = v

                    draft_val = _clean_model_val(model_row[3])
                    if draft_val and isinstance(draft_val, str) and draft_val.startswith('..ID..'):
                        parts = draft_val.split('..')
                        if len(parts) >= 3:
                            try:
                                ref_id = int(parts[2])
                                ref_row = conn.execute(
                                    "SELECT model_path FROM engine_models WHERE id = ?",
                                    (ref_id,),
                                ).fetchone()
                                if ref_row and ref_row["model_path"]:
                                    draft_val = ref_row["model_path"]
                            except (ValueError, IndexError):
                                pass

                    path_keys = {
                        1: ("LLAMA_ARG_MODEL", "model"),
                        2: ("LLAMA_ARG_MMPROJ", "mmproj_path"),
                        3: ("LLAMA_ARG_SPEC_DRAFT_MODEL", "draft_model_path"),
                    }
                    for idx, (env_key, model_key) in path_keys.items():
                        val = draft_val if idx == 3 else _clean_model_val(model_row[idx])
                        if val:
                            model_env[env_key] = val
                            model_model[model_key] = val

            if model_env or model_model:
                chain.append(ConfigLevel(2, "model_definition", env_vars=model_env, model_params=model_model))

            # L3: Preset template
            if preset_row and preset_row[0]:
                preset_raw = json.loads(preset_row[0])
                is_structured = any(k in preset_raw for k in ("env", "cli_opts", "model"))
                if is_structured:
                    preset_env = preset_raw.get("env") or {}
                    preset_cli = list(preset_raw.get("cli_opts") or [])
                    preset_model = preset_raw.get("model") or {}
                else:
                    preset_env = preset_raw or {}
                    preset_model = {}
                    preset_cli = []

                cleaned = _clean_model_dict(preset_model)
                preset_metadata = {}
                if preset_gpu:
                    preset_env["qr_cluster_gpu_override"] = preset_gpu
                    preset_metadata["gpu_device"] = preset_gpu

                chain.append(ConfigLevel(3, "preset_template",
                                         env_vars=preset_env, cli_opts=preset_cli,
                                         model_params=cleaned, metadata=preset_metadata))

        # L5: Instance override
        if inst.get("config_override"):
            override_raw = _parse_config_override(inst["config_override"])
            has_structured = isinstance(override_raw, dict) and any(
                k in override_raw for k in ("env", "cli_opts", "model")
            )
            has_flat = isinstance(override_raw, dict) and len(override_raw) > 0

            if has_structured:
                ov_env = dict(override_raw.get("env") or {})
                ov_cli = list(override_raw.get("cli_opts") or [])
                ov_model = override_raw.get("model") or {}
                # GPU override aliases at top-level of config_override — merge into env
                for alias in ("gpu_override", "device", "qr_cluster_gpu_override"):
                    if alias in override_raw and override_raw[alias] is not None:
                        ov_env["qr_cluster_gpu_override"] = override_raw[alias]
            elif has_flat:
                # Build/engine config keys go into env — same exception list as llama_server builder.
                _BUILD_KEYS = {
                    "binary_path", "git_clone_url", "model_root_path", "node_build_dir",
                    "node_build_install_depends", "node_build_run_cmd", "node_build_set_cmd",
                    "node_git_pull_cmd", "node_src_dir", "restart_policy", "skip_build",
                    "start_on_boot", "base_port",
                }
                ov_env = {}
                ov_model = {}
                for k, v in override_raw.items():
                    # Normalize gpu_override/device aliases to canonical name for env
                    if k in ("gpu_override", "device"):
                        k = "qr_cluster_gpu_override"
                    if k.startswith("LLAMA_ARG_") or k == "qr_cluster_gpu_override" or k in _BUILD_KEYS:
                        ov_env[k] = v
                    else:
                        wrapped = _wrap_flat_as_model({k: v})
                        ov_model.update(wrapped)
                ov_cli = []
            else:
                ov_env = override_raw if isinstance(override_raw, dict) else {}
                ov_model = {}
                ov_cli = []

            cleaned_ov = _clean_model_dict(ov_model)
            chain.append(ConfigLevel(5, "instance_override",
                                     env_vars=ov_env, cli_opts=ov_cli, model_params=cleaned_ov))

    # ===== L7: RPC bindings (highest precedence) =====
    chain_result, _ = chain.get_merged()

    # Build CLI args: -H {host} -p {port} -d {device} + remaining preset cli_opts
    device = None
    if chain_result.get("env", {}).get("qr_cluster_gpu_override"):
        device = chain_result["env"]["qr_cluster_gpu_override"]
    for i in range(len(chain_result.get("cli_opts", [])) - 1):
        if chain_result["cli_opts"][i] == "-d" and i + 1 < len(chain_result["cli_opts"]):
            device = chain_result["cli_opts"][i + 1]
    if inst.get("gpu_device"):
        device = inst["gpu_device"]

    host = chain_result.get("env", {}).get("LLAMA_ARG_HOST") or DEFAULT_RPC_BIND_HOST
    port = inst.get("port_assigned", 0) or 0

    if host != "0.0.0.0":
        _host_check = str(host)
        if ":" in _host_check and "." not in _host_check:
            host = f"[{_host_check}]"

    # Build base cli_args from config, then append remaining preset cli_opts
    extra_opts = []
    opts_list = chain_result.get("cli_opts", []) if isinstance(chain_result.get("cli_opts"), list) else []
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
        "env": chain_result["env"],
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

        # Check for GPU override from config_override to determine explicit device
        inst_co_raw = inst.get("config_override") or "{}"
        gpu_override_summary = None
        try:
            co_data = json.loads(inst_co_raw) if isinstance(inst_co_raw, str) else {}
            if isinstance(co_data, dict):
                gpu_override_summary = co_data.get("qr_cluster_gpu_override") or None
        except (json.JSONDecodeError, TypeError):
            pass

        has_explicit_device_summary = (gpu_override_summary is not None) or (base_dev is not None and base_dev != "none")

        # Compute tensor_split using extracted helper
        gpu_slot, tensor_split = _compute_tensor_split(inst.get("split"), rpc_splits, has_explicit_device_summary)

        # Parse cli_flags from config_override (unified herd state), fallback to column
        instance_cli_flags = []
        co_raw = inst.get("config_override") or "{}"
        try:
            co_data = json.loads(co_raw) if isinstance(co_raw, str) else {}
            if isinstance(co_data, dict):
                instance_cli_flags = co_data.get("cli_flags", [])
        except (json.JSONDecodeError, TypeError):
            pass
        if not instance_cli_flags:
            # Fallback to legacy cli_flags column
            raw_flags = inst.get("cli_flags") or "[]"
            try:
                instance_cli_flags = json.loads(raw_flags) if isinstance(raw_flags, str) else []
                if not isinstance(instance_cli_flags, list):
                    instance_cli_flags = []
            except (json.JSONDecodeError, TypeError):
                pass
        # Check for GPU override from config_override
        gpu_override = None
        try:
            co_data2 = json.loads(co_raw) if isinstance(co_raw, str) else {}
            if isinstance(co_data2, dict):
                gpu_override = co_data2.get("qr_cluster_gpu_override") or None
        except (json.JSONDecodeError, TypeError):
            pass
        # Resolve draft devices: RPCs with draft > 0 get --device-draft RPC<N>
        draft_devices = []
        for idx, b in enumerate(rpc_bindings):
            d = b.get("draft", 0)
            if isinstance(d, int) and d > 0:
                draft_devices.append(f"RPC{idx}")

        # Compute available actions for this instance state
        try:
            from lib.lib_engine_actions import get_available_actions
            actions = get_available_actions("llama_server", inst.get("state") or "unknown")
        except Exception as _e:
            actions = []

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
            "actions": actions,
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
            if rpc_row["state"] != QR_STATE_RUNNING:
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


# ============================================================
# Timestamp Proxy Environment Builder
# ============================================================


def resolve_target_instances(db_path, target_ids):
    """Resolve target instance IDs to host, port, and auth_token.

    Looks up each target instance from the DB and extracts:
    - hostname (node_hostname or ipv4_address)
    - port_assigned
    - auth_token (for proxying requests with the target's token)

    Args:
        db_path: Path to SQLite database.
        target_ids: List of integer instance IDs.

    Returns:
        List of dicts: [{instance_id, name, hostname, port_assigned, auth_token}]
        Skips targets that are not found or have no valid host/port.
    """
    from db.sqlite import pool
    results = []
    if not target_ids:
        return results

    with pool(db_path) as conn:
        placeholders = ','.join('?' for _ in target_ids)
        rows = conn.execute(
            f"SELECT id, name, node_hostname, port_assigned, "
            f"config_override, auth_token FROM instances "
            f"WHERE id IN ({placeholders})",
            target_ids,
        ).fetchall()

    for row in rows:
        rid = row["id"]
        hostname = row["node_hostname"] or ""
        port = row["port_assigned"]

        if not hostname or not port:
            continue  # incomplete target — skip

        # Parse config_override to get LLAMA_ARG_HOST (bind address override)
        co_data = row["config_override"] or "{}"
        if isinstance(co_data, str):
            try:
                co_data = json.loads(co_data)
            except (json.JSONDecodeError, TypeError):
                co_data = {}

        # Use LLAMA_ARG_HOST from config_override if set, otherwise use node_hostname
        host = co_data.get("LLAMA_ARG_HOST", "") or hostname
        auth_token = row["auth_token"] or ""

        results.append({
            "instance_id": rid,
            "name": row["name"],
            "hostname": host,
            "port_assigned": port,
            "auth_token": auth_token,
        })

    return results


def build_timestamp_proxy_env(db_path, instance_id):
    """Build complete environment dict for a timestamp_proxy deployment.

    Resolution chain:
      L1: Engine configs (ts_proxy_inject_*, ts_proxy_timestamp_*)
      L2: Instance config_override (overrides L1 defaults)
      L3: Resolved target instances (from target_instance_ids column)
          → TS_PROXY_TARGET_N_HOST, TS_PROXY_TARGET_N_PORT, TS_PROXY_TARGET_N_TOKEN

    Args:
        db_path: Path to SQLite database.
        instance_id: Integer primary key of the timestamp_proxy instance.

    Returns:
        dict with keys:
            env:      Complete merged env dict (ready for playbook template)
            cli_args: Empty string (timestamp_proxy uses env-driven config only)
    """
    from db.sqlite import pool
    from lib.lib_config_merge import _parse_config_override

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise ClusterEnvError(f"Instance {instance_id} not found")
        inst = dict(row)

        engine_type_id = inst["engine_type_id"]
        port = inst.get("port_assigned")

    # ===== L1: Engine config defaults =====
    chain = LayeredMergeChain()
    layer1_env = {}
    with pool(db_path) as conn:
        ec_rows = conn.execute(
            "SELECT key, value FROM engine_configs WHERE engine_type_id = ?",
            (engine_type_id,),
        ).fetchall()
        for r in ec_rows:
            layer1_env[r["key"]] = r["value"]

    if layer1_env:
        chain.append(ConfigLevel(1, "engine_configs", env_vars=layer1_env))

    # ===== L2: Instance config_override (TS_PROXY_* keys) =====
    co_raw = inst.get("config_override") or "{}"
    co_dict = _parse_config_override(co_raw)
    if co_dict:
        # Only extract TS_PROXY_* keys for the env layer
        ts_env = {k: v for k, v in co_dict.get("env", {}).items()
                  if k.startswith("TS_PROXY_") or k.startswith("ts_proxy_")}
        if ts_env:
            chain.append(ConfigLevel(2, "config_override", env_vars=ts_env))

    # ===== L3: Resolve target instances =====
    target_ids = inst.get("target_instance_ids") or []
    if isinstance(target_ids, str):
        try:
            target_ids = json.loads(target_ids)
        except (json.JSONDecodeError, TypeError):
            target_ids = []

    targets = resolve_target_instances(db_path, target_ids)

    # Add resolved targets to merged env
    target_env = {}
    for i, t in enumerate(targets):
        prefix = f"TS_PROXY_TARGET_{i}"
        target_env[f"{prefix}_HOST"] = t["hostname"]
        target_env[f"{prefix}_PORT"] = str(t["port_assigned"])
        if t["auth_token"]:
            target_env[f"{prefix}_TOKEN"] = t["auth_token"]

    if target_env:
        chain.append(ConfigLevel(3, "target_resolution", env_vars=target_env))

    # ===== Merge =====
    merged, _ = chain.get_merged()

    # Inject port_assigned as TS_PROXY_PORT
    if port:
        merged["env"]["TS_PROXY_PORT"] = str(port)

    return {
        "env": merged["env"],
        "cli_args": "",
    }
