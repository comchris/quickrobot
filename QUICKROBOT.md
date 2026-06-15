# quickrobot — Architecture & Project Reference

**quickrobot** is a standalone REST API + Web UI controller for managing LLM inference servers, remote nodes, and system services on a local network. Full redesign of keeper_v1 with decoupled API/DB from Web UI, engine type registry, 4-layer config merge chain, Ansible-based deployment, and explicit state machine for instance lifecycle.

> **See also:** `AGENTS.md` for coding rules; `SKILL.md` for API/MCP usage; `docs/TODO_v005.md` for historical task list.

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
| `docs/TODO.md` | Current open tasks (open items only; archive at `TODO_v005.md`) |
| `docs/TODO_v005.md` | Full historical task list (all completed + open) |
| `CHANGELOG.md` | Current release entries |
| `CHANGELOG_v005.md` | v0.05-v0.06 changelog |
| `docs/ansible_output_format.md` | Ansible JSON output normalization reference |
| `docs/sortable_tables.md` | WebUI sortable table pattern (JS + HTML) |

---

## Seed File — Chain-of-Trust Verification

The seed file (`data/_seed/seed_v006.sql`) is a plain SQL file with `INSERT OR REPLACE` statements that populate all seed data: engine_types, engine_configs, engine_presets, engine_models, playbook_registry (with checksum_sha256 + file_size), and benchmark_prompts.

### Verification Flow (`--init` mode)
1. **Pre-flight:** Load `.quickrobot.env`, validate required keys + seed checksum BEFORE any filesystem change → **HARD EXIT** if mismatch
2. **Backup existing DB** → timestamped copy (if exists)
3. **Delete old DB file** → fresh init continues
4. **Migrations run** → creates fresh DB with all tables
5. **Engine discovery** → auto-registers engine types from `engine/` subdirectories
6. **Seed import:** `import_seed_file()` executes seed SQL via `conn.executescript()` — idempotent
7. **Auto-provision system instances** (API, WebUI, MCP)

### `.quickrobot.env` Keys for Seed Verification
| Key | Purpose |
|-----|---------|
| `QUICKROBOT_SEED_CHECKSUM` | SHA256 hex digest of seed file |
| `QUICKROBOT_SEED_FILESIZE` | File size in bytes |
| `QUICKROBOT_SEED_MAX_ID` | Max ID range for seed data (default 1000) |

### Mode Behavior
| Scenario | `--init` mode | Normal mode |
|----------|---------------|-------------|
| Seed checksum mismatch | Hard exit (1) | Skip seed silently |
| Seed size mismatch | Hard exit (1) | Skip seed silently |
| Missing seed file | Hard exit (1) | Continue without seed |

### Key Files
| File | Role |
|------|------|
| `lib/lib_startup.py::import_seed_file()` | Reads seed SQL, executes via `executescript()` in `--init` mode |
| `lib/lib_startup.py::pre_validate_seed_checksum()` | Validates checksum+size BEFORE any filesystem change |
| `db/adapters/playbooks.py::register_playbook()` | INSERT OR REPLACE for core playbooks (idempotent) |

---

## Key Design Patterns

### Ansible Output Normalization
Ansible 2.10+ stores results under `task["hosts"][hostname]` (dict keyed by hostname). The `parse_ansible_json()` function in `lib/lib_ansible_runner.py` normalizes to `task["results"]` (list) for consistent iteration. See `docs/ansible_output_format.md` for the full normalization schema.

### Dynamic Inventory
All `run_playbook()` calls use dynamic inventory via `lib/qr_dynamic_inventory.py` — no stale `.ini` files. The legacy `generate_inventory()` function was removed in favor of DB-backed JSON inventory that reads node data directly from SQLite at runtime. Every handler passes `inventory_path=None`, which resolves hosts dynamically from the nodes table.

### Running Tasks Tracking (`qr_actions`)
Playbook executions create a `qr_actions` record with `status='running'` BEFORE blocking on playbook execution. On completion, the record updates to `status='completed'` or `status='failed'`. The WebUI `/webui/qr-tasks` page shows live duration with auto-refresh toggle (5s/10s/30s) and stuck detection. This enables detecting stuck processes during 15-30 minute compile/deploy operations without SSHing to remote nodes.

### Sortable Tables
WebUI tables use pattern: `<th class="sortable" data-col="N">` + JavaScript with `qrSettings` (localStorage) persistence. Arrow indicators auto-appear. Numeric sort for specific columns. Used on all WebUI table pages. See `docs/sortable_tables.md`.

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

**B) Full integration (seed file + env):** Add INSERT OR REPLACE to `data/_seed/seed_v006.sql`, update seed checksum in `.quickrobot.env`. Future `--init` deployments will include this config.

---

## System Architecture

### Shared Build Paths
All llama.cpp instances share ONE clone + build per node:
- **Source:** `/opt/quickrobot/llama.cpp` (one git clone per host)
- **Build:** `/opt/quickrobot/llama.cpp/build` (one cmake --build per host)
- **Binary (llama_server):** `{build}/bin/llama-server`
- **Binary (rpc):** `{build}/bin/rpc-server`

Paths stored in `engine_configs`, passed to playbooks via extra_vars. Only one cmake build at a time across all instances on the same node.

### Playbook Registry (19 core playbooks)

