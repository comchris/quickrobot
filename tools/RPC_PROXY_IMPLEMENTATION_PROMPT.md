# RPC Multiplexing Proxy — Implementation Brief

You are implementing the RPC multiplexing proxy from `tools/rpc_proxy_implementation_v010.md` (502 lines, design spec).

## Context

The proxy multiplexes N llama-server instances over 1 RPC server, sharing expert weights to reduce VRAM usage. Currently each llama-server opens its own TCP connection to the RPC server and loads duplicate model tensors on the RPC side. The proxy consolidates this to 1 connection with shared buffer allocation.

## Key Design Decisions

1. **Multi-port listener** — proxy listens on `base_port + max_connections` (default 4). Each llama-server connects to its own port, identified by port offset.
2. **FIFO command serialization** — all commands from all clients queue up and dispatch one-at-a-time on the single RPC connection. Responses routed back to originating client.
3. **Shared buffer via first-wins** — first ALLOC_BUFFER + SET_TENSOR loads model on RPC side. Subsequent clients' duplicate allocations are remapped to existing `remote_ptr`.
4. **Model fingerprinting** — SHA256 of sorted tensor names+shapes+type. First client establishes baseline; subsequent clients compared against it. Mismatch → reject sharing, normal flow.
5. **Logging** — `-v/--verbose` for console output, `/tmp/rpc_proxy.log` (10MB rotation) always active.

## Files to Create

```
/opt/rpc_proxy/rpc_proxy.py          # Main proxy script (~650 lines Python stdlib)
data/_seed/seed_vXXX.sql             # (future) seed entry for proxy service config
```

## Implementation Phases

### Phase 1: Core Proxy (~400 lines)
- Multi-port TCP listener on `[base_port, base_port + max_connections)`
- Connection acceptance with `client_id` derived from port offset
- RPC connection to backend server (single socket)
- Command forwarding: HELLO, ALLOC_BUFFER, SET_TENSOR, GRAPH_COMPUTE, GRAPH_RECOMPUTE
- FIFO queue for GRAPH_COMPUTE serialization
- Console logging + file logging with rotation

### Phase 2: Shared Buffer Logic (~150 lines)
- Tensor name extraction from SET_TENSOR commands
- `shared_buffers` registry (tensor_name → remote_ptr, first wins)
- Response remapping for duplicate ALLOC_BUFFER
- Model fingerprinting: SHA256 of sorted tensor metadata, baseline establishment, comparison

### Phase 3: Robustness (~100 lines)
- Client disconnect detection → buffer cleanup
- RPC connection failure → reconnect
- Graceful shutdown
- Health endpoint (HTTP GET /health → status JSON)

### Phase 4: Quickrobot Integration
- MCP tools: `rpc_proxy_start(endpoint, base_port, max_connections)`, `rpc_proxy_stop()`
- WebUI section on herd page showing proxy status, connected clients, shared buffers
- Playbook deploy for remote nodes

## Reference Documents

- **Design analysis:** `tools/rpc_multiplexing_proxy_v010.md` (356 lines) — protocol deep-dive, architecture, buffer coordination
- **Implementation spec:** `tools/rpc_proxy_implementation_v010.md` (502 lines) — exact data structures, command handlers, logging, deployment

## TODO References

- `docs/TODO.md#RPC-MULTIPLEX` — main task entry
- `docs/design/expert_split_enhancements_v010.md` §E — related to expert split enhancements

## Quick Start (Manual SSH Testing)

```bash
# 1. Create script
cp tools/rpc_proxy_implementation_v010.md /tmp/rpc_proxy.py
# (adjust — actual code needs to be written from spec)

# 2. Start proxy on RPC node
python3 /tmp/rpc_proxy.py \
  --base-port 50052 \
  --max-connections 4 \
   --rpc-endpoint <rpc-host>:50052 \
  -v

# 3. From each llama-server, connect to proxy port instead of direct RPC
# 4. Verify VRAM sharing via `nvidia-smi` on RPC node
```

## Verification Checklist

After implementation:
- [ ] Proxy starts and listens on configured ports
- [ ] Client connects → HELLO forwarded correctly
- [ ] First client's model tensors loaded on RPC server
- [ ] Second client's ALLOC_BUFFER remapped to shared buffers
- [ ] GRAPH_COMPUTE queued and serialized (one at a time)
- [ ] Model mismatch detected when different models connect
- [ ] File logging works with rotation at 10MB
- [ ] `-v` flag enables verbose console output
- [ ] Disconnect → buffer cleanup works
- [ ] `nvidia-smi` shows ~1× model VRAM instead of N×
