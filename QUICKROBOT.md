# quickrobot — Architecture & Project Reference

**quickrobot** is a standalone REST API + Web UI controller for managing LLM inference servers, remote nodes, and system services on a local network. Full redesign of keeper_v1 with decoupled API/DB from Web UI, engine type registry, 4-layer config merge chain, Ansible-based deployment, and explicit state machine for instance lifecycle.

> **See also:** `AGENTS.md` for coding rules; `SKILL.md` for API/MCP usage; `docs/TODO.md` for current open tasks; `docs/TODO_done.md` for archived items.

---

## Project Structure

```
<project_root>/
├── AGENTS.md              # Coding rules, file handling, server control
├── QUICKROBOT.md          # This file — architecture and project overview
├── SKILL.md               # API endpoint reference for agents
├── manifest.log           # Append-only file modification tracker
├── data/                  # SQLite database + engine state files
├── db/                    # Database adapter + migration runner
│   ├── adapters/          # Per-entity DB operations (instances, nodes, models, etc.)
│   └── migrations/        # SQL migration files
├── docs/                  # Design specs, phase documentation, roadmap, tests
│   ├── design/            # Architecture and design specification docs
│   └── workflows/         # Process and operational workflow docs
├── engine/                # Engine implementations (subdirectory packages)
│   ├── base.py            # BaseEngine class with state machines
│   ├── llama_server/      # llama.cpp server engine
│   ├── llama_rpc/         # llama.cpp RPC engine
│   ├── iperf3/            # iPerf3 network testing engine
│   ├── universal/         # Generic command/playbook engine
│   ├── subprocess/        # Local subprocess engine
│   ├── quickrobot_api/    # API server engine
│   ├── quickrobot_webui/  # WebUI engine
│   └── quickrobot_mcp/    # MCP server engine
├── lib/                   # Shared libraries (lib_<category>_<purpose>.py)
│   ├── qr_engine_ids.py   # Engine ID/name/category constants (SOT)
│   ├── lib_ansible_runner.py  # Ansible playbook execution
│   ├── lib_cluster_env_builder.py  # RPC cluster config merge
│   ├── lib_config_merge.py      # 6-layer config merge chain
│   ├── lib_system_engine.py     # System engine PID management
│   └── qr_dynamic_inventory.py  # Dynamic Ansible inventory
├── logs/                  # Runtime log files
├── playbooks/             # Ansible playbooks (deploy, manage, validate)
│   ├── common/            # Shared playbook tasks
│   ├── node/              # Node-specific playbooks (validate, discover, scan)
│   └── templates/         # Jinja2 service/env file templates
├── quickrobot/            # Flask app package
│   ├── __init__.py        # App factory + 131 route registrations (~4,670 lines)
│   ├── lib_instances.py   # Instance business logic (~1,670 lines)
│   ├── lib_nodes.py       # Node business logic (~126 lines)
│   ├── lib_responses.py   # Response helpers (~52 lines)
│   ├── routes_instances.py  # Instance route handlers
│   └── routes_nodes.py      # Node/misc route handlers
├── quickrobot.py          # Thin shim entry point (66 lines)
├── quickrobot_webui.py    # WebUI Flask application
├── webui/                 # HTML templates for Web UI pages
├── engine/qr_mcp_server.py  # MCP server
└── OLD_ignore/            # Moved-away files and archived backups
```

### Documentation Index

| File | Purpose |
|------|---------|
| `AGENTS.md` | Coding rules, file handling, server control, Ansible gotchas |
| `SKILL.md` | API endpoint reference + MCP tool usage for agents |
| `docs/TODO.md` | Current open tasks |
| `docs/TODO_done.md` | Archived resolved items |
| `CHANGELOG.md` | Current release entries |
| `CHANGELOG_v005.md` | v0.05-v0.06 changelog |
| `docs/design/ansible_output_format.md` | Ansible JSON output normalization reference |
| `docs/design/sortable_tables.md` | WebUI sortable table pattern (JS + HTML) |

---

## Seed File — Chain-of-Trust Verification

The seed file (`data/_seed/seed_v007.sql`) is a plain SQL file with `INSERT OR REPLACE` statements that populate all seed data: engine_types, engine_configs, engine_presets, engine_models, playbook_registry (with checksum_sha256 + file_size), and benchmark_prompts.

### Verification Flow (fresh DB creation)
1. **Pre-flight:** Load `.quickrobot.env`, validate required keys + seed checksum BEFORE any filesystem change → **HARD EXIT** if mismatch
2. **Apply base schema:** `007_base.sql` creates all tables (idempotent via CREATE TABLE IF NOT EXISTS)
3. **Seed import:** `import_seed_file()` executes seed SQL via `conn.executescript()` — idempotent, ONE TIME ONLY on fresh DB creation
4. **Engine discovery** → auto-registers engine types from `engine/` subdirectories
5. **Auto-provision system instances** (API, WebUI, MCP)

### `.quickrobot.env` Keys for Seed Verification
| Key | Purpose |
|-----|---------|
| `QUICKROBOT_SEED_CHECKSUM` | SHA256 hex digest of seed file |
| `QUICKROBOT_SEED_FILESIZE` | File size in bytes |
| `QUICKROBOT_SEED_MAX_ID` | Max ID range for seed data (default 1000) |

### Startup Flow
| Scenario | Behavior |
|----------|----------|
| DB file does not exist | Warn user, create fresh DB with base schema + seed, backup skipped (nothing to back up) |
| DB file exists | Backup first (timestamped copy), use existing DB in-place, seed skipped |

### Development Workflow: `--mode dev-update`

For development sessions where you need current playbook checksums synced to the DB:

```bash
# Sync disk checksums to DB records (keeps running, mode switches to prod after sync)
python3 quickrobot.py --mode dev-update
```

**What this does:**
1. **`dev-update`:** Scans disk for new playbooks not in DB and registers them, then syncs all checksums from disk to DB records. On mismatches, prints detail diff. After sync, switches `pb_mode` to "prod" and **keeps running** (no longer one-shot — the `--init` flag that used to trigger exit-once behavior is now a no-op).

**Why this is useful:** During active development, playbook files change frequently. The seed file embeds static checksums that become stale. Running `--mode dev-update` syncs them — no manual sed/regex surgery on the seed file needed.

**Result:** Server running in prod mode with correct checksums. All `playbook_registry` entries reflect actual disk file hashes.

> **NOTE:** `--mode dev` and `--mode dev-update` are development tools. Run only on explicit USER REQUEST — not as automatic pre-flight steps during session start.

### Updating Seed File Checksums (Surgical)

When playbook files change during development, update only the checksum entries without editing other seed data:

```bash
# Step 1: Run dev-update to sync DB with current disk checksums
python3 quickrobot.py --mode dev-update

# Step 2: Export updated playbook_registry from fresh DB
sqlite3 data/quickrobot.db "SELECT 'INSERT OR REPLACE INTO playbook_registry ... ' || 
  id || ', ' || file_path || ', ... FROM playbook_registry;" > /tmp/new_registry.sql

# Step 3: Replace last section of seed_v006.sql with updated entries
# (Manual step — or automate by moving playbook_registry to end of seed file)
```

**Future improvement:** Move `playbook_registry` INSERT statements to the end of `seed_v006.sql`. Then `dev-update` can truncate the seed at the last non-registry line and append fresh DB-exported entries — fully automated surgical update.

