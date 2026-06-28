# llama.cpp Cluster Configuration — Expert Split 3 Nodes (Qwen3.6-35B-A3B)

**Instance ID:** 108 (name: `servmeah`)  
**Host:** dllama4.lan (node 5)  
**Date:** 2026-06-28  
**Model:** Qwen3.6-35B-A3B-MTP-Q5_K_M.gguf (23GB, single file, MTP-preserving)

---

## Model & Preset

| Field | Value |
|-------|-------|
| **Model ID** | 452 |
| **Model Name** | Qwen3.6-35B-A3B-MTP-Q5_K_M.gguf |
| **Model Path** | `/mnt/llama/gguf/models/llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q5_K_M.gguf` |
| **Preset ID** | 107 |
| **Preset Name** | QR-DESIGNER: Qwen36-35B-A3B-Q5KM-23GB |
| **Preset Env** | BATCH=2048, UBATCH=512 (overridden to 128), CACHE_RAM=2048, N_GPU_LAYERS=100, N_PARALLEL=1, FLASH_ATTN=1, KV_UNIFIED=1, CTX_SIZE=262144 |
| **Preset CLI** | `--no-context-shift --presence-penalty 1.0 --repeat-penalty 1.0 --jinja --spec-draft-n-max 2 --spec-type draft-mtp --ctx-checkpoints 64 --checkpoint-min-step 4096` |

---

## Instance Settings (config_override)

```json
{
  "LLAMA_ARG_CTX_SIZE": "262144",
  "LLAMA_ARG_BATCH": "2048",
  "LLAMA_ARG_UBATCH": "128",
  "LLAMA_ARG_DEVICE": "Vulkan0",
  "cli_flags": [
    "--flash-attn", "on",
    "--spec-type", "draft-mtp",
    "--spec-draft-ngl", "0",
    "-v",
    "--device-draft", "Vulkan0",
    "--tools read_file,get_datetime"
  ],
  "expert_split": {
    "template_prefix": "blk.",
    "template_suffix": "ffn_(up|gate|down)_exps.*",
    "_rpc_modes": {
      "104": { "mode": "a" },
      "105": { "mode": "a" }
    }
  }
}
```

- **Split Mode:** `layer`
- **Split Value:** `100` (server takes 100% of tensor_split)
- **Tensor Split:** `100,0,0` (server=100%, both RPCs=0%)
- **Expert Split Mode:** A (Stride) — interleaved expert allocation, 25 experts per RPC

---

## RPC Bindings (2 nodes total)

| ID | Name | Host | Port | Split | Experts | Mode |
|----|------|------|------|-------|---------|------|
| 104 | mintiger-rpc-4t | mintiger.lan | 50052 | 0 | 25 | A (Stride) |
| 105 | bender-rpc-4t | bender.lan | 50052 | 0 | 25 | A (Stride) |

**Total experts:** 50 (25+25)  
Both RPCs: 0% tensor_split — they serve expert layers only via `-ot` flags.

---

## Expert-Split Flags (generated -ot flags)

```
-ot "blk.(0|2|4|6|8|10|12|14|16|18|20|22|24|26|28|30|32|34|36|38|40|42|44|46|48).ffn_(up|gate|down)_exps.*=RPC0[mintiger.lan:50052]"
-ot "blk.(1|3|5|7|9|11|13|15|17|19|21|23|25|27|29|31|33|35|37|39|41|43|45|47|49).ffn_(up|gate|down)_exps.*=RPC0[bender.lan:50052]"
```

**Allocation strategy:** Mode A (Stride) — interleaved allocation via greedy distance-maximization. Even indices to mintiger (25), odd indices to bender (25). Total 50 experts, each RPC gets exactly half distributed with maximum spacing.

---

## Nodes (1 main + 2 RPC)

