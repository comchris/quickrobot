# RPC Multiplexing Proxy — Implementation Suggestion

> **Purpose:** Detailed implementation spec for the multiplexing proxy that shares expert weights across multiple llama-servers connected to one RPC instance.
>
> **Design doc:** `tools/rpc_multiplexing_proxy_v010.md`

---

## A) File Naming Schema from Disk Cache

The disk cache uses FNV-1a hash of tensor data → 16-char hex filename:

```cpp
uint64_t hash = fnv_hash((const uint8_t*)data, size);
snprintf(hash_str, sizeof(hash_str), "%016" PRIx64, hash);
fs::path cache_file = fs::path(cache_dir) / hash_str;  // e.g., "cbf29ce484222325"
```

**Key observations:**
- Hash is over tensor **data**, not tensor name
- Filename is 16 chars, hex, no extension
- Tensor data includes the full tensor blob (weights, dimensions, offsets)
- Same tensor data → same hash → same file

**For proxy use:** The tensor `name` field (from `rpc_tensor.name`, 64-char string) is more useful than data hash for matching because:
1. Two clients loading the same model will have IDENTICAL tensor names
2. Tensor names are stable (always "blk.0.ffn_up_exps.*", never change)
3. Name-based matching works even if tensor data sizes differ slightly

**Recommended approach:** Use tensor name as the matching key for shared buffers, NOT data hash. The name uniquely identifies the logical tensor within the model.

---

## B) Proxy Architecture

### Multi-Port Listening

The proxy listens on multiple ports, one per client:

```
Proxy configuration:
  base_port: 50052 (default — same as RPC default)
  max_connections: 4 (default)
  effective_ports: 50052, 50053, 50054, 50055

Client A → connect to proxy port 50052
Client B → connect to proxy port 50053
Client C → connect to proxy port 50054
```

Each client connects to a unique port. The proxy uses the port number as the client identifier internally.

### Connection Lifecycle

```
1. Proxy starts, listens on ports [base_port .. base_port+max-1]
2. Client connects to one of those ports
3. Proxy accepts connection, assigns client_id (derived from port offset)
4. Client performs HELLO → ALLOC_BUFFER → SET_TENSOR → GRAPH_COMPUTE sequence
5. When client disconnects: proxy frees that client's buffer entries
6. Port becomes available for new clients
```

### Data Structures

```python
class RpcProxyClient:
    """Represents one connected llama-server instance."""
    def __init__(self, client_id, socket):
        self.client_id = client_id      # 0, 1, 2, ... (from port offset)
        self.socket = socket            # TCP connection to this client
        self.allocated_buffers = {}     # tensor_name → remote_ptr (this client's buffers)
        self.is_active = True

class RpcProxy:
    """Main proxy instance — multiplexes N clients over 1 RPC connection."""
    def __init__(self, base_port=50052, max_connections=4, rpc_endpoint=None):
        self.base_port = base_port
        self.max_connections = max_connections
        self.rpc_endpoint = rpc_endpoint
        
        # Port → client mapping
        self.clients = {}  # client_id → RpcProxyClient
        
        # Shared buffer registry — first allocation wins
        self.shared_buffers = {}  # tensor_name → remote_ptr
        
        # Request tracking for serialization
        self.pending_requests = []  # ordered queue of pending requests
        self.current_request = None  # request currently being processed on RPC
        
        # Logging
        self.verbose = False
        self.log_file = "/tmp/rpc_proxy.log"
        self.max_log_size = 10 * 1024 * 1024  # 10 MB
    
    def start(self):
        """Start listening on configured ports."""
        for i in range(self.max_connections):
            port = self.base_port + i
            sock = socket.create_server(("0.0.0.0", port))
            spawn_thread(self.accept_client, sock, client_id=i)
        
        # Start main RPC loop
        self.rpc_conn = connect(self.rpc_endpoint)
        negotiate_hello(self.rpc_conn)
        spawn_thread(self.handle_rpc_responses)
    
    def accept_client(self, listener_sock, client_id):
        """Accept new client connection on this port."""
        while True:
            sock = listener_sock.accept()
            if sock is None:
                break
            
            # Check if we're at capacity
            if len([c for c in self.clients.values() if c.is_active]) >= self.max_connections:
                # Reject — send error and close
                sock.close()
                continue
            
            client = RpcProxyClient(client_id, sock)
            self.clients[client_id] = client
            
            # Start client read loop in new thread
            spawn_thread(self.read_client_commands, client)
```