### Lesson: Concatenating Two Files (head + cat, not python)
When rebuilding the seed file with a refreshed playbook_registry section:
```bash
# Cut old data (lines before playbook_registry section), cat in new export
head -n 200 seed_v006.sql > /tmp/seed_new.sql && cat /tmp/new_registry.sql >> /tmp/seed_new.sql && mv /tmp/seed_new.sql seed_v006.sql
```
Over-engineering with a Python script to "concatenate two ASCII files" is token waste. `head` extracts the prefix, `cat` appends the new section, `mv` replaces in place — all in one pipe chain, zero dependencies, no process startup overhead.

### Key Files
| File | Role |
|------|------|
| `lib/lib_startup.py::import_seed_file()` | Reads seed SQL, executes via `executescript()` on fresh DB creation |
| `lib/lib_startup.py::pre_validate_seed_checksum()` | Validates checksum+size BEFORE any filesystem change |
| `db/adapters/playbooks.py::register_playbook()` | INSERT OR REPLACE for core playbooks (idempotent) |

---

## Core Functionality — Do Not Change Without User Auth

**Rule:** No agent may change core functionality (playbook registration, DB schema, seed file, config merge chain, state machine, playbook lifecycle) without explicit user authorization. This includes:
- Adding automated imports/registrations that were previously disabled for security
- Modifying the playbook registry auto-import behavior (`register_all_core_playbooks` was disabled in prod — do not re-enable without auth)
- Changing seed file format or content
- Adding new columns, tables, migrations without design doc + user approval
- Altering config merge chain layers

When in doubt: check if the feature was previously disabled (commented out, guarded by mode flag) before re-enabling it. If it was disabled for a reason (security, stability), assume it stays disabled until explicitly re-enabled.

## Key Design Patterns

### Ansible Output Normalization
Ansible 2.10+ stores results under `task["hosts"][hostname]` (dict keyed by hostname). The `parse_ansible_json()` function in `lib/lib_ansible_runner.py` normalizes to `task["results"]` (list) for consistent iteration. See `docs/design/ansible_output_format.md` for the full normalization schema.

### Playbook Vars — Fail on Missing, No Silent Defaults
**Rule:** All domain configuration variables used by playbooks MUST be declared in the `vars:` section with NO `default()` filter. If a required var is not provided via extra_vars (from engine_configs, preset, or runtime), the playbook should fail with Ansible's built-in "undefined variable" error — not silently use a hardcoded default that masks configuration errors.

**Why:** Silent defaults hide misconfiguration. When a var is missing, you want the build to FAIL FAST with a clear error, rather than succeeding with wrong values deep in logs.

**Correct pattern:**
```yaml
vars:
    cmake_configure_cmd: "{{ node_build_set_cmd }}"
    cmake_build_cmd: "{{ node_build_run_cmd }}"
    git_repo_url: "{{ git_clone_url }}"
    _node_src_dir: "{{ node_src_dir }}"
```
(Use `_` prefix for intermediate vars to avoid accidental export.)

**Acceptable `default()` usage (infrastructure, not domain config):**
```yaml
- hosts: "{{ inventory_host | default('localhost') }}"   # runner infra var — not user-configurable
  become_user: "{{ remote_node_user | default(omit) }}"    # runner infra var
  changed_when: false
  failed_when: false
```

**When adding a new playbook:** Every domain config var in `vars:` must be checked — if it has `| default(...)`, ask: "Should this really be optional?" If the answer is no, remove the `default()`. Version-bump the playbook header (`# @version:`) to signal the breaking change.

### Dynamic Inventory
All `run_playbook()` calls use dynamic inventory via `lib/qr_dynamic_inventory.py` — no stale `.ini` files. The legacy `generate_inventory()` function was removed in favor of DB-backed JSON inventory that reads node data directly from SQLite at runtime. Every handler passes `inventory_path=None`, which resolves hosts dynamically from the nodes table.

### Running Tasks Tracking (`qr_actions`)
Playbook executions create a `qr_actions` record with `status='running'` BEFORE blocking on playbook execution. On completion, the record updates to `status='completed'` or `status='failed'`. The WebUI `/webui/qr-tasks` page shows live duration with auto-refresh toggle (5s/10s/30s) and stuck detection. This enables detecting stuck processes during 15-30 minute compile/deploy operations without SSHing to remote nodes.

### Sortable Tables
WebUI tables use pattern: `<th class="sortable" data-col="N">` + JavaScript with `qrSettings` (localStorage) persistence. Arrow indicators auto-appear. Numeric sort for specific columns. Used on all WebUI table pages. See `docs/design/sortable_tables.md`.

### CLI Output Convention
Use `print("[qr] message")` prefix for API/server messages. Print only actionable info. Omit decorative separators and section headers — every line costs tokens during coding sessions.

### Console Output — No Hardcoded Values
ALL values in console prints MUST come from actual config sources (`.quickrobot.env`, `_CONFIG`, or SOT constants). See `AGENTS.md` §3 for full details.

### Constant Lookup Rule
Before writing code that references engine types, ports, or versions:
1. **Read** `lib/qr_engine_ids.py` — single source of truth for engine IDs, names, categories, port defaults
2. **Read** `lib/lib_constants.py` — re-exports backward-compatible constants
3. Use SOT constants (`QR_ENGINE_*`, `QR_DEFAULT_*`, `QUICKROBOT_VERSION`)
4. Never hardcode string literals like `"rpc"`, `"8040"`, `"v0.06"`

### `.quickrobot.env` — Single Source of Truth
Host, port, and token configuration for ALL system-managed engines lives in `.quickrobot.env` (human-edited only), NEVER duplicated in code constants or the database.

**Current mapping:**
| Component | Env Keys | Runtime-overridable via API? |
|-----------|----------|-----------------------------|
| API server | `QUICKROBOT_API_HOST`, `QUICKROBOT_API_PORT`, `QUICKROBOT_API_BEARER_TOKEN` | No (env-only) |
| WebUI | `QUICKROBOT_WEBUI_HOST`, `QUICKROBOT_WEBUI_PORT`, `QUICKROBOT_WEBUI_BEARER_TOKEN`, `QUICKROBOT_WEBUI_AUTOSTART` | Partial: `webui_detach`, `webui_autostart` (DB fallback) |
| MCP server | `QUICKROBOT_MCP_HOST`, `QUICKROBOT_MCP_PORT`, `QUICKROBOT_MCP_BEARER_TOKEN`, `MCP_READ/WRITE/PROXY` | Yes: READ/WRITE/PROXY via API |

**Fallback chain:** `.quickrobot.env` (L1) → `engine_configs` table (L2) → instance `config_override` (L3). L3 wins over L2 wins over L1.

### Adding Global Default Values
To add a new global default config for any engine:

**A) Quick runtime change (no restart, no seed update):**
```sql
INSERT OR REPLACE INTO engine_configs (engine_type_id, key, value, description)
VALUES (<engine_id>, 'KEY_NAME', 'value', 'Description here');
```
- `engine_type_id`: 21=llama_server, 22=llama_rpc, 31=iperf3, 12=subprocess, etc.
- Key must use `LLAMA_ARG_` prefix for env variable treatment during config merge
- Empty value (`value=''`) is valid — Jinja2 template filters it out
- Takes effect immediately on next deploy

**B) Full integration (seed file + env):** Add INSERT OR REPLACE to `data/_seed/seed_v007.sql`, update seed checksum in `.quickrobot.env`. Fresh DB creation (automatic, no `--init` flag needed) will include this config.

---

## No Silent Fallback Rule

**ALL code MUST FAIL HARD when it cannot find the resource it needs. NEVER silently fall back to hardcoded strings, alternative hosts, localhost, or default values that mask real problems.**

When a function needs to look up a host, port, engine name, playbook, config value, etc.:
- If the lookup succeeds → use the found value
- If the lookup fails → raise `SystemExit(1)` or return an error response — NOT a fallback

