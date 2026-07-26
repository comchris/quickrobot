# RPC Multiplexing Proxy — Option B Design Analysis

> **Purpose:** Analyze and specify a proxy that multiplexes N llama-servers over 1 RPC instance, sharing VRAM load while maintaining per-client isolation of buffer allocations.
>
> **Reference:** `ggml-rpc.cpp` protocol analysis (llama.cpp source)

---

## A) Problem Statement

**Current state (N servers → 1 RPC):**
- N independent TCP connections to RPC server
- Each server loads the same model, allocates N copies of buffers on RPC VRAM
- N× VRAM wasted (e.g., 3× for 50GB model = 150GB instead of 50GB)
- Compute serialized anyway (RPC server is single-threaded)

**Goal:**
- 1 TCP connection to RPC server from the proxy
- All N llama-servers share the same model buffers on RPC VRAM
- Per-client buffer tracking so ALLOC_BUFFER results are routed correctly
- Round-robin or fair scheduling of GRAPH_COMPUTE across clients
- Transparent to each llama-server — no llama.cpp changes needed

---

## B) Protocol Deep-Dive

### Message Format

```
REQUEST  : [cmd:1][size:8][data:size]
RESPONSE : [size:8][data:size]
```

Each command is self-contained. No persistent session state beyond the socket connection.

### Command Flow per llama-server

```
1. HELLO          → negotiate protocol version + transport caps
2. ALLOC_BUFFER   → allocate VRAM buffer (returns remote_ptr + remote_size)
3. SET_TENSOR     → push tensor data to allocated buffer
4. GRAPH_COMPUTE  → run inference (sends serialized graph)
5. GET_TENSOR     → read results back (for evaluation/decoding)
6. FREE_BUFFER    → release VRAM (on shutdown)
```

### Key Challenge: Buffer Ownership

When client A calls `ALLOC_BUFFER(1024MB)`, the RPC server allocates 1GB of VRAM and returns `remote_ptr=0x7fff1000`. If client B then calls `ALLOC_BUFFER(512MB)`, it gets a DIFFERENT buffer at `remote_ptr=0x7fff2000`.

For multiplexing, the proxy needs to track: **which client owns which buffer**. If client A's tensor references `remote_ptr=0x7fff1000`, the proxy must ensure that when the GRAPH_COMPUTE arrives for client A, it uses the correct buffer mapping.

### Solution: Client-Side Buffer Table

Each client maintains its OWN `alloc_buffer` table (client-side), mapping logical buffer IDs to remote_ptr values. When the proxy multiplexes, it:

1. Receives `ALLOC_BUFFER(1024MB)` from Client A
2. Forwards to RPC server → gets back `remote_ptr=0x7fff1000`
3. Remembers: `client=A, buffer_id=1, remote_ptr=0x7fff1000`
4. When Client A later sends `GRAPH_COMPUTE` with tensors referencing `buffer_id=1`, proxy transparently routes it

The llama.cpp client-side code already maintains this mapping internally (`ggml_backend_rpc_buffer_context.remote_ptr`). The proxy just needs to multiplex the raw socket bytes without interpreting tensor data — the tensor `id` field is a client-local ID, not a global ID.

### Critical Realization: Tensor IDs are Client-Local

Looking at line 1370 in `ggml-rpc.cpp`:
```cpp
tensor_ptrs.emplace(tensors[i].id, &tensors[i]);
```

The `rpc_tensor.id` is generated client-side during graph serialization. Different clients can use the same tensor IDs — they're scoped to each llama-server instance. The proxy does NOT need to translate tensor IDs; it just forwards the raw message bytes.

**This makes multiplexing trivial:** the proxy is a **transparent byte-level multiplexer**. It doesn't need to understand the protocol semantics at all — it just needs to track which client sent which request and route responses back correctly.

---

## C) Architecture

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│ llama    │     │ llama    │     │ llama    │
│ server A │     │ server B │     │ server C │
└────┬─────┘     └────┬─────┘     └────┬─────┘
     │ TCP            │ TCP            │ TCP
     ▼                ▼                ▼
┌──────────────────────────────────────────┐
│           Multiplexing Proxy             │
│                                          │
│  Client A socket ←──→ [queue A] ──┐      │
│  Client B socket ←──→ [queue B] ──┤      │
│  Client C socket ←──→ [queue C] ──┤      │
│                                   ▼      │
│                        [shared RPC conn]  │
│                        → RPC server        │
│                                          │
│  Response router (by request ID)         │
└──────────────────────────────────────────┘
                     │ TCP
                     ▼
            ┌─────────────────┐
            │  RPC Server     │
            │  (single conn)  │
            └─────────────────┘