| ID | File Path | Description |
|----|-----------|-------------|
| APT_UPDATE_V1 | `playbooks/apt_update.yml` | Node apt update |
| APT_UPGRADE_V1 | `playbooks/apt_upgrade.yml` | Node apt upgrade |
| DEPLOY_LLAMA_SERVER_V1 | `playbooks/deploy_llama_server.yml` | Deploy llama.cpp server |
| DEPLOY_LLAMA_RPC_V1 | `playbooks/deploy_rpc.yml` | Deploy RPC server |
| DEPLOY_IPERF3_V1 | `playbooks/deploy_iperf3.yml` | Deploy iperf3 |
| UPDATE_LLAMA_SERVER_V1 | `playbooks/update_llama_server.yml` | Update + compile llama.cpp |
| UPDATE_AND_COMPILE_V1 | `playbooks/update_and_compile.yml` | Standalone git pull + cmake build |
| UNDEPLOY_LLAMA_SERVER_V1 | `playbooks/undeploy_llama_server.yml` | Undeploy llama.cpp server |
| UNDEPLOY_LLAMA_RPC_V1 | `playbooks/undeploy_rpc.yml` | Undeploy RPC server |
| UNDEPLOY_IPERF3_V1 | `playbooks/undeploy_iperf3.yml` | Undeploy iperf3 |
| CHECK_UNDEPLOY_V1 | `playbooks/check_undeploy.yml` | Pre-undeploy check |
| MANAGE_INSTANCE_V1 | `playbooks/manage_instance.yml` | Generic start/stop/restart |
| CLEAN_SHARED_LLAMACPP_BUILD_V1 | `playbooks/clean_shared_build.yml` | Clean shared build dirs |
| NODE_VALIDATE_V1 | `playbooks/node/validate.yml` | Node validation |
| NODE_DISCOVER_V1 | `playbooks/node/discover.yml` | Node discovery |
| NODE_SCAN_MODELS_V1 | `playbooks/node/scan_models.yml` | Model scan |
| REBOOT_NODE_V1 | `playbooks/reboot_node.yml` | Node reboot |
| SHUTDOWN_NODE_V1 | `playbooks/shutdown_node.yml` | Node shutdown |
| UPDATE_CONFIG_V1 | `playbooks/update_config.yml` | Config-only update (no build) |

**Resolution priority:** ID lookup (preferred) → tag-based AND-match → file_path exact match.

### System-Managed Engines
System-managed instances use PID-based lifecycle via `lib/lib_system_engine.py`:

| Engine | Type ID | Port Source | Lifecycle |
|--------|---------|-------------|-----------|
| quickrobot-api | 1 | `QUICKROBOT_API_PORT` | tmux session `qr_api` |
| quickrobot-webui | 2 | `QUICKROBOT_WEBUI_PORT` | Subprocess (PID-in-DB) |
| quickrobot-mcp | 3 | `QUICKROBOT_MCP_PORT` | Subprocess (PID-in-DB) |

All provisioned with `node_id=1` via `_auto_provision_system_instances()`. Host/port removed from `engine_configs` table — stored in `.quickrobot.env`.

**System Instance Protection:** IDs 1, 2, 3 protected from delete/deploy/config-change (returns 409 `SYSTEM_MANAGED_INSTANCE`). Use `POST /instances/<id>/restart_system` for restart.

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

### Herd Page (`/webui/rpccluster`)
- Left panel: llama-server list with state, split_mode, RPC count, tensor_split
- Right panel: selected server detail — split config (mode + value), RPC bindings table (inline edit), deploy + restart actions
- API endpoints: GET /rpccluster/summary, PUT/DELETE bind-rpc, PATCH split-mode, PUT split/experts/draft

---

## Operational Rules

### --init Guard
**NO agent may use `--init` without explicit user confirmation.** Backs up existing DB (single copy), then creates a completely fresh database — all instances, nodes, ansible actions, and build history are lost. This is NOT a restart; it's a reset. Always state what will be lost before using `--init`. See `AGENTS.md` §7.

### Process Kill Guard
**NO agent may kill the API process (`kill <pid>`) without explicit user confirmation.** Killing the API also kills all running ansible-playbook subprocesses (compiles, deploys in progress). The `update_and_compile` playbook can take 15-30 minutes. Before killing: verify no long-running operations via instance states (`updating`, `configuring`, `deploying`, `starting`, `stopping`, `loading`, `compiling`). See `AGENTS.md` §7.

### Compile Verification Rule
When instances show state `updating` after triggering `update-build`: **do NOT assume the build is stuck.** The shared cmake build can take up to 30 minutes. Before reporting or acting: SSH to remote nodes and check for active cmake/compile processes (`ps aux | grep cmake`, CPU usage via `top`). Only declare a build "stuck" if no active compile processes after 15+ minutes.

### Admin Node Toggle
Set `is_active=0` on a node to block ALL operations (deploy, restart, delete, apt-update, etc.). Returns `NODE_INACTIVE` error. This is the admin "do not touch" flag, separate from ping connectivity (`ping_state`).

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

**Correct:** `PUT /instances/<id>` with `{preset_id: N, skip_build: true}` — triggers BC-1 fast path (config-only update via UPDATE_CONFIG_V1 playbook). Writes new env file, reloads systemd daemon, restarts service.

### Port Resolution
Ports come from `.quickrobot.env`, never hardcoded in code. API port: `QUICKROBOT_API_PORT`, WebUI: `QUICKROBOT_WEBUI_PORT`, MCP: `QUICKROBOT_MCP_PORT`. Port allocation auto-increments from engine config `LLAMA_ARG_PORT` (llama_server), `base_port` (llama_rpc, iperf3).

### RPC Model Loading — Node Dependent
RPC servers load the model on-demand when first routed to by a llama-server. Fresh RPCs start with near-zero CPU until actual inference tokens reach them. Early benchmark results undercount throughput — the curve stabilizes only after all bound RPCs have loaded the model. This is per-node behavior.

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

---

## Completed Phases Summary (Archived)

Full phase history moved to `docs/TODO_v005.md`. This section retained for quick reference only. See CHANGELOG files for detailed per-entry changelog.
