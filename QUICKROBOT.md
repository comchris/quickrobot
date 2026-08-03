# quickrobot — Architecture & Project Reference

Current release: **v0.10**.

**quickrobot** is a standalone REST API + Web UI controller for managing LLM inference servers, remote nodes, and system services on a local network. Full redesign of keeper_v1 with decoupled API/DB from Web UI, engine type registry, 4-layer config merge chain, Ansible-based deployment, and explicit state machine for instance lifecycle.

> **See also:** `AGENTS.md` for coding rules; `prompts/skill.md` for API/MCP usage; `docs/TODO.md` for current open tasks. Full history in `CHANGELOG_v010.md` and `CHANGELOG_v009.md`.

---

## Project Structure (Skeleton)

```
<project_root>/
├── AGENTS.md              # Coding rules, file handling, server control
├── QUICKROBOT.md          # This file — architecture and project overview
├── prompts/skill.md       # API endpoint reference for agents
├── data/                  # SQLite database + engine state files
├── db/                    # Database adapter + migration runner
│   ├── adapters/          # Per-entity DB operations
│   └── migrations/        # SQL migration files (base_v011.sql)
├── docs/                  # Design specs, phase documentation
├── engine/                # Engine implementations
│   ├── base.py            # BaseEngine class
│   ├── llama_server/      # llama.cpp server
│   ├── llama_rpc/         # llama.cpp RPC gRPC
│   ├── iperf3/            # iPerf3 networking
│   └── quickrobot_*/      # API/WebUI/MCP/Scheduler engines
├── lib/                   # Shared libraries
│   ├── qr_engine_ids.py   # Engine ID/name/category constants (SOT)
│   ├── lib_runner.py      # PlaybookRunner staged chain
│   ├── lib_cluster_env_builder.py  # RPC cluster config merge
│   ├── lib_config_merge.py      # 6-layer config merge
│   └── qr_dynamic_inventory.py  # Dynamic Ansible inventory
├── playbooks/             # Ansible playbooks (core, llama, node, templates)
├── qr_api/                # Flask app package + route submodules
├── quickrobot.py          # Thin shim entry point
├── quickrobot_webui.py    # WebUI Flask application
├── tools/                 # Standalone helper scripts + design docs
│   ├── rpc_proxy.py       # RPC multiplexing proxy MVP (shares VRAM across servers)
│   ├── rpc_multiplexing_proxy_v010.md  # Design analysis for Option B
│   ├── rpc_proxy_implementation_v010.md  # Implementation spec (555 lines)
│   └── RPC_PROXY_IMPLEMENTATION_PROMPT.md  # Implementation brief
├── tests/                 # pytest test suite (17 files, 105 tests)
└── webui/                 # HTML templates
```

---

## Seed File — Chain-of-Trust Verification

The seed file (`db/migrations/seed_v011.sql`) contains `INSERT OR REPLACE` statements for: engine_types, engine_presets, engine_models, playbook_registry, engine_prompts, benchmark_prompts. Engine configs (66 rows) moved to base migration `base_v011.sql`.

### Verification Flow (fresh DB creation)
1. **Pre-flight:** Load `.quickrobot.env`, validate seed checksum+size BEFORE filesystem change → **HARD EXIT** if mismatch
2. **Apply base schema:** `base_v011.sql` creates all 25 tables (idempotent)
3. **Seed import:** `import_seed_file()` executes seed SQL — ONE TIME ONLY on fresh DB creation
4. **Engine discovery** → auto-registers engine types from `engine/` subdirectories
5. **Auto-provision system instances** (API, WebUI, MCP, Scheduler)

### `.quickrobot.env` Keys for Seed Verification
| Key | Purpose |
|-----|---------|
| `QUICKROBOT_SEED_CHECKSUM` | SHA256 hex digest of seed file |
| `QUICKROBOT_SEED_FILESIZE` | File size in bytes |
| `QUICKROBOT_SEED_MAX_ID` | Max ID range for seed data (default 1000) |

### Startup Flow
| Scenario | Behavior |
|----------|----------|
| DB file does not exist | Warn user, create fresh DB with base schema + seed |
| DB file exists | Backup first (timestamped copy), reuse existing DB in-place |

---

## Key Design Patterns

