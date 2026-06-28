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

"""Quickrobot — Canonical config merge module (CONFIG-1 Phase 2).

Single source of truth for building merged deployment configuration from
engine defaults, presets, models, and per-instance overrides.

Uses the extensible LayeredMergeChain (CONFIG-1 Phase 2) internally while
preserving backward-compatible external API.

Functions:
    build_merged_config(db_path, instance_id, node_id=None)
        Build complete merged config — same signature as before.
    build_config_layers(db_path, instance_id, node_id=None)
        New: returns (merged_dict, layers_dict) for introspection.
    _deep_merge(base, override)
        Recursive deep-merge; null/empty sentinel removes keys from output.
    _resolve_model_paths(merged_config, node_id, db_path)
        Resolve relative model paths against node-level base path.
"""

import json

from lib.lib_config_level import (
    ConfigLevel,
    LayeredMergeChain,
    make_env_layer,
)
from lib.qr_engine_ids import QR_ENGINE_LLAMA_RPC, get_id_by_name, _QR_MODEL_PATH_PLACEHOLDERS


class MergeError(Exception):
    """Raised on merge-specific errors."""


# Model path placeholder values — from SSOT _QR_MODEL_PATH_PLACEHOLDERS


def _clean_model_val(val):
    """Clean a model path value — strip whitespace, skip placeholders."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in _QR_MODEL_PATH_PLACEHOLDERS:
        return None
    return s


def _clean_model_dict(d):
    """Remove placeholder values from model dict (recursive for nested dicts)."""
    if not isinstance(d, dict):
        return d
    result = {}
    for k, v in d.items():
        clean = _clean_model_val(v)
        if clean is not None:
            result[k] = clean
        elif isinstance(v, dict):
            nested = _clean_model_dict(v)
            if nested:
                result[k] = nested
    return result


def _resolve_draft_cross_ref(model_dict, db_path, raise_on_missing=False):
    """Resolve '..ID..NNN' cross-references in draft model fields.

    Handles both 'draft_model_path' and 'LLAMA_ARG_SPEC_DRAFT_MODEL' keys.
    If the value starts with '..ID..' it is resolved to the referenced
    model's model_path from the database.  Non-cross-reference values
    are left untouched so file-path drafts continue to work.

    Args:
        model_dict: Dict with draft model fields to resolve (modified in-place).
        db_path: Path to the SQLite database for lookup.
        raise_on_missing: If True, raises ValueError when resolution fails
            instead of silently keeping the unresolved '..ID..NNN' string.
            Used during deploy to prevent broken refs from reaching the env file.

    Returns:
        The model_dict with draft fields resolved in-place.

    Raises:
        ValueError: If raise_on_missing=True and a cross-reference cannot
            be resolved (referenced model ID does not exist).
    """
    DRAFT_KEYS = ("draft_model_path", "LLAMA_ARG_SPEC_DRAFT_MODEL")
    for key in DRAFT_KEYS:
        val = model_dict.get(key)
        if not isinstance(val, str) or not val.startswith('..ID..'):
            continue
        parts = val.split('..')
        if len(parts) < 3:
            continue
        try:
            ref_id = int(parts[2])
            from db.sqlite import pool
            with pool(db_path) as conn:
                ref_row = conn.execute(
                    "SELECT model_path FROM engine_models WHERE id = ?",
                    (ref_id,),
                ).fetchone()
            if ref_row and ref_row["model_path"]:
                model_dict[key] = ref_row["model_path"]
            elif raise_on_missing:
                raise ValueError(
                    f"Draft cross-reference '{val}' resolved to model ID {ref_id} "
                    f"which does not exist in engine_models"
                )
        except (ValueError, IndexError) as exc:
            if raise_on_missing:
                raise ValueError(
                    f"Invalid draft cross-reference '{val}': {exc}"
                ) from exc
            pass  # Keep original value if resolution fails
    return model_dict


# ---------------------------------------------------------------------------
# Core deep-merge with null/empty sentinel semantics
# ---------------------------------------------------------------------------


def _deep_merge(base, override, _layer=None):
    """Recursively deep-merge two dicts; override takes priority.

    Null/empty sentinel: if override value is None or empty string "",
    the key is removed from the output entirely (no VAR= line in env file).

    Args:
        base: The base configuration dict.
        override: The overriding configuration dict.
        _layer: Internal tracking for source annotation (not user-facing).

    Returns:
        New merged dict with source annotations appended as _source key.
    """
    result = {}
    # Copy all keys from base first
    for key, value in base.items():
        if isinstance(value, dict) and not key.startswith("_"):
            nested = _deep_merge(value, override.get(key, {}), _layer=_layer)
            result[key] = nested
        else:
            result[key] = value

    # Apply overrides on top
    for key, value in override.items():
        if key.startswith("_"):
            continue  # Skip internal annotation keys from override source
        if value is None or value == "":
            # Sentinel: remove key entirely from output
            result.pop(key, None)
        elif (key in result and isinstance(result[key], dict)
                and isinstance(value, dict)):
            # Recursive merge for nested dicts
            result[key] = _deep_merge(result[key], value, _layer=_layer)
        else:
            result[key] = value

    return result


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------


def _resolve_model_paths(merged_config, node_id, db_path, engine_type_id=None):
    """Resolve relative model file paths against the active base path.

    Priority:
    1. If path starts with "/" → already absolute, use as-is (backward compat)
    2. Look up nodes.model_base_path for this node → override
    3. Fall back to engine_configs.model_root_path (engine default)
    4. Concatenate: base_path + "/" + relative_path

    Args:
        merged_config: The merged config dict containing model section.
        node_id: Integer primary key of the node.
        db_path: Path to SQLite database.
        engine_type_id: Used for fallback to engine_configs.model_root_path.

    Returns:
        Merged config with resolved absolute paths in model section.
    """
    model = merged_config.get("model", {})
    if not model:
        return merged_config

    # Find the base path
    base_path = _lookup_model_base_path(node_id, db_path, engine_type_id=engine_type_id)
    if not base_path:
        return merged_config  # No base path available, keep paths as-is

    # Resolve each path in model section (skip placeholder values)
    for key in ("LLAMA_ARG_MODEL", "LLAMA_ARG_MMPROJ", "LLAMA_ARG_SPEC_DRAFT_MODEL"):
        path = _clean_model_val(model.get(key))
        if path and not str(path).startswith("/"):
            model[key] = f"{base_path}/{path}"

    # Post-resolution cleanup — remove values that aren't real paths
    for key in list(model.keys()):
        v = model[key]
        if isinstance(v, str):
            if v.startswith("%") or v == "/" or not any(
                v.endswith(ext) for ext in (".gguf", ".bin", ".model", "-00001-of-")
            ):
                model.pop(key, None)
                merged_config.get("env", {}).pop(key, None)

    return merged_config


def _lookup_model_base_path(node_id, db_path, engine_type_id=None):
    """Look up the active model base path for a node.

    Resolution order:
      1. Per-node override (nodes.model_base_path)
      2. Engine default (engine_configs.model_root_path)
      3. None — no source configured

    Returns:
        The base path string, or None if not configured.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        # Step 1: Check per-node override
        row = conn.execute(
            "SELECT model_base_path FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row and row["model_base_path"]:
            return row["model_base_path"]

        # Step 2: Fall back to engine default via engine_configs.model_root_path
        if engine_type_id:
            ec_row = conn.execute(
                "SELECT value FROM engine_configs WHERE engine_type_id = ? AND key = 'model_root_path'",
                (engine_type_id,),
            ).fetchone()
            if ec_row and ec_row["value"]:
                return ec_row["value"]

        return None


# ---------------------------------------------------------------------------
# Internal: Build LayeredMergeChain from DB data
# ---------------------------------------------------------------------------


def _build_chain(conn, engine_type_id, preset_id, inst_config_override,
                 supports_models, engine_name):
    """Build a LayeredMergeChain from database + instance data.

    Internal function called by build_merged_config / build_config_layers.

    Args:
        conn: Active SQLite connection.
        engine_type_id: Engine type ID.
        preset_id: Preset ID (may be None).
        inst_config_override: Raw config_override JSON string or dict.
        supports_models: Whether this engine supports model parameters.
        engine_name: Engine name string.

    Returns:
        LayeredMergeChain with L1-L5 layers populated.
    """
    chain = LayeredMergeChain()

    # ------------------------------------------------------------------
    # Layer 1: Engine default configs
    # ------------------------------------------------------------------
    ec_rows = conn.execute(
        "SELECT key, value FROM engine_configs WHERE engine_type_id = ?",
        (engine_type_id,),
    ).fetchall()
    layer1_env = {}
    for r in ec_rows:
        layer1_env[r[0]] = r[1]

    if layer1_env:
        chain.append(ConfigLevel(1, "engine_configs", env_vars=layer1_env))

    # ------------------------------------------------------------------
    # Layer 2+3: Preset + Model (model_params is base, preset overrides)
    # ------------------------------------------------------------------
    preset_row = None

    if preset_id is not None:
        preset_row = conn.execute(
            "SELECT config_template, gpu_device, model_id FROM engine_presets WHERE id = ?",
            (preset_id,),
        ).fetchone()

        preset_gpu = preset_row["gpu_device"] if preset_row and preset_row["gpu_device"] else None

        # --- Step A: Model params as base defaults (layer 2) ---
        model_layer_contribution = {}
        if preset_row and preset_row["model_id"]:
            model_row = conn.execute(
                "SELECT model_params, model_path, mmproj_path, draft_model_path "
                "FROM engine_models WHERE id = ?",
                (preset_row["model_id"],),
            ).fetchone()

            if model_row:
                # Resolve sampling params (model_params JSON column)
                mp = model_row[0]
                if mp:
                    model_params = json.loads(mp)
                    if isinstance(model_params, dict):
                        # Wrap short keys (temp, top_p, etc.) into LLAMA_ARG_ prefixed keys
                        wrapped = _wrap_flat_as_model(model_params)
                        model_section_keys = list(wrapped.keys())
                        model_layer_contribution = dict(model_params)

                # Resolve model file paths → env keys
                # Row indices: 0=model_params, 1=model_path, 2=mmproj_path, 3=draft_model_path
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
                            pass  # Keep original value if resolution fails

                path_keys = {
                    1: ("LLAMA_ARG_MODEL", "model"),
                    2: ("LLAMA_ARG_MMPROJ", "mmproj_path"),
                    3: ("LLAMA_ARG_SPEC_DRAFT_MODEL", "draft_model_path"),
                }
                for idx, (env_key, model_key) in path_keys.items():
                    val = draft_val if idx == 3 else _clean_model_val(model_row[idx])
                    if val:
                        model_layer_contribution[env_key] = val

        # Build L2 ConfigLevel from model params
        if model_layer_contribution:
            chain.append(ConfigLevel(2, "model_definition",
                                     env_vars={k: v for k, v in model_layer_contribution.items()
                                               if k.startswith("LLAMA_ARG_")},
                                     model_params={k: v for k, v in model_layer_contribution.items()
                                                   if not k.startswith("LLAMA_ARG_")}))

        # --- Step B: Preset config template overrides model defaults (layer 3) ---
        if preset_row and preset_row[0]:
            preset_raw = json.loads(preset_row[0])

            # Detect structured format vs flat legacy format
            is_structured = any(
                k in preset_raw for k in ("env", "cli_opts", "model")
            )

            if is_structured:
                preset_env = preset_raw.get("env") or {}
                preset_cli = list(preset_raw.get("cli_opts") or [])
                preset_model = preset_raw.get("model") or {}
            else:
                # Flat preset -> wrap based on engine model support
                if supports_models:
                    preset_model = _wrap_flat_as_model(preset_raw)
                    preset_env = {}
                    preset_cli = []
                else:
                    preset_env = preset_raw or {}
                    preset_model = {}
                    preset_cli = []

            # Resolve ..ID.. cross-references in preset draft model fields
            # raise_on_missing=True prevents unresolved refs from reaching the env file
            cleaned = _clean_model_dict(preset_model)
            if cleaned and isinstance(cleaned, dict):
                _resolve_draft_cross_ref(cleaned, conn, raise_on_missing=True)

            preset_metadata = {}
            if preset_gpu:
                preset_env["LLAMA_ARG_DEVICE"] = preset_gpu
                preset_metadata["gpu_device"] = preset_gpu

            chain.append(ConfigLevel(3, "preset_template",
                                     env_vars=preset_env,
                                     cli_opts=preset_cli,
                                     model_params=cleaned,
                                     metadata=preset_metadata))

    # ------------------------------------------------------------------
    # Layer 5: Instance override (FINAL — overrides everything)
    # ------------------------------------------------------------------
    has_structured_override = False
    if inst_config_override:
        override_raw = _parse_config_override(inst_config_override)
        has_structured_override = isinstance(override_raw, dict) and any(
            k in override_raw for k in ("env", "cli_opts", "model")
        )
        has_flat_override = isinstance(override_raw, dict) and len(override_raw) > 0

        if has_structured_override:
            ov_env = override_raw.get("env") or {}
            ov_cli = list(override_raw.get("cli_opts") or [])
            ov_model = override_raw.get("model") or {}
        elif supports_models and has_flat_override:
            # Legacy flat override for model engines: split into env (LLAMA_ARG_* keys) and model (other keys)
            ov_env = {}
            ov_model = {}
            for k, v in override_raw.items():
                if k.startswith("LLAMA_ARG_"):
                    ov_env[k] = v
                else:
                    wrapped = _wrap_flat_as_model({k: v})
                    ov_model.update(wrapped)
            ov_cli = []
        else:
            # Legacy flat override for non-model engines -> env section
            ov_env = override_raw if isinstance(override_raw, dict) else {}
            ov_model = {}
            ov_cli = []

        # Resolve ..ID.. cross-references in instance override draft model fields
        cleaned_ov = _clean_model_dict(ov_model)

        chain.append(ConfigLevel(5, "instance_override",
                                 env_vars=ov_env,
                                 cli_opts=ov_cli,
                                 model_params=cleaned_ov))

    return chain


# ---------------------------------------------------------------------------
# Metadata resolution (restart_policy, start_on_boot)
# ---------------------------------------------------------------------------


def _resolve_metadata(conn, engine_type_id, preset_id, inst_config_override):
    """Resolve restart_policy and start_on_boot from the full override chain.

    Args:
        conn: Active SQLite connection.
        engine_type_id: Engine type ID.
        preset_id: Preset ID (may be None).
        inst_config_override: Raw config_override value.

    Returns:
        Tuple of (restart_policy_str, start_on_boot_bool).
    """
    # Restart policy resolution: engine → node → preset → instance
    restart_policy = None

    ec_rows = conn.execute(
        "SELECT key, value FROM engine_configs WHERE engine_type_id = ?",
        (engine_type_id,),
    ).fetchall()
    for row in ec_rows:
        if row[0] == "restart_policy" and not restart_policy:
            restart_policy = row[1]

    # Preset restart_policy
    if preset_id is not None:
        preset_row = conn.execute(
            "SELECT config_template FROM engine_presets WHERE id = ?", (preset_id,)
        ).fetchone()
        if preset_row and preset_row[0]:
            preset_raw = json.loads(preset_row[0])
            if isinstance(preset_raw, dict) and "restart_policy" in preset_raw:
                restart_policy = preset_raw["restart_policy"]

    # Instance override restart_policy
    if inst_config_override:
        override_raw = _parse_config_override(inst_config_override)
        if isinstance(override_raw, dict) and "restart_policy" in override_raw:
            restart_policy = override_raw["restart_policy"]

    # Start_on_boot resolution: config_override > instance column (handled by caller) > engine_configs default
    sob_default = False
    for row in ec_rows:
        if row[0] == "start_on_boot":
            _cfg_val = str(row[1]).lower()
            sob_default = _cfg_val in ("true", "1", "yes")
            break

    return restart_policy or "no", sob_default


# ---------------------------------------------------------------------------
# Public API: backward-compatible entry point
# ---------------------------------------------------------------------------


def build_merged_config(db_path, instance_id, node_id=None):
    """Build complete merged config for an instance (single source of truth).

    Uses LayeredMergeChain internally (CONFIG-1 Phase 2) while preserving
    the exact same return shape as before:
        env: dict — merged environment variables
        cli_opts: list — merged CLI arguments
        model: dict — merged model parameters
        _layers: dict — source annotation mapping layer name to contributed keys
        restart_policy: str — effective restart policy
        start_on_boot: bool — effective boot enable state

    Args:
        db_path: Path to SQLite database.
        instance_id: Integer primary key of the instance.
        node_id: Integer primary key of the node (for model base path resolution).

    Returns:
        dict with keys: env, cli_opts, model, _layers, restart_policy, start_on_boot.
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if inst is None:
            raise MergeError(f"Instance {instance_id} not found")
        inst = dict(inst)

        engine_type_id = inst["engine_type_id"]
        preset_id = inst.get("preset_id")
        _node_id = node_id or inst.get("node_id")

        # Get engine type info for capabilities detection
        et_row = conn.execute(
            "SELECT capabilities, name FROM engine_types WHERE id = ?",
            (engine_type_id,),
        ).fetchone()
        cap_data = {}
        if et_row and et_row["capabilities"]:
            try:
                parsed = json.loads(et_row["capabilities"])
                cap_data = parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                pass

        engine_name = et_row["name"] if et_row else ""
        supports_models = bool(cap_data.get("supports_models", True))
        is_model_engine = supports_models and get_id_by_name(engine_name) != QR_ENGINE_LLAMA_RPC

    # Build chain and merge
    merged, source_map = _build_config_layers(db_path, instance_id, preset_id,
                                               inst.get("config_override"),
                                               supports_models, engine_name)

    # Post-merge: Model base path resolution
    merged = _resolve_model_paths(merged, _node_id, db_path, engine_type_id=engine_type_id)

    # Metadata
    with pool(db_path) as conn:
        restart_policy, sob_default = _resolve_metadata(conn, engine_type_id, preset_id,
                                                         inst.get("config_override"))

    # start_on_boot: config_override > instance column > engine_configs default
    start_on_boot = sob_default
    if inst.get("config_override"):
        override_raw = _parse_config_override(inst["config_override"])
        if isinstance(override_raw, dict) and "start_on_boot" in override_raw:
            sob_raw = override_raw["start_on_boot"]
            if isinstance(sob_raw, bool):
                start_on_boot = sob_raw
            elif isinstance(sob_raw, str):
                start_on_boot = sob_raw.lower() in ("true", "1", "yes")
            else:
                start_on_boot = bool(int(sob_raw))
        elif inst.get("start_on_boot") is not None:
            sob_raw = inst["start_on_boot"]
            if isinstance(sob_raw, bool):
                start_on_boot = sob_raw
            elif isinstance(sob_raw, str):
                start_on_boot = sob_raw.lower() in ("true", "1", "yes")
            else:
                start_on_boot = bool(int(sob_raw))

    # Build _layers annotation (same format as old code)
    layers = {}
    # TODO: implement actual layer tracking from source_map (currently placeholder)

    return {
        "env": merged["env"],
        "cli_opts": merged["cli_opts"],
        "model": merged["model"],
        "_layers": _build_layers_annotation(db_path, instance_id),
        "restart_policy": restart_policy,
        "start_on_boot": start_on_boot,
    }


def _build_layers_annotation(db_path, instance_id):
    """Build _layers source annotation dict (same format as original code).

    This is kept for backward compatibility with callers that inspect
    _layers to understand which layer contributed which keys.
    """
    from db.sqlite import pool
    layers = {}

    with pool(db_path) as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if not inst:
            return layers
        inst = dict(inst)

        engine_type_id = inst["engine_type_id"]
        preset_id = inst.get("preset_id")

        # L1: engine_default
        ec_rows = conn.execute(
            "SELECT key FROM engine_configs WHERE engine_type_id = ?",
            (engine_type_id,),
        ).fetchall()
        if ec_rows:
            layers["engine_default"] = {
                "env_keys": [r[0] for r in ec_rows],
                "cli_opts_count": 0,
                "model_keys": [],
            }

        # L2+L3: preset (model + preset_template combined in old code)
        if preset_id is not None:
            preset_row = conn.execute(
                "SELECT config_template, model_id FROM engine_presets WHERE id = ?",
                (preset_id,),
            ).fetchone()
            if preset_row and preset_row[0]:
                preset_raw = json.loads(preset_row[0])
                is_structured = any(k in preset_raw for k in ("env", "cli_opts", "model"))
                if is_structured:
                    env_keys = list((preset_raw.get("env") or {}).keys())
                    cli_count = len(preset_raw.get("cli_opts") or [])
                    model_keys = list((preset_raw.get("model") or {}).keys())
                else:
                    env_keys = []
                    cli_count = 0
                    model_keys = list(preset_raw.keys())

                layers["preset"] = {
                    "env_keys": env_keys,
                    "cli_opts_count": cli_count,
                    "model_keys": model_keys,
                }

                # Model layer
                if preset_row and preset_row[1]:
                    model_row = conn.execute(
                        "SELECT model_params FROM engine_models WHERE id = ?",
                        (preset_row["model_id"],),
                    ).fetchone()
                    if model_row and model_row[0]:
                        mp = json.loads(model_row[0])
                        if isinstance(mp, dict):
                            layers["model"] = {"keys": list(mp.keys())}

        # L5: instance_override
        if inst.get("config_override"):
            override_raw = _parse_config_override(inst["config_override"])
            if isinstance(override_raw, dict) and len(override_raw) > 0:
                has_structured = any(k in override_raw for k in ("env", "cli_opts", "model"))
                if has_structured:
                    ov_env_keys = list((override_raw.get("env") or {}).keys())
                    ov_cli_count = len(override_raw.get("cli_opts") or [])
                    ov_model_keys = list((override_raw.get("model") or {}).keys())
                else:
                    # Flat override (no env/cli_opts/model structure): all keys are env vars
                    ov_env_keys = list(override_raw.keys())
                    ov_cli_count = 0
                    ov_model_keys = []

                layers["instance_override"] = {
                    "env_keys": ov_env_keys,
                    "cli_opts_count": ov_cli_count,
                    "model_keys": ov_model_keys,
                }

        # L6: metadata
        layers["metadata"] = {}

    return layers


# ---------------------------------------------------------------------------
# NEW PUBLIC API: build_config_layers — returns layers dict for introspection
# ---------------------------------------------------------------------------


def build_config_layers(db_path, instance_id, node_id=None):
    """Build complete merged config with full layer annotations.

    New function (CONFIG-1 Phase 2) that returns both the merged config
    and a detailed per-layer breakdown for introspection and API exposure.

    Args:
        db_path: Path to SQLite database.
        instance_id: Integer primary key of the instance.
        node_id: Integer primary key of the node (for model base path resolution).

    Returns:
        Tuple of (merged_dict, layers_dict):
            merged_dict: Same shape as build_merged_config() return value
                        (env, cli_opts, model, restart_policy, start_on_boot)
            layers_dict: {
                "engine_default": ConfigLevel(...),
                "model_definition": ConfigLevel(...) or None,
                "preset_template": ConfigLevel(...) or None,
                "instance_override": ConfigLevel(...) or None,
            }
    """
    from db.sqlite import pool

    with pool(db_path) as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if inst is None:
            raise MergeError(f"Instance {instance_id} not found")
        inst = dict(inst)

        engine_type_id = inst["engine_type_id"]
        preset_id = inst.get("preset_id")
        _node_id = node_id or inst.get("node_id")

        et_row = conn.execute(
            "SELECT capabilities, name FROM engine_types WHERE id = ?",
            (engine_type_id,),
        ).fetchone()
        cap_data = {}
        if et_row and et_row["capabilities"]:
            try:
                parsed = json.loads(et_row["capabilities"])
                cap_data = parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                pass

        engine_name = et_row["name"] if et_row else ""
        supports_models = bool(cap_data.get("supports_models", True))

    # Build chain
    merged, _source_map = _build_config_layers(db_path, instance_id, preset_id,
                                                inst.get("config_override"),
                                                supports_models, engine_name)

    # Post-merge: Model base path resolution
    merged = _resolve_model_paths(merged, _node_id, db_path, engine_type_id=engine_type_id)

    # Metadata
    with pool(db_path) as conn:
        restart_policy, sob_default = _resolve_metadata(conn, engine_type_id, preset_id,
                                                         inst.get("config_override"))

    start_on_boot = sob_default
    if inst.get("config_override"):
        override_raw = _parse_config_override(inst["config_override"])
        if isinstance(override_raw, dict) and "start_on_boot" in override_raw:
            sob_raw = override_raw["start_on_boot"]
            if isinstance(sob_raw, bool):
                start_on_boot = sob_raw
            elif isinstance(sob_raw, str):
                start_on_boot = sob_raw.lower() in ("true", "1", "yes")
            else:
                start_on_boot = bool(int(sob_raw))

    layers_dict = {}

    # Rebuild layer objects for introspection
    with pool(db_path) as conn:
        # L1: engine_defaults
        ec_rows = conn.execute(
            "SELECT key, value FROM engine_configs WHERE engine_type_id = ?",
            (engine_type_id,),
        ).fetchall()
        if ec_rows:
            layer1_env = {r[0]: r[1] for r in ec_rows}
            layers_dict["engine_default"] = ConfigLevel(1, "engine_configs", env_vars=layer1_env)

        # L2+L3: model + preset
        if preset_id is not None:
            preset_row = conn.execute(
                "SELECT config_template, gpu_device, model_id FROM engine_presets WHERE id = ?",
                (preset_id,),
            ).fetchone()

            model_cl = None
            preset_cl = None

            if preset_row and preset_row["model_id"]:
                model_row = conn.execute(
                    "SELECT model_params, model_path, mmproj_path, draft_model_path "
                    "FROM engine_models WHERE id = ?",
                    (preset_row["model_id"],),
                ).fetchone()
                if model_row and model_row[0]:
                    model_params = json.loads(model_row[0])
                    if isinstance(model_params, dict):
                        wrapped = _wrap_flat_as_model(model_params)
                        # Separate LLAMA_ARG_* keys (env) from model keys
                        m_env = {}
                        m_model = {}
                        for k, v in wrapped.items():
                            if k.startswith("LLAMA_ARG_"):
                                m_env[k] = v
                            else:
                                m_model[k] = v

                        # Add path keys
                        draft_val = _clean_model_val(model_row[3])
                        path_keys = {
                            1: ("LLAMA_ARG_MODEL", "model"),
                            2: ("LLAMA_ARG_MMPROJ", "mmproj_path"),
                            3: ("LLAMA_ARG_SPEC_DRAFT_MODEL", "draft_model_path"),
                        }
                        for idx, (env_key, model_key) in path_keys.items():
                            val = draft_val if idx == 3 else _clean_model_val(model_row[idx])
                            if val:
                                m_env[env_key] = val
                                m_model[model_key] = val

                        if m_env or m_model:
                            model_cl = ConfigLevel(2, "model_definition",
                                                   env_vars=m_env, model_params=m_model)
                            layers_dict["model_definition"] = model_cl

            if preset_row and preset_row[0]:
                preset_raw = json.loads(preset_row[0])
                is_structured = any(k in preset_raw for k in ("env", "cli_opts", "model"))
                if is_structured:
                    p_env = preset_raw.get("env") or {}
                    p_cli = list(preset_raw.get("cli_opts") or [])
                    p_model = preset_raw.get("model") or {}
                else:
                    if supports_models:
                        p_model = _wrap_flat_as_model(preset_raw)
                        p_env = {}
                        p_cli = []
                    else:
                        p_env = preset_raw or {}
                        p_model = {}
                        p_cli = []

                cleaned = _clean_model_dict(p_model)
                meta = {}
                if preset_row["gpu_device"]:
                    p_env["LLAMA_ARG_DEVICE"] = preset_row["gpu_device"]
                    meta["gpu_device"] = preset_row["gpu_device"]

                preset_cl = ConfigLevel(3, "preset_template",
                                        env_vars=p_env, cli_opts=p_cli,
                                        model_params=cleaned, metadata=meta)
                layers_dict["preset_template"] = preset_cl

        # L5: instance_override
        if inst.get("config_override"):
            override_raw = _parse_config_override(inst["config_override"])
            if isinstance(override_raw, dict) and len(override_raw) > 0:
                has_structured = any(k in override_raw for k in ("env", "cli_opts", "model"))
                if has_structured:
                    ov_env = override_raw.get("env") or {}
                    ov_cli = list(override_raw.get("cli_opts") or [])
                    ov_model = override_raw.get("model") or {}
                elif supports_models:
                    ov_env = {}
                    ov_model = {}
                    for k, v in override_raw.items():
                        if k.startswith("LLAMA_ARG_"):
                            ov_env[k] = v
                        else:
                            wrapped = _wrap_flat_as_model({k: v})
                            ov_model.update(wrapped)
                    ov_cli = []
                else:
                    ov_env = override_raw
                    ov_model = {}
                    ov_cli = []

                cleaned_ov = _clean_model_dict(ov_model)
                layers_dict["instance_override"] = ConfigLevel(5, "instance_override",
                                                                env_vars=ov_env,
                                                                cli_opts=ov_cli,
                                                                model_params=cleaned_ov)

    result = {
        "env": merged["env"],
        "cli_opts": merged["cli_opts"],
        "model": merged["model"],
        "restart_policy": restart_policy or "no",
        "start_on_boot": start_on_boot,
    }

    return result, layers_dict


# ---------------------------------------------------------------------------
# Internal: build chain + merge (used by both public entry points)
# ---------------------------------------------------------------------------


def _build_config_layers(db_path, instance_id, preset_id, inst_config_override,
                         supports_models, engine_name):
    """Build chain and merge — shared internal logic.

    Args:
        db_path: For cross-ref resolution.
        instance_id: Instance ID (for query).
        preset_id: Preset ID.
        inst_config_override: Raw config_override value.
        supports_models: Whether engine supports model params.
        engine_name: Engine name for capabilities check.

    Returns:
        Tuple of (merged_dict, source_map).
    """
    from db.sqlite import pool

    chain = LayeredMergeChain()

    with pool(db_path) as conn:
        inst = conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if inst is None:
            raise MergeError(f"Instance {instance_id} not found")
        inst = dict(inst)

        engine_type_id = inst["engine_type_id"]

        # Build layers via internal helper
        chain = _build_chain(conn, engine_type_id, preset_id,
                             inst_config_override, supports_models, engine_name)

    merged, source_map = chain.get_merged()
    return merged, source_map


# ---------------------------------------------------------------------------
# Helpers (unchanged from original)
# ---------------------------------------------------------------------------


def _wrap_flat_as_model(flat_dict):
    """Wrap a flat dict's values into model section with LLAMA_ARG_ prefixes.

    Keys present in _FLAT_MODEL_KEYS get their mapped name.
    All other keys get the generic LLAMA_ARG_ prefix with underscore conversion.

    Args:
        flat_dict: A flat dictionary of string key-value pairs.

    Returns:
        dict suitable for the model section of merged config.
    """
    _FLAT_MODEL_KEYS = {
        "gpu_layers": "LLAMA_ARG_N_GPU_LAYERS",
        "context_size": "LLAMA_ARG_CTX_SIZE",
        "batch_size": "LLAMA_ARG_BATCH_SIZE",
        "mmap": "LLAMA_ARG_MMAP",
        "temp": "LLAMA_ARG_TEMP",
        "top_p": "LLAMA_ARG_TOP_P",
        "top_k": "LLAMA_ARG_TOP_K",
        "min_p": "LLAMA_ARG_MIN_P",
        "model_path": "LLAMA_ARG_MODEL",
        "mmproj": "LLAMA_ARG_MMPROJ",
        "draft": "LLAMA_ARG_SPEC_DRAFT_MODEL",
        "device": "LLAMA_ARG_DEVICE",
        "split_mode": "LLAMA_ARG_SPLIT_MODE",
    }

    result = {}
    for key, value in flat_dict.items():
        if key in _FLAT_MODEL_KEYS:
            structured_key = _FLAT_MODEL_KEYS[key]
        else:
            clean = key.replace("LLAMA_ARG_", "").lstrip("_")
            structured_key = "LLAMA_ARG_" + clean.upper() if not clean.startswith("LLAMA_ARG") else key
        result[structured_key] = value
    return result


def _parse_config_override(raw_value):
    """Parse config_override JSON, handling double-encoding from update path.

    Defensive: returns {} for empty strings, whitespace-only, or any
    non-dict result to prevent "'str' object has no attribute 'items'" errors.
    """
    if isinstance(raw_value, dict):
        return raw_value
    if not isinstance(raw_value, str) or not raw_value.strip():
        return {}
    try:
        first = json.loads(raw_value)
        # If result is still a string, it was double-encoded — parse again
        if isinstance(first, str):
            return json.loads(first)
        # Ensure we always return a dict (not a list, int, etc.)
        if not isinstance(first, dict):
            return {}
        return first
    except (json.JSONDecodeError, TypeError):
        return {}
