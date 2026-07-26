# Copyright 2026 comchris quickrobot .de project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the URL at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared logging infrastructure for all quickrobot system engines.

Provides a single factory function that gives every engine dual handlers
(consolE stderr + log file) with consistent format and runtime level control.

Usage:
    import lib.lib_logging as ll
    logger = ll.create_logger("scheduler", debug_level=10)
    logger.info("message")  # → writes to BOTH console AND file

Engine names map to: "scheduler", "mcp", "webui", "api"
"""

import logging
import os
import sys


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOG_DIR = "logs"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"
# Use %(name)s which will be "qr.scheduler", "qr.mcp", etc.
_LOG_FMT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB rotation threshold

# Default log level when no env var is set (WARNING = quiet production)
_DEFAULT_LEVEL = logging.WARNING


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_log_path(engine_name):
    """Get the log file path for a system engine.

    Args:
        engine_name: Short name like "scheduler", "mcp", "webui"

    Returns:
        Absolute path to the engine's log file.
    """
    return os.path.join(os.getcwd(), _LOG_DIR, f"{engine_name}.log")


def _rotate_if_needed(log_path):
    """Truncate log file if it exceeds MAX_LOG_BYTES.

    Args:
        log_path: Path to the log file to check.

    Returns:
        True if rotation occurred, False otherwise.
    """
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > _MAX_LOG_BYTES:
            with open(log_path, "w"):
                pass
            return True
    except OSError:
        pass
    return False


def create_logger(engine_name, debug_level=None):
    """Create a logger with dual handlers (stderr + file).

    All calls to the returned logger write to BOTH console and file.
    The console handler is filtered by debug_level; the file handler
    always captures DEBUG level (full archival).

    Args:
        engine_name: Short name for log prefix (e.g. "scheduler").
                     Logger name becomes "qr.<engine_name>".
        debug_level: Numeric level from .quickrobot.env or env var.
                     >= 10 → DEBUG, < 10 → WARNING.
                     If None, falls back to QUICKROBOT_<NAME>_LOG_LEVEL env
                     var, then defaults to WARNING.

    Returns:
        logging.Logger configured with both stderr and file handlers.
    """
    logger_name = f"qr.{engine_name}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)  # lowest level; handlers filter
    logger.propagate = False

    # Determine effective console level
    if debug_level is None:
        env_key = f"QUICKROBOT_{engine_name.upper()}_LOG_LEVEL"
        env_val = os.environ.get(env_key, "")
        if env_val.isdigit():
            debug_level = int(env_val)
        else:
            debug_level = _DEFAULT_LEVEL

    console_level = logging.DEBUG if debug_level >= 10 else _DEFAULT_LEVEL

    # Stderr handler (tmux capture / console)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(console_level)
    sh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_DATEFMT))
    logger.addHandler(sh)

    # File handler — only add when NOT a managed subprocess.
    # Managed subprocesses (WebUI/MCP/Scheduler) have stdout/stderr redirected
    # to the same log file by lib_system_engine. Adding a FileHandler would
    # duplicate every log line. Detection: QUICKROBOT_LOG_PATH env var is set
    # by lib_system_engine when spawning managed subprocesses.
    _managed_log_path = os.environ.get("QUICKROBOT_LOG_PATH", "")
    _is_managed_subprocess = bool(_managed_log_path)

    # File handler — always logs everything to file (only for standalone/direct mode)
    if not _is_managed_subprocess:
        log_path = get_log_path(engine_name)
        _rotate_if_needed(log_path)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_DATEFMT))
        logger.addHandler(fh)

    return logger


def update_logger_level(logger, debug_level):
    """Update only the stderr handler level (file always logs DEBUG).

    Call this at runtime when the user changes log level via API.

    Args:
        logger: The logger returned by create_logger().
        debug_level: Same numeric value as passed to create_logger().
    """
    lvl = logging.DEBUG if debug_level >= 10 else _DEFAULT_LEVEL
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setLevel(lvl)


def get_werkzeug_logger(log_level=None):
    """Configure werkzeug's application logger for the API server.

    Unlike other engines, the Flask API uses werkzeug's built-in logger
    which writes to stdout (captured by tmux). This function sets its
    level and optionally adds a file handler.

    Args:
        log_level: Numeric or string log level. None = WARNING (production).

    Returns:
        The configured werkzeug logger instance.
    """
    if log_level is None:
        env_val = os.environ.get("QUICKROBOT_API_LOG_LEVEL", "")
        if env_val.isdigit():
            log_level = int(env_val)
        else:
            log_level = _DEFAULT_LEVEL

    logger = logging.getLogger("werkzeug")
    if isinstance(log_level, str):
        log_level = getattr(logging, log_level.upper(), _DEFAULT_LEVEL)
    logger.setLevel(log_level)
    return logger
