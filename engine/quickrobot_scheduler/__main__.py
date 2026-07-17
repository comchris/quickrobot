#!/usr/bin/env python3
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

"""quickrobot — Scheduler Engine (modular rewrite).

Background subprocess that polls for queued jobs, executes stages via
PlaybookRunner, and updates instance state on completion.

Modular architecture:
  stale.py  — stale detection (orphaned jobs, stuck transitions)
  health.py — periodic health check cycle
  runner.py — task execution wrapper
  __main__.py — thin entry point: config loading, main loop, signals

Usage:
    python3 engine/quickrobot_scheduler/__main__.py [--db PATH] [--interval SECS]
"""

import argparse
import logging
import os
import signal
import sys
import time as _time_module

# Ensure project root is on sys.path so sibling packages (db/, lib/) resolve.
# This is required when running as `python3 engine/quickrobot_scheduler/__main__.py`
# because Python only adds the package directory (engine/) to sys.path.
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── SSOT timeouts ───────────────────────────────────────────────────
from lib.qr_engine_ids import (
    QR_TIMEOUT_COMPILE,
    QR_TIMEOUT_SOURCE,
    QR_TIMEOUT_DEFAULT,
    QR_TIMEOUT_HEALTH_CHECK,
    QR_TIMEOUT_JOB,
    QR_STAGE_COMPILE,
    QR_STAGE_SOURCE,
    QR_JOB_HEALTH_CHECK,
)

# ── Module imports ──────────────────────────────────────────────────
from engine.quickrobot_scheduler import stale, health, runner as sched_runner

# ── Logging setup ───────────────────────────────────────────────────