### Examples of violations (anti-patterns)
| Violation | Why it's bad |
|-----------|--------------|
| Dynamic inventory with duplicate hostnames silently picking the last one | Ansible connects to wrong port, errors are cryptic and take hours to debug |
| `ssh_port` defaults to 22 when not in DB | Hides that a node was configured for a non-standard port |
| `ansible_user` falls back to `DEFAULT_ANSIBLE_USER` when missing from node record | Silent misconfiguration — connects with wrong credentials |
| `hostname` used as dict key in inventory, later entries overwrite earlier | Same hostname across multiple nodes → silent data loss |
| Fallback to localhost when remote host is unreachable | Silent operation on wrong machine |

### Examples of correct behavior
| Situation | Correct action |
|-----------|---------------|
| No active nodes found for a requested hostname | Fail immediately — no fallback to other hosts |
| Duplicate hostname in inventory | `SystemExit(1)` with list of conflicting node IDs |
| Missing playbook file | `SystemExit(1)` in prod mode (already implemented) |
| Hostname lookup returns nothing | Raise error, NOT "try localhost" |

### Concrete fix: dynamic inventory duplicate detection
`lib/qr_dynamic_inventory.py::build_inventory()` now groups nodes by inventory name and raises `SystemExit(1)` if any name maps to multiple nodes. No fallback — the ambiguity is surfaced immediately rather than silently resolved.

---

## SSOT Principle — No Local Redefinitions

### Rule: Define Once, Import Everywhere

Every constant that describes **what the system does** (engine names, IDs, display labels, categories, port defaults, aliases) must be defined in a single canonical file and imported by all consumers. **Never redefine the same constant in multiple files, functions, or modules.**

### Why This Matters

When the same concept is defined in two places:
- **Change drift:** Update one, forget the other → silent bugs
- **Identity confusion:** Two different string literals (`"qr_api"` vs `"quickrobot-api"`) represent the same entity but don't compare equal
- **Maintenance tax:** Every feature requires touching N files instead of 1

### Concrete Examples (from v0.07)

| Constant | SSOT File | Bug When Redefined Locally |
|----------|-----------|--------------------------|
| `_QR_ENGINES` (engine list) | `lib/qr_engine_ids.py` | Duplicate engine_type_id=32 for scheduler created because auto-register ran before seed import — two entries, one skipped nav override |
| `_QR_NAME_ALIASES` | `lib/qr_engine_ids.py` | `"rpc"` alias handled specially in webui but not in other modules → nav inconsistency |
| Nav display names | `lib/qr_engine_ids.py: _QR_NAV_DISPLAY_NAMES` | `if/elif` chain in `render_nav()` — adding a new engine required editing 3 locations (display dict, section map, no-config set) |
| `_SHORT_NAME_ALIASES` | `lib/qr_engine_ids.py: _QR_NAV_SHORT_ALIASES` | Hardcoded tuple `("qr_api", "qr_webui", "qr_mcp", "rpc")` in webui — didn't match alias system in engine registry |
| `_QR_SYSTEM_NAMES` | `lib/qr_engine_ids.py` (auto-derived from `_QR_ENGINES`) | Inline tuple `(QR_ENGINE_API_NAME, QR_ENGINE_WEBUI_NAME, ...)` in render_nav — could drift from actual engine list |

### Pattern: Auto-Generated Maps from a Single Data Source

The SSOT file uses `_QR_ENGINES` as the single data source. All derived maps are generated at import time:
```python
# Single data source
_QR_ENGINES = [
    (1,  "quickrobot-api",  "system"),
    (4,  "quickrobot-scheduler", "system"),
    # ...
]

# Auto-derived — no manual sync needed
QR_ENGINE_NAME_MAP = {_id: name for _id, name, _cat in _QR_ENGINES}
QR_SYSTEM_IDS = tuple(_id for _id, _, c in _QR_ENGINES if c == "system")
_QR_SYSTEM_NAMES = tuple(name for _, name, cat in _QR_ENGINES if cat == "system")
```

Adding a new engine: one tuple in `_QR_ENGINES` → all maps update automatically. **Zero risk of drift.**

### Anti-Pattern to Avoid

**BAD — Multiple local redefinitions:**
```python
# In webui/render_nav.py
_SHORT_NAME_ALIASES = ("qr_api", "qr_webui", "qr_mcp", "rpc")   # Line A
_NAV_DISPLAY_NAMES = { ... }                                     # Line B
_NAV_NO_CONFIG = {"subprocess", "universal", "iperf3", "llama_rpc"}  # Line C
```

**GOOD — Single import from SSOT:**
```python
from lib.qr_engine_ids import _QR_NAV_SHORT_ALIASES, _QR_NAV_DISPLAY_NAMES, _QR_NAV_NO_CONFIG
```

### Guideline: Before Adding a Local Constant

Ask: **"Is this describing a system entity (engine, node, state, port) or an implementation detail (magic number, timeout value)?"**
- If **entity**: add to `lib/qr_engine_ids.py` or `lib/lib_constants.py`, then import
- If **implementation detail** (e.g., retry count, sleep duration): local is fine

### Guideline: Before Writing an `if x == "value"` Check

Ask: **"Is this string a known system constant or a magic literal?"**
- If it matches an engine name, state name, or config key → use the SOT constant
- If it's a random string like `"foobar"` → local is fine

### Guideline: Before Creating a New Dict of Overrides

Ask: **"Will this dict grow with every new engine?"**
- If yes → move to SSOT file as an auto-generated or explicitly maintained map
- Example: `_QR_NAV_DISPLAY_NAMES` started in webui (local), moved to `qr_engine_ids.py` (SSOT) after it was discovered that 3 separate dicts lived in one function

---

## Staged Playbook Runner Chain (RUNNER-1)

The monolithic `deploy_llama_server.yml` is decomposed into 6 focused, independently retryable stages orchestrated by `PlaybookRunner`. Full design spec (legacy) archived at `OLD_ignore/design_runner_chain.md`.

### Architecture
- **Command side:** `lib/lib_runner.py::PlaybookRunner` — creates jobs, runs playbooks sequentially, writes results
- **Query side:** API routes read from `jobs` + `tasks` tables (<10ms per call)
- **Scheduler:** `engine/quickrobot_scheduler/__main__.py` polls for queued tasks, executes with retry logic

### Stage Chains (per engine type)
Stages use **playbook IDs** resolved from `playbook_registry` at runtime via `_resolve_playbook()`. All stage names and state mappings are centralized in `lib/qr_engine_ids.py` (`QR_STAGE_*` constants + `STAGE_STATE_MAP`).

| Engine | Stages (7) |
|--------|------------|
| llama_server | preflight → deps → source → compile → config_svc → config_env → start |
| llama_rpc | preflight → deps → source → compile → config_svc → config_env → start |
| iperf3 | preflight → deps → config_svc → config_env → start |
| universal | preflight → deps → config_svc → config_env → start |

### Job Types (SSOT in `qr_engine_ids.py`)
All job types are defined as `_QR_JOB_TYPES` tuple. Known types: `deploy`, `rebuild`, `reconfigure`, `undeploy`, `bind`, `unbind`, `start`, `restart`. Unknown types raise `ValueError` in `_get_stage_chain()` — no silent fallback to full deploy chain.

- **deploy**: Full staged chain (all 7 stages)
- **rebuild**: Source + compile + config only (skip deps, skip preflight)
- **reconfigure**: Config-only env update (single `config_env` stage — no root needed, no systemctl reload)
- **start**: Single task — `service_start.yml` (just start the systemd unit)
- **restart**: Two tasks — `service_stop.yml` → `service_start.yml` (+ optional RPC health_probe)
- **undeploy**: Health probe (RPC) → stop + cleanup
- **bind**: Stop + config_svc + config_env + start (systemd unit file changes with new RPC args)
- **unbind**: Same as bind — both config_svc AND config_env needed (current code runs both for safety)

