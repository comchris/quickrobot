# quickrobot — API Usage Skill

**quickrobot** is a LAN controller for managing LLM inference servers, remote nodes, and system services. It provides:
- **REST API** (`/api/v1`) — all operations via HTTP
- **Web UI** (`/webui/*`) — browser-based management at `QUICKROBOT_WEBUI_PORT`
- **MCP Server** (`qr_mcp_server.py`) — LLM agent tools at `QUICKROBOT_MCP_PORT`

> **Note:** Replace `<API_PORT>` with `QUICKROBOT_API_PORT` from `.quickrobot.env`. Similarly, `<WEBUI_PORT>` and `<MCP_PORT>` come from `QUICKROBOT_WEBUI_PORT` / `QUICKROBOT_MCP_PORT`. Never hardcode ports.

---

## System Overview

### Services
| Service | Port (from .quickrobot.env) | Access |
|---------|-----------------------------|--------|
| API server | `QUICKROBOT_API_PORT` | `http://127.0.0.1:<API_PORT>/api/v1/` |
| Web UI | `QUICKROBOT_WEBUI_PORT` | `http://127.0.0.1:<WEBUI_PORT>/webui/` |
| MCP server | `QUICKROBOT_MCP_PORT` | MCP protocol (tools via LLM) |

### System-Managed Subprocess Lifecycle
All 4 system instances (API in tmux, WebUI/MCP/Scheduler as subprocesses) are managed by `lib/lib_system_engine.py`. Subprocesses use a minimal env whitelist (engine-scoped, sensitive tokens filtered) and a health check thread that self-terminates after 3 failed API checks (~9s total). Logs are written to `logs/{webui,mcp,scheduler}.log` with structured startup banners.

**`--mode exit`:** Test zombie behavior: `python3 quickrobot.py --mode exit` spawns engines, prints PIDs, exits. Engines self-terminate after ~9s when health check detects dead API.

### Environment Variable Whitelist
Subprocesses receive only necessary vars (not full env inheritance):
- **Base (all):** `PATH`, `HOME`, `LANG`, `LC_ALL`, `QUICKROBOT_API_BEARER_TOKEN`, `QUICKROBOT_API_HOST`, `QUICKROBOT_API_PORT`, `QUICKROBOT_CONSOLE_DEBUG_LEVEL`, `QUICKROBOT_ANSIBLE_LOG_LEVEL`, `QUICKROBOT_LOG_PATH`
- **WebUI only:** `QUICKROBOT_WEBUI_HOST`, `QUICKROBOT_WEBUI_PORT`
- **MCP only:** `PYTHONPATH`, `QUICKROBOT_MCP_HOST`, `QUICKROBOT_MCP_PORT`, `QUICKROBOT_MCP_READ/WRITE/PROXY`, `QUICKROBOT_MCP_ALLOWED_HOSTS`, `QUICKROBOT_MCP_DISABLE_DNS_REBINDING`

### API Response Format
- **Single resource:** `{ "status": "ok", "data": { ... } }`
- **List resources:** `{ "status": "ok", "total": N, "items": [...] }`
- **Error:** `{ "status": "error", "code": "...", "message": "..." }`

### Important: Content-Type Required for ALL POST/PUT/DELETE
All 42+ route handlers use `require_json()` which rejects requests without `Content-Type: application/json`. This is a hard gate, not a hint.

```bash
# WRONG — will return 415:
curl -X POST http://127.0.0.1:<API_PORT>/api/v1/instances -d '{"name":"x"}'

# RIGHT — always include header:
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances \
  -H 'Content-Type: application/json' \
  -d '{"name":"x"}'
```

**Affected:** Every POST, PUT, DELETE endpoint. GET endpoints do NOT need it.

### Quick Reference — All Endpoints

| Purpose | Method | Endpoint |
|---------|--------|----------|
| List instances | GET | `/instances` |
| Get instance | GET | `/instances/<id>` |
| Create instance | POST | `/instances` (with `preset_id` triggers auto-deploy via RUNNER-1 staged chain) |
 | Update instance | PUT | `/instances/<id>` |
| Herd config (DB-only) | PUT | `/instances/<id>/herd-config` — stores ENV overrides in config_override without triggering BC-1 deploy |
  | CLI flags | GET/PUT | `/instances/<id>/cli-flags` — stored in config_override.cli_flags for unified herd state |
  | Split mode | PATCH | `/instances/<id>/split-mode` — set layer/tensor split mode (DB-only) |
  | Split value | PUT | `/instances/<id>/split` — set server tensor split percentage (DB-only) |
  | GPU override | GET/PUT | `/instances/<id>/gpu-override` — set device flag (DB-only, no reconfigure trigger) |
  | Herd summary | GET | `/rpccluster/summary` — returns all servers with bind counts, tensor_splits, RPC lists |
  | Bind RPC | POST | `/instances/<id>/bind-rpc` — add RPC instance(s) to server's rpc_bind_ids |
  | Unbind RPC | DELETE | `/instances/<id>/unbind-rpc` — remove RPC instance(s) from server |
 | Deploy instance | POST | `/instances/<id>/deploy` |