def _setup_logging():
    """Configure logging for the scheduler.

    Writes to both stderr (for tmux capture) and logs/scheduler.log file.
    """
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log_dir = os.path.join(_project_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "scheduler.log")

    logger = logging.getLogger("quickrobot.scheduler")
    logger.setLevel(logging.DEBUG)

    # File handler
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [qr-scheduler] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger.addHandler(fh)

    # Stderr handler
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter(
        "%(asctime)s [qr-scheduler] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger.addHandler(sh)

    # Prevent duplicate propagation
    logger.propagate = False
    return logger


logger = _setup_logging()


# ── Config loading with sanity checks ───────────────────────────────

def load_scheduler_config(db_path):
    """Load scheduler config from engine_configs table.

    Reads all scheduler config values and validates them. Exits on
    parse errors (not silent fallback).

    Args:
        db_path: Path to SQLite database.

    Returns:
        dict with keys: poll_interval_sec, log_level, max_retries.

    Raises:
        SystemExit: On invalid config values (string in int field, out of bounds).
    """
    from db.adapters.configs import get_engine_config as _gec

    config = {}

    # Load scheduler_poll_interval_sec
    row = _gec(db_path, 4, "scheduler_poll_interval_sec")
    raw = row.get("value", "5") if row else "5"
    try:
        v = int(raw)
    except (ValueError, TypeError):
        logger.error("[qr-scheduler] FATAL: scheduler_poll_interval_sec='%s' is not a valid integer", raw)
        sys.exit(1)
    if v < 1 or v > 60:
        logger.error(
            "[qr-scheduler] FATAL: scheduler_poll_interval_sec=%d out of valid range (1-60)", v,
        )
        sys.exit(1)
    config["poll_interval_sec"] = v

    # Load scheduler_max_retries
    row = _gec(db_path, 4, "scheduler_max_retries")
    raw = row.get("value", "3") if row else "3"
    try:
        v = int(raw)
    except (ValueError, TypeError):
        logger.error("[qr-scheduler] FATAL: scheduler_max_retries='%s' is not a valid integer", raw)
        sys.exit(1)
    if v < 1 or v > 10:
        logger.error(
            "[qr-scheduler] FATAL: scheduler_max_retries=%d out of valid range (1-10)", v,
        )
        sys.exit(1)
    config["max_retries"] = v

    # Load scheduler_log_level
    row = _gec(db_path, 4, "scheduler_log_level")
    raw = row.get("value", "info") if row else "info"
    valid_levels = ("debug", "info", "warning", "error", "critical")
    if str(raw).lower() not in valid_levels:
        logger.error(
            "[qr-scheduler] FATAL: scheduler_log_level='%s' is not valid (must be one of %s)",
            raw, ", ".join(valid_levels),
        )
        sys.exit(1)
    config["log_level"] = str(raw).lower()

    # Load health_check_logging (boolean flag for verbose HC logging)
    row = _gec(db_path, 4, "health_check_logging")
    raw_hc = row.get("value", "true") if row else "true"
    config["hc_logging"] = str(raw_hc).lower() not in ("false", "0", "no")

    return config


def update_log_level(config):
    """Update logger level based on loaded config.

    Args:
        config: Config dict with 'log_level' key.
    """
    level_name = config.get("log_level", "info").upper()
    numeric_level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(numeric_level)
    for handler in logger.handlers:
        handler.setLevel(numeric_level)


# ── API health check with config sync ───────────────────────────────

def _api_health_loop(db_path):
    """Periodic health check for the quickrobot API.

    Runs every 10s. On each successful check, also syncs scheduler config
    from engine_configs table (poll_interval, log_level, max_retries).
    After 3 consecutive failures, exits the process.

    This replaces the old api_health_check_loop() from lib.lib_system_engine
    and adds config sync capability.
    """
    import requests as _requests

    api_host = os.getenv("QUICKROBOT_API_HOST")
    api_port_raw = os.getenv("QUICKROBOT_API_PORT")

    if not api_host or not api_port_raw:
        logger.error(
            "[qr-scheduler] FATAL: QUICKROBOT_API_HOST and QUICKROBOT_API_PORT must be set in .quickrobot.env",
        )
        sys.exit(1)

    try:
        api_port = int(api_port_raw)
    except ValueError:
        logger.error("[qr-scheduler] FATAL: QUICKROBOT_API_PORT='%s' is not a valid integer", api_port_raw)
        sys.exit(1)

    base_url = f"http://{api_host}:{api_port}"
    consecutive_failures = 0
    max_failures = 3

    # Load config once at startup
    try:
        config = load_scheduler_config(db_path)
        update_log_level(config)
        logger.info("[qr-scheduler] Config loaded at startup: poll=%ds, retries=%d, log=%s",
                     config["poll_interval_sec"], config["max_retries"], config["log_level"])
    except SystemExit:
        raise  # Re-raise — config load already logged FATAL

    while True:
        try:
            resp = _requests.get(f"{base_url}/api/v1/app/status", timeout=5)
            if resp.status_code != 200:
                raise ConnectionError(f"API returned {resp.status_code}")

            # API reachable — reset failure counter and sync config
            consecutive_failures = 0

            # Periodic config sync (every cycle, not just on startup)
            try:
                new_config = load_scheduler_config(db_path)

                # Check for changed values
                if new_config["poll_interval_sec"] != config.get("poll_interval_sec"):
                    logger.info(
                        "[qr-scheduler] Config synced: poll_interval %ds → %ds",
                        config.get("poll_interval_sec", "?"), new_config["poll_interval_sec"],
                    )
                    config["poll_interval_sec"] = new_config["poll_interval_sec"]

                if new_config["max_retries"] != config.get("max_retries"):
                    logger.info(
                        "[qr-scheduler] Config synced: max_retries %d → %d",
                        config.get("max_retries", "?"), new_config["max_retries"],
                    )
                    config["max_retries"] = new_config["max_retries"]

                if new_config["log_level"] != config.get("log_level"):
                    logger.info(
                        "[qr-scheduler] Config synced: log_level %s → %s",
                        config.get("log_level", "?"), new_config["log_level"],
                    )
                    update_log_level(new_config)
                    config["log_level"] = new_config["log_level"]

                if new_config["hc_logging"] != config.get("hc_logging"):
                    logger.info(
                        "[qr-scheduler] Config synced: hc_logging %s → %s",
                        config.get("hc_logging", "?"), new_config["hc_logging"],
                    )
                    config["hc_logging"] = new_config["hc_logging"]

            except SystemExit:
                # Config sync failed — exit (config is critical)
                raise
            except Exception as exc:
                print(f"[qr-scheduler] Config sync error: {exc}", flush=True)
                logger.warning("[qr-scheduler] Config sync error: %s", exc)

        except Exception as exc:
            consecutive_failures += 1
            print(f"[qr-scheduler] API check failed ({consecutive_failures}/{max_failures}): {exc}", flush=True)
            logger.warning(
                "[qr-scheduler] API check failed (%d/%d): %s",
                consecutive_failures, max_failures, exc,
            )

            if consecutive_failures >= max_failures:
                print("[qr-scheduler] FATAL: API unreachable. Exiting.", flush=True)
                logger.error("[qr-scheduler] FATAL: API unreachable after %d attempts. Exiting.", max_failures)
                # Must use os._exit (not sys.exit) — sys.exit from daemon thread
                # only kills the thread, not the process.
                os._exit(1)

        # Faster retry during failures (5s) vs normal interval (10s) to match WebUI/MCP timing
        _time_module.sleep(5 if consecutive_failures > 0 else 10)


# ── Main process lifecycle ─────────────────────────────────────────

def cleanup_threads():
    """Remove completed threads from tracking list."""
    with sched._bg_lock:
        sched._bg_threads = [t for t in sched._bg_threads if t.is_alive()]


def _process_cycle(config):
    """Execute one full scheduler cycle.

    Order:
      1. Cleanup dead threads
      2. Stale detection (orphaned jobs, stuck transitions)
      3. Health check cycle (interval-gated, per-instance update)
      4. Task execution (pick next queued task, run async)

    Args:
        config: Dict with scheduler config (poll_interval_sec, etc.).
    """
    # Cleanup dead threads
    cleanup_threads()

    # Stale detection (once per cycle, but only resets old stuck jobs)
    try:
        reset = stale.detect_stale_tasks(sched.db_path)
        if reset > 0:
            logger.info("[qr-scheduler] Reset %d stale task(s)", reset)
    except Exception as exc:
        logger.error("[qr-scheduler] Stale detection failed: %s", exc, exc_info=True)

    # Health check cycle
    try:
        hc_logging = config.get("hc_logging", True)
        result = health.run_health_cycle(sched.db_path, sched.runner, hc_logging=hc_logging)
        if result["queued"] > 0:
            logger.info(
                "[qr-scheduler] Health cycle: queued=%d, skipped_no_task=%d, skipped_interval=%d, errors=%d",
                result["queued"],
                result["skipped_no_running_task"],
                result["skipped_interval_window"],
                result["errors"],
            )
    except Exception as exc:
        logger.error("[qr-scheduler] Health cycle failed: %s", exc, exc_info=True)

    # Task execution (pick ONE queued task, run async)
    try:
        task = sched.get_next_queued_task()
        if task:
            sched.execute_task(task)
    except Exception as exc:
        logger.error("[qr-scheduler] Task execution failed: %s", exc, exc_info=True)


# ── Global runner instance (module-level for thread sharing) ────────
sched = None


def main():
    """Entry point for scheduler subprocess."""
    global sched

    # Root guard
    if os.getuid() == 0:
        print("this robot won't run as root", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Quickrobot Staged Playbook Scheduler")
    parser.add_argument("--db", default="data/quickrobot.db", help="Path to SQLite database")
    parser.add_argument("--interval", type=int, default=5, help="Poll interval in seconds")
    parser.add_argument("--playbook-dir", default="playbooks/", help="Playbook directory")
    args = parser.parse_args()

    # Resolve db path relative to project root
    if not os.path.isabs(args.db):
        args.db = os.path.join(os.getcwd(), args.db)

    # Validate required API connection config
    api_host = os.getenv("QUICKROBOT_API_HOST")
    api_port_raw = os.getenv("QUICKROBOT_API_PORT")
    if not api_host:
        print("[qr-scheduler] FATAL: QUICKROBOT_API_HOST not set. Define it in .quickrobot.env.", file=sys.stderr)
        sys.exit(1)
    if not api_port_raw:
        print(f"[qr-scheduler] FATAL: QUICKROBOT_API_PORT not set (host={api_host}).", file=sys.stderr)
        sys.exit(1)
    try:
        api_port = int(api_port_raw)
    except ValueError:
        print(f"[qr-scheduler] FATAL: QUICKROBOT_API_PORT value '{api_port_raw}' is not a valid integer.", file=sys.stderr)
        sys.exit(1)

    # Load and validate scheduler config from DB
    try:
        config = load_scheduler_config(args.db)
        update_log_level(config)
        logger.info("[qr-scheduler] Config loaded: poll=%ds, retries=%d, log=%s",
                     config["poll_interval_sec"], config["max_retries"], config["log_level"])
    except SystemExit:
        raise  # Already logged FATAL

    # Create runner instance
    poll_interval = config["poll_interval_sec"]
    max_retries = config["max_retries"]

    sched = sched_runner.SchedulerRunner(
        db_path=args.db,
        playbook_dir=args.playbook_dir,
    )
    # Override max retries on the runner
    sched.max_retries = max_retries

    # Enforce minimum poll interval
    min_poll = int(os.environ.get("QUICKROBOT_SCHEDULER_MIN_INTERVAL", "1"))
    if poll_interval < min_poll:
        logger.warning("[qr-scheduler] poll_interval=%ds < minimum %ds, clamping", poll_interval, min_poll)
        poll_interval = min_poll

    # Log rotation on startup
    try:
        from lib.lib_system_engine import get_engine_log_path as _eng_log, rotate_log_if_needed as _rot
        _rot(_eng_log("scheduler"), "scheduler")
    except Exception as exc:
        logger.warning("[qr-scheduler] Log rotation failed: %s", exc)

    # Structured startup log
    logger.info(
        "STARTUP: pid=%d db=%s api=%s:%d interval=%ds log_level=%s",
        os.getpid(), args.db, api_host, api_port, poll_interval, config["log_level"],
    )

    # Run stale detection at startup (crash recovery)
    try:
        stale_count = stale.detect_stale_tasks(args.db)
        if stale_count > 0:
            logger.info("[qr-scheduler] Startup: recovered %d stale task(s)", stale_count)
    except Exception as exc:
        logger.error("[qr-scheduler] Startup stale detection failed: %s", exc, exc_info=True)

    # Setup signal handlers
    running = [True]

    def _shutdown(signum, frame):
        logger.info("[qr-scheduler] Received signal %d, shutting down", signum)
        running[0] = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Start API health check thread (runs config sync + self-termination)
    import threading as _threading
    health_thread = _threading.Thread(
        target=_api_health_loop,
        args=(args.db,),
        daemon=True,
        name="scheduler-api-health",
    )
    health_thread.start()
    logger.info("[qr-scheduler] API health check thread started (interval=10s)")

    # Boot delay — give API time to fully initialize before scheduler starts
    # querying and scheduling health checks. Configurable via env var, default 5s.
    boot_delay = int(os.environ.get("QUICKROBOT_SCHEDULER_BOOT_DELAY", "5"))
    if boot_delay > 0:
        logger.info("[qr-scheduler] Boot delay: %ds (API initializing...)", boot_delay)
        _time_module.sleep(boot_delay)

    # Main poll loop
    logger.info("[qr-scheduler] Entering main poll loop (interval=%ds)", poll_interval)
    while running[0]:
        try:
            _process_cycle(config)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error("[qr-scheduler] Loop error: %s", exc, exc_info=True)

        # Sleep for configured interval (may be updated by config sync)
        current_interval = config.get("poll_interval_sec", poll_interval)
        _time_module.sleep(current_interval)

    logger.info("[qr-scheduler] Stopped")


if __name__ == "__main__":
    main()
