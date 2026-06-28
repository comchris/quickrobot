# Project Agent Rules — quickrobot

## Scope
These rules apply to **all agents** operating within the project directory.
Agent-specific roles and workflows are defined in `.opencode/agents/*.md`.

> **See also:** `SKILL.md` for API usage; `QUICKROBOT.md` for architecture and design patterns.

### AGENTS.md Session Rule

AGENTS.md is the source of truth for coding rules. During a session, agents MUST NOT modify this file directly — changes go into `AGENTS_new.md` (copy of current AGENTS.md with edits applied). The user reviews `AGENTS_new.md` and manually replaces `AGENTS.md` BEFORE the next session starts. This prevents rule drift mid-session.

---

## 1. Hard Rules — Never Violate

### 1a. No Silent Database Wipe
**DB creation is automatic** on startup (no `--init` flag needed). If no `.db` file exists, quickrobot creates a fresh database with base schema + seed data — all instances, nodes, ansible actions, build history are lost. Existing DB is backed up first (timestamped copy) before in-place use.
- Fresh DB creation fires without explicit confirmation when file doesn't exist. This IS the normal behavior — do NOT run `--init` just to "refresh" (it's a no-op now).
- **--mode dev** = normal operation, playbook verification with warnings on mismatch. Use during development/playbook changes.
- **--mode dev-update** = update current DB's `playbook_registry` checksums to match disk files, then continue running in prod mode (no longer exits). Does NOT touch seed file or `.quickrobot.env`.
- Both `dev` and `dev-update` are **development tools** — only run on explicit USER REQUEST, not as automatic pre-flight.
- **--init is deprecated (no-op).** DB creation is automatic based on file existence: no DB → warn + create fresh; DB exists → backup + reuse in-place.
- When in doubt about `--mode dev-update`, the semantics are defined in `QUICKROBOT.md` §Seed File — they do not change.
- **AGENTS.md is protected:** Agents MUST NOT edit `AGENTS.md` directly during a session. Changes go into `AGENTS_new.md`. The user reviews `AGENTS_new.md` and manually replaces `AGENTS.md` before the next session. This prevents rule drift mid-session.

### 1b. No Force by Default
Unless explicitly told, all actions must be **non-destructive**:
- `deploy` (not `deploy --force`)
- `start` (not `start --ignore-state`)
- Check current state before acting; skip if already in target state

### 1c. System Instance / Node Protection
- **Node ID 1** is the localhost machine where the API runs. Never removable via API.
- **System instances (IDs 1-4, `system_managed=1`):** api(1), webui(2), mcp(3), scheduler(4). Protected from delete/deploy/config-change (HTTP 409 `SYSTEM_MANAGED_INSTANCE`). Use `POST /instances/<id>/restart_system` for restart.
- IDs < 10 with `system_managed=1` are protected. ID ranges are conventions, not DB-enforced constraints — always check the flag.
- Node ID 1 is localhost — do NOT deploy remote instances to it unless explicitly requested.

### 1d. Deployed-State Assumption
When an RPC instance shows `"error"` state, it is **already deployed** — the issue is usually a crashed service or port conflict. Use `POST /instances/<id>/start` to restart, NOT `POST /instances/<id>/deploy` which regenerates unit file + env unnecessarily. Deploy only when: preset changed, RPC bindings changed, node IP changed, or config_override changed.

---

## 2. SSOT — Single Source of Truth

### 2a. Constant Hierarchy
Before writing code that references engine types, ports, versions, or job states:
1. **Read `lib/qr_engine_ids.py`** — single source of truth for ALL entity constants
2. **Read `lib/lib_constants.py`** — runtime defaults (not entity definitions)
3. **Use SSOT constants** — see list below
4. **Never hardcode** string literals like `"rpc"`, `"8040"`, `"v0.07"` in code

**SSOT Constant Reference (all from `lib/qr_engine_ids.py`):**