### Config Split Rationale: config_svc vs config_env

The `config_svc` / `config_env` split enables **fast userspace config changes** without root privileges:

| Operation | Needs config_svc? | Needs config_env? | Root required? |
|-----------|------------------|-------------------|----------------|
| Preset change (model, CLI args) | No | Yes | No (SSH user) |
| Cluster config change (split, tensor_split) | No | Yes | No (SSH user) |
| Bind/unbind RPC bindings | Yes | Yes | Yes |
| Start-on-boot toggle | Yes | No | Yes (systemd) |
| Restart policy change | Yes | No | Yes (systemd) |
| Full deploy | Yes | Yes | Yes |

**Why split?** The systemd ExecStart reads CLI args from the env file (`$QR_CLI_ARGS_JOINED`). Preset changes, cluster config updates, and model param changes only modify the env file — no systemctl reload needed. Config_env runs as SSH user (no-become), is faster than config_svc, and avoids unnecessary service unit regeneration.

**Why bind/unbind use both:** When RPC bindings change, the systemd unit file's ExecStart line changes (new `-rpc host:port` args). The env file also changes (merged CLI args in `QR_CLI_ARGS_JOINED`). Both must be rewritten. Technically, since the unit reads from env, config_env alone would suffice — but current code runs both for safety.

**Legacy `deploy_config.yml`:** The 83-line hybrid playbook that writes BOTH env + service is deprecated. Archival details at `OLD_ignore/playbook_optimization_v008.md`. It creates confusion because it duplicates the split pair's functionality.

### New DB Tables
- `jobs` — parent job record (deploy, restart, benchmark_run, etc.)
- `tasks` — per-stage progress within a job
- `playbook_runs` — raw ansible JSON output per stage
- `engine_job_types` — engine-registered job type registry
- `scripts` + `script_steps` — dynamic multi-step scripting (SCRIPT-1)

### CONFIG-1: ENV-Driven CLI Args
Systemd ExecStart now uses `$QR_CLI_ARGS_JOINED` from the env file instead of inline Jinja2 args:
```ini
# Before: ExecStart=... {{ merged_cli_opts | join(' ') }}
# After:  ExecStart=... $QR_CLI_ARGS_JOINED
```
Config changes only require updating the env file — no `daemon-reload` needed. Env templates generate `QR_CLI_ARGS_JOINED` from `merged_cli_opts`.

**Phase 1 Complete (2026-06-17):** All 4 engine types now use uniform `$QR_CLI_ARGS_JOINED` pattern:
- Staged chain `config.yml` uses SSOT-compliant template routing dict (mirrors `_QR_ENGINES` in `lib/qr_engine_ids.py`)
- `llama_server_env.j2`, `rpc_env.j2`, `iperf3_env.j2`, `universal_env.j2` — all write `QR_CLI_ARGS_JOINED`
- `llama_server_service.j2`, `rpc_service.j2`, `iperf3_service.j2`, `universal_service.j2` — all read `$QR_CLI_ARGS_JOINED`
- Legacy `deploy_iperf3.yml` now writes env file + uses `$QR_CLI_ARGS_JOINED` (previously skipped env)
- Legacy `deploy_universal.yml` fixed extra_vars mismatch (`merged_cli_opts`/`merged_env` instead of `cli_args`/`env_vars`)
- `lib/qr_engine_ids.py` SSOT harmonized: added `QR_ENGINE_UNIVERSAL_NAME`, `QR_ENGINE_IPERF3_NAME`, `QR_ENGINE_SUBPROCESS_NAME`, `QR_ENGINE_SCHEDULER_NAME`; removed inconsistent `QUICKROBOT_SCHEDULER_NAME` prefix; replaced all `QR_ENGINE_QUICKROBOT_*` usages with short aliases (`QR_ENGINE_API`, `QR_ENGINE_WEBUI`, `QR_ENGINE_MCP`)

### STATUS-1: Unified Status Endpoint
`GET /instances/<id>/status` returns engine-specific data, available actions, warnings, and metadata in a standardized format:
```json
{
  "data": {
    "id": 106, "state": "running", "engine_type_name": "llama_server",
    "engine_data": {"port_assigned": 8080, "node_hostname": "remote-node.lan"},
    "actions": [{"name": "stop", "label": "Stop"}, ...],
    "warnings": [],
    "_meta": {"valid_next_states": [...], "is_transitioning": false}
  }
}
```
Each engine class provides `get_instance_status(db_path, instance_id)`.

### Completed
- **DONE 2026-06-16:** Wired `api_deploy_instance()` to use `PlaybookRunner.chain()`. Includes: node build lock for shared cmake builds, RPC binding warnings, UUID preflight, skip_build → reconfigure chain routing. Returns same response shape as legacy path for WebUI compat.
- **DONE 2026-06-16:** Added `chain()` method to `PlaybookRunner` in `lib/lib_runner.py`. Creates jobs/tasks, executes stages sequentially, handles retries via `_NODE_BUILD_LOCK`, returns result matching `api_deploy_instance` format.
- **DONE 2026-06-16:** Fixed `_resolve_playbook()` — was using `lstrip("playbooks/")` which stripped chars individually (e.g., "playbooks/preflight.yml" → "reflight.yml"). Replaced with `removeprefix("playbooks/")`.
- **DONE 2026-06-16:** WebUI incremental migration: added `initStatusActions()` JS function that enhances Jinja2-rendered action buttons with STATUS-1 data from API. Generic rendering for new actions not in Jinja2 template.
- **DONE 2026-06-16:** Added universal engine to `DEFAULT_STAGE_CHAINS` (preflight→deps→config→start). Registered staged playbook checksums via migration 006_staged_playbooks.sql.

### Completed 2026-06-16
- **DONE:** STATUS-1: Subprocess engine `get_instance_status()` method added + dispatcher registration
- **DONE:** STATUS-1: WebUI `initStatusActions()` JS row lookup fixed (tr lacked data-inst-id attribute)
- **DONE:** STATUS-1: instances.html action buttons render correctly from Jinja2 defaults + STATUS-1 JS enhancement
- **DONE:** Playwright tests 3 and 6 completed (job expand + herd RPC bindings)

### Completed 2026-06-20 (RUNNER-1 Enhancements)
- **RUNNER-SSOT:** All job types, stage names, `STAGE_STATE_MAP`, `SKIPABLE_STAGES` centralized in `qr_engine_ids.py`; `lib_runner.py` imports from SSOT instead of hardcoding
- **PLAYBOOK-ID-REFS:** `DEFAULT_STAGE_CHAINS` uses registered playbook IDs (not file paths); `_resolve_playbook()` resolves by ID first via `playbook_registry`; task integrity check checks by ID first
- **START-JOB-TASKS:** `start` job type returns 1 task (`service_start.yml`), `restart` returns 2 tasks (`service_stop.yml` + `service_start.yml`); unknown job types raise `ValueError`
- **PLAYBOOK-ID-FIX:** Reconfigure button fixed: `UPDATE_CONFIG_V1` → `update_config` (4 refs in `routes_instances.py`)
- **QR-API-CT:** `qrApi()` in `base.html` auto-injects `Content-Type: application/json` for POST/PUT/PATCH/DELETE — fixes 415 errors
- **PLAYBOOK-V2:** Created `playbooks/core/service_stop.yml` (v2), registered as `service_stop` in playbook_registry
- Seed file updated (`seed_v007.sql` appended service_stop entry); `.quickrobot.env` checksum updated
- Fresh DB creation verified: 26 tables, 43 playbooks, 39 models, 50 presets all seeded correctly