### Ansible Output Normalization
Ansible 2.10+ stores results under `task["hosts"][hostname]` (dict keyed by hostname). The `parse_ansible_json()` function in `lib/lib_ansible_runner.py` normalizes to `task["results"]` (list) for consistent iteration. See `docs/design/ansible_output_format.md`.

### Dynamic Inventory
All `run_playbook()` calls use dynamic inventory via `lib/qr_dynamic_inventory.py` — no stale `.ini` files. Reads node data directly from SQLite at runtime. Every handler passes `inventory_path=None`, resolving hosts dynamically from the nodes table.

### Sortable Tables
WebUI tables use pattern: `<th class="sortable" data-col="N">` + JavaScript with `qrSettings` (localStorage) persistence. Arrow indicators auto-appear. Used on all WebUI table pages. See `docs/design/sortable_tables.md`.

### CLI Output Convention
Use `print("[qr] message")` prefix for API/server messages. Print only actionable info. Omit decorative separators.

### Constant Lookup Rule
Before writing code that references engine types, ports, or versions:
1. **Read** `lib/qr_engine_ids.py` — single source of truth for engine IDs, names, categories, port defaults
2. Use SOT constants (`QR_ENGINE_*`, `QR_DEFAULT_*`, `QUICKROBOT_VERSION`)
3. Never hardcode string literals like `"rpc"`, `"8040"`, `"v0.06"`

### `.quickrobot.env` — Single Source of Truth
Host, port, and token configuration for ALL system-managed engines lives in `.quickrobot.env`. Fallback chain: `.quickrobot.env` (L1) → `engine_configs` table (L2) → instance `config_override` (L3). L3 wins.

### Adding Global Default Values
Quick runtime change (no restart): `INSERT OR REPLACE INTO engine_configs (engine_type_id, key, value, description) VALUES (<id>, 'KEY', 'val', 'desc');`. Full integration: add to seed file + update `.quickrobot.env` checksum.

---

## No Silent Fallback Rule

**ALL code MUST FAIL HARD when it cannot find the resource it needs.** If lookup succeeds → use the value. If lookup fails → raise `SystemExit(1)` or return error — NOT a fallback.

Concrete fix: `lib/qr_dynamic_inventory.py::build_inventory()` groups nodes by inventory name and raises `SystemExit(1)` if any name maps to multiple nodes. No silent last-entry-wins.

---

## SSOT Principle — No Local Redefinitions

Every constant that describes **what the system does** (engine names, IDs, display labels, categories) must be defined in a single canonical file (`lib/qr_engine_ids.py`) and imported everywhere. Never redefine the same constant in multiple files.

Pattern: `_QR_ENGINES` as single data source → auto-derived maps generated at import time. Adding a new engine: one tuple → all maps update automatically. **Zero risk of drift.**

Guideline: If a string matches an engine name, state name, or config key → use the SOT constant. If it's a random literal like `"foobar"` → local is fine.

---

## Staged Playbook Runner Chain (RUNNER-1)

The monolithic `deploy_llama_server.yml` is decomposed into 6 focused, independently retryable stages orchestrated by `PlaybookRunner`.

### Architecture
- **Command side:** `lib/lib_runner.py::PlaybookRunner` — creates jobs, runs playbooks sequentially, writes results
- **Query side:** API routes read from `jobs` + `tasks` tables (<10ms per call)
- **Scheduler:** `engine/quickrobot_scheduler/__main__.py` polls for queued tasks

### Stage Chains (per engine type)
| Engine | Stages |
|--------|--------|
| llama_server / llama_rpc | preflight → deps → source → compile → config_svc → config_env → start |
| iperf3 / universal | preflight → deps → config_svc → config_env → start |

### Job Types
All defined as `_QR_JOB_TYPES` in `qr_engine_ids.py`. Unknown types raise `ValueError` — no silent fallback.

| Job Type | Stages |
|----------|--------|
| deploy | Full staged chain (all 7 stages) |
| rebuild | Compile + config only (skip deps, skip source git pull) |
| reconfigure | Config-only env update (single `config_env` stage) |
| start | Single task — `service_start.yml` |
| restart | Two tasks — `service_stop.yml` → `service_start.yml` |
| undeploy | Health probe (RPC) → stop + cleanup |
| bind/unbind | Stop + config_svc + config_env + start |

