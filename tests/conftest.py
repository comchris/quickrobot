"""quickrobot test suite configuration.

Shared fixtures for pytest:
  - env_config:    Parse .quickrobot.env into dict
  - seed_info:     Verify seed file checksum + size match .env values
  - playbook_info: Verify playbook registry checksums vs disk files
  - db_path:       Path to test database (temp copy or fresh)
  - app:           Flask app instance with test config
  - client:        Flask test client (WSGI layer)

Test suite split (185 tests total):
  Part A (core API + config, 99 tests):  pytest tests/ -m part_a   (~1s)
  Part B (integration + UI + infra, 86 tests): pytest tests/ -m part_b  (~12s)
  Auto-applied by conftest.py::pytest_collection_modifyitems() based on filename.
"""

import os
import shutil
import hashlib
import tempfile
import pytest
from pathlib import Path


# Prevent /tmp tmpfs overflow: redirect all test temp files to real disk.
# On systems where /tmp is tmpfs (RAM-backed), tempfile.TemporaryDirectory()
# fills RAM quickly — 64 tests × ~16MB DB ≈ 1GB+. Redirect to persistent dir.
@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    _test_tmp = Path("/CORE/tmp/qr_test")
    _test_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(_test_tmp)


# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".quickrobot.env"

# Source .quickrobot.env so PYTHONPYCACHEPREFIX=/tmp/pycache is set before
# any module imports that might create __pycache__ dirs.
from dotenv import load_dotenv
load_dotenv(str(ENV_FILE))
SEED_DIR = PROJECT_ROOT / "db" / "migrations"
PLAYBOOKS_DIR = PROJECT_ROOT / "playbooks"
DB_DIR = PROJECT_ROOT / "data"
DB_PATH = DB_DIR / "quickrobot.db"

# ---------------------------------------------------------------------------
# env_config — reads from os.environ (populated by load_dotenv above)
# ---------------------------------------------------------------------------

def _parse_env(path: Path) -> dict:
    """Fallback parser for .quickrobot.env into a key->value dict.
    
    Used when os.environ keys are insufficient (e.g., testing env parsing).
    Prefer reading from os.environ directly where dotenv is active.
    """
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# Seed file verification
# ---------------------------------------------------------------------------

def _seed_checksum(path: Path) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: path.read_bytes()[:8192], b""):
        h.update(path.read_bytes())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _get_seed_info(env_cfg: dict) -> dict:
    """Return (expected_checksum, expected_size, actual_checksum, actual_size)."""
    expected_cksum = env_cfg.get("QUICKROBOT_SEED_CHECKSUM", "")
    expected_size = int(env_cfg.get("QUICKROBOT_SEED_FILESIZE", 0))

    seed_files = sorted(SEED_DIR.glob("seed_v*.sql"), key=lambda p: p.name)
    if not seed_files:
        return {
            "expected_checksum": expected_cksum,
            "expected_size": expected_size,
            "actual_checksum": None,
            "actual_size": 0,
            "seed_file": None,
            "error": "No seed files found",
        }

    # Prefer non-backup files: filter out *_backup_* from candidate list
    active_seed = [f for f in seed_files if "_backup_" not in f.name]
    if active_seed:
        seed_file = sorted(active_seed, key=lambda p: p.name)[-1]
    else:
        seed_file = seed_files[-1]
    actual_cksum = hashlib.sha256(seed_file.read_bytes()).hexdigest()
    actual_size = seed_file.stat().st_size
    return {
        "expected_checksum": expected_cksum,
        "expected_size": expected_size,
        "actual_checksum": actual_cksum,
        "actual_size": actual_size,
        "seed_file": str(seed_file),
    }


# ---------------------------------------------------------------------------
# Playbook registry checksum verification
# ---------------------------------------------------------------------------

