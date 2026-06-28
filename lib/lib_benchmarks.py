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

"""quickrobot (v0.04) — Benchmark feature: prompts, runs, and results.

Manages benchmark prompt definitions and execution against llama.cpp server
instances. Background threads handle the actual API calls; results are stored
in SQLite with incremental output capture for crash recovery.

Module-level config:
    BENCHMARK_MAX_TIMEOUT: Max seconds for a single benchmark run (default 600).
"""

import json
import time
import threading
import urllib.request as _urq
import urllib.error as _ure
from lib.qr_engine_ids import QR_DEFAULT_LOCALHOST

# Global configurable max timeout per benchmark run (seconds)
BENCHMARK_MAX_TIMEOUT = 600


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict.

    Args:
        row: sqlite3.Row object.

    Returns:
        dict with column names as keys, or None if row is None.
    """
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Prompt CRUD
# ---------------------------------------------------------------------------

def get_prompt(db_path, prompt_id):
    """Get a single benchmark prompt by ID.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Integer primary key.

    Returns:
        Prompt dict on success, None if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM benchmark_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def create_prompt(db_path, name, content, max_tokens=20):
    """Create a benchmark prompt.

    Args:
        db_path: Path to the SQLite database.
        name: Human-readable prompt name (unique).
        content: The actual prompt text.
        max_tokens: Max tokens to generate (default 20).

    Returns:
        dict with the new prompt's data on success.
        Raises RuntimeError on duplicate name or other DB errors.
    """
    from db.sqlite import pool

    if not name or not name.strip():
        raise RuntimeError("Prompt name is required")
    if not content or not content.strip():
        raise RuntimeError("Prompt content is required")

    with pool(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO benchmark_prompts (name, content, max_tokens) VALUES (?, ?, ?)",
                (name.strip(), content.strip(), int(max_tokens)),
            )
            prompt_id = cursor.lastrowid
            row = conn.execute(
                "SELECT * FROM benchmark_prompts WHERE id = ?", (prompt_id,)
            ).fetchone()
            return _row_to_dict(row)
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                raise RuntimeError(f"PROMPT_DUPLICATE") from exc
            raise


def list_prompts(db_path):
    """List all prompts sorted by created_at descending.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        list of prompt dicts.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM benchmark_prompts ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_prompt(db_path, prompt_id, name=None, content=None, max_tokens=None):
    """Update an existing prompt.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Integer primary key.
        name: New name (optional).
        content: New content (optional).
        max_tokens: New max_tokens value (optional).

    Returns:
        Updated prompt dict on success.
        Raises RuntimeError if prompt not found.
    """
    from db.sqlite import pool

    if name is None and content is None and max_tokens is None:
        raise RuntimeError("At least one field required for update")

    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM benchmark_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"PROMPT_NOT_FOUND")

        updates = {}
        if name is not None:
            updates["name"] = name.strip()
        if content is not None:
            updates["content"] = content.strip()
        if max_tokens is not None:
            updates["max_tokens"] = int(max_tokens)

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [prompt_id]
        conn.execute(
            f"UPDATE benchmark_prompts SET {set_clause} WHERE id = ?", values
        )
        return _row_to_dict(conn.execute(
            "SELECT * FROM benchmark_prompts WHERE id = ?", (prompt_id,)
        ).fetchone())


def delete_prompt(db_path, prompt_id):
    """Delete a prompt by id.

    Args:
        db_path: Path to the SQLite database.
        prompt_id: Integer primary key.

    Returns:
        True if deleted, False if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM benchmark_prompts WHERE id = ?", (prompt_id,)
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Interlock check
# ---------------------------------------------------------------------------

def check_interlock(db_path, instance_id):
    """Check if a benchmark is currently running on this instance.

    Looks for entries where finished_at IS NULL and started_at is within
    the last 30 minutes to avoid false positives from stale entries.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer instance ID.

    Returns:
        tuple of (active_run_id_or_None, error_msg_or_None).
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        # Use running flag instead of time-based check — prevents concurrent
        # benchmarks regardless of duration (fixes 30min window issue)
        row = conn.execute(
            """SELECT run_id FROM benchmark_results
               WHERE instance_id = ? AND running = 1
               ORDER BY started_at DESC LIMIT 1""",
            (instance_id,),
        ).fetchone()
        if row:
            return (row["run_id"], None)
        return (None, None)


# ---------------------------------------------------------------------------
# Benchmark run — background thread
# ---------------------------------------------------------------------------

def get_results(db_path, instance_id, limit=20):
    """List recent benchmark results for an instance.

    Args:
        db_path: Path to the SQLite database.
        instance_id: Integer instance ID.
        limit: Max rows to return (default 20).

    Returns:
        list of result dicts with output truncated to last 500 chars.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        rows = conn.execute(
            """SELECT r.*, p.name as prompt_name
               FROM benchmark_results r
               JOIN benchmark_prompts p ON r.prompt_id = p.id
               WHERE r.instance_id = ?
               ORDER BY r.started_at DESC
               LIMIT ?""",
            (instance_id, limit),
        ).fetchall()
        results = []
        for r in rows:
            d = _row_to_dict(r)
            if d.get("output") and len(d["output"]) > 500:
                d["output_last_line"] = d["output"][-500:]
            else:
                d["output_last_line"] = d.get("output", "")
            results.append(d)
        return results