### Config Split Rationale: config_svc vs config_env
The split enables **fast userspace config changes** without root privileges. The systemd ExecStart reads CLI args from the env file (`$QR_CLI_ARGS_JOINED`). Preset changes and cluster config updates only modify the env file — no systemctl reload needed.

| Operation | config_svc? | config_env? | Root required? |
|-----------|------------|-------------|----------------|
| Preset change (model, CLI args) | No | Yes | No (SSH user) |
| Cluster config change | No | Yes | No |
| Bind/unbind RPC bindings | Yes | Yes | Yes |
| Start-on-boot toggle | Yes | No | Yes |
| Full deploy | Yes | Yes | Yes |

### CONFIG-1: ENV-Driven CLI Args
Systemd ExecStart uses `$QR_CLI_ARGS_JOINED` from the env file. Config changes only require updating the env file — no `daemon-reload` needed. All 4 engine types use this pattern.

### STATUS-1: Unified Status Endpoint
`GET /instances/<id>/status` returns engine-specific data, available actions, warnings, and metadata in standardized format. Each engine class provides `get_instance_status(db_path, instance_id)`.

---

## System Architecture

### Shared Build Paths
All llama.cpp instances share ONE clone + build per node:
- **Source:** `/opt/quickrobot/llama.cpp` (one git clone per host)
- **Build:** `/opt/quickrobot/llama.cpp/build` (one cmake --build per host)
- Only one cmake build at a time across all instances on the same node.

### Playbook Registry (43 registered playbooks)
Dynamically populated from playbook headers (`# @playbook_id:`). Two naming conventions: legacy `_V1` suffix and modern descriptive names. Resolution priority: ID lookup → tag-based AND-match → file_path exact match.

**Core playbooks used by staged chain:**
| Playbook ID | Description |
|-------------|-------------|
| preflight_check | Node connectivity + OS detection |
| install_deps | Install build dependencies |
| source_llama | Git clone llama.cpp source |
| build_compile_llama | cmake + build |
| deploy_config_service | Write systemd unit file |
| deploy_config_env | Write env file + reload daemon |
| service_start / service_stop | Start/stop service + health probe |
| rpc_health_check | gRPC health probe for RPC nodes |

### Instance & Node ID Ranges
| Range | Type | Protection |
|-------|------|------------|
| Node 1 | System localhost | Never removable via API |
| Nodes 2-99 | Remote nodes | Deletable via API |
| Instance 1-4 | System instances (api, webui, mcp, scheduler) | `system_managed=1` flag |
| Instance 5-99 | User/system mix | Check `system_managed` column |
| Instance 100+ | User instances | Deletable via API |

All protection driven by `system_managed=1` DB column on instances and `node_id == 1` check on nodes. ID ranges are conventions, not DB-enforced constraints.

### Localhost (Node 1) Deploy Support
All engine types can deploy to node_id=1. Dynamic inventory sets `ansible_connection: local`. Playbook execution prepends `sudo` when `node_id == 1`. All playbooks run natively on localhost with full `become: yes` support.

### System-Managed Engines
System-managed instances use PID-based lifecycle via `lib/lib_system_engine.py`. Host/port stored in `.quickrobot.env`, not `engine_configs` table.

| Engine | Type ID | Lifecycle |
|--------|---------|-----------|
| quickrobot-api | 1 | tmux session `qr_api` |
| quickrobot-webui | 2 | Subprocess (PID-in-DB) |
| quickrobot-mcp | 3 | Subprocess (PID-in-DB) |
| quickrobot-scheduler | 4 | Subprocess (PID-in-DB, no port) |

**System Instance Protection:** IDs 1-4 protected from delete/deploy/config-change (returns 409). Use `POST /instances/<id>/restart_system` for restart.

#### Pre-Flight Port + Process Scan
On API startup, each system engine checks: 1) Port via `ss -tlnp`, 2) Process via `ps aux` grep, 3) DB PID status. If ANY conflict → prints FATAL and **aborts** that engine's auto-start (does NOT kill/restart). Agent reads report, kills conflicting processes, then restarts API.

#### Health Check & Self-Termination
All system-managed subprocesses poll `/api/v1/app/status` every 10s. After 2 consecutive failures (5s retry delay, ~10s total), subprocess calls `os._exit(1)`. Prevents zombie accumulation when the API dies.