**Instance creation auto-deploy:** When creating an instance with `preset_id` set, the API automatically starts a staged-chain deploy job. Do NOT call `POST /instances/<id>/deploy` after create — it returns `DEPLOY_IN_PROGRESS`. Correct pattern:
```bash
# Single step: create + auto-deploy (preferred)
curl -s -X POST /api/v1/instances -d '{"name":"x","engine_type_id":21,"node_id":3,"preset_id":100}'
# Verify: GET /instances/<id> → state=deploying, or check jobs list
```
Omit `preset_id` if you want to create without deploying (stays in `unconfigured`).

### Async Job Operations — Check Status After Starting
Deploy, rebuild, start, stop, restart are all async via the RUNNER-1 job/task system. Each returns immediately with a job_id but does NOT block on completion.

**Important:** Jobs stay `'queued'` until the scheduler claims them (JOB-STATE-1 fix). Do NOT assume `status='running'` means execution has started — always check `GET /jobs/<id>` for actual task status. Job duration (`started_at`) is set only when the scheduler claims the first task, not at creation (JOB-DURATION-2 fix). Global per-job timeout: 2h (7200s, JOB-TIMEOUT).

**Pattern:**
```bash
# 1) Start operation (returns immediately)
curl -s -X POST /api/v1/instances/<id>/deploy -H 'Content-Type: application/json' | python3 -c "import sys,json; d=json.load(sys.stdin); jid=d.get('data',{}).get('job_id','?'); print(f'Job {jid}')"

# 2) Check status (poll if needed)
curl -s http://127.0.0.1:<API_PORT>/api/v1/instances/<id> | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('state','?'))"

# 3) Check job progress
curl -s http://127.0.0.1:<API_PORT>/api/v1/jobs?status=running | python3 -c "import sys,json; [print(j) for j in json.load(sys.stdin).get('items',[])]"
```

**Important:**
- `POST /instances/<id>/deploy` returns `DEPLOY_IN_PROGRESS` if a job is already running — always check state before re-deploying
- Staged-chain deploys have 6+ stages (preflight → deps → source → compile → config_svc → config_env → start) and can take 5-30 minutes
- **Do NOT run >10s `sleep` in test commands** — return to user for manual polling or use the `/jobs?status=running` endpoint
- When chaining multiple operations, use `&&` so failures stop the chain: `curl -s POST ... && curl -s GET ...`

**Job/Task Endpoints:**
| Purpose | Method | Endpoint |
|---------|--------|----------|
| List jobs | GET | `/jobs?status=running|queued|completed|failed` |
| Get job with tasks | GET | `/jobs/<id>` (returns job + list of task IDs) |
| List tasks | GET | `/tasks?status=queued|running|completed|failed|cancelled` |
| Cancel job | POST | `/jobs/<id>/cancel` |
| Get task detail | GET | `/tasks/<id>` (includes playbook_runs output) |
| Start/Stop/Restart | POST | `/instances/<id>/start`, `/stop`, `/restart` |
| Undeploy | POST | `/instances/<id>/undeploy` (stop service, remove systemd unit from remote node) |
| Delete | DELETE | `/instances/<id>` (remove from DB only — does NOT undeploy; use undeploy first to clean remote systemd units) |
| Restart system instance | POST | `/instances/<id>/restart_system` (system-managed instances only) |
| Query status (health probe) | GET | `/instances/<id>/query-status` (returns alive/latency/error — health check style) |
| Instance status (STATUS-1) | GET | `/instances/<id>/status` (returns actions, valid_next_states, engine_data — used by WebUI action rendering) |
| SSE model load proxy | GET | `/instances/<id>/models-sse` (streams /models/sse from remote llama_server; works during "starting" state for model load progress) |
| Update build | POST | `/instances/<id>/update-build` |
| List nodes | GET | `/nodes` |
| Create node | POST | `/nodes` |
| Node actions | POST | `/nodes/<id>/reboot`, `/shutdown`, `/apt-update`, `/discover` |
| Toggle host active/inactive | PUT | `/nodes/<id>/host-status` (body: `{"is_active": 0}` or `{"is_active": 1}`) — blocks all operations on the node when inactive |
| Engine configs | GET/PUT/DELETE | `/engine/<type>/config[/<key>]` |
| Presets CRUD | GET/POST/PUT/DELETE | `/engine/<type>/presets[/<id>]` |
| Models CRUD | GET/POST/PUT/DELETE | `/models[/<id>]`, `/engine/<type>/models` |
| Scan models | POST | `/models/scan?node=<id>` |
| Benchmarks | POST/GET | `/benchmarks/run`, `/benchmarks/prompts`, `/benchmarks/results` |
| Health check | POST | `/health/check` |
| Ansible log | GET | `/api/v1/ansible_actions?instance_id=<id>&limit=5` |
| Playbooks CRUD | GET/POST/PUT/DELETE | `/playbooks[/<id>]` |

