# quickrobot — MCP Server Skill

The MCP server (`engine/qr_mcp_server.py`) wraps the quickrobot REST API as tools for LLM agents. It runs on `QUICKROBOT_MCP_PORT` (from `.quickrobot.env`).

**Access:** `http://127.0.0.1:<MCP_PORT>` — MCP protocol endpoint. The server respects flags set in `.quickrobot.env`: `MCP_READ`, `MCP_WRITE`, `MCP_FULLPROXY`. WRITE or PROXY implies READ.

---

## Tool Categories

### 1. Summary Tools (recommended for small LLMs)
Filtered responses — 70-95% less data than full tools. Use for routine operations.

| Tool | Signature | Purpose |
|------|-----------|---------|
| `list_instances_summary()` | None | Instance inventory: id, name, state, engine_type_name, node_hostname, port_assigned |
| `list_nodes_summary()` | None | Node availability: id, name, hostname, status, is_active, ping_state |
| `list_presets_summary(engine_type)` | `engine_type` (str) | Preset selection: id, name, category, model_name, gpu_device |
| `list_models_summary(engine_type)` | `engine_type` (str) | Model selection: id, name, model_path, quantization, size_bytes, preset_count |

**Example:** `list_instances_summary()` returns a compact list — perfect for finding which instances are running/stopped without loading 45KB of full data.

### 2. Full Detail Tools
Return complete API responses. Use only when you need fields excluded from summary versions.

| Tool | Signature | Returns |
|------|-----------|---------|
| `list_instances()` | None | All instances with config_override, merged_config, capabilities |
| `get_instance_status(instance_id)` | `instance_id` (int) | Single instance deep dive including merged_config |
| `list_nodes()` | None | All nodes with hardware inventory (capabilities) |
| `list_presets(engine_type)` | `engine_type` (str) | Presets with full config_template JSON |
| `get_preset(preset_id, engine_type)` | `preset_id` (int), `engine_type` (str) | Single preset detail |
| `list_models(engine_type)` | `engine_type` (str) | Models with SHA256 hashes and model_params |
| `get_model(model_id, engine_type)` | `model_id` (int), `engine_type` (str) | Single model detail |

### 3. Write Tools (requires MCP_WRITE)
| Tool | Signature | Purpose |
|------|-----------|---------|
| `create_instance(name, engine_type_id, node_id, preset_id, config_override)` | All params required except config_override | Create new instance |
| `deploy_instance(instance_id, start_after_deploy)` | instance_id (int), start_after_deploy (bool) | Build + deploy systemd unit |
| `change_preset(instance_id, preset_id, skip_build=True)` | instance_id (int), preset_id (int), skip_build (bool) | Change preset (async, <100ms response) |
| `start_instance(instance_id)` | instance_id (int) | Start service |
| `stop_instance(instance_id)` | instance_id (int) | Stop service |
| `restart_instance(instance_id)` | instance_id (int) | Graceful restart |
| `delete_instance(instance_id, force)` | instance_id (int), force (bool) | Remove instance |

**NOTE on `change_preset`:** Runs async via RUNNER-1 (async_mode=True). Returns instantly with `"config_update_triggered": true`. Instance stays in running state — the scheduler transitions it to "configuring" → "deploying" → "running". Do NOT poll for "running" immediately after calling.

### 4. Proxy Tool (requires MCP_FULLPROXY)
| Tool | Signature | Purpose |
|------|-----------|---------|
| `quickrobot_api(method, path, body)` | method (str), path (str), body (dict or null) | Direct pass-through to any API endpoint |

**Example:** `quickrobot_api("POST", "/health/check", {"scope": "instances"})` — works for any endpoint not covered by dedicated tools.

---

## Common Workflows

### Check System Health
```python
# 1. List all instances (summary)
list_instances_summary()

# 2. Identify problematic ones
# Look for state != "running" and engine_type_name != "system-managed" types

# 3. If needed, run health check via proxy
quickrobot_api("POST", "/health/check", {"scope": "instances"})
```

### Deploy a New llama_server Instance
```python
# 1. List available nodes (summary)
list_nodes_summary()
# Choose an active node (is_active=1)

# 2. List available models (summary) for chosen node
list_models_summary("llama_server")
# Note: list_models_summary does NOT filter by host_id — use full list_models to see which node models are on

# 3. Create instance
create_instance(
    name="my-inference-server",
    engine_type_id=21,  # llama_server
    node_id=<chosen_node_id>,
    preset_id=101,      # small test preset
    config_override=None
)

# 4. Deploy
deploy_instance(<instance_id>, start_after_deploy=True)
```

