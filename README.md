WIP

- Fully agentic backend handling for ya Llama.cpp lab - or anything else
- Ape or AI-driven Cluster setup for RPC + GPU + Layer- + Tensor- + Expert-split + MTP 
- Model and Preset handling
- Remote Host control for agents is using ansible playbooks instead of full ssh 
- Human backwards compatible Web-UI interface, REST-API or MCP for agents
- 101% Coded by local Qwen3.6-35B-A3B-Q5KM at 30 t/s - no bigger model needed to extend the code
- no npm, no aur, no dockerhub, no pipe to bash
- open source, open weights, closed ai

8k Trailer VIDEO goes here ;o)


Example Quickrobot prompt:

"Add 3 nodes Hostnames node1.lan node2.lan node3.lan 
On each node create an RPC instance. 
On node1 also create a llama_server instance using preset Qwen36-35B 
Bind all RPCs to the server and restart it, then 
run the "Count-to-100" benchmark and report results."




## Cluster Example: 4 Nodes / 1 GPU RTX4070ti / 122 GB on Disk - DeepSeek-V4-Flash-0731-UD-Q3_K_XL / n_ctx = 100000 (Q8/Q8) 

TODO:pic

| ID | CPU | Cores | RAM | GPU | Instance | Usage |
|----|-----|-------|-----|-----|----------|--------------------|
| 1 | Ryzen 9 3900XT | 12 ~4Ghz | 4x16GB @ DDR4-3200 | Server | CUDA0: ~7GB Attn + KV CPU: 31GB experts |
| 2 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | RPC0-CPU | 26GB experts |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | RPC1-CPU | 27GB experts |
| 4 | 2013 i5-4570  | 4 ~3GHz | 4x8GB  @ DDR3-1333 | RPC2-CPU | 26GB experts |

at ~20k/20% context: ~9t/s in (pp)/ 3,4 t/s out (tg) (still testing)


## Cluster Example: 222GB Model GLM 5.2 on the 5x 6W TDP thin clients and some other e-waste

![RPCandClusterSetup](docs/pics/insane1.png)

![RPCandClusterSetup](docs/examples/llamaCPP_cluster/Expertsplit9nodes-GLM52-Q2XXS/Expertsplit9nodes-GLM52-Q2XXS_001.png)

It's 0,3 - 0,5 t/s in/out at ~ 250W overall - nice POC 


## Cluster Example: 94,5GB Model on 12GB RTX 4070ti using CUDA + Draft-MTP on 8GB Radeon using Vulkan + Experts on 2015 4c CPUs in thin clients 
4 Nodes / 2 actual GPUs / 2G5LAN / 94,5 GB on Disk - Step-3.7-flash-Q3_K_M + Q8_0 MTP / n_ctx = 262144 (Q8/Q8) 

| ID | CPU | Cores | RAM | GPU | Instance | Usage |
|----|-----|-------|-----|-----|----------|--------------------|
| 1 | Ryzen 9 3900XT | 12 ~4Ghz | 4x16GB @ DDR4-3200 | RTX4070ti 12GB 8x4.0 | Server | CUDA0: 3.3GB Attn + KV 6.5GB CPU: 34GB experts + 4GB mmproj-f16 + Browser |
| 2 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC0-CPU | 26GB experts |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC1-CPU | 26GB experts |
| 4 | 2013 i5-4570  | 4 ~3GHz | 4x8GB  @ DDR3-1333 | 2019 AMD 8GB RX5700 | RPC2-VULKAN | 3GB -mtp-Q8_0 |

![TopModels](docs/examples/llamaCPP_cluster/Expertsplit4nodes-Step-37-flash-Q3KM/ScreenshotChat.png)

198B at ~5 t/s out 
 

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

- currently no TLS/HTTPS by default 
- API key (`QUICKROBOT_API_KEY`) on all `/api/v1/*` routes
- WebUI password login (`QUICKROBOT_WEBUI_PASSWORD`), rate-limited with failed-attempt logging
- MCP auth via `QUICKROBOT_MCP_KEY` token
- REMOTE LLama.cpp SERVERS BIND TO 0.0.0.0 by default - Needs Custom per Instance override to local (v/Vx/LAN ipv4/6) and "re-deploy"   
- Run Agent Harness's console and the (API) server as different users for seperation.
- The Playbooks and Prompts have checksum and size tracking in DB. Playbooks are blocked on mismatch.    


## BUT WHY?

- Scope of the project is to help upcycle e-waste: Too old to run win 11 ? Make it an AI-node and hold some Experts.
- Use YOUR old laptop with the broken screen to store YOUR agent's active context window at home on YOUR DDR4.

## including Human interface
In case the agent is down:

![RPCandClusterSetup](docs/pics/createinstancewizz1_011.png)
v0.11 - Create instance wizzard

![RPCandClusterSetup](docs/pics/createinstancewizz2_011.png)
v0.11 - with per instance overrides

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