#### Environment Variable Whitelist
System subprocesses receive engine-scoped env whitelist (not full inheritance). Sensitive tokens like `QUICKROBOT_WEBUI_BEARER_TOKEN` are NOT passed to subprocesses. API token IS passed since all engines need it for authentication.

### MCP Server — SSE Transport (CRITICAL)
The MCP server uses FastMCP with **traditional SSE transport** (`sse_app()`), NOT streamable HTTP transport. Required for compatibility with llama.cpp web UI MCP client.

**Key difference:** `sse_app()` → GET `/sse` establishes connection, server pushes handshake events, then POST messages to `/messages/?session_id=XXX`. `streamable_http_app()` → different flow that does NOT work with llama.cpp web UI.

Working config: `mcp.settings.json_response = False`, `fastmcp_app = mcp.sse_app()`.
CORS middleware: `allow_origins=["*"]`, `allow_methods=["GET", "POST", "OPTIONS"]`.

### MCP SSE Auth + CORS — Debugging Guide (MCP-20260730)

The MCP SSE endpoint (`/sse`) is protected by a token auth wrapper (`_wrap_sse_for_auth()`). When token validation fails, the 401 response **includes `Access-Control-Allow-Origin: *`** so cross-origin browsers can read the error. Without this header, the browser silently shows "failed to fetch" instead of "unauthorized".

**Token acceptance:** The wrapper accepts tokens via THREE methods (all equivalent):
A) `?token=<value>` query parameter (EventSource/browser default — works with SSE clients)
B) `X-MCP-Token: <value>` header (programmatic clients)
C) `Authorization: <value>` header (opencode's default config format)

**Common failure patterns:**

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| Browser shows "failed to fetch" on `/sse` | Token mismatch OR CORS origin not in `QUICKROBOT_MCP_CORS_ORIGINS` | Check 1: `curl -v http://host:8040/sse?token=...` → expect 200. Check 2: `grep MCP_CORS_ORIGINS .env` |
| 401 in browser (can read the error) | Token value mismatch between `.env` and client config | Align `QUICKROBOT_MCP_TOKEN` with what the client sends |
| No CORS headers on 401 response | Old code before `access-control-allow-origin` added to auth wrapper (v0.10→v0.11) | Verify `qr_mcp_server.py:1086` has `b"access-control-allow-origin"` in the 401 headers list |
| SSE connects but tools return errors | CORS allows the connection but API proxy token is wrong | MCP server's `_api_call()` uses `QUICKROBOT_API_KEY` (not `MCP_TOKEN`) for backend API calls |

**Diagnostic flow:**
```bash
# Step 1: Verify token auth works (bypasses CORS entirely)
curl -s http://host:8040/sse?token=YOUR_TOKEN --max-time 3
# Expected: HTTP 200 + SSE stream begins

# Step 2: Check if the calling origin is in allowed list
grep MCP_CORS_ORIGINS .quickrobot.env
# Should include the origin of the client (e.g., http://mintiger.lan:8080)

# Step 3: Check CORS headers on response
curl -s -D - -H "Origin: http://mintiger.lan:8080" http://host:8040/sse --max-time 3 | head -6
# Should include: access-control-allow-origin: *

# Step 4: Check MCP log for startup token info
tail -5 logs/mcp.log | grep -i 'token\|sse'
```

### API execv Restart — Race Condition (API-RESTART-RACE, v0.11)
The WebUI "Restart API" button can cause the API to die and never come back up. Known issues:

A) **Marker removal race** — `quickrobot.py` removed `_restart_marker` before `phase4_pid_port()` could read it. Fixed by using env var `QR_RESTART_MARKER=1` instead.
B) **PID file not removed fast enough** — Restart thread (`_shutdown_execv`) runs in a background thread. `execv` replaces the process instantly, potentially killing the thread mid-operation. PID file may still exist when new process starts.
C) **`_kill_existing()` kills wrong process** — If PID file exists, `_kill_existing()` sends SIGTERM to whatever PID it reads. On restart this can be the old (already dead) PID OR accidentally the new process.

**Current status:** Marker/env mechanism fixed and confirmed working. PID file removal timing still under investigation. Restart thread removes PID file at `lib_instances.py:1142-1144` BEFORE the 2-second sleep and execv, but `execv` may interrupt the thread before file sync completes.

