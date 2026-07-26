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

from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST, get_port_default


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
        print(f"[qr] WARNING: MCP host '{mcp_host}' is not a specific address — defaulting to {QR_DEFAULT_LOCALHOST}")
        cfg["QUICKROBOT_MCP_HOST"] = QR_DEFAULT_LOCALHOST

    return cfg


def load_system_engine_config():
    """Load system engine configuration from .quickrobot.env (strict mode).

    Exits with error if required keys are missing.
    Returns logging config as part of qr_env dict for later consumption
    by quickrobot.py after _CONFIG is defined.

    Per-engine log levels (Phase 2 of LOG-CONSOLIDATE):
    - Reads QUICKROBOT_<ENGINE>_LOG_LEVEL env vars (scheduler, mcp, webui, api)
    - Falls back to QUICKROBOT_CONSOLE_DEBUG_LEVEL if per-engine var absent
    - Numeric value >= 10 means DEBUG, < 10 means WARNING (quiet production)

    Returns:
        Tuple of (parsed_config, console_debug_level, ansible_log_level).
        The tuple unpacking lets the caller set _CONFIG keys at the right time.
    """
    from lib import lib_constants as _lc

    qr_env = load_env_strict()

    # Legacy single console level — kept for backward compatibility
    console_level = qr_env.get("QUICKROBOT_CONSOLE_DEBUG_LEVEL")
    if console_level is not None:
        try:
            _lc.QUICKROBOT_CONSOLE_DEBUG_LEVEL = int(console_level)
        except ValueError:
            pass

    ansible_level = qr_env.get("QUICKROBOT_ANSIBLE_LOG_LEVEL", "errors")
    if ansible_level not in ("errors", "warnings", "all"):
        ansible_level = "errors"

    # Per-engine log levels (LOG-CONSOLIDATE Phase 2)
    _engine_names = ("scheduler", "mcp", "webui", "api")
    _per_engine = {}
    for _name in _engine_names:
        _key = f"QUICKROBOT_{_name.upper()}_LOG_LEVEL"
        _val = qr_env.get(_key)
        if _val is not None:
            try:
                _per_engine[_name] = int(_val)
            except (ValueError, TypeError):
                _per_engine[_name] = 0  # WARNING
        elif console_level is not None:
            # Fallback to legacy single console level if per-engine var absent
            _per_engine[_name] = int(console_level)

    return (qr_env, _lc.QUICKROBOT_CONSOLE_DEBUG_LEVEL, ansible_level, _per_engine)


def backup_database(db_path):
    """Backup SQLite database using cp -n on process start.

    Keeps last `max_backups` copies. Removes oldest when limit exceeded.

    Args:
        db_path: Path to the SQLite database file.
    """

    import shutil
    from datetime import datetime
    from qr_api import _CONFIG, _project_root

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
        from qr_api import _project_root
        project_root = _project_root
    if _seed_file_path is None:
        _seed_file_path = os.path.join(project_root, "data", "_seed", "seed_v011.sql")
    return _seed_file_path


def import_seed_file(db_path):
    """Import seed SQL into the database.

    Seed file contains INSERT OR REPLACE statements for models, presets,
    engine_types, playbook_registry, and benchmark_prompts.
    engine_configs moved to 010_base.sql (v0.10 split).

    Only runs on fresh DB creation (gated by _db_was_created flag).
    Never re-imports on existing DB startup — seed is one-time only.

    Args:
        db_path: Path to the SQLite database.
    """
    from qr_api import _CONFIG
    from db.sqlite import pool as _pool
    seed_path = resolve_seed_path()

    # Only seed on fresh DB creation, never on existing DB startup
    if not _CONFIG.get("_db_was_created", False):
        return

    # Checksum already validated in phase3_db_handling before DB creation.
    # This function is idempotent — re-validation here is a safety net.
    env_cfg = _CONFIG.get("qr_env", {})
    pre_validate_seed_checksum(env_cfg)

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
            # Seed has NULL for node 1 ansible_user — set to current OS user at runtime.
            from lib.lib_constants import DEFAULT_ANSIBLE_USER
            conn.execute(
                "UPDATE nodes SET ansible_user=? WHERE id=1",
                (DEFAULT_ANSIBLE_USER,),
            )
            conn.commit()
            print("[qr] Seed file imported successfully, node 1 ansible_user=%s", DEFAULT_ANSIBLE_USER)
    except Exception as exc:
        print(f"[qr] WARNING: seed import failed: {exc}")

def pre_validate_seed_checksum(env_cfg):
    """Validate seed file integrity BEFORE any filesystem change.

    Called during fresh DB creation — exits on mismatch since filesystem
    is guaranteed untouched at call time.

    Args:
        env_cfg: Dict from load_env_config() (required keys already validated).
    """
    seed_path = resolve_seed_path()
    if not os.path.isfile(seed_path):
        print(f"[qr] FATAL: Seed file not found at {seed_path}")
        sys.exit(1)

    with open(seed_path, "rb") as sf:
        actual_checksum = hashlib.sha256(sf.read()).hexdigest()
    expected_checksum = env_cfg.get("QUICKROBOT_SEED_CHECKSUM", "")
    if actual_checksum != expected_checksum:
        print(f"[qr] FATAL: Seed checksum mismatch")
        print(f"  expected: {expected_checksum[:32]}...")
        print(f"  actual:   {actual_checksum[:32]}...")
        print("[qr] Check .quickrobot.env QUICKROBOT_SEED_CHECKSUM and seed file integrity.")
        sys.exit(1)

    expected_size = int(env_cfg.get("QUICKROBOT_SEED_FILESIZE", "0"))
    actual_size = os.path.getsize(seed_path)
    if actual_size != expected_size:
        print(f"[qr] FATAL: Seed file size mismatch")
        print(f"  expected: {expected_size}")
        print(f"  actual:   {actual_size}")
        print("[qr] Check .quickrobot.env QUICKROBOT_SEED_FILESIZE and seed file integrity.")
        sys.exit(1)