| What | Constant | Example |
|------|----------|---------|
| Engine ID | `QR_ENGINE_LLAMA_SERVER` = 21 | Use in port allocation, not `8080` literal |
| Engine string name (DB compare) | `QR_ENGINE_LLAMA_SERVER_NAME` = `"llama_server"` | Use in DB queries, not `"llama_server"` literal |
| System engine short alias | `QR_ENGINE_API`, `QR_ENGINE_WEBUI`, `QR_ENGINE_MCP` | Short names for lifecycle functions |
| Port defaults | `QR_ENGINE_PORT_DEFAULTS["llama_server"]` = 8080 | Lookup map, not hardcoded port |
| Job types | `QR_JOB_DEPLOY`, `QR_JOB_REBUILD`, `QR_JOB_RECONFIGURE`, `QR_JOB_BIND`, etc. | All job type comparisons must use these |
| Stage names | `QR_STAGE_CONFIG_ENV`, `QR_STAGE_CONFIG_SVC`, `QR_STAGE_START`, `QR_STAGE_COMPILE`, `QR_STAGE_PREFLIGHT`, etc. | Staged chain stage references |
| Stage → state map | `STAGE_STATE_MAP[QR_STAGE_START]` → `"running"` | Never hardcode state transition strings |
| Skipable stages | `SKIPABLE_STAGES` = `{QR_STAGE_SOURCE, QR_STAGE_COMPILE}` | Binary-exists skip logic |
| Timeout defaults | `QR_TIMEOUT_COMPILE` = 1800, `QR_TIMEOUT_SOURCE` = 600, `QR_TIMEOUT_DEFAULT` = 300 | Playbook execution timeouts |
| Final states per job | `JOB_FINAL_STATES["deploy"]` → `"running"` | Post-job state resolution |
| Version | `QUICKROBOT_VERSION` = `"v0.07"` | Display versions, not hardcoded strings |
| Localhost fallback | `QR_DEFAULT_LOCALHOST` = `"127.0.0.1"` | Bind address fallbacks |
| System instance ID lookup | `get_system_instance_id("webui")` → 2 | Use this function, not hardcoded instance IDs like `2`, `3`, `4` |
| ID ↔ name conversion | `get_name_by_id(21)` → `"llama_server"` / `get_id_by_name("llama_rpc")` → 22 | Bidirectional lookup, handles hyphen/underscore aliases |

### 2b. Anti-Pattern: Local Redefinition
When the same concept is defined in two places (e.g., engine name in `qr_engine_ids.py` AND hardcoded as `"llama_server"` in a route handler), it is a bug waiting for drift. Every entity constant must be imported from SSOT, never redefined locally.

**BAD — engine name comparison:**
```python
# Hardcoded string drift risk: what if someone changes "llama_server" here but not in qr_engine_ids.py?
if engine_name == "llama_server": ...
```
**GOOD — SSOT import:**
```python
from lib.qr_engine_ids import QR_ENGINE_LLAMA_SERVER_NAME
if engine_name == QR_ENGINE_LLAMA_SERVER_NAME: ...
```

**BAD — job type comparison:**
```python
if job_type == "reconfigure": ...  # Magic string, no sync mechanism
```
**GOOD — SSOT constant:**
```python
from lib.qr_engine_ids import QR_JOB_RECONFIGURE
if job_type == QR_JOB_RECONFIGURE: ...
```

**BAD — stage state hardcoded:**
```python
# If STAGE_STATE_MAP changes but this doesn't, instance shows wrong state after deploy
new_state = "running" if stage == "start" else "deployed"
```
**GOOD — SSOT lookup:**
```python
from lib.qr_engine_ids import QR_STAGE_START, STAGE_STATE_MAP
new_state = STAGE_STATE_MAP.get(QR_STAGE_START, "deployed")
```

**BAD — timeout magic number:**
```python
timeout=1800  # What does 1800 mean? Compile timeout? Source timeout?
```
**GOOD — named constant:**
```python
from lib.qr_engine_ids import QR_TIMEOUT_COMPILE
timeout = QR_TIMEOUT_COMPILE  # Clear: 30 min compile timeout
```

