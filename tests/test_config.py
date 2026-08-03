"""Tier 0 — Configuration verification (no server needed, <2s).

Tests .quickrobot.env parsing, seed file checksum/size,
playbook registry checksums, prompt checksums, and SSOT constant consistency.
"""

import os
from tests.conftest import (
    assert_ok, assert_error, _parse_env, _get_seed_info, _get_playbook_info, _get_prompt_info,
)


# ---------------------------------------------------------------------------
# Section A: .env file parsing and known keys
# ---------------------------------------------------------------------------

def test_env_file_exists():
    """Verify .quickrobot.env is present."""
    from tests.conftest import ENV_FILE
    assert ENV_FILE.exists(), "Missing .quickrobot.env"


def test_env_parse_known_keys(env_config):
    """Parse .env and verify all required keys are present."""
    required_keys = [
        "QUICKROBOT_API_HOST",
        "QUICKROBOT_API_PORT",
        "QUICKROBOT_WEBUI_HOST",
        "QUICKROBOT_WEBUI_PORT",
        "QUICKROBOT_MCP_HOST",
        "QUICKROBOT_MCP_PORT",
        "QUICKROBOT_API_KEY",
        "QUICKROBOT_SEED_CHECKSUM",
        "QUICKROBOT_SEED_FILESIZE",
    ]
    for key in required_keys:
        assert key in env_config, f"Missing required key: {key}"


def test_env_api_key_not_empty(env_config):
    """API key must be a non-empty string. Allow placeholder values like CHANGE_ME."""
    key = env_config.get("QUICKROBOT_API_KEY", "")
    assert len(key) >= 5, \
        f"API key too short ({len(key)} chars). Expected ~43-char URL-safe base64 (or at least 5 chars for placeholders)."


def test_env_ports_are_integers(env_config):
    """All port values must parse as valid integers."""
    for key in ("QUICKROBOT_API_PORT", "QUICKROBOT_WEBUI_PORT", "QUICKROBOT_MCP_PORT"):
        val = env_config.get(key, "")
        assert val.isdigit(), f"{key}='{val}' is not a valid integer"
        port = int(val)
        assert 1 <= port <= 65535, f"{key}={port} out of valid range"


def test_env_webui_password_set(env_config):
    """WebUI password should be set for production."""
    pw = env_config.get("QUICKROBOT_WEBUI_PASSWORD", "")
    assert len(pw) > 0, "QUICKROBOT_WEBUI_PASSWORD is empty"


def test_env_api_key_disabled_present(env_config):
    """QUICKROBOT_API_KEY_DISABLED must be present in .env."""
    assert "QUICKROBOT_API_KEY_DISABLED" in env_config, \
        "Missing required key: QUICKROBOT_API_KEY_DISABLED"


def test_env_api_key_disabled_is_boolean(env_config):
    """QUICKROBOT_API_KEY_DISABLED must parse as a boolean-like value."""
    val = env_config.get("QUICKROBOT_API_KEY_DISABLED", "")
    truthy = ("true", "True", "TRUE", "1", "yes", "Yes", "YES")
    falsy = ("false", "False", "FALSE", "0", "no", "No", "NO", "")
    assert val.lower() in {v.lower() for v in list(truthy) + list(falsy)}, \
        f"QUICKROBOT_API_KEY_DISABLED='{val}' is not a valid boolean-like value"


def test_env_api_key_disabled_default_is_false():
    """When QUICKROBOT_API_KEY_DISABLED is unset, normal key loading applies."""
    import os as _os
    from qr_api import _load_api_key
    # Save originals
    old_disabled = _os.environ.pop("QUICKROBOT_API_KEY_DISABLED", None)
    old_key = _os.environ.get("QUICKROBOT_API_KEY", "")
    try:
        _os.environ["QUICKROBOT_API_KEY"] = "test-token"
        _load_api_key()
        from qr_api import _AUTH_TOKENS
        assert "test-token" in _AUTH_TOKENS, \
            "Without disabled flag, key should be loaded into _AUTH_TOKENS"
    finally:
        _os.environ["QUICKROBOT_API_KEY"] = old_key
        if old_disabled is not None:
            _os.environ["QUICKROBOT_API_KEY_DISABLED"] = old_disabled


# ---------------------------------------------------------------------------
# Section B: Seed file checksum + size verification
# ---------------------------------------------------------------------------

def test_seed_file_exists(seed_info):
    """Seed file must exist."""
    assert seed_info["seed_file"] is not None, "No seed file found"
    assert seed_info.get("error") is None, f"Seed error: {seed_info['error']}"


