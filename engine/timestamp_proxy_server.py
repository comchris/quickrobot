#!/usr/bin/env python3
"""Quickrobot Timestamp Proxy — Standalone server.

Transparent chat proxy between clients and llama.cpp servers.
Injects timestamps into user messages, captures response timing.

Usage:
    python3 timestamp_proxy_server.py --port 8090 --db 1

Environment variables (QUICKROBOT_TS_PROXY_* prefix):
    QUICKROBOT_TS_PROXY_BACKEND_HOST — llama.cpp server host (required)
    QUICKROBOT_TS_PROXY_BACKEND_PORT — llama.cpp server port (default: 8080)
    QUICKROBOT_TS_PROXY_INJECT_USER_TIMESTAMP — boolean (default: false)
    QUICKROBOT_TS_PROXY_INJECT_RESPONSE_TIME — boolean (default: false)
    QUICKROBOT_TS_PROXY_TIMESTAMP_POSITION — "front"|"back"|"both" (default: front)
    QUICKROBOT_TS_PROXY_TIMESTAMP_FORMAT — strftime format (default: %Y-%m-%d %H:%M:%S)

Health endpoint: GET /health → JSON status
Proxy endpoint:  POST /v1/chat/completions → proxied to backend
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request as _urq
import urllib.error as _ure
import urllib.parse as _up
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# ---------------------------------------------------------------------------
# Logging setup — write to logs/timestamp_proxy.log if available
# ---------------------------------------------------------------------------
logger = logging.getLogger("timestamp_proxy")
logger.setLevel(logging.DEBUG)

_log_path = os.environ.get("QUICKROBOT_LOG_PATH", "")
if _log_path:
    try:
        _h = logging.FileHandler(_log_path)
        _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
        logger.addHandler(_h)
    except Exception:
        pass

_stream_h = logging.StreamHandler(sys.stderr)
_stream_h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(_stream_h)

# ---------------------------------------------------------------------------
# Configuration — CLI args > env vars > defaults
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description="Timestamp Proxy Server")
    p.add_argument("--port", type=int, default=None, help="Listen port")
    p.add_argument("--db", type=str, default=None, help="Node ID (for logging)")
    return p.parse_args()


def _get_env(key, default=None):
    """Read env var, fall back to CLI arg."""
    val = os.environ.get(key)
    if val is not None:
        return val
    # CLI args are accessible via sys.argv for simple scalar values
    return default


def load_config():
    """Load configuration from environment with sensible defaults.

    Supports multi-target via TS_PROXY_TARGET_N_* env vars:
        TS_PROXY_TARGET_0_HOST=192.168.31.11
        TS_PROXY_TARGET_0_PORT=8080
        TS_PROXY_TARGET_0_TOKEN=YFoLkZ1s...

    Only the first target is used for proxying (mirror mode future).
    """
    port_str = _get_env("QUICKROBOT_TS_PROXY_PORT", "") or _get_env("PORT", "")
    try:
        port = int(port_str) if port_str else 8090
    except (ValueError, TypeError):
        port = 8090

    # Multi-target resolution (from deploy-time resolved env vars)
    targets = []
    idx = 0
    while True:
        host = os.environ.get(f"TS_PROXY_TARGET_{idx}_HOST", "")
        if not host:
            break
        port_val = os.environ.get(f"TS_PROXY_TARGET_{idx}_PORT", "8080")
        token = os.environ.get(f"TS_PROXY_TARGET_{idx}_TOKEN", "")
        try:
            port_val = int(port_val)
        except (ValueError, TypeError):
            port_val = 8080
        targets.append({
            "host": host,
            "port": port_val,
            "token": token if token else None,
        })
        idx += 1

    # Backward compat: fall back to single-target env vars if no multi-target found
    backend_host = ""
    backend_port = 0
    if not targets:
        backend_host = _get_env("backend_host", "127.0.0.1")
        try:
            backend_port = int(_get_env("backend_port", "8080"))
        except (ValueError, TypeError):
            backend_port = 8080
        targets.append({
            "host": backend_host,
            "port": backend_port,
            "token": None,
        })

    inject_user_ts = _get_env("ts_proxy_inject_user_timestamp", "false").lower() in ("true", "1", "yes")
    inject_resp_time = _get_env("ts_proxy_inject_response_time", "false").lower() in ("true", "1", "yes")
    ts_position = _get_env("ts_proxy_timestamp_position", "front")
    if ts_position not in ("front", "back", "both"):
        ts_position = "front"
    ts_format = _get_env("ts_proxy_timestamp_format", "%Y-%m-%d %H:%M:%S")

    return {
        "port": port,
        "backend_host": backend_host or targets[0]["host"],
        "backend_port": backend_port or targets[0]["port"],
        "targets": targets,
        "inject_user_timestamp": inject_user_ts,
        "inject_response_time": inject_resp_time,
        "timestamp_position": ts_position,
        "timestamp_format": ts_format,
    }


# ---------------------------------------------------------------------------
# Core proxy logic
# ---------------------------------------------------------------------------

def format_timestamp(fmt="%Y-%m-%d %H:%M:%S"):
    """Generate timestamp string in configured format."""
    return datetime.now().strftime(fmt)


def inject_user_timestamp(messages, position, timestamp_str):
    """Inject timestamp into user message content.

    Mutates messages in place.
    """
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            # Handle list-of-parts content (multimodal)
            parts = []
            for p in content if isinstance(content, list) else [content]:
                if isinstance(p, dict) and p.get("type") == "text":
                    text = p.get("text", "")
                    if position in ("front", "both"):
                        text = f"{timestamp_str} {text}"
                    if position in ("back", "both"):
                        text = f"{text} {timestamp_str}"
                    parts.append({"type": "text", "text": text})
                else:
                    parts.append(p)
            msg["content"] = parts
        else:
            if position in ("front", "both"):
                content = f"{timestamp_str} {content}"
            if position in ("back", "both"):
                content = f"{content} {timestamp_str}"
            msg["content"] = content
    return messages


def parse_request_data(body_bytes):
    """Parse JSON request body, return (data_dict, raw_bytes)."""
    try:
        data = json.loads(body_bytes) if isinstance(body_bytes, bytes) else json.loads(body_bytes.decode("utf-8"))
        return data
    except Exception as e:
        logger.debug("JSON parse failed: %s", e)
        return None


def build_request_url(target):
    """Build backend request URL for a target dict."""
    return f"http://{target['host']}:{target['port']}/v1/chat/completions"


def proxy_completion_sync(config, body_bytes):
    """Non-streaming proxy: buffer, modify, return.

    Uses the first target from config["targets"]. Mirror mode (fan-out) is future.

    1. Parse JSON request body
    2. Inject timestamps into user messages (if enabled)
    3. Forward to backend
    4. Capture timing
    5. Inject timing into response (if enabled)
    6. Return modified response
    """
    if not isinstance(body_bytes, bytes):
        body_bytes = body_bytes.encode("utf-8")

    # Phase A: modify request
    data = parse_request_data(body_bytes)
    if data and config["inject_user_timestamp"]:
        ts_str = format_timestamp(config["timestamp_format"])
        inject_user_timestamp(data.get("messages", []), config["timestamp_position"], ts_str)
        modified_body = json.dumps(data).encode("utf-8")
    else:
        modified_body = body_bytes

    # Phase B: forward to first target
    target = config["targets"][0] if config["targets"] else {"host": "127.0.0.1", "port": 8080, "token": None}
    url = build_request_url(target)
    headers = {"Content-Type": "application/json"}

    # Inject auth token if the target requires it
    if target.get("token"):
        headers["Authorization"] = f"Bearer {target['token']}"

    req = urllib.request.Request(
        url,
        data=modified_body,
        method="POST",
        headers=headers,
    )

    start_time = time.time()
    try:
        resp = _urq.urlopen(req, timeout=300)  # 5 min timeout for long completions
        duration_ms = int((time.time() - start_time) * 1000)
        response_bytes = resp.read()
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error("Backend request failed: %s", e)
        return f'{{"error": "backend_error", "detail": "{str(e)}", "proxy_timing_ms": {duration_ms}}}'.encode("utf-8")

    # Phase C: modify response
    if config["inject_response_time"]:
        try:
            resp_data = json.loads(response_bytes)
            resp_data["timestamp_proxy_timing_ms"] = duration_ms
            return json.dumps(resp_data).encode("utf-8")
        except Exception:
            pass

    return response_bytes


class TimestampProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler for timestamp proxy.

    Routes:
        GET  /health     → JSON health status
        POST /v1/chat/completions → proxied to backend with timestamp injection
    """

    config = None  # Set by server after startup

    def log_message(self, format, *args):
        """Override default logging to use our logger."""
        logger.debug("%s - %s", self.address_string(), format % args)

    def address_string(self):
        return self.client_address[0] if self.client_address else "?"

    def do_GET(self):
        if self.path == "/health":
            config = self.__class__.config
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            status = {
                "status": "ok",
                "targets": [
                    {"host": t["host"], "port": t["port"], "token_set": bool(t.get("token"))}
                    for t in config.get("targets", [])
                ],
                "target_count": len(config.get("targets", [])),
                "inject_user_timestamp": config.get("inject_user_timestamp", False),
                "inject_response_time": config.get("inject_response_time", False),
                "uptime_seconds": int(time.time() - self.__class__._start_time),
            }
            self.wfile.write(json.dumps(status).encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "not_found"}')

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            config = self.__class__.config
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"

            response = proxy_completion_sync(config, body_bytes)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "not_found"}')

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def run_server(config):
    """Start the HTTP server."""
    # Store config on handler class for access in request handlers
    TimestampProxyHandler.config = config
    TimestampProxyHandler._start_time = time.time()

    server = HTTPServer(("0.0.0.0", config["port"]), TimestampProxyHandler)
    logger.info(
        "timestamp_proxy STARTUP: pid=%d port=%d backend=%s:%d "
        "inject_user_ts=%s inject_resp_time=%s position=%s format=%s",
        os.getpid(), config["port"], config["backend_host"], config["backend_port"],
        config["inject_user_timestamp"], config["inject_response_time"],
        config["timestamp_position"], config["timestamp_format"],
    )

    # Structured startup banner (matching other system engines)
    print(
        f"[ts] STARTUP: pid={os.getpid()} port={config['port']} "
        f"backend={config['backend_host']}:{config['backend_port']} "
        f"inject_user_ts={config['inject_user_timestamp']} "
        f"inject_resp_time={config['inject_response_time']}",
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = _parse_args()
    config = load_config()

    # CLI args can override env vars
    if args.port:
        config["port"] = args.port

    logger.info("timestamp_proxy starting (port=%d, backend=%s:%d)",
                config["port"], config["backend_host"], config["backend_port"])

    run_server(config)