### Remaining
- SKILL.md / MCP skill: Add new API endpoints (job/task queries, /instances/<id>/status)
- **PLAYBOOK-OPT-2** (v0.08): Per-node build lock + deploy chain refactor — verify task ordering and seed file entry

---

## System Architecture

### Shared Build Paths
All llama.cpp instances share ONE clone + build per node:
- **Source:** `/opt/quickrobot/llama.cpp` (one git clone per host)
- **Build:** `/opt/quickrobot/llama.cpp/build` (one cmake --build per host)
- **Binary (llama_server):** `{build}/bin/llama-server`
- **Binary (rpc):** `{build}/bin/rpc-server`

Paths stored in `engine_configs`, passed to playbooks via extra_vars. Only one cmake build at a time across all instances on the same node.

### Playbook Registry (43 registered playbooks)

Registry is dynamically populated from playbook headers (`# @playbook_id:`) and disk files. Two naming conventions coexist:

| Convention | Example | Description |
|------------|---------|-------------|
| `_V1` suffix | `APT_UPDATE_V1`, `DEPLOY_LLAMA_SERVER_V1` | Legacy naming, mostly node actions |
| Descriptive | `preflight_check`, `service_start`, `deploy_config_env` | Modern naming, used by RUNNER-1 staged chain |

**Core playbooks used by staged chain:**

| Playbook ID | File Path | Description |
|-------------|-----------|-------------|
| preflight_check | `playbooks/core/preflight_check.yml` | Node connectivity + OS detection |
| install_deps | `playbooks/core/install_deps.yml` | Install build dependencies |
| source_llama | `playbooks/llama/source_llama.yml` | Git clone llama.cpp source |
| build_compile_llama | `playbooks/llama/build_compile_llama.yml` | cmake + build |
| deploy_config_service | `playbooks/core/deploy_config_service.yml` | Write systemd unit file |
| deploy_config_env | `playbooks/core/deploy_config_env.yml` | Write env file + reload daemon |
| service_start | `playbooks/core/service_start.yml` | Stop + start service + health probe |
| service_stop | `playbooks/core/service_stop.yml` | Stop service + verify port closed |
| rpc_health_check | `playbooks/core/rpc_health_check.yml` | gRPC health probe for RPC nodes |

**Resolution priority:** ID lookup (preferred) → tag-based AND-match → file_path exact match.

### Instance & Node ID Ranges

| Range | Type | Description | Protection |
|-------|------|-------------|------------|
| Node 1 | System localhost | Machine where quickrobot API runs. Never removable via API (`api_delete_node` guard). | API-level `node_id == 1` check |
| Nodes 2-99 | Remote nodes | Created by user for remote targets. | Deletable via API |
| Instance 1-4 | System instances | Auto-provisioned: api(1), webui(2), mcp(3), scheduler(4). Protected from delete. | `system_managed=1` flag |
| Instance 5-99 | User/system mix | May be system-managed (protected) or user-owned (deletable). Check `system_managed` column. | API-level `check_system_managed()` check |
| Instance 100+ | User instances | Always user-owned, deletable via API. | Deletable via API |

**Key rule:** All protection is driven by the `system_managed=1` DB column on instances and the hard-coded `node_id == 1` check on nodes. The ID ranges are conventions, not enforced by DB constraints.

### System-Managed Engines
System-managed instances use PID-based lifecycle via `lib/lib_system_engine.py`:

| Engine | Type ID | Port Source | Lifecycle |
|--------|---------|-------------|-----------|
| quickrobot-api | 1 | `QUICKROBOT_API_PORT` | tmux session `qr_api` |
| quickrobot-webui | 2 | `QUICKROBOT_WEBUI_PORT` | Subprocess (PID-in-DB) |
| quickrobot-mcp | 3 | `QUICKROBOT_MCP_PORT` | Subprocess (PID-in-DB) |
| quickrobot-scheduler | 4 | N/A | Subprocess (PID-in-DB, no network endpoint) |

All provisioned with `node_id=1` via `_auto_provision_system_instances()`. Host/port removed from `engine_configs` table — stored in `.quickrobot.env`.

**System Instance Protection:** IDs 1, 2, 3, 4 protected from delete/deploy/config-change (returns 409 `SYSTEM_MANAGED_INSTANCE`). Use `POST /instances/<id>/restart_system` for restart.

#### Startup Behavior — Pre-Flight Port + Process Scan
On API startup, each system engine undergoes a pre-flight check before attempting to start (`_start_system_engine()` in `lib_startup_pipeline.py`):

1. **Port check** via `ss -tlnp`: verifies the assigned port is free (WebUI 8038, MCP 8040; Scheduler has no port)
2. **Process scan** via `ps aux`: grep for known Python file names per engine (`quickrobot_webui.py`, `qr_mcp_server.py`, `quickrobot_scheduler`)
3. **DB PID check**: verifies `pid_last_known` status for additional context

If ANY conflict detected, prints FATAL message with PID + command details and **aborts** that engine's auto-start (does NOT attempt to kill or restart). The agent reads the report and takes action (kill conflicting processes, then restart API).

```
[qr] FATAL: WEBUI pre-flight conflict: Port 8038 occupied by python3 pid=214601
[qr] FATAL: WEBUI pre-flight conflict: Stale process found: pid=214601 cmd='...'
[qr] [WEBUI] Auto-start ABORTED — resolve conflicts before restarting
```

Once conflicting processes are killed and startup re-tried, the scan passes and engines start fresh with latest code. **This is startup-only behavior.** Explicit `POST /instances/<id>/start` and `POST /instances/<id>/restart_system` endpoints retain their existing DB PID check logic.

#### Health Check & Self-Termination
All system-managed subprocesses run a periodic health check thread that polls `/api/v1/app/status` every 10s. After 2 consecutive failures (5s retry delay between retries, 10s total self-kill time), the subprocess calls `os._exit(1)` to terminate immediately. This prevents zombie accumulation when the API dies:

```bash
# Process listing — clean commands, no redundant CLI args
$ ps aux | grep quickrobot_webui
python3 .../quickrobot_webui.py --host 127.0.0.1 --port 8038   # <-- no --api-host/--api-token

$ ps aux | grep qr_mcp_server
pipx/python .../qr_mcp_server.py                               # <-- reads all from env vars

$ ps aux | grep quickrobot_scheduler
python3 -m engine.quickrobot_scheduler --db /path/to/quickrobot.db  # <-- interval from DB (1s), not hardcoded
```

**Log files:** Each engine writes to its own `logs/{engine}.log` file with structured startup banners and FATAL exit messages:
```
[2026-06-23T13:39:13Z] scheduler STARTUP: pid=106098 db=/path/api=127.0.0.1:8039 interval=1s log_level=info
[2026-06-23T13:46:55Z] mcp: [qr] FATAL: API unreachable after 3 attempts. Exiting.
```

**`--mode exit`:** `python3 quickrobot.py --mode exit` starts the API, spawns all system engines, prints their PIDs, then exits before Flask's main loop. Useful for testing zombie self-termination behavior. The API exits but system engines continue running (with their health checks) and self-terminate after ~9s when they can't reach the dead API.

#### Environment Variable Whitelist
System subprocesses receive a minimal, engine-scoped env whitelist instead of inheriting the full environment:

| Layer | Vars | Scope |
|-------|------|-------|
| Base (all 3 engines) | `PATH`, `HOME`, `LANG`, `LC_ALL`, `QUICKROBOT_API_BEARER_TOKEN`, `QUICKROBOT_API_HOST`, `QUICKROBOT_API_PORT`, `QUICKROBOT_CONSOLE_DEBUG_LEVEL`, `QUICKROBOT_ANSIBLE_LOG_LEVEL`, `QUICKROBOT_LOG_PATH` | All subprocesses |
| WebUI extras | `QUICKROBOT_WEBUI_HOST`, `QUICKROBOT_WEBUI_PORT` | WebUI only |
| MCP extras | `PYTHONPATH`, `QUICKROBOT_MCP_HOST`, `QUICKROBOT_MCP_PORT`, `QUICKROBOT_MCP_READ/WRITE/PROXY`, `QUICKROBOT_MCP_ALLOWED_HOSTS` (exact host:port strings, no wildcards), `QUICKROBOT_MCP_DISABLE_DNS_REBINDING`, `QUICKROBOT_MCP_CORS_ORIGINS` | MCP only |

Sensitive tokens (e.g., `QUICKROBOT_WEBUI_BEARER_TOKEN`) are NOT passed to subprocesses — they're used at the reverse-proxy level. API token (`QUICKROBOT_API_BEARER_TOKEN`) IS passed since all engines need it to authenticate with the API.

### MCP Server — SSE Transport (CRITICAL, 2026-06-24)

The MCP server (`engine/qr_mcp_server.py`) uses FastMCP with **traditional SSE transport** (`sse_app()`), NOT streamable HTTP transport. This is required for compatibility with the llama.cpp web UI MCP client.

**Key insight:** FastMCP supports two transport modes:
- `sse_app()` → traditional SSE: GET `/sse` establishes connection, server immediately pushes MCP handshake events (server_info, tools/list). The client then POSTs messages to `/messages/?session_id=XXX`. **This is what the llama.cpp web UI expects.**
- `streamable_http_app()` → streamable HTTP: Client POSTs initialize JSON to `/sse`, server responds with SSE events on the same connection. **Does NOT work with llama.cpp web UI** because it never sends a POST to `/sse` — it only does GET.

**Working configuration** (verified 2026-06-24):
```python
# In qr_mcp_server.py startup block:
mcp.settings.json_response = False      # Return responses as SSE events
# DO NOT set these — they're for streamable HTTP, not traditional SSE:
# mcp.settings.streamable_http_path = "/sse"  # ← WRONG for llama.cpp UI
# mcp.settings.stateless_http = True          # ← WRONG for llama.cpp UI

fastmcp_app = mcp.sse_app()               # ← CORRECT: traditional SSE transport
```

**MCP protocol flow:**
1. Client GET `/sse` → Server returns `event: endpoint` with `/messages/?session_id=XXX`
2. Client POST to `/messages/?session_id=XXX` with JSON-RPC initialize message
3. Server responds via SSE on the original connection with `server_info` + `tools/list` events
4. Subsequent messages (tool calls) follow the same pattern: POST → SSE response

**CORS:** Configured in middleware chain before FastMCP app:
```python
cors_app = CORSMiddleware(fastmcp_app,
    allow_origins=["*"],              # HARDCODED — not from env vars
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True)
```

### Development tmux Session (qr_api)

Quickrobot-api runs in a tmux session (`qr_api`). Sessions use default server socket (no custom `-S` binding).

**Session:** `qr_api`  
**Scope:** Development use only — production deployment uses systemd.

#### Session lifecycle

Create the session with a bare shell, then start quickrobot via `send-keys`:

```bash
# Initial setup (once)
tmux new-session -d -s qr_api          # bare shell, no command

# Start quickrobot (any time)
tmux send-keys -t qr_api 'cd /CORE/projects/quickrobot && python3 quickrobot.py' C-m

# Check status
tmux has-session -t qr_api              # exit 0 = alive
curl -s http://127.0.0.1:8039/api/v1/app/status                   # API health check
```

#### Reading tmux output

`capture-pane -p` only captures what's visible on screen (the current viewport). To scrape the full scrollback buffer:

```bash
# Visible screen only (may be blank if process exited)
tmux capture-pane -t qr_api -p

# Full history from beginning (-S - = start at line 0)
tmux capture-pane -t qr_api -p -S - | tail -60
```

Use `-S -` whenever you need the actual output, not just the current screen.

#### Inspecting all tmux servers

```bash
# Query the qr_api session
tmux list-sessions

# List all tmux sessions on the system
tmux list-sessions -a 2>/dev/null || true
```

**MCP WRITE→READ Implication:** `reads_allowed = allow_reads or allow_writes or allow_proxy`. If WRITE or PROXY is set, READ is implicitly enabled.

### Subprocess Engine (ID=12)
Manages arbitrary local processes via `subprocess.Popen` on the API host. No playbook, no systemd, no remote node. Config: `executable` (required), `working_dir`, `cli_args`, `env_vars`, `host`, `port`. Health check via HTTP probe. State machine: unconfigured → deployed → starting → running → stopping → stopped | error.

---

## Configuration & Merge Chains

### Config Mutation Rules
- `config_override` uses **merge semantics** — partial PUTs update only specified keys, never replace entire dict
- All config values traceable to DB column or API response field
- No hardcoded fallback values in WebUI display or API status endpoints

### 6-Layer Config Merge Chain (deploy time)
The deploy function resolves configuration through six layers:
1. **Engine defaults** — `engine_configs` table, global for engine type
2. **Node defaults** — `node_configs` table, per-node overrides
3. **Preset template** — `engine_presets.config_template` JSON (env + cli_opts + model_id)
4. **Model params** — resolved from `engine_models` via preset's `model_id` FK
5. **Cluster binding** — RPC bindings, split, experts, draft values
6. **Instance override** — `instances.config_override` (FINAL layer)

### Llama-server/RPC Host & Port Merge Chain
Host and port merge differs by engine type:

| Source | Priority | Where it goes | How it reaches ExecStart |
|--------|----------|---------------|-------------------------|
| Instance `config_override.env.LLAMA_ARG_HOST/PORT` | Highest (Layer 5) | merged["env"] | Via `build_llama_server_env` → base_cli |
| Preset `config_template.env` | Layer 3 | merged["env"] | Same as above |
| Engine configs (`LLAMA_ARG_HOST`/`PORT`) | Layer 1 (lowest) | merged["env"] | Converted to `-H`/`-p` in builder |
| Node IP (`ipv4_address`/`ipv6_address`) | Deploy-time fallback | — | Used when all above are empty |
| Port from `port_assigned` column | N/A | Instance record | Used in builders for `-p` arg |

**Key insight:** LLAMA_ARG_HOST/PORT values live in `merged["env"]` (not `cli_opts`). The cluster env builders convert them to CLI args:
- `build_rpc_server_env`: Produces `-H {host} -p {port} -d {device}`
- `build_llama_server_env`: Produces base_cli with `-H`/`-p`

### SSH Config Resolution Flow
`ansible_user` and `ansible_key_path` follow a layered resolution:
1. **Request body** (explicit per-host override)
2. **`.quickrobot.env`** system defaults
3. **Fallback defaults:** `getpass.getuser()` for user, `None` (ssh-agent) for key

Resolution happens once at node creation time — values stored in DB columns. Changing `.quickrobot.env` does NOT affect existing nodes.

---

## RPC Cluster Architecture

### Instances Table Columns (Cluster)
- `rpc_bind_ids`: JSON array of RPC instance IDs bound to a llama-server
- `split`: Server split value (INTEGER, default 100, CHECK 0-100)
- `experts`: Per-instance MoE expert count (INTEGER, default 100, CHECK 0-100)
- `draft`: Per-instance draft model token count (INTEGER, default 100, CHECK 0-100)