---

## Configuration & Merge Chains

### 6-Layer Config Merge Chain (deploy time)
1. **Engine defaults** — `engine_configs` table, global for engine type
2. **Node defaults** — `node_configs` table, per-node overrides
3. **Preset template** — `engine_presets.config_template` JSON (env + cli_opts + model_id)
4. **Model params** — resolved from `engine_models` via preset's `model_id` FK
5. **Cluster binding** — RPC bindings, split, experts, draft values
6. **Instance override** — `instances.config_override` (FINAL layer)

LLAMA_ARG_HOST/PORT values live in `merged["env"]` (not `cli_opts`). The cluster env builders convert them to CLI args (`-H host -p port`).

### SSH Config Resolution Flow
1. Request body (explicit per-host override)
2. `.quickrobot.env` system defaults
3. Fallback: `getpass.getuser()` for user, `None` (ssh-agent) for key
Resolution happens once at node creation time — values stored in DB columns.

---

## RPC Cluster Architecture

### Cluster Columns
- `rpc_bind_ids`: JSON array of RPC instance IDs bound to a llama-server
- `split`: Server GPU split value (INTEGER, 0-100)
- `experts`: Per-instance MoE expert count (INTEGER, 0-100)
- `draft`: Per-instance draft model token count (INTEGER, 0-100)

### Tensor Split Computation Rules
- Split = NULL → `tensor_split = [rpc1_split, rpc2_split, ...]`
- Split = 0 → `tensor_split = "0,rpc1_split,..."`
- Split = N (N>0) → `tensor_split = "N,rpc1_split,..."`

**Important:** Use `inst.get("split") is not None` (not `if inst["split"]`) to distinguish split=0 from NULL.

### Cluster Device Naming
RPC instances act as distributed GPU devices: `-dev Vulkan0,RPC0,RPC1,RPC2`. Each RPC gets auto-assigned device name (`RPC0`, `RPC1`, …) based on position in `rpc_bind_ids` array. If any bound RPC is down, llama-server crashes on connect — entire instance enters error state.

### Herd Page (`/webui/rpccluster`)
Left panel: llama-server list with state, split_mode, RPC count. Right panel: selected server detail with split config, RPC bindings table (inline edit), deploy + restart actions. API: GET /rpccluster/summary, PUT/DELETE bind-rpc, PATCH split-mode.

---

## Operational Rules

### ENV.SAMPLE — .env.sample Sync Discipline (CRITICAL)
**The `.quickrobot.env.sample` file is the public-facing template. It MUST stay in sync with `.quickrobot.env` after every config change.**

Before committing any change to `.quickrobot.env`:
1. Update `.quickrobot.env.sample` with matching new keys/values
2. Verify no production secrets leaked into `.sample`:
   - `QUICKROBOT_WEBUI_PASSWORD` should be `CHANGE_ME` (not the real password)
   - `QUICKROBOT_MCP_TOKEN` should be `CHANGE_ME` (not a real token)
   - `QUICKROBOT_API_KEY` should be `CHANGE_ME` (not a real key)
   - LAN IPs in `.sample` should use loopback (`127.0.0.1`) or generic placeholders
3. Before deploying or running regression tests, verify sample is current:
   ```bash
   diff .quickrobot.env .quickrobot.env.sample | head -40
   # Expect ONLY intentional differences (production secrets, LAN IPs, seed checksums)
   ```
 4. Seed file checksum/size in `.sample` are REAL values (source of truth for fresh installs). The fresh install flow copies `.sample` → `.env` without replacing these keys. `pre_validate_seed_checksum()` compares them against the actual seed file — mismatch = FATAL. On seed file changes: update both `.sample` AND `.env`.

**Why this matters:** `.env.sample` is the first thing new devs, CI systems, and automated agents see. A leaked password or real token in `.sample` = exposed credential on push. A stale config key = broken fresh deploy from sample.

---

### DB Creation Guard
When DB file does not exist at startup: quickrobot creates a fresh database with base schema + seed data. All instances, nodes, ansible actions, build history are lost. `--init` flag is now deprecated (no-op).

### Process Kill Guard
**NO agent may kill the API process without explicit user confirmation.** Killing the API also kills all running ansible-playbook subprocesses (compiles/deploy in progress can take 15-30 minutes). Before killing: verify no instances in `updating`, `configuring`, `deploying`, `starting`, `stopping`, `loading`, or `compiling` states.