def list_all_results(db_path, limit=50):
    """List recent benchmark results across ALL instances.

    Args:
        db_path: Path to the SQLite database.
        limit: Max rows to return (default 50).

    Returns:
        list of result dicts sorted by started_at DESC.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        rows = conn.execute(
            """SELECT r.*, p.name as prompt_name
               FROM benchmark_results r
               JOIN benchmark_prompts p ON r.prompt_id = p.id
               ORDER BY r.started_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        results = []
        for r in rows:
            d = _row_to_dict(r)
            if d.get("output") and len(d["output"]) > 500:
                d["output_last_line"] = d["output"][-500:]
            else:
                d["output_last_line"] = d.get("output", "")
            results.append(d)
        return results


def get_result_detail(db_path, run_id):
    """Get full benchmark result detail.

    Args:
        db_path: Path to the SQLite database.
        run_id: UUID string of the run.

    Returns:
        Full result dict including complete output, or None if not found.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            """SELECT r.*, p.name as prompt_name, p.content as prompt_content
               FROM benchmark_results r
               JOIN benchmark_prompts p ON r.prompt_id = p.id
               WHERE r.run_id = ?""",
            (run_id,),
        ).fetchone()
        return _row_to_dict(row)


def get_progress(db_path, run_id):
    """Get current progress of a benchmark run.

    Args:
        db_path: Path to the SQLite database.
        run_id: UUID string of the run.

    Returns:
        dict with running status, output snapshot, and completion data.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM benchmark_results WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row)
        running = d["finished_at"] is None
        output_snap = ""
        if d.get("output"):
            if running:
                output_snap = d["output"][-2000:] if len(d["output"]) > 2000 else d["output"]
            else:
                output_snap = d["output"]
        return {
            "run_id": d["run_id"],
            "running": running,
            "output_snapshot": output_snap,
            "finished_at": d["finished_at"],
            "success": d["success"],
            "duration_ms": d["duration_ms"],
        }