def _get_playbook_info() -> dict:
    """Verify registered playbook checksums against disk files.

    Uses the seed file's playbook_registry section to build expected checksums,
    then compares against actual disk files.
    """
    seed_files = sorted(SEED_DIR.glob("seed_v*.sql"), key=lambda p: p.name)
    if not seed_files:
        return {"error": "No seed files found"}

    # Prefer non-backup files
    active_seed = [f for f in seed_files if "_backup_" not in f.name]
    seed_file = sorted(active_seed, key=lambda p: p.name)[-1] if active_seed else seed_files[-1]
    seed_content = seed_file.read_text()

    # Parse all INSERT OR REPLACE INTO playbook_registry statements
    import re
    # Match individual INSERT statements — each starts with "INSERT OR REPLACE INTO playbook_registry"
    # and ends with a semicolon. They may be on one line or multiple lines.
    pattern = re.compile(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+playbook_registry\s*\([^)]+\)\s*VALUES\s*\((.*?)\);",
        re.DOTALL,
    )
    
    expected_playbooks = {}
    for m in pattern.finditer(seed_content):
        values_str = m.group(1)
        # Extract all single-quoted strings from the VALUES clause.
        # Column order: id (int), file_path, version, checksum_sha256, file_type, tags, playbook_id, ...
        # Only quoted values are captured, so indices shift past integer columns.
        strings = re.findall(r"'([^']*)'", values_str)
        if len(strings) >= 4:
            filepath = strings[0]   # 2nd column (after int id)
            checksum = strings[2]   # 4th column (after int id + version)
            expected_playbooks[filepath] = checksum

    if not expected_playbooks:
        return {"error": "No playbook_registry entries found in seed"}

    # Compare against disk — seed paths include 'playbooks/' prefix, so use PROJECT_ROOT
    results = []
    for filepath, expected_cs in expected_playbooks.items():
        full_path = PROJECT_ROOT / filepath
        if not full_path.exists():
            results.append({
                "file": filepath,
                "status": "missing",
                "expected": expected_cs[:16] + "...",
            })
        else:
            actual_cs = hashlib.sha256(full_path.read_bytes()).hexdigest()
            match = actual_cs == expected_cs
            results.append({
                "file": filepath,
                "status": "match" if match else "mismatch",
                "expected": expected_cs[:16] + "...",
                "actual": actual_cs[:16] + "...",
                "match": match,
            })

    mismatches = [r for r in results if r.get("status") == "mismatch"]
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatches": len(mismatches),
        "results": results,
    }


def _get_prompt_info() -> dict:
    """Verify engine_prompts checksums from seed file against disk files.

    The seed file stores checksum_sha256 + file_size for each prompt.
    This function parses the seed and compares against actual files.
    """
    seed_files = sorted(SEED_DIR.glob("seed_v*.sql"), key=lambda p: p.name)
    if not seed_files:
        return {"error": "No seed files found"}

    # Prefer non-backup files
    active_seed = [f for f in seed_files if "_backup_" not in f.name]
    seed_file = sorted(active_seed, key=lambda p: p.name)[-1] if active_seed else seed_files[-1]
    seed_content = seed_file.read_text()

    import re
    # Parse INSERT OR REPLACE INTO engine_prompts statements
    pattern = re.compile(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+engine_prompts\s*\([^)]+\)\s*VALUES\s*\((.*?)\);",
        re.DOTALL,
    )

    expected_prompts = {}
    for m in pattern.finditer(seed_content):
        values_str = m.group(1)
        # Extract single-quoted strings from VALUES clause.
        # Column order: id(int), prompt_id, title, description, file_path,
        #               message_role, file_type, prompt_type, file_size(int),
        #               checksum_sha256, version, tags, arguments, ...
        strings = re.findall(r"'([^']*)'", values_str)
        if len(strings) >= 10:
            prompt_id = strings[0]       # prompt_id (1st quoted, after int id)
            filepath = strings[3]        # file_path (4th quoted)
            checksum = strings[7]        # checksum_sha256 (8th quoted)
            expected_prompts[prompt_id] = {"filepath": filepath, "checksum": checksum}

    if not expected_prompts:
        return {"error": "No engine_prompts entries found in seed"}

    # Compare against disk
    results = []
    for prompt_id, info in expected_prompts.items():
        filepath = info["filepath"]
        expected_cs = info["checksum"]
        full_path = PROJECT_ROOT / filepath
        if not full_path.exists():
            results.append({
                "prompt": prompt_id,
                "file": filepath,
                "status": "missing",
                "expected": expected_cs[:16] + "...",
            })
        else:
            actual_cs = hashlib.sha256(full_path.read_bytes()).hexdigest()
            match = actual_cs == expected_cs
            results.append({
                "prompt": prompt_id,
                "file": filepath,
                "status": "match" if match else "mismatch",
                "expected": expected_cs[:16] + "...",
                "actual": actual_cs[:16] + "...",
                "match": match,
            })

    mismatches = [r for r in results if r.get("status") == "mismatch"]
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatches": len(mismatches),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Fixture: env_config
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def env_config():
    """Parse .quickrobot.env into a dict. Available in all tests."""
    assert ENV_FILE.exists(), f"Missing {ENV_FILE}"
    return _parse_env(ENV_FILE)


# ---------------------------------------------------------------------------
# Fixture: seed_info
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def seed_info(env_config):
    """Seed file checksum and size verification data."""
    return _get_seed_info(env_config)