| ID | Name | Hostname | Cores | RAM | GPU | OS |
|----|------|----------|-------|-----|-----|----|
| 5 | LXC-32G-DDR3-PVE-Navi10 | dllama4.lan | 4 | 8192 MB | Vulkan (Mesa Navi10) | Debian 13 |
| 6 | LX-Mint-32G-Vulkan-iGPU | mintiger.lan | 4 | 31011 MB | Intel HD 530 (Vulkan) | Linux Mint 22.3 |
| 7 | Bender-31G-Vulkan-iGPU | bender.lan | 4 | 31020 MB | Intel HD 530 (Vulkan) | Debian 13 |

All nodes use SSH user `mepaw`, port 22. Model base path: `/mnt/llama/gguf/models`

---

## Full CLI Args (deploy-preview output)

```
--host 0.0.0.0 --port 8080
--rpc mintiger.lan:50052,bender.lan:50052
-dev Vulkan0,RPC0,RPC1
-ot "blk.(0|2|4|6|8|10|12|14|16|18|20|22|24|26|28|30|32|34|36|38|40|42|44|46|48).ffn_(up|gate|down)_exps.*=RPC0[mintiger.lan:50052]"
-ot "blk.(1|3|5|7|9|11|13|15|17|19|21|23|25|27|29|31|33|35|37|39|41|43|45|47|49).ffn_(up|gate|down)_exps.*=RPC0[bender.lan:50052]"
--flash-attn on --spec-type draft-mtp --spec-draft-ngl 0 -v --device-draft Vulkan0 --tools read_file,get_datetime
```

## Full Environment Variables

```
LLAMA_ARG_BATCH=2048
LLAMA_ARG_CACHE_RAM=2048
LLAMA_ARG_CTX_SIZE=262144
LLAMA_ARG_FIT=off
LLAMA_ARG_FLASH_ATTN=1
LLAMA_ARG_HOST=0.0.0.0
LLAMA_ARG_KV_UNIFIED=1
LLAMA_ARG_MIN_P=0.0
LLAMA_ARG_MMAP=false
LLAMA_ARG_MODEL=/mnt/llama/gguf/models/llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q5_K_M.gguf
LLAMA_ARG_N_GPU_LAYERS=100
LLAMA_ARG_N_PARALLEL=1
LLAMA_ARG_PORT=8080
LLAMA_ARG_SEED=1337
LLAMA_ARG_SPLIT_MODE=layer
LLAMA_ARG_TEMP=0.6
LLAMA_ARG_TENSOR_SPLIT=100,0,0
LLAMA_ARG_TOP_K=20
LLAMA_ARG_TOP_P=0.95
LLAMA_ARG_UBATCH=128
LLAMA_ARG_UI=true
LLAMA_ARG_UI_MCP_PROXY=true
LLAMA_CHAT_TEMPLATE_KWARGS={"enable_thinking": true, "preserve_thinking": true}
```

---

## Recreation Prompt

Copy the text below into an agent context (MCP or direct prompt) to recreate this exact cluster configuration from scratch:

---

**PROMPT BEGINS HERE**

You are setting up a llama.cpp MoE cluster with 2 RPC nodes using quickrobot. Use MCP tools or direct API calls. Execute in this order:

### Part A — Node Setup (3 existing nodes)

Create all 3 nodes first. Each `create_node` call auto-validates via SSH and discovers hardware:

```
create_node(name="LXC-32G-DDR3-PVE-Navi10", hostname="dllama4.lan", ansible_user="mepaw")
create_node(name="LX-Mint-32G-Vulkan-iGPU", hostname="mintiger.lan", ansible_user="mepaw")
create_node(name="Bender-31G-Vulkan-iGPU", hostname="bender.lan", ansible_user="mepaw")
```

Wait for all nodes to be `online` (check with `list_nodes_summary`). Note the node IDs returned — you will need them. Rename the responses:
- dllama4.lan node ID = `<node_dllama4>`
- mintiger.lan node ID = `<node_mintiger>`
- bender.lan node ID = `<node_bender>`

### Part B — Cluster Setup (from 3 existing nodes)

#### Step 1: Create 2 RPC instances (engine_type_id=22, preset 15)

Both RPCs use preset 15 (RPC-CPU-4Threads: `LLAMA_ARG_HOST=0.0.0.0`, `-d CPU -t 4`). Override `experts` via config_override.