---

## C) Command Interception Logic

### 1. HELLO (pass-through)

Forward to RPC server as-is. No state tracking needed.

```python
def handle_hello(self, client, data):
    response = send_rpc_cmd(self.rpc_conn, HELLO, data)
    client.socket.send(response)
    log("HELLO client=%d" % client.client_id)
```

### 2. ALLOC_BUFFER (intercept — key operation)

This is where shared buffer logic lives. The proxy intercepts allocations and checks if the same buffer was already allocated by another client.

**Challenge:** `ALLOC_BUFFER` request only contains `device` and `size`, not tensor name. We need to match via `SET_TENSOR` which DOES include tensor name.

**Solution:** Two-phase approach:
- Phase 1: Proxy sees ALLOC_BUFFER → stores it in client's buffer table, forwards to RPC
- Phase 2: Proxy sees SET_TENSOR for this tensor → checks if tensor was already loaded by another client
  - If yes: mark this allocation as "shared", remap future refs
  - If no: first load, forward normally

```python
def handle_alloc_buffer(self, client, request):
    """Intercept ALLOC_BUFFER, track in client's buffer table."""
    response = send_rpc_cmd(self.rpc_conn, ALLOC_BUFFER, request)
    
    # Register buffer for this client
    buffer_id = f"buf_{request.device}_{request.size}"
    client.allocated_buffers[buffer_id] = response.remote_ptr
    
    log("ALLOC_BUFFER client=%d size=%d ptr=0x%x" % (
        client.client_id, request.size, response.remote_ptr))
    
    client.socket.send(response)
```

### 3. SET_TENSOR / SET_TENSOR_HASH (intercept — shared buffer detection)

This is where we detect that a tensor was already loaded by another client.

```python
def handle_set_tensor(self, client, request):
    """Intercept SET_TENSOR, detect shared model tensors."""
    tensor = deserialize_rpc_tensor(request.tensor)
    tensor_name = tensor.name  # e.g., "blk.0.ffn_up_exps.*"
    
    # Check if this tensor was already loaded by another client
    if tensor_name in self.shared_buffers:
        # Already loaded — skip sending data to RPC server
        # The buffer is shared; RPC server already has the data
        
        # Remap: use the existing remote_ptr instead of client's new allocation
        existing_ptr = self.shared_buffers[tensor_name]
        
        # Update client's buffer table to point to shared buffer
        buffer_id = f"buf_{request.tensor.buffer >> 32}_{tensor_name}"
        client.allocated_buffers[buffer_id] = existing_ptr
        
        log("SET_TENSOR SHARED tensor=%s ptr=0x%x (shared with client %d)" % (
            tensor_name, existing_ptr,
            self._find_buffer_owner(tensor_name)))
        
        # Return success without actually sending data to RPC
        return send_msg(client.socket, response_size, response_data)
    
    # First load — forward to RPC server normally
    response = send_rpc_cmd(self.rpc_conn, SET_TENSOR, request)
    
    # Register as shared for future clients
    self.shared_buffers[tensor_name] = client.allocated_buffers.get(
        f"buf_{request.tensor.buffer >> 32}_{tensor_name}")
    
    log("SET_TENSOR first_load tensor=%s ptr=0x%x" % (tensor_name, existing_ptr))
    
    return send_msg(client.socket, response)
```

### 4. GRAPH_COMPUTE (intercept — serialization queue)