**Deployment Stages** (llama_server/llama_rpc):
`preflight` → `deps` → `source` (git clone) → `compile` (cmake, up to 30min) → `config_svc` (systemd unit) → `config_env` (env file, `$QR_CLI_ARGS_JOINED`) → `start` (service + health probe)

## Configuration

Network configuration is defined in `.quickrobot.env` (human-edited, read-only by the server):

| Key | Default | Purpose |
|-----|---------|---------|
| `QUICKROBOT_API_HOST` / `PORT` | `127.0.0.1` / `8039` | API server bind address |
| `QUICKROBOT_WEBUI_HOST` / `PORT` | `127.0.0.1` / `8038` | WebUI bind address |
| `QUICKROBOT_MCP_HOST` / `PORT` | `127.0.0.1` / `8040` | MCP server bind address |
| `*_AUTOSTART` flags | `true` | Auto-start WebUI/MCP/Scheduler on API boot |

## "Get started" 

... see requirements.txt or WIP: [quickstart_guide](docs/git/quickstart_guide_011.md)

# First run

```
python3 ./quickrobot.py
============================================================
[qr] Fresh setup detected — generated new .quickrobot.env
============================================================
[qr] WebUI password: OIcESNg6TsRkSzkMkjZFtywcPFkWcSbh
[qr] API key:        QEOmrxniOO9_PfKayKnS3pSiE-bGMc6UK9btgSS4oSM
[qr] MCP token:      8KusuwnI_8ZjKyLKYhGSyqXV2dtVjDsK1UMK8w4xZls
[qr] File created:   /temp/quickrobot/.quickrobot.env
[qr] Restart quickrobot to create fresh DB with seed data.
============================================================

```
Edit the ./.quickrobot.env

1. optional danger: Change MCP Server listening to local LAN ipv4  
QUICKROBOT_MCP_HOST=192.168.1.123 

2. optional danger: Disable the currently unencrypted (false sense of security) API Keys by changing:   
QUICKROBOT_API_KEY_DISABLED=true
QUICKROBOT_MCP_KEY_DISABLED=true

3. optional: enable quicksetup: Deploys a llama-server instance on localhost and downloads first model
Currently in testing phase - Do NOT expect to work: QUICKROBOT_QUICKSETUP=true
 
```

python3 ./quickrobot.py
[qr] 17:53:14 main() starting
[qr] 17:53:14 pre-flight stale check starting
[qr] 17:53:14 importing app/routes
[qr] 17:53:14 imported app/routes
[qr] 17:53:14 calling run_startup
[qr] v0.11 — Quickrobot API server starting...
[qr] Backing up existing database before startup
[qr] Database backed up to /temp/quickrobot/data/_backups/qr_backup_20260726_175314.db
[qr] Engine registry loaded
[qr] localhost node OK: name=localhost, hostname=Blablubblabla
Updated system-managed instance QR-API (ID 1)
Updated system-managed instance QR-WebUI (ID 2)
Updated system-managed instance QR-MCP (ID 3)
Updated system-managed instance QR-Sched (ID 4)
[qr] Instance ID range: system=1-99, user>=100
[qr] Pre-flight: all ports and processes clear
[qr] host ping started (interval=120s)
[qr] 17:53:14 run_startup returned
[qr] quickrobot API server starting on http://127.0.0.1:8039
[qr] version=v0.11 mode=prod
[qr] 17:53:14 starting waitress.serve
[qr] starting waitress.serve()
[qr] ENV: subprocess env reduced 127 → 16 keys (whitelist)
[qr] [WEBUI] auto-start: Quickrobot Webui at http://127.0.0.1:8038/webui/  pid=540300  api=127.0.0.1:8039
[qr] Quickrobot Mcp start failed: mcp package not installed. Run: pipx install mcp or pip install mcp
[qr] [SCHEDULER] auto-start: Quickrobot Scheduler at N/A (background process, no network endpoint)  pid=540308  api=127.0.0.1:8039
```

open http://127.0.0.1:8038/webui/ in Browser 

In "LLAMA.CPP" / Config: 

1. set model_root_path - After a deploy of a Llama.cpp-server instance, The "scan button" will locate and import Model files in this folder (on local / remote nodes)
2. optional danger: Set auto_auth_token=false - will NOT generate random API keys for llama-server instances.
3. optional danger: Set LLAMA_ARG_UI_MCP_PROXY=false if You do NOT want to use the llama-server-webui with the MCP.    
4. Change the "defaults" for git deployments if needed

In "Hosts": Add (optional!) additional "remote" nodes.

In "Models": Chosse the node from the drop-down menu, hit scan, Your downloaded models should be shown. 
Use Button +Preset to create a corresponding preset that can be selected during server deployment. 

In "instances": Use the "Wizzard" to select a node to deploy a llama-server instance: 
For a quick test without fresh compile from git, use a binary template for CPU, and Preset "100/no model" to start the instance without loading a model, 
OR choose the preset you have created. 

The instance should be visible with state "Deploying"  in the instance list view. 

Use "Logs" to show deploy Job - expand it to see task details. 

For debugging: Use the "Herd" section, select the server instance after deployment: 
The Button "Config & Show startup" should show the startup log.




