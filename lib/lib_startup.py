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

"""quickrobot — Startup initialization module.

Centralizes all application startup logic: config loading, database backup,
seed file import, auto-provisioning of system instances, and playbook registration.

Extracted from quickrobot.py to reduce monolith size and improve modularity.
"""

import hashlib
import json
import os
import sys

from lib.qr_engine_ids import get_port_default


def load_env_strict(env_path=".quickrobot.env"):
    """Load and validate .quickrobot.env with strict required keys.

    Unlike the old load_system_engine_config(), this function:
    - Exits (sys.exit 1) if required keys are missing (not just warns)
    - Validates that system engine hosts are not 0.0.0.0
    - Returns the parsed config dict

    Args:
        env_path: Path to the .quickrobot.env file.

    Returns:
        Dict of environment key-value pairs.

    Raises:
        SystemExit: If .quickrobot.env is missing, or required keys are missing/empty,
                   or a system engine host is 0.0.0.0.
    """
    if not os.path.isfile(env_path):
        print(f"[qr] FATAL: .quickrobot.env not found at '{env_path}'")
        print(f"[qr]   The API server cannot start without system engine config.")
        print(f"[qr]   Copy .quickrobot.env.example to .quickrobot.env and edit.")
        sys.exit(1)

    cfg = {}
    with open(env_path, "r") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()

    # ── Required system engine keys — must be present and non-empty ───
    required_system_keys = {
        "API": ["QUICKROBOT_API_HOST", "QUICKROBOT_API_PORT"],
        "WebUI": ["QUICKROBOT_WEBUI_HOST", "QUICKROBOT_WEBUI_PORT"],
    }

    for eng_name, keys in required_system_keys.items():
        for key in keys:
            val = cfg.get(key)
            if not val or val.strip() == "":
                print(f"[qr] FATAL: {eng_name} engine requires '{key}' in .quickrobot.env")
                print(f"[qr]   Key is missing or empty (line {line_no})")
                sys.exit(1)

    # ── Validate 0.0.0.0 rejection for system engines ─────────────────
    for eng_name, key in [("API", "QUICKROBOT_API_HOST"), ("WebUI", "QUICKROBOT_WEBUI_HOST")]:
        host = cfg.get(key, "")
        if host in ("0.0.0.0", "::", "::0"):
            print(f"[qr] FATAL: {eng_name} bind host '{host}' is not a specific address")
            print(f"[qr]   System engines must bind to a specific IP (e.g., 127.0.0.1)")
            sys.exit(1)

    # ── MCP validation — host must not be 0.0.0.0, but missing is OK ──
    mcp_host = cfg.get("QUICKROBOT_MCP_HOST", "")
    if mcp_host and mcp_host in ("0.0.0.0", "::", "::0"):
        print(f"[qr] WARNING: MCP host '{mcp_host}' is not a specific address — defaulting to 127.0.0.1")
        cfg["QUICKROBOT_MCP_HOST"] = "127.0.0.1"

    return cfg


def load_system_engine_config():
    """Load system engine configuration from .quickrobot.env (strict mode).

    Exits with error if required keys are missing.
    Returns logging config as part of qr_env dict for later consumption
    by quickrobot.py after _CONFIG is defined.

    Returns:
        Tuple of (parsed_config, console_debug_level, ansible_log_level).
        The tuple unpacking lets the caller set _CONFIG keys at the right time.
    """
    from lib import lib_constants as _lc

    qr_env = load_env_strict()
    console_level = qr_env.get("QUICKROBOT_CONSOLE_DEBUG_LEVEL")
    if console_level is not None:
        try:
            _lc.QUICKROBOT_CONSOLE_DEBUG_LEVEL = int(console_level)
        except ValueError:
            pass
    ansible_level = qr_env.get("QUICKROBOT_ANSIBLE_LOG_LEVEL", "errors")
    if ansible_level not in ("errors", "warnings", "all"):
        ansible_level = "errors"
    return (qr_env, _lc.QUICKROBOT_CONSOLE_DEBUG_LEVEL, ansible_level)


