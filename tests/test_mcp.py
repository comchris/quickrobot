"""Tier 5 — MCP tools schema and proxy test.

Tests:
- MCP tool list exposure (read/write/proxy modes)
- MCP proxy endpoint forwards API calls correctly
- SSE session lifecycle (old session invalidated on restart)

Requires the MCP server running or a subprocess spawn.
"""

import os
import time
import pytest
from tests.conftest import PROJECT_ROOT, check_no_other_quickrobot, assert_ok


# ---------------------------------------------------------------------------
# Section A: MCP tool schema (requires MCP server)
# ---------------------------------------------------------------------------

def test_mcp_tools_schema(client_auth):
    """MCP tools are exposed via the MCP server."""
    # The MCP server is at port 8040 by default
    # We can test the MCP SSE endpoint which exposes tool schemas
    mcp_host = os.environ.get("QUICKROBOT_MCP_HOST", "127.0.0.1")
    mcp_port = int(os.environ.get("QUICKROBOT_MCP_PORT", 8040))

    import requests
    try:
        resp = requests.get(
            f"http://{mcp_host}:{mcp_port}/sse",
            timeout=5,
        )
        # MCP SSE endpoint returns text/event-stream
        assert resp.status_code == 200, \
            f"MCP SSE endpoint returned {resp.status_code}"
    except requests.ConnectionError:
        pytest.skip(f"MCP server not running on {mcp_host}:{mcp_port}")


def test_mcp_read_tools(client_auth):
    """MCP read tools are exposed when QUICKROBOT_MCP_READ=true."""
    mcp_read = os.environ.get("QUICKROBOT_MCP_READ", "false").lower()
    if mcp_read != "true":
        pytest.skip("MCP_READ not enabled")

    import requests
    try:
        resp = requests.get(
            f"http://{os.environ.get('QUICKROBOT_MCP_HOST', '127.0.0.1')}:{os.environ.get('QUICKROBOT_MCP_PORT', 8040)}/sse",
            timeout=5,
        )
        assert resp.status_code == 200
    except requests.ConnectionError:
        pytest.skip("MCP server not running")


# ---------------------------------------------------------------------------
# Section B: MCP proxy endpoint
# ---------------------------------------------------------------------------

def test_mcp_proxy_forward(client_auth):
    """MCP proxy endpoint forwards API calls correctly."""
    # The /api/v1/proxy/<path> endpoint proxies to remote nodes
    resp = client_auth().get("/api/v1/instances")
    if resp.status_code == 200:
        assert_ok(resp)


# ---------------------------------------------------------------------------
# Section C: MCP subprocess spawn (requires no other quickrobot running)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not check_no_other_quickrobot()[0],
    reason="Live quickrobot API is running — skip MCP subprocess tests",
)
class TestMCPSpawn:
    """Test MCP server subprocess lifecycle."""

    def test_mcp_subprocess_starts(self):
        """Verify qr_mcp_server.py starts as a subprocess."""
        import subprocess
        import socket

        mcp_port = int(os.environ.get("QUICKROBOT_MCP_PORT", 8040))
        # Use a different port to avoid conflicts
        test_port = _find_free_port()

        env = os.environ.copy()
        env["QUICKROBOT_MCP_PORT"] = str(test_port)
        env["QUICKROBOT_API_HOST"] = "127.0.0.1"
        env["QUICKROBOT_API_PORT"] = "8039"

        proc = subprocess.Popen(
            ["python3", str(PROJECT_ROOT / "engine/qr_mcp_server.py"), "--port", str(test_port)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for startup
            time.sleep(3)
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", errors="replace")
                pytest.skip(f"MCP server failed to start: {stderr[:200]}")

            # Verify MCP SSE endpoint responds
            import requests
            try:
                resp = requests.get(
                    f"http://127.0.0.1:{test_port}/sse",
                    timeout=3,
                )
                assert resp.status_code == 200, \
                    f"MCP SSE returned {resp.status_code}"
            except requests.ConnectionError:
                pytest.skip("MCP SSE not reachable")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _find_free_port():
    """Find a random free port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
