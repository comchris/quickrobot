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

"""Quickrobot — Layered configuration merge engine (CONFIG-1 Phase 2).

Implements an extensible ConfigLevel struct with a LayeredMergeChain that
applies deep-merge semantics across ordered layers. Each layer contributes
env_vars (dict), cli_opts (list), and model_params (dict). Higher-level
layers override lower ones per key.

Key classes:
    ConfigLevel — Immutable record representing one merge layer.
    LayeredMergeChain — Ordered list of layers with deep-merge semantics.

Usage:
    chain = LayeredMergeChain()
    chain.append(ConfigLevel(1, "engine_configs", env_vars={...}, cli_opts=[...]))
    chain.append(ConfigLevel(3, "preset_template", env_vars={...}))
    result, source_map = chain.get_merged()

Layer numbering (higher = higher precedence):
    1  engine_defaults     → engine_configs table
    2  model_definition    → engine_models.model_params
    3  preset_template     → engine_presets.config_template
    4  node_defaults       → node_configs table (future)
    5  instance_override   → instances.config_override (FINAL per-instance)
    6  metadata            → restart_policy, start_on_boot injection
    7  cluster_bindings    → llama_server/rpc tensor_split, -dev, --rpc (runtime only)
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConfigLevel — immutable record of one merge layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigLevel:
    """One layer in the configuration merge chain.

    Frozen to prevent accidental mutation during merge. Each level is
    uniquely identified by (level, source); levels with the same number
    from different sources can coexist (used for composite layers).

    Args:
        level: Precedence integer (higher wins). 1=lowest, 7=highest.
        source: Human-readable source name ("engine_configs", etc.).
        env_vars: Dict of environment variable overrides.
        cli_opts: List of CLI argument fragments (will be joined at render).
        model_params: Dict of model-specific parameters.
        metadata: Arbitrary per-layer extras (gpu_device, etc.).
    """

    level: int
    source: str
    env_vars: dict = field(default_factory=dict)
    cli_opts: list = field(default_factory=list)
    model_params: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def merge_into(self, target_env: dict, target_cli: list, target_model: dict) -> None:
        """Deep-merge this level's contents into the target accumulators."""
        if self.env_vars:
            for k, v in self.env_vars.items():
                if v is None or v == "":
                    target_env.pop(k, None)
                elif isinstance(v, dict):
                    existing = target_env.get(k, {})
                    if isinstance(existing, dict):
                        _deep_merge_dicts(existing, v)
                        target_env[k] = existing
                    else:
                        target_env[k] = v
                else:
                    target_env[k] = v

        if self.cli_opts:
            target_cli.extend(self.cli_opts)

        if self.model_params:
            for k, v in self.model_params.items():
                if v is None or v == "":
                    target_model.pop(k, None)
                elif isinstance(v, dict):
                    existing = target_model.get(k, {})
                    if isinstance(existing, dict):
                        _deep_merge_dicts(existing, v)
                        target_model[k] = existing
                    else:
                        target_model[k] = v
                else:
                    target_model[k] = v