### Compile Verification Rule
When instances show state `updating` after triggering a build: **do NOT assume the build is stuck.** The shared cmake build can take up to 30 minutes. SSH to remote nodes and check for active compile processes before declaring stuck.

### Admin Node Toggle
Set `is_active=0` on a node to block ALL operations (deploy, restart, delete). Returns `NODE_INACTIVE` error. Separate from ping connectivity (`ping_state`).

### Standalone Mode
All four system engines can run as standalone processes in separate tmux sessions. Set all AUTOSTART flags to `false` in `.quickrobot.env` to disable auto-start from the API.

### Logging — Unified Logger Factory (LOG-CONSOLIDATE, v0.11)
All system engines use `lib/lib_logging.py::create_logger()` providing dual handlers (stderr + dedicated log file) with consistent format: `%(asctime)s [%(name)s] %(levelname)s: %(message)s`.

**Per-engine log files:**
| Engine | Log File | Logger Name |
|--------|----------|-------------|
| API | tmux stdout (werkzeug) | `qr.api` |
| WebUI | `logs/webui.log` | `qr.webui` |
| MCP | `logs/mcp.log` | `qr.mcp` |
| Scheduler | `logs/scheduler.log` | `qr.scheduler` |

**Per-engine log level control (env vars):**
```
QUICKROBOT_SCHEDULER_LOG_LEVEL=10  # DEBUG
QUICKROBOT_MCP_LOG_LEVEL=0         # WARNING (quiet)
QUICKROBOT_WEBUI_LOG_LEVEL=0       # WARNING (quiet)
QUICKROBOT_API_LOG_LEVEL=10        # DEBUG
```
Numeric value >= 10 → DEBUG, < 10 → WARNING. Falls back to legacy `QUICKROBOT_CONSOLE_DEBUG_LEVEL` if per-engine key absent. Scheduler additionally supports runtime level change via `scheduler_log_level` in engine_configs table (polls every health cycle).

**Subprocess detection:** When `QUICKROBOT_LOG_PATH` env var is set (managed subprocesses), `create_logger()` skips the FileHandler to prevent duplicate log lines from Popen stdout redirect + FileHandler writing to the same file.

---

## Security

### Root Guard — All System Engines
All 6 server entry points refuse to run as root via `os.getuid() == 0` guard. Non-interactive HTTP servers should not run as root.

### Security Model (v0.07+)
 quickrobot provides **layered authentication** designed for trusted local networks:
 - API key gatekeeper on all `/api/v1/*` routes (`QUICKROBOT_API_KEY`)
 - WebUI password login (`QUICKROBOT_WEBUI_PASSWORD`), rate-limited with failed-attempt logging
 - MCP SSE endpoint auth via `QUICKROBOT_MCP_TOKEN` (falls back to `QUICKROBOT_API_KEY` if unset)
 - MCP proxy calls to API use `QUICKROBOT_API_KEY` (unchanged)
 - MCP DNS rebinding protection configurable via `QUICKROBOT_MCP_DISABLE_DNS_REBINDING`
 - SSL/TLS optional — plain HTTP on all 3 ports by default
 - CORS enabled with wildcard origins (`*`) by default, configurable via `QUICKROBOT_API_CORS_ORIGINS`
 - RPC servers bind to `0.0.0.0` by default — use per-instance override for local-only

### API Key Toggle — QUICKROBOT_API_KEY_DISABLED (v0.11)
 `QUICKROBOT_API_KEY_DISABLED=true` disables API key authentication:
 - Startup: skips FATAL "API_KEY required" check in prod mode
 - Auth: `_AUTH_TOKENS` stays empty → all routes accept any request
 - Also disables MCP SSE token enforcement when `QUICKROBOT_MCP_TOKEN` is set
 - Valid values: `true`, `1`, `yes` (truthy); `false`, `0`, `no`, empty (falsy) — case-insensitive
 - Default: unset → normal enforcement (key required in prod, matched on every request)

### Preset Benchmarking — Swap Presets, Not Instances
**Do NOT create a new instance for each preset benchmark.** Reuse existing test server instances and switch presets via `change_preset(instance_id, preset_id)` or `PUT /instances/<id>` with `{preset_id: N, skip_build: true}`. All other context (port, node hardware, build number, RPC bindings) stays identical for clean apples-to-apples comparison.

