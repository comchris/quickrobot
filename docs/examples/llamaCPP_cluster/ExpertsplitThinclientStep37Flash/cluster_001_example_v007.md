# llama.cpp Cluster Configuration — Expert Split Thin Client Step 3.7 Flash

**Instance ID:** 108 (name: `servmeah`)  
**Host:** dllama4.lan (node 5)  
**Date:** 2026-06-27  
**Model:** Step-3.7-flash-Q3_K_M.gguf (88GB, Q3_K_M quantization, 3 shards)

---

## Model & Preset

| Field | Value |
|-------|-------|
| **Model ID** | 398 |
| **Model Name** | Step-3.7-flash-Q3_K_M.gguf-88GB |
| **Model Path** | `/mnt/llama/gguf/models/stepfun-ai/Step-3.7-Flash-GGUF/Step-3.7-flash-Q3_K_M-00001-of-00003.gguf` |
| **MMProj Path** | `/mnt/llama/gguf/models/stepfun-ai/Step-3.7-Flash-GGUF/mmproj-step3.7-flash-f16.gguf` |
| **Draft Model** | `/mnt/llama/gguf/models/stepfun-ai/Step-3.7-Flash-GGUF/Step3.7-flash-mtp-Q8_0.gguf` |
| **Preset ID** | 111 |
| **Preset Name** | QR-BIG-Step37flash-Q3KM-87GB |
| **Preset Env** | BATCH=2048, UBATCH=512, CACHE_RAM=2048, N_GPU_LAYERS=100, N_PARALLEL=1, FLASH_ATTN=1, KV_UNIFIED=1, CTX_SIZE=262144 |

---

## Instance Settings (config_override)

```json
{
  "LLAMA_ARG_CTX_SIZE": "32768",
  "LLAMA_ARG_DEVICE": "Vulkan0",
  "cli_flags": [
    "--no-repack",
    "--spec-type", "draft-mtp",
    "--spec-draft-n-min", "5",
    "-v",
    "--device-draft", "Vulkan0",
    "--tools", "read_file,get_datetime"
  ],
  "expert_split": {
    "template_prefix": "blk.",
    "template_suffix": "ffn_(up|gate|down)_exps.*",
    "_rpc_modes": {
      "100": { "mode": "c" },
      "101": { "mode": "c" },
      "102": { "mode": "c" },
      "104": { "mode": "c" },
      "105": { "mode": "c" },
      "106": { "mode": "c" },
      "107": { "mode": "c" }
    }
  }
}
```

- **Split Mode:** `layer`
- **Split Value:** `0` (server contributes 0%, RPCs take all)
- **Tensor Split:** `0,100,100,0,0,0,0,0` (server=0, mintiger=100, bender=100, dllama1-3/6/7=0 each)

---

## RPC Bindings (7 nodes total)

| ID | Name | Host | Port | Split | Experts | Mode |
|----|------|------|------|-------|---------|------|
| 104 | mintiger-rpc-4t | mintiger.lan | 50052 | 100 | 0 | C (Load-Dist) |
| 105 | bender-rpc-4t | bender.lan | 50052 | 100 | 0 | C (Load-Dist) |
| 100 | dllama1-rpc-2t | dllama1.lan | 50052 | 0 | 11 | C (Load-Dist) |
| 101 | dllama2-rpc-2t | dllama2.lan | 50052 | 0 | 11 | C (Load-Dist) |
| 102 | dllama3-rpc-2t | dllama3.lan | 50052 | 0 | 11 | C (Load-Dist) |
| 106 | dllama6-rpc-2t | dllama6.lan | 50052 | 0 | 7 | C (Load-Dist) |
| 107 | dllama7-rpc-2t | dllama7.lan | 50052 | 0 | 7 | C (Load-Dist) |

**Total experts:** 48 (11+11+11+7+7)  
**mintiger & bender:** 0 experts — these handle tensor splitting and serving, not expert computation

---

## Expert-Split Flags (generated -ot flags)

```
-ot "blk.(0|5|10|15|20|25|30|35|38|41|44).ffn_(up|gate|down)_exps.*=RPC0[dllama1.lan:50052]"
-ot "blk.(1|6|11|16|21|26|31|36|39|42|45).ffn_(up|gate|down)_exps.*=RPC0[dllama2.lan:50052]"
-ot "blk.(2|7|12|17|22|27|32|37|40|43|46).ffn_(up|gate|down)_exps.*=RPC0[dllama3.lan:50052]"
-ot "blk.(3|8|13|18|23|28|33).ffn_(up|gate|down)_exps.*=RPC0[dllama6.lan:50052]"
-ot "blk.(4|9|14|19|24|29|34).ffn_(up|gate|down)_exps.*=RPC0[dllama7.lan:50052]"
```