def _deep_merge_dicts(base: dict, override: dict) -> None:
    """Mutate base in-place: overlay override keys into base.

    Recursive for nested dicts. Keys with None or "" values are removed
    from base (null sentinel semantics).
    """
    for k, v in override.items():
        if k.startswith("_"):
            continue
        if v is None or v == "":
            base.pop(k, None)
        elif isinstance(v, dict) and k in base and isinstance(base[k], dict):
            _deep_merge_dicts(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# LayeredMergeChain — ordered sequence of ConfigLevel layers
# ---------------------------------------------------------------------------


class LayeredMergeChain:
    """Ordered list of configuration layers with deep-merge semantics.

    Layers are appended in order (lowest precedence first). Calling
    get_merged() produces a final merged result where higher-level
    layers override lower ones per key.

    Args:
        strict_mode: If True, raises ValueError on duplicate levels.
                     If False, duplicates are silently merged together.
    """

    def __init__(self, strict_mode: bool = True):
        self._layers: list[ConfigLevel] = []
        self._strict = strict_mode

    def append(self, layer: ConfigLevel) -> "LayeredMergeChain":
        """Append a layer (lowest precedence first).

        Args:
            layer: The ConfigLevel to add.

        Returns:
            self for method chaining.

        Raises:
            ValueError: If strict_mode and level already exists.
        """
        if self._strict:
            existing_levels = [l.level for l in self._layers]
            if layer.level in existing_levels:
                raise ValueError(
                    f"Duplicate level {layer.level} ({layer.source}) in chain. "
                    f"Existing levels: {existing_levels}"
                )
        self._layers.append(layer)
        return self

    def get_merged(self) -> tuple[dict, dict]:
        """Merge all layers and return (result_dict, source_annotation_map).

        Result dict has keys: env, cli_opts, model.
        Source map records which layer contributed each key:
            {"LLAMA_ARG_HOST": "engine_configs", ...}

        Returns:
            Tuple of (merged_config, source_annotations).
        """
        result_env: dict = {}
        result_cli: list = []
        result_model: dict = {}
        source_map: dict = {}

        for layer in self._layers:
            layer.merge_into(result_env, result_cli, result_model)

            # Track source annotations
            for k in layer.env_vars:
                if layer.env_vars[k] is not None and layer.env_vars[k] != "":
                    if k not in source_map:
                        source_map[k] = layer.source
            for k in layer.model_params:
                if layer.model_params[k] is not None and layer.model_params[k] != "":
                    if k not in source_map:
                        source_map[k] = layer.source

        return {
            "env": result_env,
            "cli_opts": list(result_cli),
            "model": result_model,
        }, source_map

    def to_dict(self) -> dict:
        """Return the merged config without source annotations.

        Convenience wrapper around get_merged() for callers that only
        need the final merged values.
        """
        result, _ = self.get_merged()
        return result

    @property
    def layer_count(self) -> int:
        """Number of layers in this chain."""
        return len(self._layers)

    @property
    def levels(self) -> list[int]:
        """Return the precedence levels in order (ascending)."""
        return [l.level for l in self._layers]

    def clear(self) -> None:
        """Remove all layers from this chain."""
        self._layers.clear()


# ---------------------------------------------------------------------------
# Factory helpers — build common layer configs from DB data
# ---------------------------------------------------------------------------


def make_env_layer(level: int, source: str, env_vars: dict,
                   cli_opts: Optional[list] = None,
                   model_params: Optional[dict] = None,
                   metadata: Optional[dict] = None) -> ConfigLevel:
    """Create a ConfigLevel from raw config dicts.

    Convenience function for callers that have raw data but not
    ConfigLevel objects.

    Args:
        level: Precedence level.
        source: Source identifier string.
        env_vars: Environment variable dict.
        cli_opts: Optional CLI arguments list.
        model_params: Optional model parameters dict.
        metadata: Optional per-layer metadata.

    Returns:
        A frozen ConfigLevel instance.
    """
    return ConfigLevel(
        level=level,
        source=source,
        env_vars=dict(env_vars) if env_vars else {},
        cli_opts=list(cli_opts) if cli_opts else [],
        model_params=dict(model_params) if model_params else {},
        metadata=dict(metadata) if metadata else {},
    )


def build_chain_from_rows(conn, engine_type_id: int, node_id: Optional[int] = None):
    """Build a LayeredMergeChain from database query results.

    Reads engine_configs (and optionally node_configs) rows and creates
    ConfigLevel objects for each source. The caller is responsible for
    adding model, preset, override, and cluster layers.

    Args:
        conn: SQLite connection (must be in a 'with' block).
        engine_type_id: Engine type to look up configs for.
        node_id: Optional node ID for node-level configs.

    Returns:
        LayeredMergeChain with L1 (engine_defaults) and optional L4 (node_defaults).
    """
    chain = LayeredMergeChain()

    # L1: Engine default configs
    ec_rows = conn.execute(
        "SELECT key, value FROM engine_configs WHERE engine_type_id = ?",
        (engine_type_id,),
    ).fetchall()
    layer1_env = {}
    for r in ec_rows:
        layer1_env[r[0]] = r[1]
    if layer1_env:
        chain.append(ConfigLevel(1, "engine_configs", env_vars=layer1_env))

    # L4: Node default configs (future — not currently populated)
    if node_id:
        nc_rows = conn.execute(
            "SELECT key, value FROM node_configs WHERE node_id = ? AND engine_type_id = ?",
            (node_id, engine_type_id),
        ).fetchall()
        layer4_env = {}
        for r in nc_rows:
            layer4_env[r[0]] = r[1]
        if layer4_env:
            chain.append(ConfigLevel(4, "node_configs", env_vars=layer4_env))

    return chain