def ensure_db_and_env(project_root=None):
    """Pre-flight check for DB file + .env file existence.

    Handles 4 scenarios:
      A) Both exist → return (proceed normally)
      B) .env missing, DB exists → FATAL: copy sample then restart
      C) DB missing, .env exists → return (phase3_db_handling() creates fresh DB)
      D) Both missing + sample exists → copy sample, generate secrets, EXIT 1

    Args:
        project_root: Project root directory. Defaults to current working dir.

    Returns:
        None — exits via sys.exit(1) in scenarios B, D. Scenario C returns.
    """
    import secrets as _secrets

    if project_root is None:
        try:
            from qr_api import _project_root
            project_root = _project_root
        except ImportError:
            project_root = os.getcwd()

    db_path = os.path.join(project_root, "data", "quickrobot.db")
    env_path = os.path.join(project_root, ".quickrobot.env")
    sample_path = os.path.join(project_root, ".quickrobot.env.sample")

    db_exists = os.path.isfile(db_path)
    env_exists = os.path.isfile(env_path)
    sample_exists = os.path.isfile(sample_path)

    if db_exists and env_exists:
        return  # both present, proceed

    if not env_exists and db_exists and sample_exists:
        print("[qr] FATAL: .quickrobot.env missing but DB exists.")
        print(f"[qr]   Copy the sample: cp {sample_path} {env_path}")
        print("[qr]   Then restart quickrobot to load config.")
        sys.exit(1)

    if env_exists and not db_exists:
        print("[qr] .quickrobot.env exists — will create fresh DB on next phase.")
        return  # Let phase3_db_handling() create the DB naturally

    # Both missing — attempt auto-provision from sample
    if not env_exists and not db_exists and sample_exists:
        # Copy sample → .env
        import shutil as _shutil
        _shutil.copy2(sample_path, env_path)

        # Generate random secrets for WebUI password and API key
        new_password = _secrets.token_urlsafe(24)
        new_api_key = _secrets.token_urlsafe(32)

        # Replace CHANGE_ME placeholders in the new .env file
        with open(env_path, "r") as f:
            content = f.read()
        content = content.replace("QUICKROBOT_WEBUI_PASSWORD=CHANGE_ME",
                                  f"QUICKROBOT_WEBUI_PASSWORD={new_password}")
        content = content.replace("QUICKROBOT_API_KEY=CHANGE_ME",
                                  f"QUICKROBOT_API_KEY={new_api_key}")
        with open(env_path, "w") as f:
            f.write(content)

        print("=" * 60)
        print("[qr] Fresh setup detected — generated new .quickrobot.env")
        print("=" * 60)
        print(f"[qr] WebUI password: {new_password}")
        print(f"[qr] API key:        {new_api_key}")
        print(f"[qr] File created:   {env_path}")
        print("[qr] Restart quickrobot to create fresh DB with seed data.")
        print("=" * 60)
        sys.exit(1)

    # Fallback: nothing found
    print("[qr] FATAL: No DB, no .env, no sample file found.")
    if not sample_exists:
        print(f"[qr]   Expected sample at: {sample_path}")
    sys.exit(1)


def load_ssl_context(cert_key_map):
    """Load ssl.SSLContext from cert/key pairs in env config dict.

    Checks each (cert, key) pair from cert_key_map. If both files exist and
    are valid, loads the first successful context and returns it.

    Args:
        cert_key_map: dict of {engine_name: {"cert": path, "key": path}}

    Returns:
        ssl.SSLContext if any pair is valid, or None.
    """
    import ssl as _ssl

    for engine, paths in cert_key_map.items():
        cert = paths.get("cert", "")
        key = paths.get("key", "")
        if not cert or not key:
            continue
        if os.path.isfile(cert) and os.path.isfile(key):
            ctx = _ssl.create_default_context(_ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(cert, key)
            return ctx
    return None


def check_ssl_engine(cert_path, key_path, engine_name):
    """Validate SSL cert/key pair for a single engine.

    Returns (ssl_context_or_None, log_message).
    - Empty paths → returns (None, "HTTP")
    - Missing file → logs warning, returns (None, "HTTP (cert/key missing)")
    - Wrong file type → logs error, returns (None, "HTTP (bad file)")
    - Both valid files → loads context, returns (ctx, "HTTPS")

    Args:
        cert_path: Path to SSL certificate file
        key_path: Path to SSL private key file
        engine_name: Human-readable engine name for logging

    Returns:
        tuple: (ssl.SSLContext or None, log_message string)
    """
    import ssl as _ssl

    if not cert_path and not key_path:
        return None, "HTTP"
    if not cert_path or not key_path:
        missing = "cert" if not cert_path else "key"
        print(f"[qr] WARNING: {engine_name} SSL — {missing} path empty, using HTTP")
        return None, f"HTTP (no {missing})"
    if not os.path.isfile(cert_path):
        print(f"[qr] WARNING: {engine_name} SSL — cert not a file ({cert_path}), using HTTP")
        return None, f"HTTP (cert missing)"
    if not os.path.isfile(key_path):
        print(f"[qr] WARNING: {engine_name} SSL — key not a file ({key_path}), using HTTP")
        return None, f"HTTP (key missing)"
    try:
        ctx = _ssl.create_default_context(_ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert_path, key_path)
        return ctx, "HTTPS"
    except Exception as exc:
        print(f"[qr] WARNING: {engine_name} SSL — load failed ({exc}), using HTTP")
        return None, f"HTTP (load error: {exc})"