**Allocation strategy:** Mode C (Load-Dist) — greedy distance-maximization with per-node expert quotas. dllama1-2-3 get 11 each, dllama6-7 get 7 each, distributed to maximize spacing.

---

## Nodes (8 RPC servers + 1 main server)

| ID | Name | Hostname | Cores | RAM | GPU | OS | Free GB |
|----|------|----------|-------|-----|-----|----|---------|
| 5 | LXC-32G-DDR3-PVE-Navi10 | dllama4.lan | 4 | 8192 MB | Vulkan (Mesa) | Debian 13 | 21 |
| 6 | LX-Mint-32G-Vulkan-iGPU | mintiger.lan | 4 | 31020 MB | Intel HD 530 (Vulkan) | Linux Mint 22.3 | 51 |
| 7 | Bender-31G-Vulkan-iGPU | bender.lan | 4 | 31020 MB | Intel HD 530 (Vulkan) | Debian 13 | 39 |
| 2 | LXC-32G-DDR4-Thinclient-1 | dllama1.lan | 2 | 26624 MB | Vulkan (Mesa) | Debian 13 | 20 |
| 3 | LXC-32G-DDR4-Thinclient-2 | dllama2.lan | 2 | 26624 MB | Vulkan (Mesa) | Debian 13 | 5 |
| 4 | LXC-32G-DDR4-Thinclient-3 | dllama3.lan | 2 | 26624 MB | Vulkan (Mesa) | Debian 13 | 18 |
| 8 | BareM-16G-DDR4-Thinclient-6 | dllama6.lan | 2 | 14940 MB | AMD Stoney (Vulkan) | Debian 13 | 19 |
| 9 | BareM-16G-DDR4-Thinclient-7 | dllama7.lan | 2 | 14940 MB | AMD Stoney (Vulkan) | Debian 13 | 17 |

All nodes use SSH user `mepaw`, port 22. Model base path: `/mnt/llama/gguf/models`

---

## Full CLI Args (deploy-preview output)

```
--host 0.0.0.0 --port 8080
--rpc mintiger.lan:50052,bender.lan:50052,dllama1.lan:50052,dllama2.lan:50052,dllama3.lan:50052,dllama6.lan:50052,dllama7.lan:50052
-dev Vulkan0,RPC0,RPC1,RPC2,RPC3,RPC4,RPC5,RPC6
-ot "blk.(0|5|10|15|20|25|30|35|38|41|44).ffn_(up|gate|down)_exps.*=RPC0[dllama1.lan:50052]"
-ot "blk.(1|6|11|16|21|26|31|36|39|42|45).ffn_(up|gate|down)_exps.*=RPC0[dllama2.lan:50052]"
-ot "blk.(2|7|12|17|22|27|32|37|40|43|46).ffn_(up|gate|down)_exps.*=RPC0[dllama3.lan:50052]"
-ot "blk.(3|8|13|18|23|28|33).ffn_(up|gate|down)_exps.*=RPC0[dllama6.lan:50052]"
-ot "blk.(4|9|14|19|24|29|34).ffn_(up|gate|down)_exps.*=RPC0[dllama7.lan:50052]"
--no-repack --spec-type draft-mtp --spec-draft-n-min 5 -v --device-draft Vulkan0 --tools read_file,get_datetime
```

## Full Environment Variables