---

## Instance Lifecycle

### Instance Response Enrichment
`GET /instances` returns each instance with an additional `_host_inactive` boolean field. When `true`, the instance's host node has `is_active=0` and operations on that instance will be blocked with `NODE_INACTIVE` error. Use this to visually distinguish inactive hosts in the WebUI (grey background) or to skip instances in agent logic.

### Create an Instance
```bash
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-server","engine_type_id":21,"node_id":69,"preset_id":101}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Display name |
| engine_type_id | int | yes | 1=API, 2=WebUI, 3=MCP, 4=scheduler, 11=universal, 12=subprocess, 21=llama_server, 22=llama_rpc, 31=iperf3 |
| node_id | int | yes | Target node ID |
| preset_id | int | no | Preset to apply (default: preset 1 for llama_server/llama_rpc) |
| config_override | dict | no | Per-instance overrides: `{env:{}, cli_opts:[], model:{}}` |
| deploy | bool | no | Skip auto-deploy if false (default: true) |

**Engine type IDs:** API=1, WebUI=2, MCP=3, scheduler=4, universal=11, subprocess=12, llama_server=21, llama_rpc=22, iperf3=31. Always read from `qr_engine_ids.py` or the DB if unsure.

### Common Operations
```bash
# Start a deployed instance
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/start

# Stop a running instance
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/stop

# Restart (graceful stop + start)
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/restart

# Deploy (regenerate config + deploy systemd unit)
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/deploy

# Undeploy (stop service, remove systemd unit)
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/undeploy

# Delete instance
curl -s -X DELETE http://127.0.0.1:<API_PORT>/api/v1/instances/<id>
```

### State Machine
```
unconfigured → configuring → deploying → deployed → starting → running → stopping → stopped → error
                                                                  updating | compiling
```

**Key transitions:**
- Create → `unconfigured` → auto-deploy → `configuring` → `deploying` → `deployed` → (if start_after_deploy) `starting` → `running`
- Deploy (running instance) → `updating` → back to `running`
- Update-build → `updating` → `deployed` or `build_error`
- Start from deployed/stopped/unconfigured → `starting` → `running` (auto-deploys if unconfigured)
- Stop from running/starting → `stopping` → `stopped`
- Undeploy → `unconfigured`
- **Undeploy allowed states:** `running`, `stopped`, `starting`, `stopping`, `configuring`, `deploying`, `error`, `deployed`, `updating`, `build_error`. Returns `INVALID_STATE` error if instance is already `unconfigured`.

**System-managed instances (IDs 1,2,3,4 — API, WebUI, MCP, Scheduler):** Protected from delete. Returns HTTP 409 `SYSTEM_MANAGED_INSTANCE`. Use `POST /instances/<id>/restart_system` for restart; engine config page for settings changes. Note: IDs < 10 with `system_managed=1` are protected; the ID ranges are conventions not enforced by DB constraints.

**Startup pre-flight scan:** On API restart, system engines run a port + process pre-flight check (`lib_system_engine.py::check_port_and_process_free()`). If port occupied or stale process detected: logs FATAL messages and **aborts** auto-start (does NOT kill/restart). Agent must resolve conflicts (kill PIDs) then re-restart API. Explicit `/instances/<id>/restart_system` endpoints bypass this scan.

### Important Patterns
- **Deploy vs Restart:** `deploy` regenerates config files + systemd unit; `restart` just stops + starts with existing config
- **Preset change on running instance:** Use `PUT /instances/<id>` with `{preset_id: N, skip_build: true}` — NOT `POST /deploy` (deploy reads preset from DB, ignores request body)
- **RPC special:** llama_rpc instances always auto-start after deploy regardless of `start_after_deploy` flag
- **No sleep:** Always poll API endpoints instead of sleeping. Use `date && curl ...` for timestamping.
- **State consistency:** Instance list (`GET /instances`) and instance status (`GET /instances/<id>/status`) may show different states if a background process (health check, state reconciliation) is running between calls. For authoritative state, use `GET /instances/<id>` or `GET /instances/<id>/status` after list results suggest an action.
- **Cluster RPC workflow:** Create RPCs first → wait for running → create server → bind RPCs → deploy+restart only the server. RPCs don't need restart after binding.

---

## Configuration

### Engine Configs (Global Defaults)
Engine-level defaults applied to ALL instances of that type. Stored in DB, modifiable via API:
```bash
# List configs for an engine
curl -s http://127.0.0.1:<API_PORT>/api/v1/engine/llama_server/config

