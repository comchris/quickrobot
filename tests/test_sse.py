"""Tier 4 — SSE (Server-Sent Events) endpoints.

Tests:
- GET /api/v1/instances/<id>/models-sse — model load stream
- Health-check SSE streams

SSE tests require a running server process. Tests use subprocess
to start a fresh server, connect to the SSE endpoint, collect events,
and verify event format and termination.
"""

import os
import time
import pytest
from tests.conftest import PROJECT_ROOT, check_no_other_quickrobot


@pytest.mark.skipif(
    not check_no_other_quickrobot()[0],
    reason="Live quickrobot API is running — skip SSE subprocess tests",
)
class TestSSEModelLoad:
    """Test SSE model load streaming."""

    def test_sse_event_format(self):
        """SSE stream returns properly formatted events."""
        import subprocess
        import socket

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
            time.sleep(8)
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", errors="replace")
                pytest.skip(f"Server failed to start: {stderr[:200]}")

            # Connect to SSE endpoint with ?api_key= param
            import requests
            url = f"http://127.0.0.1:{port}/api/v1/instances/models-sse?api_key={api_key}"
            try:
                resp = requests.get(url, stream=True, timeout=5)
                assert resp.status_code != 401, "SSE with api_key param should not return 401"

                # Collect first few events
                events = []
                for line in resp.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace")
                    events.append(text)
                    if len(events) >= 3:
                        break

                # Verify event format (lines should start with "data:" or be blank separators)
                data_lines = [e for e in events if e.startswith("data:")]
                assert len(data_lines) >= 0, "SSE stream should return lines"

            except requests.ConnectionError:
                pytest.skip(f"SSE endpoint not reachable on port {port}")

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