def backup_database(db_path, skip_if_init=False):
    """Backup SQLite database using cp -n on process start.

    Keeps last `max_backups` copies. Removes oldest when limit exceeded.

    Args:
        db_path: Path to the SQLite database file.
        skip_if_init: If True, skip backup during --init mode (first backup already done).
    """
    if skip_if_init:
        return

    import shutil
    from datetime import datetime
    from quickrobot import _CONFIG, _project_root

    backup_dir = _CONFIG.get("backup_dir", os.path.join(_project_root, "data", "_backups"))
    max_keep = _CONFIG.get("max_backups", 3)

    try:
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"qr_backup_{ts}.db"
        dst = os.path.join(backup_dir, backup_name)

        if not os.path.exists(dst):
            shutil.copy2(db_path, dst)
            print(f"[qr] Database backed up to {dst}")

        # Cleanup oldest backups beyond max_keep
        backups = sorted([
            f for f in os.listdir(backup_dir)
            if f.startswith("qr_backup_") and f.endswith(".db")
        ])
        while len(backups) > max_keep + 1:
            oldest = backups.pop(0)
            os.remove(os.path.join(backup_dir, oldest))

    except Exception as exc:
        print(f"[qr] WARNING: database backup failed: {exc}")


# Seed file path resolver (cached after first call)
_seed_file_path = None


def resolve_seed_path(project_root=None):
    """Resolve seed file path relative to project root.

    Args:
        project_root: Project root directory. Defaults to quickrobot._project_root.

    Returns:
        str — absolute path to the seed SQL file.
    """
    global _seed_file_path
    if project_root is None:
        from quickrobot import _project_root
        project_root = _project_root
    if _seed_file_path is None:
        _seed_file_path = os.path.join(project_root, "data", "_seed", "seed_v006.sql")
    return _seed_file_path


def import_seed_file(db_path):
    """Import seed SQL into the database.

    Seed file contains INSERT OR REPLACE statements for models, presets,
    engine_types, engine_configs, playbook_registry, and benchmark_prompts.

    In non-init mode (existing DB): skip — seed SQL only applied on fresh DB.
    In --init mode: execute seed SQL. Checksum verified in main() before DB creation.

    The seed file is required for --init mode — if missing, execution continues
    but data will be empty (seed is expected to exist since checksum check passed).

    Args:
        db_path: Path to the SQLite database.
    """
    from quickrobot import _CONFIG
    from db.sqlite import pool as _pool
    seed_path = resolve_seed_path()
    init_mode = _CONFIG.get("init_mode", False)

    # In non-init mode: skip seed SQL import (playbooks registered elsewhere)
    if not init_mode:
        return

    # --- Init mode (fresh DB): execute seed SQL ---
    try:
        with open(seed_path) as f:
            sql = f.read()
    except Exception as exc:
        print(f"[qr] WARNING: failed to read seed file: {exc}")
        return

    # Execute seed SQL (INSERT OR REPLACE — idempotent, overwrites matching IDs)
    try:
        with _pool(db_path) as conn:
            conn.executescript(sql)
        print("[qr] Seed file imported successfully")
    except Exception as exc:
        print(f"[qr] WARNING: seed import failed: {exc}")


def pre_validate_seed_checksum(env_cfg, init_mode=False):
    """Validate seed file integrity BEFORE any filesystem change.

    Called in main() after args parse, before --init DB backup.
    Exits on mismatch — filesystem is guaranteed untouched at call time.

    Args:
        env_cfg: Dict from load_env_config() (required keys already validated).
        init_mode: If True, fail on mismatch via sys.exit(1).
                   If False, warn only (for non-init startup checks).
    """
    seed_path = resolve_seed_path()
    if not os.path.isfile(seed_path):
        print(f"[qr] FATAL: Seed file not found at {seed_path}")
        sys.exit(1)

    with open(seed_path, "rb") as sf:
        actual_checksum = hashlib.sha256(sf.read()).hexdigest()
    expected_checksum = env_cfg.get("QUICKROBOT_SEED_CHECKSUM", "")
    if actual_checksum != expected_checksum:
        print(f"[qr] FATAL: Seed checksum mismatch (init mode)")
        print(f"  expected: {expected_checksum[:32]}...")
        print(f"  actual:   {actual_checksum[:32]}...")
        print("[qr] Check .quickrobot.env QUICKROBOT_SEED_CHECKSUM and seed file integrity.")
        if init_mode:
            sys.exit(1)

    expected_size = int(env_cfg.get("QUICKROBOT_SEED_FILESIZE", "0"))
    actual_size = os.path.getsize(seed_path)
    if actual_size != expected_size:
        print(f"[qr] FATAL: Seed file size mismatch (init mode)")
        print(f"  expected: {expected_size}")
        print(f"  actual:   {actual_size}")
        print("[qr] Check .quickrobot.env QUICKROBOT_SEED_FILESIZE and seed file integrity.")
        if init_mode:
            sys.exit(1)