### 2c. Runtime Defaults (`lib/lib_constants.py`)
Runtime operational defaults (not entity definitions) live in `lib/lib_constants.py`. Import these instead of hardcoding:
- `QUICKROBOT_CONSOLE_DEBUG_LEVEL` = 10 (numeric, 0=quiet, >=10=verbose)
- `QUICKROBOT_ANSIBLE_LOG_LEVEL` = `"errors"` (what gets persisted to ansible_actions table)
- `GRACE_PERIOD_RUNNING` = 300 (seconds before crash-detection for large model loads)
- `QUICKROBOT_PLAYBOOK_TIMEOUT` = 3600 (default playbook execution timeout)
- `DEFAULT_ANSIBLE_USER` — current OS user via `getpass`

### 2d. .quickrobot.env Is the Source of Truth for Network Config
Host, port, and token configuration for ALL system-managed engines lives in `.quickrobot.env`. Never duplicate these values in code constants or the database:
- API: `QUICKROBOT_API_HOST`, `QUICKROBOT_API_PORT`
- WebUI: `QUICKROBOT_WEBUI_HOST`, `QUICKROBOT_WEBUI_PORT`, `QUICKROBOT_WEBUI_AUTOSTART`
- MCP: `QUICKROBOT_MCP_HOST`, `QUICKROBOT_MCP_PORT`, `QUICKROBOT_MCP_*`
- System instances get their host/port from `.env`, not `engine_configs` table

### 2d. Code Reuse Rule
When adding a feature that resembles existing code (another playbook, another engine handler, another UI page):
1. Check if an existing implementation covers 80%+ of the need
2. Extend or parameterize the existing code instead of writing a new one
3. If creating a new playbook: it should share structure with existing playbooks, not reinvent task patterns

**Concrete examples:**
- Adding support for a new engine type: add one tuple to `_QR_ENGINES` in `qr_engine_ids.py`, create an engine subclass that inherits from `BaseEngine`, register its status handler. Do NOT duplicate the staged chain logic — it already exists in `lib_runner.py`.
- Creating a new playbook: use the same `@playbook_id:` header format, follow the `set_fact` + template pattern of existing playbooks, register in `playbook_registry` with checksum in seed. Do NOT invent a new result format — `parse_ansible_json()` already normalizes all outputs.
- WebUI page for a new engine config: use the shared `<span class="actions-render">` pattern from `instance_detail.html`, not inline button loops. The shared renderer is at `webui/base.html`.

---

## 3. Safety & File Handling

### Backup Before Every Edit
Before editing ANY file < 10MB in this project:
```bash
cp -n -v <filename> <filename>_backup_TIMESTAMP
```
- `-n` prevents overwrites, `-v` shows written files
- Backup files are **excluded** from the manifest

**YAML/JSON naming:** Append timestamp AFTER the extension. `deploy_llama_server.yml_backup_20260606T144327` — NOT `deploy_llama_server_backup_20260606T144327.yml`. Old-style backups pollute glob results and playbook discovery.

### Read Before Write
Always read the entire file before attempting an edit. Never write blindly.

### No Silent Deletion
Never use `rm`, `rm -f`, `rm -rf`, or `2>/dev/null`. Move unwanted files to `./OLD_ignore/` instead.

### File Naming
- Only **ONE dot** in filenames (e.g., `qr_api_server.py`)
- Max **30 characters** per filename (before extension)
- Use project prefixes: `QR_*`, `app_*`, `lib_*`

---

## 4. Tool Execution

### Bash Chains
Limit to **3-4 operators** (`&&`, `||`, `|`). Prefer dedicated tools over shell chaining.

### No Wildcard Kills
Never use `pkill -f "pattern"`. Query exact PID first:
```bash
ps aux | grep exact_filename.py | grep -v grep | awk '{print $2}'
```

### No `cat` on Binary Files
Never use `cat` on binary files (SQLite databases, images). Use `file <filename>` to check type, then `Read` tool for text or `xxd <file> | head` for binary inspection.