# ---------------------------------------------------------------------------
# Fixture: playbook_info
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def playbook_info():
    """Playbook registry checksum verification data."""
    return _get_playbook_info()


# ---------------------------------------------------------------------------
# Fixture: db_path
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def db_path(env_config):
    """Database path for tests.

    If DB exists, use it (copy to temp dir).
    If DB does not exist, create a fresh temp DB and seed it.
    Uses tempfile.TemporaryDirectory so files are auto-removed on GC,
    even if pytest is interrupted (Ctrl+C).
    """
    import sqlite3

    tmpdir = tempfile.TemporaryDirectory(prefix="qr_test_")
    try:
        # Check if we have an existing DB
        if DB_PATH.exists():
            # Use a copy so tests don't pollute live DB
            tmp_path = os.path.join(tmpdir.name, "quickrobot.db")
            shutil.copy2(str(DB_PATH), tmp_path)
        else:
            # Fresh DB — create temp, apply base schema + seed data
            tmp_path = os.path.join(tmpdir.name, "quickrobot.db")

            seed_files = sorted(SEED_DIR.glob("seed_v*.sql"), key=lambda p: p.name)
            if seed_files:
                active_seed = [f for f in seed_files if "_backup_" not in f.name]
                seed_file = sorted(active_seed, key=lambda p: p.name)[-1] if active_seed else seed_files[-1]
                conn = sqlite3.connect(tmp_path)
                conn.executescript(seed_file.read_text())
                conn.close()

        yield tmp_path
    finally:
        # TemporaryDirectory auto-removes on close — safe even if interrupted
        tmpdir.cleanup()


# Session finalizer: clean up any orphaned qr_test_*.db files in data/
@pytest.fixture(scope="session", autouse=True)
def _clean_test_db_orphans():
    """Remove any qr_test_*.db files left behind from previous interrupted test runs."""
    yield
    for f in DB_DIR.glob("qr_test_*.db"):
        try:
            os.unlink(f)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fixture: app (Flask application)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def app(db_path, env_config):
    """Flask app configured with test database."""
    # Set the API key from .env for auth middleware
    api_key = env_config.get("QUICKROBOT_API_KEY", "test-token")
    os.environ["QUICKROBOT_API_KEY"] = api_key

    # Import app factory and register routes
    sys_path_0 = os.path.dirname(__file__)
    if PROJECT_ROOT not in os.sys.path:
        os.sys.path.insert(0, str(PROJECT_ROOT))

    from qr_api import app as flask_app, register_routes, _CONFIG, _load_api_key
    # Use prod mode with loaded tokens — auth is enforced for all tests
    _CONFIG["pb_mode"] = "prod"
    _load_api_key()
    register_routes(flask_app)
    flask_app.config["TESTING"] = True

    yield flask_app


# ---------------------------------------------------------------------------
# Fixture: client (Flask test client)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def client(app):
    """Flask WSGI test client with automatic API key injection."""
    # Determine API key
    api_key = os.environ.get("QUICKROBOT_API_KEY", "test-token")
    _base_headers = {"X-API-Key": api_key}

    class AuthClient:
        """Test client wrapper that injects X-API-Key header by default."""
        def __init__(self, base_headers):
            self._client = app.test_client()
            self._base_headers = base_headers

        def get(self, *args, headers=None, **kwargs):
            merged = dict(self._base_headers)
            if headers:
                merged.update(headers)
            kwargs["headers"] = merged
            return self._client.get(*args, **kwargs)

        def post(self, *args, headers=None, **kwargs):
            merged = dict(self._base_headers)
            if headers:
                merged.update(headers)
            kwargs["headers"] = merged
            return self._client.post(*args, **kwargs)

        def put(self, *args, headers=None, **kwargs):
            merged = dict(self._base_headers)
            if headers:
                merged.update(headers)
            kwargs["headers"] = merged
            return self._client.put(*args, **kwargs)

        def delete(self, *args, headers=None, **kwargs):
            merged = dict(self._base_headers)
            if headers:
                merged.update(headers)
            kwargs["headers"] = merged
            return self._client.delete(*args, **kwargs)

        def patch(self, *args, headers=None, **kwargs):
            merged = dict(self._base_headers)
            if headers:
                merged.update(headers)
            kwargs["headers"] = merged
            return self._client.patch(*args, **kwargs)

        def raw(self, *args, **kwargs):
            """Bypass header injection — use for auth-fail tests."""
            return self._client.get(*args, **kwargs)

    def _make_client(headers=None):
        merged = dict(_base_headers)
        if headers:
            merged.update(headers)
        return AuthClient(merged)

    # Yield a factory so tests can optionally override auth headers
    yield _make_client