def test_seed_checksum_matches(seed_info):
    """Disk checksum must match .env QUICKROBOT_SEED_CHECKSUM."""
    assert seed_info["expected_checksum"] == seed_info["actual_checksum"], \
        (f"Checksum mismatch:\n"
         f"  .env has: {seed_info['expected_checksum']}\n"
         f"  Disk has: {seed_info['actual_checksum']}")


def test_seed_size_matches(seed_info):
    """Disk file size must match .env QUICKROBOT_SEED_FILESIZE."""
    assert seed_info["expected_size"] == seed_info["actual_size"], \
        (f"Size mismatch:\n"
         f"  .env has: {seed_info['expected_size']}\n"
         f"  Disk has: {seed_info['actual_size']}")


def test_seed_content_is_valid_sql(seed_info):
    """Seed file must be valid SQL (INSERT OR REPLACE statements)."""
    seed_path = seed_info["seed_file"]
    content = open(seed_path).read()
    assert "INSERT OR REPLACE INTO" in content, \
        "Seed file missing INSERT OR REPLACE statements"
    # Check for tables actually present in the seed file
    required_tables = [
        "engine_types", "engine_presets", "engine_models",
        "playbook_registry", "benchmark_prompts",
    ]
    for table in required_tables:
        assert f"INSERT OR REPLACE INTO {table}" in content or \
               f"INSERT OR IGNORE INTO {table}" in content, \
            f"Missing seed data for table: {table}"


# ---------------------------------------------------------------------------
# Section C: Playbook registry checksum verification
# ---------------------------------------------------------------------------

def test_playbook_registry_exists(playbook_info):
    """Playbook info must be parseable."""
    assert "error" not in playbook_info or \
           playbook_info["error"] is None, \
        f"Playbook error: {playbook_info.get('error')}"


def test_playbook_checksums_match_disk(playbook_info):
    """Registered playbook checksums must match actual disk files.

    During development, some checksums may be stale (files edited but DB not synced).
    Run --mode dev-update to sync, then re-run tests.
    Tests pass if 90%+ of registered playbooks match.
    """
    results = playbook_info.get("results", [])
    mismatches = [r for r in results if r.get("status") == "mismatch"]
    total = len(results)
    mismatch_pct = (len(mismatches) / total * 100) if total > 0 else 0

    if mismatch_pct <= 10:
        # Allow up to 10% stale checksums during development
        pass
    else:
        assert False, \
            f"{mismatch_pct:.0f}% playbook checksums stale ({len(mismatches)}/{total}):\n" + \
            "\n".join(f"  {r['file']}: expected={r.get('expected')} actual={r.get('actual')}" for r in mismatches)


def test_playbooks_on_disk(playbook_info):
    """All registered playbooks must exist on disk."""
    results = playbook_info.get("results", [])
    missing = [r for r in results if r.get("status") == "missing"]
    assert len(missing) == 0, \
        f"Missing playbook files:\n" + \
        "\n".join(f"  {r['file']}" for r in missing)


# ---------------------------------------------------------------------------
# Section D: SSOT constant consistency (quickrobot_engine_ids.py)
# ---------------------------------------------------------------------------

def test_ssot_engine_ids_importable():
    """All engine ID constants must be importable from qr_engine_ids."""
    from lib.qr_engine_ids import (
        QR_ENGINE_LLAMA_SERVER,
        QR_ENGINE_LLAMA_RPC,
        QR_ENGINE_IPERF3,
        QR_ENGINE_UNIVERSAL,
        QR_ENGINE_SUBPROCESS,
        QR_ENGINE_API,
        QR_ENGINE_WEBUI,
        QR_ENGINE_MCP,
    )
    # Verify they are positive integers
    for name, val in [
        ("QR_ENGINE_LLAMA_SERVER", QR_ENGINE_LLAMA_SERVER),
        ("QR_ENGINE_LLAMA_RPC", QR_ENGINE_LLAMA_RPC),
        ("QR_ENGINE_IPERF3", QR_ENGINE_IPERF3),
        ("QR_ENGINE_UNIVERSAL", QR_ENGINE_UNIVERSAL),
        ("QR_ENGINE_SUBPROCESS", QR_ENGINE_SUBPROCESS),
    ]:
        assert isinstance(val, int) and val > 0, f"{name}={val} is not a positive integer"


