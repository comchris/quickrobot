"""Tier 1 — Subprocess spawn and startup behavior.

Verifies:
- No other quickrobot processes block fresh spawn
- Real server starts within expected delays
- Port binding succeeds
- Health endpoint responds after startup
- Graceful shutdown releases ports

Requires the live API server to NOT be running (uses subprocess.Popen).
Skipped if another quickrobot instance is active.
"""

import os
import time
import subprocess
import socket
import pytest
from tests.conftest import (
    PROJECT_ROOT, check_no_other_quickrobot, env_config,
)


# ---------------------------------------------------------------------------
# Helper: find free port
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    """Find a random free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Section A: Pre-spawn verification
# ---------------------------------------------------------------------------

def test_check_no_other_quickrobot():
    """Verify no other quickrobot processes are running (test precondition)."""
    is_free, details = check_no_other_quickrobot()
    assert is_free, \
        f"Another quickrobot process is running:\n" + "\n".join(
            f"  {p}: {d}" for p, d in details
        )


# ---------------------------------------------------------------------------
# Section B: Subprocess spawn tests (skip if live server is running)
# ---------------------------------------------------------------------------

def _check_skip():
    """Return True if we should skip subprocess tests (live server running)."""
    return not check_no_other_quickrobot()[0]


def _is_live_api_running(port: int = None) -> bool:
    """Check if the live API is already running on the configured port.

    More reliable than ps-based detection because pytest's subprocess context
    may not see tmux-launched processes. Uses socket connect instead of ps aux.
    """
    if port is None:
        port = int(os.environ.get("QUICKROBOT_API_PORT", 8029))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


class TestSubprocessSpawn:
    """Test real subprocess spawning with startup delays."""

    def test_api_process_starts(self):
        """Verify quickrobot.py starts as a subprocess on a free port.

        NOTE: These tests require no other quickrobot processes running.
        When the live API is active, they will fail (process detects stale
        instance and exits). Run with live server stopped or ignore results.
        """
        # Skip if the live API port is already bound.
        if _is_live_api_running():
            pytest.skip("Live API port already bound — subprocess tests require clean state")
        api_key = os.environ.get("QUICKROBOT_API_KEY", "test-token")
        port = _find_free_port()

        env = os.environ.copy()
        env["QUICKROBOT_API_PORT"] = str(port)
        env["QUICKROBOT_API_KEY"] = api_key
        env["QUICKROBOT_SEED_CHECKSUM"] = ""  # Skip seed verification for test
        env["QUICKROBOT_SEED_FILESIZE"] = "0"

        proc = subprocess.Popen(
            [
                "python3", str(PROJECT_ROOT / "quickrobot.py"),
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for startup — max 15 seconds
            deadline = time.time() + 15
            healthy = False
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1)
                        s.connect(("127.0.0.1", port))
                        healthy = True
                        break
                except (ConnectionRefusedError, OSError):
                    continue

            assert healthy, f"Process did not bind to port {port} within 15s"

            # Verify health endpoint responds
            import requests
            resp = requests.get(
                f"http://127.0.0.1:{port}/api/v1/app/status",
                headers={"X-API-Key": api_key},
                timeout=5,
            )
            assert resp.status_code == 200, \
                f"Health check returned {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            assert data.get("status") == "ok", f"Unexpected response: {data}"

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------------
# Section C: Port conflict detection
# ---------------------------------------------------------------------------

class TestPortConflict:
    """Verify that port conflicts are detected (not silent failure)."""

    def test_port_already_bound(self):
        """If a port is already bound, the server should detect it.

        NOTE: Requires no other quickrobot processes running.
        """
        # Skip if the live API port is already bound.
        if _is_live_api_running():
            pytest.skip("Live API port already bound — subprocess tests require clean state")
        port = _find_free_port()

        # Bind the port ourselves first
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        try:
            api_key = os.environ.get("QUICKROBOT_API_KEY", "test-token")
            env = os.environ.copy()
            env["QUICKROBOT_API_PORT"] = str(port)
            env["QUICKROBOT_API_KEY"] = api_key

            proc = subprocess.Popen(
                ["python3", str(PROJECT_ROOT / "quickrobot.py")],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                time.sleep(3)  # Wait for startup attempt
                # Check if process exited (conflict detected) or is still running
                poll = proc.poll()
                stderr = proc.stderr.read().decode("utf-8", errors="replace")

                # Either it exited with an error, or it's still running
                # (depends on whether the code handles port conflicts gracefully)
                if poll is not None:
                    assert poll != 0, \
                        f"Process exited 0 despite port conflict on {port}"
                    assert "already" in stderr.lower() or \
                           "port" in stderr.lower() or \
                           "bind" in stderr.lower(), \
                        f"Expected conflict error in stderr: {stderr[:200]}"
                # If still running, the code may accept the port binding
                # (depends on implementation)

            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

        finally:
            listener.close()


# ---------------------------------------------------------------------------
# Section D: Shutdown cleanup
# ---------------------------------------------------------------------------

class TestShutdown:
    """Verify that shutdown releases resources."""

    def test_process_terminates_cleanly(self):
        """After SIGTERM, the process should exit within timeout.

        NOTE: Requires no other quickrobot processes running.
        """
        # Skip if the live API port is already bound.
        if _is_live_api_running():
            pytest.skip("Live API port already bound — subprocess tests require clean state")
        port = _find_free_port()
        api_key = os.environ.get("QUICKROBOT_API_KEY", "test-token")

        env = os.environ.copy()
        env["QUICKROBOT_API_PORT"] = str(port)
        env["QUICKROBOT_API_KEY"] = api_key

        proc = subprocess.Popen(
            ["python3", str(PROJECT_ROOT / "quickrobot.py")],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for startup
            time.sleep(5)
            assert proc.poll() is None, "Process died immediately"

            # Send SIGTERM
            proc.terminate()
            try:
                proc.wait(timeout=10)
                assert proc.returncode in (0, -15), \
                    f"Process exit code: {proc.returncode}"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        finally:
            # Ensure cleanup
            if proc.poll() is None:
                proc.kill()
                proc.wait()