GRAPH_COMPUTE is fire-and-forget (no response), so we need special tracking.

```python
def handle_graph_compute(self, client, request):
    """Queue GRAPH_COMPUTE for serialized execution."""
    # Parse tensor names from the graph compute request for logging
    tensors = parse_tensors_from_request(request)
    tensor_names = [t.name for t in tensors if t.name]
    
    req_id = self._next_request_id()
    queued_req = {
        'req_id': req_id,
        'client_id': client.client_id,
        'cmd': GRAPH_COMPUTE,
        'data': request,
        'tensor_names': tensor_names[:5],  # log first 5 tensor names
    }
    
    self.pending_requests.append(queued_req)
    log("GRAPH_COMPUTE queued client=%d tensors=%s" % (
        client.client_id, tensor_names[:3]))
    
    # If nothing is currently being processed on RPC, dispatch this request
    if self.current_request is None:
        self._dispatch_next()
```

### 5. GRAPH_RECOMPUTE (pass-through)

Forward as-is. No shared state impact.

---

## D) Response Routing Logic

GRAPH_COMPUTE has no response, so the main concern is matching ALLOC_BUFFER and SET_TENSOR responses back to the correct client.

### Simple FIFO Queue (sufficient for current protocol)

Since the RPC protocol is strictly sequential:
1. Client sends command → proxy receives on its connection
2. Proxy processes (intercepts or forwards) → sends response back
3. Client receives response → sends next command

The proxy doesn't need request IDs because there's no pipelining — each response matches the last sent command for that client.

**Exception:** When multiple clients send commands simultaneously, the proxy interleaves them on the RPC connection:

```
Client A sends ALLOC_BUFFER #1     → proxy queues it
Client B sends ALLOC_BUFFER #2     → proxy queues it
Proxy dispatches A's request to RPC → waits for response → routes to A
Proxy dispatches B's request to RPC → waits for response → routes to B
```

### Response Routing Function

```python
def route_response(self, client_id, response_data):
    """Send RPC response back to the correct client."""
    client = self.clients.get(client_id)
    if client and client.is_active:
        client.socket.send(response_data)
        log("Response routed to client=%d (%d bytes)" % (client_id, len(response_data)))
    else:
            log("WARNING: client=%d not found or inactive" % client_id)
```

### D.1) Model Fingerprinting (Same-Model Verification)

Before sharing buffers, the proxy must verify all clients use the same model. The `rpc_tensor` struct includes `name[64]`, `ne[4]` dimensions, and `type` — all flow through SET_TENSOR and GRAPH_COMPUTE.

**Fingerprint algorithm:**
```python
import hashlib

def compute_fingerprint(client_tensors):
    """Deterministic fingerprint from tensor metadata."""
    parts = []
    for t in sorted(client_tensors, key=lambda x: x.name):
        parts.append("%s:%d,%d,%d,%d:%d" % (
            t.name, t.ne[0], t.ne[1], t.ne[2], t.ne[3], t.type))
    fp_string = "\n".join(parts)
    return hashlib.sha256(fp_string.encode()).hexdigest()[:16]
```

**Proxy integration:**
- Client A: first SET_TENSOR → proxy computes fingerprint → stores as baseline
- Client B: first SET_TENSOR → proxy computes fingerprint → compares with baseline
  - Match → share buffers (same model)
  - Mismatch → reject sharing, forward normally (different models)

**Why SHA256 of sorted tensor names+shapes:**
- Different models → different tensor sets → different fingerprint
- Same model + same quantization → identical fingerprint
- Same model + different quantization → different `type` field → different fingerprint
- Deterministic → reproducible across clients

