
---

## Changelog

### v0.07 (2026-06-15 — 2026-06-24)

**Staged Playbook Runner Chain (RUNNER-1)**
Replaced monolithic deploy with a 6-stage pipeline: preflight → deps → source → compile → config_svc → config_env. Each stage is independently retryable, tracked as a task under a parent job. Async scheduler picks up queued tasks in parallel across nodes. Stop/restart/bind/unbind/all config changes now go through the same chain for consistent visibility.

**Configuration Merge & ENV-Driven CLI (CONFIG-1)**
Systemd ExecStart now reads `$QR_CLI_ARGS_JOINED` from an env file — no `daemon-reload` needed for CLI changes. Config resolution flows through 6 layers: engine defaults → model params → preset template → node defaults → instance override → cluster bindings. New `/instances/<id>/config-levels` API endpoints expose each layer with merge annotations.

**SSOT Hardening**
Every hardcoded string (engine names, job types, stage names, timeouts, ports) consolidated into `lib/qr_engine_ids.py`. Zero raw string comparisons remain in runner, routes, or engine modules. System instance ID lookups use a single helper instead of four duplicated if/elif blocks.

**Playbook Directory Restructure**
Flat `playbooks/` root reorganized into `core/` (shared tasks) and `llama/` (llama.cpp-specific). All runner-compatible playbooks renamed to lowercase v2 format (`check_undeploy`, `service_start`). Seed file playbook registry cleaned from 43 → 31 entries by removing unused V1 deploy scripts.

**Unified Status Endpoint (STATUS-1)**
Each engine implements `get_instance_status()` returning standardized data: engine_data, available actions, warnings, and metadata. WebUI pages use a shared `renderInstanceActions()` function instead of per-page button logic. Detail page badges reflect real-time process health, not stale DB state.

**Herd Page — Expert Split for MoE Models**
Full configuration UI for distributed expert offloading: per-RPC mode selection (stride/block/freeform), template prefix/suffix, batch-set-all RPCs, and live CLI flag preview. DB CHECK constraint expanded from 0-100 to 0-1000. Collapsible sections for CLI flags and ENV overrides.

**Model Scan v2 + Persistent Highlights**
Scan playbook with path pre-verification and `ansible.builtin.` module prefixes. Newly discovered models highlighted in green; missing files in red; modified files (detected via disk mtime) in blue; unresolved draft cross-references in orange. Highlights persist until user clears them.

**Zombie Prevention + Health Checks**
All system subprocesses (WebUI, MCP, Scheduler) self-terminate within ~9 seconds when the API dies, using `os._exit(1)` from a health check daemon thread with 3-second retry interval. Pre-flight port+process scan on API startup catches stale processes before they block.

**Scheduler Fixes**
Background threads enable parallel compilation across nodes (previously serialized). Stale task detection fixed: UTC timezone drift eliminated, 15-second startup grace period added, and Case C detection for tasks where the scheduler crashed between DB mark and subprocess spawn. Closed DB error in `_detect_stale_tasks()` resolved.

**MCP Port Resolution + CORS Config**
Uvicorn now correctly reads `QUICKROBOT_MCP_PORT` from the environment instead of receiving `None` from CLI args. CORS origins are configurable via `QUICKROBOT_MCP_CORS_ORIGINS` env var with CLI override. Transport confirmed as traditional SSE (`sse_app()`), compatible with llama.cpp web UI.

**SSE Loading State**
RPC instances transition directly to "running" (no SSE endpoint). Server-side transition from "loading" to "running" triggers on `loaded`/`sleeping` SSE events. A `finally` block provides fallback for 404/timeout cases. Stop button now available while loading.

