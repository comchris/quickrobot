
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

---

### v0.08 (2026-07-03 — 2026-07-10)

**Unified Logging System**
Consolidated 5 old tables (ansible_actions, jobs, tasks, qr_actions, instance_logs) into a single `log_entries` table with parent-child hierarchy (`parent_id IS NULL` = job header, `parent_id IS NOT NULL` = task sub-row). 14 write paths updated across the codebase. New API endpoints: `GET /api/v1/log_entries` (unified query with filters) and `POST /api/v1/log_entries/cleanup` (bulk delete). WebUI logs page rewritten in JS with expand/collapse, lazy task loading, color-coded stage progress bars, composite status aggregation, and auto-refresh. Log preservation on instance/host delete via `PRAGMA foreign_keys = OFF`.

**API Endpoint Consolidation (EP-CONSOLIDATE)**
Reduced total endpoints by ~14% through four consolidation phases: P1 merged 3 status/health endpoints into unified `GET /instances/<id>/status` with `?remote=true` health probe; P2 unified 3 APT operations into single `POST /nodes/<id>/apt`; P3+P4 consolidated 16 config/split endpoints into `GET /instances/<id>/config` and `PUT /instances/<id>/config`. All ~16 old endpoints preserved as thin wrappers for backward compatibility. Duplicate route registration bug fixed.

**Scheduler Modular Rewrite (SCHEDULER-REDESIGN)**
Split monolithic 960-line `__main__.py` into four focused modules: `stale.py` (orphaned/stuck job detection), `health.py` (periodic health check cycle with interval gate), `runner.py` (task execution wrapper around PlaybookRunner), and thin `__main__.py` entry point (~450 lines). Each operation uses explicit per-connection scope (no nested `with pool()` blocks). Health check interval gate fixed: closed-connection bug and SQLite WAL+DEFERRED isolation snapshot issue both resolved via pre-fetch + cache pattern. Config sync loop reads engine_configs every cycle with sanity checks.

**Background Scheduled Health Checks (BG-HEALTH-1)**
Scheduler now runs periodic health checks based on state-group intervals: running/deploying at 60s, error states at 20s (fast recovery), stopped at 600s. Configurable via `engine_configs` table (unified with all other engines). Per-instance enable/disable toggle. Auto-recovery: error → running on successful health check. New API endpoint `POST /instances/health-check-all` for on-demand checks. Health check duration reduced from 27-30s to ~4s by eliminating redundant `engine.query_status()` call — service state now parsed directly from ansible playbook output JSON.

**Herd Page Overhaul**
All herd settings (CLI flags, ENV overrides, GPU override) now auto-save with debounced promise chaining — no manual Save buttons needed. GPU override storage key renamed from `LLAMA_ARG_DEVICE` to `qr_cluster_gpu_override` to separate cluster-level device spec from regular llama-server env vars. Device flags display updates after save without page reload. Null guards added to all 8 herd save functions. Layout reordered: GPU Override → Device Flags → Split Mode → Split Value. Inactive-host instances filtered from herd view. Preset dropdown uses ID prefix format matching instance list page.

**Health Check State Machine Refinements**
Multiple rounds of fixes for health check state transitions: (A) Successful health checks now always set `state='running'` on non-error instances; (B) `health_check` stage added to `_QR_STAGE_STATES` mapping so it shows "running" not "configuring"; (C) Conservative correction only — health checks skip instances in transition states (deploying/configuring), only write when discrepancy found, preserve unknown states; (D) Stopped instance guard — intentional stops preserved even when health check finds service inactive; (E) Failed child task detection prevents `_finalize_job()` from overwriting error states with success.

**Playbook Fallback Removal**
Removed ALL `default()` filters from data-forwarding paths across 9 playbooks and 4 templates. Host resolution now requires explicit `inventory_host` — Ansible fails with "undefined variable" if missing instead of silently falling back to hardcoded "localhost". Build path, env template, and health check defaults also converted to strict mode. Infrastructure defaults (engine type display names, pkill patterns) retained.

**WebUI Improvements**
Action button styling unified across Models and Presets pages (Edit → btn-secondary, Clone → btn-success, Delete → btn-danger, +Preset → btn-secondary). "+Preset" button added to every model row. Task Log table gains numeric sorting for Instance ID and Duration columns. Instances page: state badge vertically centered, Age column replaced with relative time display, job count links with clickable log filters, override badge moved from Jobs to State column. Preset edit page: merge chain visualization shows L2 Model Params (temp, top_p, top_k), model dropdown sorted alphabetically with quantization info. Log page clear dropdown changed from day-based to minute-based intervals (10min/1h/10h/24h/7 days).

