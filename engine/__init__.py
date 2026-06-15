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

"""Quickrobot — Engine loader and registry.

Discovers engine implementations from the engine/ subdirectory packages,
loads their BaseEngine subclasses, and exposes them through the ENGINES
list. Each engine package exports a CAPABILITIES dict at module level.
"""

import importlib
import inspect
import os


# Global list of discovered engine classes
ENGINES = []

# Map of engine name -> engine class instance
_ENGINES_MAP = {}

# Predefined fixed IDs for known engine types — prevents ID drift on fresh DB
# Maps both discovered names (filesystem packages) and canonical DB names
# Uses QR_ENGINE_* constants from lib.qr_engine_ids as single source of truth
from lib.qr_engine_ids import (QR_ENGINE_API, QR_ENGINE_WEBUI, QR_ENGINE_MCP,
                               QR_ENGINE_UNIVERSAL, QR_ENGINE_SUBPROCESS,
                               QR_ENGINE_IPERF3, QR_ENGINE_LLAMA_SERVER, QR_ENGINE_LLAMA_RPC)
_ENGINE_ID_MAP = {
    "quickrobot_api": QR_ENGINE_API,
    "quickrobot_webui": QR_ENGINE_WEBUI,
    "quickrobot_mcp": QR_ENGINE_MCP,
    "universal": QR_ENGINE_UNIVERSAL,
    "subprocess": QR_ENGINE_SUBPROCESS,
    "iperf3": QR_ENGINE_IPERF3,
    "llama_server": QR_ENGINE_LLAMA_SERVER,
    "llama_rpc": QR_ENGINE_LLAMA_RPC,
}


def _discover_engine_packages(base_dir):
    """Scan subdirectories for engine packages that inherit BaseEngine.

    Args:
        base_dir: Absolute path to the engine/ directory.

    Returns:
        list of tuples (engine_name, engine_class, capabilities_dict).
    """
    discovered = []
    if not os.path.isdir(base_dir):
        return discovered

    for entry in sorted(os.listdir(base_dir)):
        pkg_path = os.path.join(base_dir, entry)
        if not os.path.isdir(pkg_path):
            continue
        init_path = os.path.join(pkg_path, "__init__.py")
        if not os.path.isfile(init_path):
            continue

        # Import the package module
        module_name = f"engine.{entry}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue

        # Look for BaseEngine subclass in the module
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if name == "BaseEngine":
                continue
            # Check if it inherits from BaseEngine (directly or indirectly)
            for base in obj.__mro__:
                if base.__name__ == "BaseEngine" and base.__module__ == "engine.base":
                    capabilities = getattr(module, "CAPABILITIES", None)
                    if capabilities:
                        discovered.append((entry, obj, capabilities))
                    break

    return discovered


def load_engines():
    """Discover and register all engine packages.

    Populates the ENGINES list and _ENGINES_MAP.

    Returns:
        list of (name, class, capabilities) tuples found.
    """
    global ENGINES
    global _ENGINES_MAP

    # Determine the engine directory path
    engine_dir = os.path.join(os.path.dirname(__file__))
    packages = _discover_engine_packages(engine_dir)

    ENGINES = []
    _ENGINES_MAP = {}

    for name, cls, capabilities in packages:
        instance = cls()
        # Use CAPABILITIES["name"] as map key if available, else directory name
        cap_name = capabilities.get("name", name)
        ENGINES.append((name, cls, capabilities))
        _ENGINES_MAP[cap_name] = instance

    return ENGINES