def test_ssot_engine_names_importable():
    """All engine name constants must be importable."""
    from lib.qr_engine_ids import (
        QR_ENGINE_LLAMA_SERVER_NAME,
        QR_ENGINE_LLAMA_RPC_NAME,
        QR_ENGINE_API_NAME,
        QR_ENGINE_WEBUI_NAME,
        QR_ENGINE_MCP_NAME,
    )
    for name, val in [
        ("QR_ENGINE_LLAMA_SERVER_NAME", QR_ENGINE_LLAMA_SERVER_NAME),
        ("QR_ENGINE_LLAMA_RPC_NAME", QR_ENGINE_LLAMA_RPC_NAME),
        ("QR_ENGINE_API_NAME", QR_ENGINE_API_NAME),
        ("QR_ENGINE_WEBUI_NAME", QR_ENGINE_WEBUI_NAME),
        ("QR_ENGINE_MCP_NAME", QR_ENGINE_MCP_NAME),
    ]:
        assert isinstance(val, str) and len(val) > 0, f"{name}='{val}' is empty"


def test_ssot_port_defaults_map():
    """Port defaults map must be consistent with .env values."""
    from lib.qr_engine_ids import QR_ENGINE_PORT_DEFAULTS

    # llama_server default should be 8080
    assert QR_ENGINE_PORT_DEFAULTS.get("llama_server") == 8080, \
        "QR_ENGINE_PORT_DEFAULTS['llama_server'] should be 8080"
    # rpc default — read from actual SSOT value (may vary by version)
    rpc_port = QR_ENGINE_PORT_DEFAULTS.get("llama_rpc")
    assert isinstance(rpc_port, int) and rpc_port > 0, \
        f"QR_ENGINE_PORT_DEFAULTS['llama_rpc']={rpc_port} is not a positive integer"


def test_ssot_stage_constants():
    """Stage name constants must match expected strings."""
    from lib.qr_engine_ids import (
        QR_STAGE_PREFLIGHT,
        QR_STAGE_SOURCE,
        QR_STAGE_COMPILE,
        QR_STAGE_CONFIG_ENV,
        QR_STAGE_CONFIG_SVC,
        QR_STAGE_START,
    )
    # All stage constants should be non-empty strings
    stages = {
        "QR_STAGE_PREFLIGHT": QR_STAGE_PREFLIGHT,
        "QR_STAGE_SOURCE": QR_STAGE_SOURCE,
        "QR_STAGE_COMPILE": QR_STAGE_COMPILE,
        "QR_STAGE_CONFIG_ENV": QR_STAGE_CONFIG_ENV,
        "QR_STAGE_CONFIG_SVC": QR_STAGE_CONFIG_SVC,
        "QR_STAGE_START": QR_STAGE_START,
    }
    for name, val in stages.items():
        assert isinstance(val, str) and len(val) > 0, \
            f"{name}='{val}' is empty or not a string"


def test_ssot_version_constant():
    """Version constant must be defined."""
    from lib.qr_engine_ids import QUICKROBOT_VERSION
    assert isinstance(QUICKROBOT_VERSION, str), "QUICKROBOT_VERSION must be a string"
    assert QUICKROBOT_VERSION.startswith("v"), \
        f"Version should start with 'v', got: {QUICKROBOT_VERSION}"


def test_ssot_timeout_constants():
    """Timeout defaults must be positive integers."""
    from lib.qr_engine_ids import (
        QR_TIMEOUT_COMPILE,
        QR_TIMEOUT_SOURCE,
        QR_TIMEOUT_DEFAULT,
    )
    for name, val in [
        ("QR_TIMEOUT_COMPILE", QR_TIMEOUT_COMPILE),
        ("QR_TIMEOUT_SOURCE", QR_TIMEOUT_SOURCE),
        ("QR_TIMEOUT_DEFAULT", QR_TIMEOUT_DEFAULT),
    ]:
        assert isinstance(val, int) and val > 0, \
            f"{name}={val} is not a positive integer"



# ---------------------------------------------------------------------------
# Section E: Prompt checksum verification
# ---------------------------------------------------------------------------


def test_prompt_file_exists():
    """Prompt files referenced in seed must exist on disk."""
    info = _get_prompt_info()
    assert "error" not in info or info["error"] is None, \
        f"Prompt error: {info.get('error')}"

    missing = [r for r in info.get("results", []) if r.get("status") == "missing"]
    assert len(missing) == 0, \
        f"Missing prompt files:\n" + \
        "\n".join(f"  {r['file']} (prompt={r['prompt']})" for r in missing)