**Configuration Merge Chain Optimizations**
Eliminated 2x DB re-query in `build_config_layers()` by returning source chain objects instead of rebuilding identical `ConfigLevel` objects. SSOT constant completeness: added `QR_JOB_HEALTH_CHECK`, `QR_TIMEOUT_HEALTH_CHECK`, `QR_STAGE_UNDEPLOY`, `QR_STAGE_VERIFY`. Timeout defaults aligned to named constants (`QR_TIMEOUT_COMPILE=1800`, `QR_TIMEOUT_DEFAULT=300`, `QR_TIMEOUT_JOB=7200`).

**Instance State Visibility**
Deploy/rebuild state now visible immediately on instance list page — `PlaybookRunner.chain()` updates instance state at job creation time via `STAGE_STATE_MAP` lookup, not waiting for scheduler poll cycle. Stop already-stopped service → success (idempotent). Extra vars pre-computed at job creation time to avoid redundant DB re-lookups.

**System Engine Improvements**
System instances now respect `.env` AUTOSTART flags (WebUI: `QUICKROBOT_WEBUI_AUTOSTART`, MCP: `QUICKROBOT_MCP_AUTOSTART`). Restart responses include `old_pid`, `new_pid`, and `pid_changed` boolean for audit trails. Scheduler package exports `SchedulerEngine(BaseEngine)` for proper engine discovery.

**Housekeeping & Code Quality**
Migrations consolidated — only `008_base.sql` remains (old files moved to OLD_ignore). All `__pycache__/` directories removed (14 across lib/, engine/, db/, qr_api/). Seed file fixed: renamed stale v009 → v008, removed 301 runtime log entries from static seed data. Dead code cleanup: removed duplicate function definitions in routes_nodes.py, dead `QR_ENV_MCP_ALLOWED_HOSTS` constant, simplified `_resolve_engine_playbook_id()`. SSOT compliance: 7 hardcoded `"llama_server"` strings in MCP tools replaced with `QR_ENGINE_LLAMA_SERVER_NAME` constant.

---

### v0.09 (2026-07-13 — 2026-07-17)

**Route Split**
Split monolithic `qr_api/__init__.py` (~9,600 lines) into two feature-group subdirectories: `routes_instances/` (7 files: status, deploy, config, RPC cluster, jobs/tasks, health, misc) and `routes_nodes/` (13 files: lifecycle, status, config, APT, engine management, presets, models, benchmarks, system/mcp/webui mgmt, playbooks, misc). All 176 route registrations preserved. Post-split import chain fixed across 17 files — missing `json`, `logging`, `_project_root`, and cross-file utility imports were the primary failure mode.

**Expert Split Mode S (Staggered)**
New expert distribution mode for MoE models: each RPC generates 3 `-ot` flags (one per FFN sub-layer: up/gate/down) with global stride-3 round-robin across the full expert pool. Enables ~3x per-node VRAM reduction at cost of increased inter-RPC communication. Added `extra_ot_flags[]` config for manual `-ot` entries, dedicated `PATCH /instances/<id>/expert-split` atomic endpoint, and WebUI textarea for extra flag editing. Mode S index bug fixed (was using local per-RPC allocation instead of global round-robin).

**Port Resolution Fix + Seed Base Split**
llama_server `base_port` added to `engine_configs` table (previously only in engine_types capabilities JSON), fixing stale port values in env files for new instances. Engine configs (66 rows) moved from seed file into base migration `010_base.sql`, separating infrastructure defaults from user content (presets, models). Seed file reduced from 321 to 292 rows; fresh DB now produces clean state with localhost auto-provisioned and zero remote nodes.

**Model Scan v3 + Multipart Model Consolidation**
Expanded mmproj detection: case-insensitive grep, 4 additional glob fallback patterns, quantization-strip sed widens for more variants. Multipart GGUF models now correctly grouped via `total_shards` parsed from filename `of-N` suffix (was wrong when scan catches only a subset of shards). Shard consolidation removes fragment duplicates — 31 fragments merged across live scans.

**WebUI Fixes**
Preset/model edit pages aligned to consistent `<id>/edit` URL pattern. Previous/next navigation simplified to adjacent ID (removed buggy "used-ID" smart nav). CSS grid layout replaces flexbox for button alignment. Stale job detection extended to catch stuck queued children (parent marked error when any child stuck, not just all-done). RPC binding display fixed: stopped/error servers no longer show "unbound" despite intact DB bindings.

**Scheduler & Runtime Hardening**
Stale task detection extended: Phase 3 now catches both "all children done" AND "stuck queued children" patterns. Instance state reset added to Phase 3 for consistency. SQL parameterization fixed across all scheduler queries (was using f-string interpolation). `SO_REUSEADDR` added to port check for restart resilience (prevents TIME_WAIT conflicts).