# Set a config value (takes effect on next deploy)
curl -s -X PUT http://127.0.0.1:<API_PORT>/api/v1/engine/llama_server/config/LLAMA_ARG_HOST \
  -H 'Content-Type: application/json' -d '{"value": "0.0.0.0"}'

# Delete a config value
curl -s -X DELETE http://127.0.0.1:<API_PORT>/api/v1/engine/llama_server/config/LLAMA_ARG_HOST
```

**Common keys per engine:**
| Engine | Important Keys |
|--------|---------------|
| llama_server | `binary_path`, `git_clone_url`, `node_build_set_cmd`, `node_build_run_cmd`, `LLAMA_ARG_HOST`, `LLAMA_ARG_PORT`, `model_root_path` |
| llama_rpc | `binary_path`, `node_build_set_cmd`, `node_build_run_cmd` |
| subprocess | `executable`, `working_dir`, `cli_args`, `env_passthrough` |
| universal | `install_dir`, `start_command`, `start_on_boot` |
| quickrobot-webui | `web_port`, `timezone`, `webui_detach`, `webui_autostart` |
| quickrobot-mcp | `allow_reads`, `allow_writes`, `allow_proxy` |

### Presets (Predefined Config Templates)
Presets bundle env vars, CLI options, and model references:
```json
{"env": {"VAR": "value"}, "cli_opts": ["--flag", "val"], "model": {"model_id": 100}}
```

**Recommended test presets:**
| Preset ID | Use Case | Size |
|-----------|----------|------|
| 1 | Router mode (no model) — instant deploy, systemd tests | None |
| 101 | Bonsai-1-1.7B-Q2 — smallest model test | ~236 MB |
| 102 | Bonsai-1-4B-Q2 — small model test | ~546 MB |
| 257 | Qwen3.5-2B-Q4_0 — medium-small test | ~562 MB |

Avoid large presets (198=87GB, 110=23GB) for simple deploy tests.

### Model Params
When setting `model_params` in presets or models, use short key names:

| Key | Maps To | Key | Maps To |
|-----|---------|-----|---------|
| `temp` | temperature | `gpu_layers` | GPU layers |
| `top_p` | top-p sampling | `context_size` | context size |
| `top_k` | top-k sampling | `batch_size` | batch size |
| `min_p` | min-p sampling | `mmap` | memory map |
| `model_path` | model file | `mmproj` | multimodal proj |
| `draft` | draft model | `device` | GPU device |

**⚠ Only use the keys above.** `temperature` (full name) is NOT recognized. Keys not in this list get a generic `LLAMA_ARG_` prefix that llama.cpp may not understand.

---

## Nodes

### Create & Discover
```bash
# Create node (auto-validates via SSH)
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"name":"newnode","hostname":"newnode.lan"}'

# Manual discovery (re-scan hardware)
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/nodes/<id>/discover
```

### Node Actions
```bash
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/nodes/<id>/reboot
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/nodes/<id>/shutdown
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/nodes/<id>/apt-update
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/nodes/<id>/apt-upgrade
```

**Admin toggle:** `PUT /api/v1/nodes/<id>/host-status` with body `{"is_active": 0}` blocks ALL operations on the node. Returns `NODE_INACTIVE` error to clients. Separate from ping connectivity (`ping_state`). Example:
```bash
# Set node inactive (blocks deploy/restart/bind/unbind)
curl -s -X PUT http://127.0.0.1:<API_PORT>/api/v1/nodes/5/host-status \
  -H 'Content-Type: application/json' \
  -d '{"is_active": 0}'

# Restore active
curl -s -X PUT http://127.0.0.1:<API_PORT>/api/v1/nodes/5/host-status \
  -H 'Content-Type: application/json' \
  -d '{"is_active": 1}'
