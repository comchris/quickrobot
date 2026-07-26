"""Reusable cryptographically secure token generation.

Provides generate_api_key() for creating URL-safe random tokens used in:
  - API server authentication (QUICKROBOT_API_KEY)
  - llama-server per-instance auth tokens (instances.auth_token)
  - Service-level tokens (WEBUI_TOKEN, MCP_TOKEN)
"""

import secrets


def generate_api_key(length: int = 32) -> str:
    """Generate a cryptographically secure API key.

    Uses ``secrets.token_urlsafe()`` which produces URL-safe base64-encoded
    random bytes. The default of 32 bytes produces a 43-character string
    that never contains ``+``, ``=``, or ``/`` (which could interfere with
    URL parsing, shell escaping, or YAML quoting).

    Args:
        length: Number of random bytes. Default 32 = 43-char output.
                Use 64 for 86-char output (extra security margin).

    Returns:
        URL-safe base64-encoded random string.

    Example:
        >>> key = generate_api_key()
        >>> len(key)
        43

    Usage:
        - API key generation (system-level)
        - llama-server per-instance auth tokens
        - Service token rotation
    """
    return secrets.token_urlsafe(length)
