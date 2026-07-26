WIP

- Fully agentic backend handling for ya Llama.cpp lab - or anything else
- Ape or AI-driven Cluster setup for RPC + GPU + Layer- + Tensor- + Expert-split + MTP 
- Model and Preset handling
- Remote Host control for agents using ansible playbooks instead of full ssh 
- Human backwards compatible Web-UI interface, REST-API or MCP for agents
- 100% Coded by local Qwen3.6-35B-A3B-Q5KM at 30 t/s - extend it as needed - no bigger model needed
- no npm, no aur, no dockerhub, no pipe to bash
- open source, open weights, closed ai

8k Trailer VIDEO goes here ;o)


## Cluster Example: 222GB Model GLM 5.2 on the 5x 6W TDP thin clients and some other e-waste

![RPCandClusterSetup](docs/pics/insane1.png)

![RPCandClusterSetup](docs/examples/llamaCPP_cluster/Expertsplit9nodes-GLM52-Q2XXS/Expertsplit9nodes-GLM52-Q2XXS_001.png)



Example Quickrobot prompt:

"Start quickrobot server. Add 3 nodes (Hostnames node1.lan, node2.lan, node3.lan). 
On each node create an RPC instance. 
On node1 also create a llama_server instance using preset QR-DESIGNER... 
Bind all RPCs to the server. Run the "Count-to-100" benchmark and report results."

## Cluster Example: 94,5GB Model on 12GB RTX 4070ti using CUDA + Draft-MTP on 8GB Radeon using Vulkan + Experts on 2015 4c CPUs in thin clients 
4 Nodes / 2 actual GPUs / 2G5LAN / 94,5 GB on Disk - Step-3.7-flash-Q3_K_M + Q8_0 MTP / n_ctx = 262144 (Q8/Q8) 

| ID | CPU | Cores | RAM | GPU | Instance | Usage |
|----|-----|-------|-----|-----|----------|--------------------|
| 1 | Ryzen 9 3900XT | 12 ~4Ghz | 4x16GB @ DDR4-3200 | RTX4070ti 12GB 8x4.0 | Server | CUDA0: 3.3GB Attn + KV 6.5GB CPU: 34GB experts + 4GB mmproj-f16 + Browser |
| 2 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC0-CPU | 26GB experts |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC1-CPU | 26GB experts |
| 4 | 2013 i5-4570  | 4 ~3GHz | 4x8GB  @ DDR3-1333 | 2019 AMD 8GB RX5700 | RPC2-VULKAN | 3GB -mtp-Q8_0 |

![TopModels](docs/examples/llamaCPP_cluster/Expertsplit4nodes-Step-37-flash-Q3KM/ScreenshotChat.png)

198B at ~5 t/s - not fast - But it's a good story writer 

## Cluster Example: Expert-Split on E-waste ![full](docs/examples/llamaCPP_cluster/Expertsplit3nodesQwen36-35B-A3B/cluster_002_example_v007.md)
Nodes (1 main + 2 RPC)

| ID | CPU | Cores | RAM | GPU | Instance | Usage |
|----|-----|-------|-----|-----|----------|--------------------|
| 1 | 2013 i5-4570  | 4 ~3GHz | 4x8GB  @ DDR3-1333 | 2019 AMD 8GB RX5700 | Server | Vulkan0 = Attention+MTP+kV |
| 2 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC0-CPU | 8GB experts |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC1-CPU | 8GB experts |

Model Qwen3.6-35B-A3B-MTP-Q5_K_M.gguf ~ 23GB  CTX_SIZE=262144   ~ 10t/s


## Cluster Example: Layer + MTP + Expert split on 1GB LAN ![full](docs/examples/llamaCPP_cluster/ExpertsplitThinclientStep37Flash/cluster_001_example_v007.md)


![CHANGELOG](docs/git/nice_changelog.md)

## "Security":

- API key gatekeeper (`QUICKROBOT_API_KEY`) on all `/api/v1/*` routes
- WebUI password login (`QUICKROBOT_WEBUI_PASSWORD`), rate-limited with failed-attempt logging
- MCP auth via same `QUICKROBOT_API_KEY` token, configurable DNS rebinding protection
- SSL optional — plain HTTP by default (`QUICKROBOT_*_SSL_CERT/KEY` for future HTTPS)
- CORS wildcard (`*`) by default, configurable via `QUICKROBOT_API_CORS_ORIGINS`
- REMOTE LLama.cpp SERVERS BIND TO 0.0.0.0 by default - Needs Custom per Instance override to local (v/Vx/LAN ipv4/6) and "re-deploy" - but I added warning Label in Ape interface - should be fine^^  
- Run Agent Harness's console and the (API) server as different users for seperation.
- TODO: randomize API key on server deployment and use for proxy and API interactions