```python
def handle_set_tensor(self, client, request):
    """Intercept with model verification."""
    tensor = deserialize_rpc_tensor(request.tensor)
    
    # Extract/verify model fingerprint
    is_match, msg = self.extract_model_fingerprint(client.client_id)
    
    if msg == "model_mismatch":
        log("REJECT shared buffers client=%d (model mismatch)" % client.client_id)
        return forward_to_rpc(client, request)  # own buffers
    
    if tensor.name in self.buffer_registry:
        ptr = self.buffer_registry[tensor.name]
        remap_client_buffer(client, tensor.name, ptr)
        log("SET_TENSOR SHARED tensor=%s ptr=0x%x" % (tensor.name, ptr))
        return skip_duplicate_send(client, request)
    
    response = send_rpc_cmd(self.rpc_conn, SET_TENSOR, request)
    self.buffer_registry[tensor.name] = response.remote_ptr
    return forward_to_client(client, response)
```

---

## E) Buffer Sharing State Machine

```
Phase 1: Initial Load
─────────────────────
Client A connects → port 50052 (client_id=0)
  ALLOC_BUFFER(blk.0.ffn_up, 1GB) → proxy: shared_buffers["blk.0.ffn_up"] = 0xA
  SET_TENSOR(blk.0.ffn_up, data, ptr=0xA) → proxy: tensor loaded on RPC
  
Phase 2: Sharing
────────────────
Client B connects → port 50053 (client_id=1)
  ALLOC_BUFFER(blk.0.ffn_up, 1GB) → proxy: "blk.0.ffn_up" exists!
    → remap response: return 0xA instead of new allocation (0xB)
    → client B now references 0xA (same buffer as A!)
  SET_TENSOR(blk.0.ffn_up, data, ptr=0xA) → proxy: tensor already on RPC
    → skip sending duplicate data
    → log: "SHARED" instead of "first_load"
  
Phase 3: Compute
────────────────
Client B sends GRAPH_COMPUTE(tensor ref 0xA)
  → proxy queues on RPC connection
  → RPC server processes using shared buffer at 0xA ✓

Phase 4: Client Disconnect
─────────────────────────
Client A disconnects
  → proxy: free client_a.allocated_buffers
  → shared_buffers remains (Client B still needs it)
  
Client B disconnects
  → proxy: free client_b.allocated_buffers
  → shared_buffers.clear() — all buffers can be reused
```

---

## F) Logging System

### Console Output (-v / --verbose)

```bash
rpc_proxy --base-port 50052 --max-connections 4 --rpc-endpoint rpc-node.lan:50052 -v
```

When `-v` is set, all function calls are printed to stdout:

```
[12:34:56] INIT base_port=50052 max_conn=4 rpc_endpoint=<rpc-host>:50052
[12:34:56] LISTENING on ports 50052-50055
[12:34:57] ACCEPTED client_id=0 from port 50052
[12:34:57] HELLO client_id=0 proto=0.1.0
[12:34:58] ALLOC_BUFFER client_id=0 size=1073741824 ptr=0x7fff1000
[12:34:58] SET_TENSOR first_load tensor="blk.0.ffn_up_exps.*" ptr=0x7fff1000
[12:34:58] GRAPH_COMPUTE queued client_id=0 tensors=["blk.0.ffn_up_exps.*", ...]
[12:34:59] ACCEPTED client_id=1 from port 50053
[12:34:59] HELLO client_id=1 proto=0.1.0
[12:34:59] ALLOC_BUFFER client_id=1 size=1073741824 ptr=0x7fff1000 (SHARED with client 0)
[12:34:59] SET_TENSOR SHARED tensor="blk.0.ffn_up_exps.*" ptr=0x7fff1000
```

### File Logging (/tmp/rpc_proxy.log, size-limited)

All log entries (verbose or not) go to `/tmp/rpc_proxy.log`:
- Rotates at 10 MB
- Keeps last rotated file as `.log.1`
- Uses Python's `logging.handlers.RotatingFileHandler`

```python
import logging
from logging.handlers import RotatingFileHandler

def setup_logging(verbose=False):
    logger = logging.getLogger("rpc_proxy")
    logger.setLevel(logging.DEBUG)
    
    # Console handler
    if verbose:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(logging.Formatter('[%(asctime)s] %(message)s'))
        logger.addHandler(ch)
    
    # File handler (always active)
    fh = RotatingFileHandler(
        '/tmp/rpc_proxy.log',
        maxBytes=10*1024*1024,  # 10 MB
        backupCount=1
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(fh)
    
    return logger
```

