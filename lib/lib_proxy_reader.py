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

"""quickrobot (v0.04) — Universal HTTP proxy reader.

Handles response body reading for reverse-proxy use cases including:
- Regular responses with Content-Length (JSON API, static files)
- Streaming responses (SSE/Server-Sent Events from llama.cpp)
- Chunked Transfer-Encoding
- Connection resets and timeouts with descriptive errors

Functions:
    proxy_request — Unified request/response proxy helper.

Usage:
    body, status, headers = proxy_request(url, data=None, headers=None,
                                          method="GET", timeout=60)
"""

import urllib.request as _urq
import urllib.error as _ure
import urllib.parse as _urllib_parse


def proxy_request(url, data=None, headers=None, method="GET", timeout=60):
    """Send an HTTP request and read the response body.

    Handles both regular responses (with Content-Length) and streaming/chunked
    responses (SSE from llama.cpp, no Content-Length). Reads in 8192-byte chunks
    when the Content-Length header is absent.

    Args:
        url: Target URL string.
        data: Request body bytes (None for GET/HEAD).
        headers: Dict of HTTP headers to include.
        method: HTTP method string (default "GET").
        timeout: Connection/response timeout in seconds
                 (default 60 for streaming, was 10 for legacy).

    Returns:
        Tuple of (body_bytes, status_code, response_headers_dict).

    Raises:
        _ure.HTTPError: On HTTP error responses (4xx, 5xx).
        ProxyConnectionError: On connection/timeout/read errors.
    """
    req = _urq.Request(url, data=data, headers=headers or {}, method=method)

    try:
        resp = _urq.urlopen(req, timeout=timeout)
    except _ure.HTTPError as e:
        # Re-raise HTTPError so the caller can access the error body (e.g. 409 CONFLICT with MODEL_MISMATCH detail)
        raise
    except _ure.URLError as e:
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        raise ProxyConnectionError(f"Connection failed: {reason}") from e

    status_code = resp.getcode()
    resp_headers = {k: v for k, v in resp.getheaders()}
    content_type = resp_headers.get("Content-Type", "").lower()

    # Detect streaming/chunked responses.
    # Prefer Content-Length when both Transfer-Encoding and Content-Length
    # are present (common in proxy chains). Chunked only when there's no
    # Content-Length or it explicitly says "chunked".
    has_content_length = any(k.lower() == "content-length" for k in resp_headers)
    te_raw = ""
    for k, v in resp_headers.items():
        if k.lower() == "transfer-encoding":
            te_raw = v
    is_chunked = "chunked" in te_raw.lower() and not has_content_length
    is_streaming = "text/event-stream" in content_type

    if has_content_length or not (is_chunked or is_streaming):
        # Regular response with known size.
        # When both Transfer-Encoding and Content-Length are present,
        # urllib's read() prefers chunked parsing. Read directly from the
        # underlying socket buffer when Content-Length is available.
        try:
            length_str = ""
            for k, v in resp_headers.items():
                if k.lower() == "content-length":
                    length_str = v
                    break
            content_length = int(length_str) if length_str.isdigit() else None
            if content_length is not None:
                # Read directly from the socket to bypass chunked parsing
                body = b""
                while len(body) < content_length:
                    remaining = content_length - len(body)
                    chunk = resp.fp.read(remaining)  # raw socket read
                    if not chunk:
                        break
                    body += chunk
            else:
                body = resp.read()
        except Exception as exc:
            raise ProxyConnectionError(f"Read failed: {exc}") from exc
    else:
        # Streaming/chunked response — read in chunks
        body = _read_all_chunks(resp)

    return body, status_code, resp_headers


def _read_all_chunks(resp):
    """Read a streaming/chunked HTTP response in 8KB chunks.

    Args:
        resp: urllib response object.

    Returns:
        Concatenated bytes from all chunks.
    """
    chunks = []
    while True:
        try:
            chunk = resp.read(8192)
        except Exception:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


class ProxyConnectionError(Exception):
    """Raised when the proxy cannot connect to or read from the target server."""