---

## API Gotchas & Patterns

### RPC Servers ≠ HTTP
RPC servers use a custom binary protocol, NOT HTTP. Do NOT use `curl /health` on RPC endpoints. Verify RPC health via SSH process check or bound llama_server inference results.

### Restart vs Deploy
| Endpoint | What it does | Use when |
|----------|-------------|----------|
| `POST /instances/<id>/restart` | Stop + start with **existing** config | No config change needed |
| `POST /instances/<id>/deploy` | Regenerate systemd unit + env file → stop → start | Preset change, RPC binding, any config update |

### Preset Change on Running Instance
**Wrong:** `POST /instances/<id>/deploy` with `{preset_id: N}` — deploy reads preset from DB, ignores request body.
**Correct:** `PUT /instances/<id>` with `{preset_id: N, skip_build: true}` — triggers BC-1 fast path (config-only update).

### Crash Detection Bug
The `_recently_completed` cursor truthiness check: `bool(cursor)` is always `True`. Always use `.fetchone()` to get actual row data before converting to bool.

---

## Node Management

### Node Creation Flow
| Path | Steps | Result |
|------|-------|--------|
| API POST /nodes | Create node in DB → validate_node() inline | Single ansible_actions entry |
| WebUI /webui/nodes/new | POST /nodes → POST /nodes/<id>/discover | Two ansible_actions entries |

---

## Running Tasks & Logging

### Ansible Actions Logging
- `task_summary` column contains full parsed JSON output from ansible-playbook (2-44KB)
- Locale fix: `LC_ALL=en_US.UTF-8` and `LANG=en_US.UTF-8` set in subprocess env — prevents silent failures
- Node-level actions log to `ansible_actions` with `node_id`

### Manifest Tracking
All writable agents log file modifications to `./manifest.log`. Format: `<filepath> | <timestamp> | <agentname> | <backup_filename> | <reason>`. See `AGENTS.md`.

---

## Lessons Learned (Condensed)

**Scope creep:** Modify only what's asked — do NOT expand scope without explicit confirmation. Before bulk operations, state exact scope and ask confirmation.

**Preset change via deploy:** `POST /instances/<id>/deploy` reads preset from DB record, not request body. Use `PUT /instances/<id>` with `{preset_id: N, skip_build: true}` for config-only updates.

**No sleep in automation:** Always poll API endpoints instead of using `sleep`. One benchmark per instance at a time.

**Global state verification:** Never declare "all clear" based on partial checks. Read `GET /api/v1/app/status` and check `global_state` field before reporting health.

**Pre-flight exit behavior:** `sys.exit(1)` in `_start_system_engine()` exits the ENTIRE API on first conflict. Stale processes must be killed BEFORE restart.

**execv restart race condition:** When API does `execv` to replace itself, the background thread (which removes PID file and writes restart marker) can be interrupted mid-operation. The new process may see stale PID file or missing marker → false-positive "already running" → exits. Fix: use env var `QR_RESTART_MARKER=1` for restart signaling (set before execv, survives across execv).

**MCP CORS origins need BOTH hostname AND IP:** Browsers send `Origin:` header as whatever URL they used — if the user navigates via IP (`http://192.168.31.10:8080`), the origin is `http://192.168.31.10:8080`, NOT `http://mintiger.lan:8080`. Add both entries to `QUICKROBOT_MCP_CORS_ORIGINS` for each remote node.

**MCP SSE auth wrapper returns 401 without CORS headers:** The `_wrap_sse_for_auth()` function sends raw Starlette HTTP messages with only `content-type` header on 401. Cross-origin browsers can't read the response → opaque "failed to fetch" error. Added `Access-Control-Allow-Origin: *` to the 401 response (v0.11).

**MCP SSE accepts THREE token formats:** `?token=` query param, `X-MCP-Token` header, and `Authorization` header. All equivalent since v0.11 (Option B fix added `Authorization` acceptance). Opencode's `"Authorization"` header in config works now.

---

## Completed Phases Summary

Full phase history archived — see `CHANGELOG_v010.md`, `CHANGELOG_v009.md` and `docs/TODO_done_v010.md` for detailed per-entry changelog.