## BUT WHY?

- Scope of the project is to help upcycle e-waste Hardware: Too old to run win 11 ? Make it an AI-node and hold some Experts. 
- Use Your old laptop with the broken screen to store Your active context window or some Experts at home on Your DDR4.

## including Human interface
In case the agent is down:

![RPCandClusterSetup](docs/pics/herd_007.png)
Dynamic Cluster setup for IP, Port, Layer, ENV, cli 

![ListOfEngines](docs/pics/instances_007.png)
Remote Service handling, health checks (async), ping checks

![ListOfComputers](docs/pics/hosts_007.png)
Host management (rebuild/update/upgrade/reboot)

![TopModels](docs/pics/models_006.png)
Local Model manager with auto-import, Change notification,
Draft (MTP) Model handling for standalone draft heads, 
Model and Preset based Merge chain for ENV or cli
TODO wrapper for downloader with checksums




## "Get started" 
WIP

Currently llama.cpp deployment is limited to git builds per node from scratch, binary downloads will follow later. (apt)   

## Features (Unique Capabilities)

- **RPC Cluster Inference** — Distribute a single model across multiple nodes. Each RPC node holds GPU/CPU memory; the main server coordinates attention layers, KV cache, and MoE experts across the cluster via gRPC. Supports tensor_split, layer_split, and expert_split modes.
- **6-Layer Config Merge Chain** — Engine defaults → node defaults → preset template → model params → cluster bindings → instance override. Preset changes only rewrite the env file (no git clone, no cmake rebuild, no port change). Swap presets on a running instance in <100ms.
- **Ansible Playbook Deployment** — Remote nodes managed via structured playbooks (not raw SSH). Each deploy stage (preflight, deps, source, compile, config, start) is independent and retryable. Shared build per node: one git clone + cmake build serves all instances on that host.
- **Per-Instance Auth Tokens** — Every llama_server/llama_rpc instance gets a random bearer token on creation. Toggle globally via `auto_auth_token` engine config. Regenerate or disable tokens from WebUI without redeploy.
- **Staged Chain with Async Jobs** — Create + deploy + start in one API call. Scheduler polls for queued jobs, executes stages sequentially. 2-hour global timeout per job. Query progress via `/jobs` and `/tasks` endpoints.

## Architecture

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ WebUI    │     │ MCP      │     │ Scheduler│     │ User CLI │
│ (:8038)  │     │ (:8040)  │     │ (subproc)│     │ (curl)   │
└────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘
     │                │                │                │
     ▼                ▼                ▼                ▼
┌──────────────────────────────────────────────────────────────┐
│                     API Server (:8039)                       │
│  Flask REST API + subprocess manager + orchestrator          │
│  Routes → engine dispatch → playbook runner → remote nodes   │
└───────────────────────┬──────────────────────────────────────┘
                        │ (SSH + Ansible)
                        ▼
              ┌───────────────────────┐
              │    Remote Nodes       │
              │  ┌──────┐ ┌──────┐    │
              │  │RPC0  │ │RPC1  │    │
              │  │gRPC  │ │gRPC  │    │
              │  └──┬───┘ └──┬───┘    │
              │     └────┬───┘        │
              │  ┌───────┴───────┐    │
              │  │ llama_server  │    │
              │  │ (model loaded)│    │
              │  └───────────────┘    │
              └───────────────────────┘