### Ansible Locale Requirement
All ansible subprocess calls MUST have `LC_ALL` and `LANG` set to `en_US.UTF-8`. Missing locale causes rc=1, empty stdout → parsed as `{"plays": []}` → API reports "ok" with no data. Silent failure.

```python
env["LC_ALL"] = "en_US.UTF-8"
env["LANG"] = "en_US.UTF-8"
```

### Ansible Output Format
The `ansible_actions.task_summary` column stores the full parsed JSON from ansible-playbook (typically 2-44KB). See `docs/ansible_output_format.md` for normalization details.

### Mandatory Syntax Check After Edit
After editing any file that supports syntax checking, you MUST run a single-command syntax check:
- **Python** (.py): `python3 -c "import py_compile; py_compile.compile('<filepath>', doraise=True)"`
- **JSON** (.json): `python3 -c "import json; json.load(open('<filepath>'))"`
- **YAML** (.yml/.yaml): `python3 -c "import yaml; yaml.safe_load(open('<filepath>'))"`

---

## 5. Startup & Lifecycle Discipline

### 5a. API Server — tmux Session
The API runs in a dedicated tmux session (`qr_api`) with explicit socket path (avoids stale `/tmp/tmux-1001` permission issues in this environment). Production deployment uses systemd.

**Socket:** `-S /tmp/qr.sock`  
**Session:** `qr_api`  
- `remain-on-exit on` — survives process crashes

**Create session:** `tmux -S /tmp/qr.sock new-session -d -s qr_api`  
**Check status:** `tmux -S /tmp/qr.sock has-session -t qr_api 2>&1` (exit 0 = exists)  
**Start:** `tmux -S /tmp/qr.sock send-keys -t qr_api 'cd /CORE/projects/quickrobot && python3 quickrobot.py' C-m`  
**Stop:** Query PID via `ps aux`, then `kill <PID>`, wait 1s, verify dead.

**Reading output:** Use `-S -` to scrape full scrollback buffer, not just visible screen:
```bash
tmux -S /tmp/qr.sock capture-pane -t qr_api -p -S - | tail -60
```

### 5a. Loading State Transition — SSE Model Load (v0.07)
After a `start` or `restart` job completes, instance state transitions to `"loading"` (from `JOB_FINAL_STATES`). The WebUI monitors this state and:

1. **llama_server only** — connects to `GET /api/v1/instances/<id>/models-sse` which proxies the remote llama.cpp `/models/sse` endpoint
2. SSE events contain `model_status` with values: `loading`, `loaded`, `sleeping`, `unloaded`
3. When SSE reports `loaded` or `sleeping`: WebUI shows progress bar complete, then `location.reload()` after 2s
4. **Server-side transition** — `api_model_load_sse()` SSE proxy detects `status=loaded`/`status=sleeping` events and transitions DB state `"loading"` → `"running"` directly

**RPC behavior:** `llama_rpc` start/restart jobs go directly to `"running"` (engine-aware check in `_finalize_job()`, uses SSOT constant `QR_ENGINE_LLAMA_RPC_NAME`). No SSE loading state for RPC since it has no `/models/sse` endpoint.

### 5g. CONFIG-1 — Env-Driven CLI Args (No Daemon-Reload)
Systemd ExecStart reads CLI args from `$QR_CLI_ARGS_JOINED` in the env file:
```ini
ExecStart=... $QR_CLI_ARGS_JOINED
```
Preset changes, cluster config updates, and model param changes only modify the env file — no `systemctl daemon-reload` needed. Config changes via `PUT /instances/<id>` with `skip_build=true` use the BC-1 fast path (config_env + service_start stages) which is fast (<100ms) because it skips git clone and cmake build.

### 5h. MCP SSE Session Stale After API Restart (v0.08 DESIGN NEEDED)
When the API restarts, it kills the old MCP process and starts a new one with a new PID. The opencode harness has cached the old MCP's SSE session ID — every subsequent tool call fails with `MCP error -32602: Invalid request parameters`.

