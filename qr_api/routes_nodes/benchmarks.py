"""Benchmark and prompt endpoints for quickrobot.

Functions are registered with routes in __init__.py via app.add_url_rule().
"""

import json
import logging
from flask import request, jsonify

from qr_api.lib_responses import success_single, success_list, error_response, require_json
from qr_api import _CONFIG
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST
from db.sqlite import pool as db_pool
from lib.lib_constants import DEFAULT_ANSIBLE_USER

logger = logging.getLogger(__name__)

def api_get_webui_settings():
    """Get WebUI user settings from request header or defaults.
    
    Client sends settings via X-QR-Settings header as JSON string.
    Returns stored settings or empty object if none provided.
    """
    settings_raw = request.headers.get("X-QR-Settings", "")
    try:
        settings = json.loads(settings_raw) if settings_raw else {}
    except (json.JSONDecodeError, TypeError):
        settings = {}
    return success_single(settings)


def api_set_webui_settings():
    """Store WebUI user settings in DB (server-side backup).
    
    Body: {"page": "<page_name>", "settings": {key: value, ...}}
    Returns: { status: "ok", data: { saved: true } }
    """
    from db.sqlite import pool
    body = request.get_json(silent=True) or {}
    page = str(body.get("page", "")).strip()
    settings = body.get("settings", {})
    if not page or not settings:
        return error_response("VALIDATION_ERROR", "page and settings required")

    try:
        with pool(_CONFIG["db_path"]) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO config_global (key, value) VALUES ('webui_settings_" + str(page) + "', ?)",
                (json.dumps(settings),)
            )
            conn.commit()
    except Exception as _e:
        logger.debug("webui settings save failed (page=%s): %s", page, _e)
    return success_single({"saved": True})


# ---------------------------------------------------------------------------
# Benchmarks — prompt CRUD + run management
# ---------------------------------------------------------------------------

def api_list_prompts():
    """List all benchmark prompts sorted by created_at desc."""
    from lib.lib_benchmarks import list_prompts as _lp
    items = _lp(_CONFIG["db_path"])
    return success_list(items, total=len(items))


def api_get_prompt(prompt_id):
    """Get a single benchmark prompt by ID."""
    from lib.lib_benchmarks import get_prompt as _gp
    prompt = _gp(_CONFIG["db_path"], prompt_id)
    if not prompt:
        return error_response("NOT_FOUND", f"Prompt #{prompt_id} not found")
    return success_single(prompt)


def api_create_prompt():
    """Create a new benchmark prompt."""
    from lib.lib_benchmarks import create_prompt as _cp
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    max_tokens = body.get("max_tokens") or 20
    try:
        prompt = _cp(_CONFIG["db_path"], name, content, max_tokens=max_tokens)
        return success_single(prompt)
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "PROMPT_DUPLICATE":
            return error_response("DUPLICATE_NAME", f"Prompt '{name}' already exists")
        return error_response("CREATE_FAILED", msg)


def api_update_prompt(prompt_id):
    """Update an existing benchmark prompt."""
    from lib.lib_benchmarks import update_prompt as _up
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    name = body.get("name")
    content = body.get("content")
    max_tokens = body.get("max_tokens")
    try:
        prompt = _up(_CONFIG["db_path"], prompt_id, name=name, content=content, max_tokens=max_tokens)
        return success_single(prompt)
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "PROMPT_NOT_FOUND":
            return error_response("RESOURCE_NOT_FOUND", f"Prompt {prompt_id} not found")
        return error_response("UPDATE_FAILED", msg)


def api_delete_prompt(prompt_id):
    """Delete a benchmark prompt."""
    from lib.lib_benchmarks import delete_prompt as _dp
    try:
        deleted = _dp(_CONFIG["db_path"], prompt_id)
        if not deleted:
            return error_response("RESOURCE_NOT_FOUND", f"Prompt {prompt_id} not found")
        return success_single({"deleted": True})
    except RuntimeError as exc:
        return error_response("DELETE_FAILED", str(exc))