```

### Components

1. **Connection Manager**: Accepts connections from N llama-servers, each gets its own socket.
2. **Request Tracker**: Assigns unique request IDs to each incoming RPC command, tracks pending responses.
3. **Round-Robin Scheduler**: Queues GRAPH_COMPUTE requests from all clients, dispatches one at a time on the shared RPC connection.
4. **Response Router**: Matches incoming RPC responses back to the originating client using request IDs.

---

## D) Request/Response Multiplexing Design

### The Problem

The RPC protocol is synchronous: send request → wait for response → next request. With N clients, we can't have each client doing this independently because there's only 1 connection.

### Solution: Proxy-Managed Request Queue

```python
class RpcProxy:
    def __init__(self, rpc_endpoint):
        self.rpc_conn = connect(rpc_endpoint)  # single connection
        self.clients = {}  # client_id → socket
        self.pending = {}  # request_id → {client_id, callback}
        self.request_counter = 0

    def handle_client_connection(self, client_id, client_sock):
        """New llama-server connects."""
        self.clients[client_id] = client_sock
        # Start client read loop
        spawn_thread(self.read_client_requests, client_id)

    def read_client_requests(self, client_id):
        """Read raw RPC commands from client socket."""
        while True:
            cmd, size, data = recv_rpc_command(client_sock)
            self.request_counter += 1
            req_id = self.request_counter
            
            # Queue this request for processing
            queue.append({
                'req_id': req_id,
                'client_id': client_id,
                'cmd': cmd,
                'size': size,
                'data': data,
            })
            
            # If nothing is pending on the RPC connection, dispatch now
            if not self.pending:
                self.dispatch_next()

    def dispatch_next(self):
        """Send next queued request to RPC server."""
        if not queue:
            return
        req = queue.pop(0)
        
        # Send to RPC server
        send_rpc_command(self.rpc_conn, req['cmd'], req['data'])
        
        # Register pending response handler
        self.pending[req['req_id']] = {
            'client_id': req['client_id'],
            'expected_size': get_response_size(req['cmd']),
        }

    def read_rpc_response(self):
        """Read response from RPC server, route back to client."""
        while True:
            resp_size = recv(self.rpc_conn, 8)
            resp_data = recv(self.rpc_conn, resp_size)
            
            # Find which request this responds to
            req_id = self.find_matching_request(resp_data)
            
            # Route response back to correct client
            client_id = self.pending[req_id]['client_id']
            send_to_client(client_id, resp_size, resp_data)
            
            del self.pending[req_id]
            
            # Dispatch next queued request
            self.dispatch_next()
```

### Response Matching Strategy

Since the RPC protocol doesn't have explicit request IDs in the message format, we need to track responses by **FIFO ordering**:

- The proxy sends requests in order
- Responses come back in the SAME order (protocol is synchronous)
- The proxy maintains a queue of pending requests; each incoming response matches the head of the queue

**This works because:**
1. Each command has a known response size (from `send_rpc_cmd` overload signatures)
2. The protocol is strictly sequential — no pipelining
3. The RPC server processes one command at a time and sends response immediately

**Exception: `GRAPH_COMPUTE` has no fixed-size response** — it's a fire-and-forget command (the response is just an empty ack). The proxy handles this by sending the request and immediately dispatching the next queued request (fire-and-forward pattern).

---

## E) Buffer Allocation Coordination

### Challenge

When Client A allocates a buffer, the RPC server returns a `remote_ptr`. When Client B later sends a GRAPH_COMPUTE with tensors referencing buffers allocated by Client A's session, the proxy needs to know whether those buffer refs are:
1. **Own buffers** (allocated by Client B in its own ALLOC_BUFFER sequence) — use as-is
2. **Shared model buffers** (allocated when the model was first loaded) — these should map to the SAME remote_ptr

### Solution: First-Owner Wins for Model Tensors

For the typical llama-server use case:
1. The FIRST llama-server to connect loads the model (ALLOC_BUFFER + SET_TENSOR for all tensors)
2. Subsequent clients connect, receive the model config from their GGUF file
3. When subsequent clients send GRAPH_COMPUTE with tensor refs that match the first client's tensor names, those map to the SAME buffers

**Implementation:** The proxy maintains a **buffer registry**:
```python
class BufferRegistry:
    # Maps tensor name patterns → remote_ptr (first owner wins)
    model_buffers = {}  # {"blk.0.ffn_up_exps.*": 0x7fff1000, ...}
    
    def on_alloc_buffer(self, client_id, size, remote_ptr):
        """Register new buffer allocation."""
        if not self.has_model_tensors(remote_ptr):
            # Not a model tensor — register in client's private table
            self.client_buffers[client_id].append({
                'remote_ptr': remote_ptr,
                'size': size,
            })
    
    def on_set_tensor(self, client_id, tensor_name, remote_ptr, data):
        """Register tensor → buffer mapping."""
        if tensor_name in self.model_buffers:
            # Tensor name already known — this is a re-send of model data
            pass  # Server already has the data; ignore duplicate send
        else:
            self.model_buffers[tensor_name] = remote_ptr
            self.client_tensors[client_id][tensor_name] = remote_ptr