**Workaround:** Restart opencode context to refresh tool schema after API restart.
**Root cause:** `_quickrobot_mcp/__init__.py` kills orphaned MCP (PPID=1) and restarts it even when parent API just died. Options for v0.08: accept orphaned MCP, opencode auto-reconnect, session ID persistence, or dual-engine pattern.

### 5b. WebUI & MCP — Subprocess Lifecycle
Both WebUI and MCP run as API subprocesses (not tmux):
- Restart via API: `POST /instances/2/restart_system` (WebUI) or `/instances/3/restart_system` (MCP)
- For WebUI-only changes: restart via API, then verify PID changed (confirms reload)
- Children are isolated in own process groups (`start_new_session=True`) — survive API death as zombies but don't conflict on ports

### 5c. Port Resolution — Never Hardcoded
Ports come from `.quickrobot.env`, not code:
- API: `QUICKROBOT_API_PORT`
- WebUI: `QUICKROBOT_WEBUI_PORT`  
- MCP: `QUICKROBOT_MCP_PORT`
- Instance allocation: derives from `QR_ENGINE_PORT_DEFAULTS.get(engine_name)` in SSOT, auto-increments. Do NOT use hardcoded `8080` or `9000` — look up via `lib.qr_engine_ids.QR_ENGINE_PORT_DEFAULTS`.

### 5d. Process Kill Guard
NO agent may kill the API process without explicit user confirmation. Killing the API also kills all running ansible-playbook subprocesses (compiles, deploys in progress). The `update_and_compile` playbook can take 15-30 minutes. Before killing: verify no instances in `updating`, `configuring`, `deploying`, `starting`, `stopping`, `loading`, or `compiling` states.

### 5e. Compile Verification Rule
When instances show state `deploying` (or `updating` via legacy path) after triggering a build: **do NOT assume the build is stuck.** The shared cmake build can take up to 30 minutes. SSH to remote nodes and check for active compile processes (`ps aux | grep cmake`) before declaring stuck. Only declare stuck if no active compile processes after 15+ minutes.

**Parallel compiles:** Compiles run in parallel across different nodes (dllama1/2/3 all compile simultaneously). Each node processes only one compile at a time (per-node build lock, SESSION-1 fix). Next instance on that node stays `unconfigured` until current chain completes.

### 5f. System Engine Pre-Flight Scan (Startup)
On API restart, each system engine undergoes a pre-flight port + process scan before auto-start:
1. **Port check** via `ss -tlnp` — verifies assigned port is free (WebUI 8038, MCP 8040; Scheduler N/A)
2. **Process scan** via `ps aux` — grep for known Python file names
3. **DB PID check** — verifies `pid_last_known` status

If any conflict: FATAL messages logged, engine auto-start **aborted** (does NOT kill or restart). Agent reads report, kills conflicting processes, then restarts API. The scan patterns are defined in `_ENGINE_SCAN_PATTERNS`:
- webui: port 8038, `quickrobot_webui.py`
- mcp: port 8040, `qr_mcp_server.py`
- scheduler: no port, `quickrobot_scheduler` / `engine.quickrobot_scheduler`

**Note:** This is startup-only behavior. Explicit `/instances/<id>/start` and `/instances/<id>/restart_system` endpoints use the existing DB PID check logic (unchanged).

---

## 6. Connection Method Priority

The project provides three parallel control paths — **use the right one for the task**:

A) **MCP tools (primary)** — Direct function calls in every agent's context. ~26 tools organized by read/write/proxy categories. Use for all routine operations — fast (no HTTP overhead), type-safe (parsed JSON). Example: `quickrobot_run_benchmark(instance_id=108, prompt_id=1)`.

B) **API server (HTTP REST)** — Full Flask API via `curl` on port 8039. Use when you need exact HTTP semantics (status codes, headers, custom auth tokens), endpoints not exposed as MCP tools, or debugging the API layer.