```

**System Engines** (managed by API on startup):
| Engine | Port | Lifecycle | Purpose |
|--------|------|-----------|---------|
| API server | `QUICKROBOT_API_PORT` | REST API + route handlers |
| WebUI | `QUICKROBOT_WEBUI_PORT` | subprocess | Browser SPA (instances, models, herd) |
| MCP server | `QUICKROBOT_MCP_PORT` | subprocess | LLM agent tools via SSE transport |
| Scheduler | N/A | subprocess | Claims queued jobs, executes staged chains |

**Engine Types** (user instances):
| Engine ID | Type | Purpose |
|-----------|------|---------|
| 21 | llama_server | Model-loaded inference on remote nodes |
| 22 | llama_rpc | CPU gRPC serving for cluster offload |
| 31 | iperf3 | Network bandwidth testing |
| 23 | timestamp_proxy | Chat proxy with in-prompt timestamp injection |

**Deployment Stages** (llama_server/llama_rpc):
`preflight` → `deps` → `source` (git clone) → `compile` (cmake, up to 30min) → `config_svc` (systemd unit) → `config_env` (env file, `$QR_CLI_ARGS_JOINED`) → `start` (service + health probe)

## Preset System

Presets bundle environment variables, CLI arguments, and model references into reusable templates. They are the core mechanism for configuring instances.

**How presets work:**
1. Create an instance with a preset (or create without one, stays `unconfigured`)
2. Preset provides default `env` vars, `cli_opts`, and a `model_id` reference
3. At deploy time, quickrobot merges 6 layers of config (L1-L6 above) to produce the final command line
4. To change presets on a running instance: `PUT /instances/<id>` with new `preset_id` — only the env file is rewritten, the server restarts with the new model


## Configuration

Network configuration is defined in `.quickrobot.env` (human-edited, read-only by the server):

| Key | Default | Purpose |
|-----|---------|---------|
| `QUICKROBOT_API_HOST` / `PORT` | `127.0.0.1` / `8039` | API server bind address |
| `QUICKROBOT_WEBUI_HOST` / `PORT` | `127.0.0.1` / `8038` | WebUI bind address |
| `QUICKROBOT_MCP_HOST` / `PORT` | `127.0.0.1` / `8040` | MCP server bind address |
| `*_AUTOSTART` flags | `true` | Auto-start WebUI/MCP/Scheduler on API boot |

## Security Notes

**Current security model:** quickrobot is designed for trusted, air-gapped LANs. Security features are minimal and configurable.

### Implemented but untested!

| Feature | Details |
|---------|---------|
| **Per-instance auth tokens** | Each llama_server/llama_rpc instance gets a random bearer token on creation. Controlled by `auto_auth_token` engine config (`true`/`false`). Tokens used as `Authorization: Bearer` header for `/v1/completions`. |
| **MCP SSE authentication** | MCP endpoint gated by `QUICKROBOT_API_KEY` (or `QUICKROBOT_MCP_TOKEN` if set separately — dual-token feature in design). |
| **Subprocess env whitelist** | WebUI/MCP/Scheduler subprocesses receive only engine-scoped environment variables, not full inheritance. Sensitive tokens filtered. |
| **MCP DNS rebinding protection** | Configurable via `QUICKROBOT_MCP_DISABLE_DNS_REBINDING` env var. |
| **CORS wildcard** | Default `allow_origins=["*"]` for all engines. Can be tightened per-engine in config. |

### Known Risks & Planned

| Risk | Current State | Planned |
|------|--------------|---------|
| **No SSL/TLS** | Plain HTTP on all ports | TODO: reverse proxy with TLS termination |
| **RPC servers bind 0.0.0.0** | All interfaces by default; per-instance override available | Consider changing default to 127.0.0.1 (BIND-ADDR TODO, deferred) |
| **Single-token auth** | Same key gates MCP SSE + proxy calls | `QUICKROBOT_MCP_TOKEN` for serving vs `QUICKROBOT_API_KEY` for proxy (MCP-DUAL-TOKEN, v0.11+) |
| **No container isolation** | Runs directly on host OS | Users bring their own containers/VMs/airgap |

**Trust model:** SSH key-based trust for remote node access. If an untrusted host can reach the API port, it has full control over all instances (deploy, stop, reconfigure). Designed for trusted LAN only.

## MCP Integration

The MCP server exposes tools wrapping the REST API for LLM agent use. Two tiers:

**Summary tools** (low token usage, recommended for smaller models):
| Tool | Purpose |
|------|---------|
| `list_instances_summary()` | Inventory: id, name, state, engine, node, port |
| `list_nodes_summary()` | Availability: id, name, hostname, status, ping_state |
| `list_presets_summary(engine_type)` | Preset selection |
| `list_models_summary(engine_type)` | Model selection |

**Write tools:**
`create_instance()`, `deploy_instance()`, `start_instance()`, `stop_instance()`, `restart_instance()`, `change_preset()`, `delete_instance()`, `create_node()`, `discover_node()`, `toggle_node_active()`, `bind_rpc()`, `unbind_rpc()`, `scan_models()`, `run_benchmark()`

**Proxy tool** (requires `ALLOW_PROXY`):
`quickrobot_api(method, path, body)` — direct pass-through to any API endpoint.