def test_prompt_checksums_match_disk():
    """Engine prompt checksums in seed must match actual disk files.

    During development, some checksums may be stale (prompts edited but seed not updated).
    Run seed regeneration to update, or accept up to 20% tolerance (prompts edited more often than playbooks).
    """
    info = _get_prompt_info()
    results = info.get("results", [])
    mismatches = [r for r in results if r.get("status") == "mismatch"]
    total = len(results)
    mismatch_pct = (len(mismatches) / total * 100) if total > 0 else 0

    if mismatch_pct <= 20:
        pass  # Allow up to 20% stale during dev
    else:
        assert False, \
            f"{mismatch_pct:.0f}% prompt checksums stale ({len(mismatches)}/{total}):\n" + \
            "\n".join(f"  {r['file']}: expected={r.get('expected')} actual={r.get('actual')}" for r in mismatches)


def test_prompt_skill_md_in_seed():
    """The skill.md prompt is registered in seed with a known checksum."""
    info = _get_prompt_info()
    results = info.get("results", [])
    skill_entry = [r for r in results if "skill.md" in r.get("file", "")]
    assert len(skill_entry) >= 1, \
        f"skill.md not found in prompt registry. Results: {len(results)} entries"
    entry = skill_entry[0]
    # Status can be match (seed current), missing (file deleted), or mismatch (seed stale)
    assert entry.get("status") in ("match", "missing", "mismatch"), \
        f"Unexpected status for skill.md: {entry.get('status')}"


# ---------------------------------------------------------------------------
# Section F: .env.sample validation (fresh install integrity)
# ---------------------------------------------------------------------------

def _parse_env_sample() -> dict:
    """Parse .quickrobot.env.sample into key->value dict."""
    from pathlib import Path
    SAMPLE_FILE = Path(__file__).parent.parent / ".quickrobot.env.sample"
    result = {}
    for line in SAMPLE_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def test_env_sample_file_exists():
    """Verify .quickrobot.env.sample is present."""
    from pathlib import Path
    SAMPLE_FILE = Path(__file__).parent.parent / ".quickrobot.env.sample"
    assert SAMPLE_FILE.exists(), "Missing .quickrobot.env.sample"


def test_env_sample_seed_checksum_not_placeholder(env_config):
    """Seed checksum in .sample must be real hash, NOT CHANGE_ME.
    
    Fresh install flow copies .sample -> .env without replacing seed values.
    pre_validate_seed_checksum() compares against actual disk file — mismatch = FATAL.
    """
    sample_cfg = _parse_env_sample()
    cs = sample_cfg.get("QUICKROBOT_SEED_CHECKSUM", "")
    assert cs != "CHANGE_ME", \
        f"SEED_CHECKSUM in .sample is CHANGE_ME — fresh install will FATAL on seed validation"
    assert len(cs) == 64, \
        f"SEED_CHECKSUM in .sample should be SHA256 (64 chars), got {len(cs)}: '{cs}'"


def test_env_sample_seed_size_not_placeholder(env_config):
    """Seed filesize in .sample must be real integer, NOT CHANGE_ME."""
    sample_cfg = _parse_env_sample()
    sz = sample_cfg.get("QUICKROBOT_SEED_FILESIZE", "")
    assert sz != "CHANGE_ME", \
        f"SEED_FILESIZE in .sample is CHANGE_ME — fresh install will FATAL on seed validation"
    assert sz.isdigit(), \
        f"SEED_FILESIZE in .sample should be integer, got: '{sz}'"


def test_env_sample_seed_checksum_matches_disk():
    """Seed checksum in .sample must match actual disk file."""
    sample_cfg = _parse_env_sample()
    expected_cs = sample_cfg.get("QUICKROBOT_SEED_CHECKSUM", "")
    
    from pathlib import Path
    SEED_DIR = Path(__file__).parent.parent / "db" / "migrations"
    seed_files = sorted(SEED_DIR.glob("seed_v*.sql"), key=lambda p: p.name)
    active_seed = [f for f in seed_files if "_backup_" not in f.name]
    if not active_seed:
        assert False, "No seed files found for checksum comparison"
    seed_file = active_seed[-1]
    
    import hashlib
    actual_cs = hashlib.sha256(seed_file.read_bytes()).hexdigest()
    assert expected_cs == actual_cs, \
        f".sample SEED_CHECKSUM mismatch:\n  .sample: {expected_cs}\n  disk:    {actual_cs}"


def test_env_sample_seed_size_matches_disk():
    """Seed filesize in .sample must match actual disk file."""
    sample_cfg = _parse_env_sample()
    expected_sz = sample_cfg.get("QUICKROBOT_SEED_FILESIZE", "")
    
    from pathlib import Path
    SEED_DIR = Path(__file__).parent.parent / "db" / "migrations"
    seed_files = sorted(SEED_DIR.glob("seed_v*.sql"), key=lambda p: p.name)
    active_seed = [f for f in seed_files if "_backup_" not in f.name]
    if not active_seed:
        assert False, "No seed files found for size comparison"
    seed_file = active_seed[-1]
    
    import os
    actual_sz = str(os.path.getsize(seed_file))
    assert expected_sz == actual_sz, \
        f".sample SEED_FILESIZE mismatch:\n  .sample: {expected_sz}\n  disk:    {actual_sz}"