C) **WebUI (browser)** — HTML/JS SPA. Use Playwright skill for UI testing during development. For quick health checks: `curl -s http://127.0.0.1:<WEBUI_PORT>/`.

D) **SQLite CLI** — Only for schema inspection (`PRAGMA table_info`), one-shot seed file generation, or when explicitly debugging a DB-level issue. Do NOT use to check state (bypasses API guards).

**Rule of thumb:** MCP → API → WebUI → SQLite. Default to the fastest path that solves the task.

---

## 7. Playbook Rationale: Why Playbooks Drive Config Changes

Playbooks are not just deployment scripts — they are the mechanism for **configuring user-space settings on remote nodes**:
- `deploy_config_env.yml` (no-become) writes env files as SSH user, enabling preset/cluster config changes without root privileges
- `deploy_config_service.yml` (become: yes) writes systemd unit files when root is needed
- The split enables **fast userspace reconfigs**: preset changes, cluster binding updates only need `config_env`, no systemctl reload
- Full deploys use both playbooks in staged chains; config-only changes use just `config_env`

**Why this matters for agents:** When a user wants to change a preset or cluster config, the correct action is `PUT /instances/<id>` with the new preset_id — NOT creating a new playbook. The existing `deploy_config_env.yml` handles it.

---

## 8. Configuration Hierarchy

Configuration resolves through layers: L3 (instance override) > L2 (engine type config) > L1 (.env / defaults):
- **L1:** `.quickrobot.env` (system-level, human-edited) or `engine_configs` table (type-level, API-mutable)
- **L2:** `engine_configs` table keyed by `engine_type_id`
- **L3:** `config_override` JSON column in `instances` table (single instance, API-mutable)

System-managed instances (IDs 1-4) skip L3 entirely.

---

## 9. Database Rules

### General
- DO NOT run raw SQL directly on `data/quickrobot.db` without explicit user authorization.
- Quickrobot is a REST API server. The API is the interface under test — query and mutate via API endpoints, not sqlite CLI.
- sqlite CLI is acceptable only for schema inspection (`PRAGMA table_info`), one-shot seed file generation, or when explicitly debugging a DB-level issue.

### Seed File
- **Location:** `data/_seed/seed_v007.sql` (relative to project root)
- **Format:** `INSERT OR REPLACE` statements — fully idempotent
- **Verification:** `.quickrobot.env` keys `QUICKROBOT_SEED_CHECKSUM` + `QUICKROBOT_SEED_FILESIZE` are validated on fresh DB creation (automatic, no `--init` flag needed).
- After any schema change: seed file must be regenerated and checksum updated in `.quickrobot.env`

### DB Manipulation via sqlite3 CLI
Before any `DELETE`/`UPDATE`/`INSERT`: state exact rows affected and get user confirmation. Prefer API endpoints which have proper guards (system-managed checks, FK constraints).

---

## 10. Ansible Template Gotchas

### Jinja2 Whitespace
Ansible's `template` module consumes **ALL surrounding whitespace** around Jinja2 control tags. Pre-compute lines via `set_fact` before template render when needed.

### ExecStart Last in [Service]
Put `ExecStart=` as the last configurable directive in `[Service]`, followed by an empty blank line, then static lines. The empty line prevents Ansible from consuming the final newline.

---

## 11. Coding Integration — 4-Layer Sync

Every feature touches 4 layers that must stay in sync:
1. **Ansible playbook** — YAML with `@playbook_id:` header + checksum in seed
2. **API handler** (`qr_api/routes_*.py`) — parses playbook result JSON, calls DB adapter
3. **DB adapter** (`db/adapters/*.py`) — INSERT/UPDATE with dynamic column detection
4. **WebUI** (`webui/*.html`) — renders API response via JavaScript

**Integration rule:** When adding a new field, update ALL 4 layers. Changing DB schema without updating seed file means fresh DB creation (automatic) loses the new data.

---

## 12. API Testing Discipline

