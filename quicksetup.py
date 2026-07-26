#!/usr/bin/env python3
"""QUICKSETUP — Multi-step auto-deploy for fresh quickrobot installs.

Triggered by QUICKROBOT_QUICKSETUP=true in .quickrobot.env after seed import.
Runs as a background thread so the API remains responsive during setup.

Multi-step flow:
  1. Pre-flight: verify presets 20-30 exist in seed (SSOT)
  2. Step A: POST /api/v1/instances — create router instance (preset 20)
  3. Wait A: poll until "running" (30min timeout, ESC=cancel)
  4. Step B: PUT /api/v1/instances/{id}/config — reconfigure to download preset (21)
  5. Wait B: poll until "running" again (30min timeout, ESC=cancel)
  6. Step C: health probe + full console report

All console output uses [qr] prefix for consistency.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from db.sqlite import pool as _pool
from lib.qr_engine_ids import QR_ENGINE_LLAMA_SERVER
from qr_api import _project_root

# ── Constants ─────────────────────────────────────────────────────────────────
PRESET_ROUTER_ID = 20         # QuickSetup-Router (fast start, no model)
PRESET_DOWNLOAD_ID = 21       # QuickSetup-Download (model download trigger)
STEP_TIMEOUT_SEC = 30 * 60    # 30 minutes per step
POLL_INTERVAL_SEC = 5         # Poll interval for status checks
CANCEL_FLAG = threading.Event()

# ── Env config — loaded from .quickrobot.env at startup ────────────────────────
_QR_ENV = {}


def _load_env():
    """Read required keys from .quickrobot.env. Exits on missing/empty values."""
    env_path = os.path.join(_project_root, ".quickrobot.env")
    if not os.path.isfile(env_path):
        print("[qr] QUICKSETUP FATAL: .quickrobot.env not found at '{}'".format(env_path))
        sys.exit(1)

    cfg = {}
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()

    required_keys = {
        "QUICKROBOT_API_HOST":   "API bind address (e.g. 127.0.0.1)",
        "QUICKROBOT_API_PORT":   "API port number",
        "QUICKROBOT_API_KEY":    "API bearer token for auth",
    }

    for key, desc in required_keys.items():
        val = cfg.get(key)
        if not val:
            print("[qr] QUICKSETUP FATAL: {} is missing or empty in .quickrobot.env".format(key))
            print("     Required for: {}".format(desc))
            sys.exit(1)
        _QR_ENV[key] = val


# Populate _QR_ENV at module load time — fail fast if env is incomplete
_load_env()


# ── Cancellation support ──────────────────────────────────────────────────────

class QuickSetupCancelled(Exception):
    """Raised when user cancels quicksetup with ESC."""
    pass


def _cancel_handler(sig, frame):
    """Handle SIGINT (Ctrl+C) or keyboard interrupt."""
    print("[qr] QUICKSETUP: cancellation requested")
    CANCEL_FLAG.set()


try:
    signal.signal(signal.SIGINT, _cancel_handler)
except (OSError, ValueError):
    pass  # signal only works in main thread — skip in deferred threads


def _check_cancel():
    """Raise QuickSetupCancelled if ESC was pressed."""
    if CANCEL_FLAG.is_set():
        raise QuickSetupCancelled("Quicksetup cancelled by user")


# ── Console utilities ─────────────────────────────────────────────────────────

def _print_step(step_label, message):
    """Print a step marker and message."""
    print(f"[qr] QUICKSETUP [{step_label}]: {message}")


def _timer_display(start_time, max_wait, label=""):
    """Display running timer with elapsed time and remaining."""
    elapsed = int(time.time() - start_time)
    remaining = max(0, int(max_wait - elapsed))
    mins, secs = divmod(remaining, 60)
    print(f"[qr] QUICKSETUP: {label} — {elapsed}s elapsed, ~{mins}m {secs}s remaining...", end="\r", flush=True)


def _format_duration(seconds):
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(int(seconds), 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


# ── API helper ────────────────────────────────────────────────────────────────

def _api_call(method, path, data=None, timeout=10, retries=15, retry_delay=2):
    """Make an API call with retries for when the API isn't ready yet.

    Args:
        method: HTTP method (GET, POST, PUT, etc.)
        path: API path (e.g., "/api/v1/instances")
        data: JSON body dict (for POST/PUT)
        timeout: Request timeout in seconds
        retries: Number of retry attempts (default 15)
        retry_delay: Seconds between retries (default 2)

    Returns:
        Parsed JSON response dict, or None on failure.
    """
    api_host = _QR_ENV["QUICKROBOT_API_HOST"]
    api_port = _QR_ENV["QUICKROBOT_API_PORT"]
    url = "http://{}:{}{}".format(api_host, api_port, path)

    # Build headers with API key from .quickrobot.env
    headers = {"Content-Type": "application/json"} if data else {}
    headers["X-API-Key"] = _QR_ENV["QUICKROBOT_API_KEY"]

    for attempt in range(1, retries + 1):
        try:
            body = json.dumps(data).encode() if data else None
            req = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if attempt < retries:
                print(f"[qr] QUICKSETUP API not ready (attempt {attempt}/{retries}): {e}")
                time.sleep(retry_delay)
            else:
                print(f"[qr] QUICKSETUP API error after {retries} attempts: {e}")
                return None


# ── Pre-flight: verify presets exist in DB ────────────────────────────────────

def _verify_presets_in_db(db_path, preset_ids):
    """Verify that all preset IDs exist in the database.

    Args:
        db_path: Path to SQLite database
        preset_ids: List of preset IDs to verify

    Returns:
        dict mapping preset_id -> (name, exists) for each requested ID
    """
    results = {}
    try:
        with _pool(db_path) as conn:
            placeholders = ",".join("?" * len(preset_ids))
            rows = conn.execute(
                f"SELECT id, name FROM engine_presets WHERE id IN ({placeholders})",
                preset_ids,
            ).fetchall()
            for row in rows:
                results[row[0]] = (row[1], True)
    except Exception as e:
        print(f"[qr] QUICKSETUP preset check failed: {e}")

    # Fill in missing presets as not found
    for pid in preset_ids:
        if pid not in results:
            results[pid] = (None, False)

    return results


# ── Wait loop with timer display ─────────────────────────────────────────────

def _wait_for_running(inst_id, db_path, step_label, max_wait=STEP_TIMEOUT_SEC):
    """Poll instance status until it reaches 'running' state.

    Shows running timer and supports cancellation via ESC.

    Args:
        inst_id: Instance ID to poll
        db_path: Path to SQLite database
        step_label: Step label for console output
        max_wait: Maximum wait time in seconds (default 30 min)

    Returns:
        True if instance reached running state, False on timeout/cancel.
    """
    start = time.time()
    print(f"[qr] QUICKSETUP [{step_label}]: waiting for instance {inst_id} to become 'running'...")

    while time.time() - start < max_wait:
        _check_cancel()  # Check for ESC cancel

        # Poll API for current status
        resp = _api_call("GET", f"/api/v1/instances/{inst_id}")
        if resp and resp.get("data"):
            state = resp["data"].get("state", "unknown")
            if state == "running":
                print(f"\n[qr] QUICKSETUP [{step_label}]: instance {inst_id} is now 'running'")
                return True
            # Print current state (non-running)
            print(f"[qr] QUICKSETUP: instance {inst_id} state={state}", end="\r", flush=True)
        else:
            print(f"[qr] QUICKSETUP [{step_label}]: no status response yet...", end="\r", flush=True)

        # Show running timer
        _timer_display(start, max_wait, step_label)
        time.sleep(POLL_INTERVAL_SEC)

    # Timeout reached
    elapsed = int(time.time() - start)
    print(f"\n[qr] QUICKSETUP [{step_label}]: timeout after {_format_duration(elapsed)}")
    return False


# ── Multi-step execution ─────────────────────────────────────────────────────

def _run_step_create_instance(db_path):
    """Step A: Create router instance via API (preset 20).

    Returns dict with instance_id, auth_token, port_assigned or None on failure.
    """
    _print_step("STEP-A", "Creating router instance (preset 20)...")

    payload = {
        "name": "QuickSetup",
        "engine_type_id": QR_ENGINE_LLAMA_SERVER,
        "node_id": 1,                    # localhost
        "preset_id": PRESET_ROUTER_ID,   # QuickSetup-Router (no model, fast start)
        "config_override": {
            "env": {"LLAMA_ARG_HOST": QR_DEFAULT_LOCALHOST},  # 127.0.0.1 for localhost
        },
        "start_after_deploy": True,      # Auto-deploy via staged chain
    }

    resp = _api_call("POST", "/api/v1/instances", payload)
    if not resp or not resp.get("data"):
        print(f"[qr] QUICKSETUP [STEP-A]: API create failed — {resp.get('_error', resp)}")
        return None

    data = resp["data"]
    inst_id = data.get("id")
    token = data.get("auth_token", "N/A")
    port = data.get("port_assigned", "N/A")

    print(f"[qr] QUICKSETUP [STEP-A]: created instance {inst_id}")
    print(f"[qr] QUICKSETUP [STEP-A]: auth_token={token}")
    print(f"[qr] QUICKSETUP [STEP-A]: port={port}")

    return {"inst_id": inst_id, "token": token, "port": port}


def _run_step_reconfigure(db_path, inst_id, model_hf):
    """Step B: Reconfigure instance to use download preset (preset 21).

    Args:
        db_path: Path to SQLite database
        inst_id: Instance ID to reconfigure
        model_hf: HuggingFace model string (e.g., "google/gemma-3-1b-it-GGUF:Q4_K_M")

    Returns:
        True if reconfigure succeeded, False otherwise.
    """
    _print_step("STEP-B", f"Reconfiguring instance {inst_id} with download preset (21)...")

    # HF_HUB_CACHE base path — must come from .quickrobot.env, no fallback
    base_path = _QR_ENV.get("QUICKROBOT_API_MODEL_BASE_PATH", "")
    if not base_path:
        print("[qr] QUICKSETUP [STEP-B]: FATAL — QUICKROBOT_API_MODEL_BASE_PATH not set in .quickrobot.env")
        print("     Set QUICKROBOT_API_MODEL_BASE_PATH=/your/writeable/path in .quickrobot.env, then try again")
        return False

    hf_cache_path = os.path.join(base_path, "hf_cache")
    if not os.path.isdir(base_path):
        try:
            os.makedirs(base_path, exist_ok=True)
        except PermissionError:
            print("[qr] QUICKSETUP [STEP-B]: FATAL — cannot create base path: {}".format(base_path))
            print("     Ensure the user running quickrobot can write to parent directory")
            return False

    if not os.access(base_path, os.W_OK):
        print("[qr] QUICKSETUP [STEP-B]: FATAL — path not writable: {}".format(base_path))
        return False

    # Build cli_flags for model download
    cli_flags = ["--jinja"]
    if model_hf:
        # Use --hf-repo long form for reliable model resolution.
        # Expected format: user/repo:quant (e.g., unsloth/Qwen3.6-35B-A3B-MTP-GGUF:Q8_0)
        cli_flags.append("--hf-repo {}".format(model_hf))

    payload = {
        "preset_id": PRESET_DOWNLOAD_ID,
        "cli_flags": cli_flags,
        "env": {
            "HF_HUB_CACHE": hf_cache_path,
        },
    }

    # Use PUT /instances/<id> (api_update_instance) which properly handles preset_id.
    # The fast-path for preset-only changes auto-triggers the reconfigure chain.
    resp = _api_call("PUT", f"/api/v1/instances/{inst_id}", payload)
    if not resp or not resp.get("data"):
        print(f"[qr] QUICKSETUP [STEP-B]: preset change failed — {resp.get('_error', resp)}")
        return False

    print(f"[qr] QUICKSETUP [STEP-B]: switched to download preset, reconfigure triggered")
    return True


def _run_step_health_probe(port):
    """Step C: Health probe to verify model is loaded.

    Polls /v1/models endpoint for up to 5 minutes.

    Returns:
        True if models found, False otherwise.
    """
    _print_step("STEP-C", f"Health probing http://127.0.0.1:{port}/v1/models...")

    server_url = f"http://127.0.0.1:{port}/v1/models"
    max_wait = 5 * 60  # 5 minutes for model download
    start = time.time()

    while time.time() - start < max_wait:
        _check_cancel()

        try:
            req = urllib.request.Request(server_url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                models = data.get("data", [])
                if models:
                    print(f"\n[qr] QUICKSETUP [STEP-C]: model loaded — {len(models)} model(s)")
                    return True
        except Exception:
            pass

        elapsed = int(time.time() - start)
        remaining = max(0, int(max_wait - elapsed))
        mins, secs = divmod(remaining, 60)
        print(f"[qr] QUICKSETUP [STEP-C]: waiting for model... {elapsed}s elapsed, ~{mins}m remaining", end="\r", flush=True)
        time.sleep(POLL_INTERVAL_SEC)

    print("\n[qr] QUICKSETUP [STEP-C]: timeout — model still loading")
    return False


# ── Main execution ────────────────────────────────────────────────────────────

def run_quicksetup(db_path):
    """Execute the full multi-step quicksetup sequence.

    Returns:
        0 on success, 1 on failure, 2 on cancellation.
    """
    start_time = time.time()
    _print_step("START", "Multi-step quicksetup initiated")

    # ── Pre-flight: verify presets in DB ─────────────────────────────
    _print_step("PRE-FLIGHT", "Verifying presets 20-30 in database...")
    preset_ids = [PRESET_ROUTER_ID, PRESET_DOWNLOAD_ID]  # IDs 20, 21 only
    presets_verified = _verify_presets_in_db(db_path, preset_ids)

    found_count = sum(1 for _, (_, exists) in presets_verified.items() if exists)
    print(f"[qr] QUICKSETUP [PRE-FLIGHT]: presets verified: {found_count}/{len(preset_ids)}")

    # Verify critical presets exist
    router_exists = presets_verified.get(PRESET_ROUTER_ID, (None, False))[1]
    download_exists = presets_verified.get(PRESET_DOWNLOAD_ID, (None, False))[1]

    if not router_exists:
        print(f"[qr] QUICKSETUP [PRE-FLIGHT]: preset {PRESET_ROUTER_ID} (QuickSetup-Router) NOT FOUND")
        return 1
    if not download_exists:
        print(f"[qr] QUICKSETUP [PRE-FLIGHT]: preset {PRESET_DOWNLOAD_ID} (QuickSetup-Download) NOT FOUND")
        return 1

    print("[qr] QUICKSETUP [PRE-FLIGHT]: all required presets OK")

    # ── Step A: Create router instance ───────────────────────────────
    step_a_start = time.time()
    create_result = _run_step_create_instance(db_path)
    if not create_result:
        print("[qr] QUICKSETUP [STEP-A]: FAILED")
        return 1

    inst_id = create_result["inst_id"]
    token = create_result["token"]
    port = create_result["port"]

    _check_cancel()  # Check after step A

    # ── Wait A: Poll until running ───────────────────────────────────
    if not _wait_for_running(inst_id, db_path, "WAIT-A"):
        return 1 if CANCEL_FLAG.is_set() else 0

    _check_cancel()  # Check after wait A

    # ── Step B: Reconfigure to download preset ───────────────────────
    model_hf = os.environ.get("QUICKROBOT_QUICKSETUP_MODEL", "").strip()
    if model_hf:
        _print_step("STEP-B", f"Model from env: {model_hf}")

    if not _run_step_reconfigure(db_path, inst_id, model_hf):
        return 1

    # api_update_instance (PUT /instances/<id>) already triggers reconfigure chain
    # when preset_id changes (preset_only fast path). No separate restart needed.
    _check_cancel()  # Check after step B

    # ── Wait B: Poll until running again ─────────────────────────────
    if not _wait_for_running(inst_id, db_path, "WAIT-B"):
        return 1 if CANCEL_FLAG.is_set() else 0

    _check_cancel()  # Check after wait B

    # ── Step C: Health probe ─────────────────────────────────────────
    model_loaded = _run_step_health_probe(port)
    _check_cancel()  # Final cancel check

    # ── Mark as done (idempotency guard for restarts) ─────────────────
    try:
        with _pool(db_path) as conn:
            conn.execute(
                "UPDATE instances SET quicksetup_done=1 WHERE id=?",
                (inst_id,),
            )
        print("[qr] QUICKSETUP: marked instance {} as done".format(inst_id))
    except Exception as _e:
        # Non-critical — next restart will still skip if presets are missing
        print(f"[qr] QUICKSETUP: failed to set quicksetup_done ({_e})")

    # ── Console report ───────────────────────────────────────────────
    total_time = time.time() - start_time
    print()
    print("=" * 60)
    print("[qr] QUICKSETUP REPORT")
    print("=" * 60)
    print(f"  Instance ID:        {inst_id}")
    print(f"  Server URL:         http://127.0.0.1:{port}/")
    print(f"  Auth Token:         {token}")
    print(f"  Model:              {model_hf or 'Router mode (no model)'}")
    print(f"  Total time:         {_format_duration(total_time)}")
    if model_loaded:
        print(f"  Model loaded:       YES")
    else:
        print(f"  Model loaded:       TIMEOUT (still downloading)")
    print("=" * 60)
    print()

    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Only run when called directly (not imported via startup pipeline)
    if "QUICKROBOT_QUICKSETUP" not in os.environ:
        print("[qr] QUICKSETUP: QUICKROBOT_QUICKSETUP not set — skipping")
        sys.exit(0)

    flag = os.environ.get("QUICKROBOT_QUICKSETUP", "").strip().lower()
    if flag != "true":
        print(f"[qr] QUICKSETUP: QUICKROBOT_QUICKSETUP={flag} — skipping")
        sys.exit(0)

    # Determine DB path
    db_path = os.environ.get("QUICKROBOT_DB_PATH", "")
    if not db_path:
        candidate = os.path.join(_project_root, "data/quickrobot.db")
        if os.path.isfile(candidate):
            db_path = candidate
        else:
            print("[qr] QUICKSETUP: DB not found")
            sys.exit(1)

    if not os.path.isfile(db_path):
        print("[qr] QUICKSETUP: DB file does not exist yet")
        sys.exit(1)

    # Check if already done (skip if quicksetup_done=1 on any instance)
    try:
        with _pool(db_path) as conn:
            done = conn.execute("SELECT COUNT(*) FROM instances WHERE quicksetup_done=1").fetchone()[0]
            if done:
                print(f"[qr] QUICKSETUP: already done ({done} instance(s))")
                sys.exit(0)
    except Exception as e:
        print(f"[qr] QUICKSETUP: DB check skipped (column may not exist): {e}")

    # Run and exit
    result = run_quicksetup(db_path)
    sys.exit(result if result is not None else 1)