```

**Note:** Use `PUT /nodes/<id>/host-status`, NOT `POST /nodes/<id>/active`. The `/active` endpoint does not exist.

---

## Benchmarks

### Benchmark Methodology — Preset Swapping (NOT New Instances)
**Do NOT create a new instance for each preset benchmark.** Instead, reuse existing test server instances and switch presets via `change_preset` (or `PUT /instances/<id>` with `skip_build: true`). Creating a new instance changes the port, RPC bindings, node context, and build state — you end up comparing different instances rather than the same preset under consistent conditions.

**Correct workflow for benchmarking all presets:**
1. List instances → identify 5 test server instances (one per remote node, already running)
2. For each preset (start with smallest models):
   a. Call `change_preset(instance_id, preset_id, skip_build=True)` on the target instance
   b. Wait for model load (`GET /instances/<id>/status` → state changes to `running`)
   c. Run benchmark: `quickrobot_run_benchmark(instance_id, prompt_id=1)` (Count-to10)
   d. Record results (duration_ms, tokens_per_sec, output quality)
   e. Move to next preset or next host
3. After completing all presets on a host, move to the next node

**Why this matters:** Preset changes only affect the env file and CLI args (config_env stage, CONFIG-1: `$QR_CLI_ARGS_JOINED` pattern). The binary, build number, port, RPC bindings, and node hardware stay identical — giving clean apples-to-apples comparisons across presets. Preset change via `PUT /instances/<id>` returns `<100ms` (CHANGE-PRESET-ASYNC: async_mode=True, no redundant restart job). Check response for `"config_update_triggered": true` to confirm the reconfigure chain was queued.

### Benchmark Operations
```bash
# Start benchmark (fire-and-forget, returns immediately)
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/benchmarks/run \
  -H 'Content-Type: application/json' \
  -d '{"instance_id":<id>,"prompt_id":<id>}'

# List results for an instance
curl -s "http://127.0.0.1:<API_PORT>/api/v1/benchmarks/results?instance_id=<id>&limit=20" | python3 -m json.tool

# Check single result
curl -s http://127.0.0.1:<API_PORT>/api/v1/benchmarks/results/<run_id> | python3 -m json.tool
```

**Response:** `success=1` = completed (check `tokens_per_sec` and `output_last_line`), `success=-1` = failed, `success=0` = still running.

**Interlock:** Only one benchmark per instance at a time. API returns `BENCHMARK_RUNNING` if another is active.

---

## Health Check

Refresh node capabilities and instance health:
```bash
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/health/check \
  -H 'Content-Type: application/json' \
  -d '{"scope": "all"}'
```

Scopes: `all`, `nodes` (validate all nodes), `instances` (health check all running instances), `node:<id>`, `instance:<id>`.

---

## Job & Task System (RUNNER-1)

The job/task system provides async execution with staged playbook chains. Jobs represent top-level operations; tasks represent individual stages within a chain.

### API Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/jobs` | GET | List all jobs (filter: `?instance_id=<id>`, `?status=running`) |
| `/api/v1/jobs/<id>` | GET | Single job detail including status, tasks, timing |
| `/api/v1/tasks` | GET | List all tasks (filter: `?job_id=<id>`, `?instance_id=<id>`) |
| `/api/v1/tasks/<id>` | GET | Single task detail including stage, playbook, timing, ansible output |
| `/api/v1/instances/<id>/jobs` | GET | Jobs scoped to one instance |

### Query Examples
```bash
# List all running jobs
curl -s "http://127.0.0.1:<API_PORT>/api/v1/jobs?status=running"

# Get job detail with task breakdown
curl -s http://127.0.0.1:<API_PORT>/api/v1/jobs/<id> | python3 -m json.tool

# List tasks for a specific job
curl -s "http://127.0.0.1:<API_PORT>/api/v1/tasks?job_id=<id>"

# All jobs for an instance
curl -s "http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/jobs"

# Operation log (legacy qr_actions)
curl -s "http://127.0.0.1:<API_PORT>/api/v1/qr_actions?limit=20"
```

### Job Lifecycle
```
queued → running → completed | failed
```

Each job has a `job_type` (e.g., `deploy`, `health_check`) and a list of tasks. Each task corresponds to one stage in the staged playbook chain (preflight, deps, source, compile, config, start). Tasks execute sequentially; failure stops the chain.

### Async Execution Chains
When an instance is deployed via the API, a job is created automatically with tasks for each staged playbook. The scheduler picks up queued jobs and executes them. You can query progress at any time:

```bash
# Check if deploy job completed
curl -s http://127.0.0.1:<API_PORT>/api/v1/jobs/<id> | python3 -c "
import sys,json; d=json.load(sys.stdin); j=d.get('data',{})
print(f'Status: {j.get(\"status\")}')
print(f'Tasks: {j.get(\"task_count\")}')
for t in j.get('tasks',[]):
    print(f'  - {t[\"stage\"]}: {t[\"status\"]} ({t.get(\"elapsed_seconds\",0)}s)')
"
```

### MCP Access to Job System
All job/task endpoints are accessible via the `quickrobot_api` proxy tool. No dedicated MCP tools needed — the proxy handles all 5 job-related endpoints natively.

**Example via MCP:**
```python
# List running jobs via MCP proxy
quickrobot_api(method="GET", path="/jobs?status=running", body=None)

# Get task detail via MCP proxy
quickrobot_api(method="GET", path=f"/tasks/{task_id}", body=None)
```

---

## MCP Server — Tool Reference

The MCP server exposes tools wrapping the REST API for LLM agents. Two tiers available:

### Summary Tools (small LLMs — low token usage)
| Tool | Purpose | Fields |
|------|---------|--------|
| `list_instances_summary()` | Instance inventory | id, name, state, engine_type_name, node_hostname, port_assigned, host_inactive |
| `list_nodes_summary()` | Node availability | id, name, hostname, status, is_active, ping_state |
| `list_presets_summary(engine_type)` | Preset selection | id, name, category, model_name, gpu_device |
| `list_models_summary(engine_type)` | Model selection | id, name, model_path, quantization, size_bytes |

**Note:** `host_inactive` is `true` when the instance's host node has `is_active=0`. Agents should check this before attempting operations.

### Full Detail Tools (original — complete API response)
`list_instances()`, `get_instance_status(id)`, `list_nodes()`, `list_presets(type)`, `get_preset(id,type)`, `list_models(type)`, `get_model(id,type)`

### Write Tools
`create_instance(...)`, `deploy_instance(id)`, `change_preset(id, preset_id, skip_build=True)`, `start_instance(id)`, `stop_instance(id)`, `restart_instance(id)`, `delete_instance(id, force)`

### Proxy Tool (requires ALLOW_PROXY)
`quickrobot_api(method, path, body)` — direct pass-through to any API endpoint

**Common cluster operations via proxy:**
```python
# Bind RPCs to a server
quickrobot_api("POST", "/instances/<server_id>/bind-rpc", {"rpc_ids": [rpc1, rpc2]})

# Set split mode
quickrobot_api("PATCH", "/instances/<server_id>/split-mode", {"split_mode": "layer"})

# Set server split to 0% (RPCs take all)
quickrobot_api("PUT", "/instances/<server_id>/split", {"split": 0})

# Get herd summary
quickrobot_api("GET", "/rpccluster/summary", None)
```

**Rule of thumb:** Use summary tools for routine operations. Only use full tools when you need excluded fields (`config_override`, `capabilities`, `merged_config`).

#### Known MCP proxy pitfalls
- **`POST /instances/<id>/bind-rpc`**: Body must be `{"rpc_ids": [<instance_id>, ...]}` — **array**, not single int. Passing `{"rpc_instance_id": 100}` silently binds nothing (reads `body.get("rpc_ids", [])` → empty).
- **`GET /api/v1/engines/llama_server/presets`** → 404. Use singular: `GET /api/v1/engine/llama_server/presets`.
- **`GET /api/v1/benchmark/prompts`** → 404. Use plural: `GET /api/v1/benchmarks/prompts`.
- **Cluster RPC ordering**: ALL bound RPCs must be in `running` state BEFORE restarting the main server. If any RPC is down, llama-server crashes on connect and enters error state immediately.
- **tensor_split semantics**: Each position corresponds to a GPU/device in the `-dev` list. Not just percentages — e.g., `"0,100,100,0,0,0,0,0"` means Vulkan0=0% normal tensors, first 2 RPCs split attention 50:50, remaining RPCs are expert-only via -ot flags.
- **RPC restart not needed after bind**: Only the server needs restart for binding/config changes to take effect. RPCs keep running.

---

## Operational Rules

### Override Protection
Actions that bypass normal guards set `override=1` in the `qr_actions` log with a `__warning__` message:
- Force-delete instances
- Node delete with running instances (`stop_running=true`)
- Restart when instance is in deployed (not running) state

Check `details.__warning__` before destructive operations.

### Key Gotchas
- **Deploy ignores body preset_id** — deploy reads preset from DB record, not request body
- **RPC servers use binary protocol** — do NOT `curl /health` on RPC endpoints
- **Shared build per node** — only one cmake build at a time across all instances on the same host
- **Build takes up to 30 min** — check for active cmake processes before declaring stuck
- **Config changes don't need daemon-reload** — `$QR_CLI_ARGS_JOINED` env file pattern (CONFIG-1): preset/config changes via `PUT /instances/<id>` only write the env file, no systemctl reload
- **Jobs stay 'queued' until scheduler claims** — checking `GET /jobs?status=running` may show nothing even though a job was just created. Wait for scheduler poll (~1s) or check task status.
- **`start_on_boot` controls systemd enabled state** (survives reboot) but does NOT auto-start; `start_after_deploy` triggers start after deploy only
- **All RPCs must be running before server (re)start** — if any bound RPC is down, llama-server crashes on connect. Create RPCs → verify running → create server → bind → restart server only.
- **tensor_split positions are semantic** — each position maps to a device in `-dev`. Leading `0` = server Vulkan gets no normal tensors; `100,100` splits attention 50:50 between first 2 RPCs; trailing `0`s keep other RPCs expert-only via -ot flags.
- **Model files pushed from server to RPCs** — don't pre-copy .gguf files to RPC nodes. The llama-server instance pushes them during startup.

### No Sleep Rule
Always poll API endpoints instead of using `sleep`. Example:
```bash
# Check instance state, then retry if still updating
curl -s http://127.0.0.1:<API_PORT>/api/v1/instances/<id> | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('state'))"
```