### Read State Before Writing
Before ANY write action (deploy, build, create, reboot): read current state (`state`, `is_active`, `node_build_state`), decide if action makes sense, execute ONE action, verify result.

### One Action Per Test Command
Each write action is a separate command. See the raw JSON response. Do NOT chain multiple writes — you lose per-call status/error details.

### Use Inactive Nodes for Destructive Tests
When testing actions that change state, use nodes with `is_active=0` and no live instances.

### Ask Before Bulk Testing
If the test involves more than one write action, or targets a live/active node, ask the user first.

### No Sleep >10 Seconds
Always poll API endpoints instead of sleeping. Return to user for manual polling during long operations (5-30 min compiles).

---

## 13. Response Format Patterns

- **Single resource:** `{ "status": "ok", "data": { ... } }` → `resp.get("data", {})`
- **List resources:** `{ "status": "ok", "total": N, "items": [...] }` → `resp.get("items", [])`
- **Error:** `{ "status": "error", "code": "...", "message": "..." }`

**Always check `response.get("status")` first.** A 404 returns error status with `RESOURCE_NOT_FOUND` — do NOT interpret empty `items` array as "zero results" when the response is actually an error.

**Nested fields:** Fields like `pid`, `version` live INSIDE `data`, not at top level: `resp.get("data",{}).get("pid")`.

**Instance objects:** Fields are `engine_type_name` (NOT `engine_type`) and `node_hostname` (NOT `node_name`).

---

## 14. Communication

- Report task completion or failure with a structured summary
- Report blockers immediately — do not continue past them
- Use varied list formats (A/B/C, 1/2/3), avoid "-" bullets
- Pure ASCII/UTF-8 English only. No emojis in any project output.

---

## 15. File Manifest Tracking

All writable agents must log every file modification to `./manifest.log`.

### Format (append-only, pipe-delimited)
```
<filepath> | <timestamp> | <agentname> | <backup_filename> | <reason>
```

Timestamps generated dynamically: `TIMESTAMP=$(date +%Y-%m-%dT%H:%M:%S)`  
Use relative paths from project root. All five fields must be present and non-empty.

---

## 16. Forbidden Actions

The following are forbidden for ALL agents:
- `rm`, `rm -f`, `rm -rf` (anywhere)
- Wildcard process kills (`pkill -f`, `killall`)
- Installing dependencies without user confirmation
- Modifying files outside the project directory without explicit authorization
- Restoring full project backups from tar.gz without Design Agent approval
- **NEVER DELETE OLD BACKUPS** — absolute rule for ALL backup locations

---

## 17. SSH Host Key Policy

Prefer DNS names over raw IPs for SSH connections. Use `StrictHostKeyChecking=accept-new` (headless-friendly, rejects changed keys). Do NOT use `-o StrictHostKeyChecking=no` as default.

---

## 18. Instance Creation & Deploy Workflow

### Create → Auto-Deploy → Auto-Start (Single Step)
`POST /instances` (or MCP `create_instance`) **auto-deploys and auto-starts** the instance in one call. No separate deploy/start needed:

1. **Create instance** → API creates DB record, assigns port, creates a deploy job, and begins the staged chain
2. **Check jobs** → Query `GET /jobs` to see active deploy jobs with status (`queued`, `running`, `completed`, `failed`). Jobs stay `'queued'` until the scheduler claims them (JOB-STATE-1 fix).
3. **Check instances** → Query `GET /instances` for current state (`unconfigured` → `configuring` → `deploying` → `loading` → `running`)
4. **Report to user** → Summarize final state: success (all running), partial (some failed), or total failure

**No need to call:** `POST /instances/<id>/deploy` or `POST /instances/<id>/start` after creation. The create endpoint handles the full lifecycle.

### Job Duration Accuracy
Job duration = actual execution time only (`started_at` set when scheduler claims first task, not at creation). Global per-job timeout: 2h (7200s, JOB-TIMEOUT). Jobs older than `created_at + timeout` get reset — running tasks → queued, job status → `'error'` with expiry message.