```

**Practical simplification:** Since each llama-server independently loads the model from its own GGUF file and sends tensor data via SET_TENSOR, the RPC server will have **multiple copies** of the same tensors unless the proxy deduplicates. The simplest approach:

- **Proxy level 1 (no tensor dedup):** All N servers load full model → N× VRAM. The proxy only handles connection multiplexing. This is still useful for quickrobot — we control which server "owns" the model loading by sequencing.
- **Proxy level 2 (tensor dedup):** Proxy intercepts SET_TENSOR commands, compares tensor names/hash, skips duplicate sends. More complex but saves VRAM.

**Recommendation:** Start with Level 1 (no tensor dedup). The VRAM savings come from sharing the GRAPH_COMPUTE execution, not the model storage. For MOE expert split, the experts are ALLOC_BUFFER + SET_TENSOR'd per-request during graph_compute — if we share that execution, the VRAM benefit is automatic.

---

## F) Scheduling Strategy

### Round-Robin (simplest)

```python
class RoundRobinScheduler:
    def __init__(self, clients):
        self.clients = list(clients)
        self.index = 0
    
    def next_client(self):
        client = self.clients[self.index]
        self.index = (self.index + 1) % len(self.clients)
        return client
```

**Pros:** Simple, fair, no starvation.
**Cons:** May not account for client load differences.

### Weighted Round-Robin (better for heterogeneous clients)

Assign weights based on client priority or capacity:
- Client A (high priority): weight 3
- Client B, C (low priority): weight 1

### Per-Command Queues

Each client has its own queue. The scheduler picks from the highest-priority non-empty queue.

**Recommendation:** Simple round-robin for v0.1. It's proven correct for equal-load scenarios and easy to implement.

---

## G) Implementation Plan

### Phase 1: Basic Multiplexer (Python proxy)

```
File: lib/lib_rpc_proxy.py (~300 lines)

Classes:
- RpcProxyClient: manages one llama-server connection
- RpcProxyServer: listens for incoming client connections  
- RpcProxyScheduler: round-robin dispatcher
- RpcChannel: the shared RPC connection + request/response tracker

Features:
- Accept N TCP connections from llama-servers
- Each gets its own socket, handled in separate thread
- Incoming commands queued per-client
- Round-robin dispatch to shared RPC connection
- Responses routed back to originating client
```

### Phase 2: Buffer Registry

```python
# Track ALLOC_BUFFER + SET_TENSOR to prevent duplicate model loads
# Match tensor names via regex pattern comparison
# Skip duplicate SET_TENSOR sends for known model tensors
```

### Phase 3: Health + Reconnection

```python
# Detect client disconnect → clean up queue
# Detect RPC server crash → reconnect all clients
# Graceful shutdown → drain queues, close connections
```

### Phase 4: Quickrobot Integration

```python
# New MCP tool: rpc_proxy_start(rpc_endpoint, client_count)
# New MCP tool: rpc_proxy_stop()
# WebUI section on herd page: "RPC Proxy" status card
# Auto-start when cluster has >1 llama-server + <N RPC nodes
```

---

## H) Limitations and Trade-offs

| Aspect | Current (N connections) | Proxy (1 connection) |
|--------|----------------------|---------------------|
| VRAM usage | N× model size | N× model (Level 1) or 1× (Level 2) |
| Compute throughput | 1× GPU (serialized) | 1× GPU (serialized) — same |
| Latency per request | Direct | +proxy overhead (~0.1ms) |
| Failure mode | 1 client crash = 1 connection lost | RPC crash = ALL clients affected |
| Complexity | None | Proxy process needs health management |

**Key insight:** The proxy doesn't improve compute throughput (RPC is single-threaded regardless). It improves **resource sharing efficiency** — same model data, fewer TCP connections, centralized management.

For the MOE expert split use case specifically:
- Mode S with 3 RPC servers × 3 llama-servers = 9 TCP connections
- With proxy: 3 proxy instances (1 per RPC), each multiplexing 3 clients = 3 TCP connections to RPC side + 9 client connections
- Net: same client-side connections, fewer server-side connections

**Best use case:** Many clients (10+) → few RPC nodes. The proxy shines when the bottleneck is RPC server connection management, not compute throughput.