### Tensor Split Computation Rules
- Split = NULL → no GPU slot: `tensor_split = [rpc1_split, rpc2_split, ...]`
- Split = 0 → GPU slot with value 0: `tensor_split = "0,rpc1_split,..."`
- Split = N (N>0) → GPU slot with value N: `tensor_split = "N,rpc1_split,..."`

Each RPC contributes its own split value; NOT repeated for every device.

**Important:** Use `inst.get("split") is not None` (not `if inst["split"]`) to distinguish split=0 from NULL — `bool(0)` is False.

### Parallelism Modes
| split_mode | Meaning | tensor_split example (3 RPCs) |
|------------|---------|------------------------------|
| `layer` | Each RPC handles a fraction of layers — **ingress is sequential** | `"100,100,100"` |
| `tensor` | True tensor parallelism — weight matrices split across layers (needs CUDA/vulkan) | `"N,N,N"` |

Layer mode: first RPC processes all tokens, passes to next — no parallel throughput gain. Tensor mode distributes actual weight computation in parallel. Most deployments use layer mode for memory offload, not speed.

### Cluster Device Naming (RPC Nodes as GPU Devices)

The core cluster mechanism: **RPC instances act as distributed GPU devices**. The llama-server's `-dev` CLI arg combines local GPU device names with RPC node device identifiers:

```
-dev Vulkan0,RPC0,RPC1,RPC2   ← local GPU + 3 remote RPC nodes
```

Each RPC node gets an auto-assigned device name (`RPC0`, `RPC1`, …) based on its position in the `rpc_bind_ids` array. The cluster env builder generates this combined device list by merging:
1. Local GPU devices from the instance's `LLAMA_ARG_DEVICE` config (e.g., `Vulkan0`)
2. RPC node device names (`RPC0`, `RPC1`, …) from bound RPC instances

**Failure mode**: If any bound RPC service is down, llama-server fails to connect during startup and crashes immediately — the entire instance goes into error state. All RPC nodes must be running before starting a bound server instance.

 ### Herd Page (`/webui/rpccluster`)
 - Left panel: llama-server list with state, split_mode, RPC count, tensor_split
 - Right panel: selected server detail — split config (mode + value), RPC bindings table (inline edit), deploy + restart actions
 - API endpoints: GET /rpccluster/summary, PUT/DELETE bind-rpc, PATCH split-mode, PUT split/experts/draft
 - All herd settings save to DB only (`config_override` for CLI flags/ENV overrides, instance columns for split/experts) — use **Deploy Config** button to push changes to remote node via RUNNER-1 staged chain

---

## Operational Rules

### DB Creation Guard (auto)
When the DB file does not exist at startup: quickrobot prints a warning and creates a fresh database with base schema + seed data. This is NOT a restart; it's a full reset — all instances, nodes, ansible actions, and build history are lost. See `AGENTS.md` §7.

> **Note:** `--init` flag is now deprecated (accepted as no-op for backward compat). DB creation is automatic based on file existence.

### Process Kill Guard
**NO agent may kill the API process (`kill <pid>`) without explicit user confirmation.** Killing the API also kills all running ansible-playbook subprocesses (compiles, deploys in progress). The `update_and_compile` playbook can take 15-30 minutes. Before killing: verify no long-running operations via instance states (`updating`, `configuring`, `deploying`, `starting`, `stopping`, `loading`, `compiling`). See `AGENTS.md` §7.

### Compile Verification Rule
When instances show state `updating` after triggering `update-build`: **do NOT assume the build is stuck.** The shared cmake build can take up to 30 minutes. Before reporting or acting: SSH to remote nodes and check for active cmake/compile processes (`ps aux | grep cmake`, CPU usage via `top`). Only declare a build "stuck" if no active compile processes after 15+ minutes.

### Admin Node Toggle
Set `is_active=0` on a node to block ALL operations (deploy, restart, delete, apt-update, etc.). Returns `NODE_INACTIVE` error. This is the admin "do not touch" flag, separate from ping connectivity (`ping_state`).

---

### Preset Benchmarking — Swap Presets, Not Instances
**Do NOT create a new instance for each preset benchmark.** Reuse the 5 existing test server instances (one per remote node) and switch presets via `change_preset(instance_id, preset_id)` or `PUT /instances/<id>` with `{preset_id: N, skip_build: true}`. 

Creating a new instance changes port assignment, RPC bindings, build state, and potentially node context — resulting in benchmark data that compares different instances rather than the same preset under consistent conditions.

**Correct workflow:**
1. Identify test server instances (one per host, running on port 8080)
2. For each preset: call `change_preset(instance_id, preset_id)` → wait for `running` state → run benchmark
3. Preset changes use the BC-1 fast path (config_env only, no git clone or cmake build)
4. All other context (port, node hardware, build number, RPC bindings) stays identical

---

## Security

### Root Guard — All System Engines
All 6 server entry points refuse to run as root via `os.getuid() == 0` guard:

| Server | File | Guard Location |
|--------|------|----------------|
| API Flask app | `quickrobot.py` | Line ~47: in `__main__` block before Flask startup |
| API self-monitor engine | `engine/quickrobot_api/__init__.py` | In `QrApiEngine.__init__()` |
| WebUI standalone | `quickrobot_webui.py` | Line ~54: module-level guard |
| WebUI engine class | `engine/quickrobot_webui/__init__.py` | In `QuickrobotWebUIEngine.__init__()` |
| MCP server | `engine/qr_mcp_server.py` | Start of `if __name__ == "__main__"` block |
| Scheduler | `engine/quickrobot_scheduler/__main__.py` | Top of `main()` function |

Pattern: `os.getuid() == 0 → print("this robot won't run as root") → sys.exit(1)`.
Non-interactive HTTP servers should not run as root — if invoked with sudo, they exit immediately.

### Snakeoil Security Model (v0.07)
quickrobot uses a **minimal security model** designed for trusted local networks:
- NO API keys in production (only `QUICKROBOT_*_BEARER_TOKEN` defined but not enforced)
- NO SSL/TLS — plain HTTP on all 3 ports (API, WebUI, MCP)
- NO mTLS or VPN requirement
- CORS enabled with wildcard origins (`*`) by default
- MCP DNS rebinding protection configurable via `QUICKROBOT_MCP_DISABLE_DNS_REBINDING`
- RPC servers bind to `0.0.0.0` by default — use per-instance override for local-only binding
- Playbook integrity: `--mode dev` warns on checksum mismatch; prod mode kills API on mismatch ("Bad Robot!")

---

## API Gotchas & Patterns

### RPC Servers ≠ HTTP
RPC servers use a custom binary protocol, NOT HTTP. Do NOT use `curl /health` on RPC endpoints — it will hang or fail silently. Verify RPC health by:
- SSH to node → check CPU usage via `ps aux | grep rpc-server`
- Check inference results from the bound llama_server (non-empty response = working)
- Llama-server endpoints use HTTP and support `/health`

### Restart vs Deploy — Critical Distinction
| Endpoint | What it does | Use when |
|----------|-------------|----------|
| `POST /instances/<id>/restart` | Stop + start with **existing** config | No config change needed |
| `POST /instances/<id>/deploy` | Regenerate systemd unit + env file → stop → start | Preset change, RPC binding, env vars, any config update |

After calling `PUT /rpccluster/llama/<id>/bind-rpc`, you MUST call `deploy` on the llama_server. A plain `restart` will NOT pick up new `--rpc` args because the systemd unit file is unchanged.

### Preset Change on Running Instance
**Wrong:** `POST /instances/<id>/deploy` with `{preset_id: N}` — deploy reads `preset_id` from DB, ignores the one in request body. Reapplies the **stored** preset's config.