---

## G) Deployment on Remote Nodes

### File Structure

```
/opt/rpc_proxy/          # installed via playbook
├── rpc_proxy.py         # main proxy script
├── requirements.txt     # Python deps (only stdlib — no pip needed)
└── config.json          # runtime config (base_port, max_connections, rpc_endpoint)
```

### Quickrobot Playbook Integration

```yaml
# playbooks/node/deploy_rpc_proxy.yml
- name: Deploy RPC multiplexing proxy
  hosts: rpc_nodes
  become: yes
  tasks:
    - name: Copy proxy script
      copy:
        src: ../templates/rpc_proxy.py.j2
        dest: /opt/rpc_proxy/rpc_proxy.py
        mode: "0755"
    
    - name: Create config
      copy:
        content: |
          {
            "base_port": 50052,
            "max_connections": 4,
            "rpc_endpoint": "{{ item.rpc_host }}:{{ item.rpc_port }}"
          }
        dest: /opt/rpc_proxy/config.json
    
    - name: Start proxy service
      systemd:
        name: rpc-proxy
        state: started
        enabled: yes
```

### Systemd Unit File Template

```ini
[Unit]
Description=RPC Multiplexing Proxy for llama.cpp
After=network.target

[Service]
Type=simple
User=<SSH_USER>
ExecStart=/opt/rpc_proxy/rpc_proxy.py --config /opt/rpc_proxy/config.json -v
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

## H) Implementation Phases

### Phase 1: Core Proxy (MVP) — ~400 lines Python
- Multi-port listener (base_port + max_connections)
- Connection acceptance with client_id assignment
- Pass-through HELLO, ALLOC_BUFFER, SET_TENSOR, GRAPH_COMPUTE
- Basic FIFO command queuing on single RPC connection
- Console logging + file logging with rotation

### Phase 2: Shared Buffer Logic — ~150 lines
- Tensor name extraction from SET_TENSOR commands
- shared_buffers registry (first-wins allocation)
- Response remapping for duplicate ALLOC_BUFFER
- SHARED/first_load logging markers

### Phase 3: Robustness — ~100 lines
- Client disconnect detection → buffer cleanup
- RPC connection failure → reconnect all clients
- Graceful shutdown (drain queues, close sockets)
- Health endpoint (HTTP GET /health → returns status JSON)

### Phase 4: Quickrobot Integration
- MCP tools: `rpc_proxy_start(endpoint, base_port, max_connections)`, `rpc_proxy_stop()`
- WebUI section on herd page showing proxy status, connected clients, shared buffers
- Auto-start when cluster configuration detects multiple llama-servers + single RPC node

**Total estimated lines:** ~650 Python (pure stdlib, no external deps)

---

## I) Testing Approach (Manual SSH)

Since we're deploying manually via SSH first:

```bash
# 1. Copy proxy to remote node
scp /path/to/rpc_proxy.py user@rpc_node:/tmp/

# 2. Start proxy on remote RPC node (pointing to the actual RPC server)
python3 /tmp/rpc_proxy.py \
  --base-port 50052 \
  --max-connections 4 \
   --rpc-endpoint <rpc-host>:50052 \
  -v

# 3. From each llama-server node, configure to use proxy instead of direct RPC
# Edit config or command line: change --rpc from <rpc-host>:50052 to proxy_host:50052

# 4. Verify shared buffers via logs:
# Look for "SHARED" markers in proxy console output

# 5. Monitor VRAM usage on RPC server:
ssh user@rpc_node "nvidia-smi --query-gpu=memory.used --format=csv,noheader"
```

**Expected behavior:** With 3 clients sharing 1 RPC instance, VRAM should show ~1× model size instead of 3×.