### Monitor Running Instances
```python
# Check if an instance is healthy
status = get_instance_status(106)
# status.data.alive indicates liveness
# status.data.merged_config contains resolved configuration
```

### Bulk Operations
```python
# Find all stopped llama_server instances
instances = list_instances_summary()
stopped_servers = [i for i in instances if i['state'] == 'stopped' and 'llama' in i['engine_type_name']]

# Restart them one by one
for inst in stopped_servers:
    restart_instance(inst['id'])
```

---

## MCP Flags & Permissions

| Flag | Value | Effect |
|------|-------|--------|
| `MCP_READ` | true/false | Allow read-only tools (list_instances, list_nodes, etc.) |
| `MCP_WRITE` | true/false | Allow write tools (create_instance, deploy, start, stop, restart) |
| `MCP_FULLPROXY` | true/false | Allow quickrobot_api proxy tool for arbitrary endpoints |

**WRITE implies READ** — if WRITE is enabled, the agent can also call all read tools. Proxy requires explicit FULLPROXY flag.

---

## Job & Task System (RUNNER-1) via Proxy

The job/task system is accessible through the `quickrobot_api` proxy tool. No dedicated MCP tools needed — all 5 endpoints work via proxy.

**Available endpoints:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/jobs` | GET | List jobs (filter: `?status=running`, `?instance_id=X`) |
| `/jobs/<id>` | GET | Job detail with task breakdown |
| `/tasks` | GET | List tasks (filter: `?job_id=X`, `?instance_id=X`) |
| `/tasks/<id>` | GET | Task detail with stage, timing, ansible output |
| `/instances/<id>/jobs` | GET | Jobs scoped to one instance |

**NOTE on job states (JOB-STATE-1 + JOB-DURATION-2):**
- Jobs stay `'queued'` until the scheduler claims them (~1s poll). `GET /jobs?status=running` may show nothing for newly-created jobs.
- Job duration = actual execution time only (`started_at` set at first task claim, not creation).
- Global per-job timeout: 2h (7200s). Jobs exceeding this get reset to queued with `'error'` status.

**Example via MCP proxy:**
```python
# List all running jobs
quickrobot_api("GET", "/jobs?status=running", None)

# Get job detail including task breakdown
quickrobot_api("GET", f"/jobs/{job_id}", None)
```

---

## Key Rules for MCP Users

1. **Use summary tools first** — they reduce token cost by 70-95%. Full detail tools only when needed.
2. **Engine type IDs:** llama_server=21, llama_rpc=22, iperf3=31, subprocess=12, universal=11
3. **Preset ID 1** = router mode (no model). Use for deploy/systemd tests without loading models.
4. **One benchmark at a time per instance** — the API returns BENCHMARK_RUNNING if another is active.
5. **RPC servers use binary protocol** — not HTTP. Health check via `curl /health` works on llama_server only.
6. **Preset changes are async** — `change_preset` returns <100ms with `"config_update_triggered": true`. The instance state transitions (running → configuring → deploying → running) happen in background. Check status via `GET /instances/<id>` after a few seconds.
7. **Config changes need no daemon-reload** — `$QR_CLI_ARGS_JOINED` env file pattern: preset/config changes write the env file and restart the service. No systemctl reload needed (CONFIG-1).
8. **RPC preset must match node cores** — Check `list_nodes_summary()` for each node's `cpu_cores`. If a node has 2 cores, use an RPC preset with 2 threads (not 4). Over-allocating threads on thin clients causes thrashing and slower inference.
9. **Bind requires manual reconfigure** — After `bind-rpc` via MCP proxy, the server needs `change_preset(instance_id, same_preset_id, skip_build=True)` or `deploy_instance(instance_id)` to regenerate the remote env file with updated `--rpc`, `-dev`, and `LLAMA_ARG_TENSOR_SPLIT`. Without this, the server still uses the old config.
10. **MCP tools may have stale SSE session** — Early in a session, MCP tools can return `MCP error -32602: Invalid request parameters` if the opencode harness has a cached stale SSE session ID from a prior API restart. This typically resolves after 5-30 minutes of API activity as the session auto-recovers. If all tools fail simultaneously, retry after a short wait or use `quickrobot_api` proxy as fallback.
11. **Benchmark metrics are always `{}`** — The `benchmark_results` table has no `metrics` column; the API returns it as an empty default. This is a schema gap, not a failure. `success=1` means: the llama-server responded, text was captured in the `output` column, and the run completed (not just started). The `response_json` column contains `tokens_predicted` and `tokens_evaluated` from the llama.cpp `/completion` response — these are useful proxy metrics for throughput comparison. Use `duration_ms` for total wall-clock time.