```
create_instance(name="mintiger-rpc-4t", engine_type_id=22, node_id=<node_mintiger>, preset_id=15, config_override={"experts": 25})
create_instance(name="bender-rpc-4t", engine_type_id=22, node_id=<node_bender>, preset_id=15, config_override={"experts": 25})
```

Wait for both RPC instances to reach `running` state (use `list_instances_summary`). Note their IDs:
- mintiger-rpc-4t ID = `<rpc_mintiger>`
- bender-rpc-4t ID = `<rpc_bender>`

#### Step 2: Create the main llama-server instance

Use preset 107 (QR-DESIGNER: Qwen36-35B-A3B-Q5KM-23GB). Include expert_split config_override with mode "a" for both RPCs.

```
create_instance(
  name="servmeah",
  engine_type_id=21,
  node_id=<node_dllama4>,
  preset_id=107,
  config_override={
    "expert_split": {
      "template_prefix": "blk.",
      "template_suffix": "ffn_(up|gate|down)_exps.*",
      "_rpc_modes": {
        "<rpc_mintiger>": {"mode": "a"},
        "<rpc_bender>": {"mode": "a"}
      }
    },
    "LLAMA_ARG_CTX_SIZE": "262144",
    "LLAMA_ARG_BATCH": "2048",
    "LLAMA_ARG_UBATCH": "128",
    "LLAMA_ARG_DEVICE": "Vulkan0",
    "cli_flags": [
      "--flash-attn", "on",
      "--spec-type", "draft-mtp",
      "--spec-draft-ngl", "0",
      "-v",
      "--device-draft", "Vulkan0",
      "--tools read_file,get_datetime"
    ]
  }
)
```

Note the server instance ID = `<server_id>`.

#### Step 3: Bind RPCs to the server (in exact order)

Use `PUT /instances/<server_id>` with `rpc_bind_ids` array. All RPCs must be in `running` state before this step.

```
# Bind both RPCs
PUT /api/v1/instances/<server_id>
  Body: {"rpc_bind_ids": [<rpc_mintiger>, <rpc_bender>]}
```

#### Step 4: Set split mode and server split

```
PATCH /api/v1/instances/<server_id>/split-mode
  Body: {"split_mode": "layer"}

# Server takes 100% of tensor_split — RPCs get expert layers only via -ot flags
PUT /api/v1/instances/<server_id>/split
  Body: {"split": 100}
```

#### Step 5: Deploy config and restart server

```
POST /api/v1/instances/<server_id>/deploy
  Body: {"skip_build": true}
```

After deploy completes (instance shows "running"), restart to apply expert-split flags:

```
POST /api/v1/instances/<server_id>/restart
```

#### Step 6: Verify

```
GET /api/v1/instances/<server_id>/deploy-preview
# Verify tensor_split="100,0,0", -ot flags show interleaved stride pattern

GET /api/v1/rpccluster/summary
# Verify bind_count=2, tensor_split="100,0,0", both RPCs running
```

---

**Important notes:**
- All instances auto-deploy and auto-start on creation (no separate deploy/start needed)
- ALL RPCs must be in `running` state BEFORE the server gets (re)started
- Only the server needs restart for expert-split bindings to take effect — RPCs keep running
- The expert-split mode "A" (Stride) creates interleaved `-ot` flags: even block indices go to first RPC, odd to second
- `tensor_split=100,0,0` means server takes all normal tensors; RPCs serve expert-only via `-ot` flags
- GPU override "Vulkan0" requires Vulkan drivers — installed during `install_deps.yml` build step
- Model files only need to be on the server node (dllama4.lan); llama-server pushes model shards to bound RPCs at startup
- Model path: `/mnt/llama/gguf/models/llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q5_K_M.gguf`

**PROMPT ENDS HERE</parameter>
<parameter=filePath>
/CORE/projects/quickrobot/docs/examples/llamaCPP_cluster/Expertsplit3nodesQwen36-35B-A3B/cluster_108_export.md