def api_start_benchmark():
    """Start a benchmark run on a llama.cpp instance.

    Fire-and-forget: returns immediately with a run_id.
    The actual benchmark runs in a background thread.
    Interlock: only one benchmark per instance at a time.
    Override flag skips MODEL_MISMATCH check.
    """
    import uuid as _uuid
    from lib.lib_benchmarks import check_interlock as _ci, start_benchmark as _sb
    body, is_err = require_json()
    if is_err:
        return error_response("VALIDATION_ERROR", body["_error"])

    instance_id = body.get("instance_id")
    prompt_id = body.get("prompt_id")
    override = body.get("override", False)

    if instance_id is None or prompt_id is None:
        return error_response("VALIDATION_ERROR", "instance_id and prompt_id are required")

    try:
        instance_id = int(instance_id)
        prompt_id = int(prompt_id)
    except (ValueError, TypeError):
        return error_response("VALIDATION_ERROR", "instance_id and prompt_id must be integers")

    # Interlock check
    active_run_id, interlock_err = _ci(_CONFIG["db_path"], instance_id)
    if active_run_id and not override:
        return error_response(
            "BENCHMARK_RUNNING",
            f"Benchmark already running for instance {instance_id}. "
            "Use override=true to force.",
            status_code=409,
            detail={"active_run_id": active_run_id},
        )

    run_id = _uuid.uuid4().hex[:12]
    try:
        result = _sb(_CONFIG["db_path"], run_id, instance_id, prompt_id, override=override)
        return success_single(result)
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "INSTANCE_NOT_FOUND":
            return error_response("RESOURCE_NOT_FOUND", f"Instance {instance_id} not found")
        elif msg.startswith("INSTANCE_NOT_RUNNING:"):
            state = msg.split(":", 1)[1]
            return error_response(
                "INSTANCE_NOT_RUNNING",
                f"Instance {instance_id} is not running (state={state})",
                status_code=409,
            )
        elif msg == "INSTANCE_NO_PORT":
            return error_response(
                "INSTANCE_NO_PORT",
                f"Instance {instance_id} has no port assigned",
                status_code=409,
            )
        elif msg == "PROMPT_NOT_FOUND":
            return error_response("RESOURCE_NOT_FOUND", f"Prompt {prompt_id} not found")
        elif msg.startswith("MODEL_MISMATCH"):
            return error_response(
                "MODEL_MISMATCH",
                msg,
                status_code=409,
                detail={"override_hint": "Set override=true in request body to skip model verification"},
            )
        return error_response("BENCHMARK_START_FAILED", msg)


def api_list_results():
        """List benchmark results for an instance (or all instances).

        Query params:
        - instance_id: Integer instance ID, or "all" for all instances.
        - limit: Max rows to return (default 50).

        When instance_id is omitted or "all", returns results across all
        instances sorted by started_at DESC — useful for group execution
        verification and multi-node benchmark management.
        """
        from lib.lib_benchmarks import get_results as _gr, list_all_results as _lar
        instance_id = request.args.get("instance_id")
        limit = int(request.args.get("limit", 50))
        if instance_id is None or instance_id.strip() == "" or instance_id.lower() == "all":
            items = _lar(_CONFIG["db_path"], limit=limit)
            return success_list(items, total=len(items))
        try:
            instance_id_int = int(instance_id)
        except (ValueError, TypeError):
            return error_response("VALIDATION_ERROR", "instance_id must be an integer or 'all'")
        items = _gr(_CONFIG["db_path"], instance_id_int, limit=limit)
        return success_list(items, total=len(items))


def api_get_result_detail(run_id):
    """Get full benchmark result detail including complete output."""
    from lib.lib_benchmarks import get_result_detail as _grd
    result = _grd(_CONFIG["db_path"], run_id)
    if result is None:
        return error_response("RESOURCE_NOT_FOUND", f"Run {run_id} not found")
    return success_single(result)


def api_get_progress(run_id):
    """Get current progress of a benchmark run (for polling from WebUI)."""
    from lib.lib_benchmarks import get_progress as _gp
    result = _gp(_CONFIG["db_path"], run_id)
    if result is None:
        return error_response("RESOURCE_NOT_FOUND", f"Run {run_id} not found")
    return success_single(result)


def api_clear_results():
    """Clear all benchmark results."""
    from lib.lib_benchmarks import clear_results as _cr
    count = _cr(_CONFIG["db_path"])
    return success_single({"deleted": count})


def api_delete_benchmark_run(run_id):
    """Delete a single benchmark result by run_id (for stale stuck runs)."""
    from lib.lib_benchmarks import delete_result as _dr
    count = _dr(_CONFIG["db_path"], run_id)
    if count == 0:
        return error_response("RESOURCE_NOT_FOUND", f"Benchmark run {run_id} not found")
    return success_single({"deleted": run_id})


# ---------------------------------------------------------------------------
# Phase 2: System-engine management endpoints