---

### v0.10 (2026-07-17 — 2026-07-22)

**Engine Code Split — Shared Libs**
Extracted ~405 lines of duplicated code from 4 engine `__init__.py` files into 8 shared libraries: `lib_engine_health.py` (remote service check), `lib_engine_status.py` (instance status builder), `lib_engine_actions.py` (action map lookup), `lib_engine_states.py` (state machine builder), `lib_engine_config.py` (config CRUD), `lib_engine_command.py` (execute/forward), `lib_engine_resources.py` (resource listing), `lib_engine_status_query.py` (systemd status query). Engines now 280-360 lines each (down from 430-590). Remaining bulk: inline CAPABILITIES dict, custom query_status(), list_resources().

**Endpoint Consolidation (EP-CONSOLIDATE — v0.10)**
Completed remaining consolidation phases: P3 merged 2 health/status endpoints into unified `GET /instances/<id>/status` (removed `/health` and `/query-status`). P4 consolidated 5 split/expert endpoints into single `PUT /instances/<id>/config` (removed 5 thin wrappers). Total API endpoints reduced by ~14% from original monolithic count. All removed endpoints return 404; kept endpoints verified working via WebUI/MCP callers.

**Single-Token Auth Model**
Replaced 3-token model (API_KEY, WEBUI_TOKEN, MCP_TOKEN) with single `QUICKROBOT_API_KEY` gatekeeper. Removed unused token env vars from `.quickrobot.env`. Auth middleware accepts only this key in prod mode. Per-instance llama_server auth tokens retained for model-serving API authentication.

**Waitress WSGI Production Server**
Swapped Flask dev server (`app.run()`) to `waitress.serve()` for both API and WebUI. Single-process threading (4 threads), built-in SSL support, identical concurrency to 4 gunicorn workers without external binary dependency. Channel timeout 30s, cleanup interval 15s.

**Quicksetup Auto-Deploy**
New `QUICKROBOT_QUICKSETUP=true` flag triggers automated hardware scan + instance creation + deploy on fresh DB startup. Hardware-aware tier selection (T4>=56GB, T3>=28GB, T2>=13GB, T1>=8GB GPU RAM). Dynamic preset creation (no hardcoded ID), context sizing from available RAM, GPU offload via `LLAMA_ARG_N_GPU_LAYERS`. Idempotent: skips if any instance has `quicksetup_done=1`.

**Timestamp Proxy Engine (ID 23)**
New infra-level HTTP proxy engine for in-prompt timestamp injection. Transparent `/v1/chat/completions` proxy that injects timestamps into user messages and captures response timing. Supports multi-target binding (`target_instance_ids`), per-client inject toggles, and configurable timestamp position/format. Deploy chain: preflight → deps → deploy → config_svc → config_env → start.

**Build Infrastructure & Testing**
`pyproject.toml` created with setuptools build system, editable install (`pip install -e .`), and CLI entry point. All 12 direct dependencies pinned to exact versions. Full pytest suite: 105 tests across 6 tiers (86 pass, 16 fail, 3 skip — failures are pre-existing route bugs surfaced by new test harness). CORS middleware added to API server with configurable origins via `QUICKROBOT_API_CORS_ORIGINS`.

---

### v0.11 (2026-07-25 — 2026-08-02)

**SSOT Final Migration — Complete String Constant Coverage**
Final sweep migrated ~200 hardcoded string literals across 20+ files to QR_STATE_*, QR_JOB_*, QR_STAGE_*, and QR_ENGINE_* constants. Resolved: `lib_runner.py` (40+ stage strings in DEFAULT_STAGE_CHAINS, 8 health_check literals in SQL INSERTs), instance routes (30+ state comparisons in deploy_lifecycle.py, 27 action map keys in status_queries.py), engine modules (6 __init__.py files with CAPABILITIES names and supported_jobs), and all lib/ state transition logic. Added 4 missing stage constants (binary_download, reboot, apt_update, apt_upgrade) and 4 node path constants to `qr_engine_ids.py`. Zero remaining hardcoded stage/state strings in core lifecycle code.

**Binary Download Template System**
Full binary download chain implemented across 3 engine types: llama.cpp (CPU/Vulkan), subprocess (Prometheus Node Exporter), and universal (iperf3). New `engine_binaries` table tracks version, platform, download URL, SHA256 checksum, and extraction metadata. Chain variant `QR_JOB_DEPLOY_BINARY` replaces git+cmake stages with single binary_download stage (5 stages vs 7 for full deploy). Metadata persistence: config_override stores template metadata on create, re-deploy refreshes from DB. Checksum verification: all deploy_binary playbooks verify file size + SHA256 before extraction. Undeploy chains preserve shared template cache. WebUI `/webui/engine_binaries` page with sortable columns, filter sidebar, and per-engine dropdown.