def clear_results(db_path):
    """Delete all benchmark results.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Number of rows deleted.
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute("DELETE FROM benchmark_results")
        return cursor.rowcount


def delete_result(db_path, run_id):
    """Delete a single benchmark result by run_id.

    Args:
        db_path: Path to the SQLite database.
        run_id: The UUID run ID to delete.

    Returns:
        Number of rows deleted (0 or 1).
    """
    from db.sqlite import pool
    with pool(db_path) as conn:
        cursor = conn.execute("DELETE FROM benchmark_results WHERE run_id = ?", (run_id,))
        return cursor.rowcount


def start_benchmark(db_path, run_id, instance_id, prompt_id, override=False):
    """Start a benchmark in a background thread.

    Validates instance state, creates a result row, and spawns a daemon
    thread that calls llama.cpp /v1/completions and writes results back.

    Args:
        db_path: Path to the SQLite database.
        run_id: UUID string for this run.
        instance_id: Integer instance ID.
        prompt_id: Integer prompt ID.
        override: When True, skip MODEL_MISMATCH check (allow any loaded model).

    Returns:
        dict with run_id and prompt_name on success.
        Raises RuntimeError on validation failure or DB error.
    """
    from db.sqlite import pool
    from db.adapters.instances import get_instance as _gi
    import uuid

    # Validate instance exists and is running
    inst = _gi(db_path, instance_id)
    if inst is None:
        raise RuntimeError(f"INSTANCE_NOT_FOUND")
    if inst.get("state") != "running":
        raise RuntimeError(
            f"INSTANCE_NOT_RUNNING:{inst.get('state', 'unknown')}"
        )
    if not inst.get("port_assigned"):
        raise RuntimeError("INSTANCE_NO_PORT")

    # Validate prompt exists (query directly to avoid circular import)
    with pool(db_path) as conn:
        prow = _row_to_dict(conn.execute(
            "SELECT id, name, content, max_tokens FROM benchmark_prompts WHERE id = ?",
            (prompt_id,),
        ).fetchone())
    if prow is None:
        raise RuntimeError("PROMPT_NOT_FOUND")
    prompt_name = prow["name"]
    prompt_content = prow["content"]
    prompt_max_tokens = prow.get("max_tokens") or 20  # Default 20 for quick tests
    if prompt_content is None:
        raise RuntimeError("PROMPT_NOT_FOUND")

    # Resolve node hostname and model name
    node_hostname = inst.get("node_hostname") or inst.get("ipv4_address", QR_DEFAULT_LOCALHOST)
    port = inst["port_assigned"]
    preset_name = inst.get("preset_name")

    # Try to resolve expected model name from DB (preset config, models table)
    expected_model = _resolve_model_name(db_path, instance_id, node_hostname, port)

    # Pre-flight: check model availability on remote llama-server
    available_models = _check_model_availability(node_hostname, port)
    if not available_models:
        raise RuntimeError("MODEL_NOT_AVAILABLE")

    # Pre-flight: accept any loaded model for benchmark start.
    # model_name stored in results shows first loaded model for user verification.
    model_name = available_models[0] if available_models else "unknown"

    # Create result row with running=1 flag
    with pool(db_path) as conn:
        conn.execute(
            """INSERT INTO benchmark_results
               (run_id, instance_id, prompt_id, node_name, preset_name,
                model_name, output, running, success)
               VALUES (?, ?, ?, ?, ?, ?, '', 1, 0)""",
            (run_id, instance_id, prompt_id, node_hostname,
             preset_name, model_name),
        )

    # Spawn daemon thread for the actual benchmark run
    t = threading.Thread(
        target=_benchmark_thread,
        args=(db_path, run_id, instance_id, prompt_id, node_hostname,
              port, prompt_content, preset_name, model_name,
              prompt_max_tokens),
        daemon=True,
    )
    t.start()

    return {
        "run_id": run_id,
        "prompt_name": prompt_name,
    }


def _benchmark_thread(db_path, run_id, instance_id, prompt_id,
                      node_hostname, port, prompt_content,
                      preset_name, model_name, max_tokens=20):
    """Background thread: call llama.cpp /completion and capture result.

    Args:
        db_path: Path to the SQLite database.
        run_id: UUID of this benchmark run.
        instance_id: Instance ID for logging.
        prompt_id: Prompt ID for logging.
        node_hostname: Remote host to connect to.
        port: Port of the llama-server on the remote host.
        prompt_content: The prompt text to send.
        preset_name: Preset name for metadata (may be None).
        model_name: Model name for metadata (may be None).
        max_tokens: Max tokens to generate (default 20 for quick tests).
    """
    from db.sqlite import pool
    import time as _time
    import urllib.error as _ure

    started_at = None
    from db.sqlite import pool
    import time as _time

    started_at = None
    try:
        # Get prompt content from DB (in case it changed)
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT content FROM benchmark_prompts WHERE id = ?",
                (prompt_id,),
            ).fetchone()
            if row:
                prompt_content = row["content"]

        # Build request body — use llama.cpp /completion endpoint
        req_body = {
            "prompt": prompt_content,
            "n_predict": max_tokens,
        }
        body = json.dumps(req_body).encode("utf-8")

        url = f"http://{node_hostname}:{port}/completion"
        req = _urq.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")

        # Record start time
        started_at = _time.time()

        # Make the HTTP call with both connect and read timeouts
        resp = _urq.urlopen(req, timeout=BENCHMARK_MAX_TIMEOUT)
        raw_data = resp.read()

        # Parse response JSON
        try:
            data = json.loads(raw_data.decode("utf-8"))
        except Exception as _je:
            raise RuntimeError(f"JSON parse error: {_je}") from _je

        # Extract generated text from llama.cpp response
        output = ""
        try:
            # /completion returns "content"; /v1/completions returns "choices" or "generated_text"
            if isinstance(data, dict):
                if "content" in data:
                    output = data["content"]
                elif "generated_text" in data:
                    output = data["generated_text"]
                else:
                    for key in ("choices", "text", "response"):
                        if key in data:
                            val = data[key]
                            if isinstance(val, list) and len(val) > 0:
                                choice = val[0]
                                if isinstance(choice, dict):
                                    output = choice.get("text", "") or choice.get("message", {}).get("content", "")
                                elif isinstance(choice, str):
                                    output = choice
                            elif isinstance(val, str):
                                output = val
                            break
        except Exception:
            output = str(data) if data else ""

        # Compute total duration
        finished_at = _time.time()
        duration_ms = int((finished_at - started_at) * 1000) if started_at else 0

        # Store the FULL llama.cpp response JSON for auditability
        full_response_json = json.dumps(data) if data else ""

        # Write result to DB (running=0 on completion)
        try:
            from lib.lib_time import utcnow_str
            with pool(db_path) as conn:
                ts = utcnow_str()
                conn.execute(
                    """UPDATE benchmark_results
                       SET output = ?, response_json = ?, finished_at = ?, duration_ms = ?,
                           running = 0, success = 1
                       WHERE run_id = ?""",
                    (output, full_response_json, ts, duration_ms, run_id),
                )
        except Exception as _e:
            import sys as _sys
            print(f"BENCHMARK DB ERROR: {_e}", file=_sys.stderr, flush=True)

    except _urq.HTTPError as exc:
        # HTTP error — capture status code and body
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        output = f"HTTP {exc.code}: {err_body[:2000]}"
        _write_failure(db_path, run_id, output, started_at, _time.time())

    except _ure.URLError as exc:
        # Connection refused / timeout
        reason = str(getattr(exc, "reason", exc))
        output = f"Connection error: {reason[:2000]}"
        _write_failure(db_path, run_id, output, started_at, _time.time())

    except Exception as exc:
        # Any other error (JSON parse, threading exception, etc.)
        output = f"Error: {str(exc)[:2000]}"
        _write_failure(db_path, run_id, output, started_at, _time.time())


def _resolve_model_name(db_path, instance_id, node_hostname, port):
    """Resolve the model name for an instance.

    Tries preset config_template -> models table. Falls back to None
    (will use first available model from remote).

    Args:
        db_path: Path to the SQLite database.
        instance_id: Instance primary key.
        node_hostname: Remote hostname (for fallback fetch).
        port: Remote port.

    Returns:
        Model name string, or None if not found in DB.
    """
    from db.sqlite import pool
    try:
        with pool(db_path) as conn:
            row = conn.execute(
                "SELECT config_template FROM engine_presets WHERE id = "
                "(SELECT preset_id FROM instances WHERE id = ?)",
                (instance_id,),
            ).fetchone()
            if row and row["config_template"]:
                import json as _j
                ct = _j.loads(row["config_template"])
                # Check top-level model name fields first
                for key in ("model_name", "model"):
                    if key in ct and ct[key] and not isinstance(ct[key], dict):
                        return str(ct[key])
                # Check env section for LLAMA_ARG_MODEL (model path)
                env = ct.get("env") or {}
                if isinstance(env, dict):
                    model_path = env.get("LLAMA_ARG_MODEL") or env.get("model_path")
                    if model_path:
                        return str(model_path)
                # Check cli_opts for --model flag value
                cli = ct.get("cli_opts") or []
                if isinstance(cli, list):
                    for i, item in enumerate(cli):
                        if str(item).startswith("--model"):
                            val = cli[i + 1] if i + 1 < len(cli) else None
                            if val:
                                return str(val)
            # Try models table (first discovered model for this engine)
            engine_row = conn.execute(
                "SELECT engine_type_id FROM instances WHERE id = ?",
                (instance_id,),
            ).fetchone()
            if engine_row:
                mrow = conn.execute(
                    "SELECT name FROM engine_models "
                    "WHERE engine_type_id = ? AND discovered = 1 "
                    "ORDER BY last_modified DESC LIMIT 1",
                    (engine_row["engine_type_id"],),
                ).fetchone()
                if mrow:
                    return mrow["name"]
    except Exception:
        pass
    return None


def _check_model_availability(node_hostname, port):
    """Check which models are available on the remote llama-server.

    Args:
        node_hostname: Remote host to connect to.
        port: Port of the llama-server.

    Returns:
        List of model name strings, or empty list on error.
    """
    try:
        url = f"http://{node_hostname}:{port}/v1/models"
        req = _urq.Request(url)
        resp = _urq.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        models = []
        for item in data.get("data", []):
            name = item.get("id") or item.get("name") or item.get("model")
            if name:
                models.append(name)
        return models
    except Exception:
        return []


def _write_failure(db_path, run_id, output, started_at, finished_time):
    """Helper to write a failed benchmark result to DB.

    Args:
        db_path: Path to the SQLite database.
        run_id: UUID of the run.
        output: Error output text.
        started_at: Start timestamp (float or None).
        finished_time: End timestamp (float).
    """
    import time as _time
    from db.sqlite import pool
    from lib.lib_time import utcnow_str

    duration_ms = int((finished_time - started_at) * 1000) if started_at else 0
    ts = utcnow_str()
    with pool(db_path) as conn:
        conn.execute(
            """UPDATE benchmark_results
               SET output = ?, finished_at = ?, duration_ms = ?, running = 0,
                   success = -1
               WHERE run_id = ?""",
            (output, ts, duration_ms, run_id),
        )
