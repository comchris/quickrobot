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

"""Runtime engine type registry — loaded from DB at startup.

Engine types are SEED INTO THE DB during --init. This module queries the DB
once at startup and caches results in memory. All subsequent lookups use
the in-memory dicts — no more hardcoded name lists.

Naming: The DB stores engine names in HYPHEN format (e.g. "quickrobot-api").
Both hyphen and underscore variants are indexed for lookup compatibility.
"""

_SYSTEM_ENGINES = {}   # id < 10 → {"id": N, "name": "...", "display_name": "..."}
_USER_ENGINES = {}     # id >= 100 → same structure
_LOADED = False


def load_engine_registry(db_path):
    """Populate _SYSTEM_ENGINES and _USER_ENGINES from the engine_types table.

    Must be called after migrations + seed have run (Phase 5 of startup).
    Indexes both hyphen (quickrobot-api) and underscore (quickrobot_api) variants.

    Args:
        db_path: Path to the SQLite database file.
    """
    global _SYSTEM_ENGINES, _USER_ENGINES, _LOADED
    _SYSTEM_ENGINES = {}
    _USER_ENGINES = {}

    try:
        from db.sqlite import pool as _pool
        with _pool(db_path) as conn:
            for row in conn.execute(
                "SELECT id, name, display_name FROM engine_types ORDER BY id"
            ).fetchall():
                eid, name, display_name = row["id"], row["name"], row["display_name"]
                target = _SYSTEM_ENGINES if eid < 10 else _USER_ENGINES
                target[name] = {"id": eid, "name": name, "display_name": display_name}
                # Also index underscore variant for backward compat
                underscore_name = name.replace("-", "_")
                if underscore_name != name:
                    target[underscore_name] = target[name]

        _LOADED = True
    except Exception as exc:
        _LOADED = False
        print(f"[qr] WARNING: engine registry load failed: {exc}")


def get_engine_by_name(name):
    """Look up engine info by name. Returns dict or None.

    Accepts both hyphen (quickrobot-api) and underscore (quickrobot_api) names.

    Args:
        name: Engine type name string.

    Returns:
        Dict with keys: id, name, display_name — or None if not found.
    """
    if not _LOADED:
        return None
    result = _SYSTEM_ENGINES.get(name) or _USER_ENGINES.get(name)
    if result:
        return dict(result)  # return a copy
    # Try underscore variant if hyphen given, and vice versa
    alt = name.replace("-", "_")
    if alt != name:
        result = _SYSTEM_ENGINES.get(alt) or _USER_ENGINES.get(alt)
        if result:
            return dict(result)
    return None


def is_system_engine(name):
    """Check if an engine type is system-managed (ID < 10).

    Args:
        name: Engine type name string.

    Returns:
        True if the engine is system-managed, False otherwise, or None if not found.
    """
    info = get_engine_by_name(name)
    if info is None:
        return None
    return info["id"] < 10


def load_and_verify_registry(db_path):
    """Load engine registry and verify it populated correctly.

    Call this during WebUI startup to ensure is_system_engine() works.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        True if loaded successfully, False otherwise.
    """
    try:
        load_engine_registry(db_path)
        return _LOADED
    except Exception as exc:
        print(f"[qr-webui] WARNING: engine registry load failed: {exc}")
        return False


def get_engine_id(name):
    """Get the integer ID for an engine type name.

    Args:
        name: Engine type name string.

    Returns:
        Integer ID or None if not found.
    """
    info = get_engine_by_name(name)
    if info is None:
        return None
    return info["id"]


def get_display_name(name):
    """Get the display name for an engine type.

    Falls back to formatted version of the raw name if not found in registry.

    Args:
        name: Engine type name string.

    Returns:
        Display name string, or formatted raw name as fallback.
    """
    info = get_engine_by_name(name)
    if info:
        return info["display_name"]
    # Fallback: format raw name
    return name.replace("_", " ").replace("-", " ").title()


def get_all_engines():
    """Return all registered engines as a list of dicts.

    System engines (id < 10) come first, then user engines (id >= 100).

    Returns:
        List of dicts with keys: id, name, display_name.
    """
    result = []
    for eng in sorted(_SYSTEM_ENGINES.values(), key=lambda e: e["id"]):
        result.append(dict(eng))
    for eng in sorted(_USER_ENGINES.values(), key=lambda e: e["id"]):
        result.append(dict(eng))
    return result


def is_loaded():
    """Check if the registry has been loaded from the DB."""
    return _LOADED
