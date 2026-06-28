

- Fully agentic backend handling for ya Llama.cpp - or anything else
- AI²-Cluster setup for RPC + GPU + Layer- + Tensor- + Expert-split + MTP 
- Model and Preset managment, Benchmark interface
- Remote Host control for agents using ansible playbooks instead of full ssh 
- Human interface Web-UI, REST-API or MCP for agents
- 100% Coded by local Qwen3.6-35B-A3B-Q5KM at 30 t/s 
- no npm, no aur, no dockerhub, no pipe to bash
- open source, open weights, closed ai

8k Trailer VIDEO here ;o)


## Example: Expert-Split on E-waste ![full](docs/examples/llamaCPP_cluster/Expertsplit3nodesQwen36-35B-A3B/cluster_002_example_v007.md)

Nodes (1 main + 2 RPC)
| ID | CPU | Cores | RAM | GPU | Instance | Usage |
|----|-----|-------|-----|-----|----------|--------------------|
| 1 | 2013 i5-4570  | 4 ~3GHz | 4x8GB  @ DDR3-1333 | 2019 AMD 8GB RX5700 | Server | Vulkan0 = Attention+MTP+kV |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC0-CPU | 8GB experts |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC1-CPU | 8GB experts |

Model Qwen3.6-35B-A3B-MTP-Q5_K_M.gguf ~ 23GB  CTX_SIZE=262144   ~ 10t/s (details later)

## Example: Layer + MTP + Expert split on 1GB LAN ![full](docs/examples/llamaCPP_cluster/ExpertsplitThinclientStep37Flash/cluster_001_example_v007.md)




![CHANGELOG](docs/git/nice_changelog.md)

## "Security":

- NO API KEYS, NO SSL, NO mTLS, NO VPN, Insecure proxy mode, Insecure static CORS settings, No LXC, No Docker, No KxS - bring your own container, VM or airgap!
- Run Agent Harness's console and the (API) server as different users for seperation.
- REMOTE LLama.cpp SERVERS BIND TO 0.0.0.0 by default - Needs Custom per Instance override to local (v/Vx/LAN ipv4/6) and "re-deploy" - but I added warning Label in Ape interface - should be fine^^  
- TODO: non-dev-flask server for http(S) + proxy functionality if needed 
- TODO: randomize API key on server deployment and use for proxy and API interactions

## "BUT WHY?" 

- Scope of the project is to help upcycle e-waste Hardware: Too old to run win 11 ? Make it an AI-node and hold some Experts. 
- Use Your old laptop with the broken screen to store Your active context window at home on Your DDR4.


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
![Setup Quickrobot](docs/git/guide_controller.md)

WIP
![Setup Remote Node example LXC](docs/git/guide_node_lxc.md)