def _auto_register_engines(db_path):
    """Register in-memory engine types in the DB if not already present.

    Scans the ENGINES list (populated by load_engines) and ensures each
    discovered engine type has a corresponding row in the engine_types
    table. Uses get_engine_type_by_name() for lookups and add_engine_type()
    for creation.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        list of tuples (name, capabilities) that were registered.
    """
    import json as _json

    from db.adapters.engine_types import get_engine_type_by_name as _get_etb, \
        add_engine_type as _ae
    registered = []

    for eng_name, cls, cap in ENGINES:
        existing = _get_etb(db_path, eng_name)
        if existing is None:
            # Also check hyphen variant (e.g., "qr_api" vs "qr-api")
            hyphen_name = eng_name.replace("_", "-")
            if hyphen_name != eng_name:
                existing = _get_etb(db_path, hyphen_name)
        # Also check CAPABITIES["name"] for renamed engines
        if existing is None and cap.get("name"):
            existing = _get_etb(db_path, cap["name"])
        if existing is None:
            # Also check hyphen variant (e.g., "quickrobot-api" vs "quickrobot_api")
            hyphen_name = eng_name.replace("_", "-")
            if hyphen_name != eng_name:
                existing = _get_etb(db_path, hyphen_name)
        if existing is None:
            # Normalize name: use underscores (matching the filesystem package)
            # Override: iperf3 → "iperf;3" for cleaner display
            if eng_name == "iperf3":
                display_name = "iperf;3"
            else:
                display_name = cap.get("display_name", eng_name.replace("_", " ").title())
            module_path = f"engine.{eng_name}"
           # Use predefined ID if available, otherwise let DB auto-assign
            fixed_id = _ENGINE_ID_MAP.get(eng_name)
            try:
                _ae(db_path, name=eng_name, display_name=display_name,
                    module_path=module_path, capabilities=cap, engine_id=fixed_id)
                print(f"Auto-registered engine type: {eng_name}")
                registered.append((eng_name, cap))
            except Exception as exc:
                print(f"Warning: failed to register engine '{eng_name}': {exc}")
        else:
            # Sync display_name for known overrides (e.g., iperf3 → "iperf;3")
            if eng_name == "iperf3" and existing.get("display_name") != "iperf;3":
                try:
                    from db.sqlite import pool as _pool
                    with _pool(db_path) as conn:
                        conn.execute(
                            "UPDATE engine_types SET display_name = ? WHERE id = ?",
                            ("iperf;3", existing["id"])
                        )
                    print(f"Updated iperf3 display_name to 'iperf;3' (was: {existing.get('display_name', 'N/A')})")
                except Exception as sync_exc:
                    print(f"Warning: failed to update iperf3 display_name: {sync_exc}")
            # Sync ID if it drifted from the fixed mapping (e.g., MCP got id=9 on first DB creation)
            expected_id = _ENGINE_ID_MAP.get(eng_name)
            if expected_id is not None and existing["id"] != expected_id:
                try:
                    from db.sqlite import pool as _pool
                    with _pool(db_path) as conn:
                        # Disable FK — UPDATE on PK with FK targets needs it, plus updating multiple FK tables
                        conn.execute("PRAGMA foreign_keys = OFF")
                        # Move the engine_types row to expected id
                        conn.execute(
                            "UPDATE engine_types SET id = ? WHERE id = ?",
                            (expected_id, existing["id"])
                        )
                        # Update all FK references across tables to new id
                        for tbl in ("instances", "engine_presets", "engine_models", "engine_configs", "node_configs"):
                            conn.execute(
                                f"UPDATE {tbl} SET engine_type_id = ? WHERE engine_type_id = ?",
                                (expected_id, existing["id"])
                            )
                    print(f"Synced engine type '{eng_name}' id {existing['id']} -> {expected_id}")
                except Exception as sync_exc:
                    print(f"Warning: failed to sync engine type '{eng_name}' id: {sync_exc}")
    return registered


def get_engine(name):
    """Get a loaded engine instance by name.

    Args:
        name: Engine name string (e.g., 'rpc', 'llama_server').

    Returns:
        Engine instance, or None if not found.
    """
    return _ENGINES_MAP.get(name)


def get_engine_capabilities(name):
    """Get capabilities dict for an engine by name.

    Args:
        name: Engine name string.

    Returns:
        dict with capabilities, or None if not found.
    """
    for eng_name, _, cap in ENGINES:
        if eng_name == name:
            return cap
    return None


# Auto-discover on module import
if not ENGINES:
    load_engines()
