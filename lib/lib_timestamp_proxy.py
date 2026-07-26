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

"""Shared timestamp proxy logic.

Functions for injecting timestamps into user messages and capturing
response timing for the timestamp_proxy engine.
"""

import logging

logger = logging.getLogger(__name__)

from datetime import datetime


def inject_user_timestamp(messages, position, timestamp_str):
    """Inject timestamp string into user message content.

    Mutates messages list in place. Only affects messages with
    role == "user".

    Args:
        messages: List of message dicts from request body.
        position: "front", "back", or "both".
        timestamp_str: The timestamp text to inject.

    Returns:
        Modified messages list (same objects, mutated in place).
    """
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            if position == "both":
                msg["content"] = f"{timestamp_str} {content} {timestamp_str}"
            elif position == "front":
                msg["content"] = f"{timestamp_str} {content}"
            else:  # back
                msg["content"] = f"{content} {timestamp_str}"
    return messages


def format_timestamp(fmt="%Y-%m-%d %H:%M:%S"):
    """Generate timestamp string in configured format.

    Args:
        fmt: Python strftime format string.

    Returns:
        Formatted timestamp string.
    """
    return datetime.now().strftime(fmt)


def inject_response_timing(response_body, duration_ms):
    """Inject timing into response JSON body.

    For non-streaming responses, adds `timestamp_proxy_timing_ms` to
    the JSON dict. The key is chosen to not conflict with standard
    OpenAI API fields — clients that don't recognize it will ignore it.

    Args:
        response_body: dict response from backend (non-streaming).
        duration_ms: Response time in milliseconds.

    Returns:
        Modified response_body dict with timing field added.
    """
    if isinstance(response_body, dict):
        response_body["timestamp_proxy_timing_ms"] = duration_ms
    return response_body


def parse_sse_timing_line(sse_line, duration_ms):
    """Inject timing into the final SSE chunk's JSON data field.

    For streaming responses, the proxy reads each SSE line, forwards
    most pass-through, and injects timing into the last chunk (identified
    by finish_reason being set).

    Args:
        sse_line: Raw SSE line string (e.g., 'data: {...}').
        duration_ms: Response time in milliseconds.

    Returns:
        Modified SSE line string with timing injected into JSON body,
        or original line if not a data: JSON chunk.
    """
    if not sse_line.startswith("data: "):
        return sse_line

    try:
        import json as _json
        data = _json.loads(sse_line[6:])  # strip "data: " prefix
        # Only modify if it's a valid JSON object with choices (chat completion chunk)
        if isinstance(data, dict) and "choices" in data:
            data["timestamp_proxy_timing_ms"] = duration_ms
            return f"data: {_json.dumps(data)}"
    except Exception as _e:
        logger.debug("timestamp_proxy: failed to parse SSE line for timing injection: %s", _e)

    return sse_line


def is_final_chunk(sse_line):
    """Detect if an SSE line is the final chunk of a streaming response.

    Checks for finish_reason in the choices array — when present,
    this is the last chunk from llama.cpp's /v1/chat/completions stream.

    Args:
        sse_line: Raw SSE line string.

    Returns:
        True if this appears to be the final chunk.
    """
    if not sse_line.startswith("data: "):
        return False

    try:
        import json as _json
        data = _json.loads(sse_line[6:])
        choices = data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            return bool(choices[0].get("finish_reason"))
    except Exception as _e:
        logger.debug("timestamp_proxy: failed to parse SSE line for final chunk detection: %s", _e)

    return False