# ---------------------------------------------------------------------------
# Fixture: client_auth (convenience — pre-configured with auth header)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def client_auth(client):
    """Pre-configured test client with API key header."""
    return client


# ---------------------------------------------------------------------------
# Helper: assert_response structure
# ---------------------------------------------------------------------------

def assert_ok(resp, expected_status=200):
    """Assert response is HTTP 200 (or specified status) and has 'status': 'ok'."""
    assert resp.status_code == expected_status, \
        f"Expected {expected_status}, got {resp.status_code}: {resp.data[:500]}"
    data = resp.get_json()
    assert data is not None, "Response body is not valid JSON"
    assert data.get("status") == "ok", \
        f"Expected status='ok', got: {data.get('status')}"


def assert_error(resp, expected_code=None, expected_status=401):
    """Assert response is an error with specified code and status."""
    assert resp.status_code == expected_status, \
        f"Expected {expected_status}, got {resp.status_code}: {resp.data[:500]}"
    data = resp.get_json()
    assert data is not None, "Response body is not valid JSON"
    assert data.get("status") == "error", \
        f"Expected status='error', got: {data.get('status')}"
    if expected_code:
        assert data.get("code") == expected_code, \
            f"Expected code='{expected_code}', got: {data.get('code')}"


# ---------------------------------------------------------------------------
# Helper: check_no_other_quickrobot()
# ---------------------------------------------------------------------------

def check_no_other_quickrobot():
    """Verify no other quickrobot processes are running.

    Returns (is_free, details). If another process is found, tests can skip
    subprocess-related tests or fail early.
    """
    import subprocess
    result = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True
    )
    # Match quickrobot.py or qr_mcp_server.py but not this test runner
    patterns = [
        "quickrobot.py",
        "qr_mcp_server.py",
        "quickrobot_webui.py",
        "quickrobot_scheduler",
    ]
    found = []
    for line in result.stdout.splitlines():
        for pattern in patterns:
            if pattern in line and "tests/" not in line and "grep" not in line:
                # Exclude our own test process
                pid_line = line.split()
                if pid_line and pid_line[1] != str(os.getpid()):
                    found.append((pattern, line.strip()))
                    break
    return len(found) == 0, found


# ---------------------------------------------------------------------------
# Helper: is_fresh_db(db_path)
# ---------------------------------------------------------------------------

def is_fresh_db(path: str) -> bool:
    """Check if a DB has been freshly seeded (no user data).

    A fresh seed DB has no instances, nodes, or logs beyond the seed.
    Used to gate quicksetup testing and certain state-dependent tests.
    """
    import sqlite3
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    # Check if any user-created data exists
    count_instances = cur.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    count_nodes = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    conn.close()
    # If only system instances (4) and localhost node (1) exist, it's fresh
    return count_instances <= 4 and count_nodes <= 1


# ---------------------------------------------------------------------------
# Test group markers — split suite into 2 parts to avoid shell timeout
# Run: pytest -m part_a  (core API + config, ~9 files)
# Run: pytest -m part_b  (integration + UI + infra, ~8 files)
# ---------------------------------------------------------------------------

# Part A: Core API routes, config, auth, models, jobs, presets, nodes, instances
# Fastest group (~1s), no external dependencies beyond DB
PART_A_FILES = [
    "test_config.py",
    "test_api_auth.py",
    "test_api_health.py",
    "test_api_benchmarks.py",
    "test_api_jobs.py",
    "test_api_models.py",
    "test_api_presets.py",
    "test_api_nodes.py",
    "test_api_instances.py",
]

# Part B: Playbooks, prompts, SSE, WebUI, MCP, startup, merge_chain, wizard
# Slower group (~12s total), some tests skip when external services unavailable
PART_B_FILES = [
    "test_playbooks.py",
    "test_prompts.py",
    "test_sse.py",
    "test_webui.py",
    "test_mcp.py",
    "test_startup.py",
    "test_merge_chain.py",
    "test_wizard_steps.py",
]

# All files (order matters for reproducibility)
ALL_FILES = PART_A_FILES + PART_B_FILES


# ---------------------------------------------------------------------------
# Auto-apply part_a / part_b markers based on filename
# Usage: pytest -m part_a  OR  pytest -m part_b
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    """Auto-assign @pytest.mark.part_a or @pytest.mark.part_b based on filename."""
    for item in items:
        filename = item.fspath.basename
        if filename in PART_A_FILES:
            item.add_marker(pytest.mark.part_a)
        elif filename in PART_B_FILES:
            item.add_marker(pytest.mark.part_b)