---

## Backup & Initialization

**--init:** (deprecated, no-op). DB creation is automatic — fresh DB created if file missing, existing DB backed up and reused. No `--init` flag needed.

**DB backups:** Stored in `data/_backups/`. Managed by the system with configurable retention (`QUICKROBOT_MAX_BACKUPS`).

---

## Full API Reference — Detailed Guides

For expanded endpoint descriptions and examples, see individual sections below or `docs/TODO_v005.md` for historical task context.

### Instance Details & Merged Config
```bash
# Get merged configuration (all layers resolved)
curl -s http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/merged-config

# View action log
curl -s "http://127.0.0.1:<API_PORT>/api/v1/ansible_actions?instance_id=<id>&limit=10" | python3 -m json.tool
```

### Model Scanning
```bash
# Scan a node for GGUF models (auto-registers in DB)
curl -s -X POST "http://127.0.0.1:<API_PORT>/api/v1/models/scan?node=69&compute_checksums=1"
# Returns: {"status":"ok","data":{"new_models":N,"existing_models":N,"hosts_scanned":[...],"total_files_found":N}}
```

### Playbook Management
```bash
# List registered playbooks
curl -s "http://127.0.0.1:<API_PORT>/api/v1/playbooks?file_type=core&tags=node"

# Rescan playbooks directory
curl -s -X POST "http://127.0.0.1:<API_PORT>/api/v1/playbooks/rescan?remove_deleted=true"
```

### Universal Engine (run any playbook/command)
```bash
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/execute \
  -H 'Content-Type: application/json' \
  -d '{"command": "ping -c 3 8.8.8.8", "timeout": 30}'
```

### Subprocess Engine (local processes)
Manages local processes on the API host. Config fields: `executable` (required), `working_dir`, `cli_args` (supports `{PORT}` and `{IP}` template variables), `env_vars`, `host`, `port`. Health check via HTTP probe to configured host:port.

---

## API Usage — Known Gotchas & Fixes

These are documented failures from real usage sessions. Keep this list current for future integration.

| # | Symptom | Root Cause | Fix Applied |
|---|---------|------------|-------------|
| 1 | `DELETE /instances/<id>` returns `UNDEPLOY_FAILED` but instance remains in DB | Standard delete runs remote undeploy first; if undeploy fails, instance kept for investigation (DESIGN-5). Force-delete bypasses this: `POST /instances/<id>/force-delete` | Use force-delete for error-state instances without remote files |
| 2 | `POST /instances/<id>/delete` returns 404 | The delete endpoint is **DELETE** method on `/api/v1/instances/<id>`, NOT POST to `/instances/<id>/delete` (line 399 in `__init__.py`) | Always use `DELETE` method for instance deletion |
| 3 | Herd "Save ENV Overrides" doesn't remove unchecked overrides | Frontend sends only checked+non-empty keys → backend does `co.update(overrides)` which merges but never removes stale keys | Fixed in v0.07e: `api_set_herd_config()` and `api_update_instance()` now remove old keys not in incoming request |
| 4 | Instance update via PUT doesn't clear config keys | Same merge-only bug as #3 — `new_override = dict(old_config)` preserves old keys unless explicitly set to `""` | Fixed: send `{config_override: {"KEY": ""}}` or use force-clear pattern |
| 5 | `Content-Type: application/json` missing → 415 error on ALL POST/PUT/DELETE | Every route handler calls `require_json()` which rejects without this header | Always include `-H 'Content-Type: application/json'` on write operations |
| 6 | Node validation returns `NODE_UNREACHABLE` for wrong SSH port | Ansible ping task marks unreachable hosts as `unreachable: true` (not `failed`) → `parse_ansible_json()` now checks both fields | Fixed in v0.07e |

**Pattern to remember:** POST/PUT/DELETE always need `Content-Type: application/json`. GET never needs it. Delete uses `DELETE` method on the resource path, not POST to a sub-path.

### Path Naming Gotchas (Session 2026-06-24)
| # | Mistake | Result | Correct |
|---|---------|--------|---------|
| 7 | `GET /api/v1/engines/llama_server/presets` | 404 | Use **singular** `engine`: `GET /api/v1/engine/llama_server/presets` |
| 8 | `GET /api/v1/benchmark/prompts` | 404 | Use **plural** `benchmarks`: `GET /api/v1/benchmarks/prompts` |

### Multi-Host Deploy Parallelism (Session 2026-06-24)
When creating multiple instances across different nodes with preset_id:
1. All instances are created and deploy jobs queued simultaneously
2. **Parallel across hosts**: All nodes start deploying at the same time (e.g., dllama1/2/3 CPU-RPC all show `deploying`)
3. **Serial per host**: Each node processes only one deploy chain at a time. Next instance on that node stays `unconfigured` until current chain completes
4. Port allocation is per-node: instances 100,101,102 all got port 50052 (correct — different nodes)