**Node Error Diagnostics — Structured Failure Classification**
Node creation failures now return actionable, machine-parseable errors instead of generic "Node validation failed". New `_extract_node_error()` helper in `lib_ansible_runner.py` walks ansible play/task results for structured error data. `_classify_node_error()` categorizes into 7 typed diagnostics: dns_resolution, connection_refused, connection_timeout, ssh_auth, python_missing, task_failure, not_found. Response format includes `detail.diagnostic.failure_type` + hostname + suggestion. Python 3 preflight check added to validate.yml — fails early with installation instructions if Python is missing on remote host.

**MCP Dual-Token Auth + CORS Header Fix**
Separated MCP SSE token from API proxy key for clearer auth boundaries: `QUICKROBOT_MCP_TOKEN` gates `/sse` endpoint (port 8040), `QUICKROBOT_API_KEY` gates all `/api/v1/*` routes. Token fallback to API_KEY for backward compatibility. New `QUICKROBOT_MCP_KEY_DISABLED` env var mirrors API_KEY_DISABLED pattern. Fixed MCP SSE 401 response missing `Access-Control-Allow-Origin` header — cross-origin browsers now read "unauthorized" instead of opaque "failed to fetch". Added `Authorization` header acceptance alongside `X-MCP-Token` for opencode compatibility.

**Create Instance Wizard Overhaul**
Replaced flat form with 6-step progressive wizard: (1) Node cards with hardware info and search filter, (2) Engine cards with category badges and descriptions, (3) Binary Template selection (separate from presets), (4) Preset selection with model/quantization info, (5) Name auto-proposal, (6) Dynamic Engine Config Editor — fetches engine configs from API, renders grouped into Build/Runtime/GGML/System subsections with inline override toggles. Auto-advance when single option available. Validation guards prevent step skipping. "Start after deploy" checkbox defaults to checked.

**Undeploy Async + Rebuild Chain Fix**
`POST /instances/<id>/undeploy` switched from sync blocking (30s HTTP timeout on slow nodes) to async mode — returns job_id immediately, scheduler executes SSH+systemd operations in background. `Rebuild job` chain corrected: no longer includes `source_llama` stage (git pull) — rebuild now compiles existing source only, not "pull then compile". `_get_stage_chain()` null-safe guard added for node-level operations with instance_id=None.

**Seed File Integrity — Verification Watchdog**
Root cause identified and fixed: `engine_binaries` table missing `metadata` column in base_v011.sql caused seed import to silently fail at INSERT time, skipping all subsequent tables (engine_prompts, benchmark_prompts). Added `metadata TEXT DEFAULT '{}'` column. Added `_verify_seed_counts()` function in startup pipeline that checks row counts for all seeded tables post-import — FATAL on critical table failures, WARN on others. 3 default benchmark prompts added (Count to 10, Count to 100, Short Reasoning).

**.env.sample Redesign + Python Dotenv Migration**
`.quickrobot.env.sample` redesigned: 255 → 190 lines (-26%), tighter layout with inline default documentation. Seed checksum/size set to actual values (not CHANGE_ME) — fresh install flow copies sample → .env without replacing these keys, so they must match disk file. Replaced two hand-rolled `.quickrobot.env` parsers (32 lines total) with single `load_dotenv()` call per file via `python-dotenv`. PID path deduplicated into `lib/lib_pid.py`. Root guard (`os.getuid() == 0`) extracted to `lib/lib_platform.py::check_nonroot()` with Windows compatibility.

**Log System + UI Improvements**
Instance list Jobs column link fixed: now filters by node_id (all host activity) instead of instance_id (single instance). Logs page auto-refresh "Off" dropdown value restored — JavaScript `||` operator was treating `0` as falsy, falling back to 30s refresh. Build number extraction regex widened from `[a-f0-9]{7}` (7 hex chars only) to `[a-zA-Z0-9][a-zA-Z0-9._-]*` (handles git tags like `v2740`). Global logger factory (`lib/lib_logging.py`) replaces 60+ print() calls across 9 files with structured dual-handler logging (stderr + file).

**Global State & Build Fixes**
Build lock timeout changed from hardcoded `300` to `QR_TIMEOUT_DEFAULT`. Node path literals (`/opt/quickrobot/...`) replaced with QR_NODE_* constants. Migration files consolidated: `010_base.sql` → `base_v011.sql`, seed file moved from `data/_seed/` → `db/migrations/`. Seed checksum/size in `.env.sample` confirmed as SOURCE OF TRUTH for fresh installs (not per-deployment secrets).

---