```
LLAMA_ARG_BATCH=2048
LLAMA_ARG_CACHE_RAM=2048
LLAMA_ARG_CTX_SIZE=32768
LLAMA_ARG_FIT=off
LLAMA_ARG_FLASH_ATTN=1
LLAMA_ARG_HOST=0.0.0.0
LLAMA_ARG_KV_UNIFIED=1
LLAMA_ARG_MMAP=false
LLAMA_ARG_MODEL=/mnt/llama/gguf/models/stepfun-ai/Step-3.7-Flash-GGUF/Step-3.7-flash-Q3_K_M-00001-of-00003.gguf
LLAMA_ARG_MMPROJ=/mnt/llama/gguf/models/stepfun-ai/Step-3.7-Flash-GGUF/mmproj-step3.7-flash-f16.gguf
LLAMA_ARG_N_GPU_LAYERS=100
LLAMA_ARG_N_PARALLEL=1
LLAMA_ARG_PORT=8080
LLAMA_ARG_SEED=1337
LLAMA_ARG_SPEC_DRAFT_MODEL=/mnt/llama/gguf/models/stepfun-ai/Step-3.7-Flash-GGUF/Step3.7-flash-mtp-Q8_0.gguf
LLAMA_ARG_SPLIT_MODE=layer
LLAMA_ARG_TENSOR_SPLIT=0,100,100,0,0,0,0,0
LLAMA_ARG_UBATCH=512
LLAMA_ARG_UI=true
LLAMA_ARG_UI_MCP_PROXY=true
```

---

## Recreation Prompt

Copy the text below into an agent context (MCP or direct prompt) to recreate this exact cluster configuration from scratch:

---

**PROMPT BEGINS HERE**

You are setting up a llama.cpp MoE cluster with 7 RPC nodes using quickrobot. Use MCP tools or direct API calls. Execute in this order:

### Step 1: Create all 8 nodes (1 main + 7 RPC)

```
create_node(name="LXC-32G-DDR3-PVE-Navi10", hostname="dllama4.lan", ansible_user="mepaw")
create_node(name="LX-Mint-32G-Vulkan-iGPU", hostname="mintiger.lan", ansible_user="mepaw")
create_node(name="Bender-31G-Vulkan-iGPU", hostname="bender.lan", ansible_user="mepaw")
create_node(name="LXC-32G-DDR4-Thinclient-1", hostname="dllama1.lan", ansible_user="mepaw")
create_node(name="LXC-32G-DDR4-Thinclient-2", hostname="dllama2.lan", ansible_user="mepaw")
create_node(name="LXC-32G-DDR4-Thinclient-3", hostname="dllama3.lan", ansible_user="mepaw")
create_node(name="BareM-16G-DDR4-Thinclient-6", hostname="dllama6.lan", ansible_user="mepaw")
create_node(name="BareM-16G-DDR4-Thinclient-7", hostname="dllama7.lan", ansible_user="mepaw")
```

Wait for all nodes to be `online` (check with `list_nodes_summary`). Note the node IDs returned.

### Step 2: Create 7 RPC instances

Replace `<NODE_ID>` with the actual ID from step 1 results:

```
# 4-core nodes (mintiger, bender) → RPC-CPU-4Threads preset (preset 15)
create_instance(name="mintiger-rpc-4t", engine_type_id=22, node_id=<mintiger_node_id>, preset_id=15)
create_instance(name="bender-rpc-4t", engine_type_id=22, node_id=<bender_node_id>, preset_id=15)

# 2-core thin clients (dllama1-3) → RPC-CPU-2Threads preset (preset 14), experts=11 each
create_instance(name="dllama1-rpc-2t", engine_type_id=22, node_id=<dllama1_node_id>, preset_id=14, config_override={"experts": 11})
create_instance(name="dllama2-rpc-2t", engine_type_id=22, node_id=<dllama2_node_id>, preset_id=14, config_override={"experts": 11})
create_instance(name="dllama3-rpc-2t", engine_type_id=22, node_id=<dllama3_node_id>, preset_id=14, config_override={"experts": 11})

# 2-core thin clients (dllama6-7) → RPC-CPU-2Threads preset (preset 14), experts=7 each
create_instance(name="dllama6-rpc-2t", engine_type_id=22, node_id=<dllama6_node_id>, preset_id=14, config_override={"experts": 7})
create_instance(name="dllama7-rpc-2t", engine_type_id=22, node_id=<dllama7_node_id>, preset_id=14, config_override={"experts": 7})
```

Wait for all RPC instances to reach `running` state (use `list_instances_summary`).

### Step 3: Create the main llama-server instance

Use preset 111 (QR-BIG-Step37flash-Q3KM-87GB). Replace `<DLLAMA4_NODE_ID>` with node 5's ID:

```
create_instance(
  name="servmeah",
  engine_type_id=21,
  node_id=<dllama4_node_id>,
  preset_id=111,
  config_override={
    "expert_split": {
      "template_prefix": "blk.",
      "template_suffix": "ffn_(up|gate|down)_exps.*",
      "_rpc_modes": {
        "100": {"mode": "c"},
        "101": {"mode": "c"},
        "102": {"mode": "c"},
        "104": {"mode": "c"},
        "105": {"mode": "c"},
        "106": {"mode": "c"},
        "107": {"mode": "c"}
      }
    },
    "LLAMA_ARG_CTX_SIZE": "32768",
    "LLAMA_ARG_DEVICE": "Vulkan0",
    "cli_flags": [
      "--no-repack",
      "--spec-type", "draft-mtp",
      "--spec-draft-n-min", "5",
      "-v",
      "--device-draft", "Vulkan0",
      "--tools", "read_file,get_datetime"
    ]
  }
)
```

### Step 4: Bind RPCs to the server (in exact order)

Use the `quickrobot_api` proxy for the bind endpoint. Replace `<INSTANCE_108_ID>` with the actual ID from step 3:

```
# First bind mintiger and bender (the 100% split nodes, 0 experts)
PUT /api/v1/instances/<INSTANCE_108_ID>
  Body: {"rpc_bind_ids": [<mintiger_rpc_id>, <bender_rpc_id>]}

# Then append dllama1-3 (each gets 11 experts)
PUT /api/v1/instances/<INSTANCE_108_ID>
  Body: {"rpc_bind_ids": [<mintiger_rpc_id>, <bender_rpc_id>, <dllama1_rpc_id>, <dllama2_rpc_id>, <dllama3_rpc_id>]}

# Finally append dllama6-7 (each gets 7 experts)
PUT /api/v1/instances/<INSTANCE_108_ID>
  Body: {"rpc_bind_ids": [<mintiger_rpc_id>, <bender_rpc_id>, <dllama1_rpc_id>, <dllama2_rpc_id>, <dllama3_rpc_id>, <dllama6_rpc_id>, <dllama7_rpc_id>]}
```

### Step 5: Set split mode and tensor split

```
PATCH /api/v1/instances/<INSTANCE_108_ID>/split-mode
  Body: {"split_mode": "layer"}

# Set server split to 0 (server contributes 0% of tensor_split)
PUT /api/v1/instances/<INSTANCE_108_ID>/split
  Body: {"split": 0}
```

### Step 6: Deploy config and restart

```
POST /api/v1/instances/<INSTANCE_108_ID>/deploy
  Body: {"skip_build": true}

# After deploy completes (instance shows "running"), restart to apply changes:
POST /api/v1/instances/<INSTANCE_108_ID>/restart
```

### Step 7: Verify

```
GET /api/v1/instances/<INSTANCE_108_ID>/deploy-preview
# Verify the expert_split flags look correct. Should show 5 -ot flags for dllama1-3,6,7 with interleaved indices.

GET /api/v1/rpccluster/summary
# Verify bind_count=7, tensor_split="0,100,100,0,0,0,0,0", all RPCs running.
```

**Important notes:**
- All instances auto-deploy and auto-start on creation (no separate deploy/start needed)
HUMAN! Yes - only the server needs a restart for configs to take effect.
       ALL RPCs must be in running state before Server gets (re)started!

- The expert-split mode "C" (Load-Dist) uses greedy distance-maximization allocation respecting per-RPC expert quotas
HUMAN: We still have to find the perfect split, but this spreads the load
- mintiger and bender have 0 experts — they handle tensor splitting and model serving but not expert computation
HUMAN: Yes, they get the "attention" layers
- The server's tensor_split is `0,100,100,0,0,0,0,0` meaning the server takes 0%, mintiger takes 100% of its share, bender takes 100% of its share, dllama nodes take 0% each
HUMAN: in the special split above: first Zero is so that the Vulkanß gets 0 normal tensors, but the draft model 
       the 100,100, should put all "normal" layers on the 2 RPCs in a 50:50 split 
       the last 0,0,0,0,0 will keep these RPC free of "normal layers" - Those Nodes get all the Expert Layers with the help of the -ot flags
       

- GPU override is "Vulkan0" — make sure each node has Vulkan drivers installed
HUMAN: Kindoff - we need vulkan installed for compiling with Vulkan drivers - but thats why we install deps for
- Model files must be on all nodes at: `/mnt/llama/gguf/models/stepfun-ai/Step-3.7-Flash-GGUF/`
HUMAN: NO Only the llama-server nodes needs the model files and push to RPCs.

**PROMPT ENDS HERE**