**Correct:** `PUT /instances/<id>` with `{preset_id: N, skip_build: true}` — triggers BC-1 fast path (config-only update via `deploy_config_env` + `service_start` playbooks). Writes new env file, stops service, restarts. No git clone or cmake build.

### Port Resolution
Ports come from `.quickrobot.env`, never hardcoded in code. API port: `QUICKROBOT_API_PORT`, WebUI: `QUICKROBOT_WEBUI_PORT`, MCP: `QUICKROBOT_MCP_PORT`. Port allocation auto-increments from engine config `LLAMA_ARG_PORT` (llama_server), `base_port` (llama_rpc, iperf3).

### RPC Model Loading — Node Dependent
RPC servers load the model on-demand when first routed to by a llama-server. Fresh RPCs start with near-zero CPU until actual inference tokens reach them. Early benchmark results undercount throughput — the curve stabilizes only after all bound RPCs have loaded the model. This is per-node behavior.

### Crash Detection: `_recently_completed` Cursor Truthiness Bug
The `_recently_completed` query in `api_query_status()` uses a SQLite cursor for boolean checks. A common pitfall: `bool(cursor)` is always `True` because a `sqlite3.Cursor` object is never None. **Always use `.fetchone()` (or `.fetchall()`) to get the actual row data before converting to bool.**

**Wrong:** `_recently_completed = bool(_rc)` — Cursor is always truthy → crash detection skipped for ALL instances
**Correct:** `_recently_completed = bool(_rc.fetchone())` — Returns `True` only when a completed job exists within the time window

This bug caused crash detection to be permanently disabled: the `elif _active_jobs or _recently_completed:` guard always matched, preventing dead instances from transitioning to error state. Instances could remain stuck in "running" indefinitely even when their services were down.

### Llama-server vs RPC Health Check Paths
Both engines use the `instance_health_check` playbook via `_check_remote_service()`. However, llama_server's `query_status()` has two code paths: (1) HTTP `/health` endpoint check (primary), and (2) systemd fallback via `_check_remote_service()` when HTTP fails. RPC uses `_check_remote_service()` exclusively. Both health checks are logged to `qr_actions` with `action_type="health_check"`.

---

## Node Management

### Node Creation Flow
| Path | Steps | Result |
|------|-------|--------|
| API POST /nodes | 1) Create node in DB → 2) Call validate_node() inline | Single ansible_actions entry (validate_node) |
| WebUI /webui/nodes/new | 1) API POST /nodes → 2) API POST /nodes/<id>/discover | Two ansible_actions entries (validate_node + discover_node) |

### Stale File Cleanup
When adding hosts that have old systemd units from previous deployments:
1. Verify units present via `ls /etc/systemd/system/qr-*.service`
2. Clean up: `sudo /bin/sh -c "systemctl stop qr-*.service 2>/dev/null; rm -f /etc/systemd/system/qr-*.service; systemctl daemon-reload"`
3. Verify: `ls` should show "No such file"
4. Run `POST /nodes/<id>/reboot` via API to reboot the node
5. After reboot, run `POST /nodes/<id>/discover` to re-validate

---

## Running Tasks & Logging

### Running Tasks Page (`/webui/qr-tasks`)
Shows all playbook executions with `status='running'`, `completed`, `failed`, or `stuck`. Auto-refresh keeps the view current during long operations (15-30 min compiles). Stuck detection: tasks remaining in `running` state beyond expected duration. Color coding: blue=running, orange=stuck, red=failed.

### Ansible Actions Logging
- Entries filtered by `action_type` for ansible_actions queries
- `task_summary` column contains full parsed JSON output from ansible-playbook (typically 2-44KB)
- Locale fix: `LC_ALL=en_US.UTF-8` and `LANG=en_US.UTF-8` set in `run_playbook()` subprocess env — prevents silent failures
- Host field: extracted from params dict (`inventory_host`) and stored in `ansible_actions.host` column
- Node-level actions (apt_update, reboot, shutdown) log to `ansible_actions` with `node_id` — filter via `?node_id=<id>`

### qr_actions Table
Framework-level operations log. Tracks node CRUD, instance state changes, and agent overrides. Override flag: `override=1` marks actions that bypass normal guards. The `details` JSON includes a `"__warning__"` key with human-readable message.

### Manifest Tracking
All writable agents log file modifications to `./manifest.log`. Format: `<filepath> | <timestamp> | <agentname> | <backup_filename> | <reason>`. See `AGENTS.md` §5 for full format rules.

---

## Benchmark Details

### Timing Formula
Use **wall-clock time** (`date +%s`) for throughput calculations. Python `time.time()` inside piped curl response only measures HTTP round-trip, not total inference including model loading over network. Formula: `tokens / wall_seconds = tok/s`.

### Response Format
`response_json` field contains the full llama.cpp `/completion` response with timings (`prompt_per_second`, `predicted_per_second`), output content, and stop flag. When `success=-1`, output contains error description. Duration_ms shows how long the run took before failing.

---

## Lessons Learned (Condensed)

**Scope creep:** When asked to modify X, modify only X — do NOT expand scope to "everything related" without explicit confirmation. Before bulk UPDATE/INSERT/DELETE on many rows, state exact scope and ask confirmation.

**Preset change via deploy:** `POST /instances/<id>/deploy` reads preset from DB record, not request body. Use `PUT /instances/<id>` with `{preset_id: N, skip_build: true}` for config-only updates.

**No sleep in automation:** Always poll API endpoints instead of using `sleep`. One benchmark per instance at a time — API returns `BENCHMARK_RUNNING` if another is active.

**Benchmark results:** Verify `success=1` AND `tokens_per_sec` is populated before trusting benchmark data. Empty `{}` in `model_params` means "use model's internal defaults" — a valid setting, not missing data.

**Global state verification (2026-06-24):** Never declare "all clear" based on partial checks (ports/processes). Always read `GET /api/v1/app/status` and check `global_state` field. The API reports `global_state: stopped` (yellow/amber) when ANY instance is not running — including single user-owned instances. Distinguish between system-managed issues (IDs 1-4) vs user-owned instances before reporting health. Partial data → misleading summary.

**SSE loading fallback (2026-06-24):** When `/models/sse` returns 404 from remote llama.cpp, instances stuck in "loading" state need a `finally` block fallback in the SSE proxy generator. The generator's `finally` always runs on exit (404, connection error, timeout) — call `_transition_from_loading()` there to prevent permanent stuck states.

**Pre-flight exit behavior:** `sys.exit(1)` in `_start_system_engine()` exits the ENTIRE API on first conflict. All 3 system engines share the same startup pipeline — WebUI conflict → whole API dies. This is correct: clear signal for agent. But it means stale processes must be killed BEFORE restart, or you get cascading exit messages.

**SSE endpoint state-dependent:** llama.cpp `/models/sse` may behave differently during model load (503?) vs after loading completes (404). The SSE proxy 404 is NOT always a "missing endpoint" bug — it can be expected behavior when no model is actively loading.

**MCP SSE Transport (2026-06-23):** fastmcp's `json_response=True` returns tool responses as HTTP JSON bodies instead of SSE events (`event: message` + `data: {...}`). The llama.cpp web UI's MCP client expects traditional MCP SSE flow, so it waits forever in a skeleton loading state. Fix applied: `json_response=False` + Accept header middleware requires both `application/json` AND `text/event-stream`. Verified: SSE endpoint returns all 25 tools via proper SSE format, HTTP 200 OK, zero SSE errors. The remaining skeleton loading in llama.cpp web UI is a **web client rendering bug** — the EventSource connects successfully but never transitions to "tools listed" state.

---

## Completed Phases Summary (Archived)

Full phase history moved to `docs/TODO_v005.md`. This section retained for quick reference only. See CHANGELOG files for detailed per-entry changelog.
