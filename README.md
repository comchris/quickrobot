

- Fully agentic backend handling for ya Llama.cpp - or anything else
- AI²-Cluster setup for RPC + GPU + Layer- + Tensor- + Expert-split + MTP 
- Model and Preset managment, Benchmark interface
- Remote Host control for agents using ansible playbooks instead of full ssh 
- Human interface Web-UI, REST-API or MCP for agents
- 100% Coded by local Qwen3.6-35B-A3B-Q5KM at 30 t/s 
- no npm, no aur, no dockerhub, no pipe to bash
- open source, open weights, closed ai

8k Trailer VIDEO here ;o)

-Prompt + Hosts example-

-Prompt + Cluster example-





## Example 2: Expert-Split on E-waste

Nodes (1 main + 2 RPC)
| ID | CPU | Cores | RAM | GPU | Instance | Flags used |
|----|-----|-------|-----|-----|----------|--------------------|
| 1 | 2013 i5-4570  | 4 ~3GHz | 4x8GB  @ DDR3-1333 | 2019 AMD 8GB RX5700 | Server | -dev Vulkan0, layer-split 100,0,0 = Attention+MTP+kV |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC0-CPU | -ot "blk.(0|2|4|6|8|...46).ffn_(up|gate|down)_exps.* = 8GB experts |
| 3 | 2015 i5-6500T | 4 ~3GHz | 2x16GB @ DDR4-2400 | intel onboard HD530 | RPC1-CPU | -ot "blk.(1|3|5|7|9|...47).ffn_(up|gate|down)_exps.* = 8GB experts |

Model Qwen3.6-35B-A3B-MTP-Q5_K_M.gguf ~ 23GB  CTX_SIZE=262144



![Setup Quickrobot](docs/git/guide_controller.md)


![Setup Remote Node example LXC](docs/git/guide_node_lxc.md)



![CHANGELOG](docs/git/nice_changelog.md)

## "Security":

- NO API KEYS, NO SSL, NO mTLS, NO VPN, Insecure proxy mode, Insecure static CORS settings, No LXC, No Docker, No KxS - bring your own container, VM or airgap!
- Run Agent Harness's console and the (API) server as different users for seperation.
- REMOTE LLama.cpp SERVERS BIND TO 0.0.0.0 by default - Needs Custom per Instance override to local (v/Vx/LAN ipv4/6) and "re-deploy" - but I added warning Label in Ape interface - should be fine^^  
- TODO: non-dev-flask server for http(S) + proxy functionality if needed 
- TODO: randomize API key on server deployment and use for proxy and API interactions

## "BUT WHY?" 

- Scope of the project is to help upcycle e-waste Hardware: Can't run win 11 ? -> Load the Experts of a 120B LLM instead.
- Use Your old laptop with the broken screen to store Your active context window at home on DDR4 - on Your hardware

## including Human interface

![RPCandClusterSetup](docs/pics/herd_007.png)

![ListOfEngines](docs/pics/instances_007.png)

![ListOfComputers](docs/pics/hosts_006.png)

![TopModels](docs/pics/models_006.png)