### Cluster RPC Binding & Tensor Split Semantics (Session 2026-06-27, updated 2026-06-28)
When binding RPCs to a llama-server and configuring expert splits:
A) **RPC restart not needed after bind** — After `PUT /instances/<id>/bind-rpc`, only the server instance needs restart for config changes to take effect. Individual RPC instances keep running with their existing config; no restart required for them.
B) **RPC startup ordering is critical** — ALL bound RPC instances must be in `running` state BEFORE the server gets (re)started. If any RPC is down, the llama-server crashes on connect and enters error state immediately. Workflow: create all RPCs → wait for running → create server → bind RPCs → deploy+restart only the server.
C) **tensor_split positions are semantic, not just percentages** — Each position corresponds to a GPU/device in the `-dev` list. Example: `tensor_split="0,100,100,0,0,0,0,0"` means:
   - Position 0 (Vulkan0/server): gets 0% of normal tensors (expert-only via -ot flags)
   - Positions 1-2 (first 2 RPCs): each gets 100% → attention layers split 50:50 between these two RPCs
   - Positions 3-7 (remaining RPCs): get 0% normal tensors → these nodes serve only expert computation via -ot flags
   - The draft model has different allocation rules than normal layers
D) **Vulkan deps are compile-time, not runtime** — Vulkan drivers (libvulkan-dev, glslc) needed for cmake compilation but installed as part of `install_deps.yml`. RPCs running after build don't need Vulkan at runtime.
E) **Model files pushed from server to RPCs** — Model .gguf files only need to exist on the llama-server node. During startup, llama-server pushes model shards to bound RPC instances automatically. No pre-copying to RPC nodes required.
F) **Expert split Mode C is iterative** — Load-Dist (Mode C) uses greedy distance-maximization with per-RPC expert quotas. Good for initial setup but the "perfect split" still needs tuning based on actual inference results.
G) **Bind does not auto-reconfigure the server** — `POST /instances/<id>/bind-rpc` updates the DB (`rpc_bind_ids`) and merged config, but does NOT trigger a BC-1 chain to regenerate the env file on the remote node. After any bind/unbind/rebind sequence, you MUST call `change_preset(instance_id, preset_id, skip_build=True)` or `deploy_instance(instance_id, start_after_deploy=True)` to push the updated env file (with new `--rpc`, `-dev`, and `LLAMA_ARG_TENSOR_SPLIT` values) and restart the server.
H) **RPC preset must match node core count** — If a node reports 2 cores (`cpu_cores: 2`), do NOT use `RPC-CPU-4Threads` (preset that sets 4 threads). Use `RPC-CPU-Default` or an RPC preset with thread count matching the node's `cpu_cores`. Over-allocating threads on thin clients causes context-switch thrashing and slower inference. Always check `list_nodes_summary()` for `cpu_cores` before selecting an RPC preset.
I) **3-device tensor_split with 100,100,100 and -dev RPC0,RPC1 only uses 2 devices** — The split string has 3 positions but the `-dev` list has only 2 entries (RPC0, RPC1). The server's own device is NOT included. Result: the 3rd `100` in the split string is ignored; only 2 RPCs do actual computation. To use all 3 nodes:
   - Option A (CPU-only clusters): Create a 3rd RPC instance on the server node, making it `3 RPCs + server routing-only`. Set `tensor_split="100,100,100"` and `-dev RPC0,RPC1,RPC2`. Server handles zero layers, all computation via 3 RPCs.
   - Option B (GPU-equipped server): Add the GPU device to `-dev` chain with a GPU override: `-dev Vulkan0,RPC0,RPC1` and `tensor_split="50,50,50"` (or any distribution). The GPU gets its share of normal tensors, RPCs handle the rest.
J) **Benchmark metrics empty** — The `benchmark_results` table has no `metrics` column; the API returns `"metrics": {}` as a default/empty field. This is a schema gap, not a failure. When a benchmark shows `success=1`, it means: the llama-server responded, text was captured in the `output` column, and the run completed (not just started). The `response_json` column contains the raw llama.cpp `/completion` response which includes `tokens_predicted` and `tokens_evaluated` — these can serve as proxy metrics. If timing data (`timings_total`, `timings_prompt_ms`) is needed, the benchmark would need to use the `/v1/completions` endpoint instead of `/completion`, and add a `metrics` TEXT column to store parsed timing JSON.

### Crash Detection Bug (Session 2026-06-24)
The scheduler crash_detect task logged: `name '_CONFIG' is not defined`. This is a runtime bug where `_CONFIG` variable reference was undefined during a crash detection check. Track separately — does not block normal operations.