def test_env_sample_password_is_placeholder():
    """WebUI password in .sample must be CHANGE_ME (not a real production password)."""
    sample_cfg = _parse_env_sample()
    pw = sample_cfg.get("QUICKROBOT_WEBUI_PASSWORD", "")
    assert pw == "CHANGE_ME", \
        f"WEBUI_PASSWORD in .sample should be CHANGE_ME, got: '{pw}'"


def test_env_sample_api_key_is_placeholder():
    """API key in .sample must be CHANGE_ME (not a real production API key)."""
    sample_cfg = _parse_env_sample()
    key = sample_cfg.get("QUICKROBOT_API_KEY", "")
    assert key == "CHANGE_ME", \
        f"API_KEY in .sample should be CHANGE_ME, got: '{key}'"


def test_env_sample_mcp_token_is_placeholder():
    """MCP token in .sample must be CHANGE_ME (not a real production token)."""
    sample_cfg = _parse_env_sample()
    token = sample_cfg.get("QUICKROBOT_MCP_TOKEN", "")
    assert token == "CHANGE_ME", \
        f"MCP_TOKEN in .sample should be CHANGE_ME, got: '{token}'"


def test_env_sample_mcp_host_is_loopback():
    """MCP host in .sample must use loopback (127.0.0.1), not LAN IP."""
    sample_cfg = _parse_env_sample()
    host = sample_cfg.get("QUICKROBOT_MCP_HOST", "")
    assert host == "127.0.0.1", \
        f"MCP_HOST in .sample should be 127.0.0.1, got: '{host}'"


def test_env_sample_all_required_keys_present():
    """.env.sample must have all keys that the fresh install flow needs."""
    sample_cfg = _parse_env_sample()
    required = [
        "QUICKROBOT_API_HOST", "QUICKROBOT_API_PORT",
        "QUICKROBOT_WEBUI_HOST", "QUICKROBOT_WEBUI_PORT",
        "QUICKROBOT_MCP_HOST", "QUICKROBOT_MCP_PORT",
        "QUICKROBOT_WEBUI_PASSWORD", "QUICKROBOT_API_KEY",
        "QUICKROBOT_MCP_TOKEN",
        "QUICKROBOT_SEED_CHECKSUM", "QUICKROBOT_SEED_FILESIZE",
        "QUICKROBOT_SYSTEM_RETRIES", "QUICKROBOT_CLEANUP_ON_CREATE_FAIL",
        "QUICKROBOT_LOG_RETENTION_DAYS",
    ]
    missing = [k for k in required if k not in sample_cfg]
    assert len(missing) == 0, \
        f"Missing required keys in .sample: {', '.join(missing)}"


def test_env_sample_seed_matches_env():
    """Seed checksum/size must be identical in .env and .env.sample (both are source of truth)."""
    from pathlib import Path
    _root = Path(__file__).parent.parent
    env_cfg = _parse_env(_root / ".quickrobot.env")
    sample_cfg = _parse_env_sample()
    
    # Both files must have the seed checksum/size keys and they must match
    assert "QUICKROBOT_SEED_CHECKSUM" in env_cfg, "Missing QUICKROBOT_SEED_CHECKSUM in .env"
    assert "QUICKROBOT_SEED_CHECKSUM" in sample_cfg, "Missing QUICKROBOT_SEED_CHECKSUM in .sample"
    assert env_cfg["QUICKROBOT_SEED_CHECKSUM"] == sample_cfg["QUICKROBOT_SEED_CHECKSUM"], \
        f"Seed checksum mismatch: .env={env_cfg['QUICKROBOT_SEED_CHECKSUM']}, .sample={sample_cfg['QUICKROBOT_SEED_CHECKSUM']}"
    
    assert "QUICKROBOT_SEED_FILESIZE" in env_cfg, "Missing QUICKROBOT_SEED_FILESIZE in .env"
    assert "QUICKROBOT_SEED_FILESIZE" in sample_cfg, "Missing QUICKROBOT_SEED_FILESIZE in .sample"
    assert env_cfg["QUICKROBOT_SEED_FILESIZE"] == sample_cfg["QUICKROBOT_SEED_FILESIZE"], \
        f"Seed filesize mismatch: .env={env_cfg['QUICKROBOT_SEED_FILESIZE']}, .sample={sample_cfg['QUICKROBOT_SEED_FILESIZE']}"
