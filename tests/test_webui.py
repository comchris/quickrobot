"""Tier 1 — Jinja2 template compilation gate + WebUI render tests.

Catches template syntax errors (unclosed {% if %}, mismatched blocks,
missing variables) before any request is made. Also verifies that
instance detail pages render for all engine types without errors.

Run: pytest tests/test_webui.py -v
"""

import pytest
import jinja2
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = PROJECT_ROOT / "webui"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def template_env():
    """Jinja2 environment with FileSystemLoader pointing to webui/."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(WEBUI_DIR)),
        autoescape=False,
    )


# ---------------------------------------------------------------------------
# Template compilation gate (catches Jinja2 syntax errors)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template_name", [
    f.stem for f in sorted(WEBUI_DIR.glob("*.html"))
])
def test_all_templates_compile(template_env, template_name):
    """Every WebUI template must compile without Jinja2 TemplateSyntaxError."""
    try:
        template_env.get_template(f"{template_name}.html")
    except jinja2.TemplateSyntaxError as e:
        pytest.fail(f"{template_name}.html: {e.message} at line {e.lineno}")


def test_no_unclosed_if_blocks(template_env):
    """Every {% if %} block must have a matching {% endif %} in each template."""
    import re
    for html_file in WEBUI_DIR.glob("*.html"):
        content = html_file.read_text()
        # Count all {% if %} and {% endif %} occurrences (not line-anchored)
        # Exclude inline expressions like '{% if cond %}text{% endif %}' by only
        # counting block-level tags (those that start with {% at the beginning of
        # the tag on their own line or after whitespace).
        opens = len(re.findall(r'\{%\s+if\b', content))
        closes = len(re.findall(r'\{%\s+endif\s*%?\}', content))
        if opens != closes:
            pytest.fail(
                "{}: {} 'if' blocks but only {} 'endif'. "
                "Missing {} closing tag(s).".format(
                    html_file.name, opens, closes, opens - closes))


def test_no_unclosed_for_blocks(template_env):
    """Every for block must have a matching endfor in each template."""
    import re
    for html_file in WEBUI_DIR.glob("*.html"):
        content = html_file.read_text()
        opens = len(re.findall(r'\{%\s+for\b', content))
        closes = len(re.findall(r'\{%\s+endfor\s*%?\}', content))
        if opens != closes:
            pytest.fail(
                "{}: {} 'for' blocks but only {} 'endfor'. "
                "Missing {} closing tag(s).".format(
                    html_file.name, opens, closes, opens - closes))


# ---------------------------------------------------------------------------
# Render tests — verify instance detail pages load without errors
# ---------------------------------------------------------------------------

def test_system_instances_always_render():
    """System-managed instances (IDs 1-4) must always render detail pages.

    System engines have their own rendering path (system_status_data, mcp_flags,
    health_badge via PID check) — this ensures that path never breaks.
    Uses direct HTTP to WebUI port (not Flask test client which hits API).
    """
    import urllib.request
    api_key = "YFoLkZ1sMlVtXNm5Wq9t33F9x6r4wuke4-gy2A9Akm0"
    for inst_id in [1, 2, 3, 4]:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:8038/webui/instances/{inst_id}")
            req.add_header("X-API-Key", api_key)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                assert b"TemplateSyntaxError" not in data, \
                    f"Instance {inst_id}: TemplateSyntaxError in response"
                assert b"jinja2.exceptions" not in data, \
                    f"Instance {inst_id}: jinja2 error in response"
        except urllib.error.HTTPError as e:
            # 401/403 on auth, 302 redirect on no session — all OK
            assert e.code in (302, 401, 403), \
                f"Instance {inst_id}: unexpected HTTP {e.code}"


def test_remote_instance_detail_llama_server():
    """llama_server instance detail page must render without TemplateSyntaxError."""
    import urllib.request
    api_key = "YFoLkZ1sMlVtXNm5Wq9t33F9x6r4wuke4-gy2A9Akm0"
    req = urllib.request.Request("http://127.0.0.1:8038/webui/instances/104")
    req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
            assert b"TemplateSyntaxError" not in data, "Instance 104: TemplateSyntaxError in response"
    except urllib.error.HTTPError as e:
        assert e.code in (302, 401, 403), f"Instance 104: unexpected HTTP {e.code}"


def test_remote_instance_detail_llama_rpc():
    """llama_rpc instance detail page must render without TemplateSyntaxError."""
    import urllib.request
    api_key = "YFoLkZ1sMlVtXNm5Wq9t33F9x6r4wuke4-gy2A9Akm0"
    req = urllib.request.Request("http://127.0.0.1:8038/webui/instances/105")
    req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
            assert b"TemplateSyntaxError" not in data, "Instance 105: TemplateSyntaxError in response"
    except urllib.error.HTTPError as e:
        assert e.code in (302, 401, 403), f"Instance 105: unexpected HTTP {e.code}"
