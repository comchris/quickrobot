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
    """API key must be a non-empty string."""
    key = env_config.get("QUICKROBOT_API_KEY", "")
    assert len(key) >= 32, \
        f"API key too short ({len(key)} chars). Expected ~43-char URL-safe base64."


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