### Example Workflow
```
1. quickrobot_create_instance(name="node1-rpc", preset_id=10, node_id=2, engine_type_id=22)
2. → Returns instance ID (e.g., 105) with state "unconfigured"
3. quickrobot_list_instances_summary()
4. → After 1-2s: state transitions to "configuring" / "deploying" / "loading" / "running"
5. Report: "Instance 105 deployed and running on dllama1.lan:port 50052"
```

### Multi-Instance Deployment
When creating multiple instances (e.g., cluster RPC nodes):
- Create all instances first (parallel OK — they queue independently)
- Check job status via `GET /api/v1/jobs?status=running` or MCP `list_instances_summary`
- Report per-instance status: running, configuring, deploying, error

### When to Call Deploy Explicitly
Only use `POST /instances/<id>/deploy` when:
- Preset changed and config needs regenerating
- RPC bindings changed (use bind-rpc then deploy)
- Node IP changed or config_override changed
- Instance in "unconfigured" state after manual creation with `start_after_deploy=false`

### State Transition Quick Reference
| State | Meaning |
|-------|---------|
| `unconfigured` | Just created, no deploy job yet (or job queued) |
| `configuring` | Writing env/service files via playbooks |
| `deploying` | Deploy chain active (may be compiling, installing deps) |
| `loading` | Service started, model loading in progress |
| `running` | Fully operational |
| `error` | Failure detected (check jobs/tasks for details) |

---

## 19. Cross-Reference Map

| Topic | See |
|-------|-----|
| Instance creation & deploy workflow | §18 above (auto-deploy, job checking, state transitions) |
| Database structure, tables, engine IDs, seed file | `QUICKROBOT.md` §Database + §Seed File |
| Security (root guards, snakeoil model) | `QUICKROBOT.md` §Security |
| API endpoint reference + MCP tools | `SKILL.md` |
| Architecture, merge chains, playbook registry | `QUICKROBOT.md` |
| Task list (open items) | `docs/TODO.md` |
| Full task history | `docs/TODO_done.md` |
| Ansible JSON format details | `docs/ansible_output_format.md` |
| Sortable table pattern | `docs/sortable_tables.md` |
| Changelog | `CHANGELOG.md` |

---

## 20. API Endpoint Creation Rule — No Silent Additions

### Rule: No new API endpoints without explicit user auth

Before adding any **new** API endpoint (route registration in `__init__.py`), the agent MUST:
1. State the endpoint path, HTTP method, and what it does
2. List the files that will be modified
3. Wait for user confirmation before writing code

**Rationale:** Each new endpoint adds attack surface, increases API surface area, and needs to be documented in SKILL.md + MCP tools. Silent additions break the 4-layer sync rule (§11).

**Exceptions (no auth needed):**
- Bug fixes to **existing** endpoints (same path, same method)
- Parameter additions to existing endpoints (new query params, new request body keys)
- WebUI-only changes that call existing endpoints

**Examples of what needs auth:**
- `POST /api/v1/tasks/<id>/cancel` — NEW endpoint → needs auth
- `POST /api/v1/tasks/<id>/delete` — NEW endpoint → needs auth
- Adding `?include=playbook_output` query param to existing `GET /api/v1/tasks/<id>` — NO auth needed

### Task Cancel Design (v0.07)

The user prefers a **single** centralized cancel mechanism rather than per-task endpoints:
- One API endpoint for canceling tasks (not per-task)
- The scheduler handles the full lifecycle: stops ansible, marks state, cleans up
- Later cleanup (via stale task detection or manual job cleanup) removes completed/failed task records

This means the UI can have visual cancel/delete buttons, but they should either:
A) Call a single `POST /tasks/cancel` with `{task_id: N}` in the body, OR
B) Simply reset the DB state directly (since WebUI runs on the same host), OR
C) Just be visual indicators until the user approves adding the endpoint

**Current approach for v0.07:** Add UI buttons as placeholders (visual only or direct DB manipulation) until the user explicitly authorizes new API endpoints.
