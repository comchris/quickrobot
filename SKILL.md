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

### API Response Format
- **Single resource:** `{ "status": "ok", "data": { ... } }`
- **List resources:** `{ "status": "ok", "total": N, "items": [...] }`
- **Error:** `{ "status": "error", "code": "...", "message": "..." }`

### Quick Reference — All Endpoints

| Purpose | Method | Endpoint |
|---------|--------|----------|
| List instances | GET | `/instances` |
| Get instance | GET | `/instances/<id>` |
| Create instance | POST | `/instances` |
| Update instance | PUT | `/instances/<id>` |
| Deploy instance | POST | `/instances/<id>/deploy` |
| Start/Stop/Restart | POST | `/instances/<id>/start`, `/stop`, `/restart` |
| Undeploy/Delete | POST/DELETE | `/instances/<id>/undeploy`, `/<id>` |
| Restart system instance | POST | `/instances/<id>/restart_system` |
| Query status | GET | `/instances/<id>/query-status` |
| Update build | POST | `/instances/<id>/update-build` |
| List nodes | GET | `/nodes` |
| Create node | POST | `/nodes` |
| Node actions | POST | `/nodes/<id>/reboot`, `/shutdown`, `/apt-update`, `/discover` |
| Engine configs | GET/PUT/DELETE | `/engine/<type>/config[/<key>]` |
| Presets CRUD | GET/POST/PUT/DELETE | `/engine/<type>/presets[/<id>]` |
| Models CRUD | GET/POST/PUT/DELETE | `/models[/<id>]`, `/engine/<type>/models` |
| Scan models | POST | `/models/scan?node=<id>` |
| Benchmarks | POST/GET | `/benchmarks/run`, `/benchmarks/prompts`, `/benchmarks/results` |
| Health check | POST | `/health/check` |
| Ansible log | GET | `/ansible_actions?instance_id=<id>&limit=5` |
| Playbooks CRUD | GET/POST/PUT/DELETE | `/playbooks[/<id>]` |

---

## Instance Lifecycle

### Create an Instance
```bash
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-server","engine_type_id":21,"node_id":69,"preset_id":101}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Display name |
| engine_type_id | int | yes | 1=API, 2=WebUI, 3=MCP, 11=universal, 12=subprocess, 21=llama_server, 22=llama_rpc, 31=iperf3 |
| node_id | int | yes | Target node ID |
| preset_id | int | no | Preset to apply (default: preset 1 for llama_server/llama_rpc) |
| config_override | dict | no | Per-instance overrides: `{env:{}, cli_opts:[], model:{}}` |
| deploy | bool | no | Skip auto-deploy if false (default: true) |

**Engine type IDs:** API=1, WebUI=2, MCP=3, universal=11, subprocess=12, llama_server=21, llama_rpc=22, iperf3=31. Always read from `qr_engine_ids.py` or the DB if unsure.

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

**System-managed instances (IDs 1,2,3 — API, WebUI, MCP):** Protected from delete/deploy/config-change. Use `POST /instances/<id>/restart_system` instead.

### Important Patterns
- **Deploy vs Restart:** `deploy` regenerates config files + systemd unit; `restart` just stops + starts with existing config
- **Preset change on running instance:** Use `PUT /instances/<id>` with `{preset_id: N, skip_build: true}` — NOT `POST /deploy` (deploy reads preset from DB, ignores request body)
- **RPC special:** llama_rpc instances always auto-start after deploy regardless of `start_after_deploy` flag
- **No sleep:** Always poll API endpoints instead of sleeping. Use `date && curl ...` for timestamping.

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

**Admin toggle:** Set `is_active=0` on a node to block ALL operations on it. Returns `NODE_INACTIVE` error. This is the admin "do not touch" flag, separate from ping connectivity (`ping_state`).

---

## Benchmarks

Run prompts against llama-server instances and track results:
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

## MCP Server — Tool Reference

The MCP server exposes tools wrapping the REST API for LLM agents. Two tiers available:

### Summary Tools (small LLMs — low token usage)
| Tool | Purpose | Fields |
|------|---------|--------|
| `list_instances_summary()` | Instance inventory | id, name, state, engine_type_name, node_hostname, port_assigned |
| `list_nodes_summary()` | Node availability | id, name, hostname, status, is_active, ping_state |
| `list_presets_summary(engine_type)` | Preset selection | id, name, category, model_name, gpu_device |
| `list_models_summary(engine_type)` | Model selection | id, name, model_path, quantization, size_bytes |

### Full Detail Tools (original — complete API response)
`list_instances()`, `get_instance_status(id)`, `list_nodes()`, `list_presets(type)`, `get_preset(id,type)`, `list_models(type)`, `get_model(id,type)`

### Write Tools
`create_instance(...)`, `deploy_instance(id)`, `start_instance(id)`, `stop_instance(id)`, `restart_instance(id)`, `delete_instance(id, force)`

### Proxy Tool (requires ALLOW_PROXY)
`quickrobot_api(method, path, body)` — direct pass-through to any API endpoint

**Rule of thumb:** Use summary tools for routine operations. Only use full tools when you need excluded fields (`config_override`, `capabilities`, `merged_config`).

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
- **`start_on_boot` controls systemd enabled state** (survives reboot) but does NOT auto-start; `start_after_deploy` triggers start after deploy only

### No Sleep Rule
Always poll API endpoints instead of using `sleep`. Example:
```bash
# Check instance state, then retry if still updating
curl -s http://127.0.0.1:<API_PORT>/api/v1/instances/<id> | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('state'))"
```

---

## Backup & Initialization

**--init:** Creates a fresh database. Backs up existing DB, drops all data (instances, nodes, actions, builds). NEVER run without explicit user confirmation.

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